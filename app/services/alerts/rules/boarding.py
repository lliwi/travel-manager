"""Check-in: the boarding pass nobody has asked for yet.

The booking exists from the moment somebody pays. The boarding pass only exists
once somebody checks in, and airlines open check-in around 48 hours before
departure and close it at the airport counter, hours before the gate. Between
those two moments there is a window in which everything looks finished and
nothing has been done, and the trip only turns out to be a problem at the
airport.

The other rules in this package describe something that is wrong with the trip
as planned. This one describes something that has not happened yet, which is
why it closes itself: the engine reconciles, so attaching the pass makes the
alert resolve on the next run without anybody having to tell it.
"""
from app.models.enums import AlertSeverity, DocumentType, SegmentType
from app.services.alerts.base import AlertRule, register
from app.utils.timeutil import as_aware_utc, utcnow


@register
class BoardingPassRule(AlertRule):
    """Flags flights departing soon with no boarding pass attached."""

    codigo = 'sin_tarjeta_de_embarque'
    nombre = 'Sin tarjeta de embarque'
    severidad_por_defecto = AlertSeverity.MEDIA

    def evaluate(self, ctx):
        horas = int(self.setting(ctx, 'horas_antes', 48) or 48)
        ahora = utcnow()

        pases = [
            d for d in ctx.documents
            if d.tipo is DocumentType.TARJETA_EMBARQUE
        ]

        candidates = []
        for segmento in ctx.segments:
            candidato = self._revisar(ctx, segmento, pases, ahora, horas)
            if candidato is not None:
                candidates.append(candidato)
        return candidates

    def _revisar(self, ctx, segmento, pases, ahora, horas):
        if segmento.tipo is not SegmentType.VUELO:
            # Only flights. A train ticket is the ticket, and a rule that
            # demanded a boarding pass for one would be noise on every journey.
            return None
        salida = as_aware_utc(segmento.salida_utc)
        if salida is None:
            return None

        # Normalised first: PostgreSQL returns an aware datetime and SQLite a
        # naive one, so subtracting «now» from it works in production and
        # raises in the tests.
        faltan = (salida - ahora).total_seconds() / 3600
        if faltan > horas:
            return None
        if faltan < 0:
            # Already departed. Whatever happened, happened, and an alert about
            # it now is a list item nobody can act on.
            return None

        if self._cubierto(segmento, pases):
            return None

        quien = self._nombre_del_viajero(ctx, segmento)
        restantes = int(faltan)

        return self.candidate(
            ctx,
            titulo=f'Sin tarjeta de embarque para {segmento.etiqueta}',
            mensaje=(
                f'El vuelo sale en {restantes} h y no hay ninguna tarjeta de '
                f'embarque adjunta{quien}. Es posible que nadie haya facturado.'
            ),
            entidades=(segmento.id,),
            trip_traveler_id=segmento.trip_traveler_id,
            evidencia={
                'segmento_id': str(segmento.id),
                'etiqueta': segmento.etiqueta,
                'numero': segmento.numero,
                'salida_utc': salida.isoformat(),
                'horas_para_la_salida': restantes,
                'umbral_horas': horas,
                'tarjetas_en_el_viaje': len(pases),
            },
            sugerencia=(
                'Facture en la web de la aerolínea y adjunte la tarjeta de '
                'embarque al viaje. Si ya está facturado, adjúntela igualmente '
                'para que conste.'
            ),
            severidad=self._severidad(restantes),
        )

    @staticmethod
    def _severidad(horas_restantes):
        """Closer to departure is more urgent, because it stops being fixable.

        Online check-in closes hours before the gate does, and after that the
        only remedy is a queue at the airport.
        """
        if horas_restantes <= 6:
            return AlertSeverity.ALTA
        return AlertSeverity.MEDIA

    @staticmethod
    def _cubierto(segmento, pases):
        """Whether any of these boarding passes belongs to this flight.

        The matching itself lives on the document, which is where the evidence
        is. What belongs here is the direction of every doubt: failing to match
        a pass raises an alert for a flight somebody has already checked in
        for, which is a nuisance, while matching the wrong one silences the
        alert for a flight nobody has checked in for -- the failure this rule
        exists to catch. So the match is narrow, and an outbound pass never
        answers for the return.
        """
        for pase in pases:
            if pase.cubre_segmento(segmento):
                return True
        return False

    @staticmethod
    def _nombre_del_viajero(ctx, segmento):
        if segmento.trip_traveler_id is None:
            return ''
        for viajero in ctx.travelers:
            if viajero.id == segmento.trip_traveler_id:
                return f' para {viajero.nombre_completo}'
        return ''
