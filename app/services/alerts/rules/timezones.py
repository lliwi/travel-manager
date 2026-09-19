"""Timezone shift rule (specification section 2.4).

"Cambios de zona horaria que afecten a la planificación." A meeting scheduled
for the morning after a nine-hour eastward shift is a planning problem even
though every individual booking is correct.
"""
from app.models.enums import AlertSeverity
from app.services.alerts.base import AlertRule, register
from app.utils.timeutil import timezone_shift_hours


@register
class TimezoneShiftRule(AlertRule):
    """Flags journeys crossing enough timezones to affect planning."""

    codigo = 'cambio_zona_horaria'
    nombre = 'Cambio de zona horaria relevante'
    severidad_por_defecto = AlertSeverity.INFORMATIVA

    def evaluate(self, ctx):
        threshold = float(self.setting(ctx, 'umbral_horas', 3) or 3)
        candidates = []

        for traveler_id, items in ctx.items_by_traveler.items():
            for segment in items['segmentos']:
                if not segment.salida_tz or not segment.llegada_tz:
                    continue
                if segment.salida_tz == segment.llegada_tz:
                    continue

                shift = timezone_shift_hours(
                    segment.salida_tz, segment.llegada_tz, segment.salida_utc
                )
                if abs(shift) < threshold:
                    continue

                direction = 'adelanta' if shift > 0 else 'atrasa'
                # Eastward travel is harder to adjust to, which is worth saying
                # rather than reporting a bare number of hours.
                consejo = (
                    'Los desplazamientos hacia el este suelen requerir más '
                    'adaptación. Evite citas exigentes la mañana siguiente a la '
                    'llegada.'
                    if shift > 0 else
                    'Los desplazamientos hacia el oeste se toleran mejor, aunque '
                    'conviene prever cansancio a última hora del día.'
                )

                candidates.append(self.candidate(
                    ctx,
                    titulo=f'Cambio horario de {abs(shift):.0f} h en «{segment.etiqueta}»',
                    mensaje=(
                        f'El viajero {direction} el reloj {abs(shift):.0f} horas al '
                        f'viajar de {segment.salida_tz} a {segment.llegada_tz}.'
                    ),
                    entidades=(segment.id,),
                    trip_traveler_id=traveler_id,
                    evidencia={
                        'segmento_id': str(segment.id),
                        'etiqueta': segment.etiqueta,
                        'zona_origen': segment.salida_tz,
                        'zona_destino': segment.llegada_tz,
                        'diferencia_horas': round(shift, 2),
                        'umbral_horas': threshold,
                    },
                    sugerencia=consejo,
                ))

        return candidates
