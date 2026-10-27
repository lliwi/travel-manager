"""Sale stubs from the travel agency: «Talón de venta» of Viajes El Corte Inglés.

What the agency hands over after it books: one line per service with the
voucher or ticket number, a short concept, and what it cost. It says what was
bought and for how much, and almost never at what time -- the flight line
carries a day and a route, the hotel line a pair of days. So the stub is a
*cost and reference* document, and it is applied as one:

- When the itinerary already has the service (its confirmation was uploaded
  first), the stub completes it: the amount, the voucher, the ticket number,
  and only the fields that row had empty.
- When it does not, the stub proposes the service, marked provisional, and
  with the hour it lacks flagged for review. A later confirmation of the same
  service takes that row over instead of adding a second one.
- A leg the stub names without a day -- the «/BCN» of «BCN/MUC/BCN» -- is only
  ever matched, never created: a flight with no date is a row the alert engine
  can do nothing with but complain about.

The layout is fixed, so it is read by rules: the same answer every time, and
nothing sent to a model.
"""
import re
import unicodedata
from datetime import date

#: Confidence of a value read from its column, and of a time the stub does not
#: state. The second is below the review threshold on purpose.
CONFIANZA_LEIDA = 0.95
CONFIANZA_SIN_HORA = 0.3

MONEDA = 'EUR'

#: Marks the stub carries on every page, in both of its languages.
_MARCAS = ('talon de venta', 'relacion de servicios', 'expediente')

_IMPORTE = r'\d{1,3}(?:\.\d{3})*,\d{2}'
#: «01768973 HTL: ENSO HOTEL 353,30 RE 353,30» -- voucher, concept, base,
#: tax type, optionally the non-taxed base and the fee, and the line total.
_LINEA = re.compile(
    rf'^(?P<bono>\d{{6,}})\s+(?P<concepto>.+?)\s+(?P<base>{_IMPORTE})\s+'
    rf'(?P<tipo>[A-Z]{{2}})(?:\s+(?P<bns>{_IMPORTE}))?(?:\s+(?P<cuota>{_IMPORTE}))?'
    rf'\s+(?P<total>{_IMPORTE})$'
)
#: «Comisión por compra 14,85 EX 0,00 14,85»: a charge with no voucher of its
#: own, which belongs to the service above it.
_CARGO = re.compile(
    rf'^(?P<concepto>[^\d].*?)\s+(?P<base>{_IMPORTE})\s+(?P<tipo>[A-Z]{{2}})'
    rf'(?:\s+{_IMPORTE})*\s+(?P<total>{_IMPORTE})$'
)
_CABECERA = re.compile(r'(\d{5}/\d{2}/\d+)\s+(\d{2})-(\d{2})-(\d{4})\s+(\S+)')
_TOTAL = re.compile(rf'importe total de los servicios.*?({_IMPORTE})', re.IGNORECASE)
_VIAJERO = re.compile(r'Viajero\s*/\s*Viatger\s+(.+)', re.IGNORECASE)

_VUELO = re.compile(r'^(?P<aerolinea>[A-Z0-9]{2})\s*-\s*(?P<clase>[A-Z])\s*-\s*'
                    r'(?P<dia>\d{1,2})/(?P<mes>\d{1,2})')
_RUTA = re.compile(r'^[A-Z]{3}(?:/[A-Z]{3})+$')
_ESTANCIA = re.compile(r'^(?:(?P<hab>[A-Z]{2,4})/(?P<reg>[A-Z]{2})\s+)?'
                       r'(?P<d1>\d{1,2})/(?P<m1>\d{1,2})\s*-\s*(?P<d2>\d{1,2})/(?P<m2>\d{1,2})$')

#: The agency's room and board codes, in words a reviewer reads.
HABITACIONES = {
    'DUI': 'Doble uso individual', 'IND': 'Individual', 'SGL': 'Individual',
    'DBL': 'Doble', 'TWN': 'Doble (dos camas)', 'TPL': 'Triple',
}
REGIMENES = {
    'SA': 'Solo alojamiento', 'AD': 'Alojamiento y desayuno',
    'MP': 'Media pensión', 'PC': 'Pensión completa', 'TI': 'Todo incluido',
}


def _plano(texto):
    sin_tildes = unicodedata.normalize('NFKD', str(texto or ''))
    return ''.join(c for c in sin_tildes if not unicodedata.combining(c)).lower()


def es_talon(texto):
    """Whether a text is an agency sale stub."""
    plano = _plano(texto)
    return all(marca in plano for marca in _MARCAS) and bool(_lineas_de_servicio(texto))


def _importe(texto):
    return round(float(texto.replace('.', '').replace(',', '.')), 2)


def _lineas_de_servicio(texto):
    """The lines between the services header and the payment block."""
    lineas = [linea.strip() for linea in (texto or "").splitlines()]
    dentro, salida = False, []
    for linea in lineas:
        plano = _plano(linea)
        if plano.startswith('relacion de servicios'):
            dentro = True
            continue
        if plano.startswith('forma de pago'):
            break
        if dentro and linea and not plano.startswith(('bono', 'bitllet', 'concepto')):
            salida.append(linea)
    return salida


def _anio_de(dia, mes, emitido):
    """The year of a «dd/mm» on a stub issued on ``emitido``.

    The stub is issued before the service, so a month earlier than the issue
    month is next year's: a stub of December for a hotel in January.
    """
    anio = emitido.year if emitido else date.today().year
    if emitido and mes < emitido.month - 1:
        anio += 1
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def leer(texto):
    """The stub's header and its services, as found in the text."""
    cabecera = _CABECERA.search(texto or '')
    emitido = None
    if cabecera:
        try:
            emitido = date(int(cabecera.group(4)), int(cabecera.group(3)),
                           int(cabecera.group(2)))
        except ValueError:
            emitido = None

    viajero = _VIAJERO.search(texto or '')
    nombre = None
    if viajero:
        # The column is narrow: «PLANELLS BONED CAROLINA(A» is a name cut off
        # mid-word, and the fragment after the bracket is not part of it.
        nombre = re.sub(r'\(.*$', '', viajero.group(1)).strip() or None

    total = _TOTAL.search(texto or '')
    servicios = []
    for linea in _lineas_de_servicio(texto):
        principal = _LINEA.match(linea)
        if principal:
            servicios.append({
                'bono': principal.group('bono'),
                'concepto': principal.group('concepto').strip(),
                'importe': _importe(principal.group('total')),
                'cargos': [], 'detalle': [], 'linea': linea,
            })
            continue
        cargo = _CARGO.match(linea)
        if cargo and servicios:
            servicios[-1]['cargos'].append(
                (cargo.group('concepto').strip(), _importe(cargo.group('total'))))
            continue
        if servicios:
            servicios[-1]['detalle'].append(linea)

    return {
        'expediente': cabecera.group(1) if cabecera else None,
        'documento': cabecera.group(5) if cabecera else None,
        'emitido': emitido,
        'viajero': nombre,
        'total': _importe(total.group(1)) if total else None,
        'servicios': servicios,
    }


def clasificacion(texto):
    """The document class a stub belongs to: what its first service is."""
    from app.models.enums import DocumentClassification

    for servicio in leer(texto)['servicios']:
        if servicio['concepto'].upper().startswith('HTL'):
            return DocumentClassification.HOTEL
        if _VUELO.match(servicio['concepto']):
            return DocumentClassification.VUELO
    return DocumentClassification.OTRO


def extraer(document):
    """Read a stub into an extraction payload, without a model."""
    from app.models.enums import DocumentClassification

    paginas = sorted(document.pages, key=lambda p: p.numero) if document.pages else []
    pagina = next((p for p in paginas if es_talon(p.contenido)), None)
    if pagina is None:
        return {'servicios': [], 'avisos': ['No se ha reconocido el talón de venta.'],
                'confianza_global': 0.0}

    datos = leer(pagina.contenido)
    clase = document.clasificacion
    avisos, servicios = [], []

    for leido in datos['servicios']:
        cargos = sum(importe for _, importe in leido['cargos'])
        comun = {
            'importe': round(leido['importe'] + cargos, 2),
            'moneda': MONEDA,
            'bono': leido['bono'],
            'expediente': datos['expediente'],
        }
        if leido['cargos']:
            # Text, not a list: it is shown and corrected in a text box, and
            # what the reviewer reads is what gets stored.
            comun['cargos'] = '; '.join(
                f'{c}: {i:.2f} €'.replace('.', ',') for c, i in leido['cargos'])
        fragmento = '\n'.join([leido['linea'], *leido['detalle']])

        if leido['concepto'].upper().startswith('HTL'):
            if clase is not DocumentClassification.HOTEL:
                avisos.append(f"El talón incluye también «{leido['concepto']}», "
                              'que no es del mismo tipo; súbalo aparte.')
                continue
            servicios.append(_hotel(leido, comun, datos, pagina.numero, fragmento, avisos))
        elif _VUELO.match(leido['concepto']):
            if clase is not DocumentClassification.VUELO:
                avisos.append(f"El talón incluye también «{leido['concepto']}», "
                              'que no es del mismo tipo; súbalo aparte.')
                continue
            servicios.extend(_vuelos(leido, comun, datos, pagina.numero, fragmento, avisos))
        else:
            avisos.append(f"No se reconoce el concepto «{leido['concepto']}» del talón.")

    suma = round(sum(s['campos'].get('importe') or 0 for s in servicios), 2)
    if datos['total'] is not None and abs(suma - datos['total']) > 0.01 and servicios:
        avisos.append(
            f"Los servicios leídos suman {suma:.2f} € y el talón dice "
            f"{datos['total']:.2f} €; revise los importes."
        )

    confianzas = [v for s in servicios for v in s['confianzas'].values()]
    return {
        'servicios': servicios,
        'avisos': avisos,
        'confianza_global': min(confianzas) if confianzas else 0.0,
    }


def _con_procedencia(campos, pagina, fragmento, confianza=CONFIANZA_LEIDA, bajas=()):
    return {
        'campos': campos,
        'confianzas': {k: (CONFIANZA_SIN_HORA if k in bajas else confianza)
                       for k, v in campos.items() if v not in (None, '')},
        'procedencias': {k: {'pagina': pagina, 'fragmento': fragmento}
                         for k in campos},
        # How the approval treats it: completes an existing row when there is
        # one, and marks what it creates as provisional.
        'completa_existente': True,
    }


def _hotel(leido, comun, datos, pagina, fragmento, avisos):
    nombre = re.sub(r'^HTL\s*:\s*', '', leido['concepto'], flags=re.IGNORECASE).strip()
    ciudad, entrada, salida, habitacion, regimen = None, None, None, None, None
    for linea in leido['detalle']:
        estancia = _ESTANCIA.match(linea)
        if estancia:
            entrada = _anio_de(int(estancia['d1']), int(estancia['m1']), datos['emitido'])
            salida = _anio_de(int(estancia['d2']), int(estancia['m2']), datos['emitido'])
            if entrada and salida and salida < entrada:
                salida = salida.replace(year=salida.year + 1)
            habitacion = HABITACIONES.get(estancia['hab'] or '', estancia['hab'])
            regimen = REGIMENES.get(estancia['reg'] or '', estancia['reg'])
        elif ciudad is None and re.fullmatch(r"[A-ZÀ-Ý' .-]+", linea):
            ciudad = linea.title()

    if entrada is None:
        avisos.append(f'El talón no indica las fechas de «{nombre}».')

    campos = {
        'nombre': nombre.title(),
        'ciudad': ciudad,
        'huesped': datos['viajero'],
        'tipo_habitacion': habitacion,
        'regimen': regimen,
        'check_in': {'local': entrada.isoformat() if entrada else None,
                     'zona_horaria': None},
        'check_out': {'local': salida.isoformat() if salida else None,
                      'zona_horaria': None},
        **comun,
    }
    if entrada:
        avisos.append(f'«{nombre.title()}»: el talón no indica la hora de entrada ni '
                      'de salida; compruébelas si el hotel no tiene otra confirmación.')
    return _con_procedencia(campos, pagina, fragmento, bajas=('check_in', 'check_out'))


def _vuelos(leido, comun, datos, pagina, fragmento, avisos):
    """One service per leg of the route, the amount on the first.

    The stub prices the ticket, not each leg, and splitting it would be a
    guess; on the first leg the trip's total stays right.
    """
    vuelo = _VUELO.match(leido['concepto'])
    dia = _anio_de(int(vuelo['dia']), int(vuelo['mes']), datos['emitido'])
    ruta = next((linea for linea in leido['detalle']
                 if _RUTA.match(linea.replace(' ', ''))), None)
    if ruta is None:
        avisos.append(f"El billete {leido['bono']} no indica la ruta.")
        return []

    escalas = ruta.replace(' ', '').split('/')
    servicios = []
    for indice, (origen, destino) in enumerate(zip(escalas, escalas[1:], strict=False)):
        primero = indice == 0
        campos = {
            'aerolinea': vuelo['aerolinea'],
            'clase': vuelo['clase'],
            'origen_codigo': origen,
            'destino_codigo': destino,
            'pasajero': datos['viajero'],
            'numero_billete': leido['bono'],
            'expediente': comun['expediente'],
            'salida': {
                'local': f'{dia.isoformat()}T00:00' if (primero and dia) else None,
                'zona_horaria': None,
            },
        }
        if primero:
            # On a ticket the voucher column *is* the ticket number, already
            # recorded as such.
            campos.update({k: v for k, v in comun.items()
                           if k not in campos and k != 'bono'})
        servicio = _con_procedencia(campos, pagina, fragmento, bajas=('salida',))
        if not primero:
            # No day on the stub: it can complete the leg, never invent it.
            servicio['solo_si_existe'] = True
        servicios.append(servicio)

    if dia:
        avisos.append(f'Billete {leido["bono"]} ({ruta}): el talón solo indica el día '
                      'del primer vuelo y ninguna hora.')
    if len(servicios) > 1:
        avisos.append(f'Billete {leido["bono"]}: los vuelos después del primero no '
                      'tienen fecha en el talón; solo se completarán si ya están en el '
                      'itinerario.')
    return servicios
