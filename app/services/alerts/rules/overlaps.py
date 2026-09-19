"""Overlapping booking rule (specification section 2.4)."""
from app.models.enums import AlertSeverity
from app.services.alerts.base import AlertRule, register
from app.utils.timeutil import format_duration, minutes_between, overlaps


@register
class OverlappingSegmentsRule(AlertRule):
    """Flags itinerary items of one traveller whose intervals overlap.

    A person cannot be on two flights at once, nor be checked into a hotel in
    Berlin while a segment has them in Lisbon. Both cases are the same defect and
    are reported together.
    """

    codigo = 'segmentos_solapados'
    nombre = 'Solapamiento de reservas'
    severidad_por_defecto = AlertSeverity.ALTA

    def evaluate(self, ctx):
        candidates = []
        tolerance = self.setting(ctx, 'tolerancia_min', 0)

        for traveler_id, items in ctx.items_by_traveler.items():
            intervals = self._intervals(items)
            for i, first in enumerate(intervals):
                for second in intervals[i + 1:]:
                    candidate = self._check_pair(
                        ctx, traveler_id, first, second, tolerance
                    )
                    if candidate is not None:
                        candidates.append(candidate)
        return candidates

    def _intervals(self, items):
        """Every dated item as ``(kind, entity, inicio_utc, fin_utc)``."""
        intervals = []
        for kind, collection in (
            ('segmento', items['segmentos']),
            ('alojamiento', items['alojamientos']),
            ('vehiculo', items['vehiculos']),
        ):
            for item in collection:
                inicio = item.inicio_utc
                fin = item.fin_utc
                if inicio is None or fin is None:
                    continue
                intervals.append((kind, item, inicio, fin))

        intervals.sort(key=lambda entry: entry[2])
        return intervals

    def _check_pair(self, ctx, traveler_id, first, second, tolerance):
        kind_a, item_a, start_a, end_a = first
        kind_b, item_b, start_b, end_b = second

        # A hotel stay and a vehicle rental legitimately span the same days --
        # the traveller is in the city with a car parked outside. Only transport
        # genuinely conflicts with everything else.
        if kind_a in ('alojamiento', 'vehiculo') and kind_b in ('alojamiento', 'vehiculo'):
            if kind_a != kind_b:
                return None

        if not overlaps(start_a, end_a, start_b, end_b):
            return None

        overlap_minutes = minutes_between(max(start_a, start_b), min(end_a, end_b))
        if overlap_minutes is None or overlap_minutes <= tolerance:
            return None

        evidencia = {
            'elemento_a': {
                'tipo': kind_a,
                'id': str(item_a.id),
                'etiqueta': item_a.etiqueta,
                'inicio_utc': start_a.isoformat(),
                'fin_utc': end_a.isoformat(),
            },
            'elemento_b': {
                'tipo': kind_b,
                'id': str(item_b.id),
                'etiqueta': item_b.etiqueta,
                'inicio_utc': start_b.isoformat(),
                'fin_utc': end_b.isoformat(),
            },
            'solape_minutos': overlap_minutes,
            'solape_legible': format_duration(overlap_minutes),
            'tolerancia_minutos': tolerance,
        }

        return self.candidate(
            ctx,
            titulo=f'Reservas solapadas ({format_duration(overlap_minutes)})',
            mensaje=(
                f'«{item_a.etiqueta}» y «{item_b.etiqueta}» se solapan durante '
                f'{format_duration(overlap_minutes)} para el mismo viajero.'
            ),
            entidades=(item_a.id, item_b.id),
            trip_traveler_id=traveler_id,
            evidencia=evidencia,
            sugerencia=(
                'Compruebe si una de las reservas fue modificada o cancelada y no '
                'se ha actualizado en el sistema.'
            ),
        )
