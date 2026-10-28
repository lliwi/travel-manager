"""The single source of truth for authorisation.

Specification sections 2.1, 3.2 and 5.1/5.4. Every access question in the
application -- from a Jinja view, from ``/api/v1``, from a Celery task -- is
answered by :func:`can`. The web and API surfaces differ only in how the
resulting exception is rendered; the decision itself exists exactly once.

Two consequences are worth stating explicitly:

* A traveller sees only the itinerary rows that concern them. That is enforced
  by :func:`scope_itinerary_query`, used by the timeline, the alert list and the
  AI context builder -- so section 5.4's "no se incluye información de otros
  viajeros" holds by construction, not because a prompt asked the model nicely.
* Probing for a resource you have no relationship with yields 404, not 403, so
  the response cannot be used to discover which trips exist. Section 5.1 speaks
  of 403 for an unassigned user; denying with 404 satisfies that requirement and
  leaks strictly less.
"""
import enum
import functools
import logging
import uuid

from flask import g, has_request_context

from app.extensions import db
from app.models.enums import AuditResourceType, RoleCode
from app.utils.errors import (
    AuthenticationRequired,
    AuthorizationError,
    ResourceNotFound,
)

logger = logging.getLogger(__name__)


class Permiso(str, enum.Enum):
    """Every distinct thing an actor can be allowed to do."""

    VER_VIAJE = 'ver_viaje'
    CREAR_VIAJE = 'crear_viaje'
    EDITAR_VIAJE = 'editar_viaje'
    ELIMINAR_VIAJE = 'eliminar_viaje'
    GESTIONAR_VIAJEROS = 'gestionar_viajeros'

    SUBIR_DOCUMENTO = 'subir_documento'
    VER_DOCUMENTO = 'ver_documento'
    DESCARGAR_DOCUMENTO = 'descargar_documento'
    ELIMINAR_DOCUMENTO = 'eliminar_documento'
    REVISAR_EXTRACCION = 'revisar_extraccion'

    EDITAR_ITINERARIO = 'editar_itinerario'
    VER_ALERTA = 'ver_alerta'
    GESTIONAR_ALERTA = 'gestionar_alerta'
    RECALCULAR_ALERTAS = 'recalcular_alertas'

    CONSULTAR_IA = 'consultar_ia'
    INVESTIGAR_WEB = 'investigar_web'
    VER_RECOMENDACION = 'ver_recomendacion'
    GENERAR_RECOMENDACION = 'generar_recomendacion'
    VALIDAR_RECOMENDACION = 'validar_recomendacion'

    VER_COSTES = 'ver_costes'
    ADMINISTRAR = 'administrar'
    VER_AUDITORIA = 'ver_auditoria'

    def __str__(self):
        return self.value


#: Permissions that are not about a particular trip.
PERMISOS_GLOBALES = frozenset({
    Permiso.CREAR_VIAJE,
    Permiso.ADMINISTRAR,
    Permiso.VER_AUDITORIA,
    Permiso.INVESTIGAR_WEB,
})

#: Specification section 2.1, the three access profiles.
PERMISOS_ADMIN = frozenset(Permiso)

PERMISOS_GESTOR = PERMISOS_ADMIN - {Permiso.ADMINISTRAR, Permiso.VER_AUDITORIA}

#: A traveller consults only: their trips, their processed documents, their
#: itinerary, alerts and advisories. Nothing that mutates, nothing about costs.
PERMISOS_USUARIO = frozenset({
    Permiso.VER_VIAJE,
    Permiso.VER_DOCUMENTO,
    Permiso.DESCARGAR_DOCUMENTO,
    Permiso.VER_ALERTA,
    Permiso.VER_RECOMENDACION,
    Permiso.CONSULTAR_IA,
})

#: What a traveller may plan on their own trips when the organisation allows
#: it: choosing flights and lodging, by hand or with the assistant. Everything
#: after that -- documents, advisories, confirming what the extraction read --
#: stays with the manager.
TIPOS_PLANIFICABLES = ('segmento', 'alojamiento')

#: Planning stops when the manager confirms the trip: from then on the
#: itinerary is what was booked, not a proposal.
ESTADOS_PLANIFICABLES = ('borrador', 'en_preparacion')


class AccessDecision:
    """The outcome of an authorisation question.

    Carries ``motivo`` so denials can be audited and debugged precisely without
    that reason ever reaching the end user.
    """

    __slots__ = ('permitido', 'motivo', 'permisos', 'trip_id', 'no_existe')

    def __init__(self, permitido, motivo, permisos=frozenset(), trip_id=None,
                 no_existe=False):
        self.permitido = permitido
        self.motivo = motivo
        self.permisos = permisos
        self.trip_id = trip_id
        #: True when the denial should be rendered as 404 rather than 403.
        self.no_existe = no_existe

    def __bool__(self):
        return self.permitido

    def __repr__(self):
        return f'<AccessDecision {"permitido" if self.permitido else "denegado"} {self.motivo}>'


# ======================================================================
# Resolving a resource to its trip
# ======================================================================
@functools.singledispatch
def _trip_of(recurso):
    """Resolve any resource to the trip that governs access to it.

    Registering a new entity type costs one line here and no branch in
    :func:`can`.
    """
    return None


def _register_trip_resolvers():
    """Attach a resolver per entity type.

    Done inside a function so importing this module does not require the model
    package, which keeps the import graph acyclic.
    """
    from app.models.advisory import SecurityAdvisory
    from app.models.alert import Alert
    from app.models.document import Document
    from app.models.extraction import Extraction
    from app.models.itinerary import (
        Accommodation,
        OtherService,
        TravelSegment,
        VehicleRental,
    )
    from app.models.trip import Trip, TripDestination, TripTraveler

    @_trip_of.register(Trip)
    def _(recurso):
        return recurso

    for model in (
        TripTraveler, TripDestination, Document, Alert, SecurityAdvisory,
        TravelSegment, Accommodation, VehicleRental, OtherService,
    ):
        @_trip_of.register(model)
        def _(recurso):
            return recurso.trip

    @_trip_of.register(Extraction)
    def _(recurso):
        return recurso.document.trip if recurso.document else None

    @_trip_of.register(uuid.UUID)
    def _(recurso):
        return db.session.get(Trip, recurso)

    @_trip_of.register(str)
    def _(recurso):
        try:
            return db.session.get(Trip, uuid.UUID(recurso))
        except (ValueError, TypeError):
            return None


_RESOLVERS_READY = False


def _ensure_resolvers():
    global _RESOLVERS_READY
    if not _RESOLVERS_READY:
        _register_trip_resolvers()
        _RESOLVERS_READY = True


# ======================================================================
# The decision
# ======================================================================
def can(actor, permiso, recurso=None):
    """Answer one authorisation question.

    Args:
        actor: The acting :class:`~app.models.user.User`, or None.
        permiso: A :class:`Permiso` member.
        recurso: The resource in question -- a model instance, a trip UUID, or
            None for a global permission.

    Returns:
        An :class:`AccessDecision`.
    """
    _ensure_resolvers()

    if actor is None or getattr(actor, 'is_anonymous', False):
        return AccessDecision(False, 'no_autenticado')

    # Checked before is_authenticated: Flask-Login's UserMixin derives
    # is_authenticated from is_active, so a disabled account would otherwise be
    # reported as anonymous and the audit trail would lose the distinction
    # between "nobody asked" and "a disabled account asked".
    if getattr(actor, 'is_deleted', False) or not getattr(actor, 'is_active_account', False):
        return AccessDecision(False, 'cuenta_inactiva')

    permiso = Permiso(permiso) if not isinstance(permiso, Permiso) else permiso

    # --- Administrator: everything ------------------------------------
    if actor.has_role(RoleCode.ADMINISTRADOR):
        return AccessDecision(True, 'admin', PERMISOS_ADMIN, _trip_id_of(recurso))

    trip = _trip_of(recurso) if recurso is not None else None

    # --- No resource: a global permission question ---------------------
    if recurso is None:
        permisos = (PERMISOS_GESTOR if actor.has_role(RoleCode.GESTOR)
                    else _traveler_global_permissions())
        allowed = permiso in (permisos & PERMISOS_GLOBALES)
        return AccessDecision(
            allowed, 'rol_global' if allowed else 'permiso_global_denegado', permisos
        )

    # --- A resource was named but could not be resolved ----------------
    if trip is None:
        return AccessDecision(False, 'recurso_inexistente', no_existe=True)

    if getattr(trip, 'is_deleted', False) and permiso is not Permiso.VER_VIAJE:
        return AccessDecision(False, 'viaje_eliminado', trip_id=trip.id)

    # --- Manager: administers every trip (section 2.1) -----------------
    if actor.has_role(RoleCode.GESTOR):
        allowed = permiso in PERMISOS_GESTOR
        if allowed and not _trip_mutable(trip, permiso):
            return AccessDecision(False, 'viaje_no_editable', PERMISOS_GESTOR, trip.id)
        return AccessDecision(
            allowed, 'gestor' if allowed else 'permiso_gestor_denegado',
            PERMISOS_GESTOR, trip.id,
        )

    # --- Traveller: only their own trips -------------------------------
    if _is_traveler(actor, trip):
        permisos = _traveler_permissions(trip)
        allowed = permiso in permisos
        if allowed and permiso is Permiso.EDITAR_ITINERARIO:
            motivo = _fuera_de_lo_planificable(actor, trip, recurso)
            if motivo:
                return AccessDecision(False, motivo, permisos, trip.id)
        return AccessDecision(
            allowed, 'viajero_asignado' if allowed else 'permiso_viajero_denegado',
            permisos, trip.id,
        )

    # Not related to this trip at all. Deny as "does not exist".
    return AccessDecision(False, 'no_asignado', trip_id=trip.id, no_existe=True)


def permisos_de(actor):
    """Every permission this actor holds, before any particular resource.

    Distinct from :func:`can` with no resource on purpose. That one answers a
    *global* question -- «may this person do X anywhere» -- and only the four
    permissions in :data:`PERMISOS_GLOBALES` are global questions at all, so
    asking it about a per-resource permission is always denied. The reports
    page learnt that the hard way: guarded with ``can(actor, VER_VIAJE, None)``
    it answered 403 to everybody but an administrator.

    Use this to decide whether to *offer* something -- a column of costs, a
    panel, a menu entry. Use :func:`can` to decide whether to allow it on a
    given trip.
    """
    if actor is None or getattr(actor, 'is_anonymous', False):
        return frozenset()
    if getattr(actor, 'is_deleted', False) or not getattr(actor, 'is_active_account', False):
        return frozenset()
    if actor.has_role(RoleCode.ADMINISTRADOR):
        return PERMISOS_ADMIN
    if actor.has_role(RoleCode.GESTOR):
        return _narrow_by_settings(PERMISOS_GESTOR)
    return _traveler_permissions() | _traveler_global_permissions()


def _narrow_by_settings(permisos):
    """Apply the runtime switches that take a permission away from everybody."""
    from app.services import settings_service

    permisos = set(permisos)
    if not settings_service.get_bool('COSTES_HABILITADOS', False):
        permisos.discard(Permiso.VER_COSTES)
    return frozenset(permisos)


def _traveler_permissions(trip=None):
    """Traveller permissions, narrowed by the current runtime settings.

    With planning allowed, a traveller also edits the itinerary of a trip that
    is still a proposal. ``trip`` is what says whether it still is; without one
    the answer is about no trip in particular, so planning is not included.
    """
    from app.services import settings_service

    permisos = set(PERMISOS_USUARIO)
    if not settings_service.get_bool('VIAJERO_DESCARGA_ORIGINALES', True):
        permisos.discard(Permiso.DESCARGAR_DOCUMENTO)
    if not settings_service.get_bool('COSTES_HABILITADOS', False):
        permisos.discard(Permiso.VER_COSTES)
    if trip is not None and usuarios_planifican():
        if str(getattr(trip, 'estado', '') or '') in ESTADOS_PLANIFICABLES:
            permisos.add(Permiso.EDITAR_ITINERARIO)
        # Documents are not limited to planning: the boarding pass arrives on
        # the day and the receipts after it. Uploading makes nothing real --
        # the manager still reviews what is read -- so only a cancelled trip,
        # which accepts nothing from anybody, refuses one.
        if getattr(trip, 'is_editable', True):
            permisos.add(Permiso.SUBIR_DOCUMENTO)
    return frozenset(permisos)


def _traveler_global_permissions():
    """A traveller's global permissions: creating a trip, when planning is on.

    Only with a manager to hand it to -- the default one, or one the traveller
    picks. A trip nobody manages would sit in draft with its documents,
    advisories and confirmation waiting for a person who was never told.
    """
    if usuarios_planifican() and gestores_disponibles():
        return PERMISOS_USUARIO | {Permiso.CREAR_VIAJE}
    return PERMISOS_USUARIO


def usuarios_planifican():
    """Whether travellers may plan their own trips (Ajustes → Funcionalidad)."""
    from app.services import settings_service

    return settings_service.get_bool('USUARIOS_PLANIFICAN', False)


def gestor_por_defecto():
    """The manager who receives the trips travellers create, or None.

    The setting names an account by user name or email. One that does not
    exist, is disabled or has no manager role counts as unset: handing trips
    to an account that cannot act on them is how they would be lost.
    """
    from app.models.user import User
    from app.services import settings_service

    nombre = (settings_service.get('GESTOR_POR_DEFECTO') or '').strip()
    if not nombre:
        return None
    usuario = User.query.filter(
        db.or_(User.username == nombre, User.email == nombre.lower()),
        User.is_deleted.is_(False),
    ).first()
    if usuario is None or not getattr(usuario, 'is_active_account', False):
        return None
    if not (usuario.has_role(RoleCode.GESTOR) or usuario.has_role(RoleCode.ADMINISTRADOR)):
        return None
    return usuario


def gestores_disponibles():
    """Active managers, by name: who a traveller may hand a trip to."""
    from app.models.user import Role, User

    return (
        User.query.filter(User.is_deleted.is_(False))
        .join(User.roles).filter(Role.codigo == RoleCode.GESTOR)
        .order_by(User.nombre, User.apellidos).all()
    )


def gestor_para_viaje_propio(elegido=None):
    """The manager a traveller's new trip goes to.

    The default from Ajustes wins, so an organisation that names one gets a
    single inbox. Without it the traveller's choice counts, provided it is an
    active manager; with no choice and a single manager, that one. Anything
    else is refused rather than guessed: a trip sent to the wrong manager is
    a trip nobody looks at.
    """
    from app.utils.errors import ValidationError

    por_defecto = gestor_por_defecto()
    if por_defecto is not None:
        return por_defecto

    disponibles = [g for g in gestores_disponibles()
                   if getattr(g, 'is_active_account', False)]
    if elegido is not None:
        if any(str(g.id) == str(getattr(elegido, 'id', elegido)) for g in disponibles):
            return next(g for g in disponibles
                        if str(g.id) == str(getattr(elegido, 'id', elegido)))
        raise ValidationError('El gestor elegido no está disponible.')
    if len(disponibles) == 1:
        return disponibles[0]
    if not disponibles:
        raise ValidationError(
            'No hay ningún gestor activo que pueda llevar el viaje. Pídaselo al '
            'administrador.'
        )
    raise ValidationError('Elija qué gestor llevará el viaje.')


def gestiona(actor):
    """Whether the actor runs trips rather than travels on them."""
    return actor is not None and (
        actor.has_role(RoleCode.GESTOR) or actor.has_role(RoleCode.ADMINISTRADOR)
    )


def tipos_que_puede_crear(actor, trip):
    """The itinerary kinds this actor may add to ``trip`` by hand."""
    from app.services.itinerary_service import ENTITY_MAP

    if not can(actor, Permiso.EDITAR_ITINERARIO, trip):
        return ()
    if gestiona(actor):
        return tuple(ENTITY_MAP)
    return TIPOS_PLANIFICABLES


def _fuera_de_lo_planificable(actor, trip, recurso):
    """Why a traveller may not touch this itinerary row, or None.

    What they planned is theirs; what came from a document is the manager's,
    because it records what was booked, and so is a row that is somebody
    else's on a trip shared by several people.
    """
    from app.models.itinerary import (
        Accommodation,
        OtherService,
        TravelSegment,
        VehicleRental,
    )

    if not isinstance(recurso, (TravelSegment, Accommodation, VehicleRental, OtherService)):
        return None
    if not isinstance(recurso, (TravelSegment, Accommodation)):
        return 'tipo_no_planificable'
    if recurso.documento_origen_id is not None:
        return 'elemento_de_documento'

    propio = trip.traveler_for(actor.id)
    if recurso.trip_traveler_id is None:
        # For the whole trip: theirs only when the whole trip is them.
        if len(trip.active_travelers) != 1:
            return 'elemento_de_otros'
    elif propio is None or recurso.trip_traveler_id != propio.id:
        return 'elemento_de_otros'
    return None


def _trip_mutable(trip, permiso):
    """Cancelled trips accept no mutation, whoever is asking."""
    mutating = {
        Permiso.EDITAR_VIAJE, Permiso.GESTIONAR_VIAJEROS, Permiso.SUBIR_DOCUMENTO,
        Permiso.EDITAR_ITINERARIO, Permiso.REVISAR_EXTRACCION,
    }
    if permiso not in mutating:
        return True
    return getattr(trip, 'is_editable', True)


def _is_traveler(actor, trip):
    """True when the actor is assigned to the trip.

    Memoised per request: rendering a list of thirty trips would otherwise run
    thirty membership queries.
    """
    if trip is None:
        return False

    cache = None
    if has_request_context():
        cache = getattr(g, '_trip_membership', None)
        if cache is None:
            cache = {}
            g._trip_membership = cache
        key = (str(actor.id), str(trip.id))
        if key in cache:
            return cache[key]

    result = trip.has_traveler(actor.id)
    if cache is not None:
        cache[key] = result
    return result


def _trip_id_of(recurso):
    """Best-effort trip id for a decision record."""
    if recurso is None:
        return None
    _ensure_resolvers()
    trip = _trip_of(recurso)
    return getattr(trip, 'id', None)


# ======================================================================
# Enforcement
# ======================================================================
def require(actor, permiso, recurso=None, recurso_tipo=None, recurso_id=None):
    """Raise unless the actor holds the permission.

    Raises:
        AuthenticationRequired: Nobody is logged in.
        ResourceNotFound: The resource does not exist, or the actor has no
            relationship with it at all.
        AuthorizationError: The actor is related to the resource but lacks this
            particular permission.
    """
    decision = can(actor, permiso, recurso)
    if decision.permitido:
        return decision

    if decision.motivo == 'no_autenticado':
        raise AuthenticationRequired()

    _audit_denial(actor, permiso, decision, recurso, recurso_tipo, recurso_id)

    if decision.no_existe:
        raise ResourceNotFound()
    raise AuthorizationError()


def _audit_denial(actor, permiso, decision, recurso, recurso_tipo, recurso_id):
    """Leave a trace of every refusal, so probing is visible after the fact."""
    from app.services import audit_service

    try:
        audit_service.record_denied(
            f'authz.denied.{permiso}',
            recurso_tipo=recurso_tipo or _infer_resource_type(recurso),
            recurso_id=recurso_id or _infer_resource_id(recurso),
            actor=actor,
            motivo=decision.motivo,
        )
    except Exception:
        logger.exception('No se pudo auditar la denegación de acceso.')


_RESOURCE_TYPE_BY_CLASS = {
    'Trip': AuditResourceType.VIAJE,
    'TripTraveler': AuditResourceType.VIAJERO,
    'Document': AuditResourceType.DOCUMENTO,
    'Extraction': AuditResourceType.EXTRACCION,
    'Alert': AuditResourceType.ALERTA,
    'SecurityAdvisory': AuditResourceType.RECOMENDACION,
    'TravelSegment': AuditResourceType.SEGMENTO,
    'Accommodation': AuditResourceType.ALOJAMIENTO,
    'VehicleRental': AuditResourceType.VEHICULO,
    'OtherService': AuditResourceType.SERVICIO,
}


def _infer_resource_type(recurso):
    return _RESOURCE_TYPE_BY_CLASS.get(type(recurso).__name__)


def _infer_resource_id(recurso):
    return str(getattr(recurso, 'id', recurso)) if recurso is not None else None


# ======================================================================
# Loaders used by the route decorators
# ======================================================================
def authorize_trip(actor, trip_id, permiso):
    """Load a trip and check a permission on it in one step."""
    from app.models.trip import Trip

    trip = _load(Trip, trip_id)
    if trip is None:
        raise ResourceNotFound('El viaje solicitado no existe.')
    require(actor, permiso, trip, AuditResourceType.VIAJE, trip_id)
    return trip


def authorize_document(actor, document_id, permiso):
    """Load a document and check a permission on it in one step."""
    from app.models.document import Document

    document = _load(Document, document_id)
    if document is None or document.is_deleted:
        raise ResourceNotFound('El documento solicitado no existe.')
    require(actor, permiso, document, AuditResourceType.DOCUMENTO, document_id)
    return document


def authorize_alert(actor, alert_id, permiso):
    """Load an alert and check a permission on it in one step."""
    from app.models.alert import Alert

    alert = _load(Alert, alert_id)
    if alert is None:
        raise ResourceNotFound('La alerta solicitada no existe.')
    require(actor, permiso, alert, AuditResourceType.ALERTA, alert_id)
    return alert


def authorize_advisory(actor, advisory_id, permiso):
    """Load an advisory and check a permission on it in one step."""
    from app.models.advisory import SecurityAdvisory

    advisory = _load(SecurityAdvisory, advisory_id)
    if advisory is None or advisory.is_deleted:
        raise ResourceNotFound('La recomendación solicitada no existe.')
    require(actor, permiso, advisory, AuditResourceType.RECOMENDACION, advisory_id)
    return advisory


def _load(model, identifier):
    """Fetch by primary key, tolerating a string UUID."""
    if identifier is None:
        return None
    if isinstance(identifier, model):
        return identifier
    try:
        key = identifier if isinstance(identifier, uuid.UUID) else uuid.UUID(str(identifier))
    except (ValueError, TypeError):
        return None
    return db.session.get(model, key)


# ======================================================================
# Query scoping -- the only permitted way to build a list
# ======================================================================
def visible_trips_query(actor):
    """Trips this actor may see. The only supported way to list trips."""
    from app.models.trip import Trip, TripTraveler

    query = Trip.query.filter(Trip.is_deleted.is_(False))

    if actor is None or getattr(actor, 'is_anonymous', False):
        return query.filter(db.false())
    if getattr(actor, 'is_deleted', False) or not getattr(actor, 'is_active_account', False):
        return query.filter(db.false())

    if actor.has_role(RoleCode.ADMINISTRADOR) or actor.has_role(RoleCode.GESTOR):
        return query

    return (
        query.join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .filter(
            TripTraveler.user_id == actor.id,
            TripTraveler.is_deleted.is_(False),
        )
    )


def scope_itinerary_query(actor, trip, query, model):
    """Restrict an itinerary query to rows this actor may see.

    Managers and administrators see the whole trip. A traveller sees only rows
    addressed to them plus rows that apply to everyone (``trip_traveler_id`` is
    null). This is what makes section 5.4's traveller isolation structural.
    """
    if actor is None:
        return query.filter(db.false())
    if actor.has_role(RoleCode.ADMINISTRADOR) or actor.has_role(RoleCode.GESTOR):
        return query

    traveler = trip.traveler_for(actor.id) if trip else None
    if traveler is None:
        return query.filter(db.false())

    return query.filter(
        db.or_(
            model.trip_traveler_id.is_(None),
            model.trip_traveler_id == traveler.id,
        )
    )


def scope_itinerary_items(actor, trip, items):
    """In-memory equivalent of :func:`scope_itinerary_query`.

    Used where the collection is already loaded, for instance when building the
    AI context from a trip's relationships.
    """
    if actor is None:
        return []
    if actor.has_role(RoleCode.ADMINISTRADOR) or actor.has_role(RoleCode.GESTOR):
        return list(items)

    traveler = trip.traveler_for(actor.id) if trip else None
    if traveler is None:
        return []

    return [
        item for item in items
        if item.trip_traveler_id is None or item.trip_traveler_id == traveler.id
    ]


def visible_travelers(actor, trip):
    """Traveller rows this actor may see.

    A traveller sees the trip's roster -- they are travelling together, and the
    itinerary would be incomprehensible otherwise -- but not other travellers'
    personal documents or their individual segments, which
    :func:`scope_itinerary_query` filters separately.
    """
    if actor is None or trip is None:
        return []
    return trip.active_travelers


def permissions_for(actor, trip=None):
    """Every permission this actor holds, for rendering the interface.

    Used by templates to decide which buttons exist. It is a convenience, never
    the enforcement point: the server checks again on every request.
    """
    decision = can(actor, Permiso.VER_VIAJE, trip)
    return decision.permisos
