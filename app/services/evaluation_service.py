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
"""
import json
import logging
import time
from pathlib import Path

from app.extensions import db
from app.models.ai import AIRun
from app.models.enums import AIRunState
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
    from app.services import ai_service

    # «is None», no «or»: una lista vacía es «no evalúes nada», y tratarla
    # como «no me diste ninguna» haría que un caller que filtró sus casos
    # acabara midiendo el conjunto entero sin pedirlo.
    if casos is None:
        casos = casos_dorados()
    if not casos:
        return {'casos': [], 'modelo': modelo, 'total': 0, 'porcentaje': 0}

    resultados = []
    inicio_total = time.monotonic()

    for nombre, ruta, caso in casos:
        documento = _documento_temporal(ruta, caso)
        inicio = time.monotonic()
        try:
            salida = ai_service.extract_document(
                documento, clasificacion=caso.get('clasificacion'),
            )
            servicios = (salida.get('datos') or {}).get('servicios') or []
            campos = (servicios[0].get('campos') if servicios else {}) or {}
            error = None
        except Exception as exc:
            campos, error = {}, str(exc)[:200]

        comparacion = comparar(caso.get('esperado'), campos)
        comparacion.update({
            'caso': nombre,
            'error': error,
            'duracion_s': round(time.monotonic() - inicio, 1),
            'servicios_esperados': caso.get('servicios_esperados', 1),
            'servicios_obtenidos': len(servicios) if not error else 0,
        })
        resultados.append(comparacion)

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
