"""Choosing which provider and model serve a task.

Specification section 2.5: "Selección de proveedor/modelo por entorno y por
tarea." Resolution order:

1. The ``ai_task_bindings`` row for the task, if active.
2. The provider marked as default in Administración → Proveedores de IA.
3. A provider built straight from the environment, so a fresh deployment with an
   empty database still works against the host's Ollama.

Sampling parameters are layered on top of that choice; ``ai/parametros.py``
says in what order and why.

Measuring a configuration -- the golden set, the tuner -- needs to run the real
task against a provider, model and parameters that are *not* the configured
ones, without changing the configuration first. :func:`forzar` does that for
the duration of a ``with`` block, and only for the task it names.
"""
import contextvars
import logging
from contextlib import contextmanager
from dataclasses import dataclass

from flask import current_app

from app.models.ai import AIParameterProfile, AIProviderConfig, AITaskBinding
from app.models.enums import AIProviderCode, AITask
from app.services.ai import parametros as catalogo
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


@dataclass
class Forzado:
    tarea: str
    config: AIProviderConfig
    modelo: str = None
    #: Replaces the task's stored profile. None keeps the stored one.
    parametros: dict = None
    #: Prefixed to the run's ``finalidad``, so a measurement never reads as
    #: somebody's real request in the execution log.
    etiqueta: str = None


_FORZADO = contextvars.ContextVar('ia_forzado', default=None)


@contextmanager
def forzar(tarea, config, modelo=None, parametros=None, etiqueta=None):
    """Serve ``tarea`` from this provider, model and parameters for a while.

    A context variable rather than an argument threaded through every task
    function: what is measured must be the task exactly as it runs in
    production, and a second code path taking extra arguments would slowly
    stop being that.
    """
    token = _FORZADO.set(Forzado(
        tarea=str(AITask.coerce(tarea)), config=config, modelo=modelo,
        parametros=parametros, etiqueta=etiqueta,
    ))
    try:
        yield
    finally:
        _FORZADO.reset(token)


def forzado_para(tarea):
    """The override in force for this task, if any."""
    forzado = _FORZADO.get()
    if forzado is not None and forzado.tarea == str(AITask.coerce(tarea)):
        return forzado
    return None


def build_provider(config, modelo=None):
    """Instantiate the provider described by a configuration row.

    ``modelo`` is the model to send, when it is not the provider's default --
    a task bound to another model on the same endpoint. Without it the
    binding's model was recorded in ``ai_runs`` while the default one answered.
    """
    code = str(config.proveedor)
    provider_cls = PROVIDER_CLASSES.get(code)
    if provider_cls is None:
        raise AIError(f'Proveedor de IA desconocido: {code}')

    if provider_cls is OpenAICompatibleProvider:
        return provider_cls(config=config, codigo=code, modelo=modelo)
    if provider_cls is OllamaProvider:
        return provider_cls(config=config, modelo=modelo)
    return provider_cls(config=config)


def resolve(tarea):
    """Pick the provider, model and parameters for a task.

    Returns:
        ``(provider, modelo, parametros, binding)``. ``parametros`` holds the
        layers below the task (see ``ai/parametros.py``); the task's own
        layers are added by :func:`componer`.
    """
    tarea = AITask.coerce(tarea)

    forzado = forzado_para(tarea) if tarea is not None else None
    if forzado is not None:
        config = forzado.config
        modelo = forzado.modelo or config.modelo_por_defecto
        return (build_provider(config, modelo), modelo,
                _params(None, config, modelo), None)

    binding = None
    if tarea is not None:
        binding = AITaskBinding.query.filter_by(tarea=str(tarea), activo=True).first()

    if binding is not None and binding.provider_config is not None:
        config = binding.provider_config
        if config.activo:
            modelo = _model_for(binding, config)
            provider = build_provider(config, modelo)
            return provider, modelo, _params(binding, config, modelo), binding
        logger.warning(
            'El proveedor asignado a «%s» está desactivado; se usa el predeterminado.',
            tarea,
        )

    config = _default_config()
    if config is not None:
        modelo = _model_for(None, config)
        provider = build_provider(config, modelo)
        return provider, modelo, _params(None, config, modelo), None

    return _from_environment() + (None,)


def perfil(config, modelo, tarea=None):
    """The stored profile for this model, for one task or for all of them."""
    if config is None or getattr(config, 'id', None) is None or not modelo:
        return {}
    tarea = AITask.coerce(tarea)
    query = AIParameterProfile.query.filter_by(
        provider_config_id=config.id, modelo=modelo,
    )
    query = (query.filter(AIParameterProfile.tarea.is_(None)) if tarea is None
             else query.filter_by(tarea=str(tarea)))
    fila = query.first()
    return dict(fila.parametros or {}) if fila is not None else {}


def perfil_de_tarea(config, modelo, tarea):
    """The top layer: the task's profile, or what a measurement is trying."""
    forzado = forzado_para(tarea)
    if forzado is not None and forzado.parametros is not None:
        return dict(forzado.parametros)
    return perfil(config, modelo, tarea)


def capas(tarea, config, modelo, generales=None, pedidos=None):
    """Every layer, lowest first, as ``[(nombre, etiqueta, {clave: valor})]``.

    ``generales`` is what :func:`resolve` already merged, when the caller has
    it; ``pedidos`` is what the request itself asked for, which sits with the
    task's code defaults.
    """
    tarea = AITask.coerce(tarea)
    codigo = catalogo.DEFECTOS_POR_TAREA.get(str(tarea), {}) if tarea else {}
    resultado = []

    if generales is None:
        resultado.append(('sistema', 'Valor por defecto', {
            'max_tokens': catalogo.MAX_TOKENS_POR_DEFECTO,
            'temperatura': catalogo.TEMPERATURA_POR_DEFECTO,
        }))
        if config is not None:
            resultado.append(('proveedor', 'Proveedor', _del_proveedor(config)))
            resultado.append(('modelo', 'Modelo (todas las tareas)',
                              perfil(config, modelo)))
    else:
        resultado.append(('general', 'Proveedor y modelo', dict(generales)))

    resultado.append(('tarea', 'Lo que pide la tarea', {**codigo, **(pedidos or {})}))
    if tarea is not None:
        forzado = forzado_para(tarea)
        etiqueta = ('Ensayo' if forzado is not None and forzado.parametros is not None
                    else 'Perfil de la tarea')
        resultado.append(('perfil_tarea', etiqueta,
                          perfil_de_tarea(config, modelo, tarea)))
    return resultado


def componer(tarea, config, modelo, codigo_proveedor, generales=None, pedidos=None):
    """The parameters to send, and which layer each came from.

    Returns ``(efectivos, origen)``. Parameters the provider kind does not
    take are dropped here, so a model-wide ``top_k`` set on an Ollama endpoint
    never reaches an OpenAI task that happens to share the profile.
    """
    efectivos, origen = {}, {}
    for _nombre, etiqueta, valores in capas(tarea, config, modelo, generales, pedidos):
        for clave, valor in (valores or {}).items():
            if valor is None:
                continue
            efectivos[clave] = valor
            origen[clave] = etiqueta

    permitidos = catalogo.filtrar(efectivos, codigo_proveedor)
    for clave in ('max_tokens', 'temperatura'):
        if clave in efectivos:
            permitidos[clave] = efectivos[clave]
    return permitidos, {c: origen[c] for c in permitidos}


def explicar(tarea):
    """What a task would be sent right now, for the screens.

    Returns ``{proveedor, modelo, parametros: [(clave, etiqueta, valor, origen)]}``
    or None when no provider is configured at all.
    """
    try:
        provider, modelo, _generales, _binding = resolve(tarea)
    except AIError:
        return None
    config = getattr(provider, 'config', None)
    efectivos, origen = componer(tarea, config, modelo, provider.codigo)
    filas = [
        (spec.clave, spec.etiqueta, efectivos[spec.clave], origen[spec.clave])
        for spec in catalogo.CATALOGO if spec.clave in efectivos
    ]
    return {
        'proveedor': config.nombre if config is not None else provider.codigo,
        'config': config,
        'modelo': modelo,
        'parametros': filas,
    }


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


def _del_proveedor(config):
    """The provider row's own layer."""
    valores = dict(config.parametros_avanzados or {})
    if config.max_tokens:
        valores['max_tokens'] = int(config.max_tokens)
    if config.temperatura is not None:
        valores['temperatura'] = float(config.temperatura)
    return valores


def _params(binding, config, modelo=None):
    """The layers below the task: defaults, provider, model, binding."""
    valores = {
        'max_tokens': catalogo.MAX_TOKENS_POR_DEFECTO,
        'temperatura': catalogo.TEMPERATURA_POR_DEFECTO,
    }
    valores.update(_del_proveedor(config))
    valores.update(perfil(config, modelo))
    if binding is not None:
        if binding.max_tokens:
            valores['max_tokens'] = int(binding.max_tokens)
        if binding.temperatura is not None:
            valores['temperatura'] = float(binding.temperatura)
    return valores
