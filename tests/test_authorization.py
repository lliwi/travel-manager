"""Authorisation matrix and isolation tests.

These are the barrier tests: they exist so a future change cannot quietly widen
access. ``test_api_parity`` in particular is what stops an unguarded endpoint
shipping.
"""
import pytest

from app.extensions import db
from app.services.authorization_service import (
    PERMISOS_ADMIN,
    Permiso,
    can,
    scope_itinerary_items,
    visible_trips_query,
)


@pytest.mark.security
class TestMatrizDeAutorizacion:
    """Specification section 2.1: the three access profiles."""

    def test_el_administrador_puede_todo(self, admin, trip):
        for permiso in Permiso:
            assert can(admin, permiso, trip) or permiso not in PERMISOS_ADMIN

    def test_el_gestor_administra_cualquier_viaje(self, gestor, trip):
        """Section 2.1: a manager administers every trip."""
        for permiso in (Permiso.VER_VIAJE, Permiso.EDITAR_VIAJE,
                        Permiso.GESTIONAR_VIAJEROS, Permiso.SUBIR_DOCUMENTO,
                        Permiso.REVISAR_EXTRACCION, Permiso.GESTIONAR_ALERTA):
            decision = can(gestor, permiso, trip)
            assert decision.permitido, f'El gestor debería poder {permiso}.'
            assert decision.motivo == 'gestor'

    def test_el_gestor_no_administra_el_sistema(self, gestor, trip):
        """Administration is the administrator's, not the manager's."""
        assert not can(gestor, Permiso.ADMINISTRAR)
        assert not can(gestor, Permiso.VER_AUDITORIA)

    def test_el_viajero_asignado_solo_consulta(self, viajero, trip):
        """Section 2.1: a user consults only, and only their own trips."""
        assert can(viajero, Permiso.VER_VIAJE, trip)
        assert can(viajero, Permiso.VER_ALERTA, trip)
        assert can(viajero, Permiso.VER_RECOMENDACION, trip)
        assert can(viajero, Permiso.CONSULTAR_IA, trip)

        for permiso in (Permiso.EDITAR_VIAJE, Permiso.ELIMINAR_VIAJE,
                        Permiso.GESTIONAR_VIAJEROS, Permiso.SUBIR_DOCUMENTO,
                        Permiso.REVISAR_EXTRACCION, Permiso.GESTIONAR_ALERTA,
                        Permiso.VALIDAR_RECOMENDACION, Permiso.EDITAR_ITINERARIO):
            assert not can(viajero, permiso, trip), (
                f'Un viajero no debería poder {permiso}.'
            )

    def test_el_no_asignado_no_puede_nada(self, ajeno, trip):
        for permiso in (Permiso.VER_VIAJE, Permiso.VER_DOCUMENTO,
                        Permiso.DESCARGAR_DOCUMENTO, Permiso.CONSULTAR_IA):
            decision = can(ajeno, permiso, trip)
            assert not decision.permitido
            assert decision.motivo == 'no_asignado'
            assert decision.no_existe, (
                'La denegación debe renderizarse como 404 para no confirmar que '
                'el viaje existe.'
            )

    def test_el_anonimo_no_puede_nada(self, trip):
        decision = can(None, Permiso.VER_VIAJE, trip)
        assert not decision.permitido
        assert decision.motivo == 'no_autenticado'

    def test_una_cuenta_desactivada_pierde_el_acceso(self, viajero, trip):
        from app.models.enums import UserStatus

        assert can(viajero, Permiso.VER_VIAJE, trip)

        viajero.estado = UserStatus.INACTIVO
        db.session.commit()

        decision = can(viajero, Permiso.VER_VIAJE, trip)
        assert not decision.permitido
        assert decision.motivo == 'cuenta_inactiva'

    def test_un_viaje_cancelado_no_admite_cambios(self, gestor, trip):
        from app.models.enums import TripStatus

        trip.estado = TripStatus.CANCELADO
        db.session.commit()

        assert can(gestor, Permiso.VER_VIAJE, trip), 'Se debe poder seguir consultando.'
        assert not can(gestor, Permiso.EDITAR_VIAJE, trip)
        assert not can(gestor, Permiso.SUBIR_DOCUMENTO, trip)

    def test_los_costes_dependen_del_flag(self, gestor, viajero, trip):
        """Section 2.2: costs only when their treatment is enabled."""
        from app.services import settings_service

        settings_service.set_value('COSTES_HABILITADOS', False)
        assert not can(viajero, Permiso.VER_COSTES, trip)

        settings_service.set_value('COSTES_HABILITADOS', True)
        # Even enabled, the traveller profile never receives it.
        assert not can(viajero, Permiso.VER_COSTES, trip)
        assert can(gestor, Permiso.VER_COSTES, trip)


@pytest.mark.security
class TestAlcanceDeConsultas:
    """A listing must never be the place a leak happens."""

    def test_visible_trips_query_del_no_asignado_esta_vacia(self, ajeno, trip):
        assert visible_trips_query(ajeno).count() == 0

    def test_visible_trips_query_del_asignado_incluye_su_viaje(self, viajero, trip):
        trips = visible_trips_query(viajero).all()
        assert [t.id for t in trips] == [trip.id]

    def test_visible_trips_query_del_gestor_incluye_todo(self, gestor, trip):
        assert visible_trips_query(gestor).count() >= 1

    def test_visible_trips_query_del_anonimo_esta_vacia(self, trip):
        assert visible_trips_query(None).count() == 0

    def test_no_incluye_viajes_eliminados(self, gestor, trip):
        from app.services import trip_service

        assert visible_trips_query(gestor).count() == 1
        trip_service.delete_trip(gestor, trip)
        assert visible_trips_query(gestor).count() == 0


@pytest.mark.security
class TestAislamientoEntreViajeros:
    """Section 5.4: one traveller never sees another's rows."""

    def test_el_viajero_no_ve_los_elementos_de_otro(
        self, gestor, trip, viajero, otro_viajero, segment_factory
    ):
        from app.services import trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)

        mio = segment_factory(numero='IB1111', traveler=trip.traveler_for(viajero.id))
        suyo = segment_factory(numero='LH2222', traveler=trip.traveler_for(otro_viajero.id))
        comun = segment_factory(numero='COMUN', traveler=None)

        visibles = scope_itinerary_items(viajero, trip, [mio, suyo, comun])
        numeros = {s.numero for s in visibles}

        assert 'IB1111' in numeros, 'Debe ver lo suyo.'
        assert 'COMUN' in numeros, 'Debe ver lo que aplica a todo el viaje.'
        assert 'LH2222' not in numeros, 'No debe ver lo de otro viajero.'

    def test_el_gestor_ve_todo(
        self, gestor, trip, viajero, otro_viajero, segment_factory
    ):
        from app.services import trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)

        items = [
            segment_factory(numero='IB1111', traveler=trip.traveler_for(viajero.id)),
            segment_factory(numero='LH2222', traveler=trip.traveler_for(otro_viajero.id)),
        ]
        assert len(scope_itinerary_items(gestor, trip, items)) == 2

    def test_el_timeline_del_viajero_esta_filtrado(
        self, gestor, trip, viajero, otro_viajero, segment_factory
    ):
        from app.services import itinerary_service, trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)

        segment_factory(numero='IB1111', traveler=trip.traveler_for(viajero.id))
        segment_factory(numero='LH2222', traveler=trip.traveler_for(otro_viajero.id))

        timeline = itinerary_service.build_timeline(viajero, trip)
        numeros = {e.item.numero for e in timeline.entries}

        assert numeros == {'IB1111'}

    def test_las_alertas_del_viajero_estan_filtradas(
        self, gestor, trip, viajero, otro_viajero, segment_factory
    ):
        from app.services import alert_service, trip_service
        from app.services.alerts import engine

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)

        segment_factory(numero='IB1111', traveler=trip.traveler_for(viajero.id))
        segment_factory(numero='LH2222', traveler=trip.traveler_for(otro_viajero.id))
        engine.run(trip)

        del_gestor = alert_service.list_for_trip(gestor, trip)
        del_viajero = alert_service.list_for_trip(viajero, trip)

        assert len(del_gestor) > len(del_viajero), (
            'El gestor debe ver más alertas que un viajero concreto.'
        )
        otro_id = trip.traveler_for(otro_viajero.id).id
        assert all(a.trip_traveler_id != otro_id for a in del_viajero)

    def test_los_documentos_del_viajero_estan_filtrados(
        self, gestor, trip, viajero, otro_viajero, booking_pdf
    ):
        import io

        from werkzeug.datastructures import FileStorage

        from app.models.enums import DocumentType
        from app.services import document_service, trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)

        suyo = document_service.upload(
            gestor, trip,
            FileStorage(stream=io.BytesIO(booking_pdf), filename='de_otro.pdf',
                        content_type='application/pdf'),
            tipo=DocumentType.RESERVA,
            trip_traveler_id=trip.traveler_for(otro_viajero.id).id,
            commit=False,
        )
        db.session.commit()

        visibles = document_service.list_for_trip(viajero, trip)
        assert suyo.id not in {d.id for d in visibles}, (
            'Un viajero no debe ver el documento personal de otro.'
        )
        assert suyo.id in {d.id for d in document_service.list_for_trip(gestor, trip)}


@pytest.mark.security
class TestParidadDeLaAPI:
    """Every API endpoint must declare how it is authorised.

    This is the test that keeps an unguarded endpoint from shipping: adding a
    route to ``/api/v1`` without a permission decorator fails here, by name.
    """

    #: Endpoints deliberately reachable without authentication.
    PUBLICOS = {'api_v1.login'}

    def test_ningun_endpoint_api_sin_autorizacion(self, app):
        sin_guardia = []

        for rule in app.url_map.iter_rules():
            if not rule.rule.startswith('/api/v1'):
                continue
            if rule.endpoint in self.PUBLICOS:
                continue

            view = app.view_functions[rule.endpoint]
            if getattr(view, '__public__', False):
                continue
            if not hasattr(view, '__authz__'):
                sin_guardia.append(f'{rule.endpoint} ({rule.rule})')

        assert not sin_guardia, (
            'Estos endpoints de /api/v1 no declaran autorización:\n  '
            + '\n  '.join(sorted(sin_guardia))
        )

    def test_los_endpoints_web_requieren_sesion(self, app, client):
        """Every page except login and the probes redirects an anonymous visitor."""
        # «metrics» va con las sondas y por el mismo motivo: un recolector de
        # Prometheus no inicia sesión. No está desprotegido -- se defiende por
        # red y por token, y eso lo comprueba tests/test_metricas.py, no esta
        # barrera, que solo sabe preguntar por la sesión.
        exentos = {
            'auth.login', 'static', 'index', 'healthz', 'readyz', 'metrics',
        }
        fallos = []

        for rule in app.url_map.iter_rules():
            if rule.rule.startswith('/api/v1') or rule.endpoint in exentos:
                continue
            if 'GET' not in rule.methods or rule.arguments:
                continue

            response = client.get(rule.rule)
            if response.status_code not in (301, 302, 401, 403, 404):
                fallos.append(f'{rule.rule} -> {response.status_code}')

        assert not fallos, (
            'Estas páginas responden a un visitante anónimo:\n  ' + '\n  '.join(fallos)
        )
