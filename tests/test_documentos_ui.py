"""Two doors the document page never opened.

Deleting a document existed as a route, a permission and a service, and no
template linked to any of them. The review screen was shown only while the
document waited, which with automatic approval is never -- and that screen is
where the data that went in without asking gets corrected.
"""
import io

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models.enums import DocumentType
from app.services import document_service


def _subir(gestor, trip, datos, nombre='reserva.pdf'):
    storage = FileStorage(
        stream=io.BytesIO(datos), filename=nombre, content_type='application/pdf',
    )
    documento = document_service.upload(
        gestor, trip, storage, tipo=DocumentType.RESERVA, commit=False,
    )
    db.session.commit()
    return documento


@pytest.mark.integration
class TestLaFichaDeRevisionSiempreAccesible:
    def test_se_ofrece_aunque_ya_este_aprobado(self, as_user, gestor, documento_procesado):
        from app.models.enums import DocumentProcessState as S

        document_service.transition(documento_procesado, S.REVISADO)
        document_service.transition(documento_procesado, S.APROBADO)
        db.session.commit()

        with as_user(gestor) as client:
            html = client.get(
                f'/documents/{documento_procesado.id}'
            ).get_data(as_text=True)

        assert 'Revisar o corregir datos' in html, (
            'Si el dato entra solo, esta pantalla es la única forma de '
            'corregirlo.'
        )

    def test_la_pantalla_abre(self, as_user, gestor, documento_procesado):
        from app.models.enums import DocumentProcessState as S

        document_service.transition(documento_procesado, S.REVISADO)
        document_service.transition(documento_procesado, S.APROBADO)
        db.session.commit()

        with as_user(gestor) as client:
            respuesta = client.get(f'/documents/{documento_procesado.id}/revision')

        assert respuesta.status_code == 200

    def test_no_se_ofrece_si_no_hay_nada_extraido(
        self, as_user, gestor, trip, booking_pdf
    ):
        documento = _subir(gestor, trip, booking_pdf)

        with as_user(gestor) as client:
            html = client.get(f'/documents/{documento.id}').get_data(as_text=True)

        assert 'Revisar' not in html


@pytest.mark.integration
class TestEliminarUnDocumento:
    def test_el_boton_aparece(self, as_user, gestor, trip, booking_pdf):
        documento = _subir(gestor, trip, booking_pdf)

        with as_user(gestor) as client:
            html = client.get(f'/documents/{documento.id}').get_data(as_text=True)

        assert 'Eliminar documento' in html

    def test_se_elimina(self, as_user, gestor, trip, booking_pdf):
        documento = _subir(gestor, trip, booking_pdf)

        with as_user(gestor) as client:
            respuesta = client.post(
                f'/documents/{documento.id}/eliminar', follow_redirects=True,
            )

        assert respuesta.status_code == 200
        db.session.refresh(documento)
        assert documento.is_deleted

    def test_deja_de_aparecer_en_el_viaje(self, as_user, gestor, trip, booking_pdf):
        documento = _subir(gestor, trip, booking_pdf, nombre='desaparece.pdf')

        with as_user(gestor) as client:
            client.post(f'/documents/{documento.id}/eliminar', follow_redirects=True)
            html = client.get(f'/documents/viaje/{trip.id}').get_data(as_text=True)

        assert 'desaparece.pdf' not in html

    def test_lo_que_ya_estaba_en_el_itinerario_se_conserva(
        self, gestor, documento_procesado, as_user
    ):
        """Deleting the source does not retract what a manager already applied."""
        from app.models.enums import DocumentClassification
        from app.models.itinerary import TravelSegment
        from app.services import extraction_service

        documento_procesado.clasificacion = DocumentClassification.VUELO
        extraccion = documento_procesado.current_extraction
        extraccion.payload_normalizado = {
            'servicios': [{
                'campos': {'numero_vuelo': 'VY7604'},
                'confianzas': {'numero_vuelo': 0.85},
            }],
            'avisos': [],
        }
        extraccion.payload = extraccion.payload_normalizado
        db.session.commit()
        extraction_service.approve(gestor, extraccion)

        with as_user(gestor) as client:
            client.post(
                f'/documents/{documento_procesado.id}/eliminar', follow_redirects=True,
            )

        tramo = TravelSegment.query.filter_by(numero='VY7604').first()
        assert tramo is not None
        assert not tramo.is_deleted


@pytest.mark.security
class TestQuienPuedeRevisar:
    """A button that answers 403 is worse than no button.

    Reviewing an extraction is a manager's job; a traveller was being offered
    it anyway, because the link was conditioned on there being something
    extracted rather than on being allowed to touch it.
    """

    def test_al_viajero_no_se_le_ofrece_revisar(
        self, as_user, viajero, documento_procesado
    ):
        with as_user(viajero) as client:
            html = client.get(
                f'/documents/{documento_procesado.id}'
            ).get_data(as_text=True)

        assert 'Revisar' not in html

    def test_y_la_ruta_se_lo_niega(self, as_user, viajero, documento_procesado):
        with as_user(viajero) as client:
            respuesta = client.get(f'/documents/{documento_procesado.id}/revision')

        assert respuesta.status_code == 403

    def test_al_gestor_si(self, as_user, gestor, documento_procesado):
        with as_user(gestor) as client:
            html = client.get(
                f'/documents/{documento_procesado.id}'
            ).get_data(as_text=True)

        assert 'Revisar' in html


@pytest.mark.security
class TestQuienPuedeBorrarUnDocumento:
    def test_un_viajero_no_puede(self, as_user, viajero, trip, booking_pdf, gestor):
        documento = _subir(gestor, trip, booking_pdf)

        with as_user(viajero) as client:
            respuesta = client.post(f'/documents/{documento.id}/eliminar')

        assert respuesta.status_code == 403
        db.session.refresh(documento)
        assert not documento.is_deleted

    def test_al_viajero_no_se_le_ofrece(self, as_user, viajero, trip, booking_pdf, gestor):
        documento = _subir(gestor, trip, booking_pdf)

        with as_user(viajero) as client:
            html = client.get(f'/documents/{documento.id}').get_data(as_text=True)

        assert 'Eliminar documento' not in html


@pytest.mark.integration
class TestUnDocumentoBorradoNoSeOfrece:
    """Borrar un documento deja atrás lo que produjo, y debe dejarlo.

    «Esto salió de aquel documento» es un hecho sobre cómo se construyó el
    viaje, y la aprobación que lo convirtió en itinerario la hizo una persona.
    Borrar la procedencia para no dejar un enlace muerto tiraría la respuesta a
    «de dónde salió esto».

    Lo que no debe sobrevivir es el ofrecimiento: un botón que lleva a un 404
    parece una aplicación rota, no un documento que alguien quitó a propósito.
    """

    def test_el_enlace_desaparece_al_borrar_el_documento(
        self, as_user, gestor, trip, documento_aprobado,
    ):
        from app.models.itinerary import TravelSegment
        from app.services import document_service

        item = TravelSegment.query.filter_by(
            documento_origen_id=documento_aprobado.id).first()
        assert item is not None, 'el documento debería haber producido un tramo'

        with as_user(gestor) as client:
            antes = client.get(f'/trips/{trip.id}').get_data(as_text=True)
            assert f'/documents/{documento_aprobado.id}' in antes

            document_service.delete(gestor, documento_aprobado)
            despues = client.get(f'/trips/{trip.id}').get_data(as_text=True)

        assert f'/documents/{documento_aprobado.id}' not in despues

    def test_pero_la_procedencia_se_conserva(
        self, app, gestor, documento_aprobado,
    ):
        from app.extensions import db
        from app.models.itinerary import TravelSegment
        from app.services import document_service

        item = TravelSegment.query.filter_by(
            documento_origen_id=documento_aprobado.id).first()

        document_service.delete(gestor, documento_aprobado)
        db.session.refresh(item)

        assert item.documento_origen_id == documento_aprobado.id
        assert item.documento_origen_abrible is False

    def test_uno_vivo_si_se_ofrece(self, app, documento_aprobado):
        from app.models.itinerary import TravelSegment

        item = TravelSegment.query.filter_by(
            documento_origen_id=documento_aprobado.id).first()

        assert item.documento_origen_abrible is True

    def test_un_elemento_a_mano_no_ofrece_ninguno(self, app, trip, segment_factory):
        item = segment_factory(numero='IB0000')

        assert item.documento_origen_id is None
        assert item.documento_origen_abrible is False
