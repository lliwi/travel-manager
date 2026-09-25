"""Ollama provider (specification section 2.5: local deployments first).

In the reference deployment Ollama runs on the host rather than in a container,
reached at ``host.docker.internal:11434``.
"""
import logging
import time

import httpx

from app.models.enums import AIProviderCode
from app.services.ai.base import AIProvider, AIResponse
from app.services.ai.parametros import (
    MAX_TOKENS_POR_DEFECTO,
    TEMPERATURA_POR_DEFECTO,
    en_el_cable,
)
from app.services.ai.schemas import esquema_de_generacion
from app.utils import http
from app.utils.errors import AIError, TransientError

logger = logging.getLogger(__name__)

#: What each model can do, as ``/api/show`` reported it. Kept for the process:
#: a model's capabilities do not change between two requests, and asking on
#: every call would add a round trip to every extraction.
_CAPACIDADES = {}


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
        max_tokens = request.max_tokens or MAX_TOKENS_POR_DEFECTO
        temperatura = (TEMPERATURA_POR_DEFECTO if request.temperatura is None
                       else request.temperatura)
        opciones = en_el_cable(
            request.parametros or {}, self.codigo, excluir=('max_tokens', 'temperatura'),
        )
        opciones.update({
            'temperature': float(temperatura),
            'num_predict': int(max_tokens),
        })
        payload = {
            'model': self._modelo,
            'messages': request.build_messages(),
            'stream': False,
            'options': opciones,
        }
        if request.esquema:
            # Ollama constrains decoding to a JSON schema, not merely to valid
            # JSON. That distinction matters: asked only for "json", a small
            # model answers `{}` -- which is valid and useless. Given the
            # schema, the fields it must produce are forced to exist.
            payload['format'] = _formato(request.esquema)

            # A schema-bound answer is a transcription, and deliberation makes
            # it worse. Measured on a real booking confirmation, a reasoning
            # model spent 244 s against 51 s with thinking off, and used them
            # to argue itself from the document's 2026 to an invented 2023.
            # Models that cannot think ignore the flag.
            payload['think'] = False
        elif self._sin_razonamiento() and self.admite_razonamiento():
            # Fuera de una respuesta con esquema solo se pide si el modelo
            # razona: «think» en uno que no lo hace es un parámetro que sobra,
            # y algunos servidores rechazan la petición entera por él.
            payload['think'] = False

        start = time.monotonic()
        try:
            with http.cliente(timeout=self.timeout) as client:
                response = client.post(f'{self.base_url}/api/chat', json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise TransientError(
                f'Ollama no respondió en {int(time.monotonic() - start)} s.'
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

        message = (data.get('message') or {}).get('content') or ''
        if not message.strip():
            # Lo mismo que en el proveedor compatible con OpenAI: «no devolvió
            # contenido» nombra el síntoma y no la causa, que está en la
            # respuesta y se estaba tirando.
            if data.get('done_reason') == 'length':
                raise AIError(
                    f'El modelo «{self._modelo}» se quedó sin espacio para '
                    f'responder: agotó los {max_tokens} tokens de '
                    f'salida. Suba «Máximo de tokens» en Administración → '
                    f'Proveedores de IA, en el proveedor o en el perfil de '
                    f'la tarea.'
                )
            raise AIError(
                f'El modelo «{self._modelo}» no devolvió contenido'
                + (f' (motivo: {data["done_reason"]}).'
                   if data.get('done_reason') else '.')
            )

        return AIResponse(
            contenido=message,
            modelo=data.get('model') or self._modelo,
            proveedor=self.codigo,
            tokens_entrada=data.get('prompt_eval_count'),
            tokens_salida=data.get('eval_count'),
            duracion_ms=int((time.monotonic() - start) * 1000),
        )

    def _sin_razonamiento(self):
        return bool(getattr(self.config, 'sin_razonamiento', False))

    def admite_razonamiento(self):
        """Whether this model can reason at all, as Ollama itself reports it.

        ``/api/show`` lists the model's capabilities, and «thinking» is there
        only for the ones that do. That is the whole reason to ask instead of
        keeping a list of model names: a list is wrong the day somebody pulls a
        model nobody had heard of.

        On a failure it answers False -- not asking for something is always
        safe, and refusing to run because the capability query failed would
        turn a slow answer into no answer.
        """
        if self._modelo in _CAPACIDADES:
            return 'thinking' in _CAPACIDADES[self._modelo]

        try:
            with http.cliente(timeout=10) as client:
                response = client.post(
                    f'{self.base_url}/api/show', json={'model': self._modelo},
                )
                response.raise_for_status()
                capacidades = response.json().get('capabilities') or []
        except (httpx.HTTPError, ValueError) as exc:
            logger.info(
                'No se pudieron consultar las capacidades de «%s»: %s',
                self._modelo, exc,
            )
            return False

        _CAPACIDADES[self._modelo] = capacidades
        return 'thinking' in capacidades

    def list_models(self):
        """The models pulled on this Ollama."""
        try:
            with http.cliente(timeout=15) as client:
                response = client.get(f'{self.base_url}/api/tags')
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise AIError(
                f'No se pudo consultar los modelos de Ollama en {self.base_url}: {exc}'
            ) from exc

        nombres = [m.get('name', '') for m in (data.get('models') or [])]
        return sorted(n for n in nombres if n)

    def health_check(self):
        """Check that Ollama answers and holds the configured model."""
        try:
            models = self.list_models()
        except AIError as exc:
            return False, str(exc)

        if not self._modelo:
            return True, f'accesible ({len(models)} modelos disponibles)'

        # Ollama reports 'llama3.1:8b'; a configured 'llama3.1' should match.
        if any(m == self._modelo or m.startswith(f'{self._modelo}:') for m in models):
            return True, f'accesible, modelo «{self._modelo}» disponible'

        return False, (
            f'accesible, pero el modelo «{self._modelo}» no está descargado. '
            f'Ejecute: ollama pull {self._modelo}'
        )


def _safe_detail(response):
    """A short error detail that never echoes a whole response body."""
    try:
        body = response.json()
        return str(body.get('error') or body)[:200]
    except Exception:
        return response.text[:200] if response.text else str(response.status_code)


def _formato(esquema):
    """The schema Ollama constrains decoding to, or plain JSON if there is none."""
    return esquema_de_generacion(esquema) or 'json'
