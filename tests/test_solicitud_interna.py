"""Internal travel requests: the form filled in before a trip exists.

A request is not a booking. Approving one completes the trip -- motive,
project, dates, destination, traveller -- and adds nothing to the itinerary;
through the booking extraction a ticked «AVIÓN» box became a flight nobody had
booked.
"""
import io
import zipfile
from datetime import date, datetime, time

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models.enums import (
    DocumentClassification,
    DocumentProcessState,
    DocumentType,
    ExtractionMethod,
)
from app.services import document_service, ocr_service, solicitud_service

S = DocumentProcessState


def _plantilla(valores=None, hoja_oculta=False):
    """A workbook laid out like the real request template.

    Labels in column B with the value beside them, the «a cargo de» and
    «horarios» rows holding two pairs each, and a purchasing sheet after it
    that repeats the destination and the project by reference.
    """
    import openpyxl

    v = {
        'fecha_solicitud': date(2026, 10, 20),
        'empleado': 'Viajero',
        'departamento': 'Ingeniería',
        'solicitante': 'Gestora',
        'cliente': 'Volkswagen',
        'idb': None,
        'motivo': 'Revisión de prototipo',
        'proyecto': 'OT-4471',
        'destino': 'Wolfsburgo, Alemania',
        'fecha_ida': datetime(2026, 11, 3),
        'fecha_vuelta': datetime(2026, 11, 5),
        'hora_ida': time(8, 0),
        'hora_vuelta': 'por la tarde',
        'avion': 'X', 'hotel': 'X', 'coche': None, 'tren': 'NO',
        **(valores or {}),
    }

    libro = openpyxl.Workbook()
    hoja = libro.active
    hoja.title = 'SOLICITUD VIAJE'
    hoja['B5'] = 'SOLICITUD DE VIAJE PERSONAL'
    for fila, etiqueta, clave in (
        (7, 'FECHA SOLICITUD:', 'fecha_solicitud'), (8, 'EMPLEADO:', 'empleado'),
        (9, 'DEPARTAMENTO', 'departamento'), (10, 'SOLICITANTE VIAJE', 'solicitante'),
        (12, 'MOTIVO VIAJE:', 'motivo'), (14, 'PROYECTO / OT:', 'proyecto'),
        (15, 'DESTINO:', 'destino'), (16, 'FECHA IDA', 'fecha_ida'),
        (17, 'FECHA VUELTA', 'fecha_vuelta'),
        (21, 'AVION', 'avion'), (22, 'HOTEL', 'hotel'),
        (23, 'COCHE', 'coche'), (24, 'TREN', 'tren'),
    ):
        hoja[f'B{fila}'] = etiqueta
        hoja[f'C{fila}'] = v[clave]
    hoja['B11'], hoja['C11'], hoja['D11'] = 'VIAJE A CARGO DE', 'CLIENTE:', v['cliente']
    hoja['G11'], hoja['H11'] = 'IDB', v['idb']
    hoja['B18'], hoja['C18'], hoja['D18'] = 'PETICIONES HORARIOS', 'Hora ida:', v['hora_ida']
    hoja['E18'], hoja['F18'] = 'Hora vuelta:', v['hora_vuelta']
    hoja['B20'] = 'MEDIOS SOLICITADOS'

    compras = libro.create_sheet('COMPRAS')
    compras['C2'] = 'NO RELLENAR, A CARGO DE COMPRAS'
    compras['C15'], compras['D15'] = 'DESTINO:', 'Otra ciudad'
    compras['C19'], compras['D19'] = 'AVION', 'COMPAÑÍA'
    compras['C31'], compras['E31'] = 'HOTEL', 'SI'

    if hoja_oculta:
        oculta = libro.create_sheet('NOTAS')
        oculta['A1'] = 'Esto no lo ve nadie'
        oculta.sheet_state = 'hidden'

    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()


def _upload(gestor, trip, data, filename='solicitud.xlsx', tipo=DocumentType.RESERVA):
    storage = FileStorage(stream=io.BytesIO(data), filename=filename,
                          content_type=ocr_service.MIME_XLSX)
    document = document_service.upload(gestor, trip, storage, tipo=tipo, commit=False)
    db.session.commit()
    return document


def _procesar(document):
    from app.tasks import document_tasks

    for _, task in document_tasks.PIPELINE:
        task.run(str(document.id))
    db.session.refresh(document)
    return document


@pytest.mark.unit
class TestTipoDeArchivo:
    def test_un_libro_de_excel_se_reconoce(self):
        assert ocr_service.tipo_de_zip(io.BytesIO(_plantilla())) == ocr_service.MIME_XLSX

    def test_un_zip_cualquiera_no_es_un_libro(self):
        datos = io.BytesIO()
        with zipfile.ZipFile(datos, 'w') as archivo:
            archivo.writestr('word/document.xml', '<w/>')

        assert ocr_service.tipo_de_zip(datos) == 'application/zip'

    def test_un_libro_con_macros_se_rechaza(self):
        from app.utils.errors import PermanentError

        datos = io.BytesIO()
        with zipfile.ZipFile(datos, 'w') as archivo:
            archivo.writestr('xl/workbook.xml', '<workbook/>')
            archivo.writestr('xl/vbaProject.bin', b'\x00')

        with pytest.raises(PermanentError, match='macros'):
            ocr_service.tipo_de_zip(datos)

    def test_un_docx_renombrado_no_pasa_la_validacion(self, gestor, trip):
        from app.tasks import document_tasks
        from app.utils.errors import PermanentError

        datos = io.BytesIO()
        with zipfile.ZipFile(datos, 'w') as archivo:
            archivo.writestr('word/document.xml', '<w/>')
        document = _upload(gestor, trip, datos.getvalue())

        with pytest.raises(PermanentError):
            document_tasks.validate_document.run(str(document.id))
        db.session.refresh(document)
        assert document.estado_proceso is S.RECHAZADO


@pytest.mark.unit
class TestTexto:
    def _paginas(self, **kwargs):
        return ocr_service.extract(io.BytesIO(_plantilla(**kwargs)), ocr_service.MIME_XLSX).pages

    def test_una_pagina_por_hoja_y_una_linea_por_fila(self):
        solicitud, compras = self._paginas()

        assert 'FECHA IDA | 2026-11-03' in solicitud.contenido
        assert 'PETICIONES HORARIOS | Hora ida: | 08:00 | Hora vuelta: | por la tarde' \
            in solicitud.contenido
        assert 'NO RELLENAR' in compras.contenido

    def test_una_hoja_oculta_no_se_lee(self):
        paginas = self._paginas(hoja_oculta=True)

        assert all('Esto no lo ve nadie' not in p.contenido for p in paginas)


@pytest.mark.unit
class TestLectura:
    def _campos(self, texto):
        from types import SimpleNamespace

        documento = SimpleNamespace(
            pages=[SimpleNamespace(numero=1, contenido=texto)], texto=None,
        )
        datos = solicitud_service.extraer(documento)
        return datos['servicios'][0]['campos'], datos['avisos']

    def _texto_de_la_plantilla(self, **valores):
        pagina = ocr_service.extract(
            io.BytesIO(_plantilla(valores)), ocr_service.MIME_XLSX
        ).pages[0]
        return pagina.contenido

    def test_se_leen_los_campos_de_la_plantilla(self, seeded, viajero):
        campos, _ = self._campos(self._texto_de_la_plantilla())

        assert campos['empleado'] == 'Viajero'
        assert campos['motivo'] == 'Revisión de prototipo'
        assert campos['proyecto'] == 'OT-4471'
        assert campos['destino'] == 'Wolfsburgo, Alemania'
        assert campos['a_cargo_de'] == 'Cliente: Volkswagen'
        assert campos['medios'] == 'Avión, Hotel'
        assert campos['fecha_solicitud'] == '2026-10-20'

    def test_la_hora_pedida_entra_en_el_instante(self, seeded):
        campos, _ = self._campos(self._texto_de_la_plantilla())

        assert campos['ida'] == {'local': '2026-11-03T08:00', 'zona_horaria': 'Europe/Madrid'}

    def test_una_preferencia_no_es_una_hora(self, seeded):
        """«por la tarde» is for whoever books; the trip ends at the end of the day."""
        campos, avisos = self._campos(self._texto_de_la_plantilla())

        assert campos['vuelta']['local'] == '2026-11-05T23:59'
        assert campos['horario_vuelta'] == 'por la tarde'
        assert any('hora de vuelta' in a for a in avisos)

    def test_el_presupuesto_de_compras_no_se_confunde_con_la_solicitud(self, seeded):
        texto = 'Hoja: COMPRAS\nDESTINO: | Otra ciudad\nAVION | COMPAÑÍA\nHOTEL | SI'

        assert not solicitud_service.es_solicitud(texto)

    def test_tambien_se_lee_un_formulario_en_texto(self, seeded):
        texto = ('EMPLEADO: Ana Pérez\nMOTIVO VIAJE: Feria\nDESTINO: Turín\n'
                 'FECHA IDA: 03/11/2026\nFECHA VUELTA: 05/11/2026')

        campos, _ = self._campos(texto)

        assert campos['destino'] == 'Turín'
        assert campos['ida']['local'] == '2026-11-03T00:00'

    def test_se_avisa_si_no_hay_a_quien_asignar(self, seeded):
        _, avisos = self._campos(self._texto_de_la_plantilla(empleado='Nadie Conocido'))

        assert any('Nadie Conocido' in a for a in avisos)


@pytest.mark.unit
class TestPipeline:
    def test_se_clasifica_como_solicitud_aunque_se_suba_como_reserva(self, gestor, trip):
        document = _procesar(_upload(gestor, trip, _plantilla()))

        assert document.clasificacion is DocumentClassification.SOLICITUD
        assert document.tipo is DocumentType.SOLICITUD_INTERNA
        assert document.estado_proceso is S.PENDIENTE_REVISION

    def test_no_se_pregunta_al_modelo(self, gestor, trip, monkeypatch):
        from app.services import ai_service

        def prohibido(*a, **k):
            raise AssertionError('Una solicitud se lee, no se interpreta.')

        monkeypatch.setattr(ai_service, 'extract_document', prohibido)
        monkeypatch.setattr(ai_service, 'classify_document', prohibido)

        document = _procesar(_upload(gestor, trip, _plantilla()))

        assert document.current_extraction.metodo is ExtractionMethod.REGLAS


@pytest.mark.unit
class TestAprobacion:
    def _viaje_vacio(self, gestor):
        from app.services import trip_service

        return trip_service.create_trip(gestor, titulo='Viaje sin datos')

    def _aprobar(self, gestor, trip, data=None, correcciones=None):
        from app.services import extraction_service

        document = _procesar(_upload(gestor, trip, data or _plantilla()))
        extraction_service.approve(gestor, document.current_extraction,
                                   correcciones=correcciones)
        db.session.refresh(trip)
        return document

    def test_completa_el_viaje(self, gestor, viajero):
        trip = self._viaje_vacio(gestor)

        self._aprobar(gestor, trip)

        assert trip.finalidad_detalle == 'Revisión de prototipo'
        assert trip.proyecto == 'OT-4471'
        assert trip.inicio_local == datetime(2026, 11, 3, 8, 0)
        assert trip.fin_local == datetime(2026, 11, 5, 23, 59)
        assert trip.has_traveler(viajero.id)
        assert [d.ciudad for d in trip.destinations] == ['Wolfsburgo']
        assert 'Medios solicitados: Avión, Hotel' in trip.observaciones

    def test_no_anade_nada_al_itinerario(self, gestor):
        trip = self._viaje_vacio(gestor)

        self._aprobar(gestor, trip)

        assert not [s for s in trip.segments if not s.is_deleted]
        assert not [a for a in trip.accommodations if not a.is_deleted]

    def test_lo_que_escribio_el_gestor_se_respeta(self, gestor, trip):
        """The fixture trip already has dates and a destination in Berlin."""
        inicio = trip.inicio_local

        self._aprobar(gestor, trip)

        assert trip.inicio_local == inicio
        assert {d.ciudad for d in trip.destinations} == {'Berlín', 'Wolfsburgo'}

    def test_una_correccion_llega_al_viaje_al_reaprobar(self, gestor):
        from app.services import extraction_service

        trip = self._viaje_vacio(gestor)
        document = self._aprobar(gestor, trip)

        extraction_service.approve(
            gestor, document.current_extraction,
            correcciones={0: {'proyecto': 'OT-9999'}},
        )
        db.session.refresh(trip)

        assert trip.proyecto == 'OT-9999'

    def test_sin_persona_unica_no_se_asigna_a_nadie(self, gestor):
        from app.models.extraction import ExtractionApplication

        trip = self._viaje_vacio(gestor)
        document = self._aprobar(gestor, trip, _plantilla({'empleado': 'Nadie Conocido'}))

        assert not trip.active_travelers
        motivos = [a.motivo for a in ExtractionApplication.query.filter_by(
            extraction_id=document.current_extraction.id, entidad_tipo='viajero')]
        assert any('Nadie Conocido' in (m or '') for m in motivos)
