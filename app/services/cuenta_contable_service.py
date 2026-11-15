"""The chart of accounts for travel expenses.

An administrator says which ledger account each kind of expense -- a flight,
a hotel, per diems -- is posted to, and the reports group what was spent by
that account. Two rules keep the result something accounting can use:

- The kind of an expense is read off the itinerary row, never typed again, so
  the same flight cannot be a flight in the itinerary and something else in
  the ledger.
- A kind nobody has given an account is reported as such. Falling back to a
  catch-all account would post money somewhere nobody decided.
"""
from app.extensions import db
from app.models.enums import (
    AuditResourceType,
    SegmentType,
    ServiceType,
    TipoGasto,
)
from app.models.itinerary import Accommodation, TravelSegment, VehicleRental
from app.models.settings import CuentaContable
from app.services import audit_service
from app.utils.errors import ValidationError

_POR_TRAMO = {
    SegmentType.VUELO: TipoGasto.VUELO,
    SegmentType.TREN: TipoGasto.TREN,
    SegmentType.AUTOBUS: TipoGasto.AUTOBUS,
    SegmentType.FERRY: TipoGasto.FERRY,
    SegmentType.TRASLADO: TipoGasto.TRASLADO,
    SegmentType.OTRO: TipoGasto.OTRO_TRANSPORTE,
}

_POR_SERVICIO = {
    ServiceType.DIETAS: TipoGasto.DIETAS,
    ServiceType.RESTAURANTE: TipoGasto.RESTAURANTE,
    ServiceType.SEGURO: TipoGasto.SEGURO,
    ServiceType.VISADO: TipoGasto.VISADO,
    ServiceType.PARKING: TipoGasto.PARKING,
    ServiceType.ENTRADA: TipoGasto.ENTRADA,
    ServiceType.CONECTIVIDAD: TipoGasto.CONECTIVIDAD,
    ServiceType.OTRO: TipoGasto.OTRO,
}


def tipo_de_gasto(item):
    """The kind of expense an itinerary row is."""
    if isinstance(item, TravelSegment):
        return _POR_TRAMO.get(item.tipo, TipoGasto.OTRO_TRANSPORTE)
    if isinstance(item, Accommodation):
        return TipoGasto.ALOJAMIENTO
    if isinstance(item, VehicleRental):
        return TipoGasto.VEHICULO
    return _POR_SERVICIO.get(getattr(item, 'tipo', None), TipoGasto.OTRO)


def por_tipo():
    """``{TipoGasto: CuentaContable}`` for the kinds that have an account."""
    return {fila.tipo_gasto: fila for fila in CuentaContable.query.all()}


def tabla():
    """Every kind of expense with its account, or None: what the screen edits."""
    asignadas = por_tipo()
    return [(tipo, asignadas.get(tipo)) for tipo in TipoGasto]


def guardar(actor, valores):
    """Replace the chart with ``{tipo: (cuenta, descripcion)}``.

    A kind left without an account loses its row; a kind missing from
    ``valores`` is left as it was, so a partial form cannot empty the table.
    Nothing is written if any value is refused.
    """
    cambios = []
    for crudo, (cuenta, descripcion) in valores.items():
        tipo = TipoGasto.coerce(crudo)
        if tipo is None:
            raise ValidationError(f'«{crudo}» no es un tipo de gasto.')

        cuenta = str(cuenta or '').strip()
        descripcion = str(descripcion or '').strip() or None
        if len(cuenta) > 40:
            raise ValidationError(
                f'La cuenta de «{tipo.label}» no puede pasar de 40 caracteres.'
            )
        if descripcion and len(descripcion) > 200:
            raise ValidationError(
                f'La descripción de «{tipo.label}» no puede pasar de 200 caracteres.'
            )
        if descripcion and not cuenta:
            raise ValidationError(
                f'«{tipo.label}» tiene descripción pero no cuenta.'
            )
        cambios.append((tipo, cuenta, descripcion))

    actuales = por_tipo()
    tocados = []
    for tipo, cuenta, descripcion in cambios:
        fila = actuales.get(tipo)
        if not cuenta:
            if fila is not None:
                db.session.delete(fila)
                tocados.append(str(tipo))
            continue
        if fila is None:
            fila = CuentaContable(tipo_gasto=tipo)
            db.session.add(fila)
        if (fila.cuenta, fila.descripcion) != (cuenta, descripcion):
            fila.cuenta, fila.descripcion = cuenta, descripcion
            fila.actualizada_por_id = getattr(actor, 'id', None)
            tocados.append(str(tipo))

    db.session.commit()
    if tocados:
        audit_service.record(
            'accounts.updated',
            recurso_tipo=AuditResourceType.CONFIGURACION,
            actor=actor,
            metadatos={'tipos': sorted(tocados)},
        )
    return tocados
