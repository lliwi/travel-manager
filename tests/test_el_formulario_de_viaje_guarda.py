"""Lo que el formulario de viaje ofrece, la ruta lo tiene que guardar.

El fallo concreto: «Coste estimado» y «Moneda» estaban en el formulario y
«update_trip» los aceptaba, pero la ruta armaba su diccionario de campos a mano
y no los incluía. Escribías una cifra, guardabas, y el campo volvía vacío sin
ningún error.

No se notó durante meses porque el bloque de costes está tras un interruptor
que sale apagado: el campo no se dibujaba, así que nadie podía escribir nada
que dejara de guardarse.

Por eso esto no comprueba un campo sino la clase entera: cada campo del
formulario que se corresponda con una columna del viaje se escribe y se vuelve
a leer. Un campo nuevo que la ruta olvide pasar falla aquí.
"""
import pytest

from app.models.enums import TripStatus
from app.models.trip import Trip
from app.services import settings_service, trip_service

pytestmark = pytest.mark.integration

#: Lo que no se comprueba así, y por qué.
#:
#: Los instantes van como (local, zona) y los escribe «set_instant»; el gestor
#: es un identificador que depende de qué usuarios existan; los documentos son
#: ficheros; y el título es obligatorio y ya se cubre en otro sitio.
APARTE = {
    'titulo', 'gestor_id', 'documentos', 'submit', 'csrf_token',
    'inicio_local', 'inicio_tz', 'fin_local', 'fin_tz',
    'estado', 'finalidad',
    # No es una columna: es el porqué de un cambio de estado, y va a la
    # auditoría con él. Sus reglas están en «test_estados_de_viaje».
    'motivo_estado',
}

#: Un valor plausible para cada campo simple del formulario.
VALORES = {
    'finalidad_detalle': 'Reunión con el cliente',
    'observaciones': 'Llevar el portátil de repuesto',
    'proyecto': 'PRJ-0002',
    # Una casilla: que se guarda marcada y desmarcada lo prueba «TestFacturable».
    'facturable': 'y',
    'coste_estimado': '1234.50',
    'moneda': 'EUR',
}


@pytest.fixture
def viaje(gestor, seeded):
    settings_service.set_value('COSTES_HABILITADOS', True)
    return trip_service.create_trip(
        gestor, titulo='Para editar', estado=TripStatus.CONFIRMADO,
    )


def _campos_del_formulario():
    from app.blueprints.trips.forms import TripForm

    return {
        nombre for nombre in TripForm()._fields
        if nombre not in APARTE
    }


class TestTodoLoQueSePuedeEscribirSeGuarda:
    def test_no_falta_ningun_campo_por_comprobar(self, app):
        """Si mañana el formulario gana un campo, este test avisa de que nadie
        ha comprobado que se guarde."""
        with app.test_request_context():
            sin_valor = _campos_del_formulario() - set(VALORES)

        assert not sin_valor, (
            f'Campos del formulario sin comprobar aquí: {sorted(sin_valor)}. '
            f'Añádalos a VALORES o justifíquelos en APARTE.'
        )

    def test_se_guardan_al_editar(self, as_user, gestor, viaje):
        from app.extensions import db

        with as_user(gestor) as client:
            client.post(f'/trips/{viaje.id}/editar', data={
                'csrf_token': 'x',
                'titulo': 'Para editar',
                'estado': str(TripStatus.CONFIRMADO),
                **VALORES,
            }, follow_redirects=True)

        guardado = db.session.get(Trip, viaje.id)
        perdidos = {
            campo for campo in VALORES
            if getattr(guardado, campo) in (None, '')
        }

        assert not perdidos, f'Se escribieron y no se guardaron: {sorted(perdidos)}'

    def test_el_coste_estimado_llega_con_su_valor(self, as_user, gestor, viaje):
        from app.extensions import db

        with as_user(gestor) as client:
            client.post(f'/trips/{viaje.id}/editar', data={
                'csrf_token': 'x', 'titulo': 'Para editar',
                'estado': str(TripStatus.CONFIRMADO), **VALORES,
            }, follow_redirects=True)

        guardado = db.session.get(Trip, viaje.id)

        assert float(guardado.coste_estimado) == pytest.approx(1234.50)
        assert guardado.moneda == 'EUR'

    def test_y_tambien_al_crear(self, as_user, gestor, seeded):
        settings_service.set_value('COSTES_HABILITADOS', True)

        with as_user(gestor) as client:
            client.post('/trips/nuevo', data={
                'csrf_token': 'x', 'titulo': 'Recién creado',
                'estado': str(TripStatus.BORRADOR), **VALORES,
            }, follow_redirects=True)

        creado = Trip.query.filter_by(titulo='Recién creado').first()

        assert creado is not None
        assert float(creado.coste_estimado) == pytest.approx(1234.50)
        assert creado.proyecto == 'PRJ-0002'

    def test_vaciar_un_importe_lo_borra(self, as_user, gestor, viaje):
        """Poner una cifra por error y no poder quitarla es peor que no
        poder ponerla."""
        from app.extensions import db

        viaje.coste_estimado = 999
        db.session.commit()

        with as_user(gestor) as client:
            client.post(f'/trips/{viaje.id}/editar', data={
                'csrf_token': 'x', 'titulo': 'Para editar', 'proyecto': 'PRJ-0002',
                'estado': str(TripStatus.CONFIRMADO), 'coste_estimado': '',
            }, follow_redirects=True)

        assert db.session.get(Trip, viaje.id).coste_estimado is None


class TestElProyectoEsObligatorio:
    """Whoever opens a trip says what it is charged to, at every door."""

    def test_el_formulario_no_crea_sin_proyecto(self, as_user, gestor):
        with as_user(gestor) as client:
            html = client.post('/trips/nuevo', data={
                'csrf_token': 'x', 'titulo': 'Sin imputar', 'estado': 'borrador',
            }).get_data(as_text=True)

        assert 'Indique el proyecto' in html
        assert not Trip.query.filter_by(titulo='Sin imputar').count()

    def test_el_planificador_tampoco(self, as_user, gestor):
        import json

        vuelo = {'tramos': [{'origen': 'BCN', 'destino': 'LHR',
                             'salida': '2026-10-15 07:10',
                             'llegada': '2026-10-15 08:40'}]}
        with as_user(gestor) as client:
            html = client.post('/trips/planificar/crear', data={
                'csrf_token': 'x', 'titulo': 'Sin imputar',
                'vuelo': json.dumps(vuelo),
            }, follow_redirects=True).get_data(as_text=True)

        assert 'Indique el proyecto' in html
        assert not Trip.query.filter_by(titulo='Sin imputar').count()

    def test_el_planificador_guarda_proyecto_y_notas(self, as_user, gestor):
        import json

        vuelo = {'tramos': [{'origen': 'BCN', 'destino': 'LHR',
                             'salida': '2026-10-15 07:10',
                             'llegada': '2026-10-15 08:40'}]}
        with as_user(gestor) as client:
            client.post('/trips/planificar/crear', data={
                'csrf_token': 'x', 'titulo': 'Con notas', 'proyecto': 'PRJ-0007',
                'observaciones': 'Vuelvo el viernes si hay sitio.',
                'vuelo': json.dumps(vuelo),
            })

        viaje = Trip.query.filter_by(titulo='Con notas').one()
        assert viaje.proyecto == 'PRJ-0007'
        assert viaje.observaciones == 'Vuelvo el viernes si hay sitio.'

    def test_la_api_tampoco(self, client, api_login, gestor):
        api_login(gestor)

        sin = client.post('/api/v1/trips', json={'titulo': 'Por la API'})
        con = client.post('/api/v1/trips',
                          json={'titulo': 'Por la API', 'proyecto': 'PRJ-0007'})

        assert sin.status_code == 422
        assert con.status_code == 201
        assert con.get_json()['data']['proyecto'] == 'PRJ-0007'


class TestFacturable:
    def test_se_marca_al_crear_y_se_desmarca_al_editar(self, as_user, gestor):
        from app.extensions import db

        with as_user(gestor) as client:
            html = client.get('/trips/nuevo').get_data(as_text=True)
            client.post('/trips/nuevo', data={
                'csrf_token': 'x', 'titulo': 'Para el cliente', 'estado': 'borrador',
                'proyecto': 'PRJ-0002', 'facturable': 'y',
            })
            viaje = Trip.query.filter_by(titulo='Para el cliente').one()
            assert viaje.facturable

            client.post(f'/trips/{viaje.id}/editar', data={
                'csrf_token': 'x', 'titulo': 'Para el cliente',
                'estado': 'borrador', 'proyecto': 'PRJ-0002',
            })

        assert 'name="facturable"' in html
        db.session.refresh(viaje)
        assert not viaje.facturable

    def test_por_defecto_no_lo_es(self, gestor):
        from app.services import trip_service

        assert trip_service.create_trip(gestor, titulo='Interno').facturable is False

    def test_por_la_api(self, client, api_login, gestor):
        api_login(gestor)

        creado = client.post('/api/v1/trips', json={
            'titulo': 'Por la API', 'proyecto': 'PRJ-0007', 'facturable': True,
        }).get_json()['data']

        assert creado['facturable'] is True
