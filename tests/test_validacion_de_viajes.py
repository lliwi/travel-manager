"""A validator approves the trip before it is bought.

What must hold, each of which fails without a sound: nobody validates a trip
they have a stake in or were not asked about; a validator sees the trips put
before them and no others; and, when the organisation requires it, a trip is
not confirmed without a yes.
"""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.enums import RoleCode, TripStatus, ValidationState
from app.models.notification import Notification
from app.services import (
    itinerary_service,
    settings_service,
    trip_service,
    validacion_service,
)
from app.services.authorization_service import (
    Permiso,
    can,
    scope_itinerary_items,
    visible_trips_query,
)
from app.utils.errors import AuthorizationError, ConflictError, ValidationError

pytestmark = pytest.mark.security


@pytest.fixture
def validador(seeded):
    from tests.conftest import _make_user

    return _make_user('validadora', 'validadora@example.test', 'Valentina',
                      RoleCode.VALIDADOR)


@pytest.fixture
def otro_validador(seeded):
    from tests.conftest import _make_user

    return _make_user('validador2', 'validador2@example.test', 'Víctor',
                      RoleCode.VALIDADOR)


@pytest.fixture
def otro_gestor(seeded):
    from tests.conftest import _make_user

    return _make_user('gestor2', 'gestor2@example.test', 'Segundo', RoleCode.GESTOR)


@pytest.fixture
def preparado(gestor, viajero, otro_viajero):
    """A trip with everything a validator is asked about."""
    trip = trip_service.create_trip(
        gestor, titulo='Auditoría en Lyon', estado=TripStatus.BORRADOR,
        inicio_local=datetime(2026, 12, 1, 0, 0), fin_local=datetime(2026, 12, 3, 23, 59),
        inicio_tz='Europe/Madrid',
    )
    suyo = trip_service.add_traveler(gestor, trip, viajero)
    trip_service.add_traveler(gestor, trip, otro_viajero)
    itinerary_service.create_item(
        gestor, trip, 'segmento', trip_traveler_id=suyo.id,
        instants={'salida': (datetime(2026, 12, 1, 7, 10), 'Europe/Madrid'),
                  'llegada': (datetime(2026, 12, 1, 9, 0), 'Europe/Paris')},
        origen_codigo='BCN', destino_codigo='LYS',
    )
    return trip


def _avisos(usuario):
    return Notification.query.filter_by(usuario_id=usuario.id).all()


class TestElRol:
    def test_existe_y_se_puede_asignar(self, as_user, admin, validador):
        with as_user(admin) as client:
            html = client.get('/admin/usuarios/nuevo').get_data(as_text=True)

        assert 'Validador' in html
        assert validador.has_role(RoleCode.VALIDADOR)

    def test_por_si_solo_no_da_nada(self, validador, preparado):
        """An ordinary user until somebody asks them about a trip."""
        decision = can(validador, Permiso.VER_VIAJE, preparado)

        assert not decision and decision.no_existe
        assert not visible_trips_query(validador).count()
        assert not can(validador, Permiso.CREAR_VIAJE)


class TestPedirla:
    def test_el_validador_pasa_a_verlo_y_recibe_el_aviso(self, gestor, validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)

        assert preparado.validacion_estado is ValidationState.PENDIENTE
        assert can(validador, Permiso.VER_VIAJE, preparado)
        assert can(validador, Permiso.VALIDAR_VIAJE, preparado)
        assert preparado in visible_trips_query(validador).all()
        (aviso,) = _avisos(validador)
        assert 'validar' in aviso.titulo

    def test_ve_el_viaje_entero_pero_no_lo_toca_ni_ve_documentos(
        self, gestor, validador, preparado,
    ):
        validacion_service.solicitar(gestor, preparado, validador)

        tramos = [s for s in preparado.segments if not s.is_deleted]
        assert scope_itinerary_items(validador, preparado, tramos) == tramos
        for permiso in (Permiso.EDITAR_VIAJE, Permiso.EDITAR_ITINERARIO,
                        Permiso.GESTIONAR_VIAJEROS, Permiso.VER_DOCUMENTO,
                        Permiso.SUBIR_DOCUMENTO, Permiso.SOLICITAR_VALIDACION):
            assert not can(validador, permiso, preparado), permiso

    def test_solo_el_gestor_la_pide(self, viajero, validador, preparado):
        assert not can(viajero, Permiso.SOLICITAR_VALIDACION, preparado)
        assert not can(validador, Permiso.SOLICITAR_VALIDACION, preparado)

    def test_a_quien_no_es_validador_no(self, gestor, otro_gestor, preparado):
        with pytest.raises(ValidationError):
            validacion_service.solicitar(gestor, preparado, otro_gestor)

    def test_ni_a_quien_viaja_ni_a_uno_mismo(self, admin, gestor, viajero, preparado):
        from app.models.user import Role

        rol = Role.get(RoleCode.VALIDADOR.value)
        viajero.roles.append(rol)
        gestor.roles.append(rol)
        db.session.commit()

        with pytest.raises(ValidationError, match='viaja'):
            validacion_service.solicitar(gestor, preparado, viajero)
        with pytest.raises(ValidationError, match='quien pide'):
            validacion_service.solicitar(gestor, preparado, gestor)
        assert viajero not in validacion_service.validadores_para(preparado, gestor)

    def test_un_viaje_sin_preparar_no_se_envia(self, gestor, validador):
        vacio = trip_service.create_trip(gestor, titulo='A medias')

        with pytest.raises(ValidationError, match='falta'):
            validacion_service.solicitar(gestor, vacio, validador)

    def test_con_costes_hace_falta_alguna_cifra(self, gestor, validador, preparado):
        settings_service.set_value('COSTES_HABILITADOS', True)

        with pytest.raises(ValidationError, match='importe'):
            validacion_service.solicitar(gestor, preparado, validador)

        trip_service.update_trip(gestor, preparado, coste_estimado=900, moneda='EUR')
        validacion_service.solicitar(gestor, preparado, validador)
        assert can(validador, Permiso.VER_COSTES, preparado)

    def test_ya_confirmado_no(self, gestor, validador, preparado):
        preparado.estado = TripStatus.CONFIRMADO

        with pytest.raises(ConflictError):
            validacion_service.solicitar(gestor, preparado, validador)

    def test_pedirla_a_otro_retira_al_anterior(
        self, gestor, validador, otro_validador, preparado,
    ):
        validacion_service.solicitar(gestor, preparado, validador)
        validacion_service.solicitar(gestor, preparado, otro_validador)

        assert not can(validador, Permiso.VER_VIAJE, preparado)
        assert can(otro_validador, Permiso.VALIDAR_VIAJE, preparado)


class TestResponder:
    def test_aprobar_avisa_al_gestor(self, gestor, validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)
        validacion_service.resolver(validador, preparado, True)

        assert preparado.validado
        assert preparado.validacion_resuelta_en is not None
        (aviso,) = _avisos(gestor)
        assert 'validado' in aviso.titulo

    def test_rechazar_exige_decir_por_que(self, gestor, validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)

        with pytest.raises(ValidationError):
            validacion_service.resolver(validador, preparado, False)
        validacion_service.resolver(validador, preparado, False,
                                    comentario='El hotel pasa del límite.')

        assert preparado.validacion_estado is ValidationState.RECHAZADA
        assert 'límite' in _avisos(gestor)[0].mensaje

    def test_se_responde_una_vez(self, gestor, validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)
        validacion_service.resolver(validador, preparado, True)

        assert not can(validador, Permiso.VALIDAR_VIAJE, preparado)
        with pytest.raises(ConflictError):
            validacion_service.resolver(validador, preparado, False, comentario='No')
        assert can(validador, Permiso.VER_VIAJE, preparado), 'sigue viendo lo que aprobó'

    def test_nadie_mas_puede(self, admin, gestor, validador, otro_validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)

        for otro in (admin, gestor, otro_validador):
            assert not can(otro, Permiso.VALIDAR_VIAJE, preparado), otro.username
            with pytest.raises(AuthorizationError):
                validacion_service.resolver(otro, preparado, True)
        assert preparado.validacion_estado is ValidationState.PENDIENTE

    def test_sin_el_rol_pierde_el_viaje(self, gestor, validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)
        validador.roles = []
        db.session.commit()

        assert not can(validador, Permiso.VER_VIAJE, preparado)
        assert not can(validador, Permiso.VALIDAR_VIAJE, preparado)
        assert not visible_trips_query(validador).count()

    def test_tras_un_rechazo_se_puede_volver_a_pedir(self, gestor, validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)
        validacion_service.resolver(validador, preparado, False, comentario='Fechas')
        validacion_service.solicitar(gestor, preparado, validador)

        assert preparado.validacion_estado is ValidationState.PENDIENTE
        assert preparado.validacion_comentario is None
        assert len(_avisos(validador)) == 2

    def test_queda_en_la_auditoria(self, gestor, validador, preparado):
        from app.models.audit import AuditEvent

        validacion_service.solicitar(gestor, preparado, validador)
        validacion_service.resolver(validador, preparado, True)

        acciones = {e.accion for e in AuditEvent.query.filter(
            AuditEvent.accion.like('trip.validation_%'))}
        assert acciones == {'trip.validation_requested', 'trip.validation_approved'}

    def test_avisa_si_el_viaje_cambia_despues(self, gestor, validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)
        validacion_service.resolver(validador, preparado, True)
        assert not preparado.cambiado_tras_validar

        preparado.touch_itinerary()

        assert preparado.cambiado_tras_validar
        assert preparado.validado, 'avisa, no retira la aprobación'


class TestAntesDeConfirmar:
    def test_apagado_no_obliga(self, gestor, preparado):
        trip_service.change_status(gestor, preparado, TripStatus.CONFIRMADO)

        assert preparado.estado is TripStatus.CONFIRMADO

    def test_encendido_no_se_confirma_sin_aprobacion(self, gestor, validador, preparado):
        settings_service.set_value('VALIDACION_OBLIGATORIA', True)

        with pytest.raises(ValidationError, match='validador'):
            trip_service.change_status(gestor, preparado, TripStatus.CONFIRMADO)

        validacion_service.solicitar(gestor, preparado, validador)
        with pytest.raises(ValidationError, match='validador'):
            trip_service.change_status(gestor, preparado, TripStatus.CONFIRMADO)

        validacion_service.resolver(validador, preparado, False, comentario='No')
        with pytest.raises(ValidationError, match='validador'):
            trip_service.change_status(gestor, preparado, TripStatus.CONFIRMADO)

    def test_aprobado_si(self, gestor, validador, preparado):
        settings_service.set_value('VALIDACION_OBLIGATORIA', True)
        validacion_service.solicitar(gestor, preparado, validador)
        validacion_service.resolver(validador, preparado, True)

        trip_service.change_status(gestor, preparado, TripStatus.CONFIRMADO)

        assert preparado.estado is TripStatus.CONFIRMADO


class TestLasPantallas:
    def test_el_gestor_la_pide_y_el_validador_responde(
        self, as_user, gestor, validador, preparado,
    ):
        with as_user(gestor) as client:
            html = client.get(f'/trips/{preparado.id}').get_data(as_text=True)
            pedir = client.post(f'/trips/{preparado.id}/validacion/solicitar',
                                data={'validador_id': str(validador.id)})
        assert validador.nombre_completo in html
        assert pedir.status_code == 302

        with as_user(validador) as client:
            lista = client.get('/trips/').get_data(as_text=True)
            html = client.get(f'/trips/{preparado.id}').get_data(as_text=True)
            documentos = client.get(f'/documents/viaje/{preparado.id}')
            responder = client.post(f'/trips/{preparado.id}/validacion/responder',
                                    data={'decision': 'aprobar'})

        assert preparado.referencia in lista
        assert f'/trips/{preparado.id}/validacion/responder' in html
        assert 'BCN' in html, 've el itinerario'
        assert documentos.status_code in (403, 404)
        assert responder.status_code == 302
        db.session.refresh(preparado)
        assert preparado.validado

    def test_un_viajero_no_puede_responder(self, as_user, gestor, viajero, validador,
                                           preparado):
        validacion_service.solicitar(gestor, preparado, validador)

        with as_user(viajero) as client:
            html = client.get(f'/trips/{preparado.id}').get_data(as_text=True)
            respuesta = client.post(f'/trips/{preparado.id}/validacion/responder',
                                    data={'decision': 'aprobar'})

        assert 'validacion/responder' not in html
        assert 'Pendiente de validar' in html, 'pero sí ve en qué está'
        assert respuesta.status_code == 403
        assert preparado.validacion_estado is ValidationState.PENDIENTE

    def test_por_la_api(self, client, api_login, gestor, validador, preparado):
        api_login(gestor)
        pedida = client.post(f'/api/v1/trips/{preparado.id}/validation/request',
                             json={'validador_id': str(validador.id)})
        client.post('/api/v1/auth/logout')

        api_login(validador)
        resuelta = client.post(f'/api/v1/trips/{preparado.id}/validation/resolve',
                               json={'aprobar': True})

        assert pedida.get_json()['data']['validacion']['estado'] == 'pendiente'
        assert resuelta.get_json()['data']['validacion']['estado'] == 'aprobada'


class TestSeLeDiceAQuienViaja:
    def test_que_su_viaje_esta_validado(self, gestor, viajero, otro_viajero,
                                        validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)
        validacion_service.resolver(validador, preparado, True)

        for persona in (viajero, otro_viajero):
            (aviso,) = _avisos(persona)
            assert 'ha sido validado' in aviso.titulo
            assert preparado.referencia in aviso.mensaje
            assert aviso.enlace == f'/trips/{preparado.id}'
        assert len(_avisos(gestor)) == 1, 'al gestor, una vez y con sus palabras'

    def test_y_que_se_ha_rechazado_con_el_motivo(self, gestor, viajero, otro_viajero,
                                                  validador, preparado):
        validacion_service.solicitar(gestor, preparado, validador)
        validacion_service.resolver(validador, preparado, False,
                                    comentario='El hotel pasa del límite.')

        for persona in (viajero, otro_viajero):
            (aviso,) = _avisos(persona)
            assert 'no ha sido validado' in aviso.titulo
            assert 'El hotel pasa del límite.' in aviso.mensaje
            assert gestor.nombre_completo in aviso.mensaje
        assert len(_avisos(gestor)) == 1

    def test_que_esta_confirmado(self, gestor, viajero, otro_viajero, preparado):
        trip_service.change_status(gestor, preparado, TripStatus.CONFIRMADO)

        for persona in (viajero, otro_viajero):
            (aviso,) = _avisos(persona)
            assert 'está confirmado' in aviso.titulo
        assert not _avisos(gestor), 'quien lo confirma ya lo sabe'

    def test_que_se_ha_cancelado(self, gestor, viajero, preparado):
        trip_service.change_status(gestor, preparado, TripStatus.CANCELADO)

        (aviso,) = _avisos(viajero)
        assert 'se ha cancelado' in aviso.titulo
        assert gestor.nombre_completo in aviso.mensaje

    def test_los_demas_cambios_de_estado_no(self, gestor, viajero, preparado):
        preparado.estado = TripStatus.EN_PREPARACION
        trip_service.change_status(gestor, preparado, TripStatus.BORRADOR)

        assert not _avisos(viajero)

    def test_confirmarlo_otra_vez_vuelve_a_avisar(self, gestor, viajero, preparado):
        trip_service.change_status(gestor, preparado, TripStatus.CONFIRMADO)
        trip_service.change_status(gestor, preparado, TripStatus.EN_PREPARACION)
        trip_service.change_status(gestor, preparado, TripStatus.CONFIRMADO)

        assert len(_avisos(viajero)) == 2
