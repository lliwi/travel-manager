"""Apagar el razonamiento de un modelo que razona, donde se pueda.

Un modelo de razonamiento gasta tokens de salida pensando antes de escribir, y
con un presupuesto ajustado se queda sin sitio para responder: la llamada
termina «bien» y vacía. El ajuste existe para eso, pero pedirlo a ciegas es
peor que no tenerlo: un parámetro que el endpoint no admite tumba la petición
entera, así que sólo se pide cuando consta que se puede.

Qué se puede preguntar y qué no, comprobado contra los dos endpoints reales:
Ollama publica las capacidades del modelo en «/api/show» y «thinking» sólo
aparece en los que razonan; OpenAI devuelve en «/models/{id}» el identificador
y poco más, así que ahí no hay nada que consultar y se aprende del rechazo.
"""
import httpx
import pytest

pytestmark = pytest.mark.unit


class _Respuesta:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                'error', request=httpx.Request('POST', 'http://t'), response=self,
            )

    @property
    def text(self):
        import json

        return json.dumps(self._payload)


def _cliente(respuestas, registro):
    """Un cliente que va devolviendo respuestas y apunta lo que se le pidió."""

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None, **k):
            registro.append({'url': url, 'payload': json})
            return respuestas.pop(0)

        def get(self, url, **k):
            registro.append({'url': url, 'payload': None})
            return respuestas.pop(0)

    return _Cliente


class _Config:
    proveedor = 'ollama'
    base_url = 'http://ollama.test'
    modelo_por_defecto = 'qwen3:8b'
    timeout_segundos = 30
    sin_razonamiento = True
    parametros = None
    api_key_encrypted = None


@pytest.fixture
def peticion():
    from app.services.ai.base import AIRequest

    return AIRequest(tarea='resumen', sistema='s', instruccion='i',
                     max_tokens=512, temperatura=0.1)


class TestOllamaPreguntaAntesDePedir:
    def _proveedor(self):
        from app.services.ai.ollama import _CAPACIDADES, OllamaProvider

        _CAPACIDADES.clear()
        return OllamaProvider(config=_Config())

    def test_a_un_modelo_que_razona_se_le_pide(self, app, peticion, monkeypatch):
        registro = []
        monkeypatch.setattr(httpx, 'Client', _cliente([
            _Respuesta({'capabilities': ['completion', 'tools', 'thinking']}),
            _Respuesta({'message': {'content': 'hola'}}),
        ], registro))

        self._proveedor().complete(peticion)

        assert registro[0]['url'].endswith('/api/show')
        assert registro[1]['payload']['think'] is False

    def test_a_uno_que_no_razona_no_se_le_manda_el_parametro(
        self, app, peticion, monkeypatch,
    ):
        """Un «think» que sobra es un parámetro de más, y hay servidores que
        rechazan la petición entera por él."""
        registro = []
        monkeypatch.setattr(httpx, 'Client', _cliente([
            _Respuesta({'capabilities': ['completion', 'vision']}),
            _Respuesta({'message': {'content': 'hola'}}),
        ], registro))

        self._proveedor().complete(peticion)

        assert 'think' not in registro[1]['payload']

    def test_sin_el_ajuste_no_se_pregunta_siquiera(self, app, peticion, monkeypatch):
        """Una consulta de capacidades en cada llamada es un viaje de ida y
        vuelta más en cada extracción."""
        from app.services.ai.ollama import _CAPACIDADES, OllamaProvider

        _CAPACIDADES.clear()
        config = _Config()
        config.sin_razonamiento = False
        registro = []
        monkeypatch.setattr(httpx, 'Client', _cliente([
            _Respuesta({'message': {'content': 'hola'}}),
        ], registro))

        OllamaProvider(config=config).complete(peticion)

        assert len(registro) == 1
        assert 'think' not in registro[0]['payload']

    def test_si_no_se_puede_preguntar_no_se_pide(self, app, peticion, monkeypatch):
        """Negarse a funcionar porque falló una consulta de capacidades
        convertiría una respuesta lenta en ninguna respuesta."""
        registro = []

        class _Roto:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None, **k):
                if url.endswith('/api/show'):
                    raise httpx.ConnectError('sin ruta')
                registro.append({'payload': json})
                return _Respuesta({'message': {'content': 'hola'}})

        monkeypatch.setattr(httpx, 'Client', _Roto)

        self._proveedor().complete(peticion)

        assert 'think' not in registro[0]['payload']

    def test_las_capacidades_se_consultan_una_vez(self, app, peticion, monkeypatch):
        registro = []
        monkeypatch.setattr(httpx, 'Client', _cliente([
            _Respuesta({'capabilities': ['thinking']}),
            _Respuesta({'message': {'content': 'a'}}),
            _Respuesta({'message': {'content': 'b'}}),
        ], registro))

        proveedor = self._proveedor()
        proveedor.complete(peticion)
        proveedor.complete(peticion)

        assert sum(1 for r in registro if r['url'].endswith('/api/show')) == 1


class TestElEndpointCompatibleAprendeDelRechazo:
    def _proveedor(self, config):
        from app.services.ai.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(config=config, codigo='openai')

    def _config(self, app, **extra):
        from app.extensions import db
        from app.models.ai import AIProviderConfig
        from app.services import ai_provider_service

        config = AIProviderConfig(
            nombre='OpenAI de prueba', proveedor='openai',
            base_url='https://api.openai.test/v1', modelo_por_defecto='gpt-x',
            timeout_segundos=30, max_tokens=512, temperatura=0.1,
            sin_razonamiento=True, **extra,
        )
        # Un proveedor externo sin clave se niega antes de llegar al payload.
        ai_provider_service._set_api_key(config, 'una-clave')
        db.session.add(config)
        db.session.commit()
        return config

    def test_se_pide_el_esfuerzo_minimo(self, app, seeded, peticion, monkeypatch):
        registro = []
        monkeypatch.setattr(httpx, 'Client', _cliente([
            _Respuesta({'choices': [{'message': {'content': 'hola'}}]}),
        ], registro))

        self._proveedor(self._config(app)).complete(peticion)

        assert registro[0]['payload']['reasoning_effort'] == 'minimal'

    def test_un_rechazo_se_anota_y_la_llamada_sale_adelante(
        self, app, seeded, peticion, monkeypatch,
    ):
        """Sin esto, marcar la casilla rompería todas las llamadas a ese
        proveedor en vez de no hacer nada."""
        registro = []
        monkeypatch.setattr(httpx, 'Client', _cliente([
            _Respuesta({'error': {'message': "Unknown parameter: 'reasoning_effort'."}},
                       status=400),
            _Respuesta({'choices': [{'message': {'content': 'hola'}}]}),
        ], registro))

        config = self._config(app)
        respuesta = self._proveedor(config).complete(peticion)

        assert respuesta.contenido == 'hola'
        assert config.parametros['sin_razonamiento_no_admitido'] is True
        assert 'reasoning_effort' not in registro[1]['payload']

    def test_anotado_una_vez_no_se_vuelve_a_enviar(
        self, app, seeded, peticion, monkeypatch,
    ):
        registro = []
        monkeypatch.setattr(httpx, 'Client', _cliente([
            _Respuesta({'choices': [{'message': {'content': 'hola'}}]}),
        ], registro))

        config = self._config(app, parametros={'sin_razonamiento_no_admitido': True})
        self._proveedor(config).complete(peticion)

        assert 'reasoning_effort' not in registro[0]['payload']

    def test_un_400_por_otra_cosa_no_se_confunde_con_esto(
        self, app, seeded, peticion, monkeypatch,
    ):
        """Anotar el rechazo por un 400 cualquiera desactivaría el ajuste sin
        que nadie hubiera rechazado nada."""
        from app.utils.errors import AIError

        registro = []
        monkeypatch.setattr(httpx, 'Client', _cliente([
            _Respuesta({'error': {'message': 'context length exceeded'}}, status=400),
        ], registro))

        config = self._config(app)
        with pytest.raises(AIError):
            self._proveedor(config).complete(peticion)

        assert not (config.parametros or {}).get('sin_razonamiento_no_admitido')

    def test_sin_el_ajuste_no_se_manda_nada(self, app, seeded, peticion, monkeypatch):
        registro = []
        monkeypatch.setattr(httpx, 'Client', _cliente([
            _Respuesta({'choices': [{'message': {'content': 'hola'}}]}),
        ], registro))

        config = self._config(app)
        config.sin_razonamiento = False
        self._proveedor(config).complete(peticion)

        assert 'reasoning_effort' not in registro[0]['payload']


class TestElAjusteSeAdministraDesdeElPanel:
    def test_se_guarda_al_crear_un_proveedor(self, app, seeded, admin):
        from app.services import ai_provider_service

        config = ai_provider_service.create(
            admin, nombre='Uno que razona', proveedor='ollama',
            base_url='http://x', modelo='qwen3:8b', sin_razonamiento=True,
        )

        assert config.sin_razonamiento is True

    def test_cambiarlo_olvida_lo_que_el_endpoint_habia_rechazado(
        self, app, seeded, admin,
    ):
        """Puede haberse cambiado también el modelo, y arrastrar el «no» de
        otro dejaría la casilla marcada sin efecto para siempre."""
        from app.services import ai_provider_service

        config = ai_provider_service.create(
            admin, nombre='Dos', proveedor='openai', base_url='http://x',
            modelo='gpt-x', api_key='k', sin_razonamiento=False,
        )
        config.parametros = {'sin_razonamiento_no_admitido': True}

        ai_provider_service.update(admin, config, sin_razonamiento=True)

        assert not (config.parametros or {}).get('sin_razonamiento_no_admitido')
