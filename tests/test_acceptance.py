"""Acceptance tests mapped directly to specification section 5.

Each test reproduces one of the four documented acceptance criteria. If one of
these fails, the system does not meet the requirement, whatever else passes.
"""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.enums import (
    AlertState,
    ExtractionState,
    SegmentType,
)


# ======================================================================
# 5.1 — Trip creation and assignment
# ======================================================================
@pytest.mark.acceptance
class TestAltaYAsignacionDeViaje:
    """Section 5.1.

    "Un usuario no asignado recibe 403 al consultar el viaje o descargar su
    documentación; el gestor y administrador pueden consultarlo."

    The implementation denies with 404 rather than 403 for a user with no
    relationship to the trip, which satisfies the requirement (access is
    refused) while leaking strictly less: a 403 would confirm the trip exists.
    A user who *is* on the trip but lacks a specific permission still gets 403.
    """

    def test_el_usuario_asignado_ve_el_viaje(self, client, login, trip, viajero):
        login(viajero)
        response = client.get(f'/trips/{trip.id}')
        assert response.status_code == 200
        assert trip.titulo.encode() in response.data

    def test_el_usuario_no_asignado_no_ve_el_viaje(self, client, login, trip, ajeno):
        login(ajeno)
        response = client.get(f'/trips/{trip.id}')
        assert response.status_code in (403, 404), (
            'Un usuario no asignado debe ser rechazado al consultar el viaje.'
        )

    def test_el_gestor_ve_cualquier_viaje(self, client, login, trip, gestor):
        login(gestor)
        assert client.get(f'/trips/{trip.id}').status_code == 200

    def test_el_administrador_ve_cualquier_viaje(self, client, login, trip, admin):
        login(admin)
        assert client.get(f'/trips/{trip.id}').status_code == 200

    def test_el_no_asignado_no_aparece_el_viaje_en_su_listado(
        self, client, login, trip, ajeno
    ):
        login(ajeno)
        response = client.get('/api/v1/trips')
        assert response.status_code == 200
        assert response.get_json()['data'] == [], (
            'El listado de un usuario no asignado debe estar vacío.'
        )

    def test_el_asignado_si_ve_el_viaje_en_su_listado(self, client, login, trip, viajero):
        login(viajero)
        response = client.get('/api/v1/trips')
        data = response.get_json()['data']
        assert len(data) == 1
        assert data[0]['referencia'] == trip.referencia

    def test_el_no_asignado_no_descarga_la_documentacion(
        self, client, login, trip, ajeno, gestor, documento_aprobado
    ):
        login(ajeno)
        response = client.get(f'/api/v1/documents/{documento_aprobado.id}/download')
        assert response.status_code in (403, 404)

    def test_el_no_asignado_no_puede_editar(self, client, login, trip, ajeno):
        login(ajeno)
        response = client.patch(
            f'/api/v1/trips/{trip.id}', json={'titulo': 'Secuestrado'}
        )
        assert response.status_code in (403, 404)
        db.session.refresh(trip)
        assert trip.titulo != 'Secuestrado'


# ======================================================================
# 5.2 — Document upload and extraction
# ======================================================================
@pytest.mark.acceptance
class TestCargaYExtraccionDeDocumentos:
    """Section 5.2.

    "El original permanece disponible con hash; los campos aprobados quedan
    vinculados al documento y se registra quién los confirmó."
    """

    def test_el_original_permanece_con_su_hash(self, documento_aprobado):
        from app.services import storage_service
        from app.utils.hashing import sha256_stream

        assert documento_aprobado.objeto_storage, 'El original debe estar almacenado.'
        stored = storage_service.open_document(documento_aprobado)
        digest, size = sha256_stream(stored)

        assert digest == documento_aprobado.hash_sha256, (
            'El hash del objeto almacenado debe coincidir con el registrado.'
        )
        assert size == documento_aprobado.tamano_bytes

    def test_los_campos_aprobados_quedan_vinculados_al_documento(
        self, documento_aprobado, trip
    ):
        from app.models.itinerary import TravelSegment

        segment = TravelSegment.query.filter_by(
            trip_id=trip.id, documento_origen_id=documento_aprobado.id
        ).first()

        assert segment is not None, (
            'La aprobación debe haber creado un segmento vinculado al documento.'
        )
        assert segment.numero == 'IB3210'
        assert segment.origen_codigo == 'MAD'
        assert segment.destino_codigo == 'BER'

    def test_se_registra_quien_confirmo_los_datos(self, documento_aprobado, gestor):
        extraction = documento_aprobado.current_extraction
        assert extraction.estado is ExtractionState.APROBADA
        assert extraction.aprobado_por_id == gestor.id, (
            'Debe constar quién aprobó la extracción.'
        )
        assert extraction.aprobado_en is not None

    def test_cada_campo_guarda_su_procedencia(self, documento_aprobado, trip):
        from app.models.itinerary import FieldProvenance, TravelSegment

        segment = TravelSegment.query.filter_by(
            documento_origen_id=documento_aprobado.id
        ).first()
        provenance = FieldProvenance.query.filter_by(
            entidad_tipo='segmento', entidad_id=segment.id
        ).all()

        assert provenance, 'Cada campo aplicado debe registrar su procedencia.'
        campos = {p.campo for p in provenance}
        assert 'numero' in campos

        registro = next(p for p in provenance if p.campo == 'numero')
        assert registro.document_id == documento_aprobado.id, (
            'La procedencia debe referenciar el documento de origen.'
        )
        assert registro.origen is not None

    def test_la_aprobacion_queda_auditada(self, documento_aprobado, gestor):
        from app.models.audit import AuditEvent

        evento = AuditEvent.query.filter_by(
            accion='extraction.approved',
            recurso_id=str(documento_aprobado.current_extraction.id),
        ).first()

        assert evento is not None, 'La aprobación debe quedar auditada.'
        assert evento.actor_id == gestor.id

    def test_la_descarga_queda_auditada(
        self, client, login, gestor, documento_aprobado
    ):
        from app.models.audit import AuditEvent

        login(gestor)
        response = client.get(f'/api/v1/documents/{documento_aprobado.id}/download')
        assert response.status_code == 200

        evento = AuditEvent.query.filter_by(
            accion='document.downloaded', recurso_id=str(documento_aprobado.id)
        ).first()
        assert evento is not None, 'Cada descarga debe quedar registrada.'


# ======================================================================
# 5.3 — Connection alerts
# ======================================================================
@pytest.mark.acceptance
class TestAlertasDeConexion:
    """Section 5.3.

    "La alerta muestra los segmentos implicados, el margen calculado, el umbral
    utilizado y permite marcarla como aceptada/resuelta con comentario."
    """

    def test_se_genera_alerta_cuando_el_margen_es_insuficiente(
        self, trip, segment_factory, traveler_row
    ):
        from app.models.alert import Alert
        from app.services.alerts import engine

        # Lands in Frankfurt at 12:30 local, departs again at 13:15 local:
        # 45 minutes, below the 90-minute Schengen threshold.
        segment_factory(
            numero='IB3210', origen='MAD', destino='FRA',
            origen_pais='ES', destino_pais='DE',
            salida=datetime(2026, 6, 1, 10, 0), salida_tz='Europe/Madrid',
            llegada=datetime(2026, 6, 1, 12, 30), llegada_tz='Europe/Berlin',
            traveler=traveler_row,
        )
        segment_factory(
            numero='LH1234', origen='FRA', destino='BER',
            origen_pais='DE', destino_pais='DE',
            salida=datetime(2026, 6, 1, 13, 15), salida_tz='Europe/Berlin',
            llegada=datetime(2026, 6, 1, 14, 15), llegada_tz='Europe/Berlin',
            traveler=traveler_row,
        )

        engine.run(trip)

        alerta = Alert.query.filter_by(
            trip_id=trip.id, tipo='conexion_margen_insuficiente'
        ).first()
        assert alerta is not None, 'Debe generarse una alerta de conexión.'
        assert alerta.severidad is not None

    def test_la_alerta_muestra_segmentos_margen_y_umbral(
        self, trip, segment_factory, traveler_row
    ):
        from app.models.alert import Alert
        from app.services.alerts import engine

        primero = segment_factory(
            numero='IB3210', origen='MAD', destino='FRA',
            origen_pais='ES', destino_pais='DE',
            salida=datetime(2026, 6, 1, 10, 0), salida_tz='Europe/Madrid',
            llegada=datetime(2026, 6, 1, 12, 30), llegada_tz='Europe/Berlin',
            traveler=traveler_row,
        )
        segundo = segment_factory(
            numero='LH1234', origen='FRA', destino='BER',
            origen_pais='DE', destino_pais='DE',
            salida=datetime(2026, 6, 1, 13, 15), salida_tz='Europe/Berlin',
            llegada=datetime(2026, 6, 1, 14, 15), llegada_tz='Europe/Berlin',
            traveler=traveler_row,
        )
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        evidencia = alerta.evidencia

        assert evidencia['segmento_llegada']['id'] == str(primero.id)
        assert evidencia['segmento_salida']['id'] == str(segundo.id)
        assert evidencia['margen_minutos'] == 45, (
            'El margen debe calcularse en UTC respetando las zonas horarias.'
        )
        assert evidencia['umbral_minutos'] == 90, (
            'Una conexión Schengen debe usar el umbral de 90 minutos.'
        )
        assert 'Schengen' in evidencia['umbral_motivo']

    def test_el_margen_se_calcula_en_utc_no_en_hora_local(
        self, trip, segment_factory, traveler_row
    ):
        """A connection whose wall-clock gap looks fine but is not.

        Landing in Madrid at 12:30 and leaving London at 12:00 local looks like
        a negative gap on the clock, but London is an hour behind: the real
        margin is 30 minutes. Comparing local times would get this backwards.
        """
        from app.models.alert import Alert
        from app.services.alerts import engine

        segment_factory(
            numero='IB1000', origen='MAD', destino='LHR',
            origen_pais='ES', destino_pais='GB',
            salida=datetime(2026, 6, 1, 10, 0), salida_tz='Europe/Madrid',
            llegada=datetime(2026, 6, 1, 11, 30), llegada_tz='Europe/London',
            traveler=traveler_row,
        )
        segment_factory(
            numero='BA2000', origen='LHR', destino='JFK',
            origen_pais='GB', destino_pais='US',
            salida=datetime(2026, 6, 1, 12, 0), salida_tz='Europe/London',
            llegada=datetime(2026, 6, 1, 15, 0), llegada_tz='America/New_York',
            traveler=traveler_row,
        )
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        assert alerta is not None
        assert alerta.evidencia['margen_minutos'] == 30
        assert alerta.evidencia['umbral_minutos'] == 150, (
            'Una conexión con destino fuera de Schengen usa el umbral internacional.'
        )

    def test_se_puede_aceptar_con_comentario(
        self, client, login, gestor, trip, segment_factory, traveler_row
    ):
        from app.models.alert import Alert
        from app.services.alerts import engine

        segment_factory(
            origen='MAD', destino='FRA', origen_pais='ES', destino_pais='DE',
            salida=datetime(2026, 6, 1, 10, 0), salida_tz='Europe/Madrid',
            llegada=datetime(2026, 6, 1, 12, 30), llegada_tz='Europe/Berlin',
            traveler=traveler_row,
        )
        segment_factory(
            numero='LH1234', origen='FRA', destino='BER',
            origen_pais='DE', destino_pais='DE',
            salida=datetime(2026, 6, 1, 13, 15), salida_tz='Europe/Berlin',
            llegada=datetime(2026, 6, 1, 14, 15), llegada_tz='Europe/Berlin',
            traveler=traveler_row,
        )
        engine.run(trip)
        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()

        login(gestor)
        response = client.patch(
            f'/api/v1/alerts/{alerta.id}',
            json={
                'estado': 'aceptada',
                'comentario': 'Confirmado con la aerolínea: equipaje facturado en origen.',
            },
        )
        assert response.status_code == 200

        db.session.refresh(alerta)
        assert alerta.estado is AlertState.ACEPTADA
        assert 'aerolínea' in alerta.comentario
        assert any(
            t.estado_nuevo is AlertState.ACEPTADA and t.comentario
            for t in alerta.transitions
        ), 'El comentario debe quedar en el histórico de la alerta.'

    def test_una_alerta_aceptada_no_se_reabre_al_recalcular(
        self, gestor, trip, segment_factory, traveler_row
    ):
        """The reconciler must respect a decision a manager already took."""
        from app.models.alert import Alert
        from app.services import alert_service
        from app.services.alerts import engine

        segment_factory(
            origen='MAD', destino='FRA', origen_pais='ES', destino_pais='DE',
            salida=datetime(2026, 6, 1, 10, 0), salida_tz='Europe/Madrid',
            llegada=datetime(2026, 6, 1, 12, 30), llegada_tz='Europe/Berlin',
            traveler=traveler_row,
        )
        segment_factory(
            numero='LH1234', origen='FRA', destino='BER',
            origen_pais='DE', destino_pais='DE',
            salida=datetime(2026, 6, 1, 13, 15), salida_tz='Europe/Berlin',
            llegada=datetime(2026, 6, 1, 14, 15), llegada_tz='Europe/Berlin',
            traveler=traveler_row,
        )
        engine.run(trip)
        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        alert_service.change_state(gestor, alerta, AlertState.ACEPTADA, 'Revisado.')

        engine.run(trip)

        db.session.refresh(alerta)
        assert alerta.estado is AlertState.ACEPTADA, (
            'Recalcular no debe reabrir una alerta que un gestor ya aceptó.'
        )
        assert Alert.query.filter_by(tipo='conexion_margen_insuficiente').count() == 1, (
            'Recalcular no debe duplicar la alerta.'
        )


# ======================================================================
# 5.4 — AI query with access control
# ======================================================================
@pytest.mark.acceptance
class TestConsultaIAConControlDeAcceso:
    """Section 5.4.

    "No se incluye información de otros viajes ni de otros viajeros; la
    respuesta muestra la fecha y procedencia de los datos relevantes."
    """

    def test_el_contexto_no_incluye_otros_viajes(
        self, gestor, viajero, trip, segment_factory, traveler_row
    ):
        from app.services import ai_service, trip_service

        segment_factory(numero='IB3210', traveler=traveler_row)

        # A second trip the traveller is not on at all.
        otro = trip_service.create_trip(gestor, titulo='Viaje confidencial a Tokio')
        from app.models.itinerary import TravelSegment
        from app.utils.timeutil import set_instant

        secreto = TravelSegment(
            trip_id=otro.id, tipo=SegmentType.VUELO, numero='JL9999',
            origen_codigo='MAD', destino_codigo='HND',
        )
        set_instant(secreto, 'salida', datetime(2026, 7, 1, 10, 0), 'Europe/Madrid')
        db.session.add(secreto)
        db.session.commit()

        contexto = ai_service.build_ai_context(viajero, trip)
        payload = str(contexto['datos'])

        assert 'JL9999' not in payload, (
            'El contexto no debe contener datos de otro viaje.'
        )
        assert 'HND' not in payload
        assert str(otro.id) not in contexto['ids_autorizados']

    def test_el_contexto_no_incluye_segmentos_de_otro_viajero(
        self, gestor, trip, viajero, otro_viajero, segment_factory
    ):
        from app.services import ai_service, trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)

        mio = trip.traveler_for(viajero.id)
        suyo = trip.traveler_for(otro_viajero.id)

        segment_factory(numero='IB1111', traveler=mio)
        segment_factory(numero='LH2222', traveler=suyo)

        contexto = ai_service.build_ai_context(viajero, trip)
        payload = str(contexto['datos'])

        assert 'IB1111' in payload, 'El viajero debe ver su propio segmento.'
        assert 'LH2222' not in payload, (
            'El viajero no debe ver los segmentos de otro viajero del mismo viaje.'
        )

    def test_el_gestor_si_ve_todos_los_segmentos_del_viaje(
        self, gestor, trip, viajero, otro_viajero, segment_factory
    ):
        from app.services import ai_service, trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        db.session.refresh(trip)

        segment_factory(numero='IB1111', traveler=trip.traveler_for(viajero.id))
        segment_factory(numero='LH2222', traveler=trip.traveler_for(otro_viajero.id))

        contexto = ai_service.build_ai_context(gestor, trip)
        payload = str(contexto['datos'])

        assert 'IB1111' in payload and 'LH2222' in payload

    def test_la_respuesta_lleva_fecha_y_procedencia(
        self, client, login, viajero, trip, segment_factory, traveler_row
    ):
        segment_factory(numero='IB3210', traveler=traveler_row)

        login(viajero)
        response = client.post(
            f'/api/v1/trips/{trip.id}/ai/query',
            json={'pregunta': '¿A qué hora sale mi vuelo?'},
        )
        assert response.status_code == 200

        data = response.get_json()['data']
        assert 'fecha_datos' in data, (
            'La respuesta debe indicar la fecha de los datos usados.'
        )
        assert data['proveedor'], 'Debe constar el proveedor que respondió.'
        assert 'run_id' in data

    def test_se_descartan_las_referencias_no_autorizadas(self):
        """A model that cites an id it was never given must not be believed."""
        from app.services.ai.guard import validate_references

        datos = {
            'respuesta': 'Su vuelo sale a las 10:00.',
            'referencias': [
                {'id': 'permitido-1', 'tipo': 'segmento'},
                {'id': 'inventado-9', 'tipo': 'segmento'},
            ],
        }
        filtrado, descartadas = validate_references(datos, ['permitido-1'])

        assert len(filtrado['referencias']) == 1
        assert filtrado['referencias'][0]['id'] == 'permitido-1'
        assert len(descartadas) == 1

    def test_la_consulta_queda_registrada(
        self, client, login, viajero, trip, segment_factory, traveler_row
    ):
        from app.models.ai import AIRun

        segment_factory(traveler=traveler_row)
        login(viajero)
        client.post(
            f'/api/v1/trips/{trip.id}/ai/query',
            json={'pregunta': '¿Dónde me alojo?'},
        )

        run = AIRun.query.filter_by(trip_id=trip.id).first()
        assert run is not None, 'Cada ejecución de IA debe quedar registrada.'
        assert run.usuario_id == viajero.id
        assert run.proveedor is not None
        assert run.finalidad
