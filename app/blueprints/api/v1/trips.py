"""Trip endpoints (specification section 7)."""
import uuid

from flask import current_app, request
from flask_login import current_user, login_required

from app.blueprints.api.v1 import api_v1_bp, ok, paginated
from app.extensions import db
from app.models.enums import TripPurpose, TripStatus
from app.models.user import User
from app.services import trip_service
from app.services.authorization_service import Permiso, can
from app.utils.decorators import require_permiso, require_trip_access, scoped_by_query
from app.utils.errors import ValidationError


def _parse_datetime(raw, field):
    """Parse an ISO-8601 datetime from the request body."""
    if raw in (None, ''):
        return None
    from datetime import datetime

    try:
        value = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValidationError(
            f'El campo «{field}» no es una fecha válida ISO-8601.'
        ) from exc
    # The service expects a naive local wall-clock value plus a timezone name.
    return value.replace(tzinfo=None) if value.tzinfo else value


def _include_costs(actor, trip):
    """Costs appear only for actors holding VER_COSTES."""
    return bool(can(actor, Permiso.VER_COSTES, trip))


@api_v1_bp.route('/trips', methods=['GET'])
@login_required
@scoped_by_query
def list_trips():
    """List the trips the caller may see."""
    actor = current_user._get_current_object()
    per_page = min(
        request.args.get('per_page', current_app.config['ITEMS_PER_PAGE'], type=int),
        current_app.config['API_MAX_PAGE_SIZE'],
    )

    pagination = trip_service.list_trips(
        actor,
        page=request.args.get('page', 1, type=int),
        per_page=per_page,
        estado=request.args.getlist('estado') or None,
        buscar=request.args.get('buscar'),
        gestor_id=request.args.get('gestor_id'),
        orden=request.args.get('orden', 'inicio_desc'),
    )
    return paginated(
        pagination,
        lambda trip: trip.to_dict(include_costs=_include_costs(actor, trip)),
    )


@api_v1_bp.route('/trips', methods=['POST'])
@login_required
@require_permiso(Permiso.CREAR_VIAJE)
def create_trip():
    """Create a trip."""
    payload = request.get_json(silent=True) or {}
    actor = current_user._get_current_object()

    gestor = None
    if payload.get('gestor_id'):
        try:
            gestor = db.session.get(User, uuid.UUID(str(payload['gestor_id'])))
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                'El identificador del gestor no es válido.'
            ) from exc

    trip = trip_service.create_trip(
        actor,
        titulo=payload.get('titulo'),
        gestor=gestor,
        estado=TripStatus.coerce(payload.get('estado'), TripStatus.BORRADOR),
        finalidad=TripPurpose.coerce(payload.get('finalidad')),
        finalidad_detalle=payload.get('finalidad_detalle'),
        observaciones=payload.get('observaciones'),
        inicio_local=_parse_datetime(payload.get('inicio_local'), 'inicio_local'),
        inicio_tz=payload.get('inicio_tz'),
        fin_local=_parse_datetime(payload.get('fin_local'), 'fin_local'),
        fin_tz=payload.get('fin_tz'),
    )
    return ok(trip.to_dict(include_costs=_include_costs(actor, trip)), status=201)


@api_v1_bp.route('/trips/<trip_id>', methods=['GET'])
@login_required
@require_trip_access(Permiso.VER_VIAJE)
def get_trip(trip_id, trip):
    """One trip."""
    actor = current_user._get_current_object()
    return ok(trip.to_dict(include_costs=_include_costs(actor, trip)))


@api_v1_bp.route('/trips/<trip_id>', methods=['PATCH'])
@login_required
@require_trip_access(Permiso.EDITAR_VIAJE)
def update_trip(trip_id, trip):
    """Update a trip."""
    payload = request.get_json(silent=True) or {}
    actor = current_user._get_current_object()

    campos = {}
    for field in ('titulo', 'finalidad_detalle', 'observaciones', 'moneda'):
        if field in payload:
            campos[field] = payload[field]
    if 'estado' in payload:
        campos['estado'] = TripStatus.coerce(payload['estado'], trip.estado)
    if 'finalidad' in payload:
        campos['finalidad'] = TripPurpose.coerce(payload['finalidad'])
    if 'gestor_id' in payload and payload['gestor_id']:
        campos['gestor_id'] = uuid.UUID(str(payload['gestor_id']))
    for prefix in ('inicio', 'fin'):
        if f'{prefix}_local' in payload:
            campos[f'{prefix}_local'] = _parse_datetime(
                payload[f'{prefix}_local'], f'{prefix}_local'
            )
            campos[f'{prefix}_tz'] = payload.get(f'{prefix}_tz')

    trip_service.update_trip(actor, trip, **campos)
    return ok(trip.to_dict(include_costs=_include_costs(actor, trip)))


@api_v1_bp.route('/trips/<trip_id>', methods=['DELETE'])
@login_required
@require_trip_access(Permiso.ELIMINAR_VIAJE)
def delete_trip(trip_id, trip):
    """Soft-delete a trip."""
    trip_service.delete_trip(current_user._get_current_object(), trip)
    return ok({'mensaje': 'Viaje eliminado.', 'id': str(trip.id)})


# ======================================================================
# Travellers
# ======================================================================
@api_v1_bp.route('/trips/<trip_id>/travelers', methods=['GET'])
@login_required
@require_trip_access(Permiso.VER_VIAJE)
def list_travelers(trip_id, trip):
    """The trip's travellers."""
    from app.services.authorization_service import visible_travelers

    actor = current_user._get_current_object()
    return ok([t.to_dict() for t in visible_travelers(actor, trip)])


@api_v1_bp.route('/trips/<trip_id>/travelers', methods=['POST'])
@login_required
@require_trip_access(Permiso.GESTIONAR_VIAJEROS)
def add_traveler(trip_id, trip):
    """Assign a person to the trip."""
    from app.models.enums import TravelerRole

    payload = request.get_json(silent=True) or {}
    if not payload.get('user_id'):
        raise ValidationError('Se requiere el identificador de la persona viajera.')

    traveler = trip_service.add_traveler(
        current_user._get_current_object(),
        trip,
        payload['user_id'],
        rol_en_viaje=TravelerRole.coerce(
            payload.get('rol_en_viaje'), TravelerRole.VIAJERO
        ),
        desde_local=_parse_datetime(payload.get('desde_local'), 'desde_local'),
        hasta_local=_parse_datetime(payload.get('hasta_local'), 'hasta_local'),
        tz=payload.get('tz'),
        observaciones=payload.get('observaciones'),
    )
    return ok(traveler.to_dict(), status=201)


@api_v1_bp.route('/trips/<trip_id>/travelers/<user_id>', methods=['DELETE'])
@login_required
@require_trip_access(Permiso.GESTIONAR_VIAJEROS)
def remove_traveler(trip_id, user_id, trip):
    """Unassign a person from the trip."""
    trip_service.remove_traveler(current_user._get_current_object(), trip, user_id)
    return ok({'mensaje': 'Persona viajera desasignada.'})


# ======================================================================
# Destinations
# ======================================================================
@api_v1_bp.route('/trips/<trip_id>/destinations', methods=['GET'])
@login_required
@require_trip_access(Permiso.VER_VIAJE)
def list_destinations(trip_id, trip):
    """The trip's destinations, in itinerary order."""
    return ok([d.to_dict() for d in trip.destinations])


@api_v1_bp.route('/trips/<trip_id>/destinations', methods=['POST'])
@login_required
@require_trip_access(Permiso.EDITAR_VIAJE)
def add_destination(trip_id, trip):
    """Append a destination."""
    from app.models.catalog import Country

    payload = request.get_json(silent=True) or {}
    country = None
    if payload.get('pais_codigo'):
        country = Country.query.filter_by(
            codigo=str(payload['pais_codigo']).upper()
        ).first()

    destination = trip_service.add_destination(
        current_user._get_current_object(),
        trip,
        ciudad=payload.get('ciudad'),
        pais_codigo=payload.get('pais_codigo'),
        pais_nombre=country.nombre if country else payload.get('pais_nombre'),
        zona_horaria=(
            payload.get('zona_horaria')
            or (country.zona_horaria_principal if country else None)
        ),
        inicio_local=_parse_datetime(payload.get('inicio_local'), 'inicio_local'),
        fin_local=_parse_datetime(payload.get('fin_local'), 'fin_local'),
        es_escala=bool(payload.get('es_escala')),
        orden=payload.get('orden'),
        observaciones=payload.get('observaciones'),
    )
    return ok(destination.to_dict(), status=201)


@api_v1_bp.route('/trips/<trip_id>/destinations/<destination_id>', methods=['DELETE'])
@login_required
@require_trip_access(Permiso.EDITAR_VIAJE)
def remove_destination(trip_id, destination_id, trip):
    """Remove a destination."""
    trip_service.remove_destination(
        current_user._get_current_object(), trip, destination_id
    )
    return ok({'mensaje': 'Destino eliminado.'})
