"""Internal travel requests: the form an employee fills in before a trip.

A request is not a booking. It says who travels, why, where to, on which days
and what they need, and approving it completes the trip itself rather than
adding anything to the itinerary -- which is why it never goes through the
booking extraction: asked to extract «a document of type flight» from a form
whose «AVIÓN» box is ticked, a model proposes a flight that nobody booked.

The form is read by its labels, not by its cell positions. The template has
already been edited once by whoever owns it, and a reader tied to «C15» would
stop finding the destination the next time a row is inserted above it.
"""
import logging
import re
import unicodedata
from datetime import datetime, time

from app.models.enums import ApplicationOutcome
from app.models.extraction import ExtractionApplication

logger = logging.getLogger(__name__)

#: Normalised label -> field. A label names the cell beside it.
_ETIQUETAS = {
    'fecha solicitud': 'fecha_solicitud',
    'empleado': 'empleado',
    'departamento': 'departamento',
    'solicitante viaje': 'solicitante',
    'solicitante': 'solicitante',
    'cliente': 'a_cargo_cliente',
    'idb': 'a_cargo_idb',
    'motivo viaje': 'motivo',
    'motivo': 'motivo',
    'proyecto ot': 'proyecto',
    'proyecto': 'proyecto',
    'destino': 'destino',
    'fecha ida': 'fecha_ida',
    'fecha vuelta': 'fecha_vuelta',
    'hora ida': 'hora_ida',
    'hora vuelta': 'hora_vuelta',
    'avion': 'medio_avion',
    'hotel': 'medio_hotel',
    'coche': 'medio_coche',
    'tren': 'medio_tren',
}

#: Labels that head a group and have no value of their own.
_AGRUPADORES = {'viaje a cargo de', 'peticiones horarios', 'medios solicitados'}

#: What only the request form says. A budget sheet in the same workbook names
#: the destination and the project too, so those cannot identify the form.
_PROPIAS_DE_LA_SOLICITUD = {'fecha_ida', 'fecha_vuelta', 'motivo', 'empleado', 'solicitante'}

_MEDIOS = (
    ('medio_avion', 'Avión'), ('medio_tren', 'Tren'),
    ('medio_hotel', 'Hotel'), ('medio_coche', 'Coche'),
)
_MARCAS_SI = {'x', 'si', 's', 'yes', '1', 'true', 'ok', '✓', '✔'}
_MARCAS_NO = {'no', 'n', '0', 'false', '-'}

#: Confidence of a value read beside its label, and of a time this module had
#: to assume because the form gave only a day. The second is below the review
#: threshold on purpose: the reviewer should see that the hour is ours.
CONFIANZA_LEIDA = 0.95
CONFIANZA_HORA_SUPUESTA = 0.6

#: When a day has no hour: a trip starts at the start of its first day and ends
#: at the end of its last. Midnight for both would leave the return day outside
#: the trip, and every check of «is the traveller covered that night» with it.
_HORA_INICIO = time(0, 0)
_HORA_FIN = time(23, 59)

_FECHA_ISO = re.compile(r'^(\d{4})-(\d{2})-(\d{2})')
_FECHA_EUROPEA = re.compile(r'^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$')
_HORA = re.compile(r'^(\d{1,2})(?:[:.h](\d{2}))?\s*h?$')


def _normalizado(texto):
    sin_tildes = unicodedata.normalize('NFKD', str(texto or ''))
    sin_tildes = ''.join(c for c in sin_tildes if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', ' ', sin_tildes.lower()).strip()


def _filas(texto):
    """A page's text as rows of cells.

    A spreadsheet arrives one row per line with ``|`` between cells; any other
    document is one cell per line, and a label is then told from its value by
    the colon.
    """
    for linea in (texto or '').splitlines():
        celdas = [c.strip() for c in linea.split(' | ')]
        celdas = [c for c in celdas if c]
        if celdas:
            yield celdas


def _etiqueta_de(celda):
    """``(field, value-in-the-same-cell)`` when the cell is a label."""
    completa = _normalizado(celda)
    if completa in _ETIQUETAS:
        return _ETIQUETAS[completa], None
    if completa in _AGRUPADORES:
        return '', None
    if ':' in celda:
        antes, despues = celda.split(':', 1)
        clave = _normalizado(antes)
        if clave in _ETIQUETAS:
            return _ETIQUETAS[clave], despues.strip() or None
    return None, None


def leer_campos(texto):
    """``{field: (value, line)}`` for every label of the form found in ``texto``."""
    encontrados = {}
    for celdas in _filas(texto):
        for posicion, celda in enumerate(celdas):
            campo, valor = _etiqueta_de(celda)
            if not campo:
                continue
            if valor is None:
                siguiente = celdas[posicion + 1] if posicion + 1 < len(celdas) else None
                if siguiente is not None and _etiqueta_de(siguiente)[0] is None:
                    valor = siguiente
            if valor and campo not in encontrados:
                encontrados[campo] = (valor, ' | '.join(celdas))
            elif campo not in encontrados:
                # An empty box is still evidence that this is the form.
                encontrados.setdefault(f'_{campo}', (None, None))
    return encontrados


def _etiquetas_presentes(texto):
    return {k.lstrip('_') for k in leer_campos(texto)}


def es_solicitud(texto):
    """Whether a page is the request form, filled in or not."""
    return len(_etiquetas_presentes(texto) & _PROPIAS_DE_LA_SOLICITUD) >= 3


def extraer(document):
    """Read a request into an extraction payload, without a model.

    The shape is the one booking extractions use -- one «service» with its
    fields, confidences and provenance -- so the review screen, corrections and
    versioning work on it unchanged.
    """
    paginas = sorted(document.pages, key=lambda p: p.numero) if document.pages else []
    if not paginas and document.texto:
        paginas = [_PaginaUnica(document.texto.contenido)]

    pagina = next((p for p in paginas if es_solicitud(p.contenido)), None)
    avisos = []
    if pagina is None:
        avisos.append(
            'No se ha reconocido el formulario de solicitud en el documento; '
            'complete los datos a mano.'
        )
        return {'servicios': [{'campos': {}, 'confianzas': {}, 'procedencias': {}}],
                'avisos': avisos, 'confianza_global': 0.0}

    leidos = {k: v for k, v in leer_campos(pagina.contenido).items()
              if not k.startswith('_')}
    campos, confianzas, procedencias = {}, {}, {}

    def poner(campo, valor, origenes, confianza=CONFIANZA_LEIDA):
        if valor in (None, ''):
            return
        campos[campo] = valor
        confianzas[campo] = confianza
        lineas = [leidos[o][1] for o in origenes if o in leidos]
        procedencias[campo] = {'pagina': pagina.numero,
                               'fragmento': '\n'.join(lineas) or None}

    for campo in ('empleado', 'departamento', 'solicitante', 'motivo',
                  'proyecto', 'destino'):
        if campo in leidos:
            poner(campo, ' '.join(leidos[campo][0].split()), [campo])

    if 'fecha_solicitud' in leidos:
        dia = _dia(leidos['fecha_solicitud'][0])
        poner('fecha_solicitud', dia.isoformat() if dia else leidos['fecha_solicitud'][0],
              ['fecha_solicitud'])

    a_cargo = []
    if 'a_cargo_cliente' in leidos:
        a_cargo.append(f"Cliente: {leidos['a_cargo_cliente'][0]}")
    if 'a_cargo_idb' in leidos:
        valor = leidos['a_cargo_idb'][0]
        a_cargo.append('IDB' if _normalizado(valor) in _MARCAS_SI else f'IDB: {valor}')
    poner('a_cargo_de', '; '.join(a_cargo), ['a_cargo_cliente', 'a_cargo_idb'])

    medios = []
    for campo, nombre in _MEDIOS:
        if campo not in leidos:
            continue
        valor = leidos[campo][0]
        marca = _normalizado(valor)
        if marca in _MARCAS_NO:
            continue
        medios.append(nombre if marca in _MARCAS_SI else f'{nombre} ({valor})')
    poner('medios', ', '.join(medios), [c for c, _ in _MEDIOS])

    for tramo, hora_por_defecto in (('ida', _HORA_INICIO), ('vuelta', _HORA_FIN)):
        _instante(tramo, hora_por_defecto, leidos, poner, avisos)

    for campo, explicacion in (
        ('fecha_ida', 'la fecha de ida'), ('fecha_vuelta', 'la fecha de vuelta'),
        ('destino', 'el destino'), ('empleado', 'quién viaja'),
    ):
        if campo not in leidos:
            avisos.append(f'La solicitud no indica {explicacion}.')

    if 'empleado' in campos and buscar_persona(campos['empleado']) is None:
        avisos.append(
            f"No hay una única persona usuaria llamada «{campos['empleado']}»: "
            'al aprobar no se asignará como viajera; hágalo a mano.'
        )

    return {
        'servicios': [{'campos': campos, 'confianzas': confianzas,
                       'procedencias': procedencias}],
        'avisos': avisos,
        'confianza_global': min(confianzas.values()) if confianzas else 0.0,
    }


class _PaginaUnica:
    numero = 1

    def __init__(self, contenido):
        self.contenido = contenido


def _instante(tramo, hora_por_defecto, leidos, poner, avisos):
    """The trip's start or end from the form's day and its preferred hour."""
    from app.services import settings_service

    clave_fecha, clave_hora = f'fecha_{tramo}', f'hora_{tramo}'
    if clave_fecha not in leidos:
        return

    crudo = leidos[clave_fecha][0]
    dia = _dia(crudo)
    if dia is None:
        avisos.append(f'No se ha podido interpretar la fecha de {tramo} «{crudo}».')
        poner(tramo, {'local': None, 'zona_horaria': None}, [clave_fecha], 0.0)
        return

    preferida = leidos.get(clave_hora, (None, None))[0]
    hora = _hora(preferida) or _hora_de(crudo)
    if preferida and hora is None:
        # «por la mañana» is a preference for whoever books, not an instant.
        poner(f'horario_{tramo}', preferida, [clave_hora])

    # The request is made where the company is, and a trip starts and ends at
    # home: the default zone is the honest one here, not a guess about a place.
    zona = settings_service.zona_horaria_por_defecto()
    instante = datetime.combine(dia, hora or hora_por_defecto)
    poner(
        tramo, {'local': instante.isoformat(timespec='minutes'), 'zona_horaria': zona},
        [clave_fecha, clave_hora],
        CONFIANZA_LEIDA if hora else CONFIANZA_HORA_SUPUESTA,
    )
    if hora is None:
        avisos.append(
            f'La solicitud no indica la hora de {tramo}; se ha puesto '
            f'{hora_por_defecto.strftime("%H:%M")}. Corríjala si la conoce.'
        )


def _dia(valor):
    texto = str(valor or '').strip()
    coincide = _FECHA_ISO.match(texto)
    try:
        if coincide:
            return datetime(*(int(g) for g in coincide.groups())).date()
        coincide = _FECHA_EUROPEA.match(texto)
        if coincide:
            dia, mes, anio = (int(g) for g in coincide.groups())
            if anio < 100:
                anio += 2000
            return datetime(anio, mes, dia).date()
    except ValueError:
        return None
    return None


def _hora(valor):
    coincide = _HORA.match(str(valor or '').strip().lower())
    if not coincide:
        return None
    horas, minutos = int(coincide.group(1)), int(coincide.group(2) or 0)
    if horas > 23 or minutos > 59:
        return None
    return time(horas, minutos)


def _hora_de(valor):
    """The hour of an ISO date-time, when the cell held one rather than a day."""
    coincide = re.match(r'^\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})', str(valor or ''))
    return _hora(coincide.group(1)) if coincide else None


def buscar_persona(nombre):
    """The one user this name designates, or None.

    Exact after normalising, and unique. Two people with the same name are
    exactly the case where picking one assigns the wrong traveller, so that is
    left to a person.
    """
    from app.models.user import User

    buscado = _normalizado(nombre)
    if not buscado:
        return None
    candidatos = [
        u for u in User.query.filter(User.is_deleted.is_(False)).all()
        if buscado in (_normalizado(u.nombre_completo), _normalizado(u.username),
                       _normalizado(u.email))
    ]
    return candidatos[0] if len(candidatos) == 1 else None


# ======================================================================
# Approval
# ======================================================================
#: Trip fields a request fills, and the request field each comes from.
_CAMPOS_DEL_VIAJE = (
    ('finalidad_detalle', 'motivo', 500),
    ('proyecto', 'proyecto', 120),
)


def aplicar(actor, document, extraction, campos):
    """Complete the trip from an approved request.

    Empty fields only, like every other derivation onto a trip: what a manager
    typed stands. The one exception is a field this same request filled on an
    earlier approval and nobody has touched since -- that is how a correction
    made on the review screen reaches the trip on re-approval.

    Returns the :class:`ExtractionApplication` rows describing what happened.
    """
    from app.services import trip_service

    trip = document.trip
    extra = dict(trip.datos_extra or {})
    previo = dict((extra.get('solicitud_interna') or {}).get('aplicado') or {})
    aplicado = {}
    cambios = {}

    def libre(nombre, actual):
        return actual in (None, '') or previo.get(nombre) == _comparable(actual)

    for campo_viaje, campo, limite in _CAMPOS_DEL_VIAJE:
        valor = campos.get(campo)
        if valor and libre(campo_viaje, getattr(trip, campo_viaje)):
            cambios[campo_viaje] = str(valor)[:limite]

    for prefijo, tramo in (('inicio', 'ida'), ('fin', 'vuelta')):
        instante = campos.get(tramo) or {}
        local = _local(instante.get('local'))
        if local and libre(prefijo, getattr(trip, f'{prefijo}_local')):
            cambios[f'{prefijo}_local'] = local
            cambios[f'{prefijo}_tz'] = instante.get('zona_horaria')

    importe = _numero(campos.get('importe'))
    if importe is not None and libre('coste_estimado', trip.coste_estimado):
        cambios['coste_estimado'] = importe
        cambios['moneda'] = (str(campos.get('moneda') or '') or 'EUR')[:3].upper()

    resumen = _resumen(campos)
    if resumen and libre('observaciones', trip.observaciones):
        cambios['observaciones'] = resumen

    if cambios:
        trip_service.update_trip(actor, trip, commit=False, **cambios)
        for nombre in cambios:
            if nombre.endswith('_tz'):
                continue
            clave = nombre.removesuffix('_local')
            aplicado[clave] = _comparable(getattr(trip, nombre))

    aplicaciones = [ExtractionApplication(
        extraction_id=extraction.id,
        entidad_tipo='viaje',
        entidad_id=trip.id,
        resultado=ApplicationOutcome.ACTUALIZADA if cambios else ApplicationOutcome.IGNORADA,
        campos_aplicados=sorted(aplicado),
        motivo=None if cambios else (
            'El viaje ya tenía estos datos; no se ha sobrescrito nada.'
        ),
    )]
    aplicaciones.extend(_viajero(actor, trip, extraction, campos.get('empleado')))
    aplicaciones.extend(_destino(actor, trip, extraction, campos.get('destino')))

    extra['solicitud_interna'] = {
        'document_id': str(document.id),
        'campos': {k: v for k, v in campos.items() if not isinstance(v, dict)},
        'aplicado': {**previo, **aplicado},
    }
    trip.datos_extra = extra
    return aplicaciones


def _viajero(actor, trip, extraction, nombre):
    from app.services import trip_service

    if not nombre:
        return []
    persona = buscar_persona(nombre)
    if persona is None:
        return [ExtractionApplication(
            extraction_id=extraction.id, entidad_tipo='viajero',
            resultado=ApplicationOutcome.IGNORADA,
            motivo=f'No hay una única persona usuaria llamada «{nombre}»'[:400],
        )]
    if trip.traveler_for(persona.id) is not None:
        return []
    viajero = trip_service.add_traveler(actor, trip, persona, commit=False)
    return [ExtractionApplication(
        extraction_id=extraction.id, entidad_tipo='viajero',
        entidad_id=viajero.id, resultado=ApplicationOutcome.CREADA,
        campos_aplicados=['empleado'],
    )]


def _destino(actor, trip, extraction, texto):
    """Add the requested destination unless the trip already goes there.

    Resolved against the catalogue when the city is in it, so the advisories
    get a country and the timezone is right. When it is not, the city is still
    recorded -- but without a country, rather than with one guessed from the
    name.
    """
    from app.models.catalog import Location
    from app.services import trip_service

    if not texto:
        return []
    ciudad = texto.split(',')[0].strip()
    clave = _normalizado(ciudad)
    if any(_normalizado(d.ciudad) == clave for d in trip.destinations):
        return []

    lugar = next(
        (loc for loc in Location.query.filter(Location.activo.is_(True),
                                               Location.ciudad.ilike(ciudad)).all()
         if _normalizado(loc.ciudad) == clave),
        None,
    )
    destino = trip_service.add_destination(
        actor, trip, ciudad=lugar.ciudad if lugar else ciudad,
        pais_codigo=lugar.pais_codigo if lugar else None,
        location=lugar, commit=False,
    )
    return [ExtractionApplication(
        extraction_id=extraction.id, entidad_tipo='destino',
        entidad_id=destino.id, resultado=ApplicationOutcome.CREADA,
        campos_aplicados=['destino'],
        motivo=None if lugar else (
            'Ciudad no encontrada en el catálogo: complete el país del destino.'
        ),
    )]


def _resumen(campos):
    """What the request says that the trip has no column for."""
    lineas = [
        f'{etiqueta}: {campos[campo]}'
        for campo, etiqueta in (
            ('empleado', 'Empleado'), ('departamento', 'Departamento'),
            ('solicitante', 'Solicitante'), ('a_cargo_de', 'A cargo de'),
            ('medios', 'Medios solicitados'), ('horario_ida', 'Horario de ida'),
            ('horario_vuelta', 'Horario de vuelta'),
            ('fecha_solicitud', 'Fecha de la solicitud'),
        )
        if campos.get(campo)
    ]
    return ('Solicitud interna\n' + '\n'.join(lineas)) if lineas else None


def _numero(valor):
    try:
        return round(float(str(valor).replace(',', '.')), 2) if valor not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _local(valor):
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor))
    except ValueError:
        return None


def _comparable(valor):
    """A value as it can be kept in JSON and compared with what was kept."""
    from decimal import Decimal

    if isinstance(valor, Decimal):
        return float(valor)
    return valor.isoformat() if hasattr(valor, 'isoformat') else valor
