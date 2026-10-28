"""Normalisation of extracted values (specification section 2.3 step 5).

"Normalización de fechas, zonas horarias, aeropuertos, estaciones, localizadores
y nombres."

Normalisation can only lower a field's confidence, never raise it. A date the
model read correctly but whose timezone we could not resolve is *less* certain
than before, and saying so is what keeps the review screen honest.
"""
import logging
import re
from datetime import datetime

from app.models.catalog import Country, Location

logger = logging.getLogger(__name__)

#: Confidence given to a value the catalogue supplied rather than the model.
#:
#: Above a literal match on purpose. A literal match proves the model copied
#: something from the document; reference data is not a copy of anything the
#: model said. A country resolved from an airport code was scored 0.0 -- «not
#: found in the document» -- which is true and exactly backwards as a measure
#: of how much it can be trusted.
CONFIANZA_CATALOGO = 0.9

#: How much confidence survives each kind of uncertainty.
PENALTY_TZ_UNRESOLVED = 0.6
PENALTY_LOCATION_UNRESOLVED = 0.8
PENALTY_DATE_AMBIGUOUS = 0.7

_IATA_RE = re.compile(r'^[A-Z]{3}$')
_LOCATOR_RE = re.compile(r'^[A-Z0-9]{5,8}$')
_CURRENCY_RE = re.compile(r'^[A-Z]{3}$')


class NormalizationResult:
    """The normalised services plus what could not be resolved."""

    def __init__(self, servicios, avisos, resueltos):
        self.servicios = servicios
        self.avisos = avisos
        #: ``{campo: descripcion}`` of what normalisation actually resolved.
        self.resueltos = resueltos

    @property
    def confianza_global(self):
        """The mean per-field confidence across every service."""
        values = [
            v
            for servicio in self.servicios
            for v in (servicio.get('confianzas') or {}).values()
            if isinstance(v, (int, float))
        ]
        return round(sum(values) / len(values), 2) if values else None


def normalize(payload, clasificacion=None):
    """Normalise an extraction payload.

    Args:
        payload: The model's answer, one or more services.
        clasificacion: The document class, which decides which fields exist.

    Returns:
        A :class:`NormalizationResult`.
    """
    servicios = _servicios_del_payload(payload)
    avisos = list((payload or {}).get('avisos') or [])
    resueltos = {}
    normalizados = []

    for indice, servicio in enumerate(servicios):
        campos = _sin_nulos_escritos(dict(servicio.get('campos') or {}))
        confianzas = dict(servicio.get('confianzas') or {})
        propios = []

        _normalize_locators(campos, confianzas, propios)
        _normalize_names(campos)
        _normalize_currency(campos, propios)
        _normalize_locations(campos, confianzas, propios, resueltos)
        _normalize_countries(campos)
        _normalize_place_timezones(campos, confianzas, propios, resueltos)
        _normalize_instants(campos, confianzas, propios, resueltos)
        _comprobar_orden(campos, confianzas, propios)
        _descartar_ciudades_numericas(campos)

        # Say which service a warning is about, or a reviewer looking at two
        # flights cannot tell which one has the unresolved timezone.
        prefijo = f'Servicio {indice + 1}: ' if len(servicios) > 1 else ''
        avisos.extend(f'{prefijo}{a}' for a in propios)

        normalizados.append({
            # How a service is to be applied travels with it, or a rule-based
            # reader's instructions would be lost on the way to approval.
            **_instrucciones(servicio),
            'campos': campos,
            'confianzas': confianzas,
            'procedencias': dict(servicio.get('procedencias') or {}),
        })

    return NormalizationResult(normalizados, avisos, resueltos)


#: What a model writes when it means «no value» but types a string instead.
_NULOS_ESCRITOS = {'null', 'none', 'n/a', 'na', 'nil', 'undefined', '-', '—'}


def _sin_nulos_escritos(campos):
    """«"terminal_origen": "null"» is an absent terminal, not one called null."""
    return {
        nombre: (None if isinstance(valor, str) and valor.strip().lower() in _NULOS_ESCRITOS
                 else valor)
        for nombre, valor in campos.items()
    }


def _instrucciones(servicio):
    """A service's keys other than its fields, confidences and provenance."""
    if not isinstance(servicio, dict):
        return {}
    return {k: v for k, v in servicio.items()
            if k not in ('campos', 'confianzas', 'procedencias')}


#: Instants that must come in this order, start first.
_PARES_ORDENADOS = (
    ('salida', 'llegada'), ('check_in', 'check_out'),
    ('recogida', 'devolucion'), ('inicio', 'fin'), ('ida', 'vuelta'),
)


def _comprobar_orden(campos, confianzas, avisos):
    """Flag an end before its start: a stay that checks out before it checks in.

    Not corrected -- which of the two is wrong is not something to guess --
    but both go to review, and the reviewer is told why.
    """
    for inicio, fin in _PARES_ORDENADOS:
        a, b = campos.get(inicio), campos.get(fin)
        if not (isinstance(a, dict) and isinstance(b, dict)):
            continue
        if a.get('local') and b.get('local') and str(b['local']) < str(a['local']):
            avisos.append(f'«{fin}» ({b["local"]}) es anterior a «{inicio}» '
                          f'({a["local"]}); una de las dos fechas está mal.')
            for campo in (inicio, fin):
                confianzas[campo] = min(confianzas.get(campo, 1.0), PENALTY_DATE_AMBIGUOUS)


def _descartar_ciudades_numericas(campos):
    """A city that is only digits is the postal code next to it, not a city."""
    for campo in ('ciudad', 'origen_ciudad', 'destino_ciudad', 'recogida_ciudad',
                  'devolucion_ciudad'):
        valor = campos.get(campo)
        if isinstance(valor, str) and re.fullmatch(r'[\d\s-]+', valor.strip()):
            campos[campo] = None


def _servicios_del_payload(payload):
    """Read the services out of either payload shape."""
    payload = payload or {}

    servicios = payload.get('servicios')
    if isinstance(servicios, list) and servicios:
        return servicios

    campos = payload.get('campos')
    if isinstance(campos, dict):
        return [{
            'campos': campos,
            'confianzas': payload.get('confianzas') or {},
            'procedencias': payload.get('procedencias') or {},
        }]

    return []


def _normalize_locators(campos, confianzas, avisos):
    """Booking references: uppercase, no spaces, sanity-checked."""
    for field in ('localizador', 'numero_poliza', 'numero'):
        value = campos.get(field)
        if not value or not isinstance(value, str):
            continue
        cleaned = re.sub(r'[\s\-]', '', value).upper()
        campos[field] = cleaned

        if field == 'localizador' and not _LOCATOR_RE.match(cleaned):
            # Airline PNRs are six alphanumerics; rail and hotel references
            # vary, so this is a warning rather than a rejection.
            avisos.append(
                f'El localizador «{cleaned}» no tiene el formato habitual; '
                'compruébelo en el documento.'
            )
            confianzas[field] = min(confianzas.get(field, 1.0), 0.7)


def _normalize_names(campos):
    """Traveller names: collapse whitespace, keep the document's own casing."""
    for field in ('pasajero', 'huesped', 'conductor', 'asegurado', 'titular'):
        value = campos.get(field)
        if value and isinstance(value, str):
            campos[field] = re.sub(r'\s+', ' ', value).strip()


def _normalize_currency(campos, avisos):
    """Currency codes to uppercase ISO-4217, amounts to float."""
    currency = campos.get('moneda')
    if currency and isinstance(currency, str):
        cleaned = currency.strip().upper()
        symbols = {'€': 'EUR', '$': 'USD', '£': 'GBP', '¥': 'JPY'}
        cleaned = symbols.get(cleaned, cleaned)
        campos['moneda'] = cleaned if _CURRENCY_RE.match(cleaned) else None
        if campos['moneda'] is None:
            avisos.append(f'No se ha reconocido la moneda «{currency}».')

    for field in ('importe', 'franquicia'):
        value = campos.get(field)
        if value is None or isinstance(value, (int, float)):
            continue
        try:
            campos[field] = float(str(value).replace(',', '.').replace(' ', ''))
        except (TypeError, ValueError):
            campos[field] = None


def _normalize_locations(campos, confianzas, avisos, resueltos):
    """Resolve airport and station codes against the catalogue.

    The catalogue lookup is what supplies the timezone, so an unresolved
    location cascades into an unresolved instant -- which is precisely why its
    confidence must drop rather than the value being accepted at face value.
    """
    pairs = (
        ('origen_codigo', 'origen_nombre', 'origen_ciudad', 'origen_pais', 'salida'),
        ('destino_codigo', 'destino_nombre', 'destino_ciudad', 'destino_pais', 'llegada'),
    )

    for code_field, name_field, city_field, country_field, instant_field in pairs:
        code = campos.get(code_field)
        name = campos.get(name_field)
        location = None

        if code and isinstance(code, str):
            cleaned = code.strip().upper()
            campos[code_field] = cleaned
            if _IATA_RE.match(cleaned):
                location = Location.query.filter_by(codigo=cleaned, activo=True).first()

        if location is None and name:
            location = _find_by_name(name, campos.get(city_field))

        if location is None:
            if code or name:
                avisos.append(
                    f'No se ha podido identificar «{code or name}» en el catálogo; '
                    'la zona horaria puede ser incorrecta.'
                )
                for field in (code_field, instant_field):
                    if field in confianzas:
                        confianzas[field] = round(
                            confianzas[field] * PENALTY_LOCATION_UNRESOLVED, 2
                        )
            continue

        campos.setdefault(name_field, location.nombre)
        if not campos.get(name_field):
            campos[name_field] = location.nombre
        if not campos.get(city_field):
            campos[city_field] = location.ciudad
        campos[country_field] = location.pais_codigo
        confianzas[country_field] = CONFIANZA_CATALOGO
        campos[f'{code_field}_location_id'] = str(location.id)
        resueltos[code_field] = f'{location.codigo} — {location.nombre}'

        # The catalogue's timezone wins over anything the model guessed: it is
        # reference data, the model's answer is an inference. Filling it in only
        # when absent left the guess standing, and a flight out of BCN whose
        # departure was labelled Europe/London moves the whole itinerary by an
        # hour -- along with every margin the alert engine computes from it.
        instant = campos.get(instant_field)
        if isinstance(instant, dict) and location.zona_horaria:
            anterior = instant.get('zona_horaria')
            if anterior != location.zona_horaria:
                instant['zona_horaria'] = location.zona_horaria
                resueltos[f'{instant_field}.zona_horaria'] = location.zona_horaria
                if anterior:
                    avisos.append(
                        f'La zona horaria de «{instant_field}» era «{anterior}» y '
                        f'no corresponde a {location.codigo}; se ha corregido a '
                        f'«{location.zona_horaria}».'
                    )


def _find_by_name(name, city=None):
    """Find a catalogue location by name, optionally narrowed by city."""
    needle = f'%{str(name).strip().lower()}%'
    query = Location.query.filter(
        Location.activo.is_(True),
        Location.nombre.ilike(needle),
    )
    if city:
        narrowed = query.filter(Location.ciudad.ilike(f'%{str(city).strip().lower()}%'))
        found = narrowed.first()
        if found is not None:
            return found
    return query.first()


def _normalize_instants(campos, confianzas, avisos, resueltos):
    """Parse each instant and make sure it carries a usable timezone."""
    from app.services import settings_service

    default_tz = settings_service.zona_horaria_por_defecto()

    for field, value in list(campos.items()):
        if not isinstance(value, dict) or 'local' not in value:
            continue

        raw = value.get('local')
        if not raw:
            continue

        parsed = _parse_datetime(raw)
        if parsed is None:
            avisos.append(f'No se ha podido interpretar la fecha «{raw}» del campo {field}.')
            confianzas[field] = round(
                confianzas.get(field, 1.0) * PENALTY_DATE_AMBIGUOUS, 2
            )
            campos[field] = {'local': None, 'zona_horaria': value.get('zona_horaria')}
            continue

        value['local'] = parsed.isoformat(timespec='minutes')

        zone = value.get('zona_horaria')
        if zone:
            from app.utils.timeutil import is_valid_timezone

            if not is_valid_timezone(zone):
                avisos.append(
                    f'La zona horaria «{zone}» del campo {field} no es válida; '
                    f'se usará {default_tz}. Confírmelo antes de aprobar.'
                )
                value['zona_horaria'] = default_tz
                confianzas[field] = round(
                    confianzas.get(field, 1.0) * PENALTY_TZ_UNRESOLVED, 2
                )
        else:
            # No zone and none derivable: the value is usable but the derived
            # UTC instant is a guess, and every margin computed from it inherits
            # that guess. The reviewer must see that.
            value['zona_horaria'] = default_tz
            avisos.append(
                f'No se ha podido determinar la zona horaria de {field}; se asume '
                f'{default_tz}. Confírmelo antes de aprobar.'
            )
            confianzas[field] = round(
                confianzas.get(field, 1.0) * PENALTY_TZ_UNRESOLVED, 2
            )

        campos[field] = value


#: Formats seen on real booking documents, most specific first.
_DATE_FORMATS = (
    '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
    '%d/%m/%Y %H:%M', '%d-%m-%Y %H:%M', '%d.%m.%Y %H:%M',
    '%m/%d/%Y %H:%M',
    '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y',
)


#: Two-digit years, as an Italian invoice writes «Dal 03/08/26». Tried after
#: the four-digit forms, which they would otherwise shadow.
_DATE_FORMATS_CORTOS = (
    '%d/%m/%y %H:%M', '%d-%m-%y %H:%M', '%d.%m.%y %H:%M',
    '%d/%m/%y', '%d-%m-%y', '%d.%m.%y',
)

#: Month names in the languages travel documents arrive in, to their number.
_MESES = {
    nombre: numero
    for numero, nombres in enumerate((
        ('enero', 'january', 'jan', 'gennaio', 'janvier', 'januar', 'ene', 'janeiro'),
        ('febrero', 'february', 'feb', 'febbraio', 'fevrier', 'februar', 'fevereiro'),
        ('marzo', 'march', 'mar', 'mars', 'marz', 'marco'),
        ('abril', 'april', 'apr', 'abr', 'aprile', 'avril'),
        ('mayo', 'may', 'maggio', 'mai', 'maio'),
        ('junio', 'june', 'jun', 'giugno', 'juin', 'juni', 'junho'),
        ('julio', 'july', 'jul', 'luglio', 'juillet', 'juli', 'julho'),
        ('agosto', 'august', 'aug', 'ago', 'aout'),
        ('septiembre', 'setiembre', 'september', 'sep', 'sept', 'settembre',
         'septembre', 'setembro'),
        ('octubre', 'october', 'oct', 'ottobre', 'octobre', 'oktober', 'okt', 'outubro'),
        ('noviembre', 'november', 'nov', 'novembre', 'novembro'),
        ('diciembre', 'december', 'dec', 'dic', 'dicembre', 'decembre', 'dezember',
         'dez', 'dezembro'),
    ), start=1)
    for nombre in nombres
}

#: Words around a date that say nothing about it: weekdays, «de», «at».
_RELLENO_DE_FECHA = re.compile(
    r'(?i)\b(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|monday|'
    r'tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|'
    r'sun|luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|'
    r'domenica|de|del|of|at|a las|um|le|alle|h)\b\.?,?'
)


def _parse_datetime(raw):
    """Parse a datetime string, preferring day-first as European documents use.

    Numeric forms first, then the written ones a model copies from the page --
    «9 November 2026 11:30», «Miércoles 10/06/2026», «03/08/26». A time with
    no date is still refused: it is half an instant, and the day it belongs
    to is not something to guess.
    """
    text = str(raw).strip().replace('Z', '')
    # «07/08/2026T00:00»: a European date glued to an ISO time, as some
    # models write it. The «T» is only a separator there.
    text = re.sub(r'^(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})T', r'\1 ', text)
    for fmt in _DATE_FORMATS + _DATE_FORMATS_CORTOS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    return _fecha_escrita(text)


def _fecha_escrita(texto):
    """«9 November 2026 11:30» or «Miércoles 10/06/2026» as a datetime, or None."""
    import unicodedata

    plano = ''.join(c for c in unicodedata.normalize('NFKD', texto.lower())
                    if not unicodedata.combining(c))
    plano = _RELLENO_DE_FECHA.sub(' ', plano)
    hora = re.search(r'\b(\d{1,2})[:.h](\d{2})\b', plano)
    if hora:
        plano = plano[:hora.start()] + ' ' + plano[hora.end():]

    dia = mes = anio = None
    numerica = re.search(r'\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b', plano)
    if numerica:
        dia, mes, anio = (int(g) for g in numerica.groups())
    else:
        escrita = re.search(r'\b(\d{1,2})\s+([a-z]{3,10})\.?\s+(\d{2,4})\b', plano) \
            or re.search(r'\b([a-z]{3,10})\.?\s+(\d{1,2}),?\s+(\d{4})\b', plano)
        if escrita:
            a, b, c = escrita.groups()
            if a.isdigit():
                dia, mes, anio = int(a), _MESES.get(b), int(c)
            else:
                dia, mes, anio = int(b), _MESES.get(a), int(c)
    if not (dia and mes and anio):
        return None
    if anio < 100:
        anio += 2000
    try:
        return datetime(anio, mes, dia,
                        int(hora.group(1)) if hora else 0,
                        int(hora.group(2)) if hora else 0)
    except ValueError:
        return None


#: Services located by a place rather than by a station code: which instants
#: they carry, and which fields say where they happen.
_LUGARES_CON_INSTANTES = (
    (('check_in', 'check_out'), ('ciudad',), 'pais'),
    (('recogida',), ('recogida_ciudad', 'recogida_lugar'), 'recogida_pais'),
    (('devolucion',), ('devolucion_ciudad', 'devolucion_lugar'), 'devolucion_pais'),
    (('inicio', 'fin'), ('ciudad', 'lugar'), 'pais'),
)


def _normalize_place_timezones(campos, confianzas, avisos, resueltos):
    """Give a hotel, a car or a service the timezone of where it happens.

    Airports arrive with an IATA code the catalogue resolves; a hotel arrives
    with a city name, so nothing corrected the zone the model guessed. A stay in
    London came back labelled Europe/Madrid, which moves its check-in by an hour
    and with it the night the alert engine thinks is covered.

    The catalogue wins over the guess, as it does for airports, and the
    correction is reported rather than made in silence.
    """
    for prefijos, campos_lugar, campo_pais in _LUGARES_CON_INSTANTES:
        if not any(isinstance(campos.get(p), dict) for p in prefijos):
            continue

        zona, pais, _ = resolver_lugar(
            [campos.get(c) for c in campos_lugar], campos.get(campo_pais)
        )

        # The country too, while the catalogue is answering: a stay with no
        # country gives the security advisories nothing to look up.
        if pais and not campos.get(campo_pais):
            campos[campo_pais] = pais
            confianzas[campo_pais] = CONFIANZA_CATALOGO
            resueltos[campo_pais] = pais

        if not zona:
            continue

        for prefijo in prefijos:
            instante = campos.get(prefijo)
            if not isinstance(instante, dict):
                continue
            anterior = instante.get('zona_horaria')
            if anterior == zona:
                continue
            instante['zona_horaria'] = zona
            resueltos[f'{prefijo}.zona_horaria'] = zona
            if anterior:
                avisos.append(
                    f'La zona horaria de «{prefijo}» era «{anterior}» y no '
                    f'corresponde al lugar; se ha corregido a «{zona}».'
                )


def _por_alias(aguja):
    """Match a name against the catalogue's alternative spellings.

    Resolved in Python rather than in SQL: ``alias`` is a JSON array, and
    querying inside one is written differently on PostgreSQL and on SQLite --
    which would make the tests pass against a matching rule production never
    runs. Eighty-five rows cost nothing to scan.

    Returns a criterion to add to the query, or None when nothing matched, so
    the caller keeps one code path for all three ways of finding a place.
    """
    plano = _sin_acentos(aguja)
    if not plano:
        return None

    codigos = [
        fila.codigo
        for fila in Location.query.filter(Location.activo.is_(True)).all()
        if fila.codigo and any(
            _sin_acentos(a) == plano for a in (fila.alias or ())
        )
    ]
    return Location.codigo.in_(codigos) if codigos else None


def _sin_acentos(texto):
    """Lowercase without accents, so «Zürich» and «Zurich» are one name."""
    import unicodedata

    plano = unicodedata.normalize('NFKD', str(texto or ''))
    return ''.join(c for c in plano if not unicodedata.combining(c)).strip().lower()


def resolver_lugar(nombres, pais_codigo=None):
    """The timezone and country of a named place, from the catalogue.

    A hotel arrives named by its city, and the city may not be spelled the way
    the catalogue holds it: the model wrote «London» where the catalogue says
    «Londres», so an exact match on the city found nothing and the stay kept the
    zone the model had guessed. The airport's own name carries the city too, so
    that is tried next.

    Refuses rather than guesses. When several entries match and disagree about
    the timezone, nothing is returned: «London» matching two zones is a question
    for a person, not something to resolve by picking the first row.

    Returns ``(zona, pais, ciudad)``, where ``ciudad`` is the catalogue's own
    spelling -- the one thing that lets two services agree that «London» and
    «Londres» are the same place. Any of the three may be None.
    """
    for nombre in nombres:
        if not nombre or not isinstance(nombre, str):
            continue
        aguja = nombre.strip()
        if len(aguja) < 3:
            continue

        for criterio in (
            Location.ciudad.ilike(aguja),
            _por_alias(aguja),
            Location.nombre.ilike(f'%{aguja}%'),
        ):
            if criterio is None:
                continue
            filas = Location.query.filter(
                Location.activo.is_(True),
                Location.zona_horaria.isnot(None),
                criterio,
            ).all()
            if not filas:
                continue

            zonas = {f.zona_horaria for f in filas}
            if len(zonas) > 1:
                break

            paises = {f.pais_codigo for f in filas if f.pais_codigo}
            ciudades = {f.ciudad for f in filas if f.ciudad}
            return (
                zonas.pop(),
                paises.pop() if len(paises) == 1 else None,
                ciudades.pop() if len(ciudades) == 1 else None,
            )

    if pais_codigo and isinstance(pais_codigo, str) and len(pais_codigo) == 2:
        country = Country.query.filter_by(codigo=pais_codigo.upper()).first()
        if country and country.zona_horaria_principal:
            return country.zona_horaria_principal, country.codigo, None

    return None, None, None


def _normalize_countries(campos):
    """Country codes to uppercase ISO-3166 alpha-2, resolved by name if needed."""
    for field in ('pais', 'pais_emisor', 'origen_pais', 'destino_pais',
                  'recogida_pais', 'devolucion_pais'):
        value = campos.get(field)
        if not value or not isinstance(value, str):
            continue
        cleaned = value.strip()

        if len(cleaned) == 2:
            campos[field] = cleaned.upper()
            continue

        country = Country.query.filter(
            Country.nombre.ilike(cleaned)
        ).first() or Country.query.filter(
            Country.nombre_en.ilike(cleaned)
        ).first()
        campos[field] = country.codigo if country else None
