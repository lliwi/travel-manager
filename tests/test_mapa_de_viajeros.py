"""Where the travellers are, on a map, on one day.

This is the duty-of-care report: «si pasa algo en Estambul esta noche, ¿tengo a
alguien allí?». Two things it has to get right, and both fail silently:

- **Nobody is dropped.** A traveller the itinerary cannot place is reported as
  unplaced. A map missing three people answers the question with a confidence
  it has not earned.
- **A traveller sees only themselves.** An aggregate is a leak like any other:
  «tres personas en Dubái» told to a colleague says where three named people
  are sleeping tonight.
"""
from datetime import date, datetime

import pytest

from app.services import map_service
from app.utils.timeutil import set_instant

pytestmark = pytest.mark.unit

DIA = date(2026, 6, 2)


def _alojamiento(trip, ciudad, pais, traveler=None,
                 entrada=datetime(2026, 6, 1, 15, 0),
                 salida=datetime(2026, 6, 4, 11, 0), tz='Europe/Berlin'):
    from app.extensions import db
    from app.models.itinerary import Accommodation

    fila = Accommodation(
        trip_id=trip.id,
        trip_traveler_id=traveler.id if traveler is not None else None,
        nombre=f'Hotel de {ciudad}', ciudad=ciudad, pais=pais,
    )
    set_instant(fila, 'check_in', entrada, tz)
    set_instant(fila, 'check_out', salida, tz)
    db.session.add(fila)
    db.session.commit()
    return fila


class TestLaProyeccion:
    def test_el_meridiano_cero_cae_en_el_centro(self):
        x, _ = map_service.proyectar(0, 0)

        assert x == pytest.approx(50.0)

    def test_madrid_cae_a_la_izquierda_del_centro_y_arriba(self):
        x, y = map_service.proyectar(40.42, -3.70)

        assert 48 < x < 50
        assert 25 < y < 35

    def test_sidney_cae_a_la_derecha_y_abajo(self):
        x, y = map_service.proyectar(-33.87, 151.21)

        assert x > 90
        assert y > 75

    def test_el_recorte_coincide_con_el_del_dibujo(self):
        """If the two crops disagree, every marker lands somewhere else."""
        from pathlib import Path

        svg = (Path(__file__).resolve().parent.parent
               / 'app' / 'static' / 'img' / 'mapa-mundi.svg').read_text()
        alto = 1000 * (map_service.LAT_NORTE - map_service.LAT_SUR) / 360

        assert f'viewBox="0 0 1000 {alto:.1f}"' in svg


class TestElCatalogoDeCiudades:
    def test_se_reconoce_con_acentos_y_sin_ellos(self):
        assert map_service.coordenadas_de('Zúrich', 'CH') is not None
        assert map_service.coordenadas_de('zurich', 'ch') is not None

    def test_una_ciudad_desconocida_no_se_coloca_a_ojo(self):
        """A marker in the wrong place is read as confidently as a right one."""
        assert map_service.coordenadas_de('Cuenca', 'ES') is None

    def test_el_pais_forma_parte_de_la_clave(self):
        """«Santiago» is Chile here and Galicia there."""
        assert map_service.coordenadas_de('Santiago', 'CL') != \
            map_service.coordenadas_de('Santiago de Compostela', 'ES')

    def test_estan_todas_las_ciudades_del_catalogo(self, app, seeded):
        """A seeded city with no coordinates is a traveller who cannot be put
        on the map for a reason nobody will think to look for."""
        from app.extensions import db
        from app.models.catalog import Location

        faltan = {
            f'{ciudad} ({pais})'
            for ciudad, pais in db.session.query(
                Location.ciudad, Location.pais_codigo).distinct()
            if ciudad and map_service.coordenadas_de(ciudad, pais) is None
        }

        assert not faltan, f'Sin coordenadas: {sorted(faltan)}'


class TestDondeEstaCadaUno:
    def test_el_alojamiento_manda(self, app, seeded, gestor, trip, traveler_row):
        _alojamiento(trip, 'Berlín', 'DE', traveler_row)

        mapa = map_service.donde_estan(gestor, DIA)

        assert [c['ciudad'] for c in mapa['ciudades']] == ['Berlín']
        assert mapa['ciudades'][0]['total'] == 1

    def test_el_codigo_del_tramo_basta_sin_nombre_de_ciudad(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        """Lo normal en un tramo extraído: la reserva da MUC y el nombre de la
        ciudad depende del idioma del PDF, cuando viene. El catálogo sabe que
        MUC es Múnich, así que se le pregunta a él y no al texto."""
        segment_factory(destino='MUC', destino_pais='DE', traveler=traveler_row,
                        salida=datetime(2026, 6, 1, 9, 0),
                        llegada=datetime(2026, 6, 1, 11, 30))

        mapa = map_service.donde_estan(gestor, DIA)

        assert [c['ciudad'] for c in mapa['ciudades']] == ['Múnich']

    def test_un_tramo_con_ciudad_gana_al_destino_declarado(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        segment_factory(destino='MUC', destino_pais='DE', traveler=traveler_row,
                        destino_ciudad='Múnich',
                        salida=datetime(2026, 6, 1, 9, 0),
                        llegada=datetime(2026, 6, 1, 11, 30))

        mapa = map_service.donde_estan(gestor, DIA)

        assert [c['ciudad'] for c in mapa['ciudades']] == ['Múnich']

    def test_un_alojamiento_gana_a_un_tramo_posterior_en_el_dia(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        """Sleeping somewhere beats having landed somewhere."""
        segment_factory(destino='MUC', destino_pais='DE', traveler=traveler_row,
                        destino_ciudad='Múnich',
                        salida=datetime(2026, 6, 1, 9, 0),
                        llegada=datetime(2026, 6, 1, 11, 30))
        _alojamiento(trip, 'Berlín', 'DE', traveler_row)

        mapa = map_service.donde_estan(gestor, DIA)

        assert [c['ciudad'] for c in mapa['ciudades']] == ['Berlín']

    def test_un_tramo_que_llega_despues_no_cuenta_todavia(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        segment_factory(destino='MUC', destino_pais='DE', traveler=traveler_row,
                        destino_ciudad='Múnich',
                        salida=datetime(2026, 6, 3, 9, 0),
                        llegada=datetime(2026, 6, 3, 11, 30))

        mapa = map_service.donde_estan(gestor, DIA)

        assert [c['ciudad'] for c in mapa['ciudades']] == ['Berlín']

    def test_un_dia_sin_nadie_de_viaje_sale_vacio(self, app, seeded, gestor, trip):
        mapa = map_service.donde_estan(gestor, date(2026, 12, 25))

        assert mapa['total'] == 0
        assert mapa['ciudades'] == []


class TestNadieSeCaeDelMapa:
    def test_una_ciudad_que_no_se_sabe_situar_se_declara(
        self, app, seeded, gestor, trip, traveler_row,
    ):
        _alojamiento(trip, 'Cuenca', 'ES', traveler_row)

        mapa = map_service.donde_estan(gestor, DIA)

        assert mapa['ciudades'] == []
        assert [p['ciudad'] for p in mapa['sin_situar']] == ['Cuenca']
        assert mapa['total'] == 1

    def test_la_suma_cuadra_siempre(self, app, seeded, gestor, trip, traveler_row):
        """Placed plus unplaceable plus unlocated is everybody, or somebody
        vanished between the query and the screen."""
        _alojamiento(trip, 'Cuenca', 'ES', traveler_row)

        mapa = map_service.donde_estan(gestor, DIA)
        situados = sum(c['total'] for c in mapa['ciudades'])

        assert situados + len(mapa['sin_situar']) + len(mapa['sin_ubicacion']) \
            == mapa['total']


class TestUnViajeroNoVeDondeDuermenLosDemas:
    def test_el_gestor_ve_a_todos(self, app, seeded, gestor, trip, traveler_row):
        _alojamiento(trip, 'Berlín', 'DE')

        mapa = map_service.donde_estan(gestor, DIA)

        assert mapa['total'] == 1

    def test_el_viajero_solo_se_ve_a_si_mismo(
        self, app, seeded, gestor, viajero, otro_viajero, trip, traveler_row,
    ):
        from app.services import trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        _alojamiento(trip, 'Berlín', 'DE')

        mapa = map_service.donde_estan(viajero, DIA)

        assert mapa['total'] == 1
        assert sum(c['total'] for c in mapa['ciudades']) == 1
        nombres = [p['nombre'] for c in mapa['ciudades'] for p in c['personas']]
        assert nombres == [viajero.nombre_completo]

    def test_quien_no_esta_en_el_viaje_no_ve_nada(
        self, app, seeded, ajeno, trip, traveler_row,
    ):
        _alojamiento(trip, 'Berlín', 'DE', traveler_row)

        assert map_service.donde_estan(ajeno, DIA)['total'] == 0


class TestLaFechaDeLaPantalla:
    def test_se_lee_del_parametro(self):
        assert map_service.fecha_pedida('2026-06-02') == DIA

    def test_una_fecha_ilegible_no_rompe_el_informe(self):
        """The parameter is a convenience; refusing to draw over a typo helps
        nobody."""
        from app.utils.timeutil import utcnow

        assert map_service.fecha_pedida('el martes') == utcnow().date()
        assert map_service.fecha_pedida(None) == utcnow().date()


@pytest.mark.integration
class TestElPanelEnLaPaginaDeInformes:
    def test_se_dibuja_con_su_leyenda(self, as_user, gestor, trip, traveler_row):
        _alojamiento(trip, 'Berlín', 'DE', traveler_row)

        with as_user(gestor) as client:
            html = client.get('/dashboard/informes?fecha=2026-06-02').get_data(as_text=True)

        assert 'Dónde está la gente' in html
        assert 'mapa-mundi.svg' in html
        # La leyenda es la versión que se lee sin ratón y sin color.
        assert 'Berlín' in html

    def test_el_fondo_es_nuestro_y_no_de_un_tercero(self, as_user, gestor, trip,
                                                    traveler_row):
        """External tiles would blank this panel on a server with no way out,
        and would tell somebody else which cities are being looked at."""
        _alojamiento(trip, 'Berlín', 'DE', traveler_row)

        with as_user(gestor) as client:
            html = client.get(
                '/dashboard/informes?fecha=2026-06-02').get_data(as_text=True)

        assert 'mapa-mundi.svg' in html, 'no se ha renderizado el panel'
        assert 'openstreetmap' not in html.lower()
        assert 'tile' not in html.lower()

    def test_una_fecha_sin_viajes_no_rompe_la_pagina(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            respuesta = client.get('/dashboard/informes?fecha=2030-01-01')

        assert respuesta.status_code == 200
        assert 'Nadie de viaje' in respuesta.get_data(as_text=True)

    def test_una_fecha_inventada_tampoco(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            respuesta = client.get('/dashboard/informes?fecha=no-es-una-fecha')

        assert respuesta.status_code == 200


@pytest.mark.security
class TestQuienPuedeAbrirLosInformes:
    """The page answered 403 to everybody but an administrator, and no test
    caught it: the only one that opened the URL did so without logging in, so
    it asserted a redirect and passed for the wrong reason."""

    def test_el_gestor_entra(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            assert client.get('/dashboard/informes').status_code == 200

    def test_el_viajero_entra(self, as_user, viajero, seeded):
        with as_user(viajero) as client:
            assert client.get('/dashboard/informes').status_code == 200

    def test_el_administrador_entra(self, as_user, admin, seeded):
        with as_user(admin) as client:
            assert client.get('/dashboard/informes').status_code == 200

    def test_sin_sesion_no(self, client):
        assert client.get('/dashboard/informes').status_code in (302, 401)


@pytest.mark.unit
class TestElNombreQueTraeLaReserva:
    """«London» no es «Londres» y el catálogo es quien sabe que sí lo es.

    Los casos de aquí salen todos de datos reales: un alojamiento extraído de
    una reserva en inglés, un tramo con el código de ciudad y sin país, y un
    código mal escrito que ninguna capa rechazó.
    """

    def test_el_nombre_en_ingles_se_reconoce(self, app, seeded):
        ciudad, pais = map_service.ciudad_del_catalogo(nombres=('London',), pais='GB')

        assert (ciudad, pais) == ('Londres', 'GB')
        assert map_service.coordenadas_de(ciudad, pais) is not None

    @pytest.mark.parametrize('extranjero, esperado', [
        ('Lisbon', 'Lisboa'), ('Seville', 'Sevilla'), ('Vienna', 'Viena'),
        ('Copenhagen', 'Copenhague'), ('Prague', 'Praga'), ('Warsaw', 'Varsovia'),
        ('Munich', 'Múnich'), ('Mumbai', 'Bombay'), ('Athens', 'Atenas'),
    ])
    def test_los_exonimos_habituales_tambien(self, app, seeded, extranjero, esperado):
        """Antes acertar o no era casualidad ortográfica: «Munich» colaba
        porque al quitar acentos «Múnich» queda igual, y «Lisbon» no."""
        ciudad, _ = map_service.ciudad_del_catalogo(nombres=(extranjero,))

        assert ciudad == esperado

    def test_el_codigo_de_ciudad_con_varios_aeropuertos_se_reconoce(self, app, seeded):
        """Una reserva «a Londres» y no a una terminal llega como LON, que no
        estaba en el catálogo: había cuatro aeropuertos de Londres y no Londres."""
        ciudad, pais = map_service.ciudad_del_catalogo(codigo='LON')

        assert (ciudad, pais) == ('Londres', 'GB')

    def test_el_codigo_manda_sobre_el_texto(self, app, seeded):
        """El código es la mitad fiable de un tramo extraído; el nombre
        depende del idioma del documento, cuando viene."""
        ciudad, _ = map_service.ciudad_del_catalogo(
            codigo='BCN', nombres=('Lo que pusiera el PDF',),
        )

        assert ciudad == 'Barcelona'

    def test_un_codigo_inexistente_cae_al_nombre(self, app, seeded):
        """Visto en datos reales: «BNC» por «BCN». El código no existe y nada
        se quejó, pero el nombre todavía salva la fila."""
        ciudad, _ = map_service.ciudad_del_catalogo(
            codigo='BNC', nombres=('Barcelona',),
        )

        assert ciudad == 'Barcelona'

    def test_sin_pais_se_situa_si_el_nombre_es_inequivoco(self, app, seeded):
        """Un tramo extraído trae ciudad y no país con toda normalidad."""
        assert map_service.coordenadas_de('Londres', None) is not None

    def test_lo_que_el_catalogo_no_conoce_se_devuelve_tal_cual(self, app, seeded):
        """Para poder decir en pantalla el nombre que venía en el documento."""
        ciudad, pais = map_service.ciudad_del_catalogo(
            codigo='ZZZ', nombres=('Cuenca',), pais='ES',
        )

        assert (ciudad, pais) == ('Cuenca', 'ES')


@pytest.mark.unit
class TestQuienVaDeCamino:
    def test_sin_alojamiento_cuenta_en_la_ciudad_a_la_que_vuela(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        """Salió ayer y llega mañana. Sin alojamiento, la pregunta «quién
        tengo en Londres» lo quiere dentro: dejarlo fuera respondía con un
        hueco cuando el vuelo dice a dónde va."""
        from app.models.trip import TripDestination

        TripDestination.query.delete()
        segment_factory(destino='LHR', destino_pais='GB', traveler=traveler_row,
                        salida=datetime(2026, 6, 1, 9, 0),
                        llegada=datetime(2026, 6, 3, 11, 30),
                        llegada_tz='Europe/London')

        mapa = map_service.donde_estan(gestor, DIA)

        assert [c['ciudad'] for c in mapa['ciudades']] == ['Londres']
        assert mapa['sin_ubicacion'] == []

    def test_pero_sigue_constando_que_no_habia_llegado(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        """Contarlo en Londres sin decirlo sería afirmar que ya está allí."""
        from app.models.trip import TripDestination

        TripDestination.query.delete()
        segment_factory(destino='LHR', destino_pais='GB', traveler=traveler_row,
                        salida=datetime(2026, 6, 1, 9, 0),
                        llegada=datetime(2026, 6, 3, 11, 30),
                        llegada_tz='Europe/London')

        mapa = map_service.donde_estan(gestor, DIA)
        persona = mapa['ciudades'][0]['personas'][0]

        assert persona['segun'] == 'en_transito'
        assert persona['llega'] is not None
        assert len(mapa['en_transito']) == 1

    def test_un_alojamiento_le_gana_al_vuelo_en_curso(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        """Dormir en un sitio es mejor prueba que ir camino de otro."""
        segment_factory(destino='LHR', destino_pais='GB', traveler=traveler_row,
                        salida=datetime(2026, 6, 1, 9, 0),
                        llegada=datetime(2026, 6, 3, 11, 30),
                        llegada_tz='Europe/London')
        _alojamiento(trip, 'Berlín', 'DE', traveler_row)

        mapa = map_service.donde_estan(gestor, DIA)

        assert [c['ciudad'] for c in mapa['ciudades']] == ['Berlín']
        assert mapa['en_transito'] == []

    def test_quien_ya_aterrizo_no_consta_volando(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        segment_factory(destino='LHR', destino_pais='GB', traveler=traveler_row,
                        salida=datetime(2026, 6, 1, 9, 0),
                        llegada=datetime(2026, 6, 1, 11, 30),
                        llegada_tz='Europe/London')

        mapa = map_service.donde_estan(gestor, DIA)

        assert mapa['en_transito'] == []
        assert [c['ciudad'] for c in mapa['ciudades']] == ['Londres']

    def test_la_suma_sigue_cuadrando_con_alguien_de_camino(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        from app.models.trip import TripDestination

        TripDestination.query.delete()
        segment_factory(destino='LHR', destino_pais='GB', traveler=traveler_row,
                        salida=datetime(2026, 6, 1, 9, 0),
                        llegada=datetime(2026, 6, 3, 11, 30),
                        llegada_tz='Europe/London')

        mapa = map_service.donde_estan(gestor, DIA)
        situados = sum(c['total'] for c in mapa['ciudades'])

        # «en_transito» ya no es un grupo aparte sino una marca sobre gente que
        # cuenta en su ciudad: sumarlo contaría a la misma persona dos veces.
        assert situados + len(mapa['sin_situar']) + len(mapa['sin_ubicacion']) \
            == mapa['total']


@pytest.mark.unit
class TestDeDondeSaleElProximoVuelo:
    def test_un_viaje_con_solo_la_vuelta_ya_situa(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        """El itinerario más común que hay: solo el vuelo grabado. Quien
        despega de Barcelona el día 3 estaba en Barcelona el día 2."""
        from app.models.trip import TripDestination

        TripDestination.query.delete()
        segment_factory(origen='BCN', origen_pais='ES', destino='LHR',
                        destino_pais='GB', traveler=traveler_row,
                        salida=datetime(2026, 6, 3, 9, 0),
                        llegada=datetime(2026, 6, 3, 11, 30),
                        llegada_tz='Europe/London')

        mapa = map_service.donde_estan(gestor, DIA)

        assert [c['ciudad'] for c in mapa['ciudades']] == ['Barcelona']
        assert mapa['ciudades'][0]['personas'][0]['segun'] == 'proximo_tramo'

    def test_pero_no_le_gana_al_destino_declarado(
        self, app, seeded, gestor, trip, traveler_row, segment_factory,
    ):
        """«Mañana despegas de Madrid» dice menos sobre hoy que «el itinerario
        te pone hoy en Berlín»."""
        segment_factory(origen='MAD', origen_pais='ES', traveler=traveler_row,
                        salida=datetime(2026, 6, 3, 9, 0),
                        llegada=datetime(2026, 6, 3, 11, 30))

        mapa = map_service.donde_estan(gestor, DIA)

        assert [c['ciudad'] for c in mapa['ciudades']] == ['Berlín']


@pytest.mark.unit
class TestElCatalogoAprendeAlResembrar:
    def test_los_alias_se_refrescan_en_una_fila_que_ya_existia(self, app, seeded):
        """Llegaron después que el catálogo: una instalación que ya había
        sembrado no los tendría nunca y seguiría sin reconocer «London»."""
        from app.extensions import db
        from app.models.catalog import Location
        from app.services import seed_service

        fila = Location.query.filter_by(codigo='LGW').first()
        fila.alias = None
        db.session.commit()

        seed_service.seed_catalogs()

        assert 'London' in (Location.query.filter_by(codigo='LGW').first().alias or ())


@pytest.mark.unit
class TestElCalendarioDeDiasConGente:
    def test_marca_los_dias_del_viaje(self, app, seeded, gestor, trip, traveler_row):
        """El viaje va del 1 al 4 de junio."""
        cuenta = map_service.dias_con_viajeros(gestor, date(2026, 6, 1))

        assert set(cuenta) == {date(2026, 6, d) for d in (1, 2, 3, 4)}
        assert set(cuenta.values()) == {1}

    def test_un_mes_sin_viajes_sale_vacio(self, app, seeded, gestor, trip):
        assert map_service.dias_con_viajeros(gestor, date(2026, 12, 1)) == {}

    def test_dos_personas_el_mismo_dia_suman(
        self, app, seeded, gestor, viajero, otro_viajero, trip, traveler_row,
    ):
        from app.services import trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        cuenta = map_service.dias_con_viajeros(gestor, date(2026, 6, 1))

        assert cuenta[date(2026, 6, 2)] == 2

    def test_cuenta_lo_mismo_que_el_mapa_de_ese_dia(
        self, app, seeded, gestor, trip, traveler_row,
    ):
        """Dos formas de preguntar «quién está de viaje» que discrepen es un
        calendario que marca un día que el mapa dice vacío."""
        cuenta = map_service.dias_con_viajeros(gestor, date(2026, 6, 1))

        for dia, total in cuenta.items():
            assert map_service.donde_estan(gestor, dia)['total'] == total

    def test_un_viajero_solo_se_cuenta_a_si_mismo(
        self, app, seeded, gestor, viajero, otro_viajero, trip, traveler_row,
    ):
        from app.services import trip_service

        trip_service.add_traveler(gestor, trip, otro_viajero)
        cuenta = map_service.dias_con_viajeros(viajero, date(2026, 6, 1))

        assert cuenta[date(2026, 6, 2)] == 1

    def test_la_rejilla_trae_semanas_enteras(self, app, seeded, gestor, trip):
        """Un hueco en la esquina de un calendario se lee como un fallo."""
        rejilla = map_service.calendario_del_mes(gestor, date(2026, 6, 15))

        assert all(len(semana) == 7 for semana in rejilla['semanas'])
        assert rejilla['semanas'][0][0]['fecha'].weekday() == 0

    def test_se_distingue_el_dia_que_el_mapa_esta_mostrando(
        self, app, seeded, gestor, trip,
    ):
        rejilla = map_service.calendario_del_mes(gestor, date(2026, 6, 15))
        elegidos = [c for semana in rejilla['semanas'] for c in semana
                    if c['es_el_elegido']]

        assert [c['fecha'] for c in elegidos] == [date(2026, 6, 15)]

    def test_los_meses_vecinos_se_pueden_recorrer(self, app, seeded, gestor, trip):
        rejilla = map_service.calendario_del_mes(gestor, date(2026, 1, 15))

        assert rejilla['anterior'] == date(2025, 12, 1)
        assert rejilla['siguiente'] == date(2026, 2, 1)


@pytest.mark.integration
class TestElCalendarioEnLaPantalla:
    def test_se_dibuja_y_cada_dia_enlaza_a_su_mapa(
        self, as_user, gestor, trip, traveler_row,
    ):
        with as_user(gestor) as client:
            html = client.get(
                '/dashboard/informes?fecha=2026-06-02').get_data(as_text=True)

        assert 'calendario-rejilla' in html
        assert 'fecha=2026-06-03' in html
        assert 'con-gente' in html

    def test_sigue_estando_un_dia_sin_nadie(self, as_user, gestor, trip, traveler_row):
        """Es justo cuando más falta hace: dice qué días sí tienen gente."""
        with as_user(gestor) as client:
            html = client.get(
                '/dashboard/informes?fecha=2026-06-20').get_data(as_text=True)

        assert 'Nadie de viaje' in html
        assert 'calendario-rejilla' in html

    def test_el_titulo_no_capitaliza_la_preposicion(self, app, seeded, gestor):
        """«text-capitalize» la ponía en todas las palabras: «Junio De 2026»."""
        assert map_service.calendario_del_mes(gestor, date(2026, 6, 15))['titulo'] \
            == 'Junio de 2026'

    def test_el_panel_queda_abierto_al_cambiar_de_mes(self, as_user, gestor, trip):
        """Cambiar de mes recarga la página; sin esto habría que volver a
        abrirlo en cada clic para seguir buscando."""
        with as_user(gestor) as client:
            html = client.get(
                '/dashboard/informes?fecha=2026-06-01&cal=1').get_data(as_text=True)

        assert 'calendario-desplegable' in html
        assert 'open' in html[html.index('calendario-desplegable'):
                              html.index('calendario-desplegable') + 120]

    def test_sin_el_parametro_sale_cerrado(self, as_user, gestor, trip):
        with as_user(gestor) as client:
            html = client.get(
                '/dashboard/informes?fecha=2026-06-01').get_data(as_text=True)

        trozo = html[html.index('calendario-desplegable'):
                     html.index('calendario-desplegable') + 120]

        assert 'open' not in trozo

    def test_ya_no_hay_selector_nativo_de_fecha(self, as_user, gestor, trip):
        """Lo sustituye el desplegable, que además dice qué días tienen gente."""
        with as_user(gestor) as client:
            html = client.get('/dashboard/informes').get_data(as_text=True)

        assert 'type="date"' not in html
        assert 'calendario-rejilla' in html
