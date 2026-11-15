"""The trips as a spreadsheet, for whoever keeps the accounts.

Two sheets, because there are two questions. «Gastos» is one row per itinerary
amount -- project, ledger account, date, figure, billable -- which is what
gets posted. «Viajes» is one row per trip, which is what somebody reads to
know what those rows belong to.

Three rules, each of which would otherwise fail without a sound:

- It starts from ``visible_trips_query`` like every other listing, so the
  file holds exactly what its author could have opened on screen.
- Amounts follow ``VER_COSTES``. With costs switched off the columns are not
  there at all, and the file says so: a column of blanks reads as «it cost
  nothing».
- Text that starts like a formula is written as text. A hotel named
  ``=HYPERLINK(...)`` comes out of a booking document and into a cell that a
  spreadsheet would otherwise execute on somebody's desk.
"""
import io
from datetime import datetime, time

from app.models.enums import AuditResourceType
from app.models.trip import Trip
from app.services import audit_service
from app.utils.timeutil import utcnow

MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

_FORMATO_FECHA = 'DD/MM/YYYY'
_FORMATO_IMPORTE = '#,##0.00'


def _seguro(valor):
    """A cell value that no spreadsheet will run."""
    if isinstance(valor, str) and valor[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + valor
    return valor


def _viajes(actor, desde=None, hasta=None):
    """Visible trips that start in the window, oldest first."""
    from app.services.authorization_service import visible_trips_query

    query = visible_trips_query(actor)
    if desde:
        query = query.filter(Trip.inicio_local >= datetime.combine(desde, time.min))
    if hasta:
        query = query.filter(Trip.inicio_local <= datetime.combine(hasta, time.max))
    return query.order_by(Trip.inicio_local.asc().nullslast(), Trip.referencia).all()


def _fecha_de(item, viaje):
    """The day an expense belongs to: when the service starts.

    The trip's own start when the row has none, so a per diem typed without a
    date still lands in the right month rather than in no month at all.
    """
    for campo in ('salida_local', 'check_in_local', 'recogida_local', 'inicio_local'):
        momento = getattr(item, campo, None)
        if momento is not None:
            return momento.date()
    return viaje.inicio_local.date() if viaje.inicio_local else None


def _viajero_de(item):
    viajero = getattr(item, 'traveler', None)
    if viajero is None or viajero.user is None:
        return 'Todo el viaje'
    return viajero.user.nombre_completo


def _personas(viaje):
    return ', '.join(v.nombre_completo for v in viaje.active_travelers)


def filas_de_gastos(viajes, con_importes):
    """One row per itinerary element, as plain values."""
    from app.services import cuenta_contable_service

    cuentas = cuenta_contable_service.por_tipo()
    filas = []
    for viaje in viajes:
        for coleccion in (viaje.segments, viaje.accommodations,
                          viaje.vehicle_rentals, viaje.other_services):
            for item in coleccion:
                if item.is_deleted:
                    continue
                # With amounts, only what has one: this sheet is what gets
                # posted. Without them it is a list of what was booked.
                if con_importes and item.importe is None:
                    continue
                tipo = cuenta_contable_service.tipo_de_gasto(item)
                cuenta = cuentas.get(tipo)
                fila = [
                    viaje.referencia, viaje.titulo, viaje.proyecto,
                    'Sí' if viaje.facturable else 'No',
                    cuenta.cuenta if cuenta else None,
                    cuenta.descripcion if cuenta else None,
                    tipo.label, _fecha_de(item, viaje), item.etiqueta,
                    item.proveedor, item.localizador, _viajero_de(item),
                ]
                if con_importes:
                    fila += [float(item.importe), item.moneda]
                fila += [viaje.estado.label,
                         viaje.gestor.nombre_completo if viaje.gestor else None]
                filas.append(fila)
    filas.sort(key=lambda f: (f[7] is None, f[7] or datetime.max.date(), f[0]))
    return filas


def _cabecera_de_gastos(con_importes):
    cabecera = ['Referencia', 'Viaje', 'Proyecto', 'Facturable', 'Cuenta contable',
                'Descripción de la cuenta', 'Tipo de gasto', 'Fecha', 'Concepto',
                'Proveedor', 'Localizador', 'Persona']
    if con_importes:
        cabecera += ['Importe', 'Moneda']
    return cabecera + ['Estado del viaje', 'Gestor']


def filas_de_viajes(viajes, con_importes):
    filas = []
    for viaje in viajes:
        fila = [
            viaje.referencia, viaje.titulo, viaje.estado.label, viaje.proyecto,
            'Sí' if viaje.facturable else 'No',
            viaje.inicio_local.date() if viaje.inicio_local else None,
            viaje.fin_local.date() if viaje.fin_local else None,
            viaje.resumen_ruta, _personas(viaje), len(viaje.active_travelers),
            viaje.gestor.nombre_completo if viaje.gestor else None,
            viaje.validacion_estado.label if viaje.validacion_estado else 'Sin pedir',
        ]
        if con_importes:
            monedas = viaje.monedas_del_itinerario
            fila += [
                viaje.coste_total,
                ', '.join(monedas) or None,
                float(viaje.coste_estimado) if viaje.coste_estimado is not None else None,
                viaje.moneda,
            ]
        fila.append(viaje.observaciones)
        filas.append(fila)
    return filas


def _cabecera_de_viajes(con_importes):
    cabecera = ['Referencia', 'Viaje', 'Estado', 'Proyecto', 'Facturable', 'Inicio',
                'Fin', 'Ruta', 'Personas viajeras', 'Nº de personas', 'Gestor',
                'Validación']
    if con_importes:
        cabecera += ['Coste según itinerario', 'Moneda del itinerario',
                     'Coste estimado', 'Moneda del estimado']
    return cabecera + ['Notas']


def _hoja(libro, titulo, cabecera, filas):
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    hoja = libro.create_sheet(titulo)
    hoja.append(cabecera)
    for celda in hoja[1]:
        celda.font = Font(bold=True)
    for fila in filas:
        hoja.append([_seguro(valor) for valor in fila])

    for indice, nombre in enumerate(cabecera, start=1):
        letra = get_column_letter(indice)
        formato = None
        if nombre in ('Fecha', 'Inicio', 'Fin'):
            formato = _FORMATO_FECHA
        elif nombre.startswith(('Importe', 'Coste')):
            formato = _FORMATO_IMPORTE
        if formato:
            for celda in hoja[letra][1:]:
                celda.number_format = formato
        ancho = max([len(nombre)] + [len(str(f[indice - 1] or '')) for f in filas[:200]])
        hoja.column_dimensions[letra].width = min(max(ancho + 2, 10), 50)

    hoja.freeze_panes = 'A2'
    if filas:
        hoja.auto_filter.ref = hoja.dimensions
    return hoja


def exportar(actor, desde=None, hasta=None):
    """Build the workbook. Returns ``(bytes, nombre_de_fichero, resumen)``."""
    from openpyxl import Workbook

    from app.services.authorization_service import Permiso, permisos_de

    con_importes = Permiso.VER_COSTES in permisos_de(actor)
    viajes = _viajes(actor, desde, hasta)
    gastos = filas_de_gastos(viajes, con_importes)

    libro = Workbook()
    libro.remove(libro.active)
    _hoja(libro, 'Gastos', _cabecera_de_gastos(con_importes), gastos)
    _hoja(libro, 'Viajes', _cabecera_de_viajes(con_importes),
          filas_de_viajes(viajes, con_importes))

    ahora = utcnow()
    notas = [
        ['Exportado', ahora.strftime('%d/%m/%Y %H:%M UTC')],
        ['Por', actor.nombre_completo],
        ['Viajes que empiezan desde', desde.strftime('%d/%m/%Y') if desde else 'Sin límite'],
        ['Viajes que empiezan hasta', hasta.strftime('%d/%m/%Y') if hasta else 'Sin límite'],
        ['Viajes', len(viajes)],
        ['Filas de gasto', len(gastos)],
        ['Importes',
         'Los que figuran en el itinerario de cada viaje. Un elemento sin importe '
         'no aparece en «Gastos»; el coste estimado del viaje está en «Viajes».'
         if con_importes else
         'No incluidos: los costes están desactivados en Ajustes.'],
        ['Cuenta contable',
         'La asignada al tipo de gasto en Administración → Cuentas contables. '
         'Vacía si ese tipo no tiene cuenta.'],
    ]
    _hoja(libro, 'Acerca de', ['Dato', 'Valor'], notas)

    salida = io.BytesIO()
    libro.save(salida)

    resumen = {'viajes': len(viajes), 'gastos': len(gastos),
               'con_importes': con_importes}
    audit_service.record(
        'trips.exported',
        recurso_tipo=AuditResourceType.VIAJE,
        actor=actor,
        metadatos={**resumen,
                   'desde': desde.isoformat() if desde else None,
                   'hasta': hasta.isoformat() if hasta else None},
    )
    return salida.getvalue(), f'viajes_{ahora:%Y%m%d_%H%M}.xlsx', resumen
