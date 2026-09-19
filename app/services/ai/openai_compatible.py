"""OpenAI-compatible provider, shared by vLLM, OpenAI and DeepSeek.

All three speak the same ``/chat/completions`` contract, so they differ only in
base URL, model and whether a key is needed -- which is exactly what
specification section 2.5 asks for when it calls vLLM "compatible con APIs tipo
OpenAI".
"""
import logging
import time

import httpx

from app.models.enums import EXTERNAL_AI_PROVIDERS, AIProviderCode
from app.services.ai.base import AIProvider, AIResponse
from app.utils.errors import AIError, TransientError

logger = logging.getLogger(__name__)

#: Defaults per provider code, used when the configuration leaves them blank.
DEFAULTS = {
    AIProviderCode.OPENAI.value: ('https://api.openai.com/v1', 'gpt-4o-mini'),
    AIProviderCode.DEEPSEEK.value: ('https://api.deepseek.com/v1', 'deepseek-chat'),
    AIProviderCode.VLLM.value: ('http://vllm:8000/v1', None),
}


class OpenAICompatibleProvider(AIProvider):
    """Talks to any OpenAI-compatible chat completions endpoint."""

    codigo = AIProviderCode.OPENAI.value

    def __init__(self, config=None, codigo=None, base_url=None, modelo=None,
                 api_key=None, timeout=120):
        super().__init__(config)
        self.codigo = codigo or getattr(config, 'proveedor', None) or self.codigo
        self.codigo = str(self.codigo)

        default_url, default_model = DEFAULTS.get(self.codigo, (None, None))
        self.base_url = (
            base_url or getattr(config, 'base_url', None) or default_url or ''
        ).rstrip('/')
        self._modelo = (
            modelo or getattr(config, 'modelo_por_defecto', None) or default_model
        )
        self._api_key = api_key
        self.timeout = getattr(config, 'timeout_segundos', None) or timeout

    @property
    def es_externo(self):
        """True for providers outside the organisation's perimeter."""
        return any(str(p) == self.codigo for p in EXTERNAL_AI_PROVIDERS)

    @property
    def modelo(self):
        return self._modelo

    def _key(self):
        """Decrypt the stored API key, never logging or returning it elsewhere."""
        if self._api_key:
            return self._api_key
        if self.config is not None and self.config.api_key_encrypted:
            from app.utils.crypto import decrypt_secret

            return decrypt_secret(self.config.api_key_encrypted)
        return None

    def complete(self, request):
        """Run a chat completion."""
        key = self._key()
        if self.es_externo and not key:
            raise AIError(
                f'No hay clave API configurada para el proveedor «{self.codigo}».'
            )

        payload = {
            'model': self._modelo,
            'messages': request.build_messages(),
            'temperature': float(request.temperatura),
            'max_tokens': int(request.max_tokens),
        }
        if request.esquema:
            payload['response_format'] = {'type': 'json_object'}

        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = f'Bearer {key}'

        start = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f'{self.base_url}/chat/completions',
                    json=payload, headers=headers,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise TransientError(
                f'El proveedor {self.codigo} no respondió en {self.timeout} s.'
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                raise AIError(
                    f'La clave API del proveedor «{self.codigo}» fue rechazada.'
                ) from exc
            if status == 429:
                raise TransientError(
                    f'El proveedor {self.codigo} ha limitado la tasa de peticiones.'
                ) from exc
            if status >= 500:
                raise TransientError(
                    f'El proveedor {self.codigo} devolvió {status}.'
                ) from exc
            raise AIError(
                f'El proveedor {self.codigo} devolvió {status}: '
                f'{_safe_detail(exc.response)}'
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientError(
                f'No se pudo contactar con {self.codigo}: {exc}'
            ) from exc

        choices = data.get('choices') or []
        content = choices[0].get('message', {}).get('content', '') if choices else ''
        usage = data.get('usage') or {}

        return AIResponse(
            contenido=content,
            modelo=data.get('model') or self._modelo,
            proveedor=self.codigo,
            tokens_entrada=usage.get('prompt_tokens'),
            tokens_salida=usage.get('completion_tokens'),
            duracion_ms=int((time.monotonic() - start) * 1000),
        )

    def health_check(self):
        """List the models the endpoint offers."""
        key = self._key()
        headers = {'Authorization': f'Bearer {key}'} if key else {}
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(f'{self.base_url}/models', headers=headers)
                response.raise_for_status()
                models = [m.get('id') for m in (response.json().get('data') or [])]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return False, 'clave API rechazada'
            return False, f'devolvió {exc.response.status_code}'
        except httpx.HTTPError as exc:
            return False, f'no accesible en {self.base_url}: {exc}'

        if self._modelo and models and self._modelo not in models:
            return True, (
                f'accesible, pero «{self._modelo}» no aparece entre los modelos '
                f'ofrecidos ({len(models)} disponibles)'
            )
        return True, f'accesible ({len(models)} modelos)'


def _safe_detail(response):
    """A short error detail; never echo a whole body, which may contain the prompt."""
    try:
        body = response.json()
        error = body.get('error')
        if isinstance(error, dict):
            return str(error.get('message'))[:200]
        return str(error or body)[:200]
    except Exception:
        return str(response.status_code)


class StubProvider(AIProvider):
    """Deterministic provider for the test suite.

    Never reaches the network. Returns a schema-shaped empty answer so tests can
    exercise the whole pipeline -- authorisation, audit, reconciliation -- without
    a model, and without a test silently passing because a real model happened to
    be reachable.
    """

    codigo = AIProviderCode.STUB.value
    es_externo = False

    #: Tests set this to control what the next call returns.
    respuesta = None

    def complete(self, request):
        import json

        if self.respuesta is not None:
            content = (
                self.respuesta if isinstance(self.respuesta, str)
                else json.dumps(self.respuesta, ensure_ascii=False)
            )
        elif request.esquema:
            content = json.dumps(_empty_for_schema(request.esquema), ensure_ascii=False)
        else:
            content = 'Respuesta simulada.'

        return AIResponse(
            contenido=content,
            modelo='stub',
            proveedor=self.codigo,
            tokens_entrada=0,
            tokens_salida=0,
            duracion_ms=0,
        )

    def health_check(self):
        return True, 'proveedor simulado'


def _empty_for_schema(schema):
    """Build a minimal object that actually satisfies a schema.

    Type-correct rather than all-null: a required string filled with ``None``
    fails validation, which would make every test of the surrounding pipeline
    fail for a reason that has nothing to do with what it is testing.
    """
    if schema.get('type') != 'object':
        return {}

    result = {}
    properties = schema.get('properties') or {}
    for name in schema.get('required', []):
        result[name] = _placeholder(properties.get(name, {}))
    return result


def _placeholder(spec):
    """A valid value for one schema property."""
    kind = spec.get('type')
    if isinstance(kind, list):
        kind = next((k for k in kind if k != 'null'), 'string')

    if kind == 'array':
        # Respect minItems: an extraction must describe at least one service,
        # and an empty list would fail the very contract this is standing in for.
        minimo = int(spec.get('minItems') or 0)
        items = spec.get('items') or {}
        return [_placeholder(items) for _ in range(minimo)]
    if kind == 'object':
        return _empty_for_schema(spec)
    if kind == 'number':
        return 0.0
    if kind == 'integer':
        return 0
    if kind == 'boolean':
        return False
    if spec.get('enum'):
        return spec['enum'][0]
    return 'Respuesta simulada para pruebas.'
