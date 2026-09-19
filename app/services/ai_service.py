"""The internal AI contract (specification section 6).

Five operations: ``extract_document``, ``answer_trip_question``,
``summarize_trip``, ``analyze_risks`` and ``research_public_info``, plus the
document classifier and alert explainer the pipeline and interface use.

Everything here shares one discipline. The context handed to a model is built
from what the *asking actor* is already authorised to see, never from the trip as
a whole; the answer's internal references are validated back against that same
set; and every execution writes an ``ai_runs`` row with its purpose, provider,
model, sources and metrics -- but a summary of the result, not its full sensitive
content.
"""
import json
import logging
import time

from flask import g, has_request_context

from app.extensions import db
from app.models.ai import AIRun
from app.models.enums import (
    AIRunState,
    AITask,
    AuditResourceType,
    RoleCode,
)
from app.services import audit_service
from app.services.ai import (
    AIRequest,
    UntrustedBlock,
    enforce_egress,
    looks_injected,
    resolve,
    validate_references,
    validate_schema,
)
from app.services.ai.prompts import PROMPTS
from app.services.ai.schemas import SCHEMAS
from app.utils.errors import AIContractError, AIError, AIPolicyBlocked
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Longest document text sent in one request. Beyond this the pages are
#: truncated rather than silently dropped, and the truncation is reported.
MAX_DOCUMENT_CHARS = 24000


# ======================================================================
# Execution plumbing
# ======================================================================
def _run(tarea, request, actor=None, trip=None, document=None, finalidad=None,
         referencias=None, fuentes_web=None):
    """Execute a request, recording an ``ai_runs`` row whatever happens.

    Returns:
        ``(AIResponse, AIRun)``.
    """
    tarea = AITask.coerce(tarea)
    provider, modelo, params, _binding = resolve(tarea)

    request.max_tokens = request.max_tokens or params['max_tokens']
    request.temperatura = (
        params['temperatura'] if request.temperatura is None else request.temperatura
    )

    run = AIRun(
        usuario_id=getattr(actor, 'id', None),
        trip_id=getattr(trip, 'id', None),
        document_id=getattr(document, 'id', None),
        tarea=tarea,
        proveedor=str(provider.codigo),
        modelo=modelo,
        estado=AIRunState.EN_CURSO,
        finalidad=finalidad,
        referencias_fuentes=referencias or {},
        fuentes_web=fuentes_web or [],
        correlation_id=getattr(g, 'correlation_id', None) if has_request_context() else None,
    )
    db.session.add(run)
    db.session.flush()

    # The egress policy is checked after the run row exists, so a refusal is
    # recorded rather than disappearing.
    try:
        enforce_egress(request, provider, tarea)
    except AIPolicyBlocked as exc:
        run.estado = AIRunState.BLOQUEADA
        run.motivo_bloqueo = exc.mensaje[:500]
        db.session.commit()
        _audit(run, actor, resultado_denegado=True)
        raise

    start = time.monotonic()
    try:
        response = provider.complete(request)
    except Exception as exc:
        run.estado = AIRunState.ERROR
        run.error = str(exc)[:500]
        run.duracion_ms = int((time.monotonic() - start) * 1000)
        db.session.commit()
        _audit(run, actor)
        raise

    run.estado = AIRunState.COMPLETADA
    run.modelo = response.modelo or modelo
    run.tokens_entrada = response.tokens_entrada
    run.tokens_salida = response.tokens_salida
    run.duracion_ms = response.duracion_ms or int((time.monotonic() - start) * 1000)
    run.resultado_resumen = _summarise(response.contenido)

    if looks_injected(response.contenido):
        # Not treated as an error: the schema check is the real control. But the
        # run is flagged so a reviewer can see the document tried something.
        run.motivo_bloqueo = (
            'La respuesta contiene indicios de un intento de inyección de prompt '
            'en el contenido analizado.'
        )
        logger.warning(
            'Posible intento de inyección en la tarea %s (ejecución %s)', tarea, run.id
        )

    db.session.commit()
    _audit(run, actor)
    return response, run


def _summarise(content):
    """A short record of the result.

    Section 2.5: do not log the full sensitive content unless an administrator
    has approved verbose capture.
    """
    if not content:
        return None
    from app.services import settings_service

    if settings_service.get_bool('IA_REGISTRAR_CONTENIDO_COMPLETO', False):
        return content[:8000]
    return content[:300] + ('…' if len(content) > 300 else '')


def _audit(run, actor, resultado_denegado=False):
    """Record the execution in the audit trail (section 2.1)."""
    from app.models.enums import AuditResult

    audit_service.record(
        f'ai.{run.tarea}',
        recurso_tipo=AuditResourceType.EJECUCION_IA,
        recurso_id=str(run.id),
        actor=actor,
        resultado=AuditResult.DENEGADO if resultado_denegado else AuditResult.EXITO,
        metadatos={
            'proveedor': str(run.proveedor),
            'modelo': run.modelo,
            'trip_id': str(run.trip_id) if run.trip_id else None,
            'estado': str(run.estado),
            'tokens': (run.tokens_entrada or 0) + (run.tokens_salida or 0),
        },
    )


#: Keys of the extraction envelope. Anything else at the top level of an
#: extraction answer is a field the model put there directly.
_ENVELOPE_KEYS = frozenset({
    'campos', 'confianzas', 'procedencias', 'confianza_global', 'avisos',
})


def _coerce_envelope(datos, esquema):
    """Accept an extraction answered without its wrapper.

    The ``{campos: {...}, confianzas: {...}}`` envelope is our own convenience.
    A model that returns the fields flat has done the work that matters, and
    rejecting it would throw away a good answer over a formatting detail. The
    wrapper is rebuilt, and the missing confidences default to empty -- which
    leaves ``confianza_global`` unknown and therefore forces human review, so
    being liberal here cannot make a value look more trustworthy than it is.
    """
    if not isinstance(datos, dict):
        return datos
    if 'campos' in datos:
        return datos
    if not isinstance(esquema, dict):
        return datos
    if 'campos' not in (esquema.get('properties') or {}):
        return datos

    sueltos = {k: v for k, v in datos.items() if k not in _ENVELOPE_KEYS}
    if not sueltos:
        return datos

    logger.info(
        'La respuesta llegó sin el envoltorio «campos»; se reconstruye con %s campos.',
        len(sueltos),
    )
    envoltorio = {k: v for k, v in datos.items() if k in _ENVELOPE_KEYS}
    envoltorio['campos'] = sueltos
    envoltorio.setdefault('confianzas', {})
    return envoltorio


def _parse(response, tarea, esquema=None):
    """Parse and validate the model's answer against its schema."""
    try:
        datos = response.parse_json(strict=True)
    except ValueError as exc:
        raise AIContractError(
            f'El modelo no devolvió un JSON interpretable en «{tarea}»: {exc}'
        ) from exc

    datos = _coerce_envelope(datos, esquema)

    if esquema is not None:
        valid, problem = validate_schema(datos, esquema)
        if not valid:
            # The answer goes in the message: without it, diagnosing why a
            # model failed the contract means reproducing the call by hand.
            muestra = (response.contenido or '')[:300].replace('\n', ' ')
            raise AIContractError(
                f'La respuesta del modelo no cumple el esquema de «{tarea}»: '
                f'{problem}. Devolvió: {muestra}'
            )
    return datos


# ======================================================================
# 1. extract_document
# ======================================================================
def extract_document(document, clasificacion=None, actor=None):
    """Pull structured fields out of a document (section 2.3 step 4).

    The document's text enters the prompt only as :class:`UntrustedBlock`s, one
    per page so each extracted value can cite the page it came from.
    """
    clasificacion = str(clasificacion or document.clasificacion or 'otro')
    esquema = SCHEMAS.get(f'extract_{clasificacion}') or SCHEMAS['extract_otro']

    blocks, truncated = _document_blocks(document)
    if not blocks:
        raise AIError('El documento no tiene texto que analizar.')

    request = AIRequest(
        tarea=AITask.EXTRACT_DOCUMENT.value,
        sistema=PROMPTS['extract_document'],
        instruccion=(
            f'Extrae los datos estructurados del siguiente documento de tipo '
            f'«{clasificacion}». Para cada campo indica tu confianza entre 0 y 1 y, '
            f'cuando el valor aparezca literalmente en el texto, el número de página '
            f'y el fragmento exacto que lo respalda. Si un dato no aparece, usa null '
            f'y confianza 0; no lo inventes.'
        ),
        bloques=blocks,
        esquema=esquema,
        contiene_documentos=True,
        contiene_pii=True,
        temperatura=0.0,
    )

    response, run = _run(
        AITask.EXTRACT_DOCUMENT,
        request,
        actor=actor,
        trip=document.trip,
        document=document,
        finalidad=f'Extracción de datos de «{document.nombre_original}»',
        referencias={'document_id': str(document.id), 'paginas': len(blocks)},
    )

    datos = _parse(response, 'extract_document', esquema)
    _ground_in_document(datos, blocks)

    if truncated:
        datos.setdefault('avisos', []).append(
            'El documento se truncó por longitud; revise las páginas finales.'
        )

    return {'datos': datos, 'run': run, 'response': response}


#: Confidence given to a value found literally in the document, and to one the
#: model produced without a literal match.
#:
#: Deliberately below the auto-approval threshold, both of them. A literal match
#: proves the model copied rather than invented; it says nothing about whether
#: it copied the *right* string. Asked for a booking reference, a weak model
#: will happily return "Payment details" -- which is in the document, and is
#: wrong. Grounding separates copying from hallucinating, and that is all it
#: does, so no value reaches the itinerary on its strength alone.
CONFIANZA_LITERAL = 0.85
CONFIANZA_INFERIDA = 0.5


def _ground_in_document(datos, blocks):
    """Derive per-field confidence and provenance from the document text.

    Asking a small local model to rate its own confidence produces a number
    with no relation to whether it is right. Whether the value it gave appears
    literally in the document is something we can check, and it happens to be
    exactly the distinction the specification draws between a value that came
    from the document and one the model inferred (section 2.3).

    Confidences the model volunteered are kept only where we found no literal
    match, and capped: they are a hint, not evidence.
    """
    campos = datos.get('campos')
    if not isinstance(campos, dict):
        return datos

    paginas = [(b.pagina or 1, b.contenido or '') for b in blocks]

    confianzas = dict(datos.get('confianzas') or {})
    procedencias = dict(datos.get('procedencias') or {})

    for nombre, valor in campos.items():
        literal = _find_literal(valor, paginas)

        if literal is not None:
            pagina, fragmento = literal
            confianzas[nombre] = CONFIANZA_LITERAL
            procedencias[nombre] = {'pagina': pagina, 'fragmento': fragmento}
        elif valor in (None, '', {}):
            confianzas[nombre] = 0.0
            procedencias[nombre] = {'pagina': None, 'fragmento': None}
        else:
            reportada = confianzas.get(nombre)
            confianzas[nombre] = min(
                float(reportada) if isinstance(reportada, (int, float)) else
                CONFIANZA_INFERIDA,
                CONFIANZA_INFERIDA,
            )
            procedencias[nombre] = {'pagina': None, 'fragmento': None}

    datos['confianzas'] = confianzas
    datos['procedencias'] = procedencias

    _flag_invented_years(datos, paginas, confianzas)

    valores = [v for v in confianzas.values() if isinstance(v, (int, float))]
    datos['confianza_global'] = round(sum(valores) / len(valores), 2) if valores else None

    return datos


def _flag_invented_years(datos, paginas, confianzas):
    """Warn when an extracted date lands in a year the document never mentions.

    A wrong year on a flight is not a detail: it moves the whole itinerary and
    every margin computed from it. Models do produce them -- a real Vueling
    confirmation reading «31 October 2026» came back as 2023, a year absent from
    the document entirely.

    A year is cheap to check and unambiguous, unlike the day or the hour, which
    legitimately appear in many forms. Anything flagged here drops to zero
    confidence so it cannot be approved without someone looking at it.
    """
    import re

    anos_documento = set()
    for _pagina, texto in paginas:
        anos_documento.update(re.findall(r'\b(20\d{2})\b', texto))

    avisos = list(datos.get('avisos') or [])

    if not anos_documento:
        # A scan with no legible year gives nothing to check against; flagging
        # every date would bury the review screen in noise.
        datos['avisos'] = avisos
        return

    for nombre, valor in (datos.get('campos') or {}).items():
        local = valor.get('local') if isinstance(valor, dict) else None
        if not local:
            continue

        encontrado = re.match(r'\s*(20\d{2})', str(local))
        if not encontrado:
            continue

        ano = encontrado.group(1)
        if ano in anos_documento:
            continue

        confianzas[nombre] = 0.0
        avisos.append(
            f'La fecha de «{nombre}» está en {ano}, un año que no aparece en el '
            f'documento (contiene {", ".join(sorted(anos_documento))}). '
            f'Compruébela antes de aprobar.'
        )

    datos['avisos'] = avisos


#: How much text around a match to keep as the supporting excerpt.
_CONTEXTO = 60


def _find_literal(valor, paginas):
    """Locate a value in the document text, returning ``(pagina, fragmento)``.

    Compares case-insensitively and ignoring spacing, so ``XYZ 12A`` in the
    document matches the ``XYZ12A`` the model normalised.
    """
    aguja = _searchable(valor)
    if not aguja or len(aguja) < 3:
        return None

    for pagina, texto in paginas:
        pajar = _searchable(texto)
        posicion = pajar.find(aguja)
        if posicion < 0:
            continue

        # Map back to the original text for a readable excerpt: the searchable
        # form has had its spacing removed.
        aproximado = _aproximar(texto, aguja)
        return pagina, aproximado

    return None


def _searchable(valor):
    """Normalise for comparison: lowercase, no spaces or separators."""
    if isinstance(valor, dict):
        valor = valor.get('local') or ''
    if valor in (None, '', {}):
        return ''
    return ''.join(
        c for c in str(valor).lower() if c.isalnum()
    )


def _aproximar(texto, aguja):
    """Find the excerpt of ``texto`` whose normalised form contains ``aguja``."""
    indices = []
    normalizado = []
    for i, c in enumerate(texto):
        if c.isalnum():
            normalizado.append(c.lower())
            indices.append(i)

    posicion = ''.join(normalizado).find(aguja)
    if posicion < 0 or posicion >= len(indices):
        return None

    inicio = max(indices[posicion] - _CONTEXTO // 2, 0)
    fin_idx = min(posicion + len(aguja) - 1, len(indices) - 1)
    fin = min(indices[fin_idx] + _CONTEXTO // 2, len(texto))
    return ' '.join(texto[inicio:fin].split())


def _document_blocks(document):
    """One untrusted block per page, truncated to the configured budget."""
    blocks = []
    used = 0
    truncated = False

    pages = document.pages or []
    if not pages and document.texto is not None:
        pages = [type('P', (), {
            'numero': 1,
            'contenido': document.texto.contenido,
        })()]

    for page in pages:
        content = (page.contenido or '').strip()
        if not content:
            continue
        remaining = MAX_DOCUMENT_CHARS - used
        if remaining <= 0:
            truncated = True
            break
        if len(content) > remaining:
            content = content[:remaining]
            truncated = True
        used += len(content)
        blocks.append(UntrustedBlock(
            contenido=content,
            referencia=str(document.id),
            pagina=page.numero,
            tipo='documento',
        ))

    return blocks, truncated


# ======================================================================
# 2. classify_document
# ======================================================================
def classify_document(document, actor=None):
    """Classify a document when the rule-based classifier was not confident."""
    blocks, _ = _document_blocks(document)
    if not blocks:
        raise AIError('El documento no tiene texto que analizar.')

    # Only the first page is needed to tell a boarding pass from a hotel
    # confirmation, and sending less is both cheaper and safer.
    esquema = SCHEMAS['classify_document']
    request = AIRequest(
        tarea=AITask.CLASSIFY_DOCUMENT.value,
        sistema=PROMPTS['classify_document'],
        instruccion=(
            'Clasifica el siguiente documento de viaje en una de estas categorías: '
            'vuelo, tren, hotel, vehiculo, seguro, visado, otro. Indica tu '
            'confianza entre 0 y 1.'
        ),
        bloques=blocks[:1],
        esquema=esquema,
        contiene_documentos=True,
        max_tokens=256,
        temperatura=0.0,
    )

    response, run = _run(
        AITask.CLASSIFY_DOCUMENT,
        request,
        actor=actor,
        trip=document.trip,
        document=document,
        finalidad=f'Clasificación de «{document.nombre_original}»',
    )
    return {'datos': _parse(response, 'classify_document', esquema), 'run': run}


# ======================================================================
# 3. answer_trip_question
# ======================================================================
def answer_trip_question(actor, trip, pregunta):
    """Answer a question about a trip (specification flow 5.4).

    The context is built from what *this actor* may see. A traveller's question
    is answered from their own itinerary; another traveller's segments are not in
    the context at all, so they cannot appear in the answer.
    """
    contexto = build_ai_context(actor, trip)
    esquema = SCHEMAS['answer_trip_question']

    request = AIRequest(
        tarea=AITask.ANSWER_TRIP_QUESTION.value,
        sistema=PROMPTS['answer_trip_question'],
        instruccion=(
            f'Responde a esta pregunta usando EXCLUSIVAMENTE los datos del viaje '
            f'que se incluyen a continuación. Si la información necesaria no está '
            f'en esos datos, dilo claramente en lugar de deducirla o inventarla. '
            f'Cita las referencias internas de los elementos que uses.\n\n'
            f'Pregunta: {pregunta}'
        ),
        bloques=[UntrustedBlock(
            contenido=json.dumps(contexto['datos'], ensure_ascii=False, indent=2),
            referencia=str(trip.id),
            tipo='datos_del_viaje',
        )],
        esquema=esquema,
        contiene_pii=True,
        temperatura=0.1,
    )

    response, run = _run(
        AITask.ANSWER_TRIP_QUESTION,
        request,
        actor=actor,
        trip=trip,
        finalidad='Consulta sobre el viaje',
        referencias=contexto['referencias'],
    )

    datos = _parse(response, 'answer_trip_question', esquema)
    # Anything the model cites that was not in the authorised set is discarded.
    datos, descartadas = validate_references(datos, contexto['ids_autorizados'])
    if descartadas:
        run.motivo_bloqueo = (
            f'Se descartaron {len(descartadas)} referencias no autorizadas.'
        )
        db.session.commit()

    return {
        'respuesta': datos.get('respuesta'),
        'referencias': datos.get('referencias', []),
        'confianza': datos.get('confianza'),
        'datos_insuficientes': datos.get('datos_insuficientes', False),
        'fecha_datos': contexto['fecha_datos'],
        'run_id': str(run.id),
        'proveedor': str(run.proveedor),
        'modelo': run.modelo,
    }


# ======================================================================
# 4. summarize_trip
# ======================================================================
def summarize_trip(actor, trip):
    """Produce an executive summary and a readable itinerary (section 2.5)."""
    contexto = build_ai_context(actor, trip)
    esquema = SCHEMAS['summarize_trip']

    request = AIRequest(
        tarea=AITask.SUMMARIZE_TRIP.value,
        sistema=PROMPTS['summarize_trip'],
        instruccion=(
            'Elabora un resumen ejecutivo del viaje y un itinerario legible, en '
            'español, usando únicamente los datos proporcionados. Señala lo que '
            'falte en lugar de completarlo por tu cuenta.'
        ),
        bloques=[UntrustedBlock(
            contenido=json.dumps(contexto['datos'], ensure_ascii=False, indent=2),
            referencia=str(trip.id),
            tipo='datos_del_viaje',
        )],
        esquema=esquema,
        contiene_pii=True,
        temperatura=0.2,
    )

    response, run = _run(
        AITask.SUMMARIZE_TRIP,
        request,
        actor=actor,
        trip=trip,
        finalidad='Resumen ejecutivo del viaje',
        referencias=contexto['referencias'],
    )

    datos = _parse(response, 'summarize_trip', esquema)
    return {
        'resumen': datos.get('resumen'),
        'itinerario': datos.get('itinerario', []),
        'puntos_atencion': datos.get('puntos_atencion', []),
        'fecha_datos': contexto['fecha_datos'],
        'run_id': str(run.id),
    }


# ======================================================================
# 5. analyze_risks
# ======================================================================
def analyze_risks(actor, trip, contexto_publico=None):
    """Analyse the trip's risks (section 2.5).

    Feeds the security advisory generator: the public sources gathered by the
    research service are passed in as untrusted blocks alongside the itinerary.
    """
    contexto = build_ai_context(actor, trip)
    esquema = SCHEMAS['analyze_risks']

    bloques = [UntrustedBlock(
        contenido=json.dumps(contexto['datos'], ensure_ascii=False, indent=2),
        referencia=str(trip.id),
        tipo='datos_del_viaje',
    )]
    for fuente in (contexto_publico or []):
        bloques.append(UntrustedBlock(
            contenido=fuente.get('contenido', '')[:6000],
            referencia=fuente.get('url'),
            tipo='fuente_publica',
        ))

    request = AIRequest(
        tarea=AITask.ANALYZE_RISKS.value,
        sistema=PROMPTS['analyze_risks'],
        instruccion=(
            'Analiza los riesgos logísticos y de seguridad de este viaje a partir '
            'de su itinerario y, si se incluyen, de las fuentes públicas '
            'aportadas. Para cada riesgo indica el nivel, la justificación y, '
            'cuando proceda, la fuente pública en la que te basas. No afirmes '
            'nada que no esté respaldado por los datos aportados.'
        ),
        bloques=bloques,
        esquema=esquema,
        contiene_pii=True,
        temperatura=0.2,
    )

    response, run = _run(
        AITask.ANALYZE_RISKS,
        request,
        actor=actor,
        trip=trip,
        finalidad='Análisis de riesgos del viaje',
        referencias=contexto['referencias'],
        fuentes_web=[f.get('url') for f in (contexto_publico or []) if f.get('url')],
    )

    datos = _parse(response, 'analyze_risks', esquema)
    return {'riesgos': datos.get('riesgos', []), 'nivel_global': datos.get('nivel_global'),
            'run_id': str(run.id)}


# ======================================================================
# 6. research_public_info
# ======================================================================
def research_public_info(actor, trip, consulta, categoria=None):
    """Consult the allow-listed public sources and summarise (section 2.6).

    Assisted search only: it returns links, the consultation date, the source and
    a summary. It never books or buys anything, and the manager keeps the
    decision.
    """
    from app.services import web_research_service

    resultados = web_research_service.search(
        consulta, categoria=categoria, trip=trip, actor=actor
    )
    if not resultados:
        return {
            'resumen': (
                'No se han obtenido resultados de las fuentes autorizadas para esta '
                'consulta.'
            ),
            'fuentes': [],
            'run_id': None,
        }

    esquema = SCHEMAS['research_public_info']
    bloques = [
        UntrustedBlock(
            contenido=r['contenido'][:6000],
            referencia=r['url'],
            tipo='pagina_web',
        )
        for r in resultados if r.get('contenido')
    ]

    request = AIRequest(
        tarea=AITask.RESEARCH_PUBLIC_INFO.value,
        sistema=PROMPTS['research_public_info'],
        instruccion=(
            f'Resume lo que dicen las siguientes fuentes públicas sobre esta '
            f'consulta, citando de qué fuente procede cada afirmación. No añadas '
            f'información que no aparezca en ellas, y señala explícitamente si las '
            f'fuentes no responden a la consulta.\n\nConsulta: {consulta}'
        ),
        bloques=bloques,
        esquema=esquema,
        # Public web content carries neither our documents nor personal data.
        contiene_documentos=False,
        contiene_pii=False,
        temperatura=0.2,
    )

    response, run = _run(
        AITask.RESEARCH_PUBLIC_INFO,
        request,
        actor=actor,
        trip=trip,
        finalidad=f'Investigación pública: {consulta[:200]}',
        fuentes_web=[r['url'] for r in resultados],
    )

    datos = _parse(response, 'research_public_info', esquema)
    return {
        'resumen': datos.get('resumen'),
        'hallazgos': datos.get('hallazgos', []),
        'fuentes': [
            {
                'url': r['url'],
                'titulo': r.get('titulo'),
                'fuente': r.get('fuente'),
                'es_oficial': r.get('es_oficial', False),
                'consultado_en': r.get('consultado_en'),
            }
            for r in resultados
        ],
        'advertencia': (
            'Información orientativa obtenida de fuentes públicas. Verifique '
            'siempre en la fuente original antes de tomar decisiones.'
        ),
        'run_id': str(run.id),
    }


# ======================================================================
# 7. explain_alert
# ======================================================================
def explain_alert(actor, alert):
    """Explain an alert and propose corrective actions (section 2.5)."""
    esquema = SCHEMAS['explain_alert']

    request = AIRequest(
        tarea=AITask.EXPLAIN_ALERT.value,
        sistema=PROMPTS['explain_alert'],
        instruccion=(
            'Explica en lenguaje claro por qué se ha generado esta alerta, qué '
            'consecuencias prácticas tiene para el viajero y qué acciones '
            'correctoras concretas cabe tomar. Básate únicamente en la evidencia '
            'aportada.'
        ),
        bloques=[UntrustedBlock(
            contenido=json.dumps({
                'tipo': alert.tipo,
                'regla': alert.regla,
                'severidad': str(alert.severidad),
                'titulo': alert.titulo,
                'mensaje': alert.mensaje,
                'evidencia': alert.evidencia,
            }, ensure_ascii=False, indent=2),
            referencia=str(alert.id),
            tipo='alerta',
        )],
        esquema=esquema,
        contiene_pii=True,
        temperatura=0.2,
    )

    response, run = _run(
        AITask.EXPLAIN_ALERT,
        request,
        actor=actor,
        trip=alert.trip,
        finalidad=f'Explicación de la alerta {alert.tipo}',
        referencias={'alert_id': str(alert.id)},
    )

    datos = _parse(response, 'explain_alert', esquema)
    return {
        'explicacion': datos.get('explicacion'),
        'consecuencias': datos.get('consecuencias'),
        'acciones': datos.get('acciones', []),
        'run_id': str(run.id),
    }


# ======================================================================
# Context building -- the authorisation boundary of every AI feature
# ======================================================================
def build_ai_context(actor, trip):
    """Build the model's view of a trip from what the actor may already see.

    This is the mechanism behind acceptance criterion 5.4. The itinerary is
    filtered through ``scope_itinerary_items``, so another traveller's segments
    are never in the payload; the model cannot leak what it was never given.

    Returns a dict with:
        ``datos``            -- the payload handed to the model
        ``referencias``      -- what to record on the ``ai_runs`` row
        ``ids_autorizados``  -- the set answers are validated against
        ``fecha_datos``      -- when the data was read, for the answer's footer
    """
    from app.services.authorization_service import (
        Permiso,
        can,
        scope_itinerary_items,
        visible_travelers,
    )

    include_costs = bool(can(actor, Permiso.VER_COSTES, trip))
    es_gestor = actor.has_role(RoleCode.GESTOR) or actor.has_role(RoleCode.ADMINISTRADOR)

    segments = scope_itinerary_items(actor, trip, [
        s for s in trip.segments if not s.is_deleted
    ])
    accommodations = scope_itinerary_items(actor, trip, [
        a for a in trip.accommodations if not a.is_deleted
    ])
    vehicles = scope_itinerary_items(actor, trip, [
        v for v in trip.vehicle_rentals if not v.is_deleted
    ])
    services = scope_itinerary_items(actor, trip, [
        s for s in trip.other_services if not s.is_deleted
    ])

    from app.services import alert_service

    alerts = alert_service.list_for_trip(actor, trip)

    from app.services import advisory_service

    advisories = advisory_service.list_for_trip(actor, trip)

    ids_autorizados = (
        [str(s.id) for s in segments]
        + [str(a.id) for a in accommodations]
        + [str(v.id) for v in vehicles]
        + [str(s.id) for s in services]
        + [str(a.id) for a in alerts]
        + [str(a.id) for a in advisories]
        + [str(trip.id)]
    )

    datos = {
        'viaje': {
            'id': str(trip.id),
            'referencia': trip.referencia,
            'titulo': trip.titulo,
            'estado': trip.estado.label if trip.estado else None,
            'finalidad': trip.finalidad.label if trip.finalidad else None,
            'inicio': trip.inicio_local.isoformat() if trip.inicio_local else None,
            'inicio_zona': trip.inicio_tz,
            'fin': trip.fin_local.isoformat() if trip.fin_local else None,
            'fin_zona': trip.fin_tz,
            'observaciones': trip.observaciones,
        },
        'destinos': [d.to_dict() for d in trip.destinations],
        'viajeros': [
            {'id': str(t.id), 'nombre': t.nombre_completo, 'rol': str(t.rol_en_viaje)}
            for t in visible_travelers(actor, trip)
        ],
        'segmentos': [s.to_dict(include_costs=include_costs) for s in segments],
        'alojamientos': [a.to_dict(include_costs=include_costs) for a in accommodations],
        'vehiculos': [v.to_dict(include_costs=include_costs) for v in vehicles],
        'servicios': [s.to_dict(include_costs=include_costs) for s in services],
        'alertas': [
            {
                'id': str(a.id),
                'tipo': a.tipo,
                'severidad': a.severidad.label if a.severidad else None,
                'estado': a.estado.label if a.estado else None,
                'titulo': a.titulo,
                'mensaje': a.mensaje,
            }
            for a in alerts
        ],
        'recomendaciones': [
            {
                'id': str(a.id),
                'categoria': a.categoria.label if a.categoria else None,
                'nivel': a.nivel.label if a.nivel else None,
                'titulo': a.titulo,
                'fuente': a.fuente,
                'fecha_fuente': a.fecha_fuente.isoformat() if a.fecha_fuente else None,
            }
            for a in advisories
        ],
    }

    # Managers see documents; a traveller's question is answered from the
    # itinerary, which is the processed result they are entitled to.
    if es_gestor:
        datos['documentos'] = [
            {
                'id': str(d.id),
                'nombre': d.nombre_original,
                'clasificacion': d.clasificacion.label if d.clasificacion else None,
                'estado': d.estado_proceso.label if d.estado_proceso else None,
            }
            for d in trip.documents if not d.is_deleted
        ]
        ids_autorizados += [str(d.id) for d in trip.documents if not d.is_deleted]

    return {
        'datos': datos,
        'referencias': {
            'trip_id': str(trip.id),
            'segmentos': len(segments),
            'alojamientos': len(accommodations),
            'vehiculos': len(vehicles),
            'alertas': len(alerts),
            'ambito': 'gestor' if es_gestor else 'viajero',
        },
        'ids_autorizados': ids_autorizados,
        'fecha_datos': utcnow().isoformat(),
    }
