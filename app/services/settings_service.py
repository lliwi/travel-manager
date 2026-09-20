"""Read and write administrator-editable runtime settings.

Specification section 10 leaves several decisions to the organisation. Rather
than hard-coding them, they live in ``system_settings`` with sensible defaults
declared here, so changing one is an administrative action, not a redeploy.
"""
import logging

from app.extensions import db
from app.models.settings import SystemSetting

logger = logging.getLogger(__name__)

#: Default settings, seeded on first run. Each entry is
#: ``clave: (valor, tipo, grupo, nombre, descripcion, visible_gestor)``.
DEFAULTS = {
    # --- Itinerary ----------------------------------------------------
    'CONEXION_MAX_HORAS': (
        24, 'int', 'itinerario', 'Máximo de una conexión (horas)',
        'Separación máxima entre dos trayectos para considerarlos una conexión. '
        'Por encima de ella son dos desplazamientos con una estancia en medio, '
        'no un enlace que haya que alcanzar.',
        True,
    ),

    'RECOMENDACIONES_VALIDAR_AL_GENERAR': (
        True, 'bool', 'recomendaciones', 'Validar al generar',
        'Las recomendaciones nacen validadas y el gestor rechaza las que no '
        'procedan. Desactívelo si su organización exige que alguien apruebe '
        'cada una antes de que sea visible, como describe la sección 2.7 de la '
        'especificación.',
        True,
    ),

    # --- Retention (section 3.2, RGPD) --------------------------------
    'RETENCION_DOCUMENTOS_DIAS': (
        1825, 'int', 'retencion', 'Retención de documentos (días)',
        'Días que se conserva el original de un documento antes de poder purgarlo.',
        False,
    ),
    'RETENCION_AUDITORIA_DIAS': (
        2555, 'int', 'retencion', 'Retención de auditoría (días)',
        'Días que se conservan los eventos de auditoría.',
        False,
    ),
    'RETENCION_EJECUCIONES_IA_DIAS': (
        365, 'int', 'retencion', 'Retención de ejecuciones de IA (días)',
        'Días que se conserva el registro de ejecuciones del asistente.',
        False,
    ),
    # --- Documents ----------------------------------------------------
    'DOCUMENTOS_AUTO_APROBAR': (
        False, 'bool', 'documentos', 'Aprobación automática',
        'Los datos extraídos de un documento pasan al itinerario en cuanto se '
        'obtienen, sin esperar a que nadie los apruebe. Lo dudoso llega '
        'marcado para revisión y la ficha de revisión sigue disponible para '
        'corregirlo. Desactivado, ningún dato entra hasta que un gestor lo '
        'confirma, como describe la sección 2.3 de la especificación.',
        True,
    ),
    'DOCUMENTOS_UMBRAL_REVISION': (
        0.85, 'float', 'documentos', 'Umbral de revisión obligatoria',
        'Por debajo de esta confianza un campo se marca siempre para revisión.',
        True,
    ),
    'VIAJERO_DESCARGA_ORIGINALES': (
        True, 'bool', 'documentos', 'El viajero puede descargar originales',
        'Permite a un viajero asignado descargar el documento original, '
        'siempre con registro de auditoría.',
        False,
    ),
    # --- Features -----------------------------------------------------
    'COSTES_HABILITADOS': (
        False, 'bool', 'funcionalidad', 'Tratamiento de costes',
        'Habilita los importes en viajes y servicios (sección 2.2 del requerimiento).',
        False,
    ),
    'DOCUMENTOS_VIAJERO_HABILITADOS': (
        False, 'bool', 'funcionalidad', 'Documentos personales del viajero',
        'Habilita el registro de pasaportes, visados y seguros, y las alertas '
        'de caducidad asociadas.',
        False,
    ),
    # --- AI egress policy (section 2.5) -------------------------------
    'IA_REGISTRAR_CONTENIDO_COMPLETO': (
        False, 'bool', 'ia', 'Registrar contenido completo de las ejecuciones',
        'Desactivado por defecto: solo se registra un resumen del resultado.',
        False,
    ),
    'IA_LIMITE_CONSULTAS_USUARIO_HORA': (
        30, 'int', 'ia', 'Límite de consultas por usuario y hora',
        'Protege los recursos de inferencia frente a un uso desproporcionado.',
        False,
    ),
    # --- Web research (section 2.6) -----------------------------------
    'BUSQUEDA_WEB_HABILITADA': (
        True, 'bool', 'investigacion', 'Búsqueda de información pública',
        'Permite consultar las fuentes incluidas en la lista blanca.',
        False,
    ),
    'RECOMENDACIONES_CADUCIDAD_DIAS': (
        30, 'int', 'investigacion', 'Caducidad de recomendaciones (días)',
        'Días tras los cuales una recomendación de seguridad debe regenerarse.',
        True,
    ),
    # --- Locale -------------------------------------------------------
    'IDIOMAS_OCR': (
        'spa+eng', 'string', 'general', 'Idiomas de OCR',
        'Idiomas que Tesseract intenta reconocer, separados por «+».',
        False,
    ),
    'ZONA_HORARIA_POR_DEFECTO': (
        'Europe/Madrid', 'string', 'general', 'Zona horaria por defecto',
        'Usada cuando no puede deducirse del documento ni del catálogo.',
        True,
    ),
}


def get(clave, default=None):
    """Read a setting, falling back to its declared default."""
    setting = SystemSetting.query.filter_by(clave=clave).first()
    if setting is not None and setting.valor is not None:
        return _unwrap(setting.valor)
    if default is not None:
        return default
    declared = DEFAULTS.get(clave)
    return declared[0] if declared else None


def get_bool(clave, default=False):
    """Read a boolean setting."""
    value = get(clave, None)
    if value is None:
        declared = DEFAULTS.get(clave)
        return bool(declared[0]) if declared else default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 'si', 'sí')


def get_int(clave, default=0):
    """Read an integer setting."""
    try:
        return int(get(clave, default))
    except (TypeError, ValueError):
        return default


def get_float(clave, default=0.0):
    """Read a float setting."""
    try:
        return float(get(clave, default))
    except (TypeError, ValueError):
        return default


def set_value(clave, valor, actor=None, commit=True):
    """Write a setting, creating it if it does not exist yet."""
    setting = SystemSetting.query.filter_by(clave=clave).first()
    declared = DEFAULTS.get(clave)

    if setting is None:
        setting = SystemSetting(
            clave=clave,
            tipo=declared[1] if declared else 'string',
            grupo=declared[2] if declared else 'general',
            nombre=declared[3] if declared else clave,
            descripcion=declared[4] if declared else None,
            visible_gestor=declared[5] if declared else False,
        )
        db.session.add(setting)

    setting.valor = {'v': valor}
    setting.actualizado_por_id = getattr(actor, 'id', None)

    if commit:
        db.session.commit()
    return setting


def all_settings(grupo=None, include_admin_only=True):
    """Every setting, optionally filtered by group and visibility."""
    query = SystemSetting.query
    if grupo:
        query = query.filter_by(grupo=grupo)
    if not include_admin_only:
        query = query.filter_by(visible_gestor=True)
    return query.order_by(SystemSetting.grupo, SystemSetting.clave).all()


def seed_defaults(commit=True):
    """Create any missing setting from :data:`DEFAULTS`.

    Idempotent: existing values are never overwritten, so an administrator's
    change survives an upgrade that adds new settings.
    """
    existing = {s.clave for s in SystemSetting.query.all()}
    created = 0
    for clave, (valor, tipo, grupo, nombre, descripcion, visible) in DEFAULTS.items():
        if clave in existing:
            continue
        db.session.add(SystemSetting(
            clave=clave,
            valor={'v': valor},
            tipo=tipo,
            grupo=grupo,
            nombre=nombre,
            descripcion=descripcion,
            visible_gestor=visible,
        ))
        created += 1
    if commit and created:
        db.session.commit()
    return created


def _unwrap(stored):
    """Settings are stored as ``{"v": value}`` so JSONB can hold scalars."""
    if isinstance(stored, dict) and set(stored.keys()) == {'v'}:
        return stored['v']
    return stored
