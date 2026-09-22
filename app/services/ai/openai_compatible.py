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
from app.utils import http
from app.utils.errors import AIError, TransientError

logger = logging.getLogger(__name__)

#: The two names the output-length limit goes by.
_OTRO_PARAMETRO = {
    'max_tokens': 'max_completion_tokens',
    'max_completion_tokens': 'max_tokens',
}

#: Parameters an endpoint may refuse, and what to do about each. Order matters
#: only in that each is tried once.
_CORRECCIONES = ('max_tokens', 'temperature')


def _corregir(payload, detalle, token_param):
    """Adjust the payload for a parameter the endpoint rejected.

    Returns what was learned, or None when the 400 is about something else --
    an unknown model, a malformed request -- which is a real error and must not
    be retried into silence.
    """
    texto = (detalle or '').lower()

    if 'max_tokens' in texto or 'max_completion_tokens' in texto:
        alternativa = _OTRO_PARAMETRO.get(token_param)
        if alternativa and token_param in payload:
            payload[alternativa] = payload.pop(token_param)
            return {'token_param': alternativa}

    if 'temperature' in texto and 'temperature' in payload:
        # Dropped rather than set to the value it demands: the default is the
        # only thing it accepts, and naming it here would be a second guess.
        payload.pop('temperature')
        return {'sin_temperatura': True}

    return None

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
            self._token_param(): int(request.max_tokens),
        }
        if not self._ajustes().get('sin_temperatura'):
            payload['temperature'] = float(request.temperatura)
        if request.esquema:
            payload['response_format'] = {'type': 'json_object'}

        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = f'Bearer {key}'

        start = time.monotonic()
        try:
            with http.cliente(timeout=self.timeout) as client:
                response = self._post(client, payload, headers)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise TransientError(
                f'El proveedor {self.codigo} no respondió en '
                f'{int(time.monotonic() - start)} s.'
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

    def _ajustes(self):
        """What this endpoint has already told us it will not accept."""
        return getattr(self.config, 'parametros', None) or {}

    def _token_param(self):
        """Which name this endpoint gives the output-length limit.

        The field was renamed: newer OpenAI models reject «max_tokens» outright
        and want «max_completion_tokens», while most self-hosted
        OpenAI-compatible servers only know the original.
        """
        return self._ajustes().get('token_param') or 'max_tokens'

    def _post(self, client, payload, headers):
        """Send the completion, learning what this endpoint refuses.

        An OpenAI-compatible endpoint is a family, not a contract: one model
        renames the token limit, the next accepts only its default temperature.
        Guessing from the model name would need editing every time a family
        appears, so the endpoint is asked, and what it answered is remembered on
        its configuration -- the wasted round trip happens once, not per
        document.
        """
        url = f'{self.base_url}/chat/completions'
        aprendido = {}

        for _ in range(len(_CORRECCIONES) + 1):
            response = client.post(url, json=payload, headers=headers)
            if response.status_code != 400:
                if aprendido:
                    self._recordar(aprendido)
                return response

            detalle = _safe_detail(response)
            arreglo = _corregir(payload, detalle, self._token_param())
            if not arreglo:
                return response
            aprendido.update(arreglo)
            logger.info(
                'El endpoint %s rechazó un parámetro (%s); se reintenta.',
                self.codigo, ', '.join(arreglo),
            )

        return response

    def _recordar(self, ajustes):
        """Persist what this endpoint accepts, so the retry is not repeated."""
        if self.config is None:
            return
        try:
            from app.extensions import db

            parametros = dict(self.config.parametros or {})
            parametros.update(ajustes)
            self.config.parametros = parametros
            db.session.commit()
        except Exception:
            # Not worth failing an answer we already have.
            logger.warning('No se pudo recordar la compatibilidad.', exc_info=True)

    def list_models(self):
        """The models this endpoint offers."""
        key = self._key()
        headers = {'Authorization': f'Bearer {key}'} if key else {}
        try:
            with http.cliente(timeout=15) as client:
                response = client.get(f'{self.base_url}/models', headers=headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise AIError('La clave API fue rechazada por el endpoint.') from exc
            raise AIError(
                f'El endpoint devolvió {exc.response.status_code} al pedir los modelos.'
            ) from exc
        except httpx.HTTPError as exc:
            raise AIError(
                f'No se pudo consultar los modelos en {self.base_url}: {exc}'
            ) from exc

        nombres = [m.get('id') for m in (data.get('data') or [])]
        return sorted(n for n in nombres if n)

    def health_check(self):
        """Reachable, and offering the configured model."""
        try:
            models = self.list_models()
        except AIError as exc:
            return False, str(exc)

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

    def list_models(self):
        return ['stub-small', 'stub-large']

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
