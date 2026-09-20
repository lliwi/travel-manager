"""A trip that is happening should say so without being told.

Some state changes are nobody's decision: a trip whose first flight left this
morning is under way whether or not a manager remembered to move it, and the
alert engine skips closed trips, so one left at «confirmado» for months keeps
being recalculated for a journey that already happened.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models.enums import TripStatus
from app.models.trip import Trip
from app.services import trip_service
from app.utils.timeutil import set_instant, utcnow


@pytest.fixture
def viaje(gestor, seeded):
    def _crear(estado, inicio_dias, fin_dias):
        ahora = utcnow()
        trip = trip_service.create_trip(gestor, titulo=f'Viaje {estado}', estado=estado)
        set_instant(
            trip, 'inicio',
            (ahora + timedelta(days=inicio_dias)).replace(tzinfo=None), 'UTC',
        )
        set_instant(
            trip, 'fin',
            (ahora + timedelta(days=fin_dias)).replace(tzinfo=None), 'UTC',
        )
        db.session.commit()
        return trip

    return _crear


@pytest.mark.unit
class TestElViajeEmpieza:
    def test_un_viaje_confirmado_cuya_fecha_llego_pasa_a_en_curso(self, viaje):
        trip = viaje(TripStatus.CONFIRMADO, -1, 3)

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.EN_CURSO

    def test_uno_en_preparacion_tambien(self, viaje):
        trip = viaje(TripStatus.EN_PREPARACION, -1, 3)

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.EN_CURSO

    def test_uno_que_aun_no_ha_empezado_no_se_toca(self, viaje):
        trip = viaje(TripStatus.CONFIRMADO, 5, 8)

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.CONFIRMADO

    def test_un_borrador_no_avanza(self, viaje):
        """Its dates may be a guess, and nobody confirmed the trip exists."""
        trip = viaje(TripStatus.BORRADOR, -1, 3)

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.BORRADOR


@pytest.mark.unit
class TestElViajeTermina:
    def test_uno_en_curso_cuya_vuelta_paso_se_finaliza(self, viaje):
        trip = viaje(TripStatus.EN_CURSO, -5, -1)

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.FINALIZADO

    def test_uno_confirmado_cuyo_viaje_entero_paso_se_finaliza(self, viaje):
        """In one pass, not two: it does not stop at «en curso» on the way."""
        trip = viaje(TripStatus.CONFIRMADO, -5, -1)

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.FINALIZADO

    def test_uno_en_curso_que_no_ha_vuelto_sigue_en_curso(self, viaje):
        trip = viaje(TripStatus.EN_CURSO, -1, 3)

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.EN_CURSO


@pytest.mark.unit
class TestLoQueNoSeToca:
    def test_un_viaje_cancelado_se_queda_cancelado(self, viaje):
        """That state is a decision, not a stage."""
        trip = viaje(TripStatus.CANCELADO, -5, -1)

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.CANCELADO

    def test_un_viaje_sin_fechas_no_avanza(self, gestor, seeded):
        trip = trip_service.create_trip(
            gestor, titulo='Sin fechas', estado=TripStatus.CONFIRMADO,
        )
        db.session.commit()

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.CONFIRMADO

    def test_un_viaje_eliminado_no_avanza(self, gestor, viaje):
        trip = viaje(TripStatus.CONFIRMADO, -1, 3)
        trip.soft_delete(gestor)
        db.session.commit()

        trip_service.advance_states()

        db.session.refresh(trip)
        assert trip.estado is TripStatus.CONFIRMADO


@pytest.mark.unit
class TestQuedaRastro:
    def test_el_cambio_se_audita(self, viaje):
        """A state that changed itself is the kind a reader wants explained."""
        from app.models.audit import AuditEvent

        trip = viaje(TripStatus.CONFIRMADO, -1, 3)

        trip_service.advance_states()

        evento = AuditEvent.query.filter_by(
            accion='trip.status_changed', recurso_id=str(trip.id),
        ).first()
        assert evento is not None
        assert 'ya ha llegado' in str(evento.metadatos)

    def test_la_tarea_informa_de_lo_aplicado(self, viaje):
        from app.tasks.maintenance_tasks import advance_trip_states

        viaje(TripStatus.CONFIRMADO, -1, 3)
        viaje(TripStatus.EN_CURSO, -5, -1)

        resultado = advance_trip_states.run()

        assert resultado == {'en_curso': 1, 'finalizado': 1}

    def test_esta_programada(self):
        from app.tasks.celery_app import celery

        tareas = {c['task'] for c in celery.conf.beat_schedule.values()}

        assert 'app.tasks.maintenance.advance_trip_states' in tareas


@pytest.mark.unit
class TestNoSeRepite:
    def test_pasar_dos_veces_no_cambia_nada(self, viaje):
        from app.models.audit import AuditEvent

        trip = viaje(TripStatus.CONFIRMADO, -1, 3)
        trip_service.advance_states()
        antes = AuditEvent.query.filter_by(
            accion='trip.status_changed', recurso_id=str(trip.id),
        ).count()

        trip_service.advance_states()

        assert AuditEvent.query.filter_by(
            accion='trip.status_changed', recurso_id=str(trip.id),
        ).count() == antes
        assert Trip.query.filter_by(id=trip.id).first().estado is TripStatus.EN_CURSO
