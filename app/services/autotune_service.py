"""Searching for the sampling parameters that serve a task best.

The search is a greedy coordinate descent over a small grid: measure what is
configured now, then for each parameter try each value of its grid, keeping a
change only when it measurably helps. Nothing cleverer, on purpose:

* **Every trial is expensive.** One trial is every evaluation case through a
  real model; a local model takes tens of seconds per document. A method that
  needs hundreds of samples to beat a grid is a method that never finishes.
* **Every decision has to be explainable.** The row keeps every trial, and
  «it moved top_p to 0.9 because quality went from 71 to 80» is something an
  administrator can check. A surrogate model's opinion is not.

What «better» means is fixed: quality first, by more than
:data:`TOLERANCIA` points, since the same setting scores differently on two
runs; at equal or higher quality, clearly faster. A setting that is faster
and slightly worse never wins -- that trade is somebody's decision, not the
tuner's.

The result is a proposal. It becomes the task's profile when an administrator
applies it, or on its own only if whoever launched the search asked for that
*and* the gain clears :data:`MEJORA_MINIMA_PARA_APLICAR`. Either way it is
audited, and the previous profile is kept so it can be put back.
"""
import logging

from celery.exceptions import SoftTimeLimitExceeded

from app.extensions import db
from app.models.ai import AIAutotuneRun, AIParameterProfile
from app.models.enums import (
    AIAutotuneState,
    AIParameterOrigin,
    AITask,
    AuditResourceType,
)
from app.services import audit_service, evaluation_service
from app.services.ai import parametros as catalogo
from app.utils.errors import ConflictError, ResourceNotFound, ValidationError
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Quality points two measurements may differ by and still count as equal.
TOLERANCIA = 1.0
#: How much faster a setting must be to win on speed alone.
MEJORA_DE_VELOCIDAD = 0.85
#: Gain, in quality points, below which an automatic run does not apply itself.
MEJORA_MINIMA_PARA_APLICAR = 2.0

PRESUPUESTO_POR_DEFECTO = 12
PRESUPUESTO_MAXIMO = 40
REPETICIONES_MAXIMAS = 5


# ======================================================================
# What can be tuned
# ======================================================================
def ajustables(config):
    """The parameters the tuner may search for this provider, in order."""
    return [p for p in catalogo.aplicables(config.proveedor) if p.rejilla]


def espacio_para(config, claves=None):
    """``[[clave, [valores]]]`` to search. ``claves`` None takes the defaults.

    A list rather than a mapping because the order is the search order, and
    PostgreSQL's JSONB does not keep the order of an object's keys.
    """
    disponibles = ajustables(config)
    if claves is None:
        elegidos = [p for p in disponibles if p.ajustar_por_defecto]
    else:
        por_clave = {p.clave: p for p in disponibles}
        desconocidas = [c for c in claves if c not in por_clave]
        if desconocidas:
            raise ValidationError(
                'Este proveedor no admite ajustar: ' + ', '.join(desconocidas) + '.'
            )
        elegidos = [p for p in disponibles if p.clave in claves]
    if not elegidos:
        raise ValidationError('Elija al menos un parámetro que ajustar.')
    return [[p.clave, list(p.rejilla)] for p in elegidos]


def estimar_llamadas(tarea, presupuesto, repeticiones=1):
    """Upper bound on the model calls a search will make, for the form."""
    return len(evaluation_service.casos_de(tarea)) * presupuesto * repeticiones


def tareas_evaluables():
    """``[(tarea, descripcion, n_casos)]`` -- what the tuner can work on."""
    return [
        (AITask(t), descripcion, len(evaluation_service.casos_de(t)))
        for t, descripcion in evaluation_service.EVALUABLES.items()
    ]


# ======================================================================
# Launching
# ======================================================================
def lanzar(actor, tarea, config=None, modelo=None, claves=None,
           presupuesto=PRESUPUESTO_POR_DEFECTO, repeticiones=1,
           aplicar_si_mejora=False, automatico=False, en_linea=False):
    """Queue a search. Returns the ``AIAutotuneRun``.

    ``en_linea`` runs it here and now instead of in the worker -- for the
    command line, where whoever asked is waiting for the answer.

    ``config`` and ``modelo`` default to whatever serves the task now, which
    is the usual question. Naming others tunes a model before switching the
    task to it.
    """
    tarea = AITask.coerce(tarea)
    if tarea is None:
        raise ValidationError('La tarea indicada no es válida.')
    if str(tarea) not in evaluation_service.EVALUABLES:
        raise ValidationError(
            f'«{tarea.label}» no tiene todavía forma de medirse, así que no hay '
            f'nada que optimizar.'
        )
    if not evaluation_service.casos_de(tarea):
        raise ValidationError(
            f'No hay casos de evaluación para «{tarea.label}». Sin ellos, '
            f'ajustar sería optimizar contra nada.'
        )

    if config is None:
        from app.services.ai import resolve

        provider, modelo_actual, _p, _b = resolve(tarea)
        config = getattr(provider, 'config', None)
        if config is None:
            raise ValidationError(
                'Configure un proveedor en Administración → Proveedores de IA '
                'antes de ajustarlo.'
            )
        modelo = modelo or modelo_actual
    modelo = (modelo or config.modelo_por_defecto or '').strip()
    if not modelo:
        raise ValidationError('Indique el modelo que se va a ajustar.')
    if not config.activo:
        raise ValidationError('El proveedor está desactivado.')

    presupuesto = int(presupuesto or PRESUPUESTO_POR_DEFECTO)
    if not 2 <= presupuesto <= PRESUPUESTO_MAXIMO:
        raise ValidationError(
            f'El número de ensayos debe estar entre 2 y {PRESUPUESTO_MAXIMO}.'
        )
    repeticiones = int(repeticiones or 1)
    if not 1 <= repeticiones <= REPETICIONES_MAXIMAS:
        raise ValidationError(
            f'Las repeticiones deben estar entre 1 y {REPETICIONES_MAXIMAS}.'
        )

    en_marcha = AIAutotuneRun.query.filter(
        AIAutotuneRun.tarea == str(tarea),
        AIAutotuneRun.estado.in_([AIAutotuneState.PENDIENTE.value,
                                  AIAutotuneState.EN_CURSO.value]),
    ).first()
    if en_marcha is not None:
        # Two searches on one task would measure each other's load as latency
        # and could apply over each other.
        raise ConflictError(f'Ya hay un autoajuste en marcha para «{tarea.label}».')

    ajuste = AIAutotuneRun(
        tarea=tarea,
        provider_config_id=config.id,
        modelo=modelo,
        estado=AIAutotuneState.PENDIENTE,
        espacio=espacio_para(config, claves),
        presupuesto=presupuesto,
        repeticiones=repeticiones,
        aplicar_si_mejora=bool(aplicar_si_mejora),
        automatico=bool(automatico),
        ensayos=[],
        lanzado_por_id=getattr(actor, 'id', None),
    )
    db.session.add(ajuste)
    db.session.commit()
    _audit(actor, 'ai_autotune.launched', ajuste, {
        'espacio': ajuste.espacio, 'presupuesto': presupuesto,
        'repeticiones': repeticiones, 'aplicar_si_mejora': ajuste.aplicar_si_mejora,
    })

    if en_linea:
        return ejecutar(ajuste.id)
    _despachar(ajuste)
    return ajuste


def _despachar(ajuste):
    """Hand the first step to the worker, or run the whole search here."""
    from app.tasks.dispatch import is_eager

    if is_eager():
        ejecutar(ajuste.id)
        return

    try:
        _encolar(ajuste)
    except Exception as exc:
        # Not run inline as a fallback: a search takes minutes, and a web
        # request that hangs for that long is worse than a clear refusal.
        logger.exception('No se pudo encolar el autoajuste %s', ajuste.id)
        marcar_error(ajuste.id, f'No se pudo encolar: {exc}')


def _encolar(ajuste):
    """Queue the next step under a fresh token, which becomes the owner.

    The token is the Celery task id, written before the task exists: a step
    that starts and finds a different token on the row has been superseded --
    by «Reanudar», typically -- and leaves without touching anything.
    """
    import uuid

    from app.tasks.ai_tasks import run_autotune

    token = uuid.uuid4().hex
    ajuste.celery_task_id = token
    db.session.commit()
    run_autotune.apply_async(args=[str(ajuste.id)], task_id=token)
    return token


def continuar(ajuste_id, token):
    """Queue the next step, if this step still owns the search.

    A conditional update rather than read-then-write: two steps finishing at
    once must not both hand over, or the search would fork in two.
    """
    import uuid

    from app.tasks.ai_tasks import run_autotune

    siguiente = uuid.uuid4().hex
    cambiadas = AIAutotuneRun.query.filter_by(
        id=ajuste_id, celery_task_id=token,
    ).update({'celery_task_id': siguiente}, synchronize_session=False)
    db.session.commit()
    if cambiadas:
        run_autotune.apply_async(args=[str(ajuste_id)], task_id=siguiente)
    return bool(cambiadas)


# ======================================================================
# Running: one trial per step
# ======================================================================
#
# A search used to be one Celery task, and died with it. Measured against a
# 9B model, a trial is two extractions of three or four minutes each, and
# the worker's 30-minute limit killed the process at trial 8 -- with no chance
# to record anything, so the row said «en curso» for ever. Raising the limit
# would only move the wall: a deploy restarts the worker too. So each step
# runs one trial and queues the next, and everything the search knows lives
# in the row. The plan is replayed from the trials already recorded, which is
# what makes a step after a crash, a restart or «Reanudar» carry on exactly
# where the last one stopped.


def ejecutar(ajuste_id):
    """Run a search to the end, here. For the tests and the command line."""
    while paso(ajuste_id):
        pass
    return get_or_404(ajuste_id)


def paso(ajuste_id, token=None):
    """Advance a search by one step. Returns True while there is more to do.

    ``token`` is the owning step's id; None when running inline, where there
    is nobody to compete with.
    """
    ajuste = get_or_404(ajuste_id)
    if token is not None and ajuste.celery_task_id != token:
        return False

    try:
        if ajuste.estado is AIAutotuneState.PENDIENTE:
            _empezar(ajuste)
            return True
        if ajuste.estado is not AIAutotuneState.EN_CURSO:
            return False

        candidato, mejor = _planificar(ajuste)
        if candidato is None:
            _terminar(ajuste, mejor)
            return False

        ensayo = _probar(ajuste, candidato)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        db.session.rollback()
        logger.exception('El autoajuste %s falló', ajuste_id)
        marcar_error(ajuste_id, str(getattr(exc, 'mensaje', None) or exc))
        return False

    # Re-read before keeping it: while this trial ran, the search may have
    # been cancelled, or taken over by a resumed step.
    db.session.refresh(ajuste)
    if ajuste.estado is not AIAutotuneState.EN_CURSO:
        return False
    if token is not None and ajuste.celery_task_id != token:
        return False

    # A new list, not an append: an in-place change to a JSON column is
    # invisible to the session and would never be written.
    ajuste.ensayos = list(ajuste.ensayos or []) + [ensayo]
    if len(ajuste.ensayos) == 1:
        ajuste.calidad_base = ensayo['calidad']
    db.session.commit()
    return True


def _empezar(ajuste):
    from app.services.ai.selector import perfil

    casos = evaluation_service.casos_de(ajuste.tarea)
    if not casos:
        raise ValidationError('No quedan casos de evaluación para esta tarea.')

    ajuste.estado = AIAutotuneState.EN_CURSO
    ajuste.iniciado_en = utcnow()
    ajuste.casos = len(casos)
    ajuste.parametros_base = perfil(ajuste.provider_config, ajuste.modelo, ajuste.tarea)
    db.session.commit()
    _calentar(ajuste, casos)


def _calentar(ajuste, casos):
    """One uncounted call before measuring anything.

    Measured against a real Ollama, the first trial paid for loading the model
    into memory -- 29 s against 23 s for every later one -- and «equal
    quality, clearly faster» then crowned whatever came second. Without it,
    every search is biased against what is configured now.
    """
    from app.services.ai import forzar

    with forzar(ajuste.tarea, ajuste.provider_config, modelo=ajuste.modelo,
                parametros=dict(ajuste.parametros_base or {}),
                etiqueta='Autoajuste (calentamiento)'):
        evaluation_service.medir(ajuste.tarea, casos=casos[:1])


class _Pendiente(Exception):
    """The replay reached a trial that has not been run yet."""

    def __init__(self, candidato):
        super().__init__()
        self.candidato = candidato


def _planificar(ajuste):
    """Replay the search over the recorded trials.

    Returns ``(siguiente, mejor)``: the next parameters to try, or None when
    the search is over, and the best trial so far. Greedy coordinate descent,
    one parameter at a time: see the module docstring.
    """
    ensayos = list(ajuste.ensayos or [])
    consumidos = 0
    mejor = None

    def resultado(candidato):
        nonlocal consumidos
        if consumidos >= len(ensayos):
            raise _Pendiente(candidato)
        ensayo = ensayos[consumidos]
        if ensayo['parametros'] != candidato:
            raise ValidationError(
                'Los ensayos guardados no corresponden a la búsqueda; no se '
                'puede reanudar.'
            )
        consumidos += 1
        return ensayo

    try:
        base = dict(ajuste.parametros_base or {})
        mejor = resultado(base)
        probados = [base]
        for clave, rejilla in ajuste.espacio or []:
            for valor in rejilla:
                if consumidos >= ajuste.presupuesto:
                    return None, mejor
                candidato = dict(mejor['parametros'], **{clave: valor})
                if candidato in probados:
                    continue
                probados.append(candidato)
                ensayo = resultado(candidato)
                if es_mejor(ensayo, mejor):
                    mejor = ensayo
    except _Pendiente as pendiente:
        return pendiente.candidato, mejor
    return None, mejor


def _probar(ajuste, candidato):
    """Measure one set of parameters. Returns the trial, not yet recorded."""
    from app.services.ai import forzar

    with forzar(ajuste.tarea, ajuste.provider_config, modelo=ajuste.modelo,
                parametros=candidato, etiqueta='Autoajuste'):
        medida = evaluation_service.medir(
            ajuste.tarea, repeticiones=ajuste.repeticiones,
        )
    return {
        'parametros': candidato,
        'calidad': medida['calidad'],
        'duracion_ms': medida['duracion_ms'],
        'tokens': medida['tokens'],
        'errores': medida['errores'],
        'llamadas': medida['llamadas'],
        'casos': medida['casos'],
    }


def _terminar(ajuste, mejor):
    ajuste.parametros_mejores = mejor['parametros']
    ajuste.calidad_mejor = mejor['calidad']
    ajuste.estado = AIAutotuneState.COMPLETADO
    ajuste.terminado_en = utcnow()
    db.session.commit()
    _audit(None, 'ai_autotune.completed', ajuste, {
        'calidad_base': _num(ajuste.calidad_base),
        'calidad_mejor': _num(ajuste.calidad_mejor),
        'ensayos': len(ajuste.ensayos or []),
    })

    mejora = ajuste.mejora
    if (ajuste.aplicar_si_mejora and ajuste.hay_propuesta
            and mejora is not None and mejora >= MEJORA_MINIMA_PARA_APLICAR):
        aplicar(ajuste.lanzado_por, ajuste, automatico=True)


def marcar_error(ajuste_id, mensaje):
    """Close a search as failed, saying why."""
    db.session.rollback()
    ajuste = get_or_404(ajuste_id)
    if not ajuste.activo:
        return ajuste
    ajuste.estado = AIAutotuneState.ERROR
    ajuste.error = mensaje[:500]
    ajuste.terminado_en = utcnow()
    db.session.commit()
    _audit(None, 'ai_autotune.failed', ajuste, {'error': ajuste.error})
    return ajuste


#: Without progress for this long, a running search is offered «Reanudar».
#: Longer than a normal trial; a trial that really takes longer is only
#: interrupted if somebody decides to.
PARADO_TRAS_MINUTOS = 20


def parece_parado(ajuste):
    from datetime import UTC, timedelta

    if not ajuste.activo:
        return False
    ultima = ajuste.updated_at or ajuste.created_at
    if ultima is None:
        return False
    if ultima.tzinfo is None:
        # SQLite hands timestamps back naive; they were written in UTC.
        ultima = ultima.replace(tzinfo=UTC)
    return utcnow() - ultima > timedelta(minutes=PARADO_TRAS_MINUTOS)


def reanudar(actor, ajuste):
    """Carry on a search whose worker died, from its last recorded trial.

    The new step takes the token, so if the old one was only slow after all,
    it finds itself superseded when it finishes and discards its trial rather
    than recording it twice.
    """
    if not ajuste.activo:
        raise ValidationError('Este autoajuste ya ha terminado.')
    _encolar(ajuste)
    _audit(actor, 'ai_autotune.resumed', ajuste, {
        'ensayos_hechos': len(ajuste.ensayos or []),
    })
    return ajuste


def es_mejor(nuevo, actual):
    """Whether a trial beats the best so far. See the module docstring."""
    if nuevo['calidad'] > actual['calidad'] + TOLERANCIA:
        return True
    return (
        nuevo['calidad'] >= actual['calidad']
        and nuevo['errores'] <= actual['errores']
        and nuevo['duracion_ms'] < actual['duracion_ms'] * MEJORA_DE_VELOCIDAD
    )


# ======================================================================
# Acting on the result
# ======================================================================
def aplicar(actor, ajuste, automatico=False):
    """Make the best trial the task's profile for this model.

    ``automatico`` records that nobody looked at this result before it took
    effect -- the run was launched asking for it -- even when ``actor`` names
    who launched it.
    """
    if not ajuste.hay_propuesta:
        raise ValidationError('Este autoajuste no propone ningún cambio.')
    if ajuste.aplicado:
        raise ConflictError('Este autoajuste ya está aplicado.')

    fila = _perfil_de(ajuste)
    previos = (
        {'parametros': fila.parametros or {}, 'origen': str(fila.origen)}
        if fila is not None else None
    )
    if fila is None:
        fila = AIParameterProfile(
            provider_config_id=ajuste.provider_config_id,
            modelo=ajuste.modelo, tarea=ajuste.tarea,
        )
        db.session.add(fila)

    fila.parametros = dict(ajuste.parametros_mejores)
    fila.origen = AIParameterOrigin.AUTOAJUSTE
    fila.autotune_run_id = ajuste.id
    fila.actualizado_por_id = getattr(actor, 'id', None)

    ajuste.parametros_previos = previos
    ajuste.aplicado_en = utcnow()
    ajuste.aplicado_por_id = getattr(actor, 'id', None)
    ajuste.revertido_en = None
    db.session.commit()

    _audit(actor, 'ai_autotune.applied', ajuste, {
        'antes': (previos or {}).get('parametros'),
        'despues': fila.parametros,
        'automatico': automatico,
    })
    return fila


def revertir(actor, ajuste):
    """Put back the profile that was there before this search was applied."""
    if not ajuste.aplicado:
        raise ValidationError('Este autoajuste no está aplicado.')

    fila = _perfil_de(ajuste)
    if fila is None or fila.autotune_run_id != ajuste.id:
        raise ConflictError(
            'El perfil se ha cambiado después de aplicar este autoajuste; '
            'revertirlo pisaría ese cambio.'
        )

    previos = ajuste.parametros_previos
    if previos is None:
        db.session.delete(fila)
    else:
        fila.parametros = previos.get('parametros') or {}
        fila.origen = AIParameterOrigin.coerce(previos.get('origen'),
                                               AIParameterOrigin.MANUAL)
        fila.autotune_run_id = None
        fila.actualizado_por_id = getattr(actor, 'id', None)

    ajuste.revertido_en = utcnow()
    db.session.commit()
    _audit(actor, 'ai_autotune.reverted', ajuste, {
        'restaurado': (previos or {}).get('parametros'),
    })
    return ajuste


def cancelar(actor, ajuste):
    """Stop a search. The running worker notices between two trials."""
    if not ajuste.activo:
        raise ValidationError('Este autoajuste ya ha terminado.')
    ajuste.estado = AIAutotuneState.CANCELADO
    ajuste.terminado_en = utcnow()
    db.session.commit()
    _audit(actor, 'ai_autotune.cancelled', ajuste)
    return ajuste


# ======================================================================
# Scheduled
# ======================================================================
def autoajuste_periodico():
    """Re-tune every measurable task on what serves it now.

    Off unless an administrator turns it on: every search is a few dozen model
    calls, which on an external provider is money. When on, it applies only
    what clears the automatic threshold, like any other run asked to.
    """
    from app.services import settings_service

    if not settings_service.get_bool('IA_AUTOAJUSTE_PERIODICO', False):
        return []

    lanzados = []
    for tarea, _descripcion, n_casos in tareas_evaluables():
        if not n_casos:
            continue
        try:
            lanzados.append(lanzar(
                None, tarea,
                aplicar_si_mejora=settings_service.get_bool('IA_AUTOAJUSTE_APLICAR', False),
                automatico=True,
            ))
        except (ValidationError, ConflictError) as exc:
            logger.info('Autoajuste periódico de %s omitido: %s', tarea, exc.mensaje)
    return lanzados


# ======================================================================
# Queries
# ======================================================================
def get_or_404(ajuste_id):
    import uuid

    try:
        ajuste = db.session.get(AIAutotuneRun, uuid.UUID(str(ajuste_id)))
    except (ValueError, TypeError):
        ajuste = None
    if ajuste is None:
        raise ResourceNotFound('El autoajuste indicado no existe.')
    return ajuste


def recientes(limite=30):
    return AIAutotuneRun.query.order_by(
        AIAutotuneRun.created_at.desc()
    ).limit(limite).all()


def _perfil_de(ajuste):
    return AIParameterProfile.query.filter_by(
        provider_config_id=ajuste.provider_config_id,
        modelo=ajuste.modelo, tarea=str(ajuste.tarea),
    ).first()


def _num(valor):
    return float(valor) if valor is not None else None


def _audit(actor, accion, ajuste, extra=None):
    metadatos = {
        'tarea': str(ajuste.tarea),
        'proveedor': ajuste.provider_config.nombre if ajuste.provider_config else None,
        'modelo': ajuste.modelo,
    }
    metadatos.update(extra or {})
    audit_service.record(
        accion,
        recurso_tipo=AuditResourceType.CONFIGURACION,
        recurso_id=str(ajuste.id),
        actor=actor,
        metadatos=metadatos,
    )
