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
