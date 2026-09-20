"""Corporate policy: limits an organisation chose, not facts about a trip.

Every other rule describes something true of the itinerary -- two flights
overlap, a night has no room -- and is worth knowing anywhere. These describe a
decision, and a different organisation would decide otherwise, so every one of
them stays silent until somebody sets a limit.

A threshold of zero means «no tenemos esa política», never «el límite es cero».
The difference is the whole feature: a rule with no policy behind it flagging
every trip against a number nobody chose is worse than no rule.
"""
from datetime import datetime

import pytest

from app.extensions import db
from app.models.alert import Alert
from app.services import alert_service, settings_service
from app.utils.timeutil import set_instant


def _reglas_abiertas(trip):
    return {
        a.regla for a in Alert.query.filter_by(trip_id=trip.id).all()
    }


@pytest.fixture
def con_costes(app, seeded):
    settings_service.set_value('COSTES_HABILITADOS', True)
    settings_service.set_value('POLITICA_MONEDA', 'EUR')
    return settings_service


def _hotel(trip, importe, moneda='EUR', noches=2):
    from app.models.itinerary import Accommodation

    alojamiento = Accommodation(
        trip_id=trip.id, nombre='Hotel', importe=importe, moneda=moneda,
    )
    set_instant(alojamiento, 'check_in', datetime(2026, 6, 1, 15, 0), 'Europe/Madrid')
    set_instant(
        alojamiento, 'check_out', datetime(2026, 6, 1 + noches, 11, 0), 'Europe/Madrid',
    )
    db.session.add(alojamiento)
    db.session.commit()
    return alojamiento


@pytest.mark.unit
class TestSinPoliticaNoDicenNada:
    def test_sin_limite_no_salta_el_coste(self, gestor, trip, con_costes):
        settings_service.set_value('POLITICA_COSTE_MAXIMO_VIAJE', 0)
        _hotel(trip, importe=9999)
        db.session.refresh(trip)

        alert_service.recalculate(trip)

        assert 'Coste por encima de la política' not in _reglas_abiertas(trip)

    def test_sin_costes_habilitados_tampoco(self, gestor, trip, app, seeded):
        settings_service.set_value('COSTES_HABILITADOS', False)
        settings_service.set_value('POLITICA_COSTE_MAXIMO_VIAJE', 100)
        _hotel(trip, importe=9999)
        db.session.refresh(trip)

        alert_service.recalculate(trip)

        assert 'Coste por encima de la política' not in _reglas_abiertas(trip)

    def test_sin_antelacion_configurada_no_salta(self, gestor, trip, seeded):
        settings_service.set_value('POLITICA_ANTELACION_MINIMA_DIAS', 0)

        alert_service.recalculate(trip)

        assert 'Reserva con poca antelación' not in _reglas_abiertas(trip)


@pytest.mark.unit
class TestElCosteDelViaje:
    def test_se_suma_del_itinerario(self, gestor, trip, con_costes):
        """The bill, not the estimate: the estimate is a plan."""
        _hotel(trip, importe=300)
        db.session.refresh(trip)

        assert trip.coste_total == 300

    def test_sin_importes_no_es_cero(self, gestor, trip, con_costes):
        """«No lo sabemos» y «es gratis» no son lo mismo, y un límite no debe
        saltar por el primero."""
        assert trip.coste_total is None

    def test_por_encima_del_limite_avisa(self, gestor, trip, con_costes):
        settings_service.set_value('POLITICA_COSTE_MAXIMO_VIAJE', 200)
        _hotel(trip, importe=300)
        db.session.refresh(trip)

        alert_service.recalculate(trip)

        assert 'Coste por encima de la política' in _reglas_abiertas(trip)

    def test_por_debajo_no(self, gestor, trip, con_costes):
        settings_service.set_value('POLITICA_COSTE_MAXIMO_VIAJE', 500)
        _hotel(trip, importe=300)
        db.session.refresh(trip)

        alert_service.recalculate(trip)

        assert 'Coste por encima de la política' not in _reglas_abiertas(trip)

    def test_monedas_mezcladas_no_se_suman_a_ciegas(self, gestor, trip, con_costes):
        """Converting would need a rate this application does not have, and
        one that would change the answer without saying so."""
        settings_service.set_value('POLITICA_COSTE_MAXIMO_VIAJE', 100)
        _hotel(trip, importe=200, moneda='GBP')
        db.session.refresh(trip)

        alert_service.recalculate(trip)

        alerta = Alert.query.filter_by(trip_id=trip.id).filter(
            Alert.titulo.like('%no se puede comparar%')
        ).first()
        assert alerta is not None
        assert 'GBP' in str(alerta.evidencia)


@pytest.mark.unit
class TestElCostePorNoche:
    def test_por_encima_del_limite_avisa(self, gestor, trip, con_costes):
        settings_service.set_value('POLITICA_COSTE_MAXIMO_NOCHE', 100)
        _hotel(trip, importe=400, noches=2)
        db.session.refresh(trip)

        alert_service.recalculate(trip)

        assert 'Coste por noche por encima de la política' in _reglas_abiertas(trip)

    def test_se_calcula_por_noche_y_no_por_total(self, gestor, trip, con_costes):
        """400 en cuatro noches son 100 por noche: dentro del límite."""
        settings_service.set_value('POLITICA_COSTE_MAXIMO_NOCHE', 150)
        _hotel(trip, importe=400, noches=4)
        db.session.refresh(trip)

        alert_service.recalculate(trip)

        assert 'Coste por noche por encima de la política' not in _reglas_abiertas(trip)

    def test_otra_moneda_se_deja_en_paz(self, gestor, trip, con_costes):
        settings_service.set_value('POLITICA_COSTE_MAXIMO_NOCHE', 10)
        _hotel(trip, importe=400, moneda='GBP')
        db.session.refresh(trip)

        alert_service.recalculate(trip)

        assert 'Coste por noche por encima de la política' not in _reglas_abiertas(trip)


@pytest.mark.unit
class TestLaAntelacion:
    def test_reservar_tarde_avisa(self, gestor, trip, seeded):
        from datetime import timedelta

        from app.utils.timeutil import utcnow

        settings_service.set_value('POLITICA_ANTELACION_MINIMA_DIAS', 14)
        set_instant(
            trip, 'inicio', (utcnow() + timedelta(days=3)).replace(tzinfo=None), 'UTC',
        )
        db.session.commit()

        alert_service.recalculate(trip)

        assert 'Reserva con poca antelación' in _reglas_abiertas(trip)

    def test_con_antelacion_suficiente_no(self, gestor, trip, seeded):
        from datetime import timedelta

        from app.utils.timeutil import utcnow

        settings_service.set_value('POLITICA_ANTELACION_MINIMA_DIAS', 7)
        set_instant(
            trip, 'inicio', (utcnow() + timedelta(days=60)).replace(tzinfo=None), 'UTC',
        )
        db.session.commit()

        alert_service.recalculate(trip)

        assert 'Reserva con poca antelación' not in _reglas_abiertas(trip)


@pytest.mark.unit
class TestLaInterfazSigueAlAjuste:
    def test_los_costes_se_encienden_desde_ajustes(self, as_user, gestor, seeded):
        """They were read from the environment while the switch lived in the
        panel, so turning them on there did nothing to the screens."""
        settings_service.set_value('COSTES_HABILITADOS', True)

        with as_user(gestor) as client:
            html = client.get('/trips/nuevo').get_data(as_text=True)

        assert 'coste_estimado' in html
