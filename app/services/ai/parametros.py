"""The sampling parameters an administrator may set, and where each one goes.

One catalogue, read by the forms, the validator, both providers and the tuner.
A parameter the catalogue does not know cannot be stored, and one a provider
family does not take is never sent to it: Ollama and an OpenAI-compatible
endpoint disagree on names (``repeat_penalty`` against ``repetition_penalty``)
and on what exists at all (OpenAI has no ``top_k``), and a request carrying a
parameter the endpoint does not know is refused whole.

Values are layered, lowest first:

1. the provider row -- its ``temperatura``, ``max_tokens`` and
   ``parametros_avanzados``;
2. the model's profile on that provider, for every task;
3. what the task itself asks for in code (:data:`DEFECTOS_POR_TAREA`);
4. the model's profile for that task -- which is what the tuner writes.

The order is the point. Extraction asks for temperature 0 because a document
must give the same answer twice; a model-wide «temperature 0.7» that somebody
copied from the model's card must not undo that. A profile written *for* the
task, by a person or by the tuner after measuring it, is the one thing that
may.
"""
from dataclasses import dataclass

from app.models.enums import AIProviderCode, AITask
from app.utils.errors import ValidationError

#: Families of wire format. DeepSeek and OpenAI speak the same dialect; vLLM
#: speaks it too and accepts the sampling extras on top.
OLLAMA = 'ollama'
OPENAI = 'openai'
VLLM = 'vllm'

_FAMILIA = {
    AIProviderCode.OLLAMA.value: OLLAMA,
    AIProviderCode.VLLM.value: VLLM,
    AIProviderCode.OPENAI.value: OPENAI,
    AIProviderCode.DEEPSEEK.value: OPENAI,
}

_TODAS = frozenset((OLLAMA, OPENAI, VLLM))


@dataclass(frozen=True)
class Parametro:
    clave: str
    etiqueta: str
    tipo: str  # 'float' | 'int' | 'lista'
    ayuda: str
    familias: frozenset = _TODAS
    minimo: float = None
    maximo: float = None
    paso: float = None
    #: Name on the wire, per family. Missing means the family calls it ``clave``.
    nombres: tuple = ()
    #: Values the tuner tries, in order. Empty means it never touches it.
    rejilla: tuple = ()
    #: Offered ticked in the tuning form.
    ajustar_por_defecto: bool = False

    def nombre_en(self, familia):
        return dict(self.nombres).get(familia, self.clave)

    def admite(self, familia):
        return familia in self.familias


CATALOGO = (
    Parametro(
        'temperatura', 'Temperatura', 'float',
        'Cuánto azar hay al elegir cada palabra. 0 da siempre la misma '
        'respuesta; conviene para extraer datos.',
        minimo=0, maximo=2, paso=0.05,
        nombres=((OLLAMA, 'temperature'), (OPENAI, 'temperature'), (VLLM, 'temperature')),
        rejilla=(0.0, 0.2, 0.4, 0.7, 1.0), ajustar_por_defecto=True,
    ),
    Parametro(
        'max_tokens', 'Máximo de tokens', 'int',
        'Longitud máxima de la respuesta. Un modelo que razona gasta parte '
        'pensando: si responde vacío, súbalo.',
        minimo=16, maximo=131072, paso=1,
        nombres=((OLLAMA, 'num_predict'),),
    ),
    Parametro(
        'top_p', 'Top-p', 'float',
        'Solo se eligen palabras dentro de esta probabilidad acumulada. '
        'Más bajo, más conservador.',
        minimo=0, maximo=1, paso=0.01,
        rejilla=(1.0, 0.95, 0.9, 0.8, 0.5), ajustar_por_defecto=True,
    ),
    Parametro(
        'top_k', 'Top-k', 'int',
        'Solo se eligen las k palabras más probables.',
        familias=frozenset((OLLAMA, VLLM)), minimo=1, maximo=500, paso=1,
        rejilla=(10, 20, 40, 80), ajustar_por_defecto=True,
    ),
    Parametro(
        'min_p', 'Min-p', 'float',
        'Descarta las palabras cuya probabilidad no llega a esta fracción de '
        'la más probable.',
        familias=frozenset((OLLAMA, VLLM)), minimo=0, maximo=1, paso=0.01,
        rejilla=(0.0, 0.05, 0.1),
    ),
    Parametro(
        'repeat_penalty', 'Penalización por repetición', 'float',
        'Mayor que 1 desanima repetir texto. Los modelos pequeños lo '
        'agradecen; demasiado alto les hace evitar palabras que sí tocan.',
        familias=frozenset((OLLAMA, VLLM)), minimo=0.5, maximo=2, paso=0.01,
        nombres=((VLLM, 'repetition_penalty'),),
        rejilla=(1.0, 1.05, 1.1, 1.2), ajustar_por_defecto=True,
    ),
    Parametro(
        'presence_penalty', 'Penalización por presencia', 'float',
        'Anima a tratar temas nuevos. Poco útil para extraer datos.',
        minimo=-2, maximo=2, paso=0.1, rejilla=(0.0, 0.5, 1.0),
    ),
    Parametro(
        'frequency_penalty', 'Penalización por frecuencia', 'float',
        'Desanima repetir la misma palabra muchas veces.',
        minimo=-2, maximo=2, paso=0.1, rejilla=(0.0, 0.5, 1.0),
    ),
    Parametro(
        'num_ctx', 'Ventana de contexto', 'int',
        'Tokens que el modelo lee de una vez. Ollama usa una ventana corta '
        'por defecto y trunca en silencio lo que no cabe: un documento largo '
        'pierde sus últimas páginas. Más ventana, más memoria.',
        familias=frozenset((OLLAMA,)), minimo=512, maximo=262144, paso=1,
        rejilla=(4096, 8192, 16384, 32768),
    ),
    Parametro(
        'seed', 'Semilla', 'int',
        'Fija el azar: con la misma semilla y la misma entrada, la misma '
        'salida. Útil para comparar.',
        minimo=0, maximo=2**31 - 1, paso=1,
    ),
    Parametro(
        'stop', 'Secuencias de parada', 'lista',
        'Texto que corta la respuesta al aparecer. Una por línea.',
    ),
)

POR_CLAVE = {p.clave: p for p in CATALOGO}

#: What each task asks for in code, before any profile. Kept here rather than
#: at each call site so the screens can show what a profile would override.
DEFECTOS_POR_TAREA = {
    AITask.EXTRACT_DOCUMENT.value: {'temperatura': 0.0},
    AITask.CLASSIFY_DOCUMENT.value: {'temperatura': 0.0, 'max_tokens': 256},
    AITask.ANSWER_TRIP_QUESTION.value: {'temperatura': 0.1},
    AITask.SUMMARIZE_TRIP.value: {'temperatura': 0.2},
    AITask.ANALYZE_RISKS.value: {'temperatura': 0.2},
    AITask.RESEARCH_PUBLIC_INFO.value: {'temperatura': 0.2},
    AITask.EXPLAIN_ALERT.value: {'temperatura': 0.2},
    AITask.PLAN_TRIP.value: {'temperatura': 0.3},
}

#: Used when nothing at any level says otherwise.
MAX_TOKENS_POR_DEFECTO = 2048
TEMPERATURA_POR_DEFECTO = 0.1


def familia(codigo):
    """The wire family of a provider code. The stub accepts everything."""
    return _FAMILIA.get(str(codigo), None)


def aplicables(codigo):
    """The parameters a provider of this kind takes, in catalogue order."""
    fam = familia(codigo)
    if fam is None:
        return list(CATALOGO)
    return [p for p in CATALOGO if p.admite(fam)]


def validar(parametros, codigo=None):
    """Clean and check a set of parameters before it is stored.

    Blank means «not set», so the endpoint's own default applies -- which is
    different from any value, and must stay representable. Unknown keys are
    refused rather than dropped: a typo that silently vanished would leave the
    administrator believing a setting took effect.

    ``codigo`` restricts to what that provider takes. A parameter it does not
    take is refused here, instead of being stored and quietly never sent.
    """
    limpios = {}
    admitidos = {p.clave for p in aplicables(codigo)} if codigo else set(POR_CLAVE)

    for clave, valor in (parametros or {}).items():
        spec = POR_CLAVE.get(clave)
        if spec is None:
            raise ValidationError(f'Parámetro desconocido: «{clave}».')
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            continue
        if clave not in admitidos:
            raise ValidationError(
                f'«{spec.etiqueta}» no lo admite este tipo de proveedor.'
            )
        limpios[clave] = _convertir(spec, valor)

    return limpios


def _convertir(spec, valor):
    if spec.tipo == 'lista':
        if isinstance(valor, str):
            valor = valor.splitlines()
        elementos = [str(v) for v in valor if str(v).strip()]
        if len(elementos) > 8:
            raise ValidationError(f'«{spec.etiqueta}»: como máximo 8.')
        return elementos

    try:
        numero = float(str(valor).replace(',', '.'))
    except ValueError as exc:
        raise ValidationError(f'«{spec.etiqueta}» debe ser un número.') from exc

    if spec.tipo == 'int':
        if numero != int(numero):
            raise ValidationError(f'«{spec.etiqueta}» debe ser un número entero.')
        numero = int(numero)

    if spec.minimo is not None and numero < spec.minimo:
        raise ValidationError(f'«{spec.etiqueta}» no puede ser menor que {spec.minimo}.')
    if spec.maximo is not None and numero > spec.maximo:
        raise ValidationError(f'«{spec.etiqueta}» no puede ser mayor que {spec.maximo}.')
    return numero


def desde_formulario(form, prefijo='p_'):
    """Read the catalogue's fields out of a submitted form."""
    return {
        p.clave: form.get(f'{prefijo}{p.clave}')
        for p in CATALOGO
        if form.get(f'{prefijo}{p.clave}') not in (None, '')
    }


def filtrar(parametros, codigo):
    """Drop what this provider kind does not take. Used at send time."""
    fam = familia(codigo)
    if fam is None:
        return dict(parametros)
    return {
        clave: valor for clave, valor in parametros.items()
        if clave in POR_CLAVE and POR_CLAVE[clave].admite(fam)
    }


def en_el_cable(parametros, codigo, excluir=()):
    """The parameters under the names this provider kind uses on the wire."""
    fam = familia(codigo) or OPENAI
    return {
        POR_CLAVE[clave].nombre_en(fam): valor
        for clave, valor in filtrar(parametros, codigo).items()
        if clave not in excluir
    }


def describir(parametros):
    """``[(etiqueta, valor)]`` for a screen, in catalogue order."""
    return [
        (p.etiqueta, parametros[p.clave])
        for p in CATALOGO if p.clave in (parametros or {})
    ]
