"""Qué gastó cada proyecto, mes a mes.

Dos cosas que este informe no puede hacer mal:

- **No es para todo el mundo.** Los costes viven tras el interruptor
  «COSTES_HABILITADOS» y el permiso «VER_COSTES», que el rol usuario no tiene
  nunca. A quien no lo tiene se le dice que no, no se le enseñan ceros: «todos
  los proyectos a cero» es una afirmación distinta de «esto no es para usted»,
  y la segunda es la verdadera.
- **Los totales cuadran.** El proyecto es opcional, así que los viajes sin él
  se agrupan en vez de caerse: un total que los deja fuera no suma lo que
  sumaría cualquier otro.
"""
from datetime import datetime, timedelta

import pytest

from app.services import report_service, settings_service, trip_service
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.unit


def _viaje(actor, titulo, proyecto, coste, meses_atras=0, moneda='EUR'):
    from app.extensions import db
    from app.models.enums import TripStatus

    cuando = utcnow() - timedelta(days=31 * meses_atras)
    trip = trip_service.create_trip(
        actor, titulo=titulo, estado=TripStatus.CONFIRMADO, proyecto=proyecto,
        inicio_local=datetime(cuando.year, cuando.month, 10, 9, 0),
        inicio_tz='Europe/Madrid',
        fin_local=datetime(cuando.year, cuando.month, 12, 18, 0),
        fin_tz='Europe/Madrid',
    )
    trip.coste_estimado = coste
    trip.moneda = moneda
    db.session.commit()
    return trip


@pytest.fixture
def costes_activos(app, seeded):
    settings_service.set_value('COSTES_HABILITADOS', True)


class TestElCampoProyecto:
    def test_es_opcional(self, app, seeded, gestor):
        trip = trip_service.create_trip(gestor, titulo='Sin proyecto')

        assert trip.proyecto is None

    def test_se_guarda_al_crear(self, app, seeded, gestor):
        trip = trip_service.create_trip(gestor, titulo='Con proyecto',
                                        proyecto='  P-2026-014  ')

        assert trip.proyecto == 'P-2026-014'

    def test_se_puede_cambiar(self, app, seeded, gestor):
        trip = trip_service.create_trip(gestor, titulo='x', proyecto='A')

        trip_service.update_trip(gestor, trip, proyecto='B')

        assert trip.proyecto == 'B'

    def test_sale_en_el_to_dict(self, app, seeded, gestor):
        trip = trip_service.create_trip(gestor, titulo='x', proyecto='A')

        assert trip.to_dict()['proyecto'] == 'A'


class TestQuienPuedeVerElGasto:
    def test_un_gestor_si(self, costes_activos, gestor):
        _viaje(gestor, 'Uno', 'P-1', 1000)

        assert report_service.gastos_por_proyecto(gestor) is not None

    def test_un_viajero_no(self, costes_activos, gestor, viajero):
        """Y se le dice que no, en vez de enseñarle todo a cero."""
        _viaje(gestor, 'Uno', 'P-1', 1000)

        assert report_service.gastos_por_proyecto(viajero) is None

    def test_con_los_costes_apagados_tampoco_un_gestor(self, app, seeded, gestor):
        settings_service.set_value('COSTES_HABILITADOS', False)
        _viaje(gestor, 'Uno', 'P-1', 1000)

        assert report_service.gastos_por_proyecto(gestor) is None

    def test_el_informe_completo_lo_respeta(self, costes_activos, gestor, viajero):
        _viaje(gestor, 'Uno', 'P-1', 1000)

        assert report_service.informe_completo(viajero)['gastos_por_proyecto'] is None
        assert report_service.informe_completo(gestor)['gastos_por_proyecto'] is not None


class TestLoQueSuma:
    def test_agrupa_por_proyecto(self, costes_activos, gestor):
        _viaje(gestor, 'Uno', 'P-1', 1000)
        _viaje(gestor, 'Dos', 'P-1', 500)
        _viaje(gestor, 'Tres', 'P-2', 300)

        gastos = report_service.gastos_por_proyecto(gestor)
        por_nombre = {p['nombre']: p['total'] for p in gastos['proyectos']}

        assert por_nombre == {'P-1': 1500.0, 'P-2': 300.0}

    def test_los_viajes_sin_proyecto_no_se_caen(self, costes_activos, gestor):
        """Un total que los deja fuera no suma lo que sumaría cualquier otro."""
        _viaje(gestor, 'Uno', 'P-1', 1000)
        _viaje(gestor, 'Dos', None, 400)

        gastos = report_service.gastos_por_proyecto(gestor)
        nombres = [p['nombre'] for p in gastos['proyectos']]

        assert report_service.SIN_PROYECTO in nombres
        assert gastos['total'] == 1400.0

    def test_sin_proyecto_va_el_ultimo(self, costes_activos, gestor):
        _viaje(gestor, 'Uno', None, 5000)
        _viaje(gestor, 'Dos', 'P-1', 10)

        gastos = report_service.gastos_por_proyecto(gestor)

        assert gastos['proyectos'][-1]['nombre'] == report_service.SIN_PROYECTO

    def test_se_reparte_por_mes(self, costes_activos, gestor):
        _viaje(gestor, 'Este mes', 'P-1', 100)
        _viaje(gestor, 'El anterior', 'P-1', 200, meses_atras=1)

        gastos = report_service.gastos_por_proyecto(gestor)
        fila = gastos['proyectos'][0]

        assert sum(fila['por_mes']) == 300.0
        assert len(fila['por_mes']) == len(gastos['meses'])

    def test_un_viaje_sin_coste_no_cuenta(self, costes_activos, gestor):
        trip_service.create_trip(gestor, titulo='Sin coste', proyecto='P-1')

        gastos = report_service.gastos_por_proyecto(gestor)

        assert gastos['proyectos'] == []

    def test_varias_monedas_se_avisan_en_vez_de_sumarse_a_ciegas(
        self, costes_activos, gestor,
    ):
        """Sumar euros con dólares da un número que no es nada."""
        _viaje(gestor, 'Uno', 'P-1', 1000, moneda='EUR')
        _viaje(gestor, 'Dos', 'P-2', 500, moneda='USD')

        gastos = report_service.gastos_por_proyecto(gestor)

        assert gastos['monedas_mezcladas'] is True
        assert gastos['moneda'] is None

    def test_con_una_sola_moneda_se_nombra(self, costes_activos, gestor):
        _viaje(gestor, 'Uno', 'P-1', 1000, moneda='EUR')

        assert report_service.gastos_por_proyecto(gestor)['moneda'] == 'EUR'

    def test_solo_los_viajes_que_uno_ve(self, costes_activos, gestor, ajeno):
        """Un agregado es una fuga como cualquier otra."""
        _viaje(gestor, 'Uno', 'P-1', 1000)

        assert report_service.gastos_por_proyecto(ajeno) is None


@pytest.mark.integration
class TestElPanelEnPantalla:
    def test_se_dibuja_para_un_gestor(self, costes_activos, as_user, gestor):
        _viaje(gestor, 'Uno', 'P-1', 1234)

        with as_user(gestor) as client:
            html = client.get('/dashboard/informes').get_data(as_text=True)

        assert 'Gasto por proyecto y mes' in html
        assert 'P-1' in html
        assert '1.234' in html

    def test_un_viajero_no_lo_ve(self, costes_activos, as_user, gestor, viajero):
        _viaje(gestor, 'Uno', 'P-1', 1234)

        with as_user(viajero) as client:
            html = client.get('/dashboard/informes').get_data(as_text=True)

        assert 'Gasto por proyecto y mes' not in html


class TestDeDondeSaleLoQueCuestaUnViaje:
    """El importe puede estar en cada elemento o en el viaje como gasto
    general, y el informe tiene que mirar los dos. Leyendo sólo
    «coste_estimado» se perdía todo viaje cuyos importes salieron de un
    documento, que es la mayoría: el hotel de VJ-2026-0004 traía 327,03 € y el
    viaje figuraba sin coste.
    """

    def _con_alojamiento(self, gestor, importe, proyecto='P-1'):
        from datetime import datetime

        from app.extensions import db
        from app.models.enums import TripStatus
        from app.models.itinerary import Accommodation
        from app.utils.timeutil import set_instant, utcnow

        ahora = utcnow()
        trip = trip_service.create_trip(
            gestor, titulo='Con factura', estado=TripStatus.CONFIRMADO,
            proyecto=proyecto,
            inicio_local=datetime(ahora.year, ahora.month, 10, 9, 0),
            inicio_tz='Europe/Madrid',
        )
        hotel = Accommodation(trip_id=trip.id, nombre='Hotel', ciudad='Londres',
                              pais='GB', importe=importe, moneda='EUR')
        set_instant(hotel, 'check_in', datetime(ahora.year, ahora.month, 10, 15, 0),
                    'Europe/London')
        db.session.add(hotel)
        db.session.commit()
        return trip

    def test_cuenta_lo_que_dicen_las_reservas(self, costes_activos, gestor):
        self._con_alojamiento(gestor, 327.03)

        gastos = report_service.gastos_por_proyecto(gestor)

        assert gastos['total'] == pytest.approx(327.03)

    def test_la_estimacion_solo_cuando_no_hay_factura(self, costes_activos, gestor):
        """Sumar las dos contaría el mismo viaje dos veces: una es el plan y
        la otra la factura."""
        from app.extensions import db

        trip = self._con_alojamiento(gestor, 327.03)
        trip.coste_estimado = 1000
        db.session.commit()

        gastos = report_service.gastos_por_proyecto(gestor)

        assert gastos['total'] == pytest.approx(327.03)

    def test_sin_importes_en_el_itinerario_vale_la_estimacion(
        self, costes_activos, gestor,
    ):
        _viaje(gestor, 'Solo estimado', 'P-9', 800)

        gastos = report_service.gastos_por_proyecto(gestor)

        assert gastos['total'] == 800.0
        assert gastos['estimados'] == 1

    def test_se_dice_cuantas_cifras_son_todavia_una_estimacion(
        self, costes_activos, gestor,
    ):
        """Para poder matizar el total en vez de presentar un plan como
        factura."""
        self._con_alojamiento(gestor, 100)
        _viaje(gestor, 'Solo estimado', 'P-9', 800)

        assert report_service.gastos_por_proyecto(gestor)['estimados'] == 1


class TestLaVentanaDeMeses:
    """Mirando solo hacia atrás, el informe salía vacío con los viajes de
    verdad: casi todo el gasto de viajes está comprometido antes de suceder, y
    una tabla que acaba en el mes en curso deja fuera lo que hay que
    presupuestar.
    """

    def _en_meses(self, gestor, desplazamiento, importe=500, proyecto='P-1'):
        from datetime import datetime

        from app.extensions import db
        from app.models.enums import TripStatus
        from app.utils.timeutil import utcnow

        ahora = utcnow()
        anio, mes = ahora.year, ahora.month + desplazamiento
        while mes > 12:
            mes -= 12
            anio += 1
        while mes <= 0:
            mes += 12
            anio -= 1

        trip = trip_service.create_trip(
            gestor, titulo=f'Mes {desplazamiento:+d}', estado=TripStatus.CONFIRMADO,
            proyecto=proyecto,
            inicio_local=datetime(anio, mes, 10, 9, 0), inicio_tz='Europe/Madrid',
        )
        trip.coste_estimado = importe
        trip.moneda = 'EUR'
        db.session.commit()
        return trip

    def test_entra_un_viaje_del_mes_que_viene(self, costes_activos, gestor):
        self._en_meses(gestor, +1)

        assert report_service.gastos_por_proyecto(gestor)['total'] == 500.0

    def test_y_el_del_mes_anterior(self, costes_activos, gestor):
        self._en_meses(gestor, -1)

        assert report_service.gastos_por_proyecto(gestor)['total'] == 500.0

    def test_queda_fuera_el_de_hace_dos_meses(self, costes_activos, gestor):
        """La ventana empieza en el mes anterior, no antes."""
        self._en_meses(gestor, -2)

        assert report_service.gastos_por_proyecto(gestor)['total'] == 0.0

    def test_entra_uno_de_dentro_de_cuatro_meses(self, costes_activos, gestor):
        self._en_meses(gestor, +4)

        assert report_service.gastos_por_proyecto(gestor)['total'] == 500.0

    def test_queda_fuera_lo_que_cae_lejos_por_delante(self, costes_activos, gestor):
        self._en_meses(gestor, +5)

        assert report_service.gastos_por_proyecto(gestor)['total'] == 0.0

    def test_y_lo_que_cae_lejos_por_detras(self, costes_activos, gestor):
        self._en_meses(gestor, -9)

        assert report_service.gastos_por_proyecto(gestor)['total'] == 0.0

    def test_las_columnas_cubren_la_ventana_entera(self, costes_activos, gestor):
        gastos = report_service.gastos_por_proyecto(gestor)

        assert len(gastos['meses']) == (
            report_service.MESES_ATRAS + 1 + report_service.MESES_ADELANTE
        )

    def test_el_cambio_de_año_no_rompe_la_cuenta(self, costes_activos, gestor):
        """Diciembre más dos son febrero, no el mes catorce."""
        from datetime import datetime

        desde = report_service._inicio_de_mes(datetime(2026, 12, 15), -2)

        assert (desde.year, desde.month) == (2027, 2)


class TestUnProyectoSinImportesTambienSale:
    """«PRJ-0002 no aparece» y «PRJ-0002 no tiene importes» no son lo mismo, y
    el primero se lee como un fallo del informe. Saltárselo en silencio obligó
    a preguntar qué pasaba.
    """

    def _sin_importe(self, gestor, proyecto):
        from datetime import datetime

        from app.models.enums import TripStatus
        from app.utils.timeutil import utcnow

        ahora = utcnow()
        return trip_service.create_trip(
            gestor, titulo='Sin cifra', estado=TripStatus.CONFIRMADO,
            proyecto=proyecto,
            inicio_local=datetime(ahora.year, ahora.month, 10, 9, 0),
            inicio_tz='Europe/Madrid',
        )

    def test_tiene_su_fila_a_cero(self, costes_activos, gestor):
        self._sin_importe(gestor, 'PRJ-0002')

        gastos = report_service.gastos_por_proyecto(gestor)
        nombres = [p['nombre'] for p in gastos['proyectos']]

        assert 'PRJ-0002' in nombres

    def test_y_dice_cuantos_viajes_suyos_no_llevan_cifra(self, costes_activos, gestor):
        self._sin_importe(gestor, 'PRJ-0002')
        self._sin_importe(gestor, 'PRJ-0002')

        fila = next(p for p in report_service.gastos_por_proyecto(gestor)['proyectos']
                    if p['nombre'] == 'PRJ-0002')

        assert fila['sin_importe'] == 2
        assert fila['total'] == 0.0

    def test_no_altera_el_total(self, costes_activos, gestor):
        """Una fila a cero no puede mover la suma."""
        _viaje(gestor, 'Con cifra', 'PRJ-0001', 500)
        self._sin_importe(gestor, 'PRJ-0002')

        gastos = report_service.gastos_por_proyecto(gestor)

        assert gastos['total'] == 500.0
        assert gastos['sin_importe'] == 1

    def test_la_ventana_empieza_en_el_mes_anterior(self, costes_activos, gestor):
        """Uno atrás para cerrar el mes que acaba de pasar, y el resto por
        delante, que es donde están los viajes que aún se pueden mover."""
        from app.utils.timeutil import utcnow

        gastos = report_service.gastos_por_proyecto(gestor)
        ahora = utcnow()
        anterior = report_service._inicio_de_mes(ahora, 1)

        assert len(gastos['meses']) == 6
        assert gastos['meses'][0] == f'{anterior:%Y-%m}'
        assert f'{ahora:%Y-%m}' in gastos['meses']
