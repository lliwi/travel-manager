"""The API's description must describe the API.

A hand-written contract drifts the day someone adds an endpoint and forgets the
YAML. This one is generated from the URL map, so paths, methods, parameters and
the permission each endpoint requires cannot disagree with the code. What can
still drift is the part a machine cannot infer -- what a request body means, and
what a resource looks like -- so those are what these tests pin.
"""
import pytest

from app.extensions import db
from app.services.openapi_service import OPERACIONES, build_spec

METODOS = ('get', 'post', 'patch', 'put', 'delete')


@pytest.fixture
def spec(app):
    return build_spec(app)


def _rutas_reales(app):
    return {
        str(regla) for regla in app.url_map.iter_rules()
        if str(regla).startswith('/api/v1')
    }


@pytest.mark.unit
class TestCoberturaDeLaApi:
    def test_el_documento_es_valido(self, spec):
        from openapi_spec_validator import validate

        validate(spec)

    def test_toda_ruta_de_la_api_esta_descrita(self, app, spec):
        from app.services.openapi_service import _ruta_openapi

        descritas = set(spec['paths'])
        faltan = {
            str(regla) for regla in app.url_map.iter_rules()
            if str(regla).startswith('/api/v1')
            and _ruta_openapi(regla) not in descritas
        }

        assert not faltan, f'Sin describir: {sorted(faltan)}'

    def test_las_rutas_no_repiten_el_prefijo_del_servidor(self, spec):
        """The server URL already carries /api/v1.

        Repeating it in every path makes a generated client request
        /api/v1/api/v1/trips, which fails against the running application while
        the document still validates.
        """
        assert spec['servers'][0]['url'] == '/api/v1'
        assert not [r for r in spec['paths'] if r.startswith('/api/v1')]

    def test_todo_endpoint_declara_su_cuerpo(self, app):
        """Every endpoint gets a deliberate entry, even an empty one.

        A body cannot be inferred from the code, so the table is where it is
        said. Requiring an entry per endpoint turns "nobody documented it" into
        a failing test instead of a silent gap in the contract.
        """
        sin_declarar = set()
        for regla in app.url_map.iter_rules():
            if not str(regla).startswith('/api/v1'):
                continue
            nombre = regla.endpoint.rsplit('.', 1)[-1]
            if nombre not in OPERACIONES:
                sin_declarar.add(nombre)

        assert not sin_declarar, (
            f'Endpoints sin entrada en OPERACIONES: {sorted(sin_declarar)}'
        )

    def test_cada_operacion_dice_que_permiso_exige(self, app, spec):
        """The contract states the guard, read from the guard itself."""
        sin_permiso = []
        for ruta, entrada in spec['paths'].items():
            for metodo, operacion in entrada.items():
                if metodo not in METODOS:
                    continue
                publico = operacion.get('security') == []
                marcado = 'x-permiso' in operacion
                if not publico and not marcado:
                    sin_permiso.append(f'{metodo.upper()} {ruta}')

        assert not sin_permiso, f'Sin permiso declarado: {sin_permiso}'

    def test_los_errores_estan_en_el_contrato(self, spec):
        operacion = spec['paths']['/trips/{trip_id}']['get']

        assert '404' in operacion['responses'], (
            'Un recurso ajeno responde 404, y el contrato debe decirlo.'
        )
        assert '403' in operacion['responses']
        assert '401' in operacion['responses']

    def test_las_referencias_apuntan_a_algo(self, spec):
        """A $ref to a schema that does not exist is a broken contract."""
        import json
        import re

        definidos = set(spec['components']['schemas'])
        definidos_resp = set(spec['components']['responses'])
        rotas = []
        for ref in re.findall(r'"\$ref": "([^"]+)"', json.dumps(spec)):
            nombre = ref.rsplit('/', 1)[-1]
            if ref.startswith('#/components/schemas/') and nombre not in definidos:
                rotas.append(ref)
            if ref.startswith('#/components/responses/') and nombre not in definidos_resp:
                rotas.append(ref)

        assert not rotas, f'Referencias rotas: {sorted(set(rotas))}'


@pytest.mark.unit
class TestLosEsquemasSiguenAlCodigo:
    """The drift barrier.

    ``to_dict`` is what the API actually returns. When a field is added there,
    this fails until the schema says so too -- which is the only reason the
    description can be trusted a year from now.
    """

    def _claves(self, spec, nombre):
        return set(spec['components']['schemas'][nombre]['properties'])

    def test_el_viaje_coincide(self, spec, trip):
        assert self._claves(spec, 'Viaje') == set(trip.to_dict())

    def test_el_viajero_coincide(self, spec, traveler_row):
        assert self._claves(spec, 'Viajero') == set(traveler_row.to_dict())

    def test_el_usuario_coincide(self, spec, gestor):
        assert self._claves(spec, 'Usuario') == set(gestor.to_dict())

    def test_el_tramo_coincide(self, spec, segment_factory):
        segmento = segment_factory(numero='IB1')
        db.session.commit()

        assert self._claves(spec, 'Tramo') == set(segmento.to_dict())

    def test_el_documento_coincide(self, spec, gestor, trip, booking_pdf):
        import io

        from werkzeug.datastructures import FileStorage

        from app.models.enums import DocumentType
        from app.services import document_service

        storage = FileStorage(
            stream=io.BytesIO(booking_pdf), filename='d.pdf',
            content_type='application/pdf',
        )
        documento = document_service.upload(
            gestor, trip, storage, tipo=DocumentType.RESERVA, commit=False,
        )
        db.session.commit()

        assert self._claves(spec, 'Documento') == set(documento.to_dict())

    def test_la_alerta_coincide(self, spec, trip):
        from app.models.alert import Alert
        from app.models.enums import AlertSeverity, AlertState

        alerta = Alert(
            trip_id=trip.id, tipo='conexion', regla='Margen',
            dedup_key='y' * 16, severidad=AlertSeverity.ALTA,
            estado=AlertState.ABIERTA, titulo='t', mensaje='m', evidencia={},
        )
        db.session.add(alerta)
        db.session.commit()

        assert self._claves(spec, 'Alerta') == set(alerta.to_dict())


@pytest.mark.security
class TestQuienLeeElContrato:
    def test_hace_falta_iniciar_sesion(self, client):
        assert client.get('/api/v1/openapi.json').status_code == 401

    def test_cualquier_cuenta_puede_leerlo(self, client, api_login, viajero):
        api_login(viajero)

        respuesta = client.get('/api/v1/openapi.json')

        assert respuesta.status_code == 200
        assert respuesta.get_json()['openapi'].startswith('3.1')
