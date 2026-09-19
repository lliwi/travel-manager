"""Lodging and vehicle rules (specification section 2.4)."""
from datetime import timedelta

from app.models.enums import AlertSeverity
from app.services.alerts.base import AlertRule, register
from app.utils.timeutil import format_duration, local_date, minutes_between


@register
class LateArrivalForBookingRule(AlertRule):
    """Flags a traveller arriving after their hotel check-in or car pick-up.

    "Llegada posterior al inicio de una reserva de hotel o vehículo" in section
    2.4. Some lateness is normal -- hotels hold rooms into the evening -- so a
    configurable tolerance keeps this from firing on every itinerary.
    """

    codigo = 'llegada_posterior_reserva'
    nombre = 'Llegada posterior al inicio de una reserva'
    severidad_por_defecto = AlertSeverity.MEDIA

    def evaluate(self, ctx):
        candidates = []
        tolerance = self.setting(ctx, 'margen_tolerancia_min', 60)

        for traveler_id, items in ctx.items_by_traveler.items():
            arrivals = sorted(
                (s for s in items['segmentos'] if s.llegada_utc is not None),
                key=lambda s: s.llegada_utc,
            )
            if not arrivals:
                continue

            for booking, kind, start_attr in (
                [(a, 'alojamiento', 'check_in_utc') for a in items['alojamientos']]
                + [(v, 'vehiculo', 'recogida_utc') for v in items['vehiculos']]
            ):
                start = getattr(booking, start_attr, None)
                if start is None:
                    continue

                candidate = self._check(
                    ctx, traveler_id, booking, kind, start, arrivals, tolerance
                )
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _check(self, ctx, traveler_id, booking, kind, start, arrivals, tolerance):
        """Find the arrival that should precede this booking, and compare."""
        # The relevant arrival is the last one landing before the booking would
        # normally end -- the leg that actually brings the traveller there.
        relevant = None
        for segment in arrivals:
            if booking.fin_utc is None or segment.llegada_utc <= booking.fin_utc:
                relevant = segment
        if relevant is None:
            return None

        late_by = minutes_between(start, relevant.llegada_utc)
        if late_by is None or late_by <= tolerance:
            return None

        etiqueta_tipo = 'el check-in del hotel' if kind == 'alojamiento' \
            else 'la recogida del vehículo'

        evidencia = {
            'reserva': {
                'tipo': kind,
                'id': str(booking.id),
                'etiqueta': booking.etiqueta,
                'inicio_utc': start.isoformat(),
                'inicio_local': _local_iso(booking, kind),
            },
            'llegada': {
                'id': str(relevant.id),
                'etiqueta': relevant.etiqueta,
                'llegada_utc': relevant.llegada_utc.isoformat(),
                'llegada_local': (
                    relevant.llegada_local.isoformat() if relevant.llegada_local else None
                ),
                'llegada_tz': relevant.llegada_tz,
            },
            'retraso_minutos': late_by,
            'retraso_legible': format_duration(late_by),
            'tolerancia_minutos': tolerance,
        }

        return self.candidate(
            ctx,
            titulo=f'Llegada {format_duration(late_by)} después de {etiqueta_tipo}',
            mensaje=(
                f'El viajero llega con «{relevant.etiqueta}» '
                f'{format_duration(late_by)} después de {etiqueta_tipo} '
                f'«{booking.etiqueta}».'
            ),
            entidades=(booking.id, relevant.id),
            trip_traveler_id=traveler_id,
            evidencia=evidencia,
            sugerencia=(
                'Avise al proveedor de la hora real de llegada para asegurar la '
                'reserva, o ajuste la hora de inicio.'
            ),
        )


def _local_iso(booking, kind):
    attr = 'check_in_local' if kind == 'alojamiento' else 'recogida_local'
    value = getattr(booking, attr, None)
    return value.isoformat() if value else None


@register
class UncoveredNightRule(AlertRule):
    """Flags a night of the trip with no lodging for a traveller.

    Only nights genuinely spent at the destination count: a night in the air on
    a long-haul flight is not a gap, and reporting it would train managers to
    ignore this alert.
    """

    codigo = 'noche_sin_alojamiento'
    nombre = 'Noche sin alojamiento'
    severidad_por_defecto = AlertSeverity.MEDIA

    def evaluate(self, ctx):
        trip = ctx.trip
        if trip.inicio_utc is None or trip.fin_utc is None:
            return []

        candidates = []
        for traveler_id, items in ctx.items_by_traveler.items():
            uncovered = self._uncovered_nights(ctx, trip, items)
            if not uncovered:
                continue

            candidates.append(self.candidate(
                ctx,
                titulo=(
                    f'{len(uncovered)} noche(s) sin alojamiento'
                    if len(uncovered) > 1 else 'Noche sin alojamiento'
                ),
                mensaje=(
                    'No hay alojamiento registrado para la(s) noche(s) del '
                    + ', '.join(night.isoformat() for night in uncovered)
                    + '.'
                ),
                entidades=(f'noches:{uncovered[0].isoformat()}',),
                trip_traveler_id=traveler_id,
                evidencia={
                    'noches': [n.isoformat() for n in uncovered],
                    'inicio_viaje': trip.inicio_utc.isoformat(),
                    'fin_viaje': trip.fin_utc.isoformat(),
                    'alojamientos': [
                        {'id': str(a.id), 'etiqueta': a.etiqueta}
                        for a in items['alojamientos']
                    ],
                },
                sugerencia=(
                    'Añada el alojamiento correspondiente o marque el viaje como '
                    'sin pernocta si procede.'
                ),
            ))
        return candidates

    def _uncovered_nights(self, ctx, trip, items):
        """Nights of the trip with no lodging and no overnight transport."""
        tz = trip.inicio_tz or 'UTC'
        first_night = local_date(trip.inicio_utc, tz)
        last_night = local_date(trip.fin_utc, tz)
        if not first_night or not last_night or last_night <= first_night:
            return []

        covered = set()
        for lodging in items['alojamientos']:
            if lodging.check_in_utc is None or lodging.check_out_utc is None:
                continue
            start = local_date(lodging.check_in_utc, lodging.check_in_tz or tz)
            end = local_date(lodging.check_out_utc, lodging.check_out_tz or tz)
            if not start or not end:
                continue
            night = start
            while night < end:
                covered.add(night)
                night += timedelta(days=1)

        # A segment spanning local midnight is an overnight journey; the
        # traveller is not sleeping in a bed, and that is not a defect.
        for segment in items['segmentos']:
            if segment.salida_utc is None or segment.llegada_utc is None:
                continue
            start = local_date(segment.salida_utc, segment.salida_tz or tz)
            end = local_date(segment.llegada_utc, segment.llegada_tz or tz)
            if start and end and end > start:
                night = start
                while night < end:
                    covered.add(night)
                    night += timedelta(days=1)

        uncovered = []
        night = first_night
        while night < last_night:
            if night not in covered:
                uncovered.append(night)
            night += timedelta(days=1)
        return uncovered
