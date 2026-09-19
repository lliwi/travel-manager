"""Document pipeline: state machine, validation, antivirus and storage."""
import io

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models.enums import (
    AntivirusStatus,
    DocumentClassification,
    DocumentProcessState,
    DocumentType,
)
from app.services import document_service
from app.utils.errors import ConflictError, InvalidTransition, ValidationError

S = DocumentProcessState


def _upload(gestor, trip, data, filename='documento.pdf', mime='application/pdf'):
    storage = FileStorage(
        stream=io.BytesIO(data), filename=filename, content_type=mime
    )
    document = document_service.upload(
        gestor, trip, storage, tipo=DocumentType.RESERVA, commit=False
    )
    db.session.commit()
    return document


@pytest.mark.unit
class TestMaquinaDeEstados:
    """Only ``document_service.transition`` may move a document."""

    def test_una_transicion_valida_se_registra(self, gestor, trip, booking_pdf):
        document = _upload(gestor, trip, booking_pdf)
        assert document.estado_proceso is S.RECIBIDO

        document_service.transition(document, S.VALIDADO, tarea='validate')

        assert document.estado_proceso is S.VALIDADO
        assert any(
            t.estado_nuevo is S.VALIDADO and t.tarea == 'validate'
            for t in document.transitions
        ), 'Cada transición debe quedar registrada.'

    def test_una_transicion_invalida_se_rechaza(self, gestor, trip, booking_pdf):
        document = _upload(gestor, trip, booking_pdf)

        with pytest.raises(InvalidTransition):
            document_service.transition(document, S.APROBADO)

        assert document.estado_proceso is S.RECIBIDO, (
            'Un salto no permitido no debe dejar el documento en un estado raro.'
        )

    def test_un_estado_terminal_no_avanza(self, gestor, trip, booking_pdf):
        document = _upload(gestor, trip, booking_pdf)
        document_service.transition(document, S.VALIDADO)
        document_service.transition(document, S.INFECTADO)

        with pytest.raises(InvalidTransition):
            document_service.transition(document, S.ALMACENADO)

    def test_solo_document_service_escribe_el_estado(self):
        """A grep-level guard on the invariant the whole pipeline relies on.

        If another module starts assigning ``estado_proceso`` directly, the
        transition table stops being a complete record and a failed document
        can no longer be diagnosed or resumed.
        """
        import pathlib
        import re

        raiz = pathlib.Path(__file__).resolve().parent.parent / 'app'
        # The model file declares the column; only *assignments to an
        # attribute* are the thing being guarded against.
        permitido = {'services/document_service.py', 'models/document.py'}
        patron = re.compile(r'^\s*(?!#)[\w\.\[\]]+\.estado_proceso\s*=(?!=)', re.M)

        infractores = []
        for archivo in raiz.rglob('*.py'):
            relativo = str(archivo.relative_to(raiz))
            if relativo in permitido:
                continue
            if patron.search(archivo.read_text(encoding='utf-8')):
                infractores.append(relativo)

        assert not infractores, (
            'Estos módulos asignan estado_proceso directamente en lugar de usar '
            'document_service.transition():\n  ' + '\n  '.join(infractores)
        )


@pytest.mark.unit
class TestSubida:
    def test_se_calcula_y_guarda_el_hash(self, gestor, trip, booking_pdf):
        from app.utils.hashing import sha256_bytes

        document = _upload(gestor, trip, booking_pdf)

        assert document.hash_sha256 == sha256_bytes(booking_pdf)
        assert document.tamano_bytes == len(booking_pdf)

    def test_se_rechaza_una_extension_no_permitida(self, gestor, trip):
        with pytest.raises(ValidationError, match='Formato no admitido'):
            _upload(gestor, trip, b'MZ\x90\x00', filename='malo.exe',
                    mime='application/octet-stream')

    def test_se_rechaza_un_archivo_vacio(self, gestor, trip):
        with pytest.raises(ValidationError, match='vacío'):
            _upload(gestor, trip, b'')

    def test_se_rechaza_un_duplicado_en_el_mismo_viaje(self, gestor, trip, booking_pdf):
        _upload(gestor, trip, booking_pdf, filename='primero.pdf')

        with pytest.raises(ConflictError, match='ya está adjunto'):
            _upload(gestor, trip, booking_pdf, filename='segundo.pdf')

    def test_el_mismo_archivo_en_otro_viaje_si_se_admite(
        self, gestor, trip, booking_pdf
    ):
        from app.services import trip_service

        _upload(gestor, trip, booking_pdf)
        otro = trip_service.create_trip(gestor, titulo='Otro viaje')

        document = _upload(gestor, otro, booking_pdf)
        assert document.id is not None

    def test_la_subida_queda_auditada(self, gestor, trip, booking_pdf):
        from app.models.audit import AuditEvent

        document = _upload(gestor, trip, booking_pdf)

        evento = AuditEvent.query.filter_by(
            accion='document.uploaded', recurso_id=str(document.id)
        ).first()
        assert evento is not None
        assert evento.actor_id == gestor.id


@pytest.mark.unit
class TestValidacionDeContenido:
    """The declared type is a claim; the magic bytes are evidence."""

    def test_un_pdf_real_pasa_la_validacion(self, gestor, trip, booking_pdf):
        from app.tasks import document_tasks

        document = _upload(gestor, trip, booking_pdf)
        document_tasks.validate_document.run(str(document.id))

        db.session.refresh(document)
        assert document.estado_proceso is S.VALIDADO
        assert document.mime == 'application/pdf'

    def test_un_ejecutable_disfrazado_de_pdf_se_rechaza(self, gestor, trip):
        """A .pdf that is not a PDF must never reach the rest of the pipeline."""
        from app.tasks import document_tasks
        from app.utils.errors import PermanentError

        # A Windows executable header, uploaded with a PDF name and MIME type.
        document = _upload(
            gestor, trip, b'MZ\x90\x00\x03' + b'\x00' * 200,
            filename='inocente.pdf', mime='application/pdf',
        )

        with pytest.raises(PermanentError):
            document_tasks.validate_document.run(str(document.id))

        db.session.refresh(document)
        assert document.estado_proceso is S.RECHAZADO


@pytest.mark.unit
class TestAntivirus:
    def test_un_archivo_infectado_se_bloquea_y_se_borra(
        self, app, gestor, trip, eicar_bytes, monkeypatch
    ):
        """An infected upload is destroyed, never promoted, and audited."""
        from app.models.audit import AuditEvent
        from app.services import antivirus_service, storage_service
        from app.tasks import document_tasks
        from app.utils.errors import PermanentError

        # The testing configuration uses the no-op scanner, so a real detector
        # is substituted here: the point of the test is the *handling* of a
        # detection, which must not depend on ClamAV being installed.
        class DetectingScanner(antivirus_service.AntivirusScanner):
            name = 'test'

            def scan(self, stream):
                data = stream.read()
                stream.seek(0)
                if b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE' in data:
                    return antivirus_service.ScanResult(
                        False, firma='Eicar-Test-Signature', motor='test'
                    )
                return antivirus_service.ScanResult(True, motor='test')

        monkeypatch.setattr(antivirus_service, 'get_scanner', DetectingScanner)

        document = _upload(gestor, trip, eicar_bytes, filename='virus.pdf')
        document.mime = 'application/pdf'
        document_service.transition(document, S.VALIDADO)
        clave = document.objeto_cuarentena

        with pytest.raises(PermanentError):
            document_tasks.scan_document.run(str(document.id))

        db.session.refresh(document)
        assert document.estado_proceso is S.INFECTADO
        assert document.antivirus_estado is AntivirusStatus.INFECTADO
        assert document.antivirus_firma == 'Eicar-Test-Signature'
        assert document.objeto_storage is None, (
            'Un archivo infectado nunca debe llegar al almacenamiento definitivo.'
        )

        _, cuarentena = storage_service.buckets()
        assert not storage_service.get_backend().exists(cuarentena, clave), (
            'El archivo infectado debe eliminarse de la cuarentena.'
        )
        assert AuditEvent.query.filter_by(accion='document.infected').first() is not None

    def test_un_archivo_limpio_pasa(self, gestor, trip, booking_pdf):
        from app.tasks import document_tasks

        document = _upload(gestor, trip, booking_pdf)
        document_tasks.validate_document.run(str(document.id))
        document_tasks.scan_document.run(str(document.id))

        db.session.refresh(document)
        assert document.estado_proceso is S.ESCANEADO


@pytest.mark.unit
class TestIdempotencia:
    """A redelivered task must do nothing, not do the work twice."""

    def test_reejecutar_una_tarea_no_hace_nada(self, gestor, trip, booking_pdf):
        from app.tasks import document_tasks

        document = _upload(gestor, trip, booking_pdf)
        document_tasks.validate_document.run(str(document.id))
        db.session.refresh(document)
        transiciones = len(document.transitions)

        document_tasks.validate_document.run(str(document.id))
        db.session.refresh(document)

        assert len(document.transitions) == transiciones, (
            'Una reentrega de Celery no debe producir una segunda transición.'
        )

    def test_una_tarea_fuera_de_orden_no_hace_nada(self, gestor, trip, booking_pdf):
        from app.tasks import document_tasks

        document = _upload(gestor, trip, booking_pdf)
        # Storage runs before the document has been validated or scanned.
        document_tasks.store_document.run(str(document.id))

        db.session.refresh(document)
        assert document.estado_proceso is S.RECIBIDO
        assert document.objeto_storage is None


@pytest.mark.integration
class TestPipelineCompleto:
    def test_el_documento_llega_a_revision(self, documento_procesado):
        assert documento_procesado.estado_proceso is S.PENDIENTE_REVISION
        assert documento_procesado.objeto_storage is not None
        assert documento_procesado.current_extraction is not None

    def test_se_extrae_el_texto_y_se_clasifica(self, documento_procesado):
        assert documento_procesado.texto is not None
        assert documento_procesado.texto.caracteres > 0
        assert documento_procesado.clasificacion is DocumentClassification.VUELO, (
            'Un documento con IATA, número de vuelo y localizador es un vuelo.'
        )
        assert documento_procesado.clasificacion_confianza >= 0.7

    def test_se_guarda_el_texto_por_pagina(self, documento_procesado):
        """Per-page text is what lets an extracted value cite its page."""
        assert documento_procesado.pages, 'Debe haber texto por página.'
        assert documento_procesado.pages[0].numero == 1

    def test_el_original_sigue_disponible(self, documento_procesado):
        from app.services import storage_service
        from app.utils.hashing import sha256_stream

        stream = storage_service.open_document(documento_procesado)
        digest, _ = sha256_stream(stream)
        assert digest == documento_procesado.hash_sha256


@pytest.mark.unit
class TestReproceso:
    """Reprocessing must actually reprocess.

    Every task declines to act unless the document is in its own input state --
    the property that makes a Celery redelivery harmless. Without rewinding
    first, enqueuing the chain over a finished document runs every step and
    changes nothing, which made the «Reintentar» button do nothing at all.
    """

    def test_un_documento_terminado_vuelve_a_extraerse(
        self, gestor, documento_procesado
    ):
        """The rewind is a means, not the end: what matters is a new extraction.

        Reprocessing rewinds and then runs the pipeline again, so the document
        ends back at review -- but with a new version, which is what it never
        produced before.
        """
        from app.models.extraction import Extraction

        document = documento_procesado
        assert document.estado_proceso is S.PENDIENTE_REVISION
        antes = Extraction.query.filter_by(document_id=document.id).count()

        document_service.reprocess(gestor, document)

        db.session.refresh(document)
        assert Extraction.query.filter_by(document_id=document.id).count() > antes, (
            'Reprocesar debe producir una extracción nueva, no dar un rodeo '
            'por el proceso sin hacer nada.'
        )
        assert any(
            t.estado_nuevo is S.CLASIFICADO and t.tarea == 'reprocess'
            for t in document.transitions
        ), 'El retroceso hasta la clasificación debe constar.'

    def test_el_retroceso_queda_registrado(self, gestor, documento_procesado):
        document_service.reprocess(gestor, documento_procesado)

        db.session.refresh(documento_procesado)
        assert any(
            t.tarea == 'reprocess' for t in documento_procesado.transitions
        ), 'El retroceso debe constar en el histórico de transiciones.'

    def test_se_limpia_el_error_anterior(self, gestor, trip, booking_pdf):
        document = _upload(gestor, trip, booking_pdf)
        document_service.mark_failed(document, 'algo', 'falló algo', 'validate')
        assert document.error_codigo

        document_service.reprocess(gestor, document)

        db.session.refresh(document)
        assert document.error_codigo is None, (
            'Un error resuelto no debe seguir mostrándose tras reintentar.'
        )

    def test_no_se_reprocesa_un_documento_infectado(self, gestor, trip, booking_pdf):
        from app.utils.errors import ConflictError

        document = _upload(gestor, trip, booking_pdf)
        document_service.transition(document, S.VALIDADO)
        document_service.transition(document, S.INFECTADO)

        with pytest.raises(ConflictError):
            document_service.reprocess(gestor, document)

    def test_el_retroceso_solo_va_hacia_atras(self, gestor, trip, booking_pdf):
        """A rewind is not a shortcut to a later state."""
        from app.utils.errors import InvalidTransition

        document = _upload(gestor, trip, booking_pdf)
        document_service.transition(document, S.VALIDADO)

        with pytest.raises(InvalidTransition, match='solo retrocede'):
            document_service.transition(document, S.APROBADO, retroceso=True)

    def test_tras_retroceder_las_tareas_vuelven_a_actuar(
        self, gestor, documento_procesado
    ):
        """The point of the rewind: the pipeline produces a new extraction."""
        from app.models.extraction import Extraction
        from app.tasks import document_tasks

        document = documento_procesado
        versiones_antes = Extraction.query.filter_by(document_id=document.id).count()

        document_service.reprocess(gestor, document)
        db.session.refresh(document)

        for _, task in document_tasks.PIPELINE:
            task.run(str(document.id))

        db.session.refresh(document)
        assert document.estado_proceso is S.PENDIENTE_REVISION
        assert Extraction.query.filter_by(document_id=document.id).count() > versiones_antes, (
            'El reproceso debe generar una extracción nueva.'
        )
