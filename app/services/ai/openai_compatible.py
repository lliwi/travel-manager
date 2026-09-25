"""OpenAI-compatible provider, shared by vLLM, OpenAI and DeepSeek.

All three speak the same ``/chat/completions`` contract, so they differ only in
base URL, model and whether a key is needed -- which is exactly what
specification section 2.5 asks for when it calls vLLM "compatible con APIs tipo
OpenAI".
"""
import logging
import re
import time

import httpx

from app.models.enums import EXTERNAL_AI_PROVIDERS, AIProviderCode
from app.services.ai.base import AIProvider, AIResponse
from app.services.ai.parametros import (
    MAX_TOKENS_POR_DEFECTO,
    TEMPERATURA_POR_DEFECTO,
    en_el_cable,
)
from app.utils import http
from app.utils.errors import AIError, TransientError

logger = logging.getLogger(__name__)

#: What «no reasoning» is called where the parameter exists. OpenAI takes
#: a level rather than a switch, and «minimal» is the floor it accepts:
#: there is no «none».
_ESFUERZO_MINIMO = 'minimal'

#: The two names the output-length limit goes by.
_OTRO_PARAMETRO = {
    'max_tokens': 'max_completion_tokens',
    'max_completion_tokens': 'max_tokens',
}

#: Attempts at correcting one request before giving up: the token limit, the
#: temperature, and each optional parameter an administrator may have set.
_MAX_CORRECCIONES = 10

#: Payload keys that are the request itself, never an optional parameter an
#: endpoint may be allowed to drop.
_ESENCIALES = frozenset((
    'model', 'messages', 'response_format', 'max_tokens', 'max_completion_tokens',
    'temperature', 'reasoning_effort',
))


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

    # Optional parameters before temperature: «top_p is not supported with
    # temperature» names both, and the one to drop is the one we added.
    for nombre in sorted(set(payload) - _ESENCIALES, key=len, reverse=True):
        # Whole words only: «stop» must not match «stopped», nor «seed» a
        # sentence about something that «exceeded» a limit.
        if re.search(rf'(?<![a-z_]){re.escape(nombre.lower())}(?![a-z_])', texto):
            payload.pop(nombre)
            return {'no_admitidos': [nombre]}

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

        max_tokens = request.max_tokens or MAX_TOKENS_POR_DEFECTO
        temperatura = (TEMPERATURA_POR_DEFECTO if request.temperatura is None
                       else request.temperatura)
        rechazados = set(self.rechazados())
        payload = {
            nombre: valor
            for nombre, valor in en_el_cable(
                request.parametros or {}, self.codigo,
                excluir=('max_tokens', 'temperatura'),
            ).items()
            if nombre not in rechazados and nombre not in _ESENCIALES
        }
        payload.update({
            'model': self._modelo,
            'messages': request.build_messages(),
            self._token_param(): int(max_tokens),
        })
        if not self._ajustes().get('sin_temperatura'):
            payload['temperature'] = float(temperatura)
        if request.esquema:
            payload['response_format'] = {'type': 'json_object'}
        if self._pedir_sin_razonar():
            payload['reasoning_effort'] = _ESFUERZO_MINIMO

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
            if status == 400 and 'reasoning_effort' in payload:
                detalle = _safe_detail(exc.response) or ''
                if 'reasoning_effort' in detalle:
                    # No hay forma de preguntarle a un endpoint compatible con
                    # OpenAI qué parámetros admite —«/models/{id}» devuelve el
                    # identificador y poco más—, así que se descubre una vez,
                    # se anota y no se vuelve a enviar. Sin esto, marcar la
                    # casilla rompería todas las llamadas a ese proveedor.
                    self._anotar_rechazo('sin_razonamiento_no_admitido')
                    payload.pop('reasoning_effort')
                    return self._reintentar(payload, headers, start)
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
        primera = choices[0] if choices else {}
        content = (primera.get('message') or {}).get('content') or ''
        usage = data.get('usage') or {}

        if not content.strip():
            _explicar_respuesta_vacia(self.codigo, self._modelo, primera, usage)

        return AIResponse(
            contenido=content,
            modelo=data.get('model') or self._modelo,
            proveedor=self.codigo,
            tokens_entrada=usage.get('prompt_tokens'),
            tokens_salida=usage.get('completion_tokens'),
            duracion_ms=int((time.monotonic() - start) * 1000),
        )

    def _pedir_sin_razonar(self):
        """Whether to ask this endpoint not to reason.

        Two conditions: an administrator asked for it, and this endpoint has
        not already refused the parameter.
        """
        if not getattr(self.config, 'sin_razonamiento', False):
            return False
        return not self._ajustes().get('sin_razonamiento_no_admitido')

    def _anotar_rechazo(self, clave):
        """Remember that this endpoint will not take a parameter.

        Committed on its own: the point is that the next call does not repeat
        the mistake, and losing the note to an unrelated rollback would repeat
        it for ever.
        """
        from app.extensions import db

        if self.config is None:
            return
        try:
            parametros = dict(self.config.parametros or {})
            parametros[clave] = True
            self.config.parametros = parametros
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.warning('No se pudo anotar el rechazo de «%s».', clave)

    def _reintentar(self, payload, headers, start):
        """One more attempt, with the refused parameter removed."""
        with http.cliente(timeout=self.timeout) as client:
            response = self._post(client, payload, headers)
            response.raise_for_status()
            data = response.json()

        choices = data.get('choices') or []
        primera = choices[0] if choices else {}
        content = (primera.get('message') or {}).get('content') or ''
        usage = data.get('usage') or {}
        if not content.strip():
            _explicar_respuesta_vacia(self.codigo, self._modelo, primera, usage)

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

    def rechazados(self):
        """Wire names of the optional parameters this model has refused."""
        return list((self._ajustes().get('no_admitidos') or {}).get(self._modelo) or ())

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

        for _ in range(_MAX_CORRECCIONES + 1):
            response = client.post(url, json=payload, headers=headers)
            if response.status_code != 400:
                if aprendido:
                    self._recordar(aprendido)
                return response

            detalle = _safe_detail(response)
            arreglo = _corregir(payload, detalle, self._token_param())
            if not arreglo:
                return response
            if 'no_admitidos' in arreglo:
                arreglo['no_admitidos'] = sorted(
                    set(aprendido.get('no_admitidos', ())) | set(arreglo['no_admitidos'])
                )
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
            if 'no_admitidos' in ajustes:
                # Per model: one endpoint serves several, and what o3 refuses
                # gpt-4o-mini may take. Now that a task's model is the one
                # actually sent, sharing the list would silently strip a
                # setting from the model that accepts it.
                por_modelo = dict(parametros.get('no_admitidos') or {})
                por_modelo[self._modelo] = sorted(
                    set(por_modelo.get(self._modelo) or ())
                    | set(ajustes['no_admitidos'])
                )
                ajustes = dict(ajustes, no_admitidos=por_modelo)
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


def _explicar_respuesta_vacia(codigo, modelo, choice, usage):
    """Say why the answer came back empty, in terms of what to change.

    An empty answer used to surface as «el modelo no devolvió contenido», which
    is true and useless: it names a symptom and no cause. The cause is in the
    response and was being thrown away.

    The one that actually happens: a reasoning model spends the whole output
    budget thinking and never gets to write. The trip planner sends a long
    prompt -- it now carries real flights and hotels -- so 2048 tokens go on
    reasoning and the answer is cut off before its first character. Nothing is
    broken and no setting looks wrong; it just stops working when the prompt
    grows.
    """
    motivo = choice.get('finish_reason') or choice.get('native_finish_reason')
    detalles = usage.get('completion_tokens_details') or {}
    razonamiento = detalles.get('reasoning_tokens')
    gastados = usage.get('completion_tokens')

    if motivo == 'length':
        gasto = ''
        if razonamiento:
            gasto = (f' Gastó {razonamiento} de los {gastados} tokens de salida '
                     f'razonando, y no le quedaron para responder.')
        elif gastados:
            gasto = f' Agotó los {gastados} tokens de salida.'
        raise AIError(
            f'El modelo «{modelo}» se quedó sin espacio para responder.{gasto} '
            f'Suba «Máximo de tokens» en Administración → Proveedores de IA '
            f'(en el proveedor o en el perfil de la tarea), o asigne esta '
            f'tarea a un modelo que no razone.'
        )

    if motivo == 'content_filter':
        raise AIError(
            f'El proveedor «{codigo}» bloqueó la respuesta por su filtro de '
            f'contenido.'
        )

    raise AIError(
        f'El modelo «{modelo}» no devolvió contenido'
        + (f' (motivo: {motivo}).' if motivo else '.')
    )


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
