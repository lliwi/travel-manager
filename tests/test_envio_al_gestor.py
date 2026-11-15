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


class TestQuienViajaConmigo:
    """Whoever prepares the draft says who comes -- until it is sent."""

    def test_quien_lo_prepara_anade_y_quita_a_otros(self, viajero, otro_viajero, propio):
        assert can(viajero, Permiso.GESTIONAR_VIAJEROS, propio)

        trip_service.add_traveler(viajero, propio, otro_viajero)
        assert propio.has_traveler(otro_viajero.id)

        trip_service.remove_traveler(viajero, propio, otro_viajero.id)
        assert not propio.has_traveler(otro_viajero.id)

    def test_desde_la_pantalla(self, as_user, viajero, otro_viajero, propio):
        with as_user(viajero) as client:
            html = client.get(f'/trips/{propio.id}').get_data(as_text=True)
            respuesta = client.post(f'/trips/{propio.id}/viajeros', data={
                'user_id': str(otro_viajero.id), 'rol_en_viaje': 'viajero',
            })

        assert f'/trips/{propio.id}/viajeros' in html
        assert respuesta.status_code == 302
        db.session.refresh(propio)
        assert propio.has_traveler(otro_viajero.id)

    def test_el_anadido_lo_ve_pero_ni_invita_ni_envia(self, viajero, otro_viajero, propio):
        trip_service.add_traveler(viajero, propio, otro_viajero)

        assert can(otro_viajero, Permiso.VER_VIAJE, propio)
        assert not can(otro_viajero, Permiso.GESTIONAR_VIAJEROS, propio)
        assert not can(otro_viajero, Permiso.ENVIAR_VIAJE, propio)

    def test_no_puede_quitarse_a_si_mismo(self, viajero, propio):
        with pytest.raises(ConflictError):
            trip_service.remove_traveler(viajero, propio, viajero.id)
        assert propio.has_traveler(viajero.id)

    def test_una_vez_enviado_los_viajeros_son_del_gestor(self, gestor, viajero, propio):
        trip_service.enviar_al_gestor(viajero, propio)

        assert not can(viajero, Permiso.GESTIONAR_VIAJEROS, propio)
        assert can(gestor, Permiso.GESTIONAR_VIAJEROS, propio)

    def test_en_un_viaje_del_gestor_tampoco(self, gestor, viajero):
        trip = trip_service.create_trip(gestor, titulo='Feria')
        trip_service.add_traveler(gestor, trip, viajero)

        assert not can(viajero, Permiso.GESTIONAR_VIAJEROS, trip)

    def test_planifica_para_todo_el_grupo(self, viajero, otro_viajero, propio):
        from datetime import datetime

        from app.services import itinerary_service

        def vuelo(**campos):
            return itinerary_service.create_item(
                viajero, propio, 'segmento',
                instants={'salida': (datetime(2026, 12, 1, 7, 10), 'Europe/Madrid'),
                          'llegada': (datetime(2026, 12, 1, 9, 0), 'Europe/Paris')},
                origen_codigo='BCN', destino_codigo='LYS', **campos,
            )

        antes = vuelo()
        trip_service.add_traveler(viajero, propio, otro_viajero)
        del_otro = propio.traveler_for(otro_viajero.id)
        suyo = propio.traveler_for(viajero.id)

        # What was chosen while alone is still editable, and new rows are for
        # everybody unless said to be only theirs -- never for one colleague.
        assert can(viajero, Permiso.EDITAR_ITINERARIO, antes)
        assert vuelo().trip_traveler_id is None
        assert vuelo(trip_traveler_id=del_otro.id).trip_traveler_id is None
        assert vuelo(trip_traveler_id=suyo.id).trip_traveler_id == suyo.id
        assert not can(otro_viajero, Permiso.EDITAR_ITINERARIO, antes)


class TestCorregirSuViaje:
    """The author corrects their own trip while it is still a proposal."""

    def test_lo_edita_antes_y_despues_de_enviarlo(self, viajero, propio):
        assert can(viajero, Permiso.EDITAR_VIAJE, propio)

        trip_service.enviar_al_gestor(viajero, propio)
        assert can(viajero, Permiso.EDITAR_VIAJE, propio)

        trip_service.update_trip(viajero, propio, titulo='Visita a Lyon y Grenoble')
        assert propio.titulo == 'Visita a Lyon y Grenoble'

    def test_cuando_el_gestor_lo_confirma_deja_de_poder(self, viajero, propio):
        from app.models.enums import TripStatus

        for estado in (TripStatus.CONFIRMADO, TripStatus.EN_CURSO,
                       TripStatus.FINALIZADO, TripStatus.CANCELADO):
            propio.estado = estado
            assert not can(viajero, Permiso.EDITAR_VIAJE, propio), estado

    def test_solo_el_suyo(self, gestor, viajero, otro_viajero, propio):
        del_gestor = trip_service.create_trip(gestor, titulo='Feria')
        trip_service.add_traveler(gestor, del_gestor, viajero)
        trip_service.add_traveler(viajero, propio, otro_viajero)

        assert not can(viajero, Permiso.EDITAR_VIAJE, del_gestor)
        assert not can(otro_viajero, Permiso.EDITAR_VIAJE, propio)

    def test_ni_el_gestor_ni_el_coste_ni_el_estado(self, viajero, otro_viajero, propio):
        from app.models.enums import TripStatus
        from app.utils.errors import AuthorizationError

        gestor_id, estado = propio.gestor_id, propio.estado

        trip_service.update_trip(viajero, propio, gestor_id=otro_viajero.id,
                                 coste_estimado=5000, moneda='EUR')
        with pytest.raises(AuthorizationError):
            trip_service.change_status(viajero, propio, TripStatus.CONFIRMADO)

        assert propio.gestor_id == gestor_id
        assert propio.coste_estimado is None
        assert propio.estado is estado

    def test_desde_la_pantalla(self, as_user, viajero, propio):
        estado = propio.estado

        with as_user(viajero) as client:
            html = client.get(f'/trips/{propio.id}/editar').get_data(as_text=True)
            respuesta = client.post(f'/trips/{propio.id}/editar', data={
                'titulo': 'Visita corregida', 'proyecto': 'PRJ-0009',
                'estado': 'confirmado', 'observaciones': 'Salgo un día antes.',
                'inicio_tz': 'Europe/Madrid', 'fin_tz': 'Europe/Madrid',
            })

        assert 'name="gestor_id"' not in html
        assert 'name="coste_estimado"' not in html
        assert respuesta.status_code == 302
        db.session.refresh(propio)
        assert propio.titulo == 'Visita corregida'
        assert propio.proyecto == 'PRJ-0009'
        assert propio.observaciones == 'Salgo un día antes.'
        assert propio.estado is estado, 'el estado no es suyo'
