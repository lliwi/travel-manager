"""Data egress policy (specification section 2.5).

"Política explícita de salida de datos: por defecto, documentos y PII no salen a
proveedores externos sin habilitación administrativa."

The rule is enforced here, once, before any request leaves the process. A task
whose payload contains document text or personal data is refused for an external
provider unless an administrator has explicitly enabled that class of egress, and
the refusal is recorded as a blocked ``ai_runs`` row rather than silently
downgraded -- a caller must never be able to believe the model saw data it did
not, nor have data leave because a check was skipped.
"""
import logging
import re

from app.models.enums import SENSITIVE_AI_TASKS, AITask
from app.utils.errors import AIPolicyBlocked

logger = logging.getLogger(__name__)


class EgressDecision:
    """Whether a request may be sent to a given provider."""

    __slots__ = ('permitido', 'motivo')

    def __init__(self, permitido, motivo=None):
        self.permitido = permitido
        self.motivo = motivo

    def __bool__(self):
        return self.permitido


def check(request, provider, tarea=None):
    """Decide whether this request may reach this provider.

    Returns:
        An :class:`EgressDecision`.
    """
    if not getattr(provider, 'es_externo', False):
        return EgressDecision(True, 'proveedor local')

    from app.services import settings_service

    tarea = AITask.coerce(tarea or request.tarea)

    if request.contiene_documentos or tarea in SENSITIVE_AI_TASKS:
        if not settings_service.get_bool('IA_PERMITIR_DOCUMENTOS_EXTERNOS', False):
            return EgressDecision(False, (
                'La política de la organización no permite enviar contenido de '
                'documentos a proveedores externos. Habilítelo en Administración '
                '→ Ajustes si la organización lo autoriza expresamente.'
            ))

    if request.contiene_pii:
        if not settings_service.get_bool('IA_PERMITIR_PII_EXTERNOS', False):
            return EgressDecision(False, (
                'La política de la organización no permite enviar datos personales '
                'a proveedores externos.'
            ))

    return EgressDecision(True, 'salida autorizada por configuración')


def enforce(request, provider, tarea=None):
    """Raise :class:`AIPolicyBlocked` unless the request may be sent."""
    decision = check(request, provider, tarea)
    if not decision.permitido:
        logger.warning(
            'Salida de datos bloqueada hacia %s para la tarea %s: %s',
            getattr(provider, 'codigo', '?'), tarea or request.tarea, decision.motivo,
        )
        raise AIPolicyBlocked(decision.motivo)
    return decision


# ======================================================================
# Output validation
# ======================================================================
def validate_schema(data, schema):
    """Check a model's answer against the expected JSON schema.

    A structurally invalid answer is treated as a contract failure rather than
    coerced: silently accepting a half-shaped object is how an injected payload
    ends up in the database.
    """
    try:
        import jsonschema

        jsonschema.validate(instance=data, schema=schema)
        return True, None
    except ImportError:
        return _validate_minimal(data, schema)
    except Exception as exc:
        return False, str(exc).split('\n')[0][:300]


def _validate_minimal(data, schema):
    """Fallback check when jsonschema is unavailable: types and required keys."""
    if schema.get('type') == 'object':
        if not isinstance(data, dict):
            return False, 'Se esperaba un objeto JSON.'
        for name in schema.get('required', []):
            if name not in data:
                return False, f'Falta el campo obligatorio «{name}».'
    return True, None


#: Patterns whose presence in a model's free-text answer suggests the model
#: echoed an injected instruction rather than analysing the document.
_SUSPICIOUS = (
    re.compile(r'(?i)ignor[ae]\s+(las\s+)?instrucciones\s+(anteriores|previas)'),
    re.compile(r'(?i)ignore\s+(all\s+)?(previous|prior)\s+instructions'),
    re.compile(r'(?i)system\s*prompt'),
    re.compile(r'(?i)(revela|muestra)\s+(tus|las)\s+instrucciones'),
)


def looks_injected(text):
    """True when an answer contains traces of a prompt-injection attempt.

    A heuristic, not a control: the real defence is that untrusted content is
    delimited and the answer is schema-constrained. This exists so an attempt
    can be logged and the run flagged for review.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SUSPICIOUS)


def validate_references(datos, autorizadas, campo='referencias'):
    """Drop any internal reference the model invented or borrowed elsewhere.

    Specification flow 5.4: the answer must not include information from other
    trips or other travellers. The context builder only ever hands over
    authorised records, so any id outside that set is a hallucination -- or an
    attempt to make one look real -- and is removed.

    Returns:
        ``(datos_filtrados, descartadas)``.
    """
    if not isinstance(datos, dict):
        return datos, []

    permitted = {str(r) for r in (autorizadas or [])}
    references = datos.get(campo)
    if not isinstance(references, list):
        return datos, []

    kept, dropped = [], []
    for reference in references:
        identifier = (
            reference.get('id') if isinstance(reference, dict) else str(reference)
        )
        if identifier and str(identifier) in permitted:
            kept.append(reference)
        else:
            dropped.append(reference)

    if dropped:
        logger.warning(
            'Se descartaron %s referencias no autorizadas de una respuesta de IA.',
            len(dropped),
        )
        datos = dict(datos)
        datos[campo] = kept

    return datos, dropped
