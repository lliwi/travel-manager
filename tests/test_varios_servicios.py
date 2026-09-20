"""One document, several services.

A return ticket is two flights in a single confirmation email, and a hotel
booking can cover two rooms with different dates. Extracting only the first was
silently losing half the itinerary.
"""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.enums import DocumentClassification
from app.models.itinerary import TravelSegment
from app.services import extraction_service


def _dos_vuelos():
    """The payload shape a return booking produces."""
    return {'servicios': [
        {
            'campos': {
                'numero_vuelo': 'VY7622', 'aerolinea': 'Vueling',
                'localizador': 'ODIVYR',
                'origen_codigo': 'BCN', 'destino_codigo': 'LGW',
                'salida': {'local': '2026-10-31T19:55', 'zona_horaria': 'Europe/Madrid'},
                'llegada': {'local': '2026-10-31T21:20', 'zona_horaria': 'Europe/London'},
            },
            'confianzas': {'numero_vuelo': 0.85, 'origen_codigo': 0.85},
            'procedencias': {'numero_vuelo': {'pagina': 1, 'fragmento': 'VY7622'}},
        },
        {
            'campos': {
                'numero_vuelo': 'VY7623', 'aerolinea': 'Vueling',
                'localizador': 'ODIVYR',
                'origen_codigo': 'LGW', 'destino_codigo': 'BCN',
                'salida': {'local': '2026-11-02T07:00', 'zona_horaria': 'Europe/London'},
                'llegada': {'local': '2026-11-02T10:15', 'zona_horaria': 'Europe/Madrid'},
            },
            'confianzas': {'numero_vuelo': 0.85, 'origen_codigo': 0.85},
            'procedencias': {'numero_vuelo': {'pagina': 1, 'fragmento': 'VY7623'}},
        },
    ]}


@pytest.fixture
def documento_ida_y_vuelta(app, gestor, documento_procesado):
    """A processed document whose extraction describes two flights."""
    document = documento_procesado
    document.clasificacion = DocumentClassification.VUELO
    db.session.commit()

    extraction = document.current_extraction
    extraction.payload = _dos_vuelos()
    extraction.payload_normalizado = _dos_vuelos()
    extraction.confianza_global = 0.85
    db.session.commit()
    return document


@pytest.mark.integration
class TestAprobacionDeVariosServicios:
    def test_se_crean_dos_segmentos(self, gestor, documento_ida_y_vuelta, trip):
        resultado = extraction_service.approve(
            gestor, documento_ida_y_vuelta.current_extraction
        )

        assert len(resultado['entities']) == 2, (
            'Un billete de ida y vuelta debe producir dos segmentos.'
        )

        segmentos = TravelSegment.query.filter_by(
            trip_id=trip.id, documento_origen_id=documento_ida_y_vuelta.id,
            is_deleted=False,
        ).all()
        numeros = sorted(s.numero for s in segmentos)
        assert numeros == ['VY7622', 'VY7623']

    def test_cada_segmento_guarda_su_indice(self, gestor, documento_ida_y_vuelta):
        extraction_service.approve(gestor, documento_ida_y_vuelta.current_extraction)

        segmentos = TravelSegment.query.filter_by(
            documento_origen_id=documento_ida_y_vuelta.id, is_deleted=False,
        ).order_by(TravelSegment.documento_origen_indice).all()

        assert [s.documento_origen_indice for s in segmentos] == [0, 1]

    def test_las_rutas_no_se_mezclan(self, gestor, documento_ida_y_vuelta):
        """The whole point: the return leg must not overwrite the outbound."""
        extraction_service.approve(gestor, documento_ida_y_vuelta.current_extraction)

        segmentos = {
            s.numero: s for s in TravelSegment.query.filter_by(
                documento_origen_id=documento_ida_y_vuelta.id, is_deleted=False,
            ).all()
        }

        assert segmentos['VY7622'].origen_codigo == 'BCN'
        assert segmentos['VY7622'].destino_codigo == 'LGW'
        assert segmentos['VY7623'].origen_codigo == 'LGW'
        assert segmentos['VY7623'].destino_codigo == 'BCN'

    def test_cada_uno_conserva_sus_horas(self, gestor, documento_ida_y_vuelta):
        extraction_service.approve(gestor, documento_ida_y_vuelta.current_extraction)

        ida = TravelSegment.query.filter_by(numero='VY7622').first()
        vuelta = TravelSegment.query.filter_by(numero='VY7623').first()

        assert ida.salida_local == datetime(2026, 10, 31, 19, 55)
        assert ida.salida_tz == 'Europe/Madrid'
        assert vuelta.salida_local == datetime(2026, 11, 2, 7, 0)
        assert vuelta.salida_tz == 'Europe/London'

    def test_se_registra_una_aplicacion_por_servicio(
        self, gestor, documento_ida_y_vuelta
    ):
        resultado = extraction_service.approve(
            gestor, documento_ida_y_vuelta.current_extraction
        )
        assert len(resultado['applications']) == 2

    def test_la_procedencia_es_de_cada_servicio(self, gestor, documento_ida_y_vuelta):
        from app.models.itinerary import FieldProvenance

        extraction_service.approve(gestor, documento_ida_y_vuelta.current_extraction)

        for numero in ('VY7622', 'VY7623'):
            segmento = TravelSegment.query.filter_by(numero=numero).first()
            filas = FieldProvenance.query.filter_by(
                entidad_tipo='segmento', entidad_id=segmento.id, campo='numero',
            ).all()
            assert filas, f'Falta la procedencia de {numero}.'
            assert filas[-1].valor_actual == numero, (
                'La procedencia de un vuelo no puede referirse al otro.'
            )


@pytest.mark.integration
class TestReaprobacion:
    def test_no_se_duplican_al_reaprobar(self, gestor, documento_ida_y_vuelta):
        """Re-approving updates the rows it made before, never adds a third."""
        extraction_service.approve(gestor, documento_ida_y_vuelta.current_extraction)

        extraction = extraction_service.create_version(
            documento_ida_y_vuelta, payload=_dos_vuelos(), commit=True
        )
        extraction.payload_normalizado = _dos_vuelos()
        db.session.commit()

        extraction_service.approve(gestor, extraction)

        assert TravelSegment.query.filter_by(
            documento_origen_id=documento_ida_y_vuelta.id, is_deleted=False,
        ).count() == 2

    def test_se_retira_un_servicio_que_ya_no_aparece(
        self, gestor, documento_ida_y_vuelta
    ):
        """A later extraction describing one flight withdraws the other."""
        extraction_service.approve(gestor, documento_ida_y_vuelta.current_extraction)
        assert TravelSegment.query.filter_by(
            documento_origen_id=documento_ida_y_vuelta.id, is_deleted=False,
        ).count() == 2

        solo_uno = {'servicios': [_dos_vuelos()['servicios'][0]]}
        extraction = extraction_service.create_version(
            documento_ida_y_vuelta, payload=solo_uno, commit=True
        )
        extraction.payload_normalizado = solo_uno
        db.session.commit()

        extraction_service.approve(gestor, extraction)

        vivos = TravelSegment.query.filter_by(
            documento_origen_id=documento_ida_y_vuelta.id, is_deleted=False,
        ).all()
        assert len(vivos) == 1
        assert vivos[0].numero == 'VY7622', (
            'Debe quedar el servicio que la nueva extracción sí describe.'
        )


@pytest.mark.unit
class TestRevisionDeVariosServicios:
    def test_la_pantalla_agrupa_por_servicio(self, gestor, documento_ida_y_vuelta):
        grupos = extraction_service.review_fields(
            documento_ida_y_vuelta.current_extraction
        )

        assert len(grupos) == 2
        assert 'BCN' in grupos[0]['titulo'] and 'LGW' in grupos[0]['titulo']
        assert 'LGW' in grupos[1]['titulo'] and 'BCN' in grupos[1]['titulo']

    def test_los_campos_llevan_el_indice_en_su_nombre(
        self, gestor, documento_ida_y_vuelta
    ):
        """So a correction to the return flight cannot land on the outbound."""
        grupos = extraction_service.review_fields(
            documento_ida_y_vuelta.current_extraction
        )

        assert all(c['nombre_form'].startswith('campo__0__') for c in grupos[0]['campos'])
        assert all(c['nombre_form'].startswith('campo__1__') for c in grupos[1]['campos'])

    def test_una_correccion_va_al_servicio_correcto(
        self, gestor, documento_ida_y_vuelta
    ):
        extraction = documento_ida_y_vuelta.current_extraction
        form = {'campo__1__numero_vuelo': 'VY9999'}

        correcciones = extraction_service.parse_review_form(extraction, form)

        assert correcciones == {1: {'numero_vuelo': 'VY9999'}}

    def test_la_correccion_se_aplica_solo_a_ese_servicio(
        self, gestor, documento_ida_y_vuelta
    ):
        extraction = documento_ida_y_vuelta.current_extraction

        extraction_service.approve(
            gestor, extraction, correcciones={1: {'numero_vuelo': 'VY9999'}}
        )

        numeros = sorted(
            s.numero for s in TravelSegment.query.filter_by(
                documento_origen_id=documento_ida_y_vuelta.id, is_deleted=False,
            ).all()
        )
        assert numeros == ['VY7622', 'VY9999'], (
            'Solo el segundo vuelo debía cambiar.'
        )


@pytest.mark.unit
class TestCompatibilidadConLoGuardado:
    """Extractions stored before this change must keep working."""

    def test_se_lee_una_extraccion_de_un_solo_campos(self, gestor, documento_procesado):
        extraction = documento_procesado.current_extraction
        extraction.payload = {'campos': {'numero_vuelo': 'IB3210'}}
        extraction.payload_normalizado = {'campos': {'numero_vuelo': 'IB3210'}}
        db.session.commit()

        servicios = extraction_service.servicios_de(extraction)

        assert len(servicios) == 1
        assert servicios[0]['campos']['numero_vuelo'] == 'IB3210'

    def test_se_puede_aprobar_una_extraccion_antigua(
        self, gestor, documento_procesado, trip
    ):
        documento_procesado.clasificacion = DocumentClassification.VUELO
        extraction = documento_procesado.current_extraction
        extraction.payload = {'campos': {'numero_vuelo': 'IB3210',
                                         'origen_codigo': 'MAD'}}
        extraction.payload_normalizado = extraction.payload
        db.session.commit()

        resultado = extraction_service.approve(gestor, extraction)

        assert len(resultado['entities']) == 1
        assert TravelSegment.query.filter_by(numero='IB3210').first() is not None


@pytest.mark.unit
class TestTextoDeUnCorreoHTML:
    """A confirmation email is a table, and a table has to survive as one.

    The airline puts each flight in its own row: number, airports and both
    times. Flattening every tag to a space -- while keeping the newlines the
    HTML source happens to contain between tags -- scattered each cell onto its
    own line, and with it the only thing that said which time belonged to which
    flight. The model that read it returned one flight and an invented date.
    """

    HTML = (
        '<html><body>\r\n'
        '  <table>\r\n'
        '    <tr>\r\n'
        '      <td>Outbound</td>\r\n'
        '      <td>31 October 2026</td>\r\n'
        '      <td>BCN</td>\r\n'
        '      <td>07:55h</td>\r\n'
        '      <td>LGW</td>\r\n'
        '      <td>09:20h</td>\r\n'
        '      <td>VY7604</td>\r\n'
        '    </tr>\r\n'
        '    <tr>\r\n'
        '      <td>Return</td>\r\n'
        '      <td>02 November 2026</td>\r\n'
        '      <td>LGW</td>\r\n'
        '      <td>19:55h</td>\r\n'
        '      <td>BCN</td>\r\n'
        '      <td>23:05h</td>\r\n'
        '      <td>VY7623</td>\r\n'
        '    </tr>\r\n'
        '  </table>\r\n'
        '</body></html>\r\n'
    )

    def _texto(self):
        from app.services.ocr_service import _strip_html

        return _strip_html(self.HTML)

    def _lineas(self):
        return [linea for linea in self._texto().splitlines() if linea.strip()]

    def test_cada_vuelo_queda_en_una_linea(self):
        lineas = self._lineas()

        ida = [linea for linea in lineas if 'VY7604' in linea]
        vuelta = [linea for linea in lineas if 'VY7623' in linea]

        assert len(ida) == 1 and len(vuelta) == 1
        for campo in ('31 October 2026', 'BCN', '07:55h', 'LGW', '09:20h'):
            assert campo in ida[0]
        for campo in ('02 November 2026', 'LGW', '19:55h', 'BCN', '23:05h'):
            assert campo in vuelta[0]

    def test_no_se_mezclan_los_dos_vuelos(self):
        ida = next(linea for linea in self._lineas() if 'VY7604' in linea)

        assert 'VY7623' not in ida
        assert '19:55h' not in ida

    def test_no_quedan_lineas_de_puro_espacio(self):
        texto = self._texto()

        assert '\n\n\n' not in texto
        assert all(linea == linea.strip() for linea in texto.splitlines())

    def test_las_celdas_se_separan_visiblemente(self):
        ida = next(linea for linea in self._lineas() if 'VY7604' in linea)

        assert '|' in ida
        assert '| |' not in ida


@pytest.mark.unit
class TestVariasPersonasEnUnaReserva:
    """A booking covers people, and each has their own seat on each leg.

    One «pasajero» and one «asiento» per service could hold one of the three
    names a confirmation listed and one of its six seats, so the model left
    both empty rather than choose. The data was in the document all along and
    had nowhere to go: the limit was the shape, not the model.
    """

    def _payload(self):
        return {'servicios': [
            {
                'campos': {
                    'numero_vuelo': 'VY7604',
                    'origen_codigo': 'BCN', 'destino_codigo': 'LGW',
                    'pasajeros': [
                        {'nombre': 'Llibert Morreres', 'asiento': '15D'},
                        {'nombre': 'Ailime Lemus', 'asiento': '15E'},
                        {'nombre': 'Ona Morreres', 'asiento': '15F'},
                    ],
                },
                'confianzas': {'numero_vuelo': 0.85, 'pasajeros': 0.85},
            },
            {
                'campos': {
                    'numero_vuelo': 'VY7623',
                    'origen_codigo': 'LGW', 'destino_codigo': 'BCN',
                    'pasajeros': [
                        {'nombre': 'Llibert Morreres', 'asiento': '15D'},
                        {'nombre': 'Ailime Lemus', 'asiento': '15F'},
                        {'nombre': 'Ona Morreres', 'asiento': '15E'},
                    ],
                },
                'confianzas': {'numero_vuelo': 0.85, 'pasajeros': 0.85},
            },
        ]}

    def _aprobar(self, gestor, documento_procesado):
        extraction = documento_procesado.current_extraction
        documento_procesado.clasificacion = DocumentClassification.VUELO
        extraction.payload = self._payload()
        extraction.payload_normalizado = self._payload()
        db.session.commit()
        return extraction_service.approve(gestor, extraction)

    def test_cada_tramo_guarda_su_lista_de_pasajeros(
        self, gestor, documento_procesado, trip
    ):
        self._aprobar(gestor, documento_procesado)

        ida = TravelSegment.query.filter_by(numero='VY7604').first()
        assert len(ida.datos['pasajeros']) == 3

    def test_el_asiento_es_el_de_ese_trayecto(self, gestor, documento_procesado, trip):
        """The hard part: the seats swap between outbound and return."""
        self._aprobar(gestor, documento_procesado)

        ida = TravelSegment.query.filter_by(numero='VY7604').first()
        vuelta = TravelSegment.query.filter_by(numero='VY7623').first()

        def asiento(segmento, nombre):
            return next(
                p['asiento'] for p in segmento.datos['pasajeros']
                if p['nombre'] == nombre
            )

        assert asiento(ida, 'Ailime Lemus') == '15E'
        assert asiento(vuelta, 'Ailime Lemus') == '15F'
        assert asiento(ida, 'Ona Morreres') == '15F'
        assert asiento(vuelta, 'Ona Morreres') == '15E'

    def test_una_persona_sin_nombre_se_descarta(self, gestor, documento_procesado):
        """A seat belonging to nobody is not worth recording."""
        documento_procesado.clasificacion = DocumentClassification.VUELO
        extraction = documento_procesado.current_extraction
        payload = {'servicios': [{
            'campos': {
                'numero_vuelo': 'VY1',
                'pasajeros': [{'nombre': None, 'asiento': '1A'},
                              {'nombre': 'Alguien', 'asiento': '1B'}],
            },
            'confianzas': {},
        }]}
        extraction.payload = payload
        extraction.payload_normalizado = payload
        db.session.commit()

        extraction_service.approve(gestor, extraction)

        segmento = TravelSegment.query.filter_by(numero='VY1').first()
        assert [p['nombre'] for p in segmento.datos['pasajeros']] == ['Alguien']

    def test_una_reserva_de_una_sola_persona_sigue_funcionando(
        self, gestor, documento_procesado
    ):
        documento_procesado.clasificacion = DocumentClassification.VUELO
        extraction = documento_procesado.current_extraction
        payload = {'servicios': [{
            'campos': {'numero_vuelo': 'IB1', 'pasajero': 'Solo Yo', 'asiento': '3C'},
            'confianzas': {},
        }]}
        extraction.payload = payload
        extraction.payload_normalizado = payload
        db.session.commit()

        extraction_service.approve(gestor, extraction)

        segmento = TravelSegment.query.filter_by(numero='IB1').first()
        assert segmento.asiento == '3C'
        assert not (segmento.datos or {}).get('pasajeros')
