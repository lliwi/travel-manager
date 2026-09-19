"""Administering AI providers from the panel.

Specification section 2.5: provider and model selectable per environment and
per task, keys encrypted and never exposed. All of it is an administrative
decision, so it lives in the database rather than in the deployment.
"""
import pytest

from app.extensions import db
from app.models.ai import AIProviderConfig, AITaskBinding
from app.models.enums import AIProviderCode, AITask
from app.services import ai_provider_service as svc
from app.utils.errors import ConflictError, ValidationError


@pytest.fixture
def local(app, admin):
    return svc.create(admin, 'Ollama local', AIProviderCode.OLLAMA,
                      base_url='http://host.docker.internal:11434',
                      modelo='llama3.1:8b')


@pytest.mark.unit
class TestAltaDeProveedores:
    def test_se_crea_con_los_valores_sugeridos(self, app, admin):
        config = svc.create(admin, 'Ollama', AIProviderCode.OLLAMA)

        assert config.base_url == 'http://host.docker.internal:11434'
        assert config.modelo_por_defecto == 'llama3.1:8b'
        assert config.activo

    def test_el_primero_queda_como_predeterminado(self, app, admin):
        """Otherwise nothing would answer until somebody remembered to pick one."""
        config = svc.create(admin, 'El primero', AIProviderCode.OLLAMA)
        assert config.es_por_defecto

    def test_no_se_repite_el_nombre(self, app, admin, local):
        with pytest.raises(ConflictError, match='Ya existe'):
            svc.create(admin, 'Ollama local', AIProviderCode.VLLM)

    def test_un_proveedor_externo_exige_clave(self, app, admin):
        with pytest.raises(ValidationError, match='clave API'):
            svc.create(admin, 'OpenAI', AIProviderCode.OPENAI)

    def test_un_proveedor_local_no_exige_clave(self, app, admin):
        assert svc.create(admin, 'vLLM', AIProviderCode.VLLM) is not None


@pytest.mark.security
class TestClavesApi:
    def test_la_clave_se_guarda_cifrada(self, app, admin):
        from app.utils.crypto import decrypt_secret

        clave = 'sk-secreto-de-produccion-987654'
        config = svc.create(admin, 'OpenAI', AIProviderCode.OPENAI, api_key=clave)

        assert clave not in (config.api_key_encrypted or '')
        assert decrypt_secret(config.api_key_encrypted) == clave
        assert config.api_key_pista == '7654', 'Solo los últimos cuatro.'

    def test_al_editar_sin_clave_se_conserva_la_guardada(self, app, admin):
        """The form never shows the key, so blank means unchanged."""
        from app.utils.crypto import decrypt_secret

        config = svc.create(admin, 'OpenAI', AIProviderCode.OPENAI,
                            api_key='sk-original-123456')
        cifrada = config.api_key_encrypted

        svc.update(admin, config, nombre='OpenAI renombrado', api_key=None)

        assert config.api_key_encrypted == cifrada
        assert decrypt_secret(config.api_key_encrypted) == 'sk-original-123456'

    def test_se_puede_sustituir_la_clave(self, app, admin):
        from app.utils.crypto import decrypt_secret

        config = svc.create(admin, 'OpenAI', AIProviderCode.OPENAI,
                            api_key='sk-original-123456')
        svc.update(admin, config, api_key='sk-nueva-999888')

        assert decrypt_secret(config.api_key_encrypted) == 'sk-nueva-999888'

    def test_la_clave_no_sale_en_la_representacion(self, app, admin):
        import json

        config = svc.create(admin, 'OpenAI', AIProviderCode.OPENAI,
                            api_key='sk-secreto-123456')
        payload = json.dumps(config.to_dict())

        assert 'sk-secreto' not in payload
        assert 'api_key_encrypted' not in payload


@pytest.mark.unit
class TestProveedorPredeterminado:
    def test_solo_uno_es_predeterminado(self, app, admin, local):
        otro = svc.create(admin, 'vLLM', AIProviderCode.VLLM)
        svc.set_default(admin, otro)

        db.session.refresh(local)
        assert otro.es_por_defecto
        assert not local.es_por_defecto
        assert AIProviderConfig.query.filter_by(es_por_defecto=True).count() == 1

    def test_uno_desactivado_no_puede_ser_predeterminado(self, app, admin, local):
        otro = svc.create(admin, 'vLLM', AIProviderCode.VLLM, activo=False)

        with pytest.raises(ValidationError, match='Active el proveedor'):
            svc.set_default(admin, otro)

    def test_desactivar_el_predeterminado_promueve_a_otro(self, app, admin, local):
        """Leaving no default would stop every AI task working."""
        otro = svc.create(admin, 'vLLM', AIProviderCode.VLLM)
        assert local.es_por_defecto

        svc.update(admin, local, activo=False)

        db.session.refresh(otro)
        assert not local.es_por_defecto
        assert otro.es_por_defecto

    def test_el_selector_usa_el_predeterminado(self, app, admin, local):
        from app.services.ai.selector import resolve

        otro = svc.create(admin, 'Otro Ollama', AIProviderCode.OLLAMA,
                          modelo='qwen3:8b')
        svc.set_default(admin, otro)

        _provider, modelo, _params, _binding = resolve(AITask.SUMMARIZE_TRIP)
        assert modelo == 'qwen3:8b'


@pytest.mark.unit
class TestBajaDeProveedores:
    def test_se_elimina(self, app, admin, local):
        svc.create(admin, 'vLLM', AIProviderCode.VLLM)
        svc.delete(admin, local)

        assert AIProviderConfig.query.filter_by(nombre='Ollama local').count() == 0

    def test_no_se_elimina_si_hay_tareas_asignadas(self, app, admin, local):
        """Dropping the binding silently would change the model behind a task."""
        svc.set_binding(admin, AITask.EXTRACT_DOCUMENT, local)

        with pytest.raises(ConflictError, match='tarea'):
            svc.delete(admin, local)

    def test_al_eliminar_el_predeterminado_se_promueve_otro(self, app, admin, local):
        otro = svc.create(admin, 'vLLM', AIProviderCode.VLLM)
        assert local.es_por_defecto

        svc.delete(admin, local)

        db.session.refresh(otro)
        assert otro.es_por_defecto


@pytest.mark.unit
class TestAsignacionPorTarea:
    def test_se_asigna_una_tarea(self, app, admin, local):
        binding = svc.set_binding(admin, AITask.EXTRACT_DOCUMENT, local,
                                  modelo='qwen3:8b')

        assert binding.provider_config_id == local.id
        assert binding.modelo == 'qwen3:8b'

    def test_el_selector_respeta_la_asignacion(self, app, admin, local):
        from app.services.ai.selector import resolve

        svc.set_binding(admin, AITask.EXTRACT_DOCUMENT, local, modelo='modelo-grande')

        _p, modelo, _params, binding = resolve(AITask.EXTRACT_DOCUMENT)
        assert modelo == 'modelo-grande'
        assert binding is not None

    def test_una_tarea_sin_asignar_usa_el_predeterminado(self, app, admin, local):
        from app.services.ai.selector import resolve

        _p, modelo, _params, binding = resolve(AITask.SUMMARIZE_TRIP)
        assert binding is None
        assert modelo == local.modelo_por_defecto

    def test_se_puede_quitar_la_asignacion(self, app, admin, local):
        svc.set_binding(admin, AITask.EXTRACT_DOCUMENT, local)
        svc.set_binding(admin, AITask.EXTRACT_DOCUMENT, None)

        assert AITaskBinding.query.filter_by(
            tarea=str(AITask.EXTRACT_DOCUMENT)
        ).count() == 0

    def test_se_listan_todas_las_tareas(self, app, admin, local):
        listado = svc.list_bindings()

        assert len(listado) == len(list(AITask))
        assert all(binding is None for _tarea, binding in listado)


@pytest.mark.security
class TestAuditoria:
    def test_las_operaciones_quedan_auditadas(self, app, admin, local):
        from app.models.audit import AuditEvent

        svc.update(admin, local, modelo='qwen3:8b')
        svc.set_binding(admin, AITask.EXTRACT_DOCUMENT, local)

        acciones = {e.accion for e in AuditEvent.query.all()}
        assert 'ai_provider.created' in acciones
        assert 'ai_provider.updated' in acciones
        assert 'ai_binding.set' in acciones

    def test_la_auditoria_no_contiene_la_clave(self, app, admin):
        import json

        from app.models.audit import AuditEvent

        config = svc.create(admin, 'OpenAI', AIProviderCode.OPENAI,
                            api_key='sk-jamas-en-la-auditoria-123')
        svc.update(admin, config, api_key='sk-tampoco-esta-456')

        todo = json.dumps([e.metadatos for e in AuditEvent.query.all()])
        assert 'sk-jamas' not in todo
        assert 'sk-tampoco' not in todo


@pytest.mark.unit
class TestSiembra:
    def test_se_siembra_un_proveedor_local(self, app):
        from app.services.seed_service import seed_ai_providers

        assert seed_ai_providers() == 1

        config = AIProviderConfig.query.first()
        assert config.proveedor is AIProviderCode.OLLAMA
        assert config.es_por_defecto
        assert not config.es_externo

    def test_sembrar_dos_veces_no_duplica(self, app):
        from app.services.seed_service import seed_ai_providers

        seed_ai_providers()
        assert seed_ai_providers() == 0
        assert AIProviderConfig.query.count() == 1


@pytest.mark.unit
class TestElNombreDelLimiteDeTokens:
    """The output-length limit goes by two names, and neither works everywhere.

    Newer OpenAI models reject «max_tokens» with a 400 and want
    «max_completion_tokens»; most self-hosted OpenAI-compatible servers only
    know the original. Guessing from the model name would need editing every
    time a family appears, so the endpoint is asked once and the answer kept.
    """

    def _provider(self, config=None):
        from app.services.ai.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            config=config, codigo='openai',
            base_url='https://api.example.test/v1', modelo='gpt-nuevo',
            api_key='sk-test',
        )

    def _request(self):
        from app.services.ai.base import AIRequest

        return AIRequest(
            tarea='extract_document', sistema='s', instruccion='i',
            max_tokens=512, temperatura=0.0,
        )

    def _cliente(self, enviados, rechaza):
        import httpx

        class _Cliente:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None, headers=None):
                enviados.append(json)
                if rechaza and rechaza in json:
                    return httpx.Response(
                        400,
                        json={'error': {'message':
                              "Unsupported parameter: 'max_tokens' is not "
                              "supported with this model. Use "
                              "'max_completion_tokens' instead."}},
                        request=httpx.Request('POST', url),
                    )
                return httpx.Response(
                    200,
                    json={'choices': [{'message': {'content': '{}'}}],
                          'model': 'gpt-nuevo', 'usage': {}},
                    request=httpx.Request('POST', url),
                )

        return _Cliente

    def _ejecutar(self, provider, rechaza):
        import httpx

        enviados = []
        original = httpx.Client
        httpx.Client = self._cliente(enviados, rechaza)
        try:
            provider.complete(self._request())
        finally:
            httpx.Client = original
        return enviados

    def test_por_defecto_se_envia_el_nombre_clasico(self):
        enviados = self._ejecutar(self._provider(), rechaza=None)

        assert 'max_tokens' in enviados[0]

    def test_si_lo_rechaza_se_reintenta_con_el_nuevo(self):
        enviados = self._ejecutar(self._provider(), rechaza='max_tokens')

        assert len(enviados) == 2
        assert 'max_completion_tokens' in enviados[1]
        assert 'max_tokens' not in enviados[1]

    def test_lo_aprendido_se_guarda_en_la_configuracion(self, app, admin):
        """So the wasted round trip happens once, not on every document."""
        from app.services import ai_provider_service

        config = ai_provider_service.create(
            admin, nombre='OpenAI nuevo', proveedor='openai',
            base_url='https://api.example.test/v1', modelo='gpt-nuevo',
            api_key='sk-test',
        )

        self._ejecutar(self._provider(config), rechaza='max_tokens')

        assert (config.parametros or {}).get('token_param') == 'max_completion_tokens'

    def test_con_lo_aprendido_no_hay_reintento(self, app, admin):
        from app.services import ai_provider_service

        config = ai_provider_service.create(
            admin, nombre='OpenAI recordado', proveedor='openai',
            base_url='https://api.example.test/v1', modelo='gpt-nuevo',
            api_key='sk-test',
        )
        config.parametros = {'token_param': 'max_completion_tokens'}

        enviados = self._ejecutar(self._provider(config), rechaza='max_tokens')

        assert len(enviados) == 1
        assert 'max_completion_tokens' in enviados[0]

    def test_un_400_por_otra_causa_no_se_reintenta(self):
        """Only the renamed parameter is retried; anything else is the error."""
        from app.utils.errors import AIError

        with pytest.raises(AIError):
            self._ejecutar(self._provider(), rechaza='model')

    def test_una_temperatura_rechazada_se_omite(self):
        """Some models accept only their default temperature.

        Extraction asks for 0.0 so the same document gives the same answer.
        A model that refuses it still answers at its default, which is worth
        having -- but it is a real loss of determinism, not a detail.
        """
        import httpx

        enviados = []
        original = httpx.Client

        class _Cliente:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None, headers=None):
                enviados.append(dict(json))
                if 'temperature' in json:
                    return httpx.Response(
                        400,
                        json={'error': {'message':
                              "Unsupported value: 'temperature' does not support "
                              "0.0 with this model. Only the default (1) value "
                              "is supported."}},
                        request=httpx.Request('POST', url),
                    )
                return httpx.Response(
                    200,
                    json={'choices': [{'message': {'content': '{}'}}],
                          'model': 'gpt-nuevo', 'usage': {}},
                    request=httpx.Request('POST', url),
                )

        httpx.Client = _Cliente
        try:
            self._provider().complete(self._request())
        finally:
            httpx.Client = original

        assert 'temperature' in enviados[0]
        assert 'temperature' not in enviados[-1]

    def test_se_recuerdan_las_dos_incompatibilidades(self, app, admin):
        from app.services import ai_provider_service

        config = ai_provider_service.create(
            admin, nombre='OpenAI exigente', proveedor='openai',
            base_url='https://api.example.test/v1', modelo='gpt-nuevo',
            api_key='sk-test',
        )
        import httpx

        original = httpx.Client

        class _Cliente:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None, headers=None):
                if 'max_tokens' in json:
                    mensaje = "Unsupported parameter: 'max_tokens'."
                elif 'temperature' in json:
                    mensaje = "Unsupported value: 'temperature' does not support 0.0."
                else:
                    return httpx.Response(
                        200,
                        json={'choices': [{'message': {'content': '{}'}}],
                              'model': 'gpt-nuevo', 'usage': {}},
                        request=httpx.Request('POST', url),
                    )
                return httpx.Response(
                    400, json={'error': {'message': mensaje}},
                    request=httpx.Request('POST', url),
                )

        httpx.Client = _Cliente
        try:
            self._provider(config).complete(self._request())
        finally:
            httpx.Client = original

        assert config.parametros['token_param'] == 'max_completion_tokens'
        assert config.parametros['sin_temperatura'] is True
