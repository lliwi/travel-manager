"""Administering AI providers from the interface.

Specification section 2.5 requires provider and model to be selectable per
environment and per task, and API keys to be stored encrypted and never
exposed. Both are administrative decisions, so they live in the database and
are changed from the panel -- not in the deployment's environment, where
changing a model would mean a redeploy and where a key would sit in plain text.
"""
import logging

from app.extensions import db
from app.models.ai import AIProviderConfig, AITaskBinding
from app.models.enums import (
    EXTERNAL_AI_PROVIDERS,
    AIProviderCode,
    AITask,
    AuditResourceType,
)
from app.services import audit_service
from app.utils.crypto import decrypt_secret, encrypt_secret, secret_hint
from app.utils.errors import ConflictError, ResourceNotFound, ValidationError

logger = logging.getLogger(__name__)

#: Suggested endpoint and model per provider, offered in the form so an
#: administrator does not have to remember them. Only a starting point: both
#: are editable.
SUGERENCIAS = {
    AIProviderCode.OLLAMA.value: {
        'base_url': 'http://host.docker.internal:11434',
        'modelo': 'llama3.1:8b',
        'ayuda': (
            'Ollama se ejecuta en el equipo anfitrión, fuera de Docker. '
            'Descargue el modelo con: ollama pull llama3.1:8b'
        ),
    },
    AIProviderCode.VLLM.value: {
        'base_url': 'http://vllm:8000/v1',
        'modelo': '',
        'ayuda': 'Servidor vLLM autogestionado, compatible con la API de OpenAI.',
    },
    AIProviderCode.OPENAI.value: {
        'base_url': 'https://api.openai.com/v1',
        'modelo': 'gpt-4o-mini',
        'ayuda': (
            'Proveedor externo: requiere clave API. Por defecto no se le envían '
            'documentos ni datos personales.'
        ),
    },
    AIProviderCode.DEEPSEEK.value: {
        'base_url': 'https://api.deepseek.com/v1',
        'modelo': 'deepseek-chat',
        'ayuda': (
            'Proveedor externo: requiere clave API. Por defecto no se le envían '
            'documentos ni datos personales.'
        ),
    },
}


def list_providers():
    """Every configured provider, the default first."""
    return AIProviderConfig.query.order_by(
        AIProviderConfig.es_por_defecto.desc(),
        AIProviderConfig.nombre,
    ).all()


def get_or_404(provider_id):
    """Fetch a provider or raise."""
    import uuid

    try:
        config = db.session.get(AIProviderConfig, uuid.UUID(str(provider_id)))
    except (ValueError, TypeError):
        config = None
    if config is None:
        raise ResourceNotFound('El proveedor indicado no existe.')
    return config


def default_provider():
    """The provider serving tasks with no binding of their own."""
    return (
        AIProviderConfig.query.filter_by(es_por_defecto=True, activo=True).first()
        or AIProviderConfig.query.filter_by(activo=True)
        .order_by(AIProviderConfig.created_at.asc())
        .first()
    )


def create(actor, nombre, proveedor, base_url=None, modelo=None, api_key=None,
           activo=True, por_defecto=False, timeout=120, max_tokens=2048,
           temperatura=0.1, sin_razonamiento=False, commit=True):
    """Configure a new provider."""
    proveedor = AIProviderCode.coerce(proveedor)
    if proveedor is None:
        raise ValidationError('El proveedor indicado no es válido.')

    if AIProviderConfig.query.filter_by(nombre=nombre.strip()).first():
        raise ConflictError(f'Ya existe un proveedor llamado «{nombre}».')

    es_externo = proveedor in EXTERNAL_AI_PROVIDERS
    if es_externo and not api_key:
        raise ValidationError(
            f'El proveedor «{proveedor.label}» es externo y requiere una clave API.'
        )

    sugerencia = SUGERENCIAS.get(proveedor.value, {})
    config = AIProviderConfig(
        nombre=nombre.strip(),
        proveedor=proveedor,
        base_url=(base_url or sugerencia.get('base_url') or '').strip() or None,
        modelo_por_defecto=(modelo or sugerencia.get('modelo') or '').strip() or None,
        activo=bool(activo),
        timeout_segundos=timeout,
        max_tokens=max_tokens,
        temperatura=temperatura,
        sin_razonamiento=bool(sin_razonamiento),
        creado_por_id=getattr(actor, 'id', None),
    )
    _set_api_key(config, api_key)

    db.session.add(config)
    db.session.flush()

    # The first provider configured becomes the default; otherwise nothing
    # would answer until an administrator remembered to pick one.
    if por_defecto or AIProviderConfig.query.filter_by(es_por_defecto=True).count() == 0:
        _make_default(config)

    if commit:
        db.session.commit()
        _audit(actor, 'ai_provider.created', config)
    return config


def update(actor, config, nombre=None, base_url=None, modelo=None, api_key=None,
           activo=None, por_defecto=None, timeout=None, max_tokens=None,
           temperatura=None, sin_razonamiento=None, commit=True):
    """Change a provider's configuration.

    ``api_key`` left empty keeps the stored one: the form never renders the
    key, so a blank field means "unchanged", not "remove".
    """
    cambios = []

    if nombre and nombre.strip() != config.nombre:
        if AIProviderConfig.query.filter(
            AIProviderConfig.nombre == nombre.strip(),
            AIProviderConfig.id != config.id,
        ).first():
            raise ConflictError(f'Ya existe un proveedor llamado «{nombre}».')
        config.nombre = nombre.strip()
        cambios.append('nombre')

    for campo, valor in (
        ('base_url', base_url),
        ('modelo_por_defecto', modelo),
        ('timeout_segundos', timeout),
        ('max_tokens', max_tokens),
        ('temperatura', temperatura),
    ):
        if valor is None:
            continue
        nuevo = valor.strip() or None if isinstance(valor, str) else valor
        if getattr(config, campo) != nuevo:
            setattr(config, campo, nuevo)
            cambios.append(campo)

    if api_key:
        _set_api_key(config, api_key)
        cambios.append('api_key')

    if sin_razonamiento is not None and bool(sin_razonamiento) != config.sin_razonamiento:
        config.sin_razonamiento = bool(sin_razonamiento)
        cambios.append('sin_razonamiento')
        # Lo que el endpoint rechazó se olvida al cambiar el ajuste: puede
        # haberse cambiado también el modelo, y arrastrar un «no» de otro
        # modelo dejaría la casilla marcada sin efecto para siempre.
        if config.parametros and 'sin_razonamiento_no_admitido' in config.parametros:
            restantes = dict(config.parametros)
            restantes.pop('sin_razonamiento_no_admitido')
            config.parametros = restantes or None

    if activo is not None and bool(activo) != config.activo:
        config.activo = bool(activo)
        cambios.append('activo')
        if not config.activo and config.es_por_defecto:
            # A deactivated default would leave every task without a provider.
            config.es_por_defecto = False
            _promote_another(exclude=config)

    if por_defecto:
        if not config.activo:
            raise ValidationError(
                'Un proveedor desactivado no puede ser el predeterminado.'
            )
        _make_default(config)
        cambios.append('es_por_defecto')

    if commit:
        db.session.commit()
        if cambios:
            _audit(actor, 'ai_provider.updated', config, {'campos': sorted(set(cambios))})
    return config


def delete(actor, config, commit=True):
    """Remove a provider.

    Refused while a task binding points at it: silently dropping the binding
    would leave that task falling back to a different model without anyone
    being told.
    """
    usos = AITaskBinding.query.filter(
        db.or_(
            AITaskBinding.provider_config_id == config.id,
            AITaskBinding.fallback_config_id == config.id,
        )
    ).count()
    if usos:
        raise ConflictError(
            f'No se puede eliminar: hay {usos} tarea(s) asignada(s) a este '
            'proveedor. Reasígnelas primero.'
        )

    nombre = config.nombre
    era_por_defecto = config.es_por_defecto

    db.session.delete(config)
    db.session.flush()

    if era_por_defecto:
        _promote_another()

    if commit:
        db.session.commit()
        audit_service.record(
            'ai_provider.deleted',
            recurso_tipo=AuditResourceType.CONFIGURACION,
            actor=actor,
            metadatos={'nombre': nombre},
        )
    return True


def set_default(actor, config, commit=True):
    """Make this provider the one used when a task has no binding."""
    if not config.activo:
        raise ValidationError(
            'Active el proveedor antes de marcarlo como predeterminado.'
        )
    _make_default(config)
    if commit:
        db.session.commit()
        _audit(actor, 'ai_provider.set_default', config)
    return config


# ======================================================================
# Task bindings (specification section 2.5: selection per task)
# ======================================================================
def list_bindings():
    """Every task, with the provider serving it."""
    asignadas = {b.tarea: b for b in AITaskBinding.query.all()}
    return [(tarea, asignadas.get(tarea)) for tarea in AITask]


def set_binding(actor, tarea, config, modelo=None, activo=True, commit=True):
    """Bind a task to a provider, or clear the binding when ``config`` is None."""
    tarea = AITask.coerce(tarea)
    if tarea is None:
        raise ValidationError('La tarea indicada no es válida.')

    binding = AITaskBinding.query.filter_by(tarea=str(tarea)).first()

    if config is None:
        if binding is not None:
            db.session.delete(binding)
            if commit:
                db.session.commit()
                _audit_binding(actor, 'ai_binding.cleared', tarea, None)
        return None

    # Read the foreign key before the row joins the session: touching an
    # expired attribute triggers an autoflush, which would try to insert the
    # binding while its provider is still NULL.
    provider_id = config.id

    if binding is None:
        binding = AITaskBinding(
            tarea=tarea,
            provider_config_id=provider_id,
            modelo=(modelo or '').strip() or None,
            activo=bool(activo),
        )
        db.session.add(binding)
    else:
        binding.provider_config_id = provider_id
        binding.modelo = (modelo or '').strip() or None
        binding.activo = bool(activo)

    if commit:
        db.session.commit()
        _audit_binding(actor, 'ai_binding.set', tarea, config)
    return binding


# ======================================================================
# Helpers
# ======================================================================
def available_models(config):
    """The models this provider's endpoint currently offers.

    Asking the endpoint beats trusting a name someone typed: a model that was
    renamed or never pulled only shows up otherwise when a document is already
    in the pipeline and the extraction fails.

    Returns ``(modelos, error)``. Both surfaces need to tell an endpoint that
    offers nothing from one that could not be asked, so the failure is returned
    rather than raised or swallowed.
    """
    from app.services.ai import build_provider

    try:
        provider = build_provider(config)
        return provider.list_models(), None
    except Exception as exc:
        logger.warning(
            'No se pudieron listar los modelos de %s: %s', config.nombre, exc
        )
        mensaje = getattr(exc, 'mensaje', None) or str(exc)
        return [], mensaje[:300]


def _set_api_key(config, api_key):
    """Encrypt and store the key, keeping only a hint in clear."""
    if not api_key:
        return
    config.api_key_encrypted = encrypt_secret(api_key)
    config.api_key_pista = secret_hint(api_key)


def _make_default(config):
    """Mark one provider as the default, clearing the rest."""
    AIProviderConfig.query.filter(
        AIProviderConfig.id != config.id
    ).update({'es_por_defecto': False}, synchronize_session=False)
    config.es_por_defecto = True


def _promote_another(exclude=None):
    """Pick a new default when the current one is removed or deactivated."""
    query = AIProviderConfig.query.filter_by(activo=True)
    if exclude is not None:
        query = query.filter(AIProviderConfig.id != exclude.id)

    candidato = query.order_by(AIProviderConfig.created_at.asc()).first()
    if candidato is not None:
        candidato.es_por_defecto = True
        logger.info(
            'El proveedor «%s» pasa a ser el predeterminado.', candidato.nombre
        )
    else:
        logger.warning(
            'No queda ningún proveedor de IA activo; las funciones de IA fallarán.'
        )
    return candidato


def _audit(actor, accion, config, extra=None):
    metadatos = {
        'nombre': config.nombre,
        'proveedor': str(config.proveedor),
        'modelo': config.modelo_por_defecto,
        'es_externo': config.es_externo,
        'es_por_defecto': config.es_por_defecto,
    }
    if extra:
        metadatos.update(extra)
    audit_service.record(
        accion,
        recurso_tipo=AuditResourceType.CONFIGURACION,
        recurso_id=str(config.id),
        actor=actor,
        metadatos=metadatos,
    )


def _audit_binding(actor, accion, tarea, config):
    audit_service.record(
        accion,
        recurso_tipo=AuditResourceType.CONFIGURACION,
        actor=actor,
        metadatos={
            'tarea': str(tarea),
            'proveedor': config.nombre if config else None,
        },
    )


def has_key(config):
    """True when a usable key is stored. Never returns the key itself."""
    return bool(config.api_key_encrypted) and decrypt_secret(
        config.api_key_encrypted
    ) is not None
