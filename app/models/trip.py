"""Trips, traveller assignments and destinations.

Specification section 2.2. A trip is the authorisation boundary of the whole
application: every document, segment, alert and advisory hangs off one, and
``authorization_service`` answers every access question by resolving the
resource back to its trip.
"""
from sqlalchemy import UniqueConstraint

from app.extensions import db
from app.models.base import (
    GUID,
    BaseModel,
    InstantMixin,
    JSONBType,
    SoftDeleteMixin,
    enum_check,
    enum_column,
)
from app.models.enums import (
    TRIP_CLOSED_STATES,
    TravelerRole,
    TripPurpose,
    TripStatus,
)
from app.utils.timeutil import utcnow


class Trip(SoftDeleteMixin, InstantMixin, BaseModel):
    """A business trip."""

    __tablename__ = 'trips'
    __table_args__ = (
        db.Index('ix_trips_estado_inicio', 'estado', 'inicio_utc'),
        db.Index('ix_trips_gestor_estado', 'gestor_id', 'estado'),
        enum_check('estado', TripStatus, 'trips'),
    )

    # --- Identification -----------------------------------------------
    referencia = db.Column(db.String(40), nullable=False, unique=True, index=True)
    titulo = db.Column(db.String(300), nullable=False)
    finalidad = enum_column(TripPurpose, nullable=True)
    finalidad_detalle = db.Column(db.String(500))
    observaciones = db.Column(db.Text)

    # --- Status -------------------------------------------------------
    estado = enum_column(TripStatus, nullable=False, default=TripStatus.BORRADOR, index=True)

    # --- Dates (local / tz / utc triple, see app.utils.timeutil) -------
    inicio_local = db.Column(db.DateTime(timezone=False))
    inicio_tz = db.Column(db.String(64))
    inicio_utc = db.Column(db.DateTime(timezone=True), index=True)

    fin_local = db.Column(db.DateTime(timezone=False))
    fin_tz = db.Column(db.String(64))
    fin_utc = db.Column(db.DateTime(timezone=True), index=True)

    # --- Ownership ----------------------------------------------------
    gestor_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=False, index=True)
    creado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)
    organizacion_id = db.Column(GUID(), db.ForeignKey('organizations.id'), nullable=True)

    # --- Derived state ------------------------------------------------
    #: Bumped whenever the itinerary changes, so the alert engine and any cache
    #: can tell cheaply whether a recalculation is warranted.
    itinerary_version = db.Column(db.Integer, nullable=False, default=0)
    alertas_recalculadas_en = db.Column(db.DateTime(timezone=True))

    # --- Costs (behind COSTS_ENABLED, specification section 2.2) -------
    #: Which project the trip is charged to. Free text and optional: not every
    #: trip belongs to one, and a required field would be filled with something
    #: made up, which is how a column stops meaning anything.
    proyecto = db.Column(db.String(120), index=True)

    coste_estimado = db.Column(db.Numeric(12, 2))
    moneda = db.Column(db.String(3))

    datos_extra = db.Column(JSONBType())

    # --- Relationships ------------------------------------------------
    gestor = db.relationship('User', foreign_keys=[gestor_id], lazy='joined')
    creado_por = db.relationship('User', foreign_keys=[creado_por_id], lazy='select')
    organizacion = db.relationship('Organization', lazy='select')

    travelers = db.relationship(
        'TripTraveler',
        back_populates='trip',
        cascade='all, delete-orphan',
        lazy='selectin',
    )
    destinations = db.relationship(
        'TripDestination',
        back_populates='trip',
        cascade='all, delete-orphan',
        order_by='TripDestination.orden',
        lazy='selectin',
    )
    segments = db.relationship(
        'TravelSegment', back_populates='trip', cascade='all, delete-orphan', lazy='select'
    )
    accommodations = db.relationship(
        'Accommodation', back_populates='trip', cascade='all, delete-orphan', lazy='select'
    )
    vehicle_rentals = db.relationship(
        'VehicleRental', back_populates='trip', cascade='all, delete-orphan', lazy='select'
    )
    other_services = db.relationship(
        'OtherService', back_populates='trip', cascade='all, delete-orphan', lazy='select'
    )
    documents = db.relationship(
        'Document', back_populates='trip', cascade='all, delete-orphan', lazy='select'
    )
    alerts = db.relationship(
        'Alert', back_populates='trip', cascade='all, delete-orphan', lazy='select'
    )
    advisories = db.relationship(
        'SecurityAdvisory', back_populates='trip', cascade='all, delete-orphan', lazy='select'
    )

    def __repr__(self):
        return f'<Trip {self.referencia} {self.estado}>'

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def is_closed(self):
        """True for finished or cancelled trips; the alert engine skips these."""
        return self.estado in TRIP_CLOSED_STATES

    @property
    def is_editable(self):
        """Cancelled trips are read-only; everything else can still be edited."""
        return self.estado is not TripStatus.CANCELADO and not self.is_deleted

    def touch_itinerary(self):
        """Record that the itinerary changed, so alerts get recalculated."""
        self.itinerary_version = (self.itinerary_version or 0) + 1
        self.updated_at = utcnow()
        return self

    # ------------------------------------------------------------------
    # Travellers and destinations
    # ------------------------------------------------------------------
    @property
    def active_travelers(self):
        """Traveller assignments that have not been removed."""
        return [t for t in self.travelers if not t.is_deleted]

    def traveler_for(self, user_id):
        """The assignment row for a given user, or None if not assigned."""
        target = str(user_id)
        for traveler in self.travelers:
            if not traveler.is_deleted and str(traveler.user_id) == target:
                return traveler
        return None

    def has_traveler(self, user_id):
        """True when the user is assigned to this trip."""
        return self.traveler_for(user_id) is not None

    @property
    def paises(self):
        """Distinct destination countries, in itinerary order."""
        seen = []
        for destination in self.destinations:
            if destination.pais_codigo and destination.pais_codigo not in seen:
                seen.append(destination.pais_codigo)
        return seen

    @property
    def resumen_destinos(self):
        """The places this trip goes to, e.g. ``Londres``.

        Not the same as the route: where a traveller lives is not somewhere
        they are travelling to, and it is this list that decides which
        countries get security advisories.
        """
        names = [d.ciudad for d in self.destinations if d.ciudad]
        return ' → '.join(names) if names else '—'

    @property
    def coste_total(self):
        """What the trip is known to cost, from its own itinerary.

        The sum of what the bookings say, not what somebody estimated: the
        estimate is a plan and this is the bill. Returns None when nothing
        carries an amount, which is different from zero -- «no lo sabemos» y
        «es gratis» no son lo mismo, y un límite no debe saltar por el primero.
        """
        total = None
        for coleccion in (self.segments, self.accommodations,
                          self.vehicle_rentals, self.other_services):
            for item in coleccion:
                if item.is_deleted or item.importe is None:
                    continue
                total = (total or 0) + float(item.importe)
        return total

    @property
    def monedas_del_itinerario(self):
        """Every currency the itinerary mentions.

        More than one means the total above is a sum of different things, and
        whoever reads it should be told rather than left to assume.
        """
        monedas = set()
        for coleccion in (self.segments, self.accommodations,
                          self.vehicle_rentals, self.other_services):
            for item in coleccion:
                if not item.is_deleted and item.importe is not None and item.moneda:
                    monedas.add(item.moneda)
        return sorted(monedas)

    @property
    def resumen_ruta(self):
        """The shape of the journey, e.g. ``Barcelona → Londres → Barcelona``.

        Read off the itinerary rather than the destinations, so the trip can be
        recognised at a glance without home having to be filed as a place
        someone is travelling to.
        """
        tramos = sorted(
            (s for s in self.segments if not s.is_deleted and s.salida_utc),
            key=lambda s: s.salida_utc,
        )
        if not tramos:
            return self.resumen_destinos

        parada = []
        for tramo in tramos:
            parada.append(tramo.origen_ciudad or tramo.origen_codigo)
            parada.append(tramo.destino_ciudad or tramo.destino_codigo)

        ruta = []
        for nombre in parada:
            if nombre and (not ruta or ruta[-1] != nombre):
                ruta.append(nombre)
        return ' → '.join(ruta) if ruta else self.resumen_destinos

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_dict(self, include_travelers=True, include_destinations=True, include_costs=False):
        """API representation.

        ``include_costs`` is driven by the caller's VER_COSTES permission, never
        by the model itself.
        """
        data = {
            'id': str(self.id),
            'referencia': self.referencia,
            'titulo': self.titulo,
            'estado': str(self.estado),
            'estado_label': self.estado.label if self.estado else None,
            'finalidad': str(self.finalidad) if self.finalidad else None,
            'finalidad_detalle': self.finalidad_detalle,
            'observaciones': self.observaciones,
            'proyecto': self.proyecto,
            'inicio': {
                'local': self.inicio_local.isoformat() if self.inicio_local else None,
                'tz': self.inicio_tz,
                'utc': self.inicio_utc.isoformat() if self.inicio_utc else None,
            },
            'fin': {
                'local': self.fin_local.isoformat() if self.fin_local else None,
                'tz': self.fin_tz,
                'utc': self.fin_utc.isoformat() if self.fin_utc else None,
            },
            'gestor': self.gestor.to_reference() if self.gestor else None,
            'itinerary_version': self.itinerary_version,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_travelers:
            data['viajeros'] = [t.to_dict() for t in self.active_travelers]
        if include_destinations:
            data['destinos'] = [d.to_dict() for d in self.destinations]
        if include_costs:
            data['coste_estimado'] = float(self.coste_estimado) if self.coste_estimado else None
            data['moneda'] = self.moneda
        return data


class TripTraveler(SoftDeleteMixin, BaseModel):
    """A person assigned to a trip.

    Itinerary entities reference *this* row rather than ``users.id`` directly, so
    removing someone from a trip cascades coherently through their segments,
    lodging and vehicles.
    """

    __tablename__ = 'trip_travelers'
    __table_args__ = (
        UniqueConstraint('trip_id', 'user_id', name='uq_trip_travelers_trip_id_user_id'),
        db.Index('ix_trip_travelers_user_trip', 'user_id', 'trip_id'),
        enum_check('rol_en_viaje', TravelerRole, 'trip_travelers'),
    )

    trip_id = db.Column(
        GUID(), db.ForeignKey('trips.id', ondelete='CASCADE'), nullable=False, index=True
    )
    user_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=False, index=True)
    rol_en_viaje = enum_column(TravelerRole, nullable=False, default=TravelerRole.VIAJERO)

    #: A traveller may join or leave mid-trip; null means the whole trip.
    desde_local = db.Column(db.DateTime(timezone=False))
    desde_tz = db.Column(db.String(64))
    desde_utc = db.Column(db.DateTime(timezone=True))

    hasta_local = db.Column(db.DateTime(timezone=False))
    hasta_tz = db.Column(db.String(64))
    hasta_utc = db.Column(db.DateTime(timezone=True))

    observaciones = db.Column(db.Text)
    asignado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)

    trip = db.relationship('Trip', back_populates='travelers')
    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')
    asignado_por = db.relationship('User', foreign_keys=[asignado_por_id], lazy='select')

    def __repr__(self):
        return f'<TripTraveler {self.user_id} on {self.trip_id}>'

    @property
    def nombre_completo(self):
        return self.user.nombre_completo if self.user else '—'

    def to_dict(self):
        return {
            'id': str(self.id),
            'trip_id': str(self.trip_id),
            'usuario': self.user.to_reference() if self.user else None,
            'rol_en_viaje': str(self.rol_en_viaje),
            'rol_label': self.rol_en_viaje.label if self.rol_en_viaje else None,
            'desde': self.desde_local.isoformat() if self.desde_local else None,
            'hasta': self.hasta_local.isoformat() if self.hasta_local else None,
            'observaciones': self.observaciones,
        }


class TripDestination(BaseModel):
    """One stop on the trip, in itinerary order (specification section 2.2)."""

    __tablename__ = 'trip_destinations'
    __table_args__ = (
        db.Index('ix_trip_destinations_trip_orden', 'trip_id', 'orden'),
    )

    trip_id = db.Column(
        GUID(), db.ForeignKey('trips.id', ondelete='CASCADE'), nullable=False, index=True
    )
    orden = db.Column(db.Integer, nullable=False, default=0)

    pais_codigo = db.Column(db.String(2), index=True)
    pais_nombre = db.Column(db.String(120))
    ciudad = db.Column(db.String(160))
    location_id = db.Column(GUID(), db.ForeignKey('locations.id'), nullable=True)
    zona_horaria = db.Column(db.String(64))

    #: True for a layover rather than a destination in its own right.
    es_escala = db.Column(db.Boolean, nullable=False, default=False)

    inicio_local = db.Column(db.DateTime(timezone=False))
    inicio_tz = db.Column(db.String(64))
    inicio_utc = db.Column(db.DateTime(timezone=True))

    fin_local = db.Column(db.DateTime(timezone=False))
    fin_tz = db.Column(db.String(64))
    fin_utc = db.Column(db.DateTime(timezone=True))

    observaciones = db.Column(db.Text)

    trip = db.relationship('Trip', back_populates='destinations')
    location = db.relationship('Location', lazy='joined')

    def __repr__(self):
        return f'<TripDestination {self.ciudad} ({self.pais_codigo})>'

    @property
    def etiqueta(self):
        """``Berlín (DE)`` for display."""
        if self.ciudad and self.pais_codigo:
            return f'{self.ciudad} ({self.pais_codigo})'
        return self.ciudad or self.pais_nombre or '—'

    def to_dict(self):
        return {
            'id': str(self.id),
            'orden': self.orden,
            'pais_codigo': self.pais_codigo,
            'pais_nombre': self.pais_nombre,
            'ciudad': self.ciudad,
            'zona_horaria': self.zona_horaria,
            'es_escala': self.es_escala,
            'inicio': self.inicio_local.isoformat() if self.inicio_local else None,
            'fin': self.fin_local.isoformat() if self.fin_local else None,
            'observaciones': self.observaciones,
        }
