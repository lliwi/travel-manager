"""Deleting a trip from the browser.

The service, the route and the permission existed from the start; no template
ever linked to them, so a trip could only be deleted through the API. These
tests pin the web path and, more importantly, what deletion means here: the
trip leaves the listings, and everything that proves it existed stays.
"""
import pytest

from app.extensions import db
from app.models.audit import AuditEvent
from app.models.trip import Trip


def _url(trip):
    return f'/trips/{trip.id}/eliminar'


@pytest.mark.integration
class TestBorradoDesdeLaInterfaz:
    def test_el_boton_aparece_para_quien_puede(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(f'/trips/{trip.id}').get_data(as_text=True)

        assert 'Eliminar viaje' in html
        assert _url(trip) in html

    def test_se_elimina(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            respuesta = client.post(_url(trip), follow_redirects=True)

        assert respuesta.status_code == 200
        db.session.refresh(trip)
        assert trip.is_deleted

    def test_deja_de_aparecer_en_el_listado(self, as_user, gestor, trip):
        referencia = trip.referencia

        with as_user(gestor) as client:
            antes = client.get('/trips/').get_data(as_text=True)
            assert referencia in antes

            client.post(_url(trip), follow_redirects=True)
            despues = client.get('/trips/').get_data(as_text=True)

        assert referencia not in despues

    def test_queda_registrado_quien_lo_borro(self, as_user, gestor, trip):
        """Soft-deleted, and the trail says by whom.

        There is no undo in the interface. What makes that acceptable is that
        nothing is actually destroyed: the row, its documents and the audit
        entry all survive, so a mistake is recoverable by someone with database
        access rather than gone.
        """
        with as_user(gestor) as client:
            client.post(_url(trip), follow_redirects=True)

        db.session.refresh(trip)
        assert trip.deleted_by_id == gestor.id
        assert trip.deleted_at is not None
        assert AuditEvent.query.filter_by(
            accion='trip.deleted', recurso_id=str(trip.id)
        ).count() == 1

    def test_los_documentos_sobreviven(self, as_user, gestor, trip, booking_pdf):
        import io

        from werkzeug.datastructures import FileStorage

        from app.models.document import Document
        from app.models.enums import DocumentType
        from app.services import document_service

        storage = FileStorage(
            stream=io.BytesIO(booking_pdf), filename='reserva.pdf',
            content_type='application/pdf',
        )
        documento = document_service.upload(
            gestor, trip, storage, tipo=DocumentType.RESERVA, commit=False,
        )
        db.session.commit()

        with as_user(gestor) as client:
            client.post(_url(trip), follow_redirects=True)

        db.session.refresh(documento)
        assert not documento.is_deleted
        assert Document.query.filter_by(id=documento.id).first() is not None

    def test_no_se_borra_una_segunda_vez(self, as_user, gestor, trip):
        """A deleted trip stays viewable but accepts no further mutation."""
        with as_user(gestor) as client:
            client.post(_url(trip), follow_redirects=True)
            respuesta = client.post(_url(trip))

        assert respuesta.status_code == 403
        assert Trip.query.filter_by(id=trip.id).first() is not None


@pytest.mark.security
class TestQuienPuedeBorrar:
    def test_un_viajero_no_puede(self, as_user, viajero, trip):
        with as_user(viajero) as client:
            respuesta = client.post(_url(trip))

        assert respuesta.status_code == 403
        db.session.refresh(trip)
        assert not trip.is_deleted

    def test_al_viajero_no_se_le_ofrece(self, as_user, viajero, trip):
        with as_user(viajero) as client:
            html = client.get(f'/trips/{trip.id}').get_data(as_text=True)

        assert 'Eliminar viaje' not in html

    def test_un_ajeno_no_sabe_que_existe(self, as_user, ajeno, trip):
        with as_user(ajeno) as client:
            respuesta = client.post(_url(trip))

        assert respuesta.status_code == 404

    def test_el_formulario_lleva_su_token(self, as_user, gestor, trip):
        """Deletion is a POST with a token, not a link someone can be sent.

        Whether the token is then enforced is configuration, and the suite runs
        with CSRF off so that every other test can post freely -- so what is
        checked here is the part this template is responsible for: that the
        form carries one at all.
        """
        with as_user(gestor) as client:
            html = client.get(f'/trips/{trip.id}').get_data(as_text=True)

        formulario = html[html.index(_url(trip)):][:400]

        assert 'csrf_token' in formulario
        assert 'method="post"' in html[:html.index(_url(trip))][-200:]

