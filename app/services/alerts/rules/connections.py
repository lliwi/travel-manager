"""Connection margin rule.

Specification sections 2.4 and 5.3: consecutive transport segments whose gap is
below a configurable threshold raise an alert, with the margin computed in UTC
while respecting each endpoint's own timezone.

The specification gives 90 minutes for Schengen connections and 150 for
international ones but does not say what makes a connection Schengen. The rule
here decides it from the three countries the transfer touches -- where the
arriving leg came from, where the transfer happens, and where the departing leg
goes -- because that is what determines whether an external border is crossed.
An unknown country falls back to the stricter international threshold: guessing
"probably Schengen" would silently loosen a safety check.
"""
from app.models.enums import CONNECTABLE_SEGMENT_TYPES, AlertSeverity
from app.services.alerts.base import AlertRule, register
from app.services.itinerary_service import es_conexion
from app.utils.timeutil import format_duration, minutes_between


@register
class ConnectionMarginRule(AlertRule):
    """Flags connections whose margin is below the configured threshold."""

    codigo = 'conexion_margen_insuficiente'
    nombre = 'Margen de conexión insuficiente'
    severidad_por_defecto = AlertSeverity.ALTA

    def evaluate(self, ctx):
        candidates = []
        for traveler_id, items in ctx.items_by_traveler.items():
            segments = [
                s for s in items['segmentos']
                if s.tipo in CONNECTABLE_SEGMENT_TYPES
                and s.salida_utc is not None
                and s.llegada_utc is not None
            ]
            segments.sort(key=lambda s: s.salida_utc)

            # Consecutive pairs; the offset slice is shorter by one on purpose.
            for first, second in zip(segments, segments[1:], strict=False):
                candidate = self._check_pair(ctx, traveler_id, first, second)
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _check_pair(self, ctx, traveler_id, first, second):
        """Judge one consecutive pair."""
        margin = minutes_between(first.llegada_utc, second.salida_utc)

        # Same definition the timeline uses, so both call the same pairs
        # connections. It also excludes a negative margin, which means the
        # segments overlap: that is the overlap rule's finding, not this one's,
        # and reporting it twice would be noise.
        if not es_conexion(margin):
            return None

        threshold, motivo = self._threshold_for(ctx, first, second)
        if margin >= threshold:
            return None

        severity = self.severity(ctx)
        critico = self.setting(ctx, 'margen_critico_min', 30)
        if margin < critico:
            severity = AlertSeverity.CRITICA

        evidencia = {
            'segmento_llegada': {
                'id': str(first.id),
                'etiqueta': first.etiqueta,
                'llegada_local': (
                    first.llegada_local.isoformat() if first.llegada_local else None
                ),
                'llegada_tz': first.llegada_tz,
                'llegada_utc': first.llegada_utc.isoformat(),
                'destino': first.destino_codigo or first.destino_ciudad,
                'pais_destino': first.destino_pais,
                'terminal': first.terminal_destino,
            },
            'segmento_salida': {
                'id': str(second.id),
                'etiqueta': second.etiqueta,
                'salida_local': (
                    second.salida_local.isoformat() if second.salida_local else None
                ),
                'salida_tz': second.salida_tz,
                'salida_utc': second.salida_utc.isoformat(),
                'origen': second.origen_codigo or second.origen_ciudad,
                'pais_origen': second.origen_pais,
                'terminal': second.terminal_origen,
            },
            'margen_minutos': margin,
            'margen_legible': format_duration(margin),
            'umbral_minutos': threshold,
            'umbral_motivo': motivo,
            'mismo_lugar': self._same_place(first, second),
            'cambio_terminal': (
                bool(first.terminal_destino and second.origen_codigo
                     and first.terminal_destino != second.terminal_origen)
            ),
        }

        mensaje = (
            f'El margen entre «{first.etiqueta}» y «{second.etiqueta}» es de '
            f'{format_duration(margin)}, por debajo del umbral de '
            f'{format_duration(threshold)} aplicable ({motivo}).'
        )

        if not evidencia['mismo_lugar']:
            mensaje += (
                f' Además, la llegada es a {evidencia["segmento_llegada"]["destino"]} '
                f'y la salida desde {evidencia["segmento_salida"]["origen"]}, '
                'por lo que hay que contar también el desplazamiento entre ambos.'
            )

        return self.candidate(
            ctx,
            titulo=f'Conexión ajustada: {format_duration(margin)}',
            mensaje=mensaje,
            entidades=(first.id, second.id),
            trip_traveler_id=traveler_id,
            evidencia=evidencia,
            sugerencia=(
                'Revise la conexión con el proveedor: valore un vuelo posterior, '
                'una recogida de equipaje facturado más ágil o un traslado alternativo.'
            ),
            severidad=severity,
        )

    def _threshold_for(self, ctx, first, second):
        """Pick the threshold and explain why.

        Returns ``(minutos, motivo)``; the motive is stored in the evidence so a
        manager can see exactly which rule applied, as section 5.3 requires.

        The base thresholds are the ones the specification names: 90 minutes
        within Schengen, 150 when an external border is crossed. They already
        assume the ordinary case of connecting inside one airport -- which is
        what a connection is -- so there is no "same airport" discount. What
        *does* change the answer is a connection that is not in the same place
        at all: the traveller has to physically move between airports or
        stations, and that time has to come out of the margin.
        """
        from app.models.enums import SegmentType

        # A ground transfer is not an airport connection; it has its own, lower
        # threshold because there is no check-in or security to clear.
        if SegmentType.TRASLADO in (first.tipo, second.tipo):
            return self.setting(ctx, 'traslado_min', 30), 'traslado terrestre'

        countries = [first.origen_pais, first.destino_pais, second.destino_pais]
        conocidos = [c for c in countries if c]

        if len(conocidos) < len(countries):
            # An unknown country falls back to the stricter threshold. Guessing
            # "probably Schengen" would silently loosen a safety check.
            base = self.setting(ctx, 'internacional_min', 150)
            motivo = 'conexión internacional (país desconocido, umbral más exigente)'
        elif all(ctx.is_schengen(c) for c in conocidos):
            base = self.setting(ctx, 'schengen_min', 90)
            motivo = 'conexión Schengen'
        else:
            base = self.setting(ctx, 'internacional_min', 150)
            motivo = 'conexión internacional'

        # Different airport or station: add the time the transfer itself takes.
        if not self._same_place(first, second):
            extra = self.setting(ctx, 'cambio_aeropuerto_extra_min', 120)
            return base + extra, f'{motivo}, con cambio de aeropuerto o estación'

        # Same airport but a known terminal change: allow for the shuttle.
        if not self._same_terminal(first, second):
            extra = self.setting(ctx, 'cambio_terminal_extra_min', 30)
            return base + extra, f'{motivo}, con cambio de terminal'

        return base, motivo

    @staticmethod
    def _same_place(first, second):
        if first.destino_codigo and second.origen_codigo:
            return first.destino_codigo.upper() == second.origen_codigo.upper()
        if first.destino_ciudad and second.origen_ciudad:
            return (
                first.destino_ciudad.strip().lower()
                == second.origen_ciudad.strip().lower()
            )
        return False

    @staticmethod
    def _same_terminal(first, second):
        """Unknown terminals count as the same; we do not invent a transfer."""
        if not first.terminal_destino or not second.terminal_origen:
            return True
        return (
            first.terminal_destino.strip().lower()
            == second.terminal_origen.strip().lower()
        )
