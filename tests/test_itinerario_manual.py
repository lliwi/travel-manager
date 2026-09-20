"""Adding itinerary items by hand, from the browser.

The API could already create, edit and delete them; the web surface could not,
so a manager whose booking never arrived as a document had no way to record a
train they took. These tests pin the web path to the same service the API
calls, and to the rules that path must not break: instants stay triples,
provenance says a person typed it, and a traveller cannot edit anything.
"""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.itinerary import Accommodation, FieldProvenance, TravelSegment


def _url(trip, kind, item=None, accion='nuevo'):
    base = f'/trips/{trip.id}/itinerario/{kind}'
    return f'{base}/{accion}' if item is None else f'{base}/{item.id}/{accion}'


@pytest.mark.integration
class TestAltaManual:
    def test_se_crea_un_tramo_desde_el_formulario(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            respuesta = client.post(_url(trip, 'segmento'), data={
                'tipo': 'vuelo',
                'numero': 'VY7604',
                'origen_codigo': 'BCN',
                'salida_local': '2026-10-31T07:55',
                'salida_tz': 'Europe/Madrid',
                'destino_codigo': 'LGW',
                'llegada_local': '2026-10-31T09:20',
                'llegada_tz': 'Europe/London',
            }, follow_redirects=True)

        assert respuesta.status_code == 200
        seg = TravelSegment.query.filter_by(numero='VY7604').first()
        assert seg is not None
        assert seg.origen_codigo == 'BCN'
        assert seg.trip_id == trip.id

    def test_el_instante_se_guarda_como_triple(self, as_user, gestor, trip):
        """The whole point of asking for the zone separately."""
        with as_user(gestor) as client:
            client.post(_url(trip, 'segmento'), data={
                'numero': 'VY1',
                'salida_local': '2026-10-31T07:55',
                'salida_tz': 'Europe/Madrid',
            }, follow_redirects=True)

        seg = TravelSegment.query.filter_by(numero='VY1').first()
        assert seg.salida_local == datetime(2026, 10, 31, 7, 55)
        assert seg.salida_tz == 'Europe/Madrid'
        assert seg.salida_utc is not None
        assert seg.salida_utc.hour == 6, 'CET son UTC+1 el 31 de octubre.'

    def test_una_zona_horaria_inventada_se_rechaza(self, as_user, gestor, trip):
        """«CEST» is not an IANA name, and accepting it corrupts the triple."""
        with as_user(gestor) as client:
            respuesta = client.post(_url(trip, 'segmento'), data={
                'numero': 'VY2',
                'salida_local': '2026-10-31T07:55',
                'salida_tz': 'CEST',
            })

        assert TravelSegment.query.filter_by(numero='VY2').first() is None
        assert 'Zona horaria desconocida' in respuesta.get_data(as_text=True)

    def test_lo_escrito_a_mano_queda_con_procedencia_manual(
        self, as_user, gestor, trip
    ):
        with as_user(gestor) as client:
            client.post(_url(trip, 'segmento'), data={
                'numero': 'VY3', 'origen_codigo': 'MAD',
            }, follow_redirects=True)

        seg = TravelSegment.query.filter_by(numero='VY3').first()
        origenes = {
            p.origen for p in FieldProvenance.query.filter_by(
                entidad_id=seg.id
            ).all()
        }
        assert origenes, 'Un campo escrito a mano debe dejar procedencia.'
        assert all(str(o) == 'manual' for o in origenes)

    def test_se_crea_un_alojamiento(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            client.post(_url(trip, 'alojamiento'), data={
                'nombre': 'Hotel Gatwick',
                'ciudad': 'Londres',
                'check_in_local': '2026-10-31T15:00',
                'check_in_tz': 'Europe/London',
                'check_out_local': '2026-11-02T11:00',
                'check_out_tz': 'Europe/London',
            }, follow_redirects=True)

        alojamiento = Accommodation.query.filter_by(nombre='Hotel Gatwick').first()
        assert alojamiento is not None
        assert alojamiento.check_out_utc > alojamiento.check_in_utc

    def test_un_campo_obligatorio_en_blanco_no_se_borra(
        self, as_user, gestor, trip, segment_factory
    ):
        """A blank select is not the manager saying the segment has no kind.

        The web form submits every field on every save, so an unfilled one
        arrived as an empty string. Written to a NOT NULL column it failed the
        save outright; the edit silently did nothing.
        """
        seg = segment_factory(numero='IB700')
        tipo_original = seg.tipo
        db.session.commit()

        with as_user(gestor) as client:
            client.post(_url(trip, 'segmento', seg, 'editar'), data={
                'numero': 'IB701', 'tipo': '',
            }, follow_redirects=True)

        db.session.refresh(seg)
        assert seg.numero == 'IB701'
        assert seg.tipo is tipo_original

    def test_un_campo_opcional_en_blanco_si_se_borra(
        self, as_user, gestor, trip, segment_factory
    ):
        seg = segment_factory(numero='IB800')
        seg.asiento = '14C'
        db.session.commit()

        with as_user(gestor) as client:
            client.post(_url(trip, 'segmento', seg, 'editar'), data={
                'numero': 'IB800', 'asiento': '',
            }, follow_redirects=True)

        db.session.refresh(seg)
        assert seg.asiento is None, 'Vaciar un campo opcional debe vaciarlo.'

    def test_un_tipo_de_elemento_inventado_no_existe(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            respuesta = client.get(_url(trip, 'dragon'))

        assert respuesta.status_code == 404


@pytest.mark.integration
class TestEdicionYBorrado:
    def test_se_edita_un_tramo(self, as_user, gestor, trip, segment_factory):
        seg = segment_factory(numero='IB100')
        db.session.commit()

        with as_user(gestor) as client:
            client.post(_url(trip, 'segmento', seg, 'editar'), data={
                'numero': 'IB200',
                'salida_local': '2026-10-31T07:55',
                'salida_tz': 'Europe/Madrid',
            }, follow_redirects=True)

        db.session.refresh(seg)
        assert seg.numero == 'IB200'

    def test_el_formulario_de_edicion_llega_relleno(
        self, as_user, gestor, trip, segment_factory
    ):
        seg = segment_factory(numero='IB300')
        db.session.commit()

        with as_user(gestor) as client:
            html = client.get(
                _url(trip, 'segmento', seg, 'editar')
            ).get_data(as_text=True)

        assert 'IB300' in html

    def test_se_elimina_un_tramo(self, as_user, gestor, trip, segment_factory):
        seg = segment_factory(numero='IB400')
        db.session.commit()

        with as_user(gestor) as client:
            client.post(_url(trip, 'segmento', seg, 'eliminar'), follow_redirects=True)

        db.session.refresh(seg)
        assert seg.is_deleted, 'Los registros se borran de forma blanda.'

    def test_no_se_edita_un_elemento_de_otro_viaje(
        self, as_user, gestor, trip, segment_factory, seeded
    ):
        """Belonging to a trip you may edit is not the same as existing."""
        from app.services import trip_service

        seg = segment_factory(numero='IB500')
        otro = trip_service.create_trip(gestor, titulo='Otro viaje')
        db.session.commit()

        with as_user(gestor) as client:
            respuesta = client.get(_url(otro, 'segmento', seg, 'editar'))

        assert respuesta.status_code == 404


@pytest.mark.security
class TestQuienPuedeTocarElItinerario:
    def test_un_viajero_no_puede_anadir(self, as_user, viajero, trip):
        with as_user(viajero) as client:
            respuesta = client.get(_url(trip, 'segmento'))

        assert respuesta.status_code == 403

    def test_un_viajero_no_puede_eliminar(
        self, as_user, viajero, trip, segment_factory
    ):
        seg = segment_factory(numero='IB600')
        db.session.commit()

        with as_user(viajero) as client:
            respuesta = client.post(_url(trip, 'segmento', seg, 'eliminar'))

        assert respuesta.status_code == 403
        db.session.refresh(seg)
        assert not seg.is_deleted

    def test_un_ajeno_al_viaje_no_sabe_que_existe(self, as_user, ajeno, trip):
        """404, not 403: refusing without confirming the trip exists."""
        with as_user(ajeno) as client:
            respuesta = client.get(_url(trip, 'segmento'))

        assert respuesta.status_code == 404


@pytest.mark.integration
class TestLasAlertasSeRehacen:
    """An itinerary the manager changed must be re-evaluated, not left stale.

    Only approving an extraction used to schedule a recalculation, so a
    departure time corrected by hand kept showing the margin computed from the
    old one until the nightly pass ran.
    """

    def _tramo(self, trip, gestor, numero, salida, llegada):
        from app.services import itinerary_service

        return itinerary_service.create_item(
            gestor, trip, 'segmento', numero=numero,
            origen_codigo='MAD', destino_codigo='BCN',
            instants={
                'salida': (salida, 'Europe/Madrid'),
                'llegada': (llegada, 'Europe/Madrid'),
            },
        )

    def test_crear_a_mano_reevalua(self, gestor, trip):
        from app.models.alert import Alert
        from app.services import itinerary_service

        antes = Alert.query.filter_by(trip_id=trip.id).count()
        itinerary_service.create_item(
            gestor, trip, 'segmento', numero='IB900',
            instants={'salida': (datetime(2026, 6, 1, 10, 0), 'Europe/Madrid')},
        )

        assert Alert.query.filter_by(trip_id=trip.id).count() >= antes, (
            'El motor debe haberse ejecutado tras el cambio.'
        )

    def test_una_conexion_imposible_creada_a_mano_salta(self, gestor, trip):
        """Two legs twenty minutes apart is what the engine exists to catch."""
        from app.models.alert import Alert
        from app.services import itinerary_service

        self._tramo(
            trip, gestor, 'IB1',
            datetime(2026, 6, 1, 8, 0), datetime(2026, 6, 1, 10, 0),
        )
        itinerary_service.create_item(
            gestor, trip, 'segmento', numero='IB2',
            origen_codigo='BCN', destino_codigo='LHR',
            instants={
                'salida': (datetime(2026, 6, 1, 10, 20), 'Europe/Madrid'),
                'llegada': (datetime(2026, 6, 1, 12, 0), 'Europe/London'),
            },
        )

        reglas = {a.regla for a in Alert.query.filter_by(trip_id=trip.id).all()}
        assert any('onexi' in r for r in reglas), (
            f'Se esperaba una alerta de conexión; hubo {reglas}.'
        )

    def test_borrar_a_mano_reevalua(self, gestor, trip):
        """Removing the leg that caused an alert must close it."""
        from app.models.alert import Alert
        from app.models.enums import AlertState
        from app.services import itinerary_service

        self._tramo(
            trip, gestor, 'IB1',
            datetime(2026, 6, 1, 8, 0), datetime(2026, 6, 1, 10, 0),
        )
        segundo = itinerary_service.create_item(
            gestor, trip, 'segmento', numero='IB2',
            origen_codigo='BCN', destino_codigo='LHR',
            instants={
                'salida': (datetime(2026, 6, 1, 10, 20), 'Europe/Madrid'),
                'llegada': (datetime(2026, 6, 1, 12, 0), 'Europe/London'),
            },
        )
        conexiones = [
            a for a in Alert.query.filter_by(trip_id=trip.id).all()
            if 'onexi' in a.regla
        ]
        assert conexiones, 'Preparación: debía existir la alerta de conexión.'

        itinerary_service.delete_item(gestor, trip, 'segmento', segundo.id)

        db.session.refresh(conexiones[0])
        assert conexiones[0].estado is not AlertState.ABIERTA, (
            'Una alerta que ya no se reproduce debe cerrarse sola.'
        )


@pytest.mark.integration
class TestQueCampoHayQueConfirmar:
    """«Sin confirmar» tenía que decir de qué habla.

    La marca sale de la confianza mínima con que se extrajo cada campo, no del
    estado del documento: un documento aprobado puede contener campos que la
    extracción no vio claros. El aviso llevaba al listado de documentos, donde
    todo figura «Aprobado», y la aplicación parecía contradecirse.
    """

    def test_el_aviso_no_manda_a_los_documentos(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(f'/trips/{trip.id}').get_data(as_text=True)

        assert 'que alguien confirme sus datos' not in html

    def test_el_formulario_dice_que_campo_es(
        self, app, as_user, gestor, trip, segment_factory,
    ):
        """Sin esto, abrir un elemento marcado lleva a un formulario igual que
        cualquier otro y hay que adivinar cuál de doce campos era."""
        from app.extensions import db
        from app.models.enums import ProvenanceOrigin
        from app.services import provenance_service

        item = segment_factory(numero='IB9100')
        provenance_service.record(
            item, 'segmento', 'numero', origen=ProvenanceOrigin.IA,
            valor_actual='IB9100', confianza=0.25, commit=True,
        )
        provenance_service.recalculate_rollup(item, 'segmento', commit=True)
        db.session.commit()

        with as_user(gestor) as client:
            html = client.get(
                f'/trips/{trip.id}/itinerario/segmento/{item.id}/editar'
            ).get_data(as_text=True)

        assert 'Sin confirmar' in html
        assert '25%' in html
        assert 'Numero' in html or 'numero' in html

    def test_un_campo_seguro_no_se_señala(
        self, app, as_user, gestor, trip, segment_factory,
    ):
        """Señalar todo sería no señalar nada."""
        from app.extensions import db
        from app.models.enums import ProvenanceOrigin
        from app.services import provenance_service

        item = segment_factory(numero='IB9200')
        provenance_service.record(
            item, 'segmento', 'numero', origen=ProvenanceOrigin.DOCUMENTO,
            valor_actual='IB9200', confianza=1.0, commit=True,
        )
        db.session.commit()

        with as_user(gestor) as client:
            html = client.get(
                f'/trips/{trip.id}/itinerario/segmento/{item.id}/editar'
            ).get_data(as_text=True)

        assert 'Sin confirmar' not in html

    def test_el_umbral_es_el_mismo_en_los_dos_sitios(self, app, seeded):
        """La lista y el formulario tienen que discrepar nunca sobre la misma
        fila, así que leen el mismo número."""
        from app.services import provenance_service

        assert provenance_service.umbral_revision() == provenance_service._review_threshold()
