"""Numbers about the trips one person may see.

An aggregate is a leak like any other: «12 viajes en curso» told to somebody who
may see three of them says something about the other nine. Every query starts
from ``visible_trips_query`` so that a new report cannot forget, and this file
is what stops that being a claim.
"""
import pytest

from app.extensions import db
from app.models.enums import TripStatus
from app.services import report_service, trip_service


@pytest.fixture
def paisaje(gestor, trip, seeded):
    """A couple of trips the manager runs and nobody else is on.

    Visibility follows belonging, not authorship: «ajeno» is neither a manager
    by role nor a traveller on these, so for them they do not exist -- and
    neither should their totals.
    """
    otro = trip_service.create_trip(
        gestor, titulo='Otro viaje del gestor', estado=TripStatus.EN_CURSO,
    )
    db.session.commit()
    return trip, otro


@pytest.mark.security
class TestUnInformeNoFiltra:
    def test_no_cuenta_viajes_que_no_puede_ver(self, ajeno, paisaje):
        """The bar chart is as much a disclosure as the list would be.

        «12 viajes en curso» dicho a quien puede ver tres cuenta algo sobre los
        otros nueve.
        """
        total = sum(f['total'] for f in report_service.viajes_por_estado(ajeno))

        assert total == 0

    def test_el_gestor_ve_los_suyos(self, gestor, paisaje):
        por_estado = {
            f['clave']: f['total'] for f in report_service.viajes_por_estado(gestor)
        }

        assert sum(por_estado.values()) >= 1

    def test_un_viajero_solo_cuenta_los_suyos(
        self, viajero, traveler_row, paisaje
    ):
        total = sum(
            f['total'] for f in report_service.viajes_por_estado(viajero)
        )

        assert total == 1, 'Un viajero cuenta sus viajes, no los de la organización.'

    def test_las_alertas_tambien_se_acotan(self, ajeno, gestor, trip, seeded):
        from app.models.alert import Alert
        from app.models.enums import AlertSeverity, AlertState

        db.session.add(Alert(
            trip_id=trip.id, tipo='x', regla='Regla de prueba',
            dedup_key='z' * 16, severidad=AlertSeverity.ALTA,
            estado=AlertState.ABIERTA, titulo='No debería verla', mensaje='m', evidencia={},
        ))
        db.session.commit()

        reglas = {
            f['regla'] for f in report_service.alertas_por_regla(ajeno)
        }

        assert 'Regla de prueba' not in reglas

    def test_la_pantalla_exige_sesion(self, client):
        assert client.get('/dashboard/informes').status_code in (302, 401)


@pytest.mark.unit
class TestLoQueMideCadaInforme:
    def test_las_reglas_salen_ordenadas_por_frecuencia(self, gestor, trip, seeded):
        from app.models.alert import Alert
        from app.models.enums import AlertSeverity, AlertState

        for i in range(3):
            db.session.add(Alert(
                trip_id=trip.id, tipo='x', regla='Frecuente',
                dedup_key=f'a{i:015d}', severidad=AlertSeverity.ALTA,
                estado=AlertState.ABIERTA, titulo='t', mensaje='m', evidencia={},
            ))
        db.session.add(Alert(
            trip_id=trip.id, tipo='x', regla='Rara',
            dedup_key='b' * 16, severidad=AlertSeverity.MEDIA,
            estado=AlertState.ABIERTA, titulo='t', mensaje='m', evidencia={},
        ))
        db.session.commit()

        reglas = report_service.alertas_por_regla(gestor)

        assert reglas[0]['regla'] == 'Frecuente'
        assert reglas[0]['total'] == 3

    def test_una_alerta_cerrada_no_cuenta(self, gestor, trip, seeded):
        from app.models.alert import Alert
        from app.models.enums import AlertSeverity, AlertState

        db.session.add(Alert(
            trip_id=trip.id, tipo='x', regla='Ya resuelta',
            dedup_key='c' * 16, severidad=AlertSeverity.ALTA,
            estado=AlertState.RESUELTA, titulo='t', mensaje='m', evidencia={},
        ))
        db.session.commit()

        reglas = {f['regla'] for f in report_service.alertas_por_regla(gestor)}

        assert 'Ya resuelta' not in reglas

    def test_la_calidad_distingue_lo_extraido_de_lo_escrito(
        self, gestor, trip, segment_factory
    ):
        """«Cuántos documentos leímos» no dice nada; «cuánto hay que revisar», sí."""
        a_mano = segment_factory(numero='A-MANO')
        a_mano.requiere_revision = False
        db.session.commit()

        calidad = report_service.calidad_de_la_extraccion(gestor)['tramos']

        assert calidad['total'] >= 1
        assert calidad['a_mano'] >= 1
        assert calidad['de_documento'] == 0

    def test_un_documento_reciente_no_esta_atascado(
        self, gestor, trip, booking_pdf
    ):
        import io

        from werkzeug.datastructures import FileStorage

        from app.models.enums import DocumentType
        from app.services import document_service

        storage = FileStorage(
            stream=io.BytesIO(booking_pdf), filename='nuevo.pdf',
            content_type='application/pdf',
        )
        document_service.upload(
            gestor, trip, storage, tipo=DocumentType.RESERVA, commit=False,
        )
        db.session.commit()

        assert report_service.documentos_atascados(gestor) == []

    def test_uno_olvidado_si(self, gestor, trip, booking_pdf):
        import io
        from datetime import timedelta

        from werkzeug.datastructures import FileStorage

        from app.models.enums import DocumentType
        from app.services import document_service
        from app.utils.timeutil import utcnow

        storage = FileStorage(
            stream=io.BytesIO(booking_pdf), filename='olvidado.pdf',
            content_type='application/pdf',
        )
        documento = document_service.upload(
            gestor, trip, storage, tipo=DocumentType.RESERVA, commit=False,
        )
        db.session.commit()
        documento.updated_at = utcnow() - timedelta(days=10)
        db.session.commit()

        atascados = report_service.documentos_atascados(gestor)

        assert documento.id in {d.id for d in atascados}

    def test_el_informe_completo_no_revienta_sin_datos(self, ajeno, seeded):
        """A fresh install opens this page before anything has happened."""
        informe = report_service.informe_completo(ajeno)

        assert informe['viajes_por_estado']
        assert informe['alertas_por_regla'] == []
        assert informe['documentos_atascados'] == []
