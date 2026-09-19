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
