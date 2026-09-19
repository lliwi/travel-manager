"""Traveller document expiry rule (specification section 2.4).

"Pasaportes, visados, seguros o requisitos documentales próximos a vencer, si
esos datos se habilitan." The conditional matters: the whole feature sits behind
``DOCUMENTOS_VIAJERO_HABILITADOS``, which is off by default, and this rule is
skipped entirely when it is.
"""
from app.models.enums import AlertSeverity
from app.services.alerts.base import AlertRule, register
from app.utils.timeutil import local_date


@register
class TravelDocumentExpiryRule(AlertRule):
    """Flags passports, visas or insurance expiring around the travel dates."""

    codigo = 'documento_viajero_por_caducar'
    nombre = 'Documento del viajero próximo a caducar'
    severidad_por_defecto = AlertSeverity.ALTA
    requiere_flags = ('DOCUMENTOS_VIAJERO_HABILITADOS',)

    def evaluate(self, ctx):
        if ctx.trip.fin_utc is None:
            return []

        # Many countries require a passport to remain valid for six months
        # beyond the date of entry, so expiry is measured against that, not
        # against the return date.
        months_required = int(self.setting(ctx, 'meses_validez_exigidos', 6) or 0)
        warn_days = int(self.setting(ctx, 'dias_aviso', 180) or 180)

        trip_end = local_date(ctx.trip.fin_utc, ctx.trip.fin_tz or 'UTC')
        if trip_end is None:
            return []

        candidates = []
        for traveler in ctx.travelers:
            for document in ctx.traveler_documents.get(traveler.user_id, []):
                candidate = self._check(
                    ctx, traveler, document, trip_end, months_required, warn_days
                )
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _check(self, ctx, traveler, document, trip_end, months_required, warn_days):
        if not document.activo or not document.fecha_caducidad:
            return None

        margin_days = (document.fecha_caducidad - trip_end).days
        required_days = months_required * 30

        if margin_days > max(required_days, 0) and margin_days > warn_days:
            return None

        if margin_days < 0:
            severidad = AlertSeverity.CRITICA
            titulo = f'{document.tipo.capitalize()} caducado durante el viaje'
            mensaje = (
                f'El {document.tipo} de {traveler.nombre_completo} caduca el '
                f'{document.fecha_caducidad.isoformat()}, antes del fin del viaje.'
            )
        elif margin_days < required_days:
            severidad = AlertSeverity.ALTA
            titulo = f'{document.tipo.capitalize()} con validez insuficiente'
            mensaje = (
                f'El {document.tipo} de {traveler.nombre_completo} caduca el '
                f'{document.fecha_caducidad.isoformat()}, {margin_days} días después '
                f'del fin del viaje. Muchos países exigen {months_required} meses '
                'de validez desde la fecha de entrada.'
            )
        else:
            severidad = AlertSeverity.INFORMATIVA
            titulo = f'{document.tipo.capitalize()} próximo a caducar'
            mensaje = (
                f'El {document.tipo} de {traveler.nombre_completo} caduca el '
                f'{document.fecha_caducidad.isoformat()}.'
            )

        return self.candidate(
            ctx,
            titulo=titulo,
            mensaje=mensaje,
            entidades=(document.id,),
            trip_traveler_id=traveler.id,
            evidencia={
                'documento_id': str(document.id),
                'tipo': document.tipo,
                'numero': (
                    f'****{document.numero_ultimos4}'
                    if document.numero_ultimos4 else None
                ),
                'pais_emisor': document.pais_emisor,
                'fecha_caducidad': document.fecha_caducidad.isoformat(),
                'fin_viaje': trip_end.isoformat(),
                'margen_dias': margin_days,
                'meses_validez_exigidos': months_required,
            },
            sugerencia=(
                'Confirme los requisitos de entrada del destino y, si procede, '
                'inicie la renovación del documento antes del viaje.'
            ),
            severidad=severidad,
        )
