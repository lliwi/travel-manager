"""Parámetros avanzados por proveedor, modelo y tarea.

Lo que tiene que cumplirse, y que falla en silencio si no: un valor que el
administrador fija llega de verdad al endpoint, con el nombre que ese endpoint
usa; lo que un tipo de proveedor no admite no se le envía; y el orden de las
capas protege lo que la tarea necesita -- una temperatura «para todo el
modelo» no deshace la temperatura 0 de la extracción.
"""
import httpx
import pytest

from app.extensions import db
from app.models.ai import AIRun
from app.models.enums import AIProviderCode, AITask
from app.services import ai_provider_service as svc
from app.services.ai import parametros as catalogo
from app.services.ai.base import AIRequest
from app.utils.errors import ValidationError


@pytest.fixture
def ollama(app, admin):
    return svc.create(admin, 'Ollama', AIProviderCode.OLLAMA,
                      base_url='http://ollama.test', modelo='qwen3:8b',
                      temperatura=0.7, max_tokens=4096)


@pytest.fixture
def simulado(app, admin):
    return svc.create(admin, 'Simulado', AIProviderCode.STUB, modelo='stub-small',
                      por_defecto=True)


def _componer(tarea, config, modelo, pedidos=None):
    from app.services.ai.selector import _params, componer

    return componer(tarea, config, modelo, config.proveedor,
                    generales=_params(None, config, modelo), pedidos=pedidos)[0]


@pytest.mark.unit
class TestElCatalogoValida:
    def test_lo_vacio_no_se_guarda(self):
        """Blank means «the endpoint's default», which is not any value."""
        assert catalogo.validar({'top_p': '', 'top_k': None}) == {}

    def test_convierte_los_numeros(self):
        assert catalogo.validar({'top_p': '0,9', 'top_k': '40'}) == {'top_p': 0.9, 'top_k': 40}

    def test_una_clave_desconocida_se_rechaza(self):
        """A typo that silently vanished would look like a setting that took."""
        with pytest.raises(ValidationError, match='desconocido'):
            catalogo.validar({'top_pp': 0.9})

    def test_fuera_de_rango_se_rechaza(self):
        with pytest.raises(ValidationError, match='mayor que'):
            catalogo.validar({'top_p': 1.5})

    def test_un_entero_no_admite_decimales(self):
        with pytest.raises(ValidationError, match='entero'):
            catalogo.validar({'top_k': 4.5})

    def test_lo_que_el_proveedor_no_admite_se_rechaza(self):
        """OpenAI has no top_k: stored, it would never be sent."""
        with pytest.raises(ValidationError, match='no lo admite'):
            catalogo.validar({'top_k': 40}, AIProviderCode.OPENAI)

    def test_las_paradas_llegan_de_un_textarea(self):
        assert catalogo.validar({'stop': 'FIN\n\n###'}) == {'stop': ['FIN', '###']}


@pytest.mark.unit
class TestElOrdenDeLasCapas:
    def test_la_temperatura_del_proveedor_no_deshace_la_de_la_extraccion(self, ollama):
        """Extraction asks for 0 so a document gives the same answer twice."""
        efectivos = _componer(AITask.EXTRACT_DOCUMENT, ollama, 'qwen3:8b')

        assert efectivos['temperatura'] == 0.0

    def test_la_del_proveedor_vale_donde_la_tarea_no_pide_nada(self, ollama):
        efectivos = _componer(AITask.EXTRACT_DOCUMENT, ollama, 'qwen3:8b')

        assert efectivos['max_tokens'] == 4096

    def test_el_perfil_del_modelo_completa_lo_del_proveedor(self, admin, ollama):
        svc.save_profile(admin, ollama, 'qwen3:8b', None, {'top_k': 20, 'temperatura': 0.9})

        efectivos = _componer(AITask.EXTRACT_DOCUMENT, ollama, 'qwen3:8b')

        assert efectivos['top_k'] == 20
        assert efectivos['temperatura'] == 0.0, 'La tarea sigue mandando.'

    def test_el_perfil_de_otro_modelo_no_se_aplica(self, admin, ollama):
        svc.save_profile(admin, ollama, 'llama3.1:8b', None, {'top_k': 20})

        assert 'top_k' not in _componer(AITask.EXTRACT_DOCUMENT, ollama, 'qwen3:8b')

    def test_el_perfil_de_la_tarea_manda_sobre_todo(self, admin, ollama):
        svc.save_profile(admin, ollama, 'qwen3:8b', AITask.CLASSIFY_DOCUMENT,
                         {'temperatura': 0.3, 'max_tokens': 1024})

        efectivos = _componer(AITask.CLASSIFY_DOCUMENT, ollama, 'qwen3:8b')

        assert efectivos['temperatura'] == 0.3
        assert efectivos['max_tokens'] == 1024, 'Por encima de los 256 del código.'

    def test_el_perfil_de_una_tarea_no_toca_las_demas(self, admin, ollama):
        svc.save_profile(admin, ollama, 'qwen3:8b', AITask.CLASSIFY_DOCUMENT, {'top_p': 0.5})

        assert 'top_p' not in _componer(AITask.SUMMARIZE_TRIP, ollama, 'qwen3:8b')

    def test_guardar_un_perfil_vacio_lo_elimina(self, admin, ollama):
        svc.save_profile(admin, ollama, 'qwen3:8b', None, {'top_k': 20})
        svc.save_profile(admin, ollama, 'qwen3:8b', None, {'top_k': ''})

        assert svc.list_profiles(ollama) == []

    def test_un_solo_perfil_general_por_modelo(self, admin, ollama):
        svc.save_profile(admin, ollama, 'qwen3:8b', None, {'top_k': 20})
        svc.save_profile(admin, ollama, 'qwen3:8b', None, {'top_k': 30})

        perfiles = svc.list_profiles(ollama)
        assert len(perfiles) == 1
        assert perfiles[0].parametros == {'top_k': 30}


@pytest.mark.unit
class TestLoConfiguradoLlegaAlEndpoint:
    """Two settings that were stored and never sent, until now."""

    def test_el_modelo_de_la_tarea_es_el_que_se_envia(self, admin, ollama):
        """It was recorded in ai_runs while the provider's default answered."""
        from app.services.ai.selector import resolve

        svc.set_binding(admin, AITask.SUMMARIZE_TRIP, ollama, modelo='llama3.1:8b')

        provider, modelo, _p, _b = resolve(AITask.SUMMARIZE_TRIP)

        assert modelo == 'llama3.1:8b'
        assert provider.modelo == 'llama3.1:8b'

    def test_el_maximo_de_tokens_del_proveedor_se_aplica(self, admin, simulado):
        """The request used to carry its own 2048 and the panel's value lost."""
        from app.services import ai_service

        svc.update(admin, simulado, max_tokens=6000)
        request = AIRequest(tarea='summarize_trip', sistema='s', instruccion='i')

        _response, run = ai_service._run(AITask.SUMMARIZE_TRIP, request)

        assert request.max_tokens == 6000
        assert run.parametros['max_tokens'] == 6000

    def test_la_ejecucion_registra_lo_que_se_envio(self, admin, simulado):
        from app.services import ai_service

        svc.save_profile(admin, simulado, 'stub-small', None, {'top_p': 0.8, 'seed': 7})
        request = AIRequest(tarea='summarize_trip', sistema='s', instruccion='i')

        _response, run = ai_service._run(AITask.SUMMARIZE_TRIP, request)

        assert db.session.get(AIRun, run.id).parametros == {
            'max_tokens': 2048, 'temperatura': 0.2, 'top_p': 0.8, 'seed': 7,
        }


class _Cliente:
    """An httpx client that records payloads and answers from a function."""

    enviados = []
    responder = None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, **k):
        type(self).enviados.append(dict(json))
        return type(self).responder(url, json)


@pytest.fixture
def cliente(monkeypatch):
    class Cliente(_Cliente):
        enviados = []

    monkeypatch.setattr(httpx, 'Client', Cliente)
    return Cliente


def _ok_ollama(url, json):
    return httpx.Response(200, json={'message': {'content': '{}'}, 'model': 'm'},
                          request=httpx.Request('POST', url))


def _ok_openai(url, json):
    return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}],
                                     'model': 'm', 'usage': {}},
                          request=httpx.Request('POST', url))


def _peticion(**parametros):
    return AIRequest(tarea='summarize_trip', sistema='s', instruccion='i',
                     max_tokens=512, temperatura=0.1, parametros=parametros)


@pytest.mark.unit
class TestCadaProveedorConSusNombres:
    def test_ollama_los_manda_en_options(self, cliente):
        from app.services.ai.ollama import OllamaProvider

        cliente.responder = staticmethod(_ok_ollama)
        OllamaProvider(base_url='http://o.test', modelo='m').complete(
            _peticion(top_k=20, num_ctx=16384, repeat_penalty=1.1),
        )

        opciones = cliente.enviados[0]['options']
        assert opciones == {'temperature': 0.1, 'num_predict': 512, 'top_k': 20,
                            'num_ctx': 16384, 'repeat_penalty': 1.1}

    def test_openai_no_recibe_lo_que_no_tiene(self, cliente):
        """A parameter the endpoint does not know gets the whole request refused."""
        from app.services.ai.openai_compatible import OpenAICompatibleProvider

        cliente.responder = staticmethod(_ok_openai)
        OpenAICompatibleProvider(codigo='openai', base_url='https://x.test/v1',
                                 modelo='m', api_key='k').complete(
            _peticion(top_p=0.9, top_k=20, num_ctx=8192, seed=3),
        )

        enviado = cliente.enviados[0]
        assert enviado['top_p'] == 0.9 and enviado['seed'] == 3
        assert 'top_k' not in enviado and 'num_ctx' not in enviado

    def test_vllm_usa_su_nombre_para_la_repeticion(self, cliente):
        from app.services.ai.openai_compatible import OpenAICompatibleProvider

        cliente.responder = staticmethod(_ok_openai)
        OpenAICompatibleProvider(codigo='vllm', base_url='http://v.test/v1',
                                 modelo='m').complete(
            _peticion(repeat_penalty=1.1, top_k=20, min_p=0.05),
        )

        enviado = cliente.enviados[0]
        assert enviado['repetition_penalty'] == 1.1
        assert enviado['top_k'] == 20 and enviado['min_p'] == 0.05
        assert 'repeat_penalty' not in enviado


@pytest.mark.unit
class TestUnParametroRechazadoSeAprende:
    """What an endpoint refuses is discovered once, per model, and not sent again."""

    @staticmethod
    def _rechaza_top_p(url, json):
        if 'top_p' in json:
            return httpx.Response(
                400, json={'error': {'message': "Unsupported parameter: 'top_p' is "
                                                "not supported with this model."}},
                request=httpx.Request('POST', url),
            )
        return _ok_openai(url, json)

    @pytest.fixture
    def openai(self, admin):
        return svc.create(admin, 'OpenAI', AIProviderCode.OPENAI,
                          base_url='https://x.test/v1', modelo='o-razona', api_key='sk-1')

    def _provider(self, config, modelo='o-razona'):
        from app.services.ai.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(config=config, codigo='openai', modelo=modelo)

    def test_se_reintenta_sin_el(self, cliente, openai):
        cliente.responder = staticmethod(self._rechaza_top_p)

        self._provider(openai).complete(_peticion(top_p=0.9))

        assert 'top_p' in cliente.enviados[0]
        assert 'top_p' not in cliente.enviados[1]

    def test_no_se_vuelve_a_enviar_a_ese_modelo(self, cliente, openai):
        cliente.responder = staticmethod(self._rechaza_top_p)
        self._provider(openai).complete(_peticion(top_p=0.9))
        cliente.enviados.clear()

        self._provider(openai).complete(_peticion(top_p=0.9))

        assert len(cliente.enviados) == 1
        assert 'top_p' not in cliente.enviados[0]

    def test_otro_modelo_del_mismo_endpoint_lo_sigue_recibiendo(self, cliente, openai):
        """What o3 refuses, gpt-4o-mini may take."""
        cliente.responder = staticmethod(self._rechaza_top_p)
        self._provider(openai).complete(_peticion(top_p=0.9))
        cliente.enviados.clear()
        cliente.responder = staticmethod(_ok_openai)

        self._provider(openai, modelo='o-no-razona').complete(_peticion(top_p=0.9))

        assert cliente.enviados[0]['top_p'] == 0.9

    def test_una_palabra_parecida_no_cuenta_como_rechazo(self):
        """«stop» must not match «stopped»."""
        from app.services.ai.openai_compatible import _corregir

        payload = {'model': 'm', 'stop': ['FIN']}
        assert _corregir(payload, 'The model stopped responding', 'max_tokens') is None
        assert 'stop' in payload


@pytest.mark.unit
class TestForzarUnaTarea:
    def test_solo_afecta_a_la_tarea_indicada(self, admin, simulado, ollama):
        from app.services.ai import forzar
        from app.services.ai.selector import resolve

        with forzar(AITask.SUMMARIZE_TRIP, ollama, modelo='otro:1b'):
            forzado = resolve(AITask.SUMMARIZE_TRIP)
            libre = resolve(AITask.EXPLAIN_ALERT)

        assert forzado[0].modelo == 'otro:1b'
        assert libre[1] == 'stub-small'

    def test_al_salir_todo_vuelve_a_ser_como_antes(self, admin, simulado, ollama):
        from app.services.ai import forzar
        from app.services.ai.selector import resolve

        with forzar(AITask.SUMMARIZE_TRIP, ollama, modelo='otro:1b'):
            pass

        assert resolve(AITask.SUMMARIZE_TRIP)[1] == 'stub-small'

    def test_evaluar_con_otro_modelo_usa_ese_modelo(self, admin, simulado, monkeypatch):
        """``flask evaluar --modelo`` used to report the model and not use it."""
        from app.services import ai_service, evaluation_service
        from app.services.ai.selector import resolve

        vistos = []

        def _extraer(*a, **k):
            vistos.append(resolve(AITask.EXTRACT_DOCUMENT)[1])
            return {'datos': {'servicios': []}}

        monkeypatch.setattr(ai_service, 'extract_document', _extraer)

        evaluation_service.evaluar(modelo='stub-large')

        assert vistos and set(vistos) == {'stub-large'}


@pytest.mark.integration
class TestLasPantallas:
    def test_el_formulario_del_proveedor_guarda_los_avanzados(
        self, as_user, admin, ollama,
    ):
        with as_user(admin) as client:
            respuesta = client.post(
                f'/admin/ajustes/proveedores/{ollama.id}/editar',
                data={'nombre': 'Ollama', 'proveedor': 'ollama',
                      'base_url': 'http://ollama.test', 'modelo_por_defecto': 'qwen3:8b',
                      'timeout_segundos': 120, 'max_tokens': 4096, 'temperatura': '0.7',
                      'activo': 'y', 'p_top_k': '30', 'p_num_ctx': '16384'},
            )

        assert respuesta.status_code == 302
        assert svc.get_or_404(ollama.id).parametros_avanzados == {
            'top_k': 30, 'num_ctx': 16384,
        }

    def test_la_pantalla_de_perfiles_guarda_uno(self, as_user, admin, ollama):
        with as_user(admin) as client:
            assert client.get(
                f'/admin/ajustes/proveedores/{ollama.id}/parametros'
            ).status_code == 200
            client.post(f'/admin/ajustes/proveedores/{ollama.id}/parametros',
                        data={'modelo': 'qwen3:8b', 'tarea': 'extract_document',
                              'p_top_p': '0.8'})

        perfil = svc.list_profiles(ollama)[0]
        assert perfil.tarea is AITask.EXTRACT_DOCUMENT
        assert perfil.parametros == {'top_p': 0.8}

    def test_un_valor_invalido_se_explica_y_no_se_guarda(self, as_user, admin, ollama):
        with as_user(admin) as client:
            html = client.post(
                f'/admin/ajustes/proveedores/{ollama.id}/parametros',
                data={'modelo': 'qwen3:8b', 'p_top_p': '7'}, follow_redirects=True,
            ).get_data(as_text=True)

        assert 'no puede ser mayor' in html
        assert svc.list_profiles(ollama) == []

    def test_un_gestor_no_entra(self, as_user, gestor, ollama):
        with as_user(gestor) as client:
            respuesta = client.get(f'/admin/ajustes/proveedores/{ollama.id}/parametros')

        assert respuesta.status_code in (403, 404)
