"""Travellers planning their own trips.

Off by default. When an organisation turns it on, somebody with the «usuario»
profile chooses their flights and lodging -- by hand or with the assistant --
while the trip is still a proposal, and the manager does the rest: documents,
advisories, confirming what was booked. Three things must hold, and each fails
quietly:

- A traveller never plans somebody else's journey, nor rewrites what came
  from a document.
- Planning ends when the manager confirms the trip.
- A trip a traveller creates always has a manager who has been told.
"""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.enums import TripStatus
from app.services import itinerary_service, settings_service, trip_service
from app.services.authorization_service import (
    ESTADOS_PLANIFICABLES,
    Permiso,
    can,
    permisos_de,
)
from app.utils.errors import AuthorizationError, ValidationError

pytestmark = pytest.mark.security


def _activar(gestor=None):
    settings_service.set_value('USUARIOS_PLANIFICAN', True)
    if gestor is not None:
        settings_service.set_value('GESTOR_POR_DEFECTO', gestor.username)


@pytest.fixture
def borrador(gestor, viajero):
    trip = trip_service.create_trip(gestor, titulo='Feria en Múnich',
                                    estado=TripStatus.BORRADOR)
    trip_service.add_traveler(gestor, trip, viajero)
    return trip


def _vuelo(actor, trip, **campos):
    return itinerary_service.create_item(
        actor, trip, 'segmento',
        instants={'salida': (datetime(2026, 11, 3, 7, 10), 'Europe/Madrid'),
                  'llegada': (datetime(2026, 11, 3, 9, 20), 'Europe/Berlin')},
        origen_codigo='BCN', destino_codigo='MUC', **campos,
    )


class TestApagadoPorDefecto:
    def test_el_viajero_sigue_solo_consultando(self, viajero, borrador):
        assert not can(viajero, Permiso.CREAR_VIAJE)
        assert not can(viajero, Permiso.EDITAR_ITINERARIO, borrador)

    def test_y_el_servicio_tambien_lo_rechaza(self, viajero, borrador):
        with pytest.raises(AuthorizationError):
            itinerary_service.create_item(viajero, borrador, 'vehiculo')


class TestEnSusViajes:
    def test_planifica_mientras_es_una_propuesta(self, viajero, borrador):
        _activar()

        assert can(viajero, Permiso.EDITAR_ITINERARIO, borrador)

        borrador.estado = TripStatus.EN_PREPARACION
        assert can(viajero, Permiso.EDITAR_ITINERARIO, borrador)

    def test_cuando_el_gestor_lo_confirma_deja_de_poder(self, viajero, borrador):
        _activar()
        borrador.estado = TripStatus.CONFIRMADO

        assert not can(viajero, Permiso.EDITAR_ITINERARIO, borrador)

    def test_en_un_viaje_ajeno_nada(self, viajero, gestor):
        _activar()
        ajeno = trip_service.create_trip(gestor, titulo='Otro', estado=TripStatus.BORRADOR)

        assert not can(viajero, Permiso.EDITAR_ITINERARIO, ajeno)

    def test_solo_vuelos_y_alojamientos(self, viajero, borrador):
        _activar()

        assert _vuelo(viajero, borrador).id
        with pytest.raises(AuthorizationError):
            itinerary_service.create_item(viajero, borrador, 'vehiculo')

    def test_lo_que_vino_de_un_documento_es_del_gestor(self, viajero, gestor, borrador,
                                                      documento_subido):
        _activar()
        tramo = _vuelo(gestor, borrador)
        tramo.documento_origen_id = documento_subido.id
        db.session.commit()

        assert not can(viajero, Permiso.EDITAR_ITINERARIO, tramo)
        with pytest.raises(AuthorizationError):
            itinerary_service.delete_item(viajero, borrador, 'segmento', tramo.id)

    def test_lo_suyo_si_lo_edita(self, viajero, borrador):
        _activar()
        tramo = _vuelo(viajero, borrador)

        itinerary_service.update_item(viajero, borrador, 'segmento', tramo.id,
                                      numero='LH1813')

        assert tramo.numero == 'LH1813'

    def test_en_un_viaje_compartido_planifica_solo_lo_suyo(
        self, viajero, otro_viajero, gestor, borrador,
    ):
        _activar()
        trip_service.add_traveler(gestor, borrador, otro_viajero)
        suyo = borrador.traveler_for(viajero.id)
        del_otro = borrador.traveler_for(otro_viajero.id)

        # «For the whole trip», or for the colleague: it is addressed to them.
        tramo = _vuelo(viajero, borrador, trip_traveler_id=del_otro.id)
        assert tramo.trip_traveler_id == suyo.id

        ajeno = _vuelo(gestor, borrador, trip_traveler_id=del_otro.id)
        comun = _vuelo(gestor, borrador)
        assert not can(viajero, Permiso.EDITAR_ITINERARIO, ajeno)
        assert not can(viajero, Permiso.EDITAR_ITINERARIO, comun)

    def test_confirmar_lo_dudoso_sigue_siendo_del_gestor(self, as_user, viajero, borrador):
        _activar()

        with as_user(viajero) as client:
            respuesta = client.get(f'/trips/{borrador.id}/confirmar')

        assert respuesta.status_code == 403


class TestCrearSuPropioViaje:
    def test_sin_gestor_configurado_no_puede_crear(self, viajero):
        _activar()

        assert not can(viajero, Permiso.CREAR_VIAJE)
        with pytest.raises(ValidationError):
            trip_service.create_trip(viajero, titulo='Mi viaje')

    def test_un_gestor_por_defecto_que_no_gestiona_no_cuenta(self, viajero, otro_viajero):
        _activar(otro_viajero)

        assert not can(viajero, Permiso.CREAR_VIAJE)

    def test_queda_en_borrador_a_cargo_del_gestor_y_con_el_de_viajero(
        self, viajero, gestor,
    ):
        _activar(gestor)

        trip = trip_service.create_trip(
            viajero, titulo='Mi viaje', estado=TripStatus.CONFIRMADO,
            gestor=viajero, coste_estimado=9999,
        )

        # Not what the traveller asked for: assigning them already moves a
        # draft into preparation, as it does for any trip.
        assert str(trip.estado) in ESTADOS_PLANIFICABLES
        assert trip.gestor_id == gestor.id
        assert trip.has_traveler(viajero.id)
        assert trip.coste_estimado is None
        assert can(viajero, Permiso.EDITAR_ITINERARIO, trip)

    def test_se_avisa_al_gestor(self, viajero, gestor):
        from app.models.notification import Notification

        _activar(gestor)
        trip = trip_service.create_trip(viajero, titulo='Mi viaje')

        aviso = Notification.query.filter_by(usuario_id=gestor.id, trip_id=trip.id).one()
        assert 'planificar' in aviso.titulo

    def test_tambien_con_el_asistente(self, viajero, gestor):
        from app.services import trip_from_search_service

        _activar(gestor)
        trip, _ = trip_from_search_service.crear_viaje(
            viajero, titulo='Desde el buscador',
            vuelos=[{'precio_valor': 180, 'tramos': [{
                'origen': 'BCN', 'destino': 'MUC',
                'salida': '2026-11-03 07:10', 'llegada': '2026-11-03 09:20',
                'aerolinea': 'Lufthansa', 'numero': 'LH 1813',
            }]}],
        )

        assert trip.gestor_id == gestor.id
        assert trip.has_traveler(viajero.id)
        (tramo,) = [s for s in trip.segments if not s.is_deleted]
        assert (tramo.origen_codigo, tramo.destino_codigo) == ('BCN', 'MUC')
        assert can(viajero, Permiso.EDITAR_ITINERARIO, tramo)

    def test_el_menu_lo_ofrece(self, as_user, viajero, gestor):
        _activar(gestor)

        assert Permiso.CREAR_VIAJE in permisos_de(viajero)
        with as_user(viajero) as client:
            html = client.get('/trips/').get_data(as_text=True)
        assert 'Nuevo viaje' in html


class TestLaPantallaDelViaje:
    def test_ofrece_solo_trayectos_y_alojamientos(self, as_user, viajero, borrador):
        _activar()

        with as_user(viajero) as client:
            html = client.get(f'/trips/{borrador.id}').get_data(as_text=True)

        assert 'itinerario/segmento/nuevo' in html
        assert 'itinerario/alojamiento/nuevo' in html
        assert 'itinerario/vehiculo/nuevo' not in html

    def test_el_formulario_de_alta_no_pide_estado_ni_documentos(
        self, as_user, viajero, gestor,
    ):
        _activar(gestor)

        with as_user(viajero) as client:
            html = client.get('/trips/nuevo').get_data(as_text=True)

        assert 'name="gestor_id"' not in html
        assert 'Documentos de reserva' not in html
        assert gestor.nombre_completo in html

    def test_crear_desde_la_pantalla(self, as_user, viajero, gestor):
        from app.models.trip import Trip

        _activar(gestor)

        with as_user(viajero) as client:
            respuesta = client.post('/trips/nuevo', data={
                'titulo': 'Revisión de prototipo', 'estado': 'confirmado',
                'inicio_tz': 'Europe/Madrid', 'fin_tz': 'Europe/Madrid',
            })

        assert respuesta.status_code == 302
        trip = Trip.query.filter_by(titulo='Revisión de prototipo').one()
        assert str(trip.estado) in ESTADOS_PLANIFICABLES
        assert trip.gestor_id == gestor.id
