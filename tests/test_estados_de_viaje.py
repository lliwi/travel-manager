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
from app.models.enums import TripStatus as S
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


@pytest.mark.unit
class TestQueTransicionesExisten:
    """Antes de esto, «change_status» aceptaba las treinta: «finalizado» de
    vuelta a «borrador», «cancelado» a «confirmado». Un estado que se puede
    poner a cualquier cosa no describe nada, y aquí decide cosas reales -- el
    motor de alertas y las recomendaciones saltan los viajes cerrados.
    """

    def _viaje(self, gestor, estado):
        return trip_service.create_trip(gestor, titulo='x', estado=estado)

    @pytest.mark.parametrize('origen, destino', [
        (S.BORRADOR, S.EN_PREPARACION),
        (S.EN_PREPARACION, S.BORRADOR),
        (S.CONFIRMADO, S.EN_PREPARACION),
        (S.CONFIRMADO, S.FINALIZADO),
        (S.EN_CURSO, S.FINALIZADO),
        (S.BORRADOR, S.CANCELADO),
        (S.EN_CURSO, S.CANCELADO),
    ])
    def test_las_que_tienen_sentido_se_permiten(self, app, seeded, gestor,
                                                origen, destino):
        viaje = self._viaje(gestor, origen)

        trip_service.change_status(gestor, viaje, destino, motivo='m')

        assert viaje.estado is destino

    @pytest.mark.parametrize('origen, destino', [
        (S.FINALIZADO, S.BORRADOR),
        (S.FINALIZADO, S.EN_CURSO),
        (S.FINALIZADO, S.CONFIRMADO),
        (S.CANCELADO, S.CONFIRMADO),
        (S.CANCELADO, S.FINALIZADO),
        (S.EN_CURSO, S.BORRADOR),
        (S.EN_CURSO, S.CONFIRMADO),
    ])
    def test_las_que_no_se_niegan(self, app, seeded, gestor, origen, destino):
        from app.utils.errors import ConflictError

        viaje = self._viaje(gestor, origen)

        with pytest.raises(ConflictError):
            trip_service.change_status(gestor, viaje, destino, motivo='m')

        assert viaje.estado is origen

    def test_finalizado_es_terminal(self, app, seeded, gestor):
        """Un viaje que ya ocurrió no vuelve a ocurrir."""
        assert trip_service.TRANSICIONES[S.FINALIZADO] == ()

    def test_el_desplegable_no_ofrece_lo_que_luego_niega(self, app, seeded, gestor):
        """Ofrecer un destino y negarlo después es peor que no ofrecerlo."""
        viaje = self._viaje(gestor, S.FINALIZADO)

        assert trip_service.estados_posibles(viaje) == []

    def test_ni_ofrece_en_curso_a_mano(self, app, seeded, gestor):
        """Llega solo por fecha; ponerlo antes diría que un viaje ha empezado
        cuando no ha empezado."""
        viaje = self._viaje(gestor, S.CONFIRMADO)

        assert S.EN_CURSO not in trip_service.estados_posibles(viaje)


@pytest.mark.unit
class TestQueHaceFaltaParaConfirmar:
    """«Confirmado» afirma tres cosas: cuándo es, quién va y cómo se llega. Sin
    ellas la palabra no significa nada y ningún informe puede apoyarse en ella.
    """

    def test_un_viaje_vacio_no_se_confirma(self, app, seeded, gestor):
        from app.utils.errors import ValidationError

        viaje = trip_service.create_trip(gestor, titulo='x',
                                         estado=S.EN_PREPARACION)

        with pytest.raises(ValidationError) as exc:
            trip_service.change_status(gestor, viaje, S.CONFIRMADO)

        assert 'fechas' in exc.value.mensaje

    def test_se_dice_todo_lo_que_falta_de_una_vez(self, app, seeded, gestor):
        """Descubrirlo de uno en uno es tres viajes a la pantalla."""
        viaje = trip_service.create_trip(gestor, titulo='x',
                                         estado=S.EN_PREPARACION)

        faltan = trip_service.requisitos_pendientes(viaje, S.CONFIRMADO)

        assert len(faltan) == 3

    def test_con_todo_puesto_se_confirma(self, app, seeded, gestor, trip,
                                         traveler_row, segment_factory):
        segment_factory()
        trip_service.change_status(gestor, trip, S.EN_PREPARACION)

        trip_service.change_status(gestor, trip, S.CONFIRMADO)

        assert trip.estado is S.CONFIRMADO

    def test_los_demas_destinos_no_piden_nada(self, app, seeded, gestor):
        """Cancelar un viaje vacío tiene que poder hacerse."""
        viaje = trip_service.create_trip(gestor, titulo='x')

        assert trip_service.requisitos_pendientes(viaje, S.CANCELADO) == []


@pytest.mark.unit
class TestReactivarUnViajeCancelado:
    def test_vuelve_a_preparacion_y_no_a_confirmado(self, app, seeded, gestor):
        """Lo que hubiera reservado hay que volver a mirarlo."""
        assert trip_service.TRANSICIONES[S.CANCELADO] == (S.EN_PREPARACION,)

    def test_exige_decir_por_que(self, app, seeded, gestor):
        """Es lo que distingue un cambio de opinión de un clic equivocado
        cuando alguien lo lea dentro de seis meses."""
        from app.utils.errors import ValidationError

        viaje = trip_service.create_trip(gestor, titulo='x', estado=S.CANCELADO)

        with pytest.raises(ValidationError) as exc:
            trip_service.change_status(gestor, viaje, S.EN_PREPARACION)

        assert 'por qué' in exc.value.mensaje

    def test_con_motivo_se_reactiva_y_queda_escrito(self, app, seeded, gestor):
        from app.models.audit import AuditEvent

        viaje = trip_service.create_trip(gestor, titulo='x', estado=S.CANCELADO)

        trip_service.change_status(gestor, viaje, S.EN_PREPARACION,
                                   motivo='El cliente ha reprogramado')

        assert viaje.estado is S.EN_PREPARACION
        evento = AuditEvent.query.filter_by(
            accion='trip.status_changed').order_by(AuditEvent.id.desc()).first()
        assert evento.metadatos['motivo'] == 'El cliente ha reprogramado'


@pytest.mark.unit
class TestElEstadoTieneUnaSolaPuerta:
    def test_editar_un_viaje_no_escribe_el_estado(self, app, seeded, gestor):
        """«update_trip» lo escribía junto al resto de campos, así que el
        formulario de edición se saltaba todas las reglas."""
        viaje = trip_service.create_trip(gestor, titulo='x', estado=S.FINALIZADO)

        trip_service.update_trip(gestor, viaje, estado=S.BORRADOR, titulo='y')

        assert viaje.estado is S.FINALIZADO
        assert viaje.titulo == 'y'

    def test_la_pantalla_de_edicion_tampoco(self, as_user, gestor, seeded):
        viaje = trip_service.create_trip(gestor, titulo='x', estado=S.FINALIZADO)

        with as_user(gestor) as client:
            client.post(f'/trips/{viaje.id}/editar', data={
                'csrf_token': 'x', 'titulo': 'x', 'estado': str(S.BORRADOR),
            }, follow_redirects=True)

        assert viaje.estado is S.FINALIZADO


@pytest.mark.unit
class TestElViajeSaleSoloDeBorrador:
    """Si alguien ya está adjuntando reservas, el viaje dejó de ser un boceto
    aunque nadie lo dijera. Pedir además que se acuerde de moverlo es como una
    lista se llena de borradores que en realidad están en marcha.
    """

    def test_al_asignar_un_viajero(self, app, seeded, gestor, viajero):
        viaje = trip_service.create_trip(gestor, titulo='x')

        trip_service.add_traveler(gestor, viaje, viajero)

        assert viaje.estado is S.EN_PREPARACION

    def test_al_crear_un_tramo(self, app, seeded, gestor):
        from datetime import datetime

        from app.services import itinerary_service

        viaje = trip_service.create_trip(gestor, titulo='x')

        itinerary_service.create_item(
            gestor, viaje, 'segmento',
            instants={'salida': (datetime(2026, 6, 1, 9, 0), 'Europe/Madrid')},
            origen_codigo='MAD', destino_codigo='BCN',
        )

        assert viaje.estado is S.EN_PREPARACION

    def test_al_adjuntar_un_documento(self, app, seeded, gestor, booking_pdf):
        import io

        from werkzeug.datastructures import FileStorage

        from app.services import document_service

        viaje = trip_service.create_trip(gestor, titulo='x')

        document_service.upload(gestor, viaje, FileStorage(
            stream=io.BytesIO(booking_pdf), filename='reserva.pdf',
            content_type='application/pdf',
        ), commit=False)

        assert viaje.estado is S.EN_PREPARACION

    def test_no_resucita_un_viaje_cancelado(self, app, seeded, gestor, viajero):
        """Lo automático no puede deshacer una decisión."""
        viaje = trip_service.create_trip(gestor, titulo='x', estado=S.CANCELADO)

        trip_service.marcar_actividad(viaje, gestor)

        assert viaje.estado is S.CANCELADO

    def test_ni_deshace_una_confirmacion(self, app, seeded, gestor):
        viaje = trip_service.create_trip(gestor, titulo='x', estado=S.CONFIRMADO)

        trip_service.marcar_actividad(viaje, gestor)

        assert viaje.estado is S.CONFIRMADO

    def test_lo_automatico_no_pide_requisitos(self, app, seeded, gestor):
        """Un viaje cuya ventana pasó está terminado, tenga itinerario o no."""
        from datetime import datetime, timedelta

        from app.utils.timeutil import set_instant, utcnow

        viaje = trip_service.create_trip(gestor, titulo='x',
                                         estado=S.EN_PREPARACION)
        ayer = utcnow() - timedelta(days=2)
        set_instant(viaje, 'fin', datetime(ayer.year, ayer.month, ayer.day, 9, 0),
                    'Europe/Madrid')
        db.session.commit()

        trip_service.advance_states()

        assert viaje.estado is S.FINALIZADO
