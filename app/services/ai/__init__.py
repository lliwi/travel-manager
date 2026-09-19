"""AI provider layer (specification sections 2.5 and 6)."""
import logging

from app.services.ai.base import (
    AIProvider,
    AIRequest,
    AIResponse,
    UntrustedBlock,
)
from app.services.ai.guard import (
    check as check_egress,
)
from app.services.ai.guard import (
    enforce as enforce_egress,
)
from app.services.ai.guard import (
    looks_injected,
    validate_references,
    validate_schema,
)
from app.services.ai.ollama import OllamaProvider
from app.services.ai.openai_compatible import OpenAICompatibleProvider, StubProvider
from app.services.ai.selector import build_provider, fallback_for, resolve

logger = logging.getLogger(__name__)

__all__ = [
    'AIProvider', 'AIRequest', 'AIResponse', 'UntrustedBlock',
    'OllamaProvider', 'OpenAICompatibleProvider', 'StubProvider',
    'resolve', 'build_provider', 'fallback_for',
    'check_egress', 'enforce_egress', 'validate_schema', 'validate_references',
    'looks_injected', 'health_check', 'health_check_all',
]


def health_check(config):
    """Check one configured provider. Returns ``(ok, detalle)``."""
    from app.extensions import db
    from app.utils.timeutil import utcnow

    try:
        provider = build_provider(config)
        ok, detail = provider.health_check()
    except Exception as exc:
        ok, detail = False, str(exc)[:200]

    config.ultimo_chequeo_en = utcnow()
    config.ultimo_chequeo_ok = ok
    config.ultimo_chequeo_detalle = detail[:500] if detail else None
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    return ok, detail


def health_check_all():
    """Check every configured provider, plus the environment default.

    Returns:
        ``{nombre: (ok, detalle)}``.
    """
    from flask import current_app

    from app.models.ai import AIProviderConfig

    results = {}
    for config in AIProviderConfig.query.filter_by(activo=True).all():
        results[config.nombre] = health_check(config)

    if not results:
        # Nothing configured yet: report on what the environment would use, so
        # `flask ai-health` is useful on a fresh install.
        try:
            provider, modelo, _, _ = resolve(None)
            ok, detail = provider.health_check()
            label = f'{provider.codigo} (entorno)'
            results[label] = (ok, f'{detail}; modelo «{modelo}»')
        except Exception as exc:
            results['configuración'] = (False, str(exc)[:200])

    return results
