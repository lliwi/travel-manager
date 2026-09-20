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
#: The blocks the settings screen is organised into, in the order they are
#: shown, with the name a person reads. A group without an entry here falls back
#: to its own key, which is how «investigacion» ended up as a heading.
GRUPOS = (
    ('general', 'General', 'Zona horaria, idioma y datos de la organización.'),
    ('correo', 'Correo', 'Servidor de salida para las notificaciones.'),
    ('documentos', 'Documentos', 'Extracción, revisión y reconocimiento de texto.'),
    ('itinerario', 'Itinerario', 'Cómo se interpreta lo que compone un viaje.'),
    ('ia', 'Inteligencia artificial', 'Qué se registra de cada ejecución.'),
    ('recomendaciones', 'Recomendaciones de seguridad',
     'Generación, validación y vigilancia de las fuentes.'),
    ('investigacion', 'Investigación pública',
     'Consulta de fuentes oficiales en internet.'),
    ('busqueda_viajes', 'Búsqueda de vuelos y alojamiento',
     'Conector externo que devuelve opciones reales de viaje. Sin él, el '
     'asistente de planificación sigue funcionando, pero orienta sobre cómo '
     'viajar en lugar de decir qué hay.'),
    ('politica', 'Política corporativa',
     'Límites que un viaje debe respetar, y por encima de los cuales hace '
     'falta una aprobación.'),
    ('funcionalidad', 'Funcionalidad opcional',
     'Partes del sistema que su organización puede no necesitar.'),
    ('retencion', 'Retención', 'Cuánto tiempo se conserva cada cosa.'),
)

#: Group key -> (label, description), for the screen.
ETIQUETAS_GRUPO = {clave: (nombre, ayuda) for clave, nombre, ayuda in GRUPOS}

DEFAULTS = {
    # --- General ------------------------------------------------------
    'ZONA_HORARIA_POR_DEFECTO': (
        'Europe/Madrid', 'string', 'general', 'Zona horaria por defecto',
        'La que se propone al crear un viaje y la que se usa cuando un '
        'documento no permite deducir la suya. Nombre IANA, por ejemplo '
        '«Europe/Madrid».',
        True,
    ),
    'IDIOMA_POR_DEFECTO': (
        'es', 'string', 'general', 'Idioma por defecto',
        'Idioma de la interfaz y de lo que se pide a los modelos.',
        True,
    ),

    # --- Correo -------------------------------------------------------
    'CORREO_HABILITADO': (
        False, 'bool', 'correo', 'Enviar correo',
        'Mientras esté desactivado, las notificaciones solo se ven dentro de '
        'la aplicación.',
        False,
    ),
    'CORREO_HOST': (
        '', 'string', 'correo', 'Servidor SMTP',
        'En desarrollo, «mailpit» recoge todo sin entregar nada.',
        False,
    ),
    'CORREO_PUERTO': (
        1025, 'int', 'correo', 'Puerto', 'Habitualmente 587 con TLS, 1025 en pruebas.',
        False,
    ),
    'CORREO_USUARIO': (
        '', 'string', 'correo', 'Usuario', 'En blanco si el servidor no pide autenticación.',
        False,
    ),
    'CORREO_CONTRASENA': (
        '', 'secreto', 'correo', 'Contraseña',
        'Se guarda cifrada y no vuelve a mostrarse. Deje el campo en blanco '
        'para conservar la que ya hay.',
        False,
    ),
    'CORREO_TLS': (
        False, 'bool', 'correo', 'Usar TLS', 'STARTTLS sobre el puerto indicado.',
        False,
    ),
    'CORREO_REMITENTE': (
        'travel-manager@localhost', 'string', 'correo', 'Remitente',
        'Dirección desde la que se envían las notificaciones.',
        False,
    ),


    # --- Flight and hotel search --------------------------------------
    'BUSQUEDA_VIAJES_HABILITADA': (
        False, 'bool', 'busqueda_viajes', 'Buscar opciones reales',
        'Mientras esté desactivado, el asistente de planificación orienta '
        'sobre cómo viajar pero no consulta vuelos ni hoteles concretos.',
        False,
    ),
    'BUSQUEDA_VIAJES_API_KEY': (
        '', 'secreto', 'busqueda_viajes', 'Clave de SerpApi',
        'Se guarda cifrada y no vuelve a mostrarse. Deje el campo en blanco '
        'para conservar la que ya hay. Se obtiene en serpapi.com.',
        False,
    ),
    'BUSQUEDA_VIAJES_MAXIMAS_DIARIAS': (
        50, 'int', 'busqueda_viajes', 'Búsquedas máximas al día',
        'Cada consulta al conector se cobra. Alcanzado el límite, el '
        'asistente sigue respondiendo sin opciones reales en lugar de '
        'generar una factura que nadie esperaba.',
        False,
    ),
    'BUSQUEDA_VIAJES_MONEDA': (
        'EUR', 'string', 'busqueda_viajes', 'Moneda',
        'En la que se piden los precios. Código ISO de tres letras.',
        False,
    ),

    # --- Itinerary ----------------------------------------------------
    'CONEXION_MAX_HORAS': (
        24, 'int', 'itinerario', 'Máximo de una conexión (horas)',
        'Separación máxima entre dos trayectos para considerarlos una conexión. '
        'Por encima de ella son dos desplazamientos con una estancia en medio, '
        'no un enlace que haya que alcanzar.',
        True,
    ),

    'RECOMENDACIONES_REGENERAR_AL_CAMBIAR': (
        True, 'bool', 'recomendaciones', 'Regenerar si la fuente cambia',
        'Cuando una fuente oficial cambie lo que dice de un destino, volver a '
        'redactar las recomendaciones del viaje. Desactívelo si prefiere que '
        'el cambio solo quede anotado y decidir usted.',
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
    'OCR_IDIOMAS': (
        'spa+eng', 'string', 'documentos', 'Idiomas del OCR',
        'Modelos de Tesseract, separados por «+». Añadir idiomas que el '
        'documento no lleva empeora la lectura, así que conviene poner solo '
        'los que la organización recibe de verdad.',
        False,
    ),
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
    'INVESTIGACION_WEB_HABILITADA': (
        True, 'bool', 'funcionalidad', 'Investigación en internet',
        'Permite consultar las fuentes públicas autorizadas para redactar '
        'recomendaciones de seguridad.',
        False,
    ),

    # --- Política corporativa -----------------------------------------
    'POLITICA_MONEDA': (
        'EUR', 'string', 'politica', 'Moneda de la política',
        'Los límites de abajo se expresan en esta moneda. Un importe en otra '
        'no se compara: convertirlo exigiría un tipo de cambio que esta '
        'aplicación no tiene y que cambiaría el resultado sin avisar.',
        True,
    ),
    'POLITICA_COSTE_MAXIMO_VIAJE': (
        0, 'int', 'politica', 'Coste máximo por viaje',
        'Por encima de este importe el viaje necesita aprobación. Cero lo '
        'desactiva.',
        True,
    ),
    'POLITICA_COSTE_MAXIMO_NOCHE': (
        0, 'int', 'politica', 'Coste máximo por noche de alojamiento',
        'Por encima de este importe por noche, el alojamiento necesita '
        'aprobación. Cero lo desactiva.',
        True,
    ),
    'POLITICA_ANTELACION_MINIMA_DIAS': (
        0, 'int', 'politica', 'Antelación mínima (días)',
        'Reservar con menos antelación que esta se señala: suele costar más y '
        'la organización puede querer saberlo. Cero lo desactiva.',
        True,
    ),

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
}


def es_secreto(clave):
    """True when this setting holds a credential."""
    declared = DEFAULTS.get(clave)
    return bool(declared) and declared[1] == 'secreto'


def get(clave, default=None):
    """Read a setting, falling back to its declared default.

    A secret comes back in the clear here and nowhere else: the screen shows
    that one exists, never what it is. The reasoning is the one the AI provider
    keys already follow -- a credential in a column anyone can SELECT is a
    credential in plain text, whichever table it sits in.
    """
    setting = SystemSetting.query.filter_by(clave=clave).first()
    if setting is not None and setting.valor is not None:
        crudo = _unwrap(setting.valor)
        if es_secreto(clave) and crudo:
            from app.utils.crypto import decrypt_secret

            try:
                return decrypt_secret(crudo)
            except Exception:
                logger.warning('No se pudo descifrar el ajuste %s.', clave)
                return None
        return crudo
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

    if es_secreto(clave) and valor:
        from app.utils.crypto import encrypt_secret

        valor = encrypt_secret(str(valor))

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


# ======================================================================
# Runtime values that used to live in the environment
# ======================================================================
def zona_horaria_por_defecto():
    """The timezone proposed for a new trip and used when none can be derived."""
    return get('ZONA_HORARIA_POR_DEFECTO', None) or _arranque('DEFAULT_TIMEZONE', 'Europe/Madrid')


def idioma_por_defecto():
    """The interface and prompt language."""
    return get('IDIOMA_POR_DEFECTO', None) or _arranque('DEFAULT_LOCALE', 'es')


def idiomas_ocr():
    """The Tesseract models to try, as «spa+eng»."""
    return get('OCR_IDIOMAS', None) or _arranque('OCR_LANGUAGES', 'spa+eng')


def _arranque(clave, por_defecto):
    """The bootstrap value, for the moment before the settings table exists.

    A fresh install runs migrations and seeds before anything reads a setting,
    and a test app may never seed at all. The environment keeps answering until
    the row exists, and stops mattering the moment it does -- which is what
    makes these administrable instead of a redeploy.
    """
    from flask import current_app

    return current_app.config.get(clave, por_defecto)
