"""Sale stubs from the travel agency (Viajes El Corte Inglés).

A stub says what was bought and what it cost, and almost never when: the
flight line has a day and a route, the hotel line two days. Read as a booking
it would add a flight at midnight beside the one the confirmation already put
there, so it completes what exists and only proposes what does not.
"""
import io
from datetime import datetime
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models.enums import DocumentClassification, DocumentType, ExtractionMethod
from app.services import document_service, extraction_service, talon_service
from app.utils.timeutil import set_instant

pytestmark = pytest.mark.unit

CABECERA = """TALÓN DE VENTA / TALÓ DE VENDA
VIAJES EL CORTE INGLÉS S.A. / Domicilio Social: Hermosilla, 112. 28009 - Madrid
Expediente / Expedient Fecha / Data Documento / Document Agente / Agent
00010/26/136612 20-10-2026 {documento} 79032843
Cliente / client
ITALDESIGN GIUGIARO BARCELONA S.L
Viajero / Viatger {viajero}
Núm. Cliente
Núm. Client 99893695 - 000
RELACIÓN DE SERVICIOS / RELACIÓ DE SERVEIS
Bono / Billete - Bo /
Bitllet
Concepto / Concepte B.S. Tipo B.N.S. Cuota Importe / Import
"""

HOTEL = CABECERA.format(documento='00010-588040', viajero='CAROLINA PLANELLS BONED') + """\
01768973 HTL: ENSO HOTEL 353,30 RE 353,30
MUNICH
DUI/SA 27/10 - 29/10
FORMA DE PAGO / FORMA DE PAGAMENT
Importe total de los servicios (Impuestos incluidos) 353,30
CARGO EN CUENTA / AUTORIZACION:2610208865
"""

VUELO = CABECERA.format(documento='00010-588251', viajero='PLANELLS BONED CAROLINA(A') + """\
22035327222661 LH - H - 27/10 489,98 EX 55,59 0,00 545,57
BCN/MUC/BCN
Comisión por compra 14,85 EX 0,00 14,85
FORMA DE PAGO / FORMA DE PAGAMENT
Importe total de los servicios (Impuestos incluidos) 560,42
CARGO EN CUENTA / AUTORIZACION:2610209787
"""


def _documento(texto):
    return SimpleNamespace(
        pages=[SimpleNamespace(numero=1, contenido=texto)],
        clasificacion=talon_service.clasificacion(texto),
    )


def _servicios(texto):
    datos = talon_service.extraer(_documento(texto))
    return datos['servicios'], datos['avisos']


class TestLectura:
    def test_se_reconoce_y_no_se_confunde(self):
        assert talon_service.es_talon(HOTEL)
        assert talon_service.es_talon(VUELO)
        assert not talon_service.es_talon('Confirmación de reserva Iberia IB3210')

    def test_cada_uno_se_clasifica_por_su_servicio(self):
        assert talon_service.clasificacion(HOTEL) is DocumentClassification.HOTEL
        assert talon_service.clasificacion(VUELO) is DocumentClassification.VUELO

    def test_el_hotel(self):
        (hotel,), _ = _servicios(HOTEL)
        campos = hotel['campos']

        assert campos['nombre'] == 'Enso Hotel'
        assert campos['ciudad'] == 'Munich'
        assert campos['check_in']['local'] == '2026-10-27'
        assert campos['check_out']['local'] == '2026-10-29'
        assert campos['tipo_habitacion'] == 'Doble uso individual'
        assert campos['regimen'] == 'Solo alojamiento'
        assert campos['importe'] == 353.30
        assert campos['bono'] == '01768973'
        assert campos['huesped'] == 'CAROLINA PLANELLS BONED'

    def test_el_vuelo_es_un_tramo_por_escala_y_el_importe_va_en_el_primero(self):
        (ida, vuelta), _ = _servicios(VUELO)

        assert (ida['campos']['origen_codigo'], ida['campos']['destino_codigo']) == \
            ('BCN', 'MUC')
        assert (vuelta['campos']['origen_codigo'], vuelta['campos']['destino_codigo']) == \
            ('MUC', 'BCN')
        assert ida['campos']['salida']['local'] == '2026-10-27T00:00'
        assert vuelta['campos']['salida']['local'] is None
        # The ticket and its purchase fee: what the company paid.
        assert ida['campos']['importe'] == 560.42
        assert 'importe' not in vuelta['campos']
        assert ida['campos']['numero_billete'] == vuelta['campos']['numero_billete'] \
            == '22035327222661'
        assert 'bono' not in ida['campos'], 'en un billete el bono es el número de billete'
        assert ida['campos']['cargos'] == 'Comisión por compra: 14,85 €'

    def test_el_nombre_cortado_por_la_columna_se_limpia(self):
        (ida, _), _ = _servicios(VUELO)

        assert ida['campos']['pasajero'] == 'PLANELLS BONED CAROLINA'

    def test_la_hora_que_no_da_queda_para_revisar(self):
        (ida, _), avisos = _servicios(VUELO)

        assert ida['confianzas']['salida'] < 0.5
        assert any('ninguna hora' in a for a in avisos)

    def test_si_la_suma_no_cuadra_se_avisa(self):
        _, avisos = _servicios(HOTEL.replace(
            'Impuestos incluidos) 353,30', 'Impuestos incluidos) 400,00'))

        assert any('400.00' in a for a in avisos)

    def test_un_servicio_de_enero_en_un_talon_de_diciembre_es_del_ano_siguiente(self):
        texto = HOTEL.replace('20-10-2026', '20-12-2026').replace(
            'DUI/SA 27/10 - 29/10', 'DUI/SA 30/12 - 02/01')

        (hotel,), _ = _servicios(texto)

        assert hotel['campos']['check_in']['local'] == '2026-12-30'
        assert hotel['campos']['check_out']['local'] == '2027-01-02'


def _pdf(texto):
    """A one-page PDF with ``texto`` one line per row, as the agency prints it."""
    lineas = [linea.encode('latin-1', 'replace').decode('latin-1')
              for linea in texto.splitlines()]

    def escapar(linea):
        return linea.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')

    cuerpo = ' '.join(f'({escapar(linea)}) Tj T*' for linea in lineas)
    contenido = f'BT /F1 9 Tf 40 800 Td 12 TL {cuerpo} ET'.encode('latin-1')
    objetos = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] '
        b'/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length ' + str(len(contenido)).encode() + b' >>\nstream\n'
        + contenido + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica '
        b'/Encoding /WinAnsiEncoding >>',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for indice, cuerpo_obj in enumerate(objetos, start=1):
        offsets.append(len(out))
        out += f'{indice} 0 obj\n'.encode() + cuerpo_obj + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n'.encode()
    for offset in offsets:
        out += f'{offset:010d} 00000 n \n'.encode()
    out += (f'trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\n'
            f'startxref\n{xref}\n%%EOF\n').encode()
    return bytes(out)


def _procesar(gestor, trip, texto, nombre='talon.pdf'):
    from app.tasks import document_tasks

    storage = FileStorage(stream=io.BytesIO(_pdf(texto)), filename=nombre,
                          content_type='application/pdf')
    document = document_service.upload(gestor, trip, storage,
                                       tipo=DocumentType.FACTURA, commit=False)
    db.session.commit()
    for _, task in document_tasks.PIPELINE:
        task.run(str(document.id))
    db.session.refresh(document)
    return document


def _tramo(trip, origen, destino, salida, llegada):
    from app.models.itinerary import TravelSegment

    fila = TravelSegment(trip_id=trip.id, numero='LH1813',
                         origen_codigo=origen, destino_codigo=destino)
    set_instant(fila, 'salida', salida, 'Europe/Madrid')
    set_instant(fila, 'llegada', llegada, 'Europe/Berlin')
    db.session.add(fila)
    db.session.commit()
    return fila


def _vivos(modelo, trip):
    return modelo.query.filter_by(trip_id=trip.id, is_deleted=False).all()


class TestPipeline:
    def test_se_lee_sin_preguntar_al_modelo(self, gestor, trip, monkeypatch):
        from app.services import ai_service

        def prohibido(*a, **k):
            raise AssertionError('Un talón se lee por reglas.')

        monkeypatch.setattr(ai_service, 'extract_document', prohibido)
        monkeypatch.setattr(ai_service, 'classify_document', prohibido)

        document = _procesar(gestor, trip, HOTEL)

        assert document.clasificacion is DocumentClassification.HOTEL
        assert document.current_extraction.metodo is ExtractionMethod.REGLAS
        (servicio,) = document.current_extraction.datos['servicios']
        assert servicio['campos']['importe'] == 353.30


class TestAlAprobar:
    def test_completa_el_vuelo_que_ya_estaba_sin_tocar_su_hora(self, gestor, trip):
        from app.models.itinerary import TravelSegment

        ida = _tramo(trip, 'BCN', 'MUC', datetime(2026, 10, 27, 7, 10),
                     datetime(2026, 10, 27, 9, 20))
        vuelta = _tramo(trip, 'MUC', 'BCN', datetime(2026, 10, 29, 18, 0),
                        datetime(2026, 10, 29, 20, 5))

        document = _procesar(gestor, trip, VUELO)
        extraction_service.approve(gestor, document.current_extraction)

        assert len(_vivos(TravelSegment, trip)) == 2, 'no se duplica nada'
        db.session.refresh(ida)
        db.session.refresh(vuelta)
        assert ida.salida_local == datetime(2026, 10, 27, 7, 10)
        assert float(ida.importe) == 560.42
        assert ida.datos['numero_billete'] == '22035327222661'
        assert vuelta.datos['numero_billete'] == '22035327222661'
        assert vuelta.importe is None

    def test_sin_el_vuelo_lo_propone_como_provisional(self, gestor, trip):
        from app.models.itinerary import TravelSegment

        document = _procesar(gestor, trip, VUELO)
        extraction_service.approve(gestor, document.current_extraction)

        (tramo,) = _vivos(TravelSegment, trip)
        assert (tramo.origen_codigo, tramo.destino_codigo) == ('BCN', 'MUC')
        assert tramo.datos['provisional'] == extraction_service.PROVISIONAL_TALON

    def test_la_vuelta_sin_fecha_no_se_inventa(self, gestor, trip):
        from app.models.extraction import ExtractionApplication

        document = _procesar(gestor, trip, VUELO)
        extraction_service.approve(gestor, document.current_extraction)

        motivos = [a.motivo for a in ExtractionApplication.query.filter_by(
            extraction_id=document.current_extraction.id)]
        assert any('no da fecha' in (m or '') for m in motivos)

    def test_la_confirmacion_posterior_adopta_el_provisional(self, gestor, trip):
        from app.models.itinerary import TravelSegment

        talon = _procesar(gestor, trip, VUELO)
        extraction_service.approve(gestor, talon.current_extraction)

        # The airline's confirmation, uploaded afterwards.
        confirmacion = _procesar(gestor, trip, 'Confirmacion Lufthansa LH1813',
                                 nombre='confirmacion.pdf')
        extraction = extraction_service.create_version(
            confirmacion, payload={'servicios': [{'campos': {
                'numero_vuelo': 'LH1813', 'origen_codigo': 'BCN', 'destino_codigo': 'MUC',
                'salida': {'local': '2026-10-27T07:10', 'zona_horaria': 'Europe/Madrid'},
                'llegada': {'local': '2026-10-27T09:20', 'zona_horaria': 'Europe/Berlin'},
            }}]},
        )
        confirmacion.clasificacion = DocumentClassification.VUELO
        db.session.commit()
        extraction_service.approve(gestor, extraction)

        (tramo,) = _vivos(TravelSegment, trip)
        assert tramo.salida_local == datetime(2026, 10, 27, 7, 10)
        assert tramo.numero == 'LH1813'
        assert float(tramo.importe) == 560.42, 'el importe del talón se conserva'
        assert 'provisional' not in (tramo.datos or {})
        assert tramo.documento_origen_id == confirmacion.id

    def test_el_hotel_completa_la_estancia_que_ya_estaba(self, gestor, trip):
        from app.models.itinerary import Accommodation

        estancia = Accommodation(trip_id=trip.id, nombre='Hotel Enso', ciudad='Múnich')
        set_instant(estancia, 'check_in', datetime(2026, 10, 27, 15, 0), 'Europe/Berlin')
        set_instant(estancia, 'check_out', datetime(2026, 10, 29, 11, 0), 'Europe/Berlin')
        db.session.add(estancia)
        db.session.commit()

        document = _procesar(gestor, trip, HOTEL)
        extraction_service.approve(gestor, document.current_extraction)

        (unica,) = _vivos(Accommodation, trip)
        assert unica.nombre == 'Hotel Enso', 'lo que ya decía no se pisa'
        # «Enso Hotel» and «Hotel Enso» are the same name, so nothing to flag.
        from app.models.extraction import ExtractionApplication

        (aplicacion,) = ExtractionApplication.query.filter_by(
            extraction_id=document.current_extraction.id).all()
        assert 'compruebe' not in (aplicacion.motivo or '')
        assert unica.check_in_local == datetime(2026, 10, 27, 15, 0)
        assert float(unica.importe) == 353.30
        assert unica.regimen == 'Solo alojamiento'

    def test_si_el_nombre_no_coincide_queda_dicho(self, gestor, trip):
        from app.models.extraction import ExtractionApplication
        from app.models.itinerary import Accommodation

        estancia = Accommodation(trip_id=trip.id, nombre='Hotel Bayerischer Hof',
                                 ciudad='Múnich')
        set_instant(estancia, 'check_in', datetime(2026, 10, 27, 15, 0), 'Europe/Berlin')
        set_instant(estancia, 'check_out', datetime(2026, 10, 29, 11, 0), 'Europe/Berlin')
        db.session.add(estancia)
        db.session.commit()

        document = _procesar(gestor, trip, HOTEL)
        extraction_service.approve(gestor, document.current_extraction)

        (aplicacion,) = ExtractionApplication.query.filter_by(
            extraction_id=document.current_extraction.id).all()
        assert 'Enso Hotel' in aplicacion.motivo
        assert 'Bayerischer Hof' in aplicacion.motivo

    def test_dos_candidatos_no_se_eligen_a_ciegas(self, gestor, trip):
        from app.models.itinerary import TravelSegment

        _tramo(trip, 'BCN', 'MUC', datetime(2026, 10, 27, 7, 10),
               datetime(2026, 10, 27, 9, 20))
        _tramo(trip, 'BCN', 'MUC', datetime(2026, 10, 27, 18, 0),
               datetime(2026, 10, 27, 20, 0))

        document = _procesar(gestor, trip, VUELO)
        extraction_service.approve(gestor, document.current_extraction)

        tramos = _vivos(TravelSegment, trip)
        assert all(t.importe is None for t in tramos if not (t.datos or {}).get('provisional'))
