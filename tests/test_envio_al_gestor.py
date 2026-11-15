"""A trip a traveller prepares is theirs alone until they send it.

Before sending, no manager sees it, lists it or is told anything about it.
Sending makes it visible and tells its manager, once, that the planning is
done. Each half fails quietly: a draft that leaks is a manager acting on a
half-chosen flight, and a send that tells nobody is a trip that waits for ever.
"""
import pytest

from app.extensions import db
from app.models.notification import Notification
from app.services import notification_service, settings_service, trip_service
from app.services.authorization_service import Permiso, can, visible_trips_query
from app.utils.errors import ConflictError

pytestmark = pytest.mark.security


@pytest.fixture
def propio(gestor, viajero):
    settings_service.set_value('USUARIOS_PLANIFICAN', True)
    settings_service.set_value('GESTOR_POR_DEFECTO', gestor.username)
    return trip_service.create_trip(viajero, titulo='Visita a la planta de Lyon')


def _avisos(usuario):
    return Notification.query.filter_by(usuario_id=usuario.id).all()


class TestMientrasSePrepara:
    def test_el_gestor_no_lo_ve(self, gestor, propio):
        decision = can(gestor, Permiso.VER_VIAJE, propio)

        assert not decision
        assert decision.no_existe, 'ni siquiera se confirma que exista'
        assert propio not in visible_trips_query(gestor).all()

    def test_ni_en_su_pantalla_ni_por_la_api(self, as_user, gestor, propio):
        with as_user(gestor) as client:
            assert client.get(f'/trips/{propio.id}').status_code == 404
            assert client.get(f'/api/v1/trips/{propio.id}').status_code == 404
            assert propio.referencia not in client.get('/trips/').get_data(as_text=True)

    def test_quien_lo_prepara_si(self, viajero, propio):
        assert can(viajero, Permiso.VER_VIAJE, propio)
        assert can(viajero, Permiso.EDITAR_ITINERARIO, propio)
        assert propio in visible_trips_query(viajero).all()

    def test_el_administrador_tambien(self, admin, propio):
        assert can(admin, Permiso.VER_VIAJE, propio)
        assert propio in visible_trips_query(admin).all()

    def test_al_gestor_no_le_llega_nada(self, gestor, viajero, propio):
        assert not _avisos(gestor)
        assert notification_service.interesados_en(propio) == [viajero]

    def test_los_viajes_del_gestor_no_nacen_pendientes(self, gestor):
        trip = trip_service.create_trip(gestor, titulo='Feria')

        assert not trip.pendiente_envio
        assert can(gestor, Permiso.VER_VIAJE, trip)

    def test_la_pantalla_ofrece_enviar(self, as_user, viajero, propio):
        with as_user(viajero) as client:
            html = client.get(f'/trips/{propio.id}').get_data(as_text=True)

        assert f'/trips/{propio.id}/enviar' in html
        assert 'aún no se ha enviado' in html


class TestEnviar:
    def test_el_gestor_pasa_a_verlo_y_recibe_el_aviso(self, gestor, viajero, propio):
        trip_service.enviar_al_gestor(viajero, propio)

        assert not propio.pendiente_envio
        assert propio.enviado_en is not None
        assert can(gestor, Permiso.EDITAR_VIAJE, propio)
        assert propio in visible_trips_query(gestor).all()

        (aviso,) = _avisos(gestor)
        assert 'terminado de planificar' in aviso.titulo
        assert aviso.trip_id == propio.id

    def test_solo_se_envia_una_vez(self, gestor, viajero, propio):
        trip_service.enviar_al_gestor(viajero, propio)

        assert not can(viajero, Permiso.ENVIAR_VIAJE, propio)
        with pytest.raises(ConflictError):
            trip_service.enviar_al_gestor(viajero, propio)
        assert len(_avisos(gestor)) == 1

    def test_queda_en_la_auditoria(self, viajero, propio):
        from app.models.audit import AuditEvent

        trip_service.enviar_al_gestor(viajero, propio)

        evento = AuditEvent.query.filter_by(accion='trip.submitted').one()
        assert evento.recurso_id == str(propio.id)

    def test_desde_la_pantalla(self, as_user, gestor, viajero, propio):
        with as_user(viajero) as client:
            respuesta = client.post(f'/trips/{propio.id}/enviar')
            html = client.get(f'/trips/{propio.id}').get_data(as_text=True)

        assert respuesta.status_code == 302
        db.session.refresh(propio)
        assert not propio.pendiente_envio
        assert len(_avisos(gestor)) == 1
        assert f'/trips/{propio.id}/enviar' not in html

    def test_por_la_api(self, client, api_login, gestor, viajero, propio):
        api_login(viajero)

        respuesta = client.post(f'/api/v1/trips/{propio.id}/submit')

        assert respuesta.status_code == 200
        assert respuesta.get_json()['data']['pendiente_envio'] is False
        assert len(_avisos(gestor)) == 1

    def test_otro_viajero_no_puede(self, as_user, otro_viajero, propio):
        assert not can(otro_viajero, Permiso.ENVIAR_VIAJE, propio)
        with as_user(otro_viajero) as client:
            assert client.post(f'/trips/{propio.id}/enviar').status_code == 404
        assert propio.pendiente_envio

    def test_un_viaje_del_gestor_no_tiene_nada_que_enviar(self, gestor, viajero):
        trip = trip_service.create_trip(gestor, titulo='Feria')
        trip_service.add_traveler(gestor, trip, viajero)

        assert not can(viajero, Permiso.ENVIAR_VIAJE, trip)
        assert not can(gestor, Permiso.ENVIAR_VIAJE, trip)

    def test_apagar_la_planificacion_no_deja_el_borrador_atrapado(self, viajero, propio):
        settings_service.set_value('USUARIOS_PLANIFICAN', False)

        assert can(viajero, Permiso.ENVIAR_VIAJE, propio)

    def test_si_el_gestor_ya_no_esta_va_a_quien_lo_lleve_ahora(
        self, gestor, viajero, propio,
    ):
        from app.models.enums import RoleCode
        from tests.conftest import _make_user

        relevo = _make_user('gestor2', 'gestor2@example.test', 'Relevo', RoleCode.GESTOR)
        settings_service.set_value('GESTOR_POR_DEFECTO', relevo.username)
        gestor.is_deleted = True
        db.session.commit()

        trip_service.enviar_al_gestor(viajero, propio)

        assert propio.gestor_id == relevo.id
        assert len(_avisos(relevo)) == 1
