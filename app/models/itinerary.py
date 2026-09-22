"""Itinerary entities: transport, lodging, vehicles and other services.

Every instant is stored as the ``_local`` / ``_tz`` / ``_utc`` triple described
in ``app.utils.timeutil``: the alert engine compares ``_utc`` while the interface
renders ``_local`` with its zone. ``trip_traveler_id`` is nullable and means
"applies to every traveller on the trip".

Field-level provenance lives in :class:`FieldProvenance`, a side table, rather
than in columns on each entity -- one document can back a dozen fields and one
field can be corrected repeatedly, so the relation is genuinely many-to-one.
"""

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
    ProvenanceOrigin,
    SegmentStatus,
    SegmentType,
    ServiceType,
)


class ItineraryItemMixin(InstantMixin):
    """Columns shared by every itinerary entity."""

    @db.declared_attr
    def trip_id(cls):
        return db.Column(
            GUID(), db.ForeignKey('trips.id', ondelete='CASCADE'), nullable=False, index=True
        )

    @db.declared_attr
    def trip_traveler_id(cls):
        """Null means the item applies to every traveller on the trip."""
        return db.Column(
            GUID(),
            db.ForeignKey('trip_travelers.id', ondelete='CASCADE'),
            nullable=True,
            index=True,
        )

    @db.declared_attr
    def localizador(cls):
        return db.Column(db.String(60), index=True)

    @db.declared_attr
    def proveedor(cls):
        return db.Column(db.String(200))

    @db.declared_attr
    def observaciones(cls):
        return db.Column(db.Text)

    @db.declared_attr
    def datos(cls):
        """Provider-specific fields that do not warrant typed columns."""
        return db.Column(JSONBType())

    # --- Provenance rollup (denormalised from FieldProvenance) --------
    @db.declared_attr
    def confianza_min(cls):
        """Lowest per-field confidence, so low-trust rows can be filtered."""
        return db.Column(db.Numeric(3, 2))

    @db.declared_attr
    def requiere_revision(cls):
        return db.Column(db.Boolean, nullable=False, default=False, index=True)

    @db.declared_attr
    def documento_origen_id(cls):
        """The document this item was primarily derived from, if any."""
        return db.Column(GUID(), db.ForeignKey('documents.id'), nullable=True, index=True)

    @property
    def documento_origen_abrible(self):
        """Whether the source document can still be opened.

        The pointer survives the document being deleted, and it should: «this
        came from that document» is a fact about how the trip was built, and
        the approval that turned it into itinerary data was made by a person.
        Erasing the provenance to tidy up a dead link would throw away the
        answer to «where did this come from».

        What must not survive is the *offer*. Deleting a document leaves its
        items behind, and a button that leads to a 404 looks like a broken
        application rather than like a document somebody removed on purpose.
        """
        if self.documento_origen_id is None:
            return False
        documento = self.documento_origen
        return documento is not None and not documento.is_deleted

    @db.declared_attr
    def documento_origen_indice(cls):
        """Which service of that document this item is.

        A booking confirmation describes one *or more* services -- a return
        ticket is two flights in one email. Without this, re-approving an
        extraction would match every service to the same row and the second
        flight would overwrite the first.
        """
        return db.Column(db.Integer, nullable=True)

    # --- Costs (behind COSTS_ENABLED) ---------------------------------
    @db.declared_attr
    def importe(cls):
        return db.Column(db.Numeric(12, 2))

    @db.declared_attr
    def moneda(cls):
        return db.Column(db.String(3))

    @property
    def applies_to_all_travelers(self):
        return self.trip_traveler_id is None


class TravelSegment(SoftDeleteMixin, ItineraryItemMixin, BaseModel):
    """A transport leg: flight, train, bus, ferry or transfer."""

    __tablename__ = 'travel_segments'
    __table_args__ = (
        db.Index('ix_travel_segments_trip_salida', 'trip_id', 'salida_utc'),
        db.Index('ix_travel_segments_traveler_salida', 'trip_traveler_id', 'salida_utc'),
        enum_check('tipo', SegmentType, 'travel_segments'),
        enum_check('estado', SegmentStatus, 'travel_segments'),
    )

    tipo = enum_column(SegmentType, nullable=False, default=SegmentType.VUELO, index=True)
    estado = enum_column(SegmentStatus, nullable=False, default=SegmentStatus.CONFIRMADO)

    # --- Carrier and identification -----------------------------------
    numero = db.Column(db.String(30))
    operado_por = db.Column(db.String(200))
    clase = db.Column(db.String(60))
    asiento = db.Column(db.String(30))
    terminal_origen = db.Column(db.String(30))
    terminal_destino = db.Column(db.String(30))

    # --- Origin -------------------------------------------------------
    origen_codigo = db.Column(db.String(10), index=True)
    origen_nombre = db.Column(db.String(200))
    origen_ciudad = db.Column(db.String(160))
    origen_pais = db.Column(db.String(2), index=True)
    origen_location_id = db.Column(GUID(), db.ForeignKey('locations.id'), nullable=True)

    salida_local = db.Column(db.DateTime(timezone=False))
    salida_tz = db.Column(db.String(64))
    salida_utc = db.Column(db.DateTime(timezone=True), index=True)

    # --- Destination --------------------------------------------------
    destino_codigo = db.Column(db.String(10), index=True)
    destino_nombre = db.Column(db.String(200))
    destino_ciudad = db.Column(db.String(160))
    destino_pais = db.Column(db.String(2), index=True)
    destino_location_id = db.Column(GUID(), db.ForeignKey('locations.id'), nullable=True)

    llegada_local = db.Column(db.DateTime(timezone=False))
    llegada_tz = db.Column(db.String(64))
    llegada_utc = db.Column(db.DateTime(timezone=True), index=True)

    # --- Relationships ------------------------------------------------
    trip = db.relationship('Trip', back_populates='segments')
    traveler = db.relationship('TripTraveler', lazy='joined')
    origen_location = db.relationship('Location', foreign_keys=[origen_location_id], lazy='select')
    destino_location = db.relationship(
        'Location', foreign_keys=[destino_location_id], lazy='select'
    )
    documento_origen = db.relationship('Document', foreign_keys='TravelSegment.documento_origen_id',
                                       lazy='select')

    def __repr__(self):
        return f'<TravelSegment {self.tipo} {self.origen_codigo}->{self.destino_codigo}>'

    @property
    def duracion_minutos(self):
        """Real elapsed time in minutes, computed in UTC."""
        from app.utils.timeutil import minutes_between

        return minutes_between(self.salida_utc, self.llegada_utc)

    @property
    def etiqueta(self):
        """``IB3210 MAD → BER`` for display."""
        route = f'{self.origen_codigo or self.origen_ciudad or "?"} → ' \
                f'{self.destino_codigo or self.destino_ciudad or "?"}'
        return f'{self.numero} {route}' if self.numero else route

    # Start/end of the interval this item occupies, used by the overlap rules.
    @property
    def inicio_utc(self):
        return self.salida_utc

    @property
    def fin_utc(self):
        return self.llegada_utc

    def to_dict(self, include_costs=False):
        data = {
            'id': str(self.id),
            'tipo': str(self.tipo),
            'tipo_label': self.tipo.label if self.tipo else None,
            'estado': str(self.estado),
            'numero': self.numero,
            'proveedor': self.proveedor,
            'operado_por': self.operado_por,
            'clase': self.clase,
            'asiento': self.asiento,
            'localizador': self.localizador,
            'trip_traveler_id': str(self.trip_traveler_id) if self.trip_traveler_id else None,
            'origen': {
                'codigo': self.origen_codigo,
                'nombre': self.origen_nombre,
                'ciudad': self.origen_ciudad,
                'pais': self.origen_pais,
                'terminal': self.terminal_origen,
                'local': self.salida_local.isoformat() if self.salida_local else None,
                'tz': self.salida_tz,
                'utc': self.salida_utc.isoformat() if self.salida_utc else None,
            },
            'destino': {
                'codigo': self.destino_codigo,
                'nombre': self.destino_nombre,
                'ciudad': self.destino_ciudad,
                'pais': self.destino_pais,
                'terminal': self.terminal_destino,
                'local': self.llegada_local.isoformat() if self.llegada_local else None,
                'tz': self.llegada_tz,
                'utc': self.llegada_utc.isoformat() if self.llegada_utc else None,
            },
            'duracion_minutos': self.duracion_minutos,
            'confianza_min': float(self.confianza_min) if self.confianza_min is not None else None,
            'requiere_revision': self.requiere_revision,
            'documento_origen_id': (
                str(self.documento_origen_id) if self.documento_origen_id else None
            ),
            'observaciones': self.observaciones,
        }
        if include_costs:
            data['importe'] = float(self.importe) if self.importe is not None else None
            data['moneda'] = self.moneda
        return data


class Accommodation(SoftDeleteMixin, ItineraryItemMixin, BaseModel):
    """A hotel or other lodging booking."""

    __tablename__ = 'accommodations'
    __table_args__ = (
        db.Index('ix_accommodations_trip_checkin', 'trip_id', 'check_in_utc'),
        db.Index('ix_accommodations_traveler_checkin', 'trip_traveler_id', 'check_in_utc'),
    )

    nombre = db.Column(db.String(250), nullable=False)
    direccion = db.Column(db.String(400))
    ciudad = db.Column(db.String(160), index=True)
    pais = db.Column(db.String(2), index=True)
    location_id = db.Column(GUID(), db.ForeignKey('locations.id'), nullable=True)
    telefono = db.Column(db.String(50))
    email = db.Column(db.String(255))

    check_in_local = db.Column(db.DateTime(timezone=False))
    check_in_tz = db.Column(db.String(64))
    check_in_utc = db.Column(db.DateTime(timezone=True), index=True)

    check_out_local = db.Column(db.DateTime(timezone=False))
    check_out_tz = db.Column(db.String(64))
    check_out_utc = db.Column(db.DateTime(timezone=True), index=True)

    numero_habitaciones = db.Column(db.Integer)
    tipo_habitacion = db.Column(db.String(120))
    regimen = db.Column(db.String(60))

    trip = db.relationship('Trip', back_populates='accommodations')
    traveler = db.relationship('TripTraveler', lazy='joined')
    location = db.relationship('Location', foreign_keys=[location_id], lazy='select')
    documento_origen = db.relationship('Document', foreign_keys='Accommodation.documento_origen_id',
                                       lazy='select')

    def __repr__(self):
        return f'<Accommodation {self.nombre}>'

    @property
    def noches(self):
        """Number of nights, by local calendar date."""
        from app.utils.timeutil import local_date

        start = local_date(self.check_in_utc, self.check_in_tz)
        end = local_date(self.check_out_utc, self.check_out_tz)
        if not start or not end:
            return None
        return max((end - start).days, 0)

    @property
    def etiqueta(self):
        return f'{self.nombre} ({self.ciudad})' if self.ciudad else self.nombre

    @property
    def inicio_utc(self):
        return self.check_in_utc

    @property
    def fin_utc(self):
        return self.check_out_utc

    def to_dict(self, include_costs=False):
        data = {
            'id': str(self.id),
            'nombre': self.nombre,
            'direccion': self.direccion,
            'ciudad': self.ciudad,
            'pais': self.pais,
            'telefono': self.telefono,
            'localizador': self.localizador,
            'proveedor': self.proveedor,
            'trip_traveler_id': str(self.trip_traveler_id) if self.trip_traveler_id else None,
            'check_in': {
                'local': self.check_in_local.isoformat() if self.check_in_local else None,
                'tz': self.check_in_tz,
                'utc': self.check_in_utc.isoformat() if self.check_in_utc else None,
            },
            'check_out': {
                'local': self.check_out_local.isoformat() if self.check_out_local else None,
                'tz': self.check_out_tz,
                'utc': self.check_out_utc.isoformat() if self.check_out_utc else None,
            },
            'noches': self.noches,
            'numero_habitaciones': self.numero_habitaciones,
            'tipo_habitacion': self.tipo_habitacion,
            'regimen': self.regimen,
            'confianza_min': float(self.confianza_min) if self.confianza_min is not None else None,
            'requiere_revision': self.requiere_revision,
            'observaciones': self.observaciones,
        }
        if include_costs:
            data['importe'] = float(self.importe) if self.importe is not None else None
            data['moneda'] = self.moneda
        return data


class VehicleRental(SoftDeleteMixin, ItineraryItemMixin, BaseModel):
    """A vehicle rental booking."""

    __tablename__ = 'vehicle_rentals'
    __table_args__ = (
        db.Index('ix_vehicle_rentals_trip_recogida', 'trip_id', 'recogida_utc'),
    )

    categoria = db.Column(db.String(120))
    modelo = db.Column(db.String(160))
    matricula = db.Column(db.String(30))
    transmision = db.Column(db.String(30))

    recogida_lugar = db.Column(db.String(300))
    recogida_ciudad = db.Column(db.String(160))
    recogida_pais = db.Column(db.String(2))
    recogida_location_id = db.Column(GUID(), db.ForeignKey('locations.id'), nullable=True)
    recogida_local = db.Column(db.DateTime(timezone=False))
    recogida_tz = db.Column(db.String(64))
    recogida_utc = db.Column(db.DateTime(timezone=True), index=True)

    devolucion_lugar = db.Column(db.String(300))
    devolucion_ciudad = db.Column(db.String(160))
    devolucion_pais = db.Column(db.String(2))
    devolucion_location_id = db.Column(GUID(), db.ForeignKey('locations.id'), nullable=True)
    devolucion_local = db.Column(db.DateTime(timezone=False))
    devolucion_tz = db.Column(db.String(64))
    devolucion_utc = db.Column(db.DateTime(timezone=True), index=True)

    conductor_nombre = db.Column(db.String(200))
    franquicia = db.Column(db.Numeric(12, 2))

    trip = db.relationship('Trip', back_populates='vehicle_rentals')
    traveler = db.relationship('TripTraveler', lazy='joined')
    recogida_location = db.relationship(
        'Location', foreign_keys=[recogida_location_id], lazy='select'
    )
    devolucion_location = db.relationship(
        'Location', foreign_keys=[devolucion_location_id], lazy='select'
    )
    documento_origen = db.relationship('Document', foreign_keys='VehicleRental.documento_origen_id',
                                       lazy='select')

    def __repr__(self):
        return f'<VehicleRental {self.proveedor} {self.categoria}>'

    @property
    def etiqueta(self):
        return f'{self.proveedor or "Vehículo"} — {self.categoria or self.modelo or ""}'.strip(' —')

    @property
    def inicio_utc(self):
        return self.recogida_utc

    @property
    def fin_utc(self):
        return self.devolucion_utc

    def to_dict(self, include_costs=False):
        data = {
            'id': str(self.id),
            'proveedor': self.proveedor,
            'categoria': self.categoria,
            'modelo': self.modelo,
            'matricula': self.matricula,
            'localizador': self.localizador,
            'trip_traveler_id': str(self.trip_traveler_id) if self.trip_traveler_id else None,
            'recogida': {
                'lugar': self.recogida_lugar,
                'ciudad': self.recogida_ciudad,
                'pais': self.recogida_pais,
                'local': self.recogida_local.isoformat() if self.recogida_local else None,
                'tz': self.recogida_tz,
                'utc': self.recogida_utc.isoformat() if self.recogida_utc else None,
            },
            'devolucion': {
                'lugar': self.devolucion_lugar,
                'ciudad': self.devolucion_ciudad,
                'pais': self.devolucion_pais,
                'local': self.devolucion_local.isoformat() if self.devolucion_local else None,
                'tz': self.devolucion_tz,
                'utc': self.devolucion_utc.isoformat() if self.devolucion_utc else None,
            },
            'conductor_nombre': self.conductor_nombre,
            'confianza_min': float(self.confianza_min) if self.confianza_min is not None else None,
            'requiere_revision': self.requiere_revision,
            'observaciones': self.observaciones,
        }
        if include_costs:
            data['importe'] = float(self.importe) if self.importe is not None else None
            data['moneda'] = self.moneda
        return data


class OtherService(SoftDeleteMixin, ItineraryItemMixin, BaseModel):
    """Insurance, visas, parking, events and anything else on the itinerary."""

    __tablename__ = 'other_services'
    __table_args__ = (
        db.Index('ix_other_services_trip_inicio', 'trip_id', 'inicio_utc'),
        enum_check('tipo', ServiceType, 'other_services'),
    )

    tipo = enum_column(ServiceType, nullable=False, default=ServiceType.OTRO, index=True)
    nombre = db.Column(db.String(250), nullable=False)
    lugar = db.Column(db.String(300))
    ciudad = db.Column(db.String(160))
    pais = db.Column(db.String(2))

    inicio_local = db.Column(db.DateTime(timezone=False))
    inicio_tz = db.Column(db.String(64))
    inicio_utc = db.Column(db.DateTime(timezone=True), index=True)

    fin_local = db.Column(db.DateTime(timezone=False))
    fin_tz = db.Column(db.String(64))
    fin_utc = db.Column(db.DateTime(timezone=True))

    trip = db.relationship('Trip', back_populates='other_services')
    traveler = db.relationship('TripTraveler', lazy='joined')
    documento_origen = db.relationship('Document', foreign_keys='OtherService.documento_origen_id',
                                       lazy='select')

    def __repr__(self):
        return f'<OtherService {self.tipo} {self.nombre}>'

    @property
    def etiqueta(self):
        return self.nombre

    def to_dict(self, include_costs=False):
        data = {
            'id': str(self.id),
            'tipo': str(self.tipo),
            'tipo_label': self.tipo.label if self.tipo else None,
            'nombre': self.nombre,
            'lugar': self.lugar,
            'ciudad': self.ciudad,
            'pais': self.pais,
            'proveedor': self.proveedor,
            'localizador': self.localizador,
            'trip_traveler_id': str(self.trip_traveler_id) if self.trip_traveler_id else None,
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
            'requiere_revision': self.requiere_revision,
            'observaciones': self.observaciones,
        }
        if include_costs:
            data['importe'] = float(self.importe) if self.importe is not None else None
            data['moneda'] = self.moneda
        return data


class FieldProvenance(BaseModel):
    """Where one stored field value came from (specification section 2.3).

    Kept as a side table rather than columns on each entity: one document backs
    many fields, and one field can be corrected repeatedly, so the newest row per
    ``(entidad_tipo, entidad_id, campo)`` is the current provenance and the older
    rows are the change history the specification asks for.
    """

    __tablename__ = 'field_provenance'
    __table_args__ = (
        db.Index('ix_field_provenance_entidad', 'entidad_tipo', 'entidad_id'),
        db.Index('ix_field_provenance_entidad_campo', 'entidad_tipo', 'entidad_id', 'campo'),
        enum_check('origen', ProvenanceOrigin, 'field_provenance'),
    )

    entidad_tipo = db.Column(db.String(50), nullable=False)
    entidad_id = db.Column(GUID(), nullable=False)
    campo = db.Column(db.String(100), nullable=False)

    origen = enum_column(ProvenanceOrigin, nullable=False, index=True)
    confianza = db.Column(db.Numeric(3, 2))

    # --- Source reference (populated for 'documento' and 'ia') ---------
    document_id = db.Column(GUID(), db.ForeignKey('documents.id'), nullable=True, index=True)
    extraction_id = db.Column(GUID(), db.ForeignKey('extractions.id'), nullable=True)
    pagina = db.Column(db.Integer)
    #: The literal text span backing the value. Populated only for origin
    #: 'documento' -- that is precisely what distinguishes it from 'ia'.
    fragmento = db.Column(db.Text)

    # --- Change record -------------------------------------------------
    valor_anterior = db.Column(db.Text)
    valor_actual = db.Column(db.Text)
    actor_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)

    document = db.relationship('Document', lazy='select')
    extraction = db.relationship('Extraction', lazy='select')
    actor = db.relationship('User', foreign_keys=[actor_id], lazy='select')

    def __repr__(self):
        return f'<FieldProvenance {self.entidad_tipo}.{self.campo} {self.origen}>'

    def to_dict(self):
        return {
            'id': str(self.id),
            'campo': self.campo,
            'origen': str(self.origen),
            'origen_label': self.origen.label if self.origen else None,
            'confianza': float(self.confianza) if self.confianza is not None else None,
            'document_id': str(self.document_id) if self.document_id else None,
            'pagina': self.pagina,
            'fragmento': self.fragmento,
            'valor_anterior': self.valor_anterior,
            'valor_actual': self.valor_actual,
            'actor': self.actor.to_reference() if self.actor else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
