"""What a document is, decided from what it says.

The upload form proposed «Reserva» and almost nobody changed it, so boarding
passes, invoices and internal requests were filed as bookings -- and «Solicitud
interna» picked for a booking sent it to a form reader that found no form and
extracted nothing. Now the type is detected unless a person chooses it, and
when no template fits the model reads the document instead.
"""
import io
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models.enums import (
    DocumentClassification,
    DocumentProcessState,
    DocumentType,
    ExtractionMethod,
    ProvenanceOrigin,
)
from app.services import ai_service, document_service

pytestmark = pytest.mark.unit
S = DocumentProcessState


def _modelo(monkeypatch, clasificacion='vuelo', tipo='factura', confianza=0.9,
            servicios=None):
    """A model that answers the classification and, if asked, the extraction."""
    llamadas = []

    def clasificar(document, actor=None):
        llamadas.append('clasificar')
        return {'datos': {'clasificacion': clasificacion, 'confianza': confianza,
                          'tipo_documento': tipo}, 'run': None}

    def extraer(document, clasificacion=None, actor=None):
        llamadas.append(f'extraer:{clasificacion}')
        return {'datos': {'servicios': servicios or [{'campos': {}}]},
                'run': SimpleNamespace(id=None, proveedor='stub', modelo='x')}

    monkeypatch.setattr(ai_service, 'classify_document', clasificar)
    monkeypatch.setattr(ai_service, 'extract_document', extraer)
    return llamadas


def _reserva():
    """A flight booking that is not a boarding pass."""
    from tests.test_talon_de_venta import _pdf

    return _pdf('Confirmacion de reserva Iberia\nVuelo IB3210 MAD - BER\n'
                'Localizador XYZ12A\nSalida 01/06/2026 10:00')


def _subir(gestor, trip, datos, nombre='documento.pdf', tipo=None,
           mime='application/pdf'):
    from app.tasks import document_tasks

    document = document_service.upload(
        gestor, trip, FileStorage(stream=io.BytesIO(datos), filename=nombre,
                                  content_type=mime),
        tipo=tipo, commit=False,
    )
    db.session.commit()
    for _, task in document_tasks.PIPELINE:
        task.run(str(document.id))
    db.session.refresh(document)
    return document


class TestSeDetecta:
    def test_sin_elegir_lo_decide_la_ia(self, gestor, trip, monkeypatch):
        llamadas = _modelo(monkeypatch, tipo='factura')

        document = _subir(gestor, trip, _reserva())

        assert 'clasificar' in llamadas, 'aunque las reglas estén seguras de la clase'
        assert document.tipo is DocumentType.FACTURA
        assert document.tipo_origen is ProvenanceOrigin.IA

    def test_lo_que_elige_una_persona_se_respeta(self, gestor, trip, monkeypatch):
        _modelo(monkeypatch, tipo='billete')

        document = _subir(gestor, trip, _reserva(), tipo=DocumentType.FACTURA)

        assert document.tipo is DocumentType.FACTURA
        assert document.tipo_origen is ProvenanceOrigin.MANUAL

    def test_sin_ia_lo_deduce_de_la_clase(self, gestor, trip, monkeypatch):
        def sin_servicio(*a, **k):
            raise RuntimeError('sin proveedor')

        monkeypatch.setattr(ai_service, 'classify_document', sin_servicio)

        document = _subir(gestor, trip, _reserva())

        assert document.tipo is DocumentType.RESERVA
        assert document.tipo_origen is ProvenanceOrigin.DOCUMENTO

    def test_un_talon_es_una_factura_sin_preguntar(self, gestor, trip, monkeypatch):
        from tests.test_talon_de_venta import HOTEL, _pdf

        llamadas = _modelo(monkeypatch)

        document = _subir(gestor, trip, _pdf(HOTEL), nombre='talon.pdf')

        assert document.tipo is DocumentType.FACTURA
        assert document.tipo_origen is ProvenanceOrigin.DOCUMENTO
        assert llamadas == []

    def test_la_plantilla_de_solicitud_se_reconoce(self, gestor, trip, monkeypatch):
        from app.services import ocr_service
        from tests.test_solicitud_interna import _plantilla

        _modelo(monkeypatch)

        document = _subir(gestor, trip, _plantilla(), nombre='solicitud.xlsx',
                          mime=ocr_service.MIME_XLSX)

        assert document.tipo is DocumentType.SOLICITUD_INTERNA
        assert document.tipo_origen is ProvenanceOrigin.DOCUMENTO

    def test_una_reserva_subida_como_solicitud_no_se_lee_como_formulario(
        self, gestor, trip, monkeypatch,
    ):
        """The commonest wrong pick, and the one that changed how it was read."""
        llamadas = _modelo(monkeypatch, clasificacion='vuelo', tipo='billete')

        document = _subir(gestor, trip, _reserva(), tipo=DocumentType.SOLICITUD_INTERNA)

        assert document.clasificacion is not DocumentClassification.SOLICITUD
        assert document.tipo is DocumentType.BILLETE
        assert 'extraer:vuelo' in llamadas


class TestSiNingunaPlantillaEncajaLeeLaIA:
    def test_una_solicitud_en_texto_libre(self, gestor, trip, monkeypatch):
        """«Solicito viaje a Ingolstadt del 27 al 29»: a request, but no form."""
        from tests.test_talon_de_venta import _pdf

        llamadas = _modelo(
            monkeypatch, clasificacion='solicitud', tipo='solicitud_interna',
            servicios=[{'campos': {'destino': 'Ingolstadt', 'motivo': 'Pruebas'}}],
        )

        document = _subir(gestor, trip, _pdf(
            'Hola, solicito viaje a Ingolstadt del 27 al 29 de octubre.'))

        assert document.clasificacion is DocumentClassification.SOLICITUD
        assert 'extraer:solicitud' in llamadas
        extraccion = document.current_extraction
        assert extraccion.metodo is ExtractionMethod.IA
        assert extraccion.datos['servicios'][0]['campos']['destino'] == 'Ingolstadt'
        assert any('ninguna plantilla' in a for a in extraccion.avisos)

    def test_la_plantilla_que_si_encaja_no_llama_a_la_ia(self, gestor, trip, monkeypatch):
        from app.services import ocr_service
        from tests.test_solicitud_interna import _plantilla

        llamadas = _modelo(monkeypatch)

        document = _subir(gestor, trip, _plantilla(), nombre='solicitud.xlsx',
                          mime=ocr_service.MIME_XLSX)

        assert document.current_extraction.metodo is ExtractionMethod.REGLAS
        assert not [c for c in llamadas if c.startswith('extraer')]

    def test_el_esquema_de_la_solicitud_existe(self):
        from app.services.ai.schemas import SCHEMAS

        assert 'empleado' in str(SCHEMAS['extract_solicitud'])


class TestLaPantalla:
    def test_por_defecto_se_detecta(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(f'/documents/viaje/{trip.id}/subir').get_data(as_text=True)

        assert 'Detectar automáticamente' in html

    def test_se_dice_quien_decidio_el_tipo(self, as_user, gestor, trip, monkeypatch):
        _modelo(monkeypatch, tipo='factura')
        document = _subir(gestor, trip, _reserva())

        with as_user(gestor) as client:
            html = client.get(f'/documents/{document.id}').get_data(as_text=True)

        assert 'detectado por la IA' in html


    def test_una_tarjeta_de_embarque_se_reconoce_aunque_la_ia_diga_billete(
        self, gestor, trip, booking_pdf, monkeypatch,
    ):
        _modelo(monkeypatch, tipo='billete')

        document = _subir(gestor, trip, booking_pdf)

        assert document.tipo is DocumentType.TARJETA_EMBARQUE
