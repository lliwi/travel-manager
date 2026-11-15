"""Telling people what the system already knows.

Without somewhere to put these, the only way to find out that an alert appeared
on your trip is to go and look -- which means they are found by whoever happens
to look rather than by whoever needs to know.

A notification is created for a *fact*, not for an event: «this trip has an open
critical alert», not «the alert engine ran». The engine reconciles and the
source watcher runs nightly, so the same fact presents itself again and again;
keyed by the fact, the second telling finds the first already there.
"""
import pytest

from app.extensions import db
from app.models.enums import NotificationKind
from app.models.notification import Notification
from app.services import notification_service


@pytest.mark.unit
class TestSeAvisaUnaSolaVez:
    def test_se_crea_una_notificacion(self, gestor, trip, seeded):
        notification_service.notificar(
            gestor, NotificationKind.VIAJE, 'x:1', 'Algo ha pasado',
        )

        assert Notification.query.filter_by(usuario_id=gestor.id).count() == 1

    def test_el_mismo_hecho_no_se_repite(self, gestor, trip, seeded):
        for _ in range(3):
            notification_service.notificar(
                gestor, NotificationKind.VIAJE, 'x:1', 'Algo ha pasado',
            )

        assert Notification.query.filter_by(usuario_id=gestor.id).count() == 1

    def test_lo_leido_no_vuelve_a_aparecer(self, gestor, trip, seeded):
        """Somebody who read it has dealt with it.

        Putting it back unread would make the list a thing that grows back
        however often it is cleared.
        """
        n = notification_service.notificar(
            gestor, NotificationKind.VIAJE, 'x:1', 'Algo ha pasado',
        )
        notification_service.marcar_leida(gestor, n.id)

        notification_service.notificar(
            gestor, NotificationKind.VIAJE, 'x:1', 'Algo ha pasado',
        )

        assert notification_service.contar_sin_leer(gestor) == 0

    def test_un_hecho_distinto_si(self, gestor, trip, seeded):
        notification_service.notificar(gestor, NotificationKind.VIAJE, 'x:1', 'Uno')
        notification_service.notificar(gestor, NotificationKind.VIAJE, 'x:2', 'Otro')

        assert notification_service.contar_sin_leer(gestor) == 2


@pytest.mark.unit
class TestAQuienSeAvisa:
    def test_al_gestor_y_a_quien_viaja(self, gestor, trip, viajero, traveler_row):
        gente = {u.username for u in notification_service.interesados_en(trip)}

        assert gestor.username in gente
        assert viajero.username in gente

    def test_no_a_todo_el_mundo(self, gestor, trip, ajeno, seeded):
        """An inbox full of other people's trips is an inbox nobody reads."""
        gente = {u.username for u in notification_service.interesados_en(trip)}

        assert ajeno.username not in gente

    def test_a_una_persona_eliminada_no(self, gestor, trip, viajero, traveler_row):
        viajero.soft_delete(gestor)
        db.session.commit()

        gente = {u.username for u in notification_service.interesados_en(trip)}

        assert viajero.username not in gente


@pytest.mark.unit
class TestDesdeUnaAlerta:
    def _conexion_imposible(self, gestor, trip, segment_factory):
        from datetime import datetime

        from app.utils.timeutil import set_instant

        primero = segment_factory(numero='IB1')
        set_instant(primero, 'salida', datetime(2026, 6, 1, 8, 0), 'Europe/Madrid')
        set_instant(primero, 'llegada', datetime(2026, 6, 1, 10, 0), 'Europe/London')
        primero.origen_codigo, primero.destino_codigo = 'MAD', 'LHR'

        segundo = segment_factory(numero='BA2')
        set_instant(segundo, 'salida', datetime(2026, 6, 1, 10, 20), 'Europe/London')
        set_instant(segundo, 'llegada', datetime(2026, 6, 1, 18, 0), 'America/New_York')
        segundo.origen_codigo, segundo.destino_codigo = 'LHR', 'JFK'
        db.session.commit()

    def test_una_alerta_grave_avisa(self, gestor, trip, segment_factory):
        from app.services import alert_service

        self._conexion_imposible(gestor, trip, segment_factory)
        alert_service.recalculate(trip)

        assert notification_service.contar_sin_leer(gestor) >= 1

    def test_una_alerta_informativa_no(self, gestor, trip, seeded):
        """An inbox that reports every finding is an inbox that gets muted,
        and then the one that mattered goes unread with the rest."""
        from app.services import alert_service

        alert_service.recalculate(trip)

        graves = [
            n for n in notification_service.sin_leer(gestor)
            if n.tipo is NotificationKind.ALERTA
        ]
        assert not graves

    def test_recalcular_no_duplica(self, gestor, trip, segment_factory):
        from app.services import alert_service

        self._conexion_imposible(gestor, trip, segment_factory)
        alert_service.recalculate(trip)
        antes = notification_service.contar_sin_leer(gestor)

        alert_service.recalculate(trip)

        assert notification_service.contar_sin_leer(gestor) == antes


@pytest.mark.unit
class TestElCorreoEsLaSegundaEntrega:
    def test_sin_correo_configurado_la_notificacion_sigue_estando(
        self, gestor, trip, seeded
    ):
        """What lands in the application lands whether or not mail works.

        The record of «we told them» must not depend on a server being
        reachable.
        """
        from app.services import settings_service

        settings_service.set_value('CORREO_HABILITADO', False)
        notification_service.notificar(
            gestor, NotificationKind.VIAJE, 'x:1', 'Algo ha pasado',
        )

        assert notification_service.enviar_pendientes_por_correo() == 0
        assert notification_service.contar_sin_leer(gestor) == 1

    def test_no_se_reenvia_lo_ya_enviado(self, gestor, trip, seeded, monkeypatch):
        from app.services import mail_service, settings_service

        settings_service.set_value('CORREO_HABILITADO', True)
        settings_service.set_value('CORREO_HOST', 'mailpit')
        settings_service.set_value('CORREO_REMITENTE', 'de@corp.test')
        enviados = []
        monkeypatch.setattr(
            mail_service, 'enviar',
            lambda dest, asunto, cuerpo, **k: enviados.append(asunto) or True,
        )
        notification_service.notificar(
            gestor, NotificationKind.VIAJE, 'x:1', 'Algo ha pasado',
        )

        notification_service.enviar_pendientes_por_correo()
        notification_service.enviar_pendientes_por_correo()

        assert len(enviados) == 1


@pytest.mark.security
class TestCadaCualVeLaSuya:
    def test_no_se_marca_leida_la_de_otro(self, gestor, viajero, trip, seeded):
        n = notification_service.notificar(
            viajero, NotificationKind.VIAJE, 'x:1', 'De otra persona',
        )

        assert notification_service.marcar_leida(gestor, n.id) is None
        db.session.refresh(n)
        assert n.leida_en is None

    def test_la_pantalla_solo_muestra_las_propias(
        self, as_user, gestor, viajero, trip, seeded
    ):
        notification_service.notificar(
            viajero, NotificationKind.VIAJE, 'x:1', 'Secreto de otra persona',
        )

        with as_user(gestor) as client:
            html = client.get('/dashboard/notificaciones').get_data(as_text=True)

        assert 'Secreto de otra persona' not in html

    def test_hace_falta_iniciar_sesion(self, client):
        respuesta = client.get('/dashboard/notificaciones')

        assert respuesta.status_code in (302, 401)


class TestLoQueDiceCadaAviso:
    """A notification is read away from what caused it -- in a list, in a
    mail, hours later. It has to say which trip, what happened and what to do,
    and lead somewhere."""

    def test_el_enlace_existe_aunque_no_haya_peticion(self, app, trip):
        """Most are born in the worker, where ``url_for`` has nothing to lean
        on; they used to arrive with no way to act on them."""
        with app.app_context():
            enlace = notification_service.enlace_a('trips.detail', trip_id=trip.id)

        assert enlace == f'/trips/{trip.id}'

    def test_el_resumen_dice_que_viaje_es(self, gestor, trip, viajero, traveler_row):
        resumen = notification_service.resumen_del_viaje(trip)

        assert resumen.startswith(trip.referencia)
        assert '1 persona' in resumen

    def test_la_alerta_dice_en_que_viaje_y_como_de_grave(
        self, gestor, trip, segment_factory,
    ):
        from app.services import alert_service

        TestDesdeUnaAlerta()._conexion_imposible(gestor, trip, segment_factory)
        alert_service.recalculate(trip)

        aviso = next(n for n in notification_service.sin_leer(gestor)
                     if n.tipo is NotificationKind.ALERTA)
        assert trip.titulo in aviso.titulo
        assert aviso.titulo.startswith('Alerta ')
        assert aviso.enlace and aviso.enlace.startswith('/alerts/')
        assert aviso.mensaje

    def test_un_documento_fallido_dice_que_hacer(self, gestor, documento_subido):
        from app.services import document_service

        document_service.mark_failed(documento_subido, 'ocr_error', 'No se pudo leer el texto')

        aviso = next(n for n in notification_service.sin_leer(documento_subido.subido_por)
                     if n.tipo is NotificationKind.DOCUMENTO and 'No se pudo' in n.titulo)
        assert 'No se pudo leer el texto.' in aviso.mensaje
        assert 'reintentarse' in aviso.mensaje
        assert aviso.enlace == f'/documents/{documento_subido.id}'

    def test_el_cambio_de_fuente_dice_cual(self, gestor, trip, seeded):
        from app.services import advisory_service

        advisory_service._avisar_del_cambio(
            trip, ['https://www.exteriores.gob.es/reino-unido'], regenerar=True,
        )

        (aviso,) = [n for n in notification_service.sin_leer(gestor)
                    if n.tipo is NotificationKind.RECOMENDACION]
        assert 'exteriores.gob.es' in aviso.mensaje
        assert 'regenerando' in aviso.mensaje


class TestElCorreoSeEntiendeSolo:
    def test_lleva_el_viaje_y_una_direccion_que_se_puede_abrir(self, app, gestor, trip):
        aviso = notification_service.notificar(
            gestor, NotificationKind.VIAJE, 'x:correo', 'Algo ha pasado',
            mensaje='Y esto es lo que hay que hacer.',
            enlace=f'/trips/{trip.id}', trip=trip,
        )

        asunto, cuerpo = notification_service.correo_de(aviso)

        assert asunto == f'[{app.config["APP_NAME"]}] Algo ha pasado'
        assert 'Y esto es lo que hay que hacer.' in cuerpo
        assert trip.referencia in cuerpo and trip.titulo in cuerpo
        assert f'{app.config["APP_PUBLIC_URL"].rstrip("/")}/trips/{trip.id}' in cuerpo

    def test_sin_viaje_ni_enlace_no_deja_huecos(self, gestor, seeded):
        aviso = notification_service.notificar(
            gestor, NotificationKind.VIAJE, 'x:suelto', 'Solo un título',
        )

        _, cuerpo = notification_service.correo_de(aviso)

        assert 'Viaje:' not in cuerpo and 'Abrir:' not in cuerpo


class TestLaListaSePuedeUsar:
    def test_lo_leido_sigue_llevando_a_su_sitio(self, as_user, gestor, trip):
        aviso = notification_service.notificar(
            gestor, NotificationKind.VIAJE, 'x:leido', 'Ya lo he visto',
            enlace=f'/trips/{trip.id}', trip=trip,
        )
        notification_service.marcar_leida(gestor, aviso.id)

        with as_user(gestor) as client:
            html = client.get('/dashboard/notificaciones').get_data(as_text=True)

        assert f'href="/trips/{trip.id}"' in html
        assert 'Abrir' in html
        assert trip.titulo[:20] in html, 'el viaje se nombra, no solo su referencia'
