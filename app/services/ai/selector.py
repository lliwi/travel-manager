"""Choosing which provider and model serve a task.

Specification section 2.5: "Selección de proveedor/modelo por entorno y por
tarea." Resolution order:

1. The ``ai_task_bindings`` row for the task, if active.
2. The provider marked as default in Administración → Proveedores de IA.
3. A provider built straight from the environment, so a fresh deployment with an
   empty database still works against the host's Ollama.
"""
import logging

from flask import current_app

from app.models.ai import AIProviderConfig, AITaskBinding
from app.models.enums import AIProviderCode, AITask
from app.services.ai.ollama import OllamaProvider
from app.services.ai.openai_compatible import OpenAICompatibleProvider, StubProvider
from app.utils.errors import AIError

logger = logging.getLogger(__name__)

PROVIDER_CLASSES = {
    AIProviderCode.OLLAMA.value: OllamaProvider,
    AIProviderCode.VLLM.value: OpenAICompatibleProvider,
    AIProviderCode.OPENAI.value: OpenAICompatibleProvider,
    AIProviderCode.DEEPSEEK.value: OpenAICompatibleProvider,
    AIProviderCode.STUB.value: StubProvider,
}


def build_provider(config):
    """Instantiate the provider described by a configuration row."""
    code = str(config.proveedor)
    provider_cls = PROVIDER_CLASSES.get(code)
    if provider_cls is None:
        raise AIError(f'Proveedor de IA desconocido: {code}')

    if provider_cls is OpenAICompatibleProvider:
        return provider_cls(config=config, codigo=code)
    return provider_cls(config=config)


def resolve(tarea):
    """Pick the provider, model and parameters for a task.

    Returns:
        ``(provider, modelo, parametros, binding)``.
    """
    tarea = AITask.coerce(tarea)

    binding = None
    if tarea is not None:
        binding = AITaskBinding.query.filter_by(tarea=str(tarea), activo=True).first()

    if binding is not None and binding.provider_config is not None:
        config = binding.provider_config
        if config.activo:
            provider = build_provider(config)
            return provider, _model_for(binding, config), _params(binding, config), binding
        logger.warning(
            'El proveedor asignado a «%s» está desactivado; se usa el predeterminado.',
            tarea,
        )

    config = _default_config()
    if config is not None:
        provider = build_provider(config)
        return provider, _model_for(None, config), _params(None, config), None

    return _from_environment() + (None,)


def fallback_for(tarea):
    """The fallback provider configured for a task, if any."""
    tarea = AITask.coerce(tarea)
    binding = AITaskBinding.query.filter_by(tarea=str(tarea), activo=True).first()
    if binding is None or binding.fallback_config is None:
        return None, None
    if not binding.fallback_config.activo:
        return None, None
    config = binding.fallback_config
    return build_provider(config), config.modelo_por_defecto


def _default_config():
    """The provider serving tasks with no binding of their own.

    Chosen in the database, from Administración → Proveedores de IA, rather
    than from the environment: changing model or provider is an administrative
    decision, not a redeployment.
    """
    config = AIProviderConfig.query.filter_by(
        es_por_defecto=True, activo=True
    ).first()
    if config is not None:
        return config

    # No explicit default (or it was deactivated): fall back to the oldest
    # active provider rather than leaving every task unserved.
    return AIProviderConfig.query.filter_by(activo=True).order_by(
        AIProviderConfig.created_at.asc()
    ).first()


def _from_environment():
    """Last resort when the database holds no provider at all.

    Only two situations reach here: the test suite, which uses the stub, and a
    deployment whose seeding has not run yet. A real installation configures
    its providers in the panel, which is why there is no endpoint or model to
    read from the environment beyond the bootstrap values.
    """
    code = current_app.config.get('AI_BOOTSTRAP_PROVIDER', AIProviderCode.OLLAMA.value)

    if code == AIProviderCode.STUB.value:
        return StubProvider(), 'stub', {'max_tokens': 512, 'temperatura': 0.0}

    if code == AIProviderCode.OLLAMA.value:
        modelo = current_app.config.get('OLLAMA_BOOTSTRAP_MODEL', 'llama3.1:8b')
        provider = OllamaProvider(
            base_url=current_app.config.get(
                'OLLAMA_BOOTSTRAP_URL', 'http://host.docker.internal:11434'
            ),
            modelo=modelo,
            timeout=current_app.config.get('AI_REQUEST_TIMEOUT', 120),
        )
        return provider, modelo, {'max_tokens': 2048, 'temperatura': 0.1}

    raise AIError(
        'No hay ningún proveedor de IA configurado. Configúrelo en '
        'Administración → Proveedores de IA.'
    )


def _model_for(binding, config):
    if binding is not None and binding.modelo:
        return binding.modelo
    return config.modelo_por_defecto


def _params(binding, config):
    """Merge task-level overrides over the provider's defaults."""
    max_tokens = (
        (binding.max_tokens if binding is not None else None)
        or config.max_tokens or 2048
    )
    temperatura = (
        (binding.temperatura if binding is not None else None)
        if (binding is not None and binding.temperatura is not None)
        else config.temperatura
    )
    return {
        'max_tokens': int(max_tokens),
        'temperatura': float(temperatura if temperatura is not None else 0.1),
    }
