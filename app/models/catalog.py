"""Reference catalogues: countries and locations.

These back the normalisation step of specification section 2.3 (airports,
stations, timezones) and the Schengen classification the connection-margin rule
needs (section 2.4).
"""
from sqlalchemy import UniqueConstraint

from app.extensions import db
from app.models.base import BaseModel, JSONBType, enum_check, enum_column
from app.models.enums import LocationKind


class Country(BaseModel):
    """An ISO 3166-1 country, with the flags the alert rules consult."""

    __tablename__ = 'countries'
    __table_args__ = (
        UniqueConstraint('codigo', name='uq_countries_codigo'),
    )

    codigo = db.Column(db.String(2), nullable=False, index=True)
    codigo_iso3 = db.Column(db.String(3))
    nombre = db.Column(db.String(120), nullable=False)
    nombre_en = db.Column(db.String(120))
    #: Drives the Schengen connection threshold (section 2.4).
    es_schengen = db.Column(db.Boolean, nullable=False, default=False, index=True)
    es_ue = db.Column(db.Boolean, nullable=False, default=False)
    zona_horaria_principal = db.Column(db.String(64))
    moneda = db.Column(db.String(3))
    prefijo_telefonico = db.Column(db.String(10))

    def __repr__(self):
        return f'<Country {self.codigo}>'

    def to_dict(self):
        return {
            'codigo': self.codigo,
            'nombre': self.nombre,
            'es_schengen': self.es_schengen,
            'es_ue': self.es_ue,
            'zona_horaria_principal': self.zona_horaria_principal,
            'moneda': self.moneda,
        }


class Location(BaseModel):
    """An airport, station, port or city used to normalise itinerary endpoints.

    The ``zona_horaria`` column is the reason this catalogue exists: without it
    a flight's local departure time cannot be converted to UTC, and the whole
    connection-margin calculation of section 5.3 is unreliable.
    """

    __tablename__ = 'locations'
    __table_args__ = (
        db.Index('ix_locations_codigo_tipo', 'codigo', 'tipo'),
        db.Index('ix_locations_ciudad_pais', 'ciudad', 'pais_codigo'),
        enum_check('tipo', LocationKind, 'locations'),
    )

    #: IATA for airports, UIC or operator code for stations.
    codigo = db.Column(db.String(10), index=True)
    codigo_icao = db.Column(db.String(10))
    tipo = enum_column(LocationKind, nullable=False, default=LocationKind.AEROPUERTO, index=True)

    nombre = db.Column(db.String(250), nullable=False)
    ciudad = db.Column(db.String(160), index=True)
    pais_codigo = db.Column(db.String(2), index=True)
    #: IANA timezone. Never guessed: an unresolved value is recorded as such so
    #: the extraction is flagged for review instead of silently defaulting.
    zona_horaria = db.Column(db.String(64))

    latitud = db.Column(db.Numeric(9, 6))
    longitud = db.Column(db.Numeric(9, 6))
    #: Alternative spellings and names, to help fuzzy matching.
    alias = db.Column(JSONBType())
    activo = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f'<Location {self.codigo or self.nombre}>'

    @property
    def etiqueta(self):
        """``MAD — Adolfo Suárez Madrid-Barajas`` for display."""
        return f'{self.codigo} — {self.nombre}' if self.codigo else self.nombre

    def to_dict(self):
        return {
            'id': str(self.id),
            'codigo': self.codigo,
            'tipo': str(self.tipo),
            'nombre': self.nombre,
            'ciudad': self.ciudad,
            'pais_codigo': self.pais_codigo,
            'zona_horaria': self.zona_horaria,
        }
