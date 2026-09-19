"""Data completeness rules (specification section 2.4).

"Datos incompletos: ausencia de localizador, fechas, destino, viajero o
documento soporte."
"""
from app.models.enums import AlertSeverity, SegmentType
from app.services.alerts.base import AlertRule, register

#: Required fields per entity kind, as ``(atributo, etiqueta)``.
REQUIRED_FIELDS = {
    'segmento': (
        ('salida_utc', 'fecha y hora de salida'),
        ('llegada_utc', 'fecha y hora de llegada'),
        ('origen_codigo', 'origen'),
        ('destino_codigo', 'destino'),
    ),
    'alojamiento': (
        ('check_in_utc', 'fecha de entrada'),
        ('check_out_utc', 'fecha de salida'),
        ('nombre', 'nombre del alojamiento'),
    ),
    'vehiculo': (
        ('recogida_utc', 'fecha de recogida'),
        ('devolucion_utc', 'fecha de devolución'),
        ('proveedor', 'proveedor'),
    ),
}

#: Kinds whose booking reference is meaningful. A transfer or a restaurant
#: booking often has none, and demanding one would produce noise.
LOCATOR_EXPECTED = {'segmento', 'alojamiento', 'vehiculo'}


@register
class IncompleteDataRule(AlertRule):
    """Flags itinerary items missing data the itinerary depends on."""

    codigo = 'datos_incompletos'
    nombre = 'Datos incompletos'
    severidad_por_defecto = AlertSeverity.INFORMATIVA

    def evaluate(self, ctx):
        candidates = []
        require_locator = self.setting(ctx, 'exigir_localizador', True)

        for kind, collection in (
            ('segmento', ctx.segments),
            ('alojamiento', ctx.accommodations),
            ('vehiculo', ctx.vehicles),
        ):
            for item in collection:
                missing = self._missing_fields(kind, item, require_locator)
                if not missing:
                    continue

                candidates.append(self.candidate(
                    ctx,
                    titulo=f'Datos incompletos en «{item.etiqueta}»',
                    mensaje=(
                        f'Falta: {", ".join(label for _, label in missing)}.'
                    ),
                    entidades=(item.id,),
                    trip_traveler_id=item.trip_traveler_id,
                    evidencia={
                        'tipo': kind,
                        'id': str(item.id),
                        'etiqueta': item.etiqueta,
                        'campos_faltantes': [
                            {'campo': attr, 'etiqueta': label}
                            for attr, label in missing
                        ],
                    },
                    sugerencia=(
                        'Complete los datos manualmente o adjunte el documento de '
                        'reserva para que el sistema los extraiga.'
                    ),
                ))

        # A trip with no travellers assigned cannot be consulted by anyone,
        # which is a defect worth surfacing on its own.
        if not ctx.travelers:
            candidates.append(self.candidate(
                ctx,
                titulo='El viaje no tiene personas viajeras asignadas',
                mensaje=(
                    'Sin personas asignadas, nadie podrá consultar este viaje ni '
                    'sus documentos.'
                ),
                entidades=(f'trip:{ctx.trip.id}',),
                evidencia={'trip_id': str(ctx.trip.id)},
                sugerencia='Asigne al menos una persona viajera al viaje.',
            ))

        # Dates are what every other rule keys off; without them nothing can be
        # validated at all.
        if ctx.trip.inicio_utc is None or ctx.trip.fin_utc is None:
            candidates.append(self.candidate(
                ctx,
                titulo='El viaje no tiene fechas completas',
                mensaje=(
                    'Sin fechas de inicio y fin no es posible validar el '
                    'itinerario ni generar recomendaciones para el destino.'
                ),
                entidades=(f'trip_fechas:{ctx.trip.id}',),
                evidencia={
                    'inicio': (
                        ctx.trip.inicio_utc.isoformat() if ctx.trip.inicio_utc else None
                    ),
                    'fin': ctx.trip.fin_utc.isoformat() if ctx.trip.fin_utc else None,
                },
                sugerencia='Indique las fechas de inicio y fin del viaje.',
            ))

        return candidates

    def _missing_fields(self, kind, item, require_locator):
        """Which required fields this item is missing."""
        missing = []
        for attr, label in REQUIRED_FIELDS.get(kind, ()):
            if not getattr(item, attr, None):
                missing.append((attr, label))

        if require_locator and kind in LOCATOR_EXPECTED and not item.localizador:
            # Ground transfers rarely carry a booking reference.
            if not (kind == 'segmento' and item.tipo is SegmentType.TRASLADO):
                missing.append(('localizador', 'localizador de la reserva'))

        return missing


@register
class MissingSupportingDocumentRule(AlertRule):
    """Flags itinerary items with no document backing them.

    Off by default in effect: a manually entered segment is legitimate, so this
    only matters for organisations that require a supporting document for every
    service. The threshold ``exigir_documento`` on ``datos_incompletos`` governs
    whether it is treated as a problem.
    """

    codigo = 'sin_documento_soporte'
    nombre = 'Sin documento soporte'
    severidad_por_defecto = AlertSeverity.INFORMATIVA

    def evaluate(self, ctx):
        candidates = []
        for kind, collection in (
            ('segmento', ctx.segments),
            ('alojamiento', ctx.accommodations),
            ('vehiculo', ctx.vehicles),
        ):
            for item in collection:
                if item.documento_origen_id is not None:
                    continue

                candidates.append(self.candidate(
                    ctx,
                    titulo=f'«{item.etiqueta}» no tiene documento soporte',
                    mensaje=(
                        'Este servicio se introdujo manualmente y no hay ningún '
                        'documento que lo respalde.'
                    ),
                    entidades=(item.id,),
                    trip_traveler_id=item.trip_traveler_id,
                    evidencia={
                        'tipo': kind,
                        'id': str(item.id),
                        'etiqueta': item.etiqueta,
                    },
                    sugerencia=(
                        'Adjunte la confirmación de la reserva al viaje para dejar '
                        'constancia documental.'
                    ),
                ))
        return candidates
