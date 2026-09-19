"""Ollama provider (specification section 2.5: local deployments first).

In the reference deployment Ollama runs on the host rather than in a container,
reached at ``host.docker.internal:11434``.
"""
import logging
import time

import httpx

from app.models.enums import AIProviderCode
from app.services.ai.base import AIProvider, AIResponse
from app.utils.errors import AIError, TransientError

logger = logging.getLogger(__name__)


class OllamaProvider(AIProvider):
    """Talks to Ollama's native chat endpoint."""

    codigo = AIProviderCode.OLLAMA.value
    es_externo = False

    def __init__(self, config=None, base_url=None, modelo=None, timeout=120):
        super().__init__(config)
        self.base_url = (
            base_url
            or getattr(config, 'base_url', None)
            or 'http://host.docker.internal:11434'
        ).rstrip('/')
        self._modelo = modelo or getattr(config, 'modelo_por_defecto', None)
        self.timeout = getattr(config, 'timeout_segundos', None) or timeout

    @property
    def modelo(self):
        return self._modelo

    def complete(self, request):
        """Run a chat completion."""
        payload = {
            'model': self._modelo,
            'messages': request.build_messages(),
            'stream': False,
            'options': {
                'temperature': float(request.temperatura),
                'num_predict': int(request.max_tokens),
            },
        }
        if request.esquema:
            # Ollama constrains decoding to a JSON schema, not merely to valid
            # JSON. That distinction matters: asked only for "json", a small
            # model answers `{}` -- which is valid and useless. Given the
            # schema, the fields it must produce are forced to exist.
            payload['format'] = _decoding_schema(request.esquema)

        start = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(f'{self.base_url}/api/chat', json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise TransientError(
                f'Ollama no respondió en {self.timeout} s.'
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = _safe_detail(exc.response)
            if exc.response.status_code in (404,):
                raise AIError(
                    f'El modelo «{self._modelo}» no está disponible en Ollama. '
                    f'Descárguelo con: ollama pull {self._modelo}'
                ) from exc
            if exc.response.status_code >= 500:
                raise TransientError(f'Ollama devolvió {exc.response.status_code}.') from exc
            raise AIError(f'Ollama devolvió un error: {detail}') from exc
        except httpx.HTTPError as exc:
            raise TransientError(
                f'No se pudo contactar con Ollama en {self.base_url}: {exc}'
            ) from exc

        message = (data.get('message') or {}).get('content', '')
        return AIResponse(
            contenido=message,
            modelo=data.get('model') or self._modelo,
            proveedor=self.codigo,
            tokens_entrada=data.get('prompt_eval_count'),
            tokens_salida=data.get('eval_count'),
            duracion_ms=int((time.monotonic() - start) * 1000),
        )

    def health_check(self):
        """Check that Ollama answers and holds the configured model."""
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(f'{self.base_url}/api/tags')
                response.raise_for_status()
                models = [
                    m.get('name', '') for m in (response.json().get('models') or [])
                ]
        except httpx.HTTPError as exc:
            return False, f'No accesible en {self.base_url}: {exc}'

        if not self._modelo:
            return True, f'accesible ({len(models)} modelos disponibles)'

        # Ollama reports 'llama3.1:8b'; a configured 'llama3.1' should match.
        if any(m == self._modelo or m.startswith(f'{self._modelo}:') for m in models):
            return True, f'accesible, modelo «{self._modelo}» disponible'

        return False, (
            f'accesible, pero el modelo «{self._modelo}» no está descargado. '
            f'Ejecute: ollama pull {self._modelo}'
        )


def _decoding_schema(esquema):
    """Reduce a validation schema to what the model should actually generate.

    Our extraction schemas wrap the fields in an envelope of confidences and
    provenance. Asking a model to fill all that in is both slow -- the grammar
    for the full nested schema takes far longer to apply than the answer is
    worth -- and pointless: a small model's self-reported confidence is a guess,
    while whether a value appears literally in the document is something we can
    check ourselves.

    So the model is asked for the fields, flat, and the envelope is rebuilt
    afterwards. Measured against a real booking email on a 4B model, this is
    the difference between an answer in under two seconds and one that does not
    arrive.
    """
    if not isinstance(esquema, dict):
        return 'json'

    propiedades = (esquema.get('properties') or {})
    campos = propiedades.get('campos')

    if isinstance(campos, dict) and campos.get('properties'):
        plano = {
            'type': 'object',
            'properties': _strip_for_grammar(campos['properties']),
        }
        # Every field required, nulls allowed: a model that must emit the key
        # says "null" for what it cannot find instead of quietly omitting it,
        # and an omitted field is indistinguishable from one it never looked for.
        plano['required'] = sorted(plano['properties'])
        return plano

    return _strip_for_grammar(esquema)


def _strip_for_grammar(node):
    """Remove what constrained decoding cannot express.

    Anything dropped here is still checked by the validator once the answer is
    back, so nothing is lost; leaving it in risks Ollama rejecting the schema
    outright and falling back to unconstrained generation.
    """
    if isinstance(node, dict):
        limpio = {}
        for clave, valor in node.items():
            if clave in ('additionalProperties', 'description'):
                continue
            limpio[clave] = _strip_for_grammar(valor)

        if limpio.get('type') == 'object' and not limpio.get('properties'):
            limpio.pop('required', None)
        return limpio

    if isinstance(node, list):
        return [_strip_for_grammar(v) for v in node]

    return node


def _safe_detail(response):
    """A short error detail that never echoes a whole response body."""
    try:
        body = response.json()
        return str(body.get('error') or body)[:200]
    except Exception:
        return response.text[:200] if response.text else str(response.status_code)
