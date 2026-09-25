"""Every enumeration used by the domain model.

Storage convention: enum *values* are ASCII snake_case so the database never
holds accented text and a value can be compared, indexed and logged safely. The
accented Spanish shown to users is the second element of each member's tuple and
never reaches the database. The specification writes states like
``en preparación``; that is the label, not the stored value.
"""
import enum


class LabeledEnum(str, enum.Enum):
    """Base class giving every enum member a Spanish display label.

    Members are declared as ``NAME = ('valor', 'Etiqueta')``. The ``str`` mixin
    makes a member compare equal to its stored value, which keeps SQLAlchemy
    filters, Jinja comparisons and JSON serialisation straightforward.
    """

    def __new__(cls, value, label_es=None):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj._label = label_es if label_es is not None else value.replace('_', ' ').capitalize()
        return obj

    @property
    def label(self):
        """The Spanish text shown in the interface."""
        return self._label

    @classmethod
    def choices(cls):
        """``(value, label)`` pairs for WTForms select fields."""
        return [(member.value, member.label) for member in cls]

    @classmethod
    def values(cls):
        """Every stored value, for CHECK constraints and validation."""
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, raw, default=None):
        """Parse a stored or submitted value, returning ``default`` on failure."""
        if isinstance(raw, cls):
            return raw
        try:
            return cls(raw)
        except (ValueError, TypeError):
            return default

    def __str__(self):
        return self.value


def label(value):
    """Render any enum member (or plain value) as its Spanish label."""
    if isinstance(value, LabeledEnum):
        return value.label
    return value


# ======================================================================
# Identity and access
# ======================================================================
class UserStatus(LabeledEnum):
    ACTIVO = ('activo', 'Activo')
    INACTIVO = ('inactivo', 'Inactivo')
    BLOQUEADO = ('bloqueado', 'Bloqueado')
    PENDIENTE = ('pendiente', 'Pendiente de activación')


class IdentityProviderCode(LabeledEnum):
    """Which identity source owns this account (specification section 3.3)."""

    LOCAL = ('local', 'Local')
    LDAP = ('ldap', 'Active Directory / LDAP')


class RoleCode(LabeledEnum):
    """The three access profiles of specification section 2.1."""

    ADMINISTRADOR = ('administrador', 'Administrador')
    GESTOR = ('gestor', 'Gestor')
    USUARIO = ('usuario', 'Usuario')


# ======================================================================
# Trips
# ======================================================================
class TripStatus(LabeledEnum):
    """Specification section 2.2 suggested states."""

    BORRADOR = ('borrador', 'Borrador')
    EN_PREPARACION = ('en_preparacion', 'En preparación')
    CONFIRMADO = ('confirmado', 'Confirmado')
    EN_CURSO = ('en_curso', 'En curso')
    FINALIZADO = ('finalizado', 'Finalizado')
    CANCELADO = ('cancelado', 'Cancelado')

    @property
    def css_class(self):
        """Bootstrap contextual class used by the templates."""
        return {
            'borrador': 'secondary',
            'en_preparacion': 'info',
            'confirmado': 'primary',
            'en_curso': 'success',
            'finalizado': 'dark',
            'cancelado': 'danger',
        }[self.value]


#: Trips in these states are closed; the alert engine skips them.
TRIP_CLOSED_STATES = (TripStatus.FINALIZADO, TripStatus.CANCELADO)


class TravelerRole(LabeledEnum):
    """Why a person is attached to a trip."""

    VIAJERO = ('viajero', 'Viajero')
    ACOMPANANTE = ('acompanante', 'Acompañante')
    ORGANIZADOR = ('organizador', 'Organizador')


class TripPurpose(LabeledEnum):
    REUNION = ('reunion', 'Reunión')
    FORMACION = ('formacion', 'Formación')
    CONGRESO = ('congreso', 'Congreso / Feria')
    VISITA_CLIENTE = ('visita_cliente', 'Visita a cliente')
    AUDITORIA = ('auditoria', 'Auditoría')
    OTRO = ('otro', 'Otro')


# ======================================================================
# Itinerary
# ======================================================================
class SegmentType(LabeledEnum):
    """Transport segment kinds (specification sections 2.2 and 2.3)."""

    VUELO = ('vuelo', 'Vuelo')
    TREN = ('tren', 'Tren')
    AUTOBUS = ('autobus', 'Autobús')
    FERRY = ('ferry', 'Ferry')
    TRASLADO = ('traslado', 'Traslado')
    OTRO = ('otro', 'Otro')

    @property
    def icon(self):
        """Bootstrap Icons name used by the timeline."""
        return {
            'vuelo': 'airplane',
            'tren': 'train-front',
            'autobus': 'bus-front',
            'ferry': 'water',
            'traslado': 'taxi-front',
            'otro': 'signpost',
        }[self.value]


#: Segment types whose consecutive pairs are checked for connection margin.
CONNECTABLE_SEGMENT_TYPES = (
    SegmentType.VUELO,
    SegmentType.TREN,
    SegmentType.AUTOBUS,
    SegmentType.FERRY,
    SegmentType.TRASLADO,
)


class SegmentStatus(LabeledEnum):
    CONFIRMADO = ('confirmado', 'Confirmado')
    PENDIENTE = ('pendiente', 'Pendiente')
    CANCELADO = ('cancelado', 'Cancelado')
    MODIFICADO = ('modificado', 'Modificado')


class ServiceType(LabeledEnum):
    """Anything on the itinerary that is not transport, lodging or a vehicle."""

    SEGURO = ('seguro', 'Seguro')
    VISADO = ('visado', 'Visado')
    PARKING = ('parking', 'Parking')
    RESTAURANTE = ('restaurante', 'Restaurante')
    ENTRADA = ('entrada', 'Entrada / Evento')
    CONECTIVIDAD = ('conectividad', 'Conectividad')
    OTRO = ('otro', 'Otro')


class LocationKind(LabeledEnum):
    """Catalogue entry kinds used to normalise origins and destinations."""

    AEROPUERTO = ('aeropuerto', 'Aeropuerto')
    ESTACION_TREN = ('estacion_tren', 'Estación de tren')
    ESTACION_BUS = ('estacion_bus', 'Estación de autobuses')
    PUERTO = ('puerto', 'Puerto')
    CIUDAD = ('ciudad', 'Ciudad')
    DIRECCION = ('direccion', 'Dirección')


# ======================================================================
# Documents
# ======================================================================
class DocumentType(LabeledEnum):
    """Purpose declared by whoever uploaded the file.

    Distinct from :class:`DocumentClassification`, which is what the system
    detected. The specification lists both in section 4.
    """

    RESERVA = ('reserva', 'Reserva')
    BILLETE = ('billete', 'Billete')
    #: Distinto de un billete a propósito: el billete existe desde que se
    #: compra, la tarjeta de embarque solo desde que alguien factura. Poder
    #: distinguirlos es lo que permite avisar de que nadie lo ha hecho.
    TARJETA_EMBARQUE = ('tarjeta_embarque', 'Tarjeta de embarque')
    FACTURA = ('factura', 'Factura')
    IDENTIDAD = ('identidad', 'Documento de identidad')
    CORRESPONDENCIA = ('correspondencia', 'Correspondencia / Confirmación')
    OTRO = ('otro', 'Otro')


class DocumentClassification(LabeledEnum):
    """Detected document class (specification section 2.3 step 3)."""

    VUELO = ('vuelo', 'Vuelo')
    TREN = ('tren', 'Tren')
    HOTEL = ('hotel', 'Hotel')
    VEHICULO = ('vehiculo', 'Alquiler de vehículo')
    SEGURO = ('seguro', 'Seguro')
    VISADO = ('visado', 'Visado')
    OTRO = ('otro', 'Otro')
    DESCONOCIDO = ('desconocido', 'Sin clasificar')


class DocumentProcessState(LabeledEnum):
    """The document pipeline state machine (specification section 2.3).

    Transitions are enforced centrally by ``document_service.transition``; no
    other module may assign this field.
    """

    RECIBIDO = ('recibido', 'Recibido')
    VALIDADO = ('validado', 'Validado')
    ESCANEADO = ('escaneado', 'Escaneado')
    ALMACENADO = ('almacenado', 'Almacenado')
    TEXTO_EXTRAIDO = ('texto_extraido', 'Texto extraído')
    CLASIFICADO = ('clasificado', 'Clasificado')
    EXTRAIDO = ('extraido', 'Datos extraídos')
    NORMALIZADO = ('normalizado', 'Normalizado')
    PENDIENTE_REVISION = ('pendiente_revision', 'Pendiente de revisión')
    REVISADO = ('revisado', 'Revisado')
    APROBADO = ('aprobado', 'Aprobado')
    # Terminal / side states
    INFECTADO = ('infectado', 'Infectado')
    RECHAZADO = ('rechazado', 'Rechazado')
    ERROR = ('error', 'Error de proceso')
    DESCARTADO = ('descartado', 'Descartado')

    @property
    def css_class(self):
        if self.value in ('infectado', 'rechazado', 'error'):
            return 'danger'
        if self.value == 'aprobado':
            return 'success'
        if self.value in ('pendiente_revision', 'revisado'):
            return 'warning'
        if self.value == 'descartado':
            return 'secondary'
        return 'info'


#: Once a document reaches one of these it never moves again.
DOCUMENT_TERMINAL_STATES = (
    DocumentProcessState.INFECTADO,
    DocumentProcessState.RECHAZADO,
    DocumentProcessState.DESCARTADO,
)

#: Ordered happy path, used to render a progress indicator.
DOCUMENT_PIPELINE_ORDER = (
    DocumentProcessState.RECIBIDO,
    DocumentProcessState.VALIDADO,
    DocumentProcessState.ESCANEADO,
    DocumentProcessState.ALMACENADO,
    DocumentProcessState.TEXTO_EXTRAIDO,
    DocumentProcessState.CLASIFICADO,
    DocumentProcessState.EXTRAIDO,
    DocumentProcessState.NORMALIZADO,
    DocumentProcessState.PENDIENTE_REVISION,
    DocumentProcessState.REVISADO,
    DocumentProcessState.APROBADO,
)


class AntivirusStatus(LabeledEnum):
    PENDIENTE = ('pendiente', 'Pendiente')
    LIMPIO = ('limpio', 'Limpio')
    INFECTADO = ('infectado', 'Infectado')
    ERROR = ('error', 'Error de análisis')
    OMITIDO = ('omitido', 'Omitido')


# ======================================================================
# Extraction and provenance
# ======================================================================
class ExtractionState(LabeledEnum):
    PENDIENTE = ('pendiente', 'Pendiente')
    EN_REVISION = ('en_revision', 'En revisión')
    APROBADA = ('aprobada', 'Aprobada')
    RECHAZADA = ('rechazada', 'Rechazada')
    SUPERADA = ('superada', 'Superada por otra versión')
    ERROR = ('error', 'Error')


class ExtractionMethod(LabeledEnum):
    REGLAS = ('reglas', 'Reglas')
    IA = ('ia', 'IA')
    MANUAL = ('manual', 'Manual')
    MIXTO = ('mixto', 'Reglas + IA')


class ProvenanceOrigin(LabeledEnum):
    """Where a stored field value came from (specification section 2.3).

    The specification names the three origins but does not draw the boundary, so
    this is the rule the whole codebase applies:

    * ``documento`` -- the value has a literal span in the document text; the
      ``fragmento`` column is populated and the page is known.
    * ``ia``        -- the model inferred the value with no literal span, for
      instance a timezone derived from an airport code or a normalised carrier
      name.
    * ``manual``    -- a person typed or corrected the value.
    * ``buscador``  -- it came from the travel connector, because somebody
      picked that row. Not ``manual``: nobody typed the departure time. Not
      ``documento``: there is no text it appears in. The distinction is what
      lets the screen say a price is Google's and indicative, and that none of
      it is a booking.
    """

    DOCUMENTO = ('documento', 'Documento')
    MANUAL = ('manual', 'Manual')
    IA = ('ia', 'IA')
    BUSCADOR = ('buscador', 'Buscador')

    @property
    def css_class(self):
        return {
            'documento': 'primary', 'manual': 'success',
            'ia': 'info', 'buscador': 'warning',
        }[self.value]


class ApplicationOutcome(LabeledEnum):
    """What approving an extraction did to an itinerary entity."""

    CREADA = ('creada', 'Creada')
    ACTUALIZADA = ('actualizada', 'Actualizada')
    IGNORADA = ('ignorada', 'Ignorada')


# ======================================================================
# Alerts
# ======================================================================
class AlertSeverity(LabeledEnum):
    """Specification section 2.4."""

    INFORMATIVA = ('informativa', 'Informativa')
    MEDIA = ('media', 'Media')
    ALTA = ('alta', 'Alta')
    CRITICA = ('critica', 'Crítica')

    @property
    def rank(self):
        """Numeric ordering, so severities can be compared and sorted."""
        return {'informativa': 0, 'media': 1, 'alta': 2, 'critica': 3}[self.value]

    @property
    def css_class(self):
        return {
            'informativa': 'info',
            'media': 'warning',
            'alta': 'danger',
            'critica': 'dark',
        }[self.value]


class AlertState(LabeledEnum):
    """Specification section 2.4."""

    ABIERTA = ('abierta', 'Abierta')
    ACEPTADA = ('aceptada', 'Aceptada')
    RESUELTA = ('resuelta', 'Resuelta')
    DESCARTADA = ('descartada', 'Descartada')

    @property
    def css_class(self):
        """How the list shades this state, or None to leave it unshaded.

        Colour marks what somebody already decided about the alert. Open is
        the state nothing has happened to yet, so it stays unshaded: it is the
        neutral ground the other three stand out against, and shading all four
        would leave the list with no background to read them against.
        """
        return {
            'abierta': None,
            'aceptada': 'aceptada',
            'resuelta': 'resuelta',
            'descartada': 'descartada',
        }[self.value]


#: States a manager deliberately acted on. The reconciler must never resurrect
#: one of these into 'abierta' while the dedup key is unchanged.
ALERT_ACKNOWLEDGED_STATES = (AlertState.ACEPTADA, AlertState.DESCARTADA)
ALERT_OPEN_STATES = (AlertState.ABIERTA,)


class AlertTrigger(LabeledEnum):
    """What caused an alert recalculation."""

    MANUAL = ('manual', 'Recálculo manual')
    DOCUMENTO = ('documento', 'Aprobación de documento')
    ITINERARIO = ('itinerario', 'Cambio de itinerario')
    PROGRAMADO = ('programado', 'Recálculo programado')
    CONFIGURACION = ('configuracion', 'Cambio de umbrales')


# ======================================================================
# Security advisories
# ======================================================================
class AdvisoryLevel(LabeledEnum):
    NORMAL = ('normal', 'Precaución normal')
    PRECAUCION = ('precaucion', 'Precaución elevada')
    ALTO_RIESGO = ('alto_riesgo', 'Alto riesgo')
    DESACONSEJADO = ('desaconsejado', 'Viaje desaconsejado')

    @property
    def rank(self):
        return {'normal': 0, 'precaucion': 1, 'alto_riesgo': 2, 'desaconsejado': 3}[self.value]

    @property
    def css_class(self):
        return {
            'normal': 'success',
            'precaucion': 'warning',
            'alto_riesgo': 'danger',
            'desaconsejado': 'dark',
        }[self.value]


class AdvisoryValidationState(LabeledEnum):
    """Phase 1 requires a manager to validate before publication (section 2.7)."""

    BORRADOR = ('borrador', 'Borrador')
    PENDIENTE_VALIDACION = ('pendiente_validacion', 'Pendiente de validación')
    VALIDADA = ('validada', 'Validada')
    RECHAZADA = ('rechazada', 'Rechazada')
    CADUCADA = ('caducada', 'Caducada')


class NotificationKind(LabeledEnum):
    """What a notification is about, which decides its icon and its urgency."""

    ALERTA = ('alerta', 'Alerta del viaje')
    DOCUMENTO = ('documento', 'Documento')
    RECOMENDACION = ('recomendacion', 'Recomendación de seguridad')
    VIAJE = ('viaje', 'Viaje')

    @property
    def icon(self):
        return {
            'alerta': 'exclamation-triangle',
            'documento': 'file-earmark-text',
            'recomendacion': 'shield-check',
            'viaje': 'briefcase',
        }[self.value]


class AdvisoryCategory(LabeledEnum):
    SEGURIDAD = ('seguridad', 'Seguridad')
    SANIDAD = ('sanidad', 'Sanidad')
    ENTRADA = ('entrada', 'Requisitos de entrada')
    TRANSPORTE = ('transporte', 'Transporte')
    CONECTIVIDAD = ('conectividad', 'Conectividad')
    METEOROLOGIA = ('meteorologia', 'Meteorología')
    OTRO = ('otro', 'Otro')


# ======================================================================
# AI layer
# ======================================================================
class AIProviderCode(LabeledEnum):
    """Specification section 2.5: local deployments are the priority."""

    OLLAMA = ('ollama', 'Ollama (local)')
    VLLM = ('vllm', 'vLLM (autogestionado)')
    OPENAI = ('openai', 'OpenAI API')
    DEEPSEEK = ('deepseek', 'DeepSeek API')
    STUB = ('stub', 'Simulado (pruebas)')


#: Providers that send data outside the organisation's perimeter. The egress
#: policy of section 2.5 is enforced against this set.
EXTERNAL_AI_PROVIDERS = (AIProviderCode.OPENAI, AIProviderCode.DEEPSEEK)


class AITask(LabeledEnum):
    """The internal AI contract of specification section 6."""

    EXTRACT_DOCUMENT = ('extract_document', 'Extracción de documento')
    ANSWER_TRIP_QUESTION = ('answer_trip_question', 'Consulta sobre el viaje')
    SUMMARIZE_TRIP = ('summarize_trip', 'Resumen del viaje')
    ANALYZE_RISKS = ('analyze_risks', 'Análisis de riesgos')
    RESEARCH_PUBLIC_INFO = ('research_public_info', 'Investigación de información pública')
    CLASSIFY_DOCUMENT = ('classify_document', 'Clasificación de documento')
    EXPLAIN_ALERT = ('explain_alert', 'Explicación de alerta')
    PLAN_TRIP = ('plan_trip', 'Planificación de viaje')


#: Tasks whose payload necessarily contains document text or personal data.
#: The egress guard refuses to route these to an external provider unless an
#: administrator has explicitly enabled it (section 2.5).
SENSITIVE_AI_TASKS = (
    AITask.EXTRACT_DOCUMENT,
    AITask.CLASSIFY_DOCUMENT,
    AITask.ANSWER_TRIP_QUESTION,
    AITask.SUMMARIZE_TRIP,
    AITask.EXPLAIN_ALERT,
)


class AIRunState(LabeledEnum):
    PENDIENTE = ('pendiente', 'Pendiente')
    EN_CURSO = ('en_curso', 'En curso')
    COMPLETADA = ('completada', 'Completada')
    ERROR = ('error', 'Error')
    BLOQUEADA = ('bloqueada', 'Bloqueada por política')


class BackupJobType(LabeledEnum):
    COPIA = ('copia', 'Copia')
    RESTAURACION = ('restauracion', 'Restauración')


class BackupJobState(LabeledEnum):
    PENDIENTE = ('pendiente', 'En cola')
    EN_CURSO = ('en_curso', 'En curso')
    COMPLETADO = ('completado', 'Completado')
    ERROR = ('error', 'Error')


class AIParameterOrigin(LabeledEnum):
    """Who wrote a parameter profile: a person, or the tuner on their behalf."""

    MANUAL = ('manual', 'Manual')
    AUTOAJUSTE = ('autoajuste', 'Autoajuste')


class AIAutotuneState(LabeledEnum):
    PENDIENTE = ('pendiente', 'En cola')
    EN_CURSO = ('en_curso', 'En curso')
    COMPLETADO = ('completado', 'Completado')
    ERROR = ('error', 'Error')
    CANCELADO = ('cancelado', 'Cancelado')


# ======================================================================
# Audit
# ======================================================================
class AuditResult(LabeledEnum):
    EXITO = ('exito', 'Éxito')
    DENEGADO = ('denegado', 'Denegado')
    ERROR = ('error', 'Error')


class AuditResourceType(LabeledEnum):
    USUARIO = ('usuario', 'Usuario')
    ROL = ('rol', 'Rol')
    VIAJE = ('viaje', 'Viaje')
    VIAJERO = ('viajero', 'Viajero')
    DOCUMENTO = ('documento', 'Documento')
    EXTRACCION = ('extraccion', 'Extracción')
    SEGMENTO = ('segmento', 'Segmento')
    ALOJAMIENTO = ('alojamiento', 'Alojamiento')
    VEHICULO = ('vehiculo', 'Vehículo')
    SERVICIO = ('servicio', 'Servicio')
    ALERTA = ('alerta', 'Alerta')
    RECOMENDACION = ('recomendacion', 'Recomendación')
    EJECUCION_IA = ('ejecucion_ia', 'Ejecución de IA')
    CONFIGURACION = ('configuracion', 'Configuración')
    SESION = ('sesion', 'Sesión')
