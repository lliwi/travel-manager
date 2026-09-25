"""Measuring the model instead of arguing about it.

Two halves, and they answer different questions.

«¿Está funcionando?» is answered from ``ai_runs``: every execution already
records its provider, model, task, duration, tokens and outcome, so the failure
rate and the latency of a model in this deployment are there to be read. That
half needs no new data and covers every model that has ever run.

«¿Es correcto?» cannot be answered that way, because nothing in a run says
whether the answer was right. It needs documents whose correct extraction
somebody wrote down once -- a golden set -- and then it is arithmetic. Without
one, changing model is a leap of faith dressed as a configuration change, which
is what it has been so far.

The same measurement is what the tuner optimises (``autotune_service``), so it
covers every task that has cases: extraction and classification from the
documents, and the generative tasks from ``data/evaluacion/tareas/<tarea>/``,
whose cases state what a good answer must and must not say.
"""
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

from app.extensions import db
from app.models.ai import AIRun
from app.models.enums import AIRunState, AITask
from app.utils.errors import AIError
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Where the golden set lives. Documents plus the fields their extraction
#: should produce, kept in the repository so a regression is visible in a diff.
CONJUNTO_DORADO = Path(__file__).resolve().parent.parent.parent / 'data' / 'evaluacion'


# ======================================================================
# From what already happened
# ======================================================================
def rendimiento_por_modelo(dias=30):
    """How each model has behaved lately, worst failure rate first.

    Read from the executions that already happened, so it costs nothing and
    covers every model that has run here -- including the one somebody swapped
    in last week without telling anybody.
    """
    from datetime import timedelta

    desde = utcnow() - timedelta(days=dias)
    filas = (
        db.session.query(
            AIRun.proveedor,
            AIRun.modelo,
            AIRun.tarea,
            AIRun.estado,
            db.func.count(AIRun.id).label('total'),
            db.func.avg(AIRun.duracion_ms).label('duracion_media'),
            db.func.sum(AIRun.tokens_entrada).label('tokens_entrada'),
            db.func.sum(AIRun.tokens_salida).label('tokens_salida'),
        )
        .filter(AIRun.created_at >= desde)
        .group_by(AIRun.proveedor, AIRun.modelo, AIRun.tarea, AIRun.estado)
        .all()
    )

    por_modelo = {}
    for proveedor, modelo, tarea, estado, total, duracion, entrada, salida in filas:
        clave = (proveedor, modelo)
        resumen = por_modelo.setdefault(clave, {
            'proveedor': proveedor, 'modelo': modelo,
            'total': 0, 'completadas': 0, 'errores': 0, 'bloqueadas': 0,
            'duracion_ms': 0, 'tokens_entrada': 0, 'tokens_salida': 0,
            'tareas': set(),
        })
        resumen['total'] += total
        resumen['tareas'].add(str(tarea))
        resumen['tokens_entrada'] += int(entrada or 0)
        resumen['tokens_salida'] += int(salida or 0)
        # Weighted by how many runs each state had, or a single slow failure
        # would count as much as a hundred fast successes.
        resumen['duracion_ms'] += float(duracion or 0) * total

        if estado is AIRunState.COMPLETADA:
            resumen['completadas'] += total
        elif estado is AIRunState.ERROR:
            resumen['errores'] += total
        elif estado is AIRunState.BLOQUEADA:
            resumen['bloqueadas'] += total

    resultado = []
    for resumen in por_modelo.values():
        total = resumen['total'] or 1
        resumen['duracion_media_ms'] = round(resumen.pop('duracion_ms') / total)
        resumen['tasa_error'] = round(resumen['errores'] * 100 / total, 1)
        resumen['tareas'] = sorted(resumen['tareas'])
        resultado.append(resumen)

    return sorted(resultado, key=lambda r: (-r['tasa_error'], -r['total']))


# ======================================================================
# Against documents whose answer we know
# ======================================================================
def casos_dorados():
    """The evaluation cases on disk, as ``(nombre, documento, esperado)``."""
    if not CONJUNTO_DORADO.is_dir():
        return []

    casos = []
    for ruta in sorted(CONJUNTO_DORADO.glob('*.json')):
        try:
            caso = json.loads(ruta.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            logger.warning('Caso de evaluación ilegible %s: %s', ruta.name, exc)
            continue

        documento = CONJUNTO_DORADO / caso.get('documento', '')
        if not documento.is_file():
            logger.warning(
                'El caso %s apunta a un documento que no está: %s',
                ruta.name, caso.get('documento'),
            )
            continue
        casos.append((ruta.stem, documento, caso))

    return casos


def comparar(esperado, obtenido):
    """Field by field, what matched and what did not.

    Compared as text, case-folded and trimmed: «BCN» and «bcn » are the same
    airport, and a golden set that fails on whitespace teaches people to ignore
    it.
    """
    aciertos, fallos, ausentes = [], [], []

    for campo, valor_esperado in (esperado or {}).items():
        valor = obtenido.get(campo) if isinstance(obtenido, dict) else None
        if isinstance(valor, dict):
            valor = valor.get('local') or valor.get('codigo') or valor

        if valor in (None, '', {}, []):
            ausentes.append(campo)
        elif str(valor).strip().casefold() == str(valor_esperado).strip().casefold():
            aciertos.append(campo)
        else:
            fallos.append({'campo': campo, 'esperado': valor_esperado, 'obtenido': valor})

    total = len(esperado or {})
    return {
        'aciertos': aciertos,
        'fallos': fallos,
        'ausentes': ausentes,
        'total': total,
        'porcentaje': round(len(aciertos) * 100 / total) if total else 0,
    }


def evaluar(modelo=None, casos=None):
    """Run the golden set and score it.

    ``modelo`` overrides the configured one for the duration, which is the
    point: the question is «¿es mejor este que el que tenemos?», and answering
    it must not require changing the deployment first.
    """
    # «is None», no «or»: una lista vacía es «no evalúes nada», y tratarla
    # como «no me diste ninguna» haría que un caller que filtró sus casos
    # acabara midiendo el conjunto entero sin pedirlo.
    if casos is None:
        casos = casos_dorados()
    if not casos:
        return {'casos': [], 'modelo': modelo, 'total': 0, 'porcentaje': 0}

    resultados = []
    inicio_total = time.monotonic()

    with _con_modelo(AITask.EXTRACT_DOCUMENT, modelo):
        for nombre, ruta, caso in casos:
            resultados.append(_extraer_y_comparar(nombre, ruta, caso))

    aciertos = sum(len(r['aciertos']) for r in resultados)
    total = sum(r['total'] for r in resultados)

    return {
        'modelo': modelo,
        'casos': resultados,
        'total': total,
        'aciertos': aciertos,
        'porcentaje': round(aciertos * 100 / total) if total else 0,
        'duracion_s': round(time.monotonic() - inicio_total, 1),
    }


def _extraer_y_comparar(nombre, ruta, caso):
    """One extraction case, scored field by field."""
    from app.services import ai_service

    documento = _documento_temporal(ruta, caso)
    inicio = time.monotonic()
    run = None
    try:
        salida = ai_service.extract_document(
            documento, clasificacion=caso.get('clasificacion'),
        )
        run = salida.get('run')
        servicios = (salida.get('datos') or {}).get('servicios') or []
        campos = (servicios[0].get('campos') if servicios else {}) or {}
        error = None
    except SoftTimeLimitExceeded:
        # The worker telling the step its time is up. Scored as a failed case,
        # it would be swallowed, and the search would run on to the hard
        # limit and die without a word.
        raise
    except Exception as exc:
        servicios, campos, error = [], {}, str(exc)[:200]

    comparacion = comparar(caso.get('esperado'), campos)
    comparacion.update({
        'caso': nombre,
        'error': error,
        'duracion_s': round(time.monotonic() - inicio, 1),
        'servicios_esperados': caso.get('servicios_esperados', 1),
        'servicios_obtenidos': len(servicios) if not error else 0,
        'tokens': _tokens(run),
    })
    return comparacion


@contextmanager
def _con_modelo(tarea, modelo):
    """Serve ``tarea`` from another model on the provider that serves it now."""
    if not modelo:
        yield
        return

    from app.services.ai import forzar, resolve

    provider, _modelo, _p, _b = resolve(tarea)
    config = getattr(provider, 'config', None)
    if config is None:
        raise AIError(
            'Para probar otro modelo tiene que haber un proveedor configurado '
            'en Administración → Proveedores de IA.'
        )
    with forzar(tarea, config, modelo=modelo, etiqueta='Evaluación'):
        yield


# ======================================================================
# Any task: what the tuner optimises
# ======================================================================
#: Where the cases of the generative tasks live, one folder per task.
CASOS_POR_TAREA = CONJUNTO_DORADO / 'tareas'

#: Tasks that can be measured, and what a case of each is made of.
EVALUABLES = {
    AITask.EXTRACT_DOCUMENT.value: 'Documentos del conjunto dorado, campo a campo.',
    AITask.CLASSIFY_DOCUMENT.value: 'Documentos del conjunto dorado, por su tipo.',
    AITask.ANSWER_TRIP_QUESTION.value: 'Preguntas sobre un viaje de ejemplo.',
    AITask.SUMMARIZE_TRIP.value: 'Viajes de ejemplo que resumir.',
    AITask.EXPLAIN_ALERT.value: 'Alertas de ejemplo que explicar.',
}


def casos_de(tarea):
    """The cases a task is measured against, as ``[(nombre, caso)]``.

    Empty for a task nobody wrote cases for -- which the tuner refuses rather
    than «optimising» against nothing.
    """
    tarea = str(AITask.coerce(tarea))
    if tarea in (AITask.EXTRACT_DOCUMENT.value, AITask.CLASSIFY_DOCUMENT.value):
        return [
            (nombre, dict(caso, _ruta=ruta)) for nombre, ruta, caso in casos_dorados()
        ]
    if tarea not in EVALUABLES:
        return []

    carpeta = CASOS_POR_TAREA / tarea
    if not carpeta.is_dir():
        return []
    casos = []
    for ruta in sorted(carpeta.glob('*.json')):
        try:
            casos.append((ruta.stem, json.loads(ruta.read_text(encoding='utf-8'))))
        except (OSError, ValueError) as exc:
            logger.warning('Caso de evaluación ilegible %s: %s', ruta.name, exc)
    return casos


def medir(tarea, casos=None, repeticiones=1):
    """Run a task's cases once or several times and reduce them to a score.

    Returns ``{calidad, duracion_ms, tokens, errores, llamadas, casos}``, with
    ``calidad`` from 0 to 100. A case that fails scores 0 rather than being
    left out: a setting that makes the model crash on one document in five is
    worse, not equally good on fewer documents.

    Whatever provider, model and parameters are in force are the ones
    measured -- see ``ai.selector.forzar``.
    """
    tarea = str(AITask.coerce(tarea))
    if casos is None:
        casos = casos_de(tarea)

    por_caso, duraciones, tokens, errores = [], [], 0, 0
    for nombre, caso in casos:
        notas = []
        for _ in range(max(1, int(repeticiones))):
            inicio = time.monotonic()
            calidad, gastados, error = _medir_caso(tarea, caso)
            duraciones.append((time.monotonic() - inicio) * 1000)
            tokens += gastados or 0
            if error:
                errores += 1
            notas.append((calidad, error))
        por_caso.append({
            'caso': nombre,
            'calidad': round(sum(n for n, _ in notas) / len(notas), 1),
            'error': next((e for _, e in notas if e), None),
        })

    llamadas = len(duraciones)
    return {
        'calidad': (round(sum(c['calidad'] for c in por_caso) / len(por_caso), 1)
                    if por_caso else 0.0),
        'duracion_ms': round(sum(duraciones) / llamadas) if llamadas else 0,
        'tokens': tokens,
        'errores': errores,
        'llamadas': llamadas,
        'casos': por_caso,
    }


def _medir_caso(tarea, caso):
    """``(calidad 0-100, tokens, error)`` for one call on one case."""
    if tarea == AITask.EXTRACT_DOCUMENT.value:
        r = _extraer_y_comparar('', caso['_ruta'], caso)
        # How many services came out counts as one more field: a round trip
        # read as a single flight is half an itinerary, however exact.
        aciertos = len(r['aciertos']) + (
            1 if r['servicios_obtenidos'] == r['servicios_esperados'] else 0
        )
        total = r['total'] + 1
        calidad = 0.0 if r['error'] else aciertos * 100 / total
        return calidad, r['tokens'], r['error']

    try:
        datos, run = _ejecutar_caso(tarea, caso)
    except SoftTimeLimitExceeded:
        # The worker telling the step its time is up. Scored as a failed case,
        # it would be swallowed, and the search would run on to the hard
        # limit and die without a word.
        raise
    except Exception as exc:
        return 0.0, 0, str(exc)[:200]

    if tarea == AITask.CLASSIFY_DOCUMENT.value:
        acierta = str(datos.get('clasificacion')) == str(caso.get('clasificacion'))
        return (100.0 if acierta else 0.0), _tokens(run), None

    return puntuar_criterios(datos, caso.get('criterios') or {}), _tokens(run), None


def _ejecutar_caso(tarea, caso):
    """Send one case exactly as production would. Returns ``(datos, run)``."""
    from app.services import ai_service
    from app.services.ai.schemas import SCHEMAS

    if tarea == AITask.CLASSIFY_DOCUMENT.value:
        salida = ai_service.classify_document(_documento_temporal(caso['_ruta'], caso))
        return salida['datos'], salida['run']

    referencia = f'caso-{caso.get("referencia", "ejemplo")}'
    if tarea == AITask.ANSWER_TRIP_QUESTION.value:
        request = ai_service.peticion_de_consulta(
            caso['datos'], referencia, caso['pregunta'],
        )
    elif tarea == AITask.SUMMARIZE_TRIP.value:
        request = ai_service.peticion_de_resumen(caso['datos'], referencia)
    elif tarea == AITask.EXPLAIN_ALERT.value:
        request = ai_service.peticion_de_explicacion(caso['alerta'], referencia)
    else:
        raise AIError(f'La tarea «{tarea}» no tiene forma de evaluarse.')

    response, run = ai_service._run(
        tarea, request, finalidad=f'Caso de evaluación «{referencia}»',
    )
    return ai_service._parse(response, tarea, SCHEMAS[tarea]), run


def puntuar_criterios(datos, criterios):
    """Score a generative answer against what its case says it must contain.

    Deterministic on purpose. Asking another model whether the answer is good
    would make the score as noisy as the thing being scored, and would let a
    model grade its own family. What can be checked literally -- the flight
    number is there, the invented hotel is not, «datos insuficientes» is
    admitted when it should be -- is checked literally.
    """
    texto = json.dumps(datos, ensure_ascii=False).casefold()
    comprobaciones = []

    for aguja in criterios.get('debe_mencionar') or []:
        comprobaciones.append(str(aguja).casefold() in texto)
    for aguja in criterios.get('no_debe_mencionar') or []:
        comprobaciones.append(str(aguja).casefold() not in texto)
    for campo, esperado in (criterios.get('campos') or {}).items():
        comprobaciones.append(datos.get(campo) == esperado)

    if not comprobaciones:
        # A schema-valid answer is all the case asked for.
        return 100.0
    return round(sum(comprobaciones) * 100 / len(comprobaciones), 1)


def _tokens(run):
    if run is None:
        return 0
    return int(run.tokens_entrada or 0) + int(run.tokens_salida or 0)


def _documento_temporal(ruta, caso):
    """A Document-shaped object the extractor can read, never persisted.

    Evaluation must not leave rows behind: a golden set run is a measurement,
    and a measurement that changes what it measures is not one.
    """
    from types import SimpleNamespace

    texto = ruta.read_text(encoding='utf-8', errors='replace')
    pagina = SimpleNamespace(numero=1, contenido=texto)


    return SimpleNamespace(
        id=None,
        nombre_original=ruta.name,
        clasificacion=caso.get('clasificacion', 'otro'),
        trip=None,
        texto=SimpleNamespace(contenido=texto),
        pages=[pagina],
    )
