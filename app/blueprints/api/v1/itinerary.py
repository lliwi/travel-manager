"""Itinerary endpoints (specification section 7)."""
from flask import request
from flask_login import current_user, login_required

from app.blueprints.api.v1 import api_v1_bp, ok
from app.services import itinerary_service
from app.services.authorization_service import Permiso, can
from app.utils.decorators import require_trip_access
from app.utils.errors import ValidationError


@api_v1_bp.route('/trips/<trip_id>/itinerary', methods=['GET'])
@login_required
@require_trip_access(Permiso.VER_VIAJE)
def get_itinerary(trip_id, trip):
    """The consolidated timeline, scoped to what the caller may see."""
    actor = current_user._get_current_object()
    timeline = itinerary_service.build_timeline(actor, trip)
    return ok(timeline.to_dict(include_costs=bool(can(actor, Permiso.VER_COSTES, trip))))


@api_v1_bp.route('/trips/<trip_id>/itinerary/<kind>', methods=['POST'])
@login_required
@require_trip_access(Permiso.EDITAR_ITINERARIO)
def create_item(trip_id, kind, trip):
    """Create an itinerary item by hand."""
    payload = request.get_json(silent=True) or {}
    instants, campos = _split_payload(kind, payload)

    item = itinerary_service.create_item(
        current_user._get_current_object(), trip, kind,
        instants=instants, **campos,
    )
    return ok(item.to_dict(), status=201)


@api_v1_bp.route('/trips/<trip_id>/itinerary/<kind>/<item_id>', methods=['PATCH'])
@login_required
@require_trip_access(Permiso.EDITAR_ITINERARIO)
def update_item(trip_id, kind, item_id, trip):
    """Edit an itinerary item."""
    payload = request.get_json(silent=True) or {}
    instants, campos = _split_payload(kind, payload)

    item = itinerary_service.update_item(
        current_user._get_current_object(), trip, kind, item_id,
        instants=instants, **campos,
    )
    return ok(item.to_dict())


@api_v1_bp.route('/trips/<trip_id>/itinerary/<kind>/<item_id>', methods=['DELETE'])
@login_required
@require_trip_access(Permiso.EDITAR_ITINERARIO)
def delete_item(trip_id, kind, item_id, trip):
    """Soft-delete an itinerary item."""
    itinerary_service.delete_item(
        current_user._get_current_object(), trip, kind, item_id
    )
    return ok({'mensaje': 'Elemento eliminado.'})


def _split_payload(kind, payload):
    """Separate timestamp triples from plain column values.

    A timestamp arrives as ``{"salida": {"local": "...", "tz": "Europe/Madrid"}}``
    so the caller can never set a UTC column directly and let the triple drift.
    """
    from datetime import datetime

    _, prefixes = itinerary_service.ENTITY_MAP.get(str(kind), (None, ()))
    if not prefixes:
        raise ValidationError(f'Tipo de elemento de itinerario desconocido: {kind}.')

    instants = {}
    campos = {}

    for key, value in payload.items():
        if key in prefixes and isinstance(value, dict):
            raw = value.get('local')
            local_dt = None
            if raw:
                try:
                    parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
                except ValueError as exc:
                    raise ValidationError(
                        f'El campo «{key}.local» no es una fecha válida ISO-8601.'
                    ) from exc
                local_dt = parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
            instants[key] = (local_dt, value.get('tz'))
        elif key.endswith('_utc'):
            # Derived columns are never accepted from a client.
            raise ValidationError(
                f'El campo «{key}» es derivado y no puede establecerse directamente. '
                f'Envíe el instante local y su zona horaria.'
            )
        else:
            campos[key] = value

    return instants, campos
