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
