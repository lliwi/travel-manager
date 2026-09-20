"""Corporate policy rules (specification section 9, phase 3).

These differ from every other rule in the file next door: the others describe
something that is true about a trip -- two flights overlap, a night has no
room -- and would be worth knowing in any organisation. These describe
something an organisation decided, and a different organisation would decide
otherwise.

That is why every one of them is off until a limit is set. A threshold of zero
means «no tenemos esa política», not «el límite es cero», and a rule with no
policy behind it should say nothing at all rather than flag every trip against
a number nobody chose.
"""
from datetime import timedelta

from app.models.enums import AlertSeverity
from app.services.alerts.base import AlertRule, register
from app.utils.timeutil import as_aware_utc, utcnow


def _limite(clave):
    """An administrator-set limit, or None when the policy is not in use."""
    from app.services import settings_service

    valor = settings_service.get_int(clave, 0)
    return valor if valor > 0 else None


def _moneda_de_la_politica():
    from app.services import settings_service

    return (settings_service.get('POLITICA_MONEDA', 'EUR') or 'EUR').upper()


def _costes_activos():
    from app.services import settings_service

    return settings_service.get_bool('COSTES_HABILITADOS', False)


@register
class TripCostOverPolicyRule(AlertRule):
    """Flags a trip whose known cost is above what policy allows unapproved."""

    codigo = 'coste_sobre_politica'
    nombre = 'Coste por encima de la política'
    severidad_por_defecto = AlertSeverity.ALTA

    def evaluate(self, ctx):
        if not _costes_activos():
            return []

        limite = _limite('POLITICA_COSTE_MAXIMO_VIAJE')
        if limite is None:
            return []

        total = ctx.trip.coste_total
        if total is None or total <= limite:
            return []

        moneda = _moneda_de_la_politica()
        monedas = ctx.trip.monedas_del_itinerario
        # Several currencies cannot be added up honestly: converting would need
        # a rate this application does not have, and one that would change the
        # answer without saying so.
        if [m for m in monedas if m.upper() != moneda]:
            return [self.candidate(
                ctx,
                titulo='El coste del viaje no se puede comparar con la política',
                mensaje=(
                    f'El itinerario mezcla {", ".join(monedas)} y la política '
                    f'está fijada en {moneda}. Convertirlo exigiría un tipo de '
                    f'cambio que el sistema no tiene, así que el límite no se '
                    f'ha aplicado.'
                ),
                evidencia={'monedas': monedas, 'moneda_politica': moneda,
                           'total_sin_convertir': total},
                sugerencia='Registre los importes en la moneda de la política.',
                severidad=AlertSeverity.INFORMATIVA,
            )]

        return [self.candidate(
            ctx,
            titulo=f'Coste del viaje por encima de la política: {total:.2f} {moneda}',
            mensaje=(
                f'El itinerario suma {total:.2f} {moneda}, por encima del '
                f'límite de {limite} {moneda} que la organización fija sin '
                f'aprobación previa.'
            ),
            evidencia={'total': total, 'limite': limite, 'moneda': moneda},
            sugerencia=(
                'Acepte la alerta dejando constancia de quién aprueba el gasto, '
                'o ajuste el itinerario.'
            ),
        )]


@register
class LodgingCostOverPolicyRule(AlertRule):
    """Flags a stay costing more per night than policy allows."""

    codigo = 'coste_noche_sobre_politica'
    nombre = 'Coste por noche por encima de la política'
    severidad_por_defecto = AlertSeverity.MEDIA

    def evaluate(self, ctx):
        if not _costes_activos():
            return []

        limite = _limite('POLITICA_COSTE_MAXIMO_NOCHE')
        if limite is None:
            return []

        moneda = _moneda_de_la_politica()
        candidatos = []

        for traveler_id, items in ctx.items_by_traveler.items():
            for alojamiento in items['alojamientos']:
                noches = alojamiento.noches
                if alojamiento.importe is None or not noches:
                    continue
                if alojamiento.moneda and alojamiento.moneda.upper() != moneda:
                    continue

                por_noche = float(alojamiento.importe) / noches
                if por_noche <= limite:
                    continue

                candidatos.append(self.candidate(
                    ctx,
                    titulo=(
                        f'{alojamiento.nombre}: {por_noche:.2f} {moneda} por noche'
                    ),
                    mensaje=(
                        f'{noches} noches por {float(alojamiento.importe):.2f} '
                        f'{moneda} salen a {por_noche:.2f} por noche, por encima '
                        f'del límite de {limite} {moneda}.'
                    ),
                    entidades=[alojamiento],
                    trip_traveler_id=traveler_id,
                    evidencia={
                        'alojamiento': alojamiento.nombre,
                        'noches': noches,
                        'importe': float(alojamiento.importe),
                        'por_noche': round(por_noche, 2),
                        'limite': limite,
                        'moneda': moneda,
                    },
                ))

        return candidatos


@register
class ShortNoticeBookingRule(AlertRule):
    """Flags a trip booked closer to departure than policy likes.

    Not a safety finding: a last-minute booking is usually more expensive, and
    an organisation that sets this wants to know how often it happens rather
    than to stop it.
    """

    codigo = 'reserva_con_poca_antelacion'
    nombre = 'Reserva con poca antelación'
    severidad_por_defecto = AlertSeverity.INFORMATIVA

    def evaluate(self, ctx):
        dias = _limite('POLITICA_ANTELACION_MINIMA_DIAS')
        if dias is None or ctx.trip.inicio_utc is None:
            return []

        # Normalised before comparing: one driver returns these aware and
        # another naive, so the same two columns compare fine in one place and
        # raise in the other.
        creado = as_aware_utc(ctx.trip.created_at)
        inicio = as_aware_utc(ctx.trip.inicio_utc)
        if creado is None or creado >= inicio:
            return []

        antelacion = (inicio - creado).days
        if antelacion >= dias:
            return []

        # A trip whose start has already passed says nothing about notice.
        if inicio < utcnow() - timedelta(days=1):
            return []

        return [self.candidate(
            ctx,
            titulo=f'Reservado con {antelacion} días de antelación',
            mensaje=(
                f'El viaje se registró {antelacion} días antes de su inicio, '
                f'por debajo de los {dias} que fija la política. Reservar con '
                f'poca antelación suele encarecer el viaje.'
            ),
            evidencia={'antelacion_dias': antelacion, 'minimo_dias': dias},
        )]
