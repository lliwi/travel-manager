"""The alert rule contract.

Specification sections 2.4 and 5.3. Two design points matter:

* A rule receives a fully loaded :class:`TripContext` and never touches the
  database. That keeps rules testable with plain factories and lets the engine
  evaluate every rule from one set of queries.
* Every candidate derives a deterministic ``dedup_key`` from the rule code, the
  trip, the traveller and the entities involved. Re-running the engine therefore
  updates the same alert rather than creating a second one, and an alert a
  manager already dismissed is never resurrected.
"""
import logging
from collections import defaultdict

from app.models.enums import AlertSeverity
from app.utils.hashing import sha256_text

logger = logging.getLogger(__name__)


class TripContext:
    """Everything the rules need about one trip, loaded once.

    Built by the engine and handed to every rule, so evaluating eight rules
    costs one set of queries rather than eight.
    """

    def __init__(self, trip, segments, accommodations, vehicles, services,
                 destinations, travelers, settings=None, countries=None,
                 traveler_documents=None, documents=None):
        self.trip = trip
        self.segments = segments
        self.accommodations = accommodations
        self.vehicles = vehicles
        self.services = services
        self.destinations = destinations
        self.travelers = travelers
        #: ``{regla: AlertRuleSetting}``.
        self.settings = settings or {}
        #: ``{codigo_pais: Country}``.
        self.countries = countries or {}
        #: ``{user_id: [TravelerDocument]}``.
        self.traveler_documents = traveler_documents or {}
        self.documents = documents or []

        self._by_traveler = None

    @property
    def items_by_traveler(self):
        """Every itinerary item grouped by traveller id.

        Items with ``trip_traveler_id`` null apply to everyone, so they appear
        in every traveller's group -- which is what makes per-traveller overlap
        and connection checks correct.
        """
        if self._by_traveler is not None:
            return self._by_traveler

        traveler_ids = [t.id for t in self.travelers]
        grouped = defaultdict(lambda: {
            'segmentos': [], 'alojamientos': [], 'vehiculos': [], 'servicios': [],
        })

        for kind, collection in (
            ('segmentos', self.segments),
            ('alojamientos', self.accommodations),
            ('vehiculos', self.vehicles),
            ('servicios', self.services),
        ):
            for item in collection:
                if item.trip_traveler_id is None:
                    for traveler_id in traveler_ids:
                        grouped[traveler_id][kind].append(item)
                else:
                    grouped[item.trip_traveler_id][kind].append(item)

        self._by_traveler = dict(grouped)
        return self._by_traveler

    def traveler_by_id(self, traveler_id):
        return next((t for t in self.travelers if t.id == traveler_id), None)

    def country(self, code):
        """Look up a country by ISO code."""
        return self.countries.get(str(code).upper()) if code else None

    def is_schengen(self, code):
        """True when the country is in the Schengen area."""
        country = self.country(code)
        if country is not None:
            return bool(country.es_schengen)
        from app.services.seed_service import SCHENGEN_COUNTRIES

        return bool(code) and str(code).upper() in SCHENGEN_COUNTRIES

    def documents_for(self, entity):
        """Documents backing an itinerary entity, if any."""
        return [d for d in self.documents if d.id == entity.documento_origen_id]


class AlertCandidate:
    """One problem a rule found, before the reconciler decides what to do."""

    __slots__ = (
        'codigo', 'regla', 'severidad', 'titulo', 'mensaje', 'evidencia',
        'sugerencia', 'trip_traveler_id', 'entidades',
    )

    def __init__(self, codigo, regla, severidad, titulo, mensaje, evidencia=None,
                 sugerencia=None, trip_traveler_id=None, entidades=()):
        self.codigo = codigo
        self.regla = regla
        self.severidad = severidad
        self.titulo = titulo
        self.mensaje = mensaje
        self.evidencia = evidencia or {}
        self.sugerencia = sugerencia
        self.trip_traveler_id = trip_traveler_id
        #: Ids of the entities involved; part of the dedup key.
        self.entidades = tuple(str(e) for e in entidades)

    def dedup_key(self, trip_id):
        """Stable identity of this finding.

        Derived from the rule, the trip, the traveller and the *sorted* entity
        ids, so the same problem always produces the same key regardless of
        evaluation order -- and a different problem never collides with it.
        """
        return sha256_text(
            self.codigo,
            str(trip_id),
            str(self.trip_traveler_id) if self.trip_traveler_id else '',
            '|'.join(sorted(self.entidades)),
        )

    def __repr__(self):
        return f'<AlertCandidate {self.codigo} {self.severidad}>'


class AlertRule:
    """Base class for every validation rule."""

    #: Stable identifier, matching a row in ``alert_rule_settings``.
    codigo = None
    nombre = None
    severidad_por_defecto = AlertSeverity.MEDIA
    #: Runtime setting keys that must be true for this rule to run at all.
    requiere_flags = ()

    def evaluate(self, ctx):
        """Find problems in a trip.

        Args:
            ctx: A :class:`TripContext`, fully loaded.

        Returns:
            A list of :class:`AlertCandidate`.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    def setting(self, ctx, key, default=None):
        """Read one administrator-configured threshold."""
        config = ctx.settings.get(self.codigo)
        if config is None:
            return default
        return config.get(key, default)

    def severity(self, ctx):
        """The configured severity, falling back to the rule's default."""
        config = ctx.settings.get(self.codigo)
        if config is not None and config.severidad is not None:
            return config.severidad
        return self.severidad_por_defecto

    def is_enabled(self, ctx):
        """True when this rule should run for this trip."""
        config = ctx.settings.get(self.codigo)
        if config is not None and not config.activa:
            return False

        if self.requiere_flags:
            from app.services import settings_service

            if not all(settings_service.get_bool(flag) for flag in self.requiere_flags):
                return False
        return True

    def candidate(self, ctx, titulo, mensaje, entidades=(), trip_traveler_id=None,
                  evidencia=None, sugerencia=None, severidad=None):
        """Build a candidate with this rule's identity already filled in."""
        return AlertCandidate(
            codigo=self.codigo,
            regla=self.nombre or self.codigo,
            severidad=severidad or self.severity(ctx),
            titulo=titulo,
            mensaje=mensaje,
            evidencia=evidencia,
            sugerencia=sugerencia,
            trip_traveler_id=trip_traveler_id,
            entidades=entidades,
        )

    def __repr__(self):
        return f'<{type(self).__name__} {self.codigo}>'


# ======================================================================
# Registry
# ======================================================================
_REGISTRY = {}


def register(rule_cls):
    """Register a rule. Usable as a decorator."""
    if not rule_cls.codigo:
        raise ValueError(f'{rule_cls.__name__} no declara un código de regla.')
    _REGISTRY[rule_cls.codigo] = rule_cls
    return rule_cls


def all_rules():
    """Every registered rule, instantiated."""
    _load_rules()
    return [cls() for cls in _REGISTRY.values()]


def get_rule(codigo):
    """One registered rule by code."""
    _load_rules()
    rule_cls = _REGISTRY.get(codigo)
    return rule_cls() if rule_cls else None


def registered_codes():
    """Codes of every registered rule."""
    _load_rules()
    return sorted(_REGISTRY)


_LOADED = False


def _load_rules():
    """Import the rule modules so their decorators run."""
    global _LOADED
    if _LOADED:
        return
    from app.services.alerts import rules  # noqa: F401

    _LOADED = True
