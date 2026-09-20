"""Alert engine: rules and reconciliation.

The reconciler's four invariants are what make the engine safe to re-run, and
they are each tested by name below.
"""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models.alert import Alert, AlertRuleSetting
from app.models.enums import (
    AlertSeverity,
    AlertState,
    TripStatus,
)
from app.services import alert_service
from app.services.alerts import engine


def _connection(segment_factory, traveler, llegada_local, salida_local,
                llegada_tz='Europe/Berlin', salida_tz='Europe/Berlin',
                hub='FRA', hub_pais='DE', origen_pais='ES', destino_pais='DE'):
    """Two consecutive segments meeting at ``hub``."""
    primero = segment_factory(
        numero='IB3210', origen='MAD', destino=hub,
        origen_pais=origen_pais, destino_pais=hub_pais,
        salida=llegada_local - timedelta(hours=2), salida_tz='Europe/Madrid',
        llegada=llegada_local, llegada_tz=llegada_tz,
        traveler=traveler,
    )
    segundo = segment_factory(
        numero='LH1234', origen=hub, destino='BER',
        origen_pais=hub_pais, destino_pais=destino_pais,
        salida=salida_local, salida_tz=salida_tz,
        llegada=salida_local + timedelta(hours=1), llegada_tz='Europe/Berlin',
        traveler=traveler,
    )
    return primero, segundo


@pytest.mark.unit
class TestReglaDeConexion:
    """Specification section 2.4: configurable connection thresholds."""

    def test_schengen_usa_noventa_minutos(self, trip, segment_factory, traveler_row):
        _connection(
            segment_factory, traveler_row,
            llegada_local=datetime(2026, 6, 1, 12, 0),
            salida_local=datetime(2026, 6, 1, 13, 0),  # 60 min < 90
        )
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        assert alerta is not None
        assert alerta.evidencia['umbral_minutos'] == 90
        assert alerta.evidencia['margen_minutos'] == 60

    def test_una_conexion_schengen_holgada_no_alerta(
        self, trip, segment_factory, traveler_row
    ):
        _connection(
            segment_factory, traveler_row,
            llegada_local=datetime(2026, 6, 1, 12, 0),
            salida_local=datetime(2026, 6, 1, 14, 0),  # 120 min > 90
        )
        engine.run(trip)

        assert Alert.query.filter_by(tipo='conexion_margen_insuficiente').count() == 0

    def test_una_conexion_internacional_usa_ciento_cincuenta(
        self, trip, segment_factory, traveler_row
    ):
        _connection(
            segment_factory, traveler_row,
            llegada_local=datetime(2026, 6, 1, 12, 0),
            salida_local=datetime(2026, 6, 1, 14, 0),  # 120 min < 150
            destino_pais='US',
        )
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        assert alerta is not None
        assert alerta.evidencia['umbral_minutos'] == 150
        assert 'internacional' in alerta.evidencia['umbral_motivo']

    def test_un_pais_desconocido_usa_el_umbral_mas_exigente(
        self, trip, segment_factory, traveler_row
    ):
        """An unknown country must not be assumed to be Schengen."""
        _connection(
            segment_factory, traveler_row,
            llegada_local=datetime(2026, 6, 1, 12, 0),
            salida_local=datetime(2026, 6, 1, 14, 0),
            destino_pais=None,
        )
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        assert alerta is not None
        assert alerta.evidencia['umbral_minutos'] == 150
        assert 'desconocido' in alerta.evidencia['umbral_motivo']

    def test_un_cambio_de_aeropuerto_suma_tiempo_de_traslado(
        self, trip, segment_factory, traveler_row
    ):
        """Connecting through a different airport needs the transfer time too."""
        segment_factory(
            numero='IB3210', origen='MAD', destino='CDG',
            origen_pais='ES', destino_pais='FR',
            salida=datetime(2026, 6, 1, 10, 0), salida_tz='Europe/Madrid',
            llegada=datetime(2026, 6, 1, 12, 0), llegada_tz='Europe/Paris',
            traveler=traveler_row,
        )
        segment_factory(
            numero='AF1234', origen='ORY', destino='BER',
            origen_pais='FR', destino_pais='DE',
            salida=datetime(2026, 6, 1, 15, 0), salida_tz='Europe/Paris',
            llegada=datetime(2026, 6, 1, 17, 0), llegada_tz='Europe/Berlin',
            traveler=traveler_row,
        )
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        assert alerta is not None, (
            '180 minutos entre dos aeropuertos distintos de París debe alertar.'
        )
        assert alerta.evidencia['umbral_minutos'] == 210, '90 Schengen + 120 de traslado.'
        assert alerta.evidencia['mismo_lugar'] is False

    def test_un_margen_muy_corto_es_critico(self, trip, segment_factory, traveler_row):
        _connection(
            segment_factory, traveler_row,
            llegada_local=datetime(2026, 6, 1, 12, 0),
            salida_local=datetime(2026, 6, 1, 12, 20),  # 20 min
        )
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        assert alerta.severidad is AlertSeverity.CRITICA

    def test_el_umbral_es_configurable(self, trip, segment_factory, traveler_row):
        """Section 2.4 requires the thresholds to be administrable."""
        setting = AlertRuleSetting.query.filter_by(
            regla='conexion_margen_insuficiente'
        ).first()
        setting.parametros = dict(setting.parametros, schengen_min=45)
        db.session.commit()

        _connection(
            segment_factory, traveler_row,
            llegada_local=datetime(2026, 6, 1, 12, 0),
            salida_local=datetime(2026, 6, 1, 13, 0),  # 60 min, now above 45
        )
        engine.run(trip)

        assert Alert.query.filter_by(tipo='conexion_margen_insuficiente').count() == 0

    def test_una_regla_desactivada_no_se_evalua(
        self, trip, segment_factory, traveler_row
    ):
        setting = AlertRuleSetting.query.filter_by(
            regla='conexion_margen_insuficiente'
        ).first()
        setting.activa = False
        db.session.commit()

        _connection(
            segment_factory, traveler_row,
            llegada_local=datetime(2026, 6, 1, 12, 0),
            salida_local=datetime(2026, 6, 1, 12, 30),
        )
        engine.run(trip)

        assert Alert.query.filter_by(tipo='conexion_margen_insuficiente').count() == 0


@pytest.mark.unit
class TestReglaDeSolapamiento:
    def test_dos_vuelos_simultaneos_alertan(self, trip, segment_factory, traveler_row):
        segment_factory(
            numero='IB1', salida=datetime(2026, 6, 1, 10, 0),
            llegada=datetime(2026, 6, 1, 14, 0),
            salida_tz='Europe/Madrid', llegada_tz='Europe/Madrid',
            traveler=traveler_row,
        )
        segment_factory(
            numero='IB2', salida=datetime(2026, 6, 1, 12, 0),
            llegada=datetime(2026, 6, 1, 16, 0),
            salida_tz='Europe/Madrid', llegada_tz='Europe/Madrid',
            traveler=traveler_row,
        )
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='segmentos_solapados').first()
        assert alerta is not None
        assert alerta.evidencia['solape_minutos'] == 120

    def test_vuelos_consecutivos_no_alertan(self, trip, segment_factory, traveler_row):
        segment_factory(
            numero='IB1', salida=datetime(2026, 6, 1, 10, 0),
            llegada=datetime(2026, 6, 1, 12, 0),
            salida_tz='Europe/Madrid', llegada_tz='Europe/Madrid',
            traveler=traveler_row,
        )
        segment_factory(
            numero='IB2', salida=datetime(2026, 6, 1, 14, 0),
            llegada=datetime(2026, 6, 1, 16, 0),
            salida_tz='Europe/Madrid', llegada_tz='Europe/Madrid',
            traveler=traveler_row,
        )
        engine.run(trip)

        assert Alert.query.filter_by(tipo='segmentos_solapados').count() == 0

    def test_hotel_y_coche_a_la_vez_no_es_un_problema(self, trip, traveler_row):
        """A hotel stay and a car rental legitimately span the same days."""
        from app.models.itinerary import Accommodation, VehicleRental
        from app.utils.timeutil import set_instant

        hotel = Accommodation(trip_id=trip.id, trip_traveler_id=traveler_row.id,
                              nombre='Hotel Central')
        set_instant(hotel, 'check_in', datetime(2026, 6, 1, 15, 0), 'Europe/Berlin')
        set_instant(hotel, 'check_out', datetime(2026, 6, 4, 11, 0), 'Europe/Berlin')

        coche = VehicleRental(trip_id=trip.id, trip_traveler_id=traveler_row.id,
                              proveedor='Europcar')
        set_instant(coche, 'recogida', datetime(2026, 6, 1, 16, 0), 'Europe/Berlin')
        set_instant(coche, 'devolucion', datetime(2026, 6, 4, 9, 0), 'Europe/Berlin')

        db.session.add_all([hotel, coche])
        db.session.commit()
        engine.run(trip)

        assert Alert.query.filter_by(tipo='segmentos_solapados').count() == 0


@pytest.mark.unit
class TestReglaDeZonaHoraria:
    def test_un_salto_grande_alerta(self, trip, segment_factory, traveler_row):
        segment_factory(
            numero='JL1', origen='MAD', destino='HND',
            origen_pais='ES', destino_pais='JP',
            salida=datetime(2026, 6, 1, 10, 0), salida_tz='Europe/Madrid',
            llegada=datetime(2026, 6, 2, 8, 0), llegada_tz='Asia/Tokyo',
            traveler=traveler_row,
        )
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='cambio_zona_horaria').first()
        assert alerta is not None
        assert alerta.evidencia['diferencia_horas'] == 7

    def test_un_salto_pequeno_no_alerta(self, trip, segment_factory, traveler_row):
        segment_factory(
            numero='IB1', origen='MAD', destino='LHR',
            origen_pais='ES', destino_pais='GB',
            salida=datetime(2026, 6, 1, 10, 0), salida_tz='Europe/Madrid',
            llegada=datetime(2026, 6, 1, 11, 0), llegada_tz='Europe/London',
            traveler=traveler_row,
        )
        engine.run(trip)

        assert Alert.query.filter_by(tipo='cambio_zona_horaria').count() == 0


@pytest.mark.unit
class TestReconciliador:
    """The four invariants that make the engine safe to re-run."""

    def _make_problem(self, trip, segment_factory, traveler_row):
        return _connection(
            segment_factory, traveler_row,
            llegada_local=datetime(2026, 6, 1, 12, 0),
            salida_local=datetime(2026, 6, 1, 13, 0),
        )

    def test_invariante_1_no_duplica(self, trip, segment_factory, traveler_row):
        self._make_problem(trip, segment_factory, traveler_row)

        engine.run(trip)
        engine.run(trip)
        engine.run(trip)

        assert Alert.query.filter_by(tipo='conexion_margen_insuficiente').count() == 1, (
            'Recalcular tres veces no debe crear tres alertas.'
        )

    def test_invariante_2_actualiza_la_evidencia(
        self, trip, segment_factory, traveler_row
    ):
        primero, segundo = self._make_problem(trip, segment_factory, traveler_row)
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        assert alerta.evidencia['margen_minutos'] == 60

        # The connection gets tighter: same problem, new numbers.
        from app.utils.timeutil import set_instant

        set_instant(segundo, 'salida', datetime(2026, 6, 1, 12, 40), 'Europe/Berlin')
        db.session.commit()
        run = engine.run(trip)

        db.session.refresh(alerta)
        assert alerta.evidencia['margen_minutos'] == 40
        assert run.actualizadas >= 1
        assert Alert.query.filter_by(tipo='conexion_margen_insuficiente').count() == 1

    def test_invariante_3_cierra_lo_resuelto(self, trip, segment_factory, traveler_row):
        primero, segundo = self._make_problem(trip, segment_factory, traveler_row)
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        assert alerta.estado is AlertState.ABIERTA

        # The manager moves the second flight; the problem is gone.
        from app.utils.timeutil import set_instant

        set_instant(segundo, 'salida', datetime(2026, 6, 1, 16, 0), 'Europe/Berlin')
        set_instant(segundo, 'llegada', datetime(2026, 6, 1, 17, 0), 'Europe/Berlin')
        db.session.commit()
        run = engine.run(trip)

        db.session.refresh(alerta)
        assert alerta.estado is AlertState.RESUELTA
        assert alerta.cierre_automatico is True
        assert run.cerradas >= 1

    def test_invariante_4_no_resucita_lo_descartado(
        self, gestor, trip, segment_factory, traveler_row
    ):
        self._make_problem(trip, segment_factory, traveler_row)
        engine.run(trip)

        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()
        alert_service.change_state(
            gestor, alerta, AlertState.DESCARTADA, 'No aplica: viaja sin equipaje.'
        )

        engine.run(trip)
        engine.run(trip)

        db.session.refresh(alerta)
        assert alerta.estado is AlertState.DESCARTADA, (
            'El motor no debe deshacer la decisión de un gestor.'
        )
        assert Alert.query.filter_by(tipo='conexion_margen_insuficiente').count() == 1

    def test_una_alerta_cerrada_por_el_motor_se_reabre_si_reaparece(
        self, trip, segment_factory, traveler_row
    ):
        from app.utils.timeutil import set_instant

        primero, segundo = self._make_problem(trip, segment_factory, traveler_row)
        engine.run(trip)
        alerta = Alert.query.filter_by(tipo='conexion_margen_insuficiente').first()

        set_instant(segundo, 'salida', datetime(2026, 6, 1, 16, 0), 'Europe/Berlin')
        db.session.commit()
        engine.run(trip)
        db.session.refresh(alerta)
        assert alerta.estado is AlertState.RESUELTA

        # The flight moves back; the problem returns.
        set_instant(segundo, 'salida', datetime(2026, 6, 1, 13, 0), 'Europe/Berlin')
        db.session.commit()
        engine.run(trip)

        db.session.refresh(alerta)
        assert alerta.estado is AlertState.ABIERTA, (
            'Una alerta que el motor cerró debe reabrirse si la condición vuelve.'
        )

    def test_un_viaje_cerrado_no_se_recalcula(self, trip, segment_factory, traveler_row):
        self._make_problem(trip, segment_factory, traveler_row)
        trip.estado = TripStatus.FINALIZADO
        db.session.commit()

        run = engine.run(trip)

        assert run.reglas_evaluadas == 0
        assert Alert.query.count() == 0

    def test_una_regla_que_falla_no_impide_las_demas(
        self, trip, segment_factory, traveler_row, monkeypatch
    ):
        """One broken rule must not stop the others protecting the traveller."""
        from app.services.alerts.rules.connections import ConnectionMarginRule

        def explota(self, ctx):
            raise RuntimeError('regla rota')

        monkeypatch.setattr(ConnectionMarginRule, 'evaluate', explota)

        self._make_problem(trip, segment_factory, traveler_row)
        run = engine.run(trip)

        assert run.creadas > 0, 'Las demás reglas deben haber generado alertas.'


@pytest.mark.unit
class TestQueCuentaComoConexion:
    """A connection is a transfer someone has to make, not any two legs.

    Taken from a real return booking: Barcelona→Gatwick on 31 October and
    Gatwick→Barcelona on 2 November were reported as a connection of «58 h
    35 min». That is the trip, with two nights and a hotel in the middle,
    described as a transfer nobody has to catch.
    """

    def _tramos(self, trip, segment_factory, salida_1, llegada_1, salida_2, llegada_2):
        from app.utils.timeutil import set_instant

        primero = segment_factory(numero='VY7604')
        set_instant(primero, 'salida', salida_1, 'Europe/Madrid')
        set_instant(primero, 'llegada', llegada_1, 'Europe/London')
        segundo = segment_factory(numero='VY7623')
        set_instant(segundo, 'salida', salida_2, 'Europe/London')
        set_instant(segundo, 'llegada', llegada_2, 'Europe/Madrid')
        db.session.commit()
        return primero, segundo

    def test_una_ida_y_vuelta_no_es_una_conexion(
        self, gestor, trip, segment_factory
    ):
        from datetime import datetime

        from app.services import itinerary_service

        self._tramos(
            trip, segment_factory,
            datetime(2026, 10, 31, 7, 55), datetime(2026, 10, 31, 9, 20),
            datetime(2026, 11, 2, 19, 55), datetime(2026, 11, 2, 23, 5),
        )

        timeline = itinerary_service.build_timeline(gestor, trip)

        assert timeline.connections == [], (
            'Dos días y dos noches de por medio no son un enlace que alcanzar.'
        )

    def test_un_enlace_del_mismo_dia_si_lo_es(self, gestor, trip, segment_factory):
        from datetime import datetime

        from app.services import itinerary_service

        self._tramos(
            trip, segment_factory,
            datetime(2026, 10, 31, 7, 55), datetime(2026, 10, 31, 9, 20),
            datetime(2026, 10, 31, 11, 0), datetime(2026, 10, 31, 13, 0),
        )

        timeline = itinerary_service.build_timeline(gestor, trip)

        assert len(timeline.connections) == 1

    def test_el_limite_es_configurable(self, app, seeded):
        from app.services import settings_service
        from app.services.itinerary_service import conexion_max_minutos, es_conexion

        settings_service.set_value('CONEXION_MAX_HORAS', 6)

        assert conexion_max_minutos() == 360
        assert es_conexion(300)
        assert not es_conexion(400)

    def test_un_solape_no_es_una_conexion(self, app, seeded):
        """Overlapping legs are the overlap rule's finding, not this one's."""
        from app.services.itinerary_service import es_conexion

        assert not es_conexion(-30)

    def test_la_regla_y_la_interfaz_usan_la_misma_definicion(
        self, gestor, trip, segment_factory
    ):
        """Or one describes something the other does not recognise."""
        from datetime import datetime

        from app.models.alert import Alert
        from app.services import alert_service, itinerary_service

        self._tramos(
            trip, segment_factory,
            datetime(2026, 10, 31, 7, 55), datetime(2026, 10, 31, 9, 20),
            datetime(2026, 11, 2, 19, 55), datetime(2026, 11, 2, 23, 5),
        )

        alert_service.recalculate(trip)
        timeline = itinerary_service.build_timeline(gestor, trip)

        reglas = {a.regla for a in Alert.query.filter_by(trip_id=trip.id).all()}
        assert timeline.connections == []
        assert not any('onexi' in r for r in reglas)


@pytest.mark.integration
class TestElFiltroPorDefecto:
    """The list opens on what is still wrong, not on everything that ever was.

    Once a trip has been running a while the resolved alerts outnumber the open
    ones and bury them, so the view worth looking at was reached only by
    filtering every time.
    """

    def _alerta(self, trip, estado, titulo, dedup):
        from app.models.alert import Alert
        from app.models.enums import AlertSeverity

        alerta = Alert(
            trip_id=trip.id, tipo='conexion', regla='Margen de conexión',
            dedup_key=dedup, severidad=AlertSeverity.ALTA, estado=estado,
            titulo=titulo, mensaje='x', evidencia={},
        )
        db.session.add(alerta)
        db.session.commit()
        return alerta

    def _url(self, trip):
        return f'/alerts/viaje/{trip.id}'

    def test_sin_filtro_solo_se_ven_las_abiertas(self, as_user, gestor, trip):
        from app.models.enums import AlertState

        self._alerta(trip, AlertState.ABIERTA, 'Sigue abierta', 'a' * 16)
        self._alerta(trip, AlertState.RESUELTA, 'Ya resuelta', 'b' * 16)

        with as_user(gestor) as client:
            html = client.get(self._url(trip)).get_data(as_text=True)

        assert 'Sigue abierta' in html
        assert 'Ya resuelta' not in html

    def test_todas_sigue_siendo_alcanzable(self, as_user, gestor, trip):
        """The default cannot become a wall: what it hides has to be one click
        away, or a manager cannot find an alert they resolved yesterday."""
        from app.models.enums import AlertState

        self._alerta(trip, AlertState.ABIERTA, 'Sigue abierta', 'c' * 16)
        self._alerta(trip, AlertState.RESUELTA, 'Ya resuelta', 'd' * 16)

        with as_user(gestor) as client:
            html = client.get(f'{self._url(trip)}?estado=todas').get_data(as_text=True)

        assert 'Sigue abierta' in html
        assert 'Ya resuelta' in html

    def test_la_pastilla_de_abiertas_sale_marcada(self, as_user, gestor, trip):
        """A filter nobody can see applied is a list that looks incomplete."""
        with as_user(gestor) as client:
            html = client.get(self._url(trip)).get_data(as_text=True)

        marcadas = [
            linea for linea in html.splitlines()
            if 'btn-outline-secondary active' in linea
        ]
        assert len(marcadas) == 1
        assert 'estado=abierta' in html

    def test_otro_estado_se_sigue_pudiendo_pedir(self, as_user, gestor, trip):
        from app.models.enums import AlertState

        self._alerta(trip, AlertState.DESCARTADA, 'Descartada', 'e' * 16)

        with as_user(gestor) as client:
            html = client.get(
                f'{self._url(trip)}?estado=descartada'
            ).get_data(as_text=True)

        assert 'Descartada' in html

    def test_sin_abiertas_se_dice_que_hay_un_filtro(self, as_user, gestor, trip):
        """Otherwise a trip with everything resolved looks like a trip that
        never had an alert, and what is hidden is exactly what matters."""
        from app.models.enums import AlertState

        self._alerta(trip, AlertState.RESUELTA, 'Ya resuelta', 'f' * 16)

        with as_user(gestor) as client:
            html = client.get(self._url(trip)).get_data(as_text=True)

        assert 'No hay alertas abiertas' in html
        assert 'Ver todas' in html

    def test_el_filtro_aplicado_se_ve(self):
        """Green fill on the active pill, because the default filter is the
        one nobody chose and therefore the one nobody expects."""
        from pathlib import Path

        css = (Path(__file__).resolve().parent.parent
               / 'app' / 'static' / 'css' / 'main.css').read_text()

        assert '.filter-pills .btn.active' in css
