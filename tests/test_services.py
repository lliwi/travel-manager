"""Service-layer behaviour: trips, normalisation, extraction and settings."""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.enums import (
    ProvenanceOrigin,
    TripStatus,
)
from app.services import (
    normalization_service,
    provenance_service,
    settings_service,
    trip_service,
    user_service,
)
from app.utils.errors import ConflictError, ResourceNotFound, ValidationError


@pytest.mark.unit
class TestServicioDeViajes:
    def test_la_referencia_es_secuencial_por_año(self, gestor, seeded):
        primero = trip_service.create_trip(gestor, titulo='Uno')
        segundo = trip_service.create_trip(gestor, titulo='Dos')

        año = datetime.now().year
        assert primero.referencia == f'VJ-{año}-0001'
        assert segundo.referencia == f'VJ-{año}-0002'

    def test_no_se_admite_un_viaje_sin_titulo(self, gestor, seeded):
        with pytest.raises(ValidationError, match='título'):
            trip_service.create_trip(gestor, titulo='   ')

    def test_no_se_admite_que_termine_antes_de_empezar(self, gestor, seeded):
        with pytest.raises(ValidationError, match='anterior'):
            trip_service.create_trip(
                gestor, titulo='Imposible',
                inicio_local=datetime(2026, 6, 10, 10, 0), inicio_tz='Europe/Madrid',
                fin_local=datetime(2026, 6, 1, 10, 0), fin_tz='Europe/Madrid',
            )

    def test_las_fechas_guardan_el_triple(self, gestor, seeded):
        trip = trip_service.create_trip(
            gestor, titulo='Con fechas',
            inicio_local=datetime(2026, 6, 1, 10, 0), inicio_tz='Europe/Madrid',
        )
        assert trip.inicio_local == datetime(2026, 6, 1, 10, 0)
        assert trip.inicio_tz == 'Europe/Madrid'
        assert trip.inicio_utc.hour == 8

    def test_no_se_asigna_dos_veces_a_la_misma_persona(self, gestor, trip, viajero):
        with pytest.raises(ConflictError, match='ya está asignada'):
            trip_service.add_traveler(gestor, trip, viajero)

    def test_reasignar_a_quien_se_quitó_reutiliza_la_fila(self, gestor, trip, viajero):
        """The unique constraint on (trip, user) would reject a second row."""
        trip_service.remove_traveler(gestor, trip, viajero.id)
        db.session.refresh(trip)
        assert trip.traveler_for(viajero.id) is None

        vuelto = trip_service.add_traveler(gestor, trip, viajero)
        db.session.refresh(trip)

        assert trip.traveler_for(viajero.id) is not None
        assert not vuelto.is_deleted

    def test_quitar_a_quien_no_esta_asignado_falla(self, gestor, trip, ajeno):
        with pytest.raises(ResourceNotFound):
            trip_service.remove_traveler(gestor, trip, ajeno.id)

    def test_un_cambio_de_itinerario_incrementa_la_version(self, gestor, trip, otro_viajero):
        antes = trip.itinerary_version
        trip_service.add_traveler(gestor, trip, otro_viajero)
        assert trip.itinerary_version > antes

    def test_los_destinos_se_ordenan_solos(self, gestor, trip, seeded):
        a = trip_service.add_destination(gestor, trip, ciudad='Berlín', pais_codigo='DE')
        b = trip_service.add_destination(gestor, trip, ciudad='Praga', pais_codigo='CZ')
        assert [a.orden, b.orden] == [1, 2] or [a.orden, b.orden] == [0, 1]

    def test_eliminar_un_destino_cierra_el_hueco(self, gestor, trip, seeded):
        for ciudad in ('Berlín', 'Praga', 'Viena'):
            trip_service.add_destination(gestor, trip, ciudad=ciudad)
        db.session.refresh(trip)

        medio = trip.destinations[1]
        trip_service.remove_destination(gestor, trip, medio.id)
        db.session.refresh(trip)

        ordenes = sorted(d.orden for d in trip.destinations)
        assert ordenes == list(range(len(ordenes))), (
            'Los órdenes deben quedar consecutivos tras eliminar uno.'
        )

    def test_un_viaje_cancelado_no_se_edita(self, gestor, trip):
        trip_service.change_status(gestor, trip, TripStatus.CANCELADO)

        with pytest.raises(ConflictError, match='cancelado'):
            trip_service.update_trip(gestor, trip, titulo='Nuevo título')


@pytest.mark.unit
class TestNormalizacion:
    """Specification section 2.3 step 5."""

    def test_el_localizador_se_normaliza(self, app, seeded):
        resultado = normalization_service.normalize(
            {'servicios': [{'campos': {'localizador': ' xyz 12a '}, 'confianzas': {'localizador': 1.0}}]}
        )
        assert resultado.servicios[0]['campos']['localizador'] == 'XYZ12A'

    def test_un_localizador_raro_baja_la_confianza(self, app, seeded):
        resultado = normalization_service.normalize(
            {'servicios': [{'campos': {'localizador': 'AB'}, 'confianzas': {'localizador': 1.0}}]}
        )
        assert resultado.servicios[0]['confianzas']['localizador'] < 1.0
        assert resultado.avisos

    def test_el_aeropuerto_aporta_su_zona_horaria(self, app, seeded):
        """The catalogue is reference data; it wins over a model's guess."""
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {
                'origen_codigo': 'mad',
                'salida': {'local': '2026-06-01T10:00', 'zona_horaria': None},
            },
            'confianzas': {'origen_codigo': 0.9, 'salida': 0.9},
        }]})

        assert resultado.servicios[0]['campos']['origen_codigo'] == 'MAD'
        assert resultado.servicios[0]['campos']['origen_pais'] == 'ES'
        assert resultado.servicios[0]['campos']['salida']['zona_horaria'] == 'Europe/Madrid'

    def test_el_aeropuerto_corrige_una_zona_horaria_equivocada(self, app, seeded):
        """Filling the gap is not enough; the guess has to be overruled.

        Taken from a real Vueling confirmation whose Barcelona departure came
        back labelled Europe/London. Accepting it moves that flight an hour and
        every connection margin computed from it.
        """
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {
                'origen_codigo': 'MAD',
                'salida': {'local': '2026-06-01T10:00', 'zona_horaria': 'Europe/London'},
            },
            'confianzas': {'origen_codigo': 0.9, 'salida': 0.9},
        }]})

        assert resultado.servicios[0]['campos']['salida']['zona_horaria'] == 'Europe/Madrid'
        assert any('Europe/London' in aviso for aviso in resultado.avisos), (
            'Corregir en silencio esconde que el modelo se equivocó.'
        )

    def test_un_aeropuerto_desconocido_baja_la_confianza(self, app, seeded):
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {'origen_codigo': 'ZZZ'},
            'confianzas': {'origen_codigo': 1.0},
        }]})
        assert resultado.avisos
        assert resultado.servicios[0]['confianzas']['origen_codigo'] < 1.0

    def test_una_zona_horaria_asumida_baja_la_confianza(self, app, seeded):
        """A guessed timezone makes every derived margin a guess too."""
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {'salida': {'local': '2026-06-01T10:00', 'zona_horaria': None}},
            'confianzas': {'salida': 1.0},
        }]})

        assert resultado.servicios[0]['campos']['salida']['zona_horaria'] == 'Europe/Madrid'
        assert resultado.servicios[0]['confianzas']['salida'] < 1.0
        assert any('zona horaria' in a for a in resultado.avisos)

    @pytest.mark.parametrize('entrada,esperado', [
        ('2026-06-01T10:00', '2026-06-01T10:00'),
        ('2026-06-01 10:00', '2026-06-01T10:00'),
        ('01/06/2026 10:00', '2026-06-01T10:00'),
        ('01-06-2026 10:00', '2026-06-01T10:00'),
        ('01.06.2026 10:00', '2026-06-01T10:00'),
    ])
    def test_formatos_de_fecha_habituales(self, app, seeded, entrada, esperado):
        """European documents are day-first; parsing must not flip the month."""
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {'salida': {'local': entrada, 'zona_horaria': 'Europe/Madrid'}},
            'confianzas': {},
        }]})
        assert resultado.servicios[0]['campos']['salida']['local'] == esperado

    def test_una_fecha_ilegible_se_señala(self, app, seeded):
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {'salida': {'local': 'el martes que viene',
                                  'zona_horaria': 'Europe/Madrid'}},
            'confianzas': {'salida': 0.8},
        }]})
        assert resultado.servicios[0]['campos']['salida']['local'] is None
        assert any('interpretar' in a for a in resultado.avisos)

    def test_la_moneda_se_normaliza(self, app, seeded):
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {'moneda': '€', 'importe': '1.234,50'},
            'confianzas': {},
        }]})
        assert resultado.servicios[0]['campos']['moneda'] == 'EUR'

    def test_la_confianza_global_es_la_media(self, app, seeded):
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {'a': 'x', 'b': 'y'},
            'confianzas': {'a': 0.8, 'b': 1.0},
        }]})
        assert resultado.confianza_global == pytest.approx(0.9)


@pytest.mark.unit
class TestProcedencia:
    def test_un_campo_manual_se_marca_como_manual(self, gestor, trip, segment_factory):
        segmento = segment_factory()
        provenance_service.record_manual_fields(
            segmento, 'segmento', ['numero'], gestor, commit=True
        )

        actual = provenance_service.current_for(segmento, 'segmento')
        assert actual['numero'].origen is ProvenanceOrigin.MANUAL
        assert float(actual['numero'].confianza) == 1.0

    def test_un_fragmento_literal_marca_origen_documento(
        self, gestor, documento_procesado, trip, segment_factory
    ):
        """The literal span is exactly what separates 'documento' from 'ia'."""
        segmento = segment_factory()
        extraction = documento_procesado.current_extraction

        provenance_service.record_extracted_field(
            segmento, 'segmento', 'numero', 'IB3210', extraction,
            confianza=0.95, pagina=1, fragmento='Vuelo: IB3210', commit=True,
        )
        provenance_service.record_extracted_field(
            segmento, 'segmento', 'salida_tz', 'Europe/Madrid', extraction,
            confianza=0.8, fragmento=None, commit=True,
        )

        actual = provenance_service.current_for(segmento, 'segmento')
        assert actual['numero'].origen is ProvenanceOrigin.DOCUMENTO
        assert actual['salida_tz'].origen is ProvenanceOrigin.IA

    def test_el_historial_conserva_las_correcciones(self, gestor, trip, segment_factory):
        segmento = segment_factory()
        provenance_service.record_manual_change(
            segmento, 'segmento', 'numero', 'IB3210', 'IB3211', gestor, commit=True
        )
        provenance_service.record_manual_change(
            segmento, 'segmento', 'numero', 'IB3211', 'IB3212', gestor, commit=True
        )

        historial = provenance_service.history_for(segmento, 'segmento', 'numero')
        assert len(historial) == 2
        assert historial[-1].valor_actual == 'IB3212'
        assert provenance_service.current_for(
            segmento, 'segmento'
        )['numero'].valor_actual == 'IB3212'

    def test_el_rollup_refleja_la_confianza_minima(
        self, gestor, documento_procesado, segment_factory
    ):
        segmento = segment_factory()
        extraction = documento_procesado.current_extraction

        for campo, confianza in (('numero', 0.95), ('origen_codigo', 0.55)):
            provenance_service.record_extracted_field(
                segmento, 'segmento', campo, 'x', extraction,
                confianza=confianza, fragmento='algo', commit=True,
            )

        minima, requiere = provenance_service.recalculate_rollup(
            segmento, 'segmento', commit=True
        )
        assert float(minima) == 0.55
        assert requiere, 'Por debajo del umbral debe marcarse para revisión.'


@pytest.mark.unit
class TestAjustes:
    def test_se_siembran_los_valores_por_defecto(self, app):
        creados = settings_service.seed_defaults()
        assert creados > 0
        assert settings_service.get_bool('COSTES_HABILITADOS') is False

    def test_sembrar_dos_veces_no_duplica(self, app):
        settings_service.seed_defaults()
        assert settings_service.seed_defaults() == 0

    def test_no_se_sobrescribe_lo_que_ajustó_un_administrador(self, app, admin):
        settings_service.seed_defaults()
        settings_service.set_value('COSTES_HABILITADOS', True, actor=admin)

        settings_service.seed_defaults()

        assert settings_service.get_bool('COSTES_HABILITADOS') is True, (
            'Una actualización no debe revertir la configuración de la organización.'
        )

    def test_los_tipos_se_respetan(self, app, admin):
        settings_service.seed_defaults()

        settings_service.set_value('RETENCION_DOCUMENTOS_DIAS', 365, actor=admin)
        assert settings_service.get_int('RETENCION_DOCUMENTOS_DIAS') == 365

        settings_service.set_value('DOCUMENTOS_UMBRAL_REVISION', 0.75, actor=admin)
        assert settings_service.get_float('DOCUMENTOS_UMBRAL_REVISION') == 0.75


@pytest.mark.unit
class TestServicioDeUsuarios:
    def test_no_se_admite_una_contrasena_debil(self, admin, seeded):
        with pytest.raises(ValidationError):
            user_service.create_user(
                admin, 'debil', 'debil@x.test', 'Débil', 'corta1A',
                role_codes=['usuario'],
            )

    def test_no_se_admite_un_correo_duplicado(self, admin, gestor, seeded):
        with pytest.raises(ConflictError, match='Ya existe'):
            user_service.create_user(
                admin, 'otro', gestor.email, 'Otro',
                'Contrasena-Muy-Segura-2026', role_codes=['usuario'],
            )

    def test_no_se_puede_eliminar_uno_mismo(self, admin, seeded):
        with pytest.raises(ValidationError, match='propia cuenta'):
            user_service.delete_user(admin, admin)

    def test_el_correo_se_guarda_en_minusculas(self, admin, seeded):
        usuario = user_service.create_user(
            admin, 'mayus', 'MAYUS@Example.TEST', 'Mayús',
            'Contrasena-Muy-Segura-2026', role_codes=['usuario'],
        )
        assert usuario.email == 'mayus@example.test'


@pytest.mark.unit
class TestFechasDesdeElItinerario:
    """A trip created from its documents takes its dates from them."""

    def test_se_deducen_cuando_el_viaje_no_tiene_fechas(
        self, gestor, seeded, segment_factory
    ):
        from datetime import datetime

        from app.services import trip_service

        trip = trip_service.create_trip(gestor, titulo='Sin fechas')
        assert trip.inicio_utc is None and trip.fin_utc is None

        from app.models.itinerary import TravelSegment
        from app.utils.timeutil import set_instant

        for salida, llegada in (
            (datetime(2026, 6, 1, 10, 0), datetime(2026, 6, 1, 12, 30)),
            (datetime(2026, 6, 4, 18, 0), datetime(2026, 6, 4, 20, 30)),
        ):
            seg = TravelSegment(trip_id=trip.id, numero='IB1')
            set_instant(seg, 'salida', salida, 'Europe/Madrid')
            set_instant(seg, 'llegada', llegada, 'Europe/Madrid')
            db.session.add(seg)
        db.session.commit()
        db.session.refresh(trip)

        aplicados = trip_service.sync_dates_from_itinerary(trip, commit=True)

        assert set(aplicados) == {'inicio', 'fin'}
        assert trip.inicio_local == datetime(2026, 6, 1, 10, 0)
        assert trip.fin_local == datetime(2026, 6, 4, 20, 30)
        assert trip.inicio_tz == 'Europe/Madrid'

    def test_no_se_pisan_las_fechas_que_puso_el_gestor(
        self, gestor, seeded, trip, segment_factory
    ):
        """A manager may deliberately set a window wider than the bookings."""
        from app.services import trip_service

        inicio_original = trip.inicio_local
        segment_factory(numero='IB1')
        db.session.refresh(trip)

        aplicados = trip_service.sync_dates_from_itinerary(trip, commit=True)

        assert aplicados == []
        assert trip.inicio_local == inicio_original

    def test_sin_itinerario_no_hace_nada(self, gestor, seeded):
        from app.services import trip_service

        trip = trip_service.create_trip(gestor, titulo='Vacío')
        assert trip_service.sync_dates_from_itinerary(trip, commit=True) == []
        assert trip.inicio_utc is None


@pytest.mark.integration
class TestAdjuntarAlCrearElViaje:
    """Documents can be attached while the trip is being created."""

    def test_se_adjuntan_y_arranca_el_procesamiento(
        self, client, login, gestor, seeded, booking_pdf
    ):
        import io

        from app.models.document import Document
        from app.models.trip import Trip

        login(gestor)
        respuesta = client.post('/trips/nuevo', data={
            'titulo': 'Viaje con reservas',
            'estado': 'confirmado',
            'documentos': [
                (io.BytesIO(booking_pdf), 'reserva_vuelo.pdf'),
            ],
        }, content_type='multipart/form-data', follow_redirects=False)

        assert respuesta.status_code == 302

        trip = Trip.query.filter_by(titulo='Viaje con reservas').first()
        assert trip is not None, 'El viaje debe crearse.'

        documentos = Document.query.filter_by(trip_id=trip.id).all()
        assert len(documentos) == 1
        assert documentos[0].nombre_original == 'reserva_vuelo.pdf'
        assert documentos[0].hash_sha256

    def test_un_archivo_invalido_no_impide_crear_el_viaje(
        self, client, login, gestor, seeded
    ):
        """Losing the trip because one attachment was unreadable would be cruel."""
        import io

        from app.models.document import Document
        from app.models.trip import Trip

        login(gestor)
        respuesta = client.post('/trips/nuevo', data={
            'titulo': 'Viaje con adjunto malo',
            'estado': 'borrador',
            'documentos': [(io.BytesIO(b'MZ\x90\x00'), 'virus.exe')],
        }, content_type='multipart/form-data', follow_redirects=False)

        assert respuesta.status_code == 302

        trip = Trip.query.filter_by(titulo='Viaje con adjunto malo').first()
        assert trip is not None, 'El viaje debe crearse aunque falle el adjunto.'
        assert Document.query.filter_by(trip_id=trip.id).count() == 0

    def test_sin_adjuntos_todo_sigue_igual(self, client, login, gestor, seeded):
        from app.models.trip import Trip

        login(gestor)
        respuesta = client.post('/trips/nuevo', data={
            'titulo': 'Viaje sin adjuntos', 'estado': 'borrador',
        }, follow_redirects=False)

        assert respuesta.status_code == 302
        assert Trip.query.filter_by(titulo='Viaje sin adjuntos').first() is not None


@pytest.mark.unit
class TestFechasPropuestasAlAsignarViajero:
    """Assigning someone should not mean retyping what the documents said.

    The trip's span is already known once its bookings are approved, so the
    assignment form offers it. It is a proposal: the manager may narrow it for
    someone who joins midway, or clear it, which still means the whole trip.
    """

    def test_se_proponen_las_fechas_del_itinerario(self, gestor, seeded):
        from datetime import datetime

        from app.models.itinerary import TravelSegment
        from app.services import trip_service
        from app.utils.timeutil import set_instant

        trip = trip_service.create_trip(gestor, titulo='Londres')
        for salida, llegada in (
            (datetime(2026, 10, 31, 7, 55), datetime(2026, 10, 31, 9, 20)),
            (datetime(2026, 11, 2, 19, 55), datetime(2026, 11, 2, 23, 5)),
        ):
            seg = TravelSegment(trip_id=trip.id, numero='VY1')
            set_instant(seg, 'salida', salida, 'Europe/Madrid')
            set_instant(seg, 'llegada', llegada, 'Europe/Madrid')
            db.session.add(seg)
        db.session.commit()
        db.session.refresh(trip)

        desde, hasta, origen = trip_service.propose_traveler_window(trip)

        assert origen == 'itinerario'
        assert desde == datetime(2026, 10, 31, 7, 55)
        assert hasta == datetime(2026, 11, 2, 23, 5)

    def test_sin_itinerario_se_proponen_las_del_viaje(self, gestor, trip):
        from app.services import trip_service

        desde, hasta, origen = trip_service.propose_traveler_window(trip)

        assert origen == 'viaje'
        assert desde == trip.inicio_local
        assert hasta == trip.fin_local

    def test_el_itinerario_manda_sobre_las_del_viaje(
        self, gestor, trip, segment_factory
    ):
        """The itinerary is the evidence; the trip's dates may predate it.

        A manager who opened the trip with a rough window before any document
        arrived should be offered what the bookings actually say.
        """
        from datetime import datetime

        from app.services import trip_service
        from app.utils.timeutil import set_instant

        seg = segment_factory(numero='VY1')
        set_instant(seg, 'salida', datetime(2026, 10, 31, 7, 55), 'Europe/Madrid')
        set_instant(seg, 'llegada', datetime(2026, 10, 31, 9, 20), 'Europe/London')
        db.session.commit()
        db.session.refresh(trip)

        desde, hasta, origen = trip_service.propose_traveler_window(trip)

        assert origen == 'itinerario'
        assert desde == datetime(2026, 10, 31, 7, 55)

    def test_un_viaje_sin_nada_no_propone_nada(self, gestor, seeded):
        from app.services import trip_service

        trip = trip_service.create_trip(gestor, titulo='Sin nada')

        assert trip_service.propose_traveler_window(trip) == (None, None, None)

    def test_un_tramo_eliminado_no_cuenta(self, gestor, trip, segment_factory):
        """A withdrawn booking must not keep widening the proposal."""
        from datetime import datetime

        from app.services import trip_service
        from app.utils.timeutil import set_instant

        seg = segment_factory(numero='VY1')
        set_instant(seg, 'salida', datetime(2027, 1, 1, 8, 0), 'Europe/Madrid')
        set_instant(seg, 'llegada', datetime(2027, 1, 1, 10, 0), 'Europe/Madrid')
        seg.soft_delete(gestor)
        db.session.commit()
        db.session.refresh(trip)

        _, hasta, origen = trip_service.propose_traveler_window(trip)

        assert origen == 'viaje'
        assert hasta != datetime(2027, 1, 1, 10, 0)


@pytest.mark.integration
class TestFormularioDeAsignacion:
    """The proposal has to reach the form, and get out of the way on a mistake."""

    def test_el_formulario_llega_con_las_fechas_puestas(
        self, as_user, gestor, trip, segment_factory
    ):
        from datetime import datetime

        from app.utils.timeutil import set_instant

        seg = segment_factory(numero='VY1')
        set_instant(seg, 'salida', datetime(2026, 10, 31, 7, 55), 'Europe/Madrid')
        set_instant(seg, 'llegada', datetime(2026, 11, 2, 23, 5), 'Europe/Madrid')
        db.session.commit()

        with as_user(gestor) as client:
            html = client.get(f'/trips/{trip.id}/viajeros').get_data(as_text=True)

        assert '2026-10-31T07:55' in html
        assert '2026-11-02T23:05' in html
        assert 'propuestas a partir del itinerario' in html

    def test_un_envio_fallido_conserva_lo_que_escribio_el_gestor(
        self, as_user, gestor, trip, segment_factory
    ):
        """A proposal that overwrites a correction is worse than no proposal."""
        from datetime import datetime

        from app.utils.timeutil import set_instant

        seg = segment_factory(numero='VY1')
        set_instant(seg, 'salida', datetime(2026, 10, 31, 7, 55), 'Europe/Madrid')
        set_instant(seg, 'llegada', datetime(2026, 11, 2, 23, 5), 'Europe/Madrid')
        db.session.commit()

        with as_user(gestor) as client:
            html = client.post(
                f'/trips/{trip.id}/viajeros',
                data={
                    'user_id': '',  # inválido: el formulario vuelve con errores
                    'rol_en_viaje': 'viajero',
                    'desde_local': '2026-11-01T09:00',
                    'hasta_local': '2026-11-02T23:05',
                },
            ).get_data(as_text=True)

        assert '2026-11-01T09:00' in html, (
            'Lo que escribió el gestor no puede perderse al revalidar.'
        )
        assert '2026-10-31T07:55' not in html

    def test_la_api_ofrece_la_misma_propuesta(
        self, client, api_login, gestor, trip, segment_factory
    ):
        from datetime import datetime

        from app.utils.timeutil import set_instant

        seg = segment_factory(numero='VY1')
        set_instant(seg, 'salida', datetime(2026, 10, 31, 7, 55), 'Europe/Madrid')
        set_instant(seg, 'llegada', datetime(2026, 11, 2, 23, 5), 'Europe/Madrid')
        db.session.commit()

        api_login(gestor)
        cuerpo = client.get(
            f'/api/v1/trips/{trip.id}/travelers/proposed-window'
        ).get_json()['data']

        assert cuerpo['origen'] == 'itinerario'
        assert cuerpo['desde_local'].startswith('2026-10-31T07:55')

    def test_un_viajero_no_puede_pedir_la_propuesta(
        self, client, api_login, viajero, trip
    ):
        """Proposing a window is part of managing travellers."""
        api_login(viajero)

        respuesta = client.get(f'/api/v1/trips/{trip.id}/travelers/proposed-window')

        assert respuesta.status_code == 403


@pytest.mark.unit
class TestZonaHorariaDeUnLugarSinCodigo:
    """An airport arrives as a code; a hotel arrives as a city name.

    Nothing resolved the latter, so the zone the model guessed stood. A stay in
    London came back labelled Europe/Madrid, which moves its check-in by an hour
    and with it the night the alert engine believes is covered.
    """

    def test_el_alojamiento_toma_la_zona_de_su_ciudad(self, app, seeded):
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {
                'nombre': 'Zedwell Piccadilly Circus',
                'ciudad': 'London',
                'pais': 'GB',
                'check_in': {'local': '2026-10-31T15:00',
                             'zona_horaria': 'Europe/Madrid'},
                'check_out': {'local': '2026-11-02T11:00',
                              'zona_horaria': 'Europe/Madrid'},
            },
            'confianzas': {},
        }]}, clasificacion='hotel')

        campos = resultado.servicios[0]['campos']
        assert campos['check_in']['zona_horaria'] == 'Europe/London'
        assert campos['check_out']['zona_horaria'] == 'Europe/London'
        assert any('Europe/Madrid' in aviso for aviso in resultado.avisos), (
            'Corregirlo en silencio esconde que el modelo se equivocó.'
        )

    def test_sin_ciudad_conocida_se_usa_la_del_pais(self, app, seeded):
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {
                'nombre': 'Casa rural',
                'ciudad': 'Villanueva del Dato Inventado',
                'pais': 'ES',
                'check_in': {'local': '2026-06-01T15:00', 'zona_horaria': None},
            },
            'confianzas': {},
        }]}, clasificacion='hotel')

        zona = resultado.servicios[0]['campos']['check_in']['zona_horaria']
        assert zona == 'Europe/Madrid'

    def test_un_lugar_desconocido_no_se_inventa(self, app, seeded):
        """Refuse rather than guess: no catalogue entry, no correction."""
        resultado = normalization_service.normalize({'servicios': [{
            'campos': {
                'nombre': 'Hotel',
                'ciudad': 'Ciudad Inexistente',
                'pais': None,
                'check_in': {'local': '2026-06-01T15:00',
                             'zona_horaria': 'Europe/Lisbon'},
            },
            'confianzas': {},
        }]}, clasificacion='hotel')

        zona = resultado.servicios[0]['campos']['check_in']['zona_horaria']
        assert zona == 'Europe/Lisbon', (
            'Sin dato en el catálogo no hay nada mejor que lo que dijo el modelo.'
        )


@pytest.mark.unit
class TestElLugarEscritoEnOtroIdioma:
    """The catalogue holds «Londres»; the model wrote «London».

    An exact match on the city found nothing, so the stay kept the timezone the
    model had guessed and carried no country at all -- which left the security
    advisories with no destination to look up.
    """

    def _hotel(self, ciudad, pais=None):
        return normalization_service.normalize({'servicios': [{
            'campos': {
                'nombre': 'Zedwell Piccadilly Circus',
                'ciudad': ciudad,
                'pais': pais,
                'check_in': {'local': '2026-10-31T15:00',
                             'zona_horaria': 'Europe/Madrid'},
            },
            'confianzas': {},
        }]}, clasificacion='hotel').servicios[0]['campos']

    def test_se_resuelve_por_el_nombre_del_aeropuerto(self, app, seeded):
        campos = self._hotel('London')

        assert campos['check_in']['zona_horaria'] == 'Europe/London'

    def test_se_deduce_tambien_el_pais(self, app, seeded):
        """Without it the advisories have nothing to look up."""
        campos = self._hotel('London')

        assert campos['pais'] == 'GB'

    def test_sigue_funcionando_el_nombre_en_castellano(self, app, seeded):
        campos = self._hotel('Londres')

        assert campos['check_in']['zona_horaria'] == 'Europe/London'
        assert campos['pais'] == 'GB'

    def test_un_nombre_ambiguo_no_se_resuelve(self, app, seeded):
        """Refuse rather than guess: two zones is a question for a person."""
        from app.models.catalog import Location
        from app.services.normalization_service import resolver_lugar

        db.session.add(Location(
            codigo='XYZ1', nombre='London Ontario', ciudad='London Ontario',
            pais_codigo='CA', zona_horaria='America/Toronto', activo=True,
        ))
        db.session.commit()

        zona, pais, ciudad = resolver_lugar(['London'], None)

        assert (zona, pais, ciudad) == (None, None, None)

    def test_un_lugar_desconocido_sigue_sin_inventarse(self, app, seeded):
        from app.services.normalization_service import resolver_lugar

        assert resolver_lugar(['Ciudad Inexistente'], None) == (None, None, None)
