"""Baseline data seeding.

Every function is idempotent: it creates what is missing and never overwrites
what an administrator has already tuned. That is what makes ``flask seed`` safe
to run on an upgrade that introduces new rules or settings.
"""
import logging

from app.extensions import db
from app.models.advisory import WebSource
from app.models.alert import AlertRuleSetting
from app.models.catalog import Country, Location
from app.models.enums import AdvisoryCategory, AlertSeverity, LocationKind, RoleCode
from app.models.user import Organization, Role

logger = logging.getLogger(__name__)

#: Role descriptions, taken from specification section 2.1.
ROLE_DEFINITIONS = {
    RoleCode.ADMINISTRADOR: (
        'Administrador',
        'Configuración global, gestión de usuarios y roles, proveedores de IA, '
        'catálogos, políticas de retención, auditoría y parámetros de integración '
        'AD/LDAP.',
    ),
    RoleCode.GESTOR: (
        'Gestor',
        'Alta, edición y cancelación de viajes; carga y consulta de documentación; '
        'extracción y revisión de datos; asignación de viajes a usuarios; consultas '
        'a IA y gestión de alertas.',
    ),
    RoleCode.USUARIO: (
        'Usuario',
        'Consulta exclusivamente de los viajes asignados, sus documentos procesados, '
        'itinerario, alertas y recomendaciones de seguridad.',
    ),
}


def seed_roles(commit=True):
    """Create the three access profiles."""
    existing = {str(r.codigo) for r in Role.query.all()}
    created = 0
    for code, (nombre, descripcion) in ROLE_DEFINITIONS.items():
        if code.value in existing:
            continue
        db.session.add(Role(
            codigo=code, nombre=nombre, descripcion=descripcion, es_sistema=True
        ))
        created += 1
    if commit and created:
        db.session.commit()
    return created


def seed_organization(commit=True):
    """Create the single default organisational scope.

    Unused in phase 1; see the note on ``Organization``.
    """
    if Organization.query.filter_by(codigo='default').first():
        return 0
    db.session.add(Organization(
        nombre='Organización', codigo='default', activa=True,
    ))
    if commit:
        db.session.commit()
    return 1


# ======================================================================
# Alert rules (specification section 2.4)
# ======================================================================
#: ``regla: (nombre, descripcion, severidad, parametros)``. The connection
#: thresholds are the ones the specification names explicitly.
ALERT_RULE_DEFINITIONS = {
    'conexion_margen_insuficiente': (
        'Margen de conexión insuficiente',
        'Detecta conexiones entre segmentos consecutivos cuyo margen es inferior '
        'al umbral configurado. El margen se calcula en UTC respetando la zona '
        'horaria de cada extremo.',
        AlertSeverity.ALTA,
        {
            'schengen_min': 90,
            'internacional_min': 150,
            'cambio_aeropuerto_extra_min': 120,
            'cambio_terminal_extra_min': 30,
            'traslado_min': 30,
            'margen_critico_min': 30,
        },
    ),
    'segmentos_solapados': (
        'Solapamiento de reservas',
        'Detecta segmentos o reservas del mismo viajero cuyos intervalos se solapan.',
        AlertSeverity.ALTA,
        {'tolerancia_min': 0},
    ),
    'llegada_posterior_reserva': (
        'Llegada posterior al inicio de una reserva',
        'La llegada del viajero es posterior al check-in del hotel o a la recogida '
        'del vehículo.',
        AlertSeverity.MEDIA,
        {'margen_tolerancia_min': 60},
    ),
    'noche_sin_alojamiento': (
        'Noche sin alojamiento',
        'Hay una noche del viaje sin alojamiento asignado para el viajero.',
        AlertSeverity.MEDIA,
        {},
    ),
    'datos_incompletos': (
        'Datos incompletos',
        'Falta el localizador, alguna fecha, el destino, el viajero o el documento '
        'soporte de un servicio.',
        AlertSeverity.INFORMATIVA,
        {'exigir_localizador': True, 'exigir_documento': False},
    ),
    'sin_documento_soporte': (
        'Sin documento soporte',
        'Un servicio del itinerario no tiene ningún documento que lo respalde.',
        AlertSeverity.INFORMATIVA,
        {},
    ),
    'cambio_zona_horaria': (
        'Cambio de zona horaria relevante',
        'El viajero cruza husos horarios con una diferencia que puede afectar a la '
        'planificación.',
        AlertSeverity.INFORMATIVA,
        {'umbral_horas': 3},
    ),
    'sin_tarjeta_de_embarque': (
        'Sin tarjeta de embarque',
        'Un vuelo sale pronto y no hay ninguna tarjeta de embarque adjunta, así que '
        'es posible que nadie haya facturado. Las aerolíneas suelen abrir la '
        'facturación 48 horas antes.',
        AlertSeverity.MEDIA,
        {'horas_antes': 48},
    ),
    'documento_viajero_por_caducar': (
        'Documento del viajero próximo a caducar',
        'El pasaporte, visado o seguro del viajero caduca cerca de las fechas del '
        'viaje. Requiere que los documentos del viajero estén habilitados.',
        AlertSeverity.ALTA,
        {'dias_aviso': 180, 'meses_validez_exigidos': 6},
    ),
}


def seed_alert_rules(commit=True):
    """Create the default configuration of each alert rule."""
    existing = {r.regla for r in AlertRuleSetting.query.all()}
    created = 0
    for regla, (nombre, descripcion, severidad, parametros) in ALERT_RULE_DEFINITIONS.items():
        if regla in existing:
            continue
        db.session.add(AlertRuleSetting(
            regla=regla,
            nombre=nombre,
            descripcion=descripcion,
            severidad=severidad,
            parametros=parametros,
            activa=True,
        ))
        created += 1
    if commit and created:
        db.session.commit()
    return created


# ======================================================================
# Web sources (specification section 2.6)
# ======================================================================
#: Official sources only. Commercial connectors need the legal and security
#: review section 2.6 demands before being added here.
WEB_SOURCE_DEFINITIONS = (
    ('Ministerio de Asuntos Exteriores (recomendaciones de viaje)',
     'exteriores.gob.es',
     'https://www.exteriores.gob.es/es/ServiciosAlCiudadano/Paginas/Recomendaciones-de-viaje.aspx',
     AdvisoryCategory.SEGURIDAD, True, 'ES', 'es', 10, 0.95),
    ('Ministerio de Sanidad — La salud también viaja',
     'sanidad.gob.es',
     'https://www.sanidad.gob.es/areas/sanidadExterior/laSaludTambienViaja/home.htm',
     AdvisoryCategory.SANIDAD, True, 'ES', 'es', 20, 0.95),
    ('Unión Europea — Re-open / viajes',
     'europa.eu', 'https://europa.eu/youreurope/citizens/travel/',
     AdvisoryCategory.ENTRADA, True, None, 'es', 30, 0.9),
    ('Organización Mundial de la Salud',
     'who.int', 'https://www.who.int/travel-advice',
     AdvisoryCategory.SANIDAD, True, None, 'en', 40, 0.9),
    ('UK Foreign Travel Advice',
     'gov.uk', 'https://www.gov.uk/foreign-travel-advice',
     AdvisoryCategory.SEGURIDAD, True, 'GB', 'en', 50, 0.85),
    # travel.state.gov queda fuera: responde 403 a cualquier cliente
    # automatizado, con cualquier User-Agent. Sortear esa protección sería
    # eludir un control de acceso ajeno, así que se sustituye por otra fuente
    # oficial equivalente en lugar de disfrazarse para entrar.
    ('Gobierno de Canadá — Travel Advice and Advisories',
     'travel.gc.ca', 'https://travel.gc.ca/travelling/advisories',
     AdvisoryCategory.SEGURIDAD, True, 'CA', 'en', 60, 0.85),
)


def seed_web_sources(commit=True):
    """Create the initial allow-list of public sources."""
    existing = {s.dominio for s in WebSource.query.all()}
    created = 0
    for (nombre, dominio, url, categoria, oficial, pais, idioma,
         prioridad, confianza) in WEB_SOURCE_DEFINITIONS:
        if dominio in existing:
            continue
        db.session.add(WebSource(
            nombre=nombre, dominio=dominio, url_base=url, categoria=categoria,
            es_oficial=oficial, pais_codigo=pais, idioma=idioma,
            prioridad=prioridad, confianza_base=confianza, activa=True,
        ))
        created += 1
    if commit and created:
        db.session.commit()
    return created


# ======================================================================
# Catalogues
# ======================================================================
#: The 29 Schengen states, which the connection-margin rule needs to pick a
#: threshold. Kept explicit rather than derived: Schengen membership is not the
#: same as EU membership and getting it wrong silently loosens a safety check.
SCHENGEN_COUNTRIES = frozenset({
    'AT', 'BE', 'BG', 'HR', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'HU',
    'IS', 'IT', 'LV', 'LI', 'LT', 'LU', 'MT', 'NL', 'NO', 'PL', 'PT', 'RO',
    'SK', 'SI', 'ES', 'SE', 'CH',
})

EU_COUNTRIES = frozenset({
    'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR',
    'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK',
    'SI', 'ES', 'SE',
})

#: ``codigo: (nombre, iso3, zona_horaria_principal, moneda, nombre_en)``. The
#: English name is not decoration: an official source in English files the
#: United Kingdom under «united-kingdom», and a destination named only in
#: Spanish cannot be recognised on its pages. A working set
#: covering the destinations a Spanish organisation travels to most; the full
#: ISO list can be imported later through the administration screen.
COUNTRY_DEFINITIONS = {
    'ES': ('España', 'ESP', 'Europe/Madrid', 'EUR', 'Spain'),
    'PT': ('Portugal', 'PRT', 'Europe/Lisbon', 'EUR', 'Portugal'),
    'FR': ('Francia', 'FRA', 'Europe/Paris', 'EUR', 'France'),
    'DE': ('Alemania', 'DEU', 'Europe/Berlin', 'EUR', 'Germany'),
    'IT': ('Italia', 'ITA', 'Europe/Rome', 'EUR', 'Italy'),
    'NL': ('Países Bajos', 'NLD', 'Europe/Amsterdam', 'EUR', 'Netherlands'),
    'BE': ('Bélgica', 'BEL', 'Europe/Brussels', 'EUR', 'Belgium'),
    'AT': ('Austria', 'AUT', 'Europe/Vienna', 'EUR', 'Austria'),
    'CH': ('Suiza', 'CHE', 'Europe/Zurich', 'CHF', 'Switzerland'),
    'DK': ('Dinamarca', 'DNK', 'Europe/Copenhagen', 'DKK', 'Denmark'),
    'SE': ('Suecia', 'SWE', 'Europe/Stockholm', 'SEK', 'Sweden'),
    'NO': ('Noruega', 'NOR', 'Europe/Oslo', 'NOK', 'Norway'),
    'FI': ('Finlandia', 'FIN', 'Europe/Helsinki', 'EUR', 'Finland'),
    'PL': ('Polonia', 'POL', 'Europe/Warsaw', 'PLN', 'Poland'),
    'CZ': ('Chequia', 'CZE', 'Europe/Prague', 'CZK', 'Czechia'),
    'HU': ('Hungría', 'HUN', 'Europe/Budapest', 'HUF', 'Hungary'),
    'GR': ('Grecia', 'GRC', 'Europe/Athens', 'EUR', 'Greece'),
    'IE': ('Irlanda', 'IRL', 'Europe/Dublin', 'EUR', 'Ireland'),
    'GB': ('Reino Unido', 'GBR', 'Europe/London', 'GBP', 'United Kingdom'),
    'US': ('Estados Unidos', 'USA', 'America/New_York', 'USD', 'United States'),
    'CA': ('Canadá', 'CAN', 'America/Toronto', 'CAD', 'Canada'),
    'MX': ('México', 'MEX', 'America/Mexico_City', 'MXN', 'Mexico'),
    'BR': ('Brasil', 'BRA', 'America/Sao_Paulo', 'BRL', 'Brazil'),
    'AR': ('Argentina', 'ARG', 'America/Argentina/Buenos_Aires', 'ARS', 'Argentina'),
    'CL': ('Chile', 'CHL', 'America/Santiago', 'CLP', 'Chile'),
    'CO': ('Colombia', 'COL', 'America/Bogota', 'COP', 'Colombia'),
    'PE': ('Perú', 'PER', 'America/Lima', 'PEN', 'Peru'),
    'MA': ('Marruecos', 'MAR', 'Africa/Casablanca', 'MAD', 'Morocco'),
    'ZA': ('Sudáfrica', 'ZAF', 'Africa/Johannesburg', 'ZAR', 'South Africa'),
    'AE': ('Emiratos Árabes Unidos', 'ARE', 'Asia/Dubai', 'AED', 'United Arab Emirates'),
    'TR': ('Turquía', 'TUR', 'Europe/Istanbul', 'TRY', 'Turkey'),
    'IL': ('Israel', 'ISR', 'Asia/Jerusalem', 'ILS', 'Israel'),
    'IN': ('India', 'IND', 'Asia/Kolkata', 'INR', 'India'),
    'CN': ('China', 'CHN', 'Asia/Shanghai', 'CNY', 'China'),
    'JP': ('Japón', 'JPN', 'Asia/Tokyo', 'JPY', 'Japan'),
    'KR': ('Corea del Sur', 'KOR', 'Asia/Seoul', 'KRW', 'South Korea'),
    'SG': ('Singapur', 'SGP', 'Asia/Singapore', 'SGD', 'Singapore'),
    'AU': ('Australia', 'AUS', 'Australia/Sydney', 'AUD', 'Australia'),
    'NZ': ('Nueva Zelanda', 'NZL', 'Pacific/Auckland', 'NZD', 'New Zealand'),
}

#: How each city is spelled elsewhere. The catalogue holds «Londres» and a
#: booking confirmation says «London»; without this the two are different
#: places, the stay keeps whatever timezone the model guessed, and the traveller
#: does not appear on the map. Only genuinely different names are listed --
#: accents are handled by normalising, so «Zürich» needs no entry for «Zurich».
#:
#: Keyed by the catalogue's own spelling, which is the one thing that lets two
#: services agree that «London» and «Londres» are the same city.
CITY_ALIASES = {
    'Londres': ('London',),
    'París': ('Paris',),
    'Lisboa': ('Lisbon',),
    'Oporto': ('Porto',),
    'Sevilla': ('Seville',),
    'Zaragoza': ('Saragossa',),
    'Palma': ('Palma de Mallorca', 'Majorca', 'Mallorca'),
    'Las Palmas': ('Las Palmas de Gran Canaria', 'Gran Canaria'),
    'Tenerife': ('Santa Cruz de Tenerife',),
    'Bruselas': ('Brussels', 'Bruxelles', 'Brussel'),
    'Berlín': ('Berlin',),
    'Fráncfort': ('Frankfurt', 'Frankfurt am Main'),
    'Múnich': ('Munich', 'München', 'Muenchen'),
    'Roma': ('Rome',),
    'Milán': ('Milan', 'Milano'),
    'Viena': ('Vienna', 'Wien'),
    'Ginebra': ('Geneva', 'Genève', 'Genf'),
    'Copenhague': ('Copenhagen', 'København'),
    'Estocolmo': ('Stockholm',),
    'Dublín': ('Dublin',),
    'Praga': ('Prague', 'Praha'),
    'Varsovia': ('Warsaw', 'Warszawa'),
    'Atenas': ('Athens', 'Athina'),
    'Estambul': ('Istanbul',),
    'Johannesburgo': ('Johannesburg',),
    'Dubái': ('Dubai',),
    # Bombay is what the catalogue says and Mumbai is what the ticket says.
    'Bombay': ('Mumbai',),
    'Nueva Delhi': ('New Delhi', 'Delhi'),
    'Pekín': ('Beijing', 'Peking'),
    'Shanghái': ('Shanghai',),
    'Seúl': ('Seoul',),
    'Tokio': ('Tokyo',),
    'Singapur': ('Singapore',),
    'Sídney': ('Sydney',),
    'Nueva York': ('New York', 'New York City'),
    'Los Ángeles': ('Los Angeles',),
    'Washington': ('Washington DC', 'Washington D.C.'),
    'Ciudad de México': ('Mexico City', 'México D.F.', 'CDMX'),
    'Santiago': ('Santiago de Chile',),
    'Ámsterdam': ('Amsterdam',),
    'Zúrich': ('Zurich',),
    'Málaga': ('Malaga',),
    'Bogotá': ('Bogota',),
    'São Paulo': ('Sao Paulo',),
    'Düsseldorf': ('Dusseldorf', 'Duesseldorf'),
}


#: ``codigo: (nombre, ciudad, pais, zona_horaria, tipo)``. Airports and major
#: stations. The timezone is the operative field: without it a local departure
#: time cannot be converted to UTC and the connection rule is unreliable.
LOCATION_DEFINITIONS = (
    # --- Spanish airports ---------------------------------------------
    ('MAD', 'Adolfo Suárez Madrid-Barajas', 'Madrid', 'ES', 'Europe/Madrid'),
    ('BCN', 'Josep Tarradellas Barcelona-El Prat', 'Barcelona', 'ES', 'Europe/Madrid'),
    ('AGP', 'Málaga-Costa del Sol', 'Málaga', 'ES', 'Europe/Madrid'),
    ('VLC', 'Valencia', 'Valencia', 'ES', 'Europe/Madrid'),
    ('SVQ', 'Sevilla', 'Sevilla', 'ES', 'Europe/Madrid'),
    ('BIO', 'Bilbao', 'Bilbao', 'ES', 'Europe/Madrid'),
    ('PMI', 'Palma de Mallorca', 'Palma', 'ES', 'Europe/Madrid'),
    ('ALC', 'Alicante-Elche', 'Alicante', 'ES', 'Europe/Madrid'),
    ('LPA', 'Gran Canaria', 'Las Palmas', 'ES', 'Atlantic/Canary'),
    ('TFN', 'Tenerife Norte', 'Tenerife', 'ES', 'Atlantic/Canary'),
    ('TFS', 'Tenerife Sur', 'Tenerife', 'ES', 'Atlantic/Canary'),
    ('SCQ', 'Santiago-Rosalía de Castro', 'Santiago de Compostela', 'ES', 'Europe/Madrid'),
    # --- European hubs -------------------------------------------------
    ('LHR', 'London Heathrow', 'Londres', 'GB', 'Europe/London'),
    ('LGW', 'London Gatwick', 'Londres', 'GB', 'Europe/London'),
    ('STN', 'London Stansted', 'Londres', 'GB', 'Europe/London'),
    ('CDG', 'Paris Charles de Gaulle', 'París', 'FR', 'Europe/Paris'),
    ('ORY', 'Paris Orly', 'París', 'FR', 'Europe/Paris'),
    ('AMS', 'Amsterdam Schiphol', 'Ámsterdam', 'NL', 'Europe/Amsterdam'),
    ('FRA', 'Frankfurt am Main', 'Fráncfort', 'DE', 'Europe/Berlin'),
    ('MUC', 'München Franz Josef Strauss', 'Múnich', 'DE', 'Europe/Berlin'),
    ('BER', 'Berlin Brandenburg', 'Berlín', 'DE', 'Europe/Berlin'),
    ('DUS', 'Düsseldorf', 'Düsseldorf', 'DE', 'Europe/Berlin'),
    ('FCO', 'Roma Fiumicino', 'Roma', 'IT', 'Europe/Rome'),
    ('MXP', 'Milano Malpensa', 'Milán', 'IT', 'Europe/Rome'),
    ('LIN', 'Milano Linate', 'Milán', 'IT', 'Europe/Rome'),
    ('LIS', 'Lisboa Humberto Delgado', 'Lisboa', 'PT', 'Europe/Lisbon'),
    ('OPO', 'Porto Francisco Sá Carneiro', 'Oporto', 'PT', 'Europe/Lisbon'),
    ('BRU', 'Brussels', 'Bruselas', 'BE', 'Europe/Brussels'),
    ('ZRH', 'Zürich', 'Zúrich', 'CH', 'Europe/Zurich'),
    ('GVA', 'Genève', 'Ginebra', 'CH', 'Europe/Zurich'),
    ('VIE', 'Wien Schwechat', 'Viena', 'AT', 'Europe/Vienna'),
    ('CPH', 'København Kastrup', 'Copenhague', 'DK', 'Europe/Copenhagen'),
    ('ARN', 'Stockholm Arlanda', 'Estocolmo', 'SE', 'Europe/Stockholm'),
    ('OSL', 'Oslo Gardermoen', 'Oslo', 'NO', 'Europe/Oslo'),
    ('HEL', 'Helsinki-Vantaa', 'Helsinki', 'FI', 'Europe/Helsinki'),
    ('DUB', 'Dublin', 'Dublín', 'IE', 'Europe/Dublin'),
    ('WAW', 'Warszawa Chopin', 'Varsovia', 'PL', 'Europe/Warsaw'),
    ('PRG', 'Praha Václav Havel', 'Praga', 'CZ', 'Europe/Prague'),
    ('BUD', 'Budapest Ferenc Liszt', 'Budapest', 'HU', 'Europe/Budapest'),
    ('ATH', 'Athens Eleftherios Venizelos', 'Atenas', 'GR', 'Europe/Athens'),
    ('IST', 'İstanbul', 'Estambul', 'TR', 'Europe/Istanbul'),
    # --- Intercontinental ----------------------------------------------
    ('JFK', 'New York John F. Kennedy', 'Nueva York', 'US', 'America/New_York'),
    ('EWR', 'Newark Liberty', 'Nueva York', 'US', 'America/New_York'),
    ('LAX', 'Los Angeles', 'Los Ángeles', 'US', 'America/Los_Angeles'),
    ('ORD', 'Chicago O\'Hare', 'Chicago', 'US', 'America/Chicago'),
    ('MIA', 'Miami', 'Miami', 'US', 'America/New_York'),
    ('SFO', 'San Francisco', 'San Francisco', 'US', 'America/Los_Angeles'),
    ('IAD', 'Washington Dulles', 'Washington', 'US', 'America/New_York'),
    ('YYZ', 'Toronto Pearson', 'Toronto', 'CA', 'America/Toronto'),
    ('MEX', 'Ciudad de México Benito Juárez', 'Ciudad de México', 'MX', 'America/Mexico_City'),
    ('GRU', 'São Paulo Guarulhos', 'São Paulo', 'BR', 'America/Sao_Paulo'),
    ('EZE', 'Buenos Aires Ezeiza', 'Buenos Aires', 'AR', 'America/Argentina/Buenos_Aires'),
    ('SCL', 'Santiago Arturo Merino Benítez', 'Santiago', 'CL', 'America/Santiago'),
    ('BOG', 'Bogotá El Dorado', 'Bogotá', 'CO', 'America/Bogota'),
    ('LIM', 'Lima Jorge Chávez', 'Lima', 'PE', 'America/Lima'),
    ('CMN', 'Casablanca Mohammed V', 'Casablanca', 'MA', 'Africa/Casablanca'),
    ('JNB', 'Johannesburg O. R. Tambo', 'Johannesburgo', 'ZA', 'Africa/Johannesburg'),
    ('DXB', 'Dubai International', 'Dubái', 'AE', 'Asia/Dubai'),
    ('TLV', 'Tel Aviv Ben Gurion', 'Tel Aviv', 'IL', 'Asia/Jerusalem'),
    ('DEL', 'Delhi Indira Gandhi', 'Nueva Delhi', 'IN', 'Asia/Kolkata'),
    ('BOM', 'Mumbai Chhatrapati Shivaji', 'Bombay', 'IN', 'Asia/Kolkata'),
    ('PEK', 'Beijing Capital', 'Pekín', 'CN', 'Asia/Shanghai'),
    ('PVG', 'Shanghai Pudong', 'Shanghái', 'CN', 'Asia/Shanghai'),
    ('HND', 'Tokyo Haneda', 'Tokio', 'JP', 'Asia/Tokyo'),
    ('NRT', 'Tokyo Narita', 'Tokio', 'JP', 'Asia/Tokyo'),
    ('ICN', 'Seoul Incheon', 'Seúl', 'KR', 'Asia/Seoul'),
    ('SIN', 'Singapore Changi', 'Singapur', 'SG', 'Asia/Singapore'),
    ('SYD', 'Sydney Kingsford Smith', 'Sídney', 'AU', 'Australia/Sydney'),
    ('AKL', 'Auckland', 'Auckland', 'NZ', 'Pacific/Auckland'),

    # --- Metropolitan codes -------------------------------------------
    # IATA codes for a city with several airports. A booking made «to London»
    # rather than to one terminal arrives as LON, which matched nothing and
    # left the traveller unplaced -- the catalogue held four London airports
    # and not London.
    ('LON', 'Londres (todos los aeropuertos)', 'Londres', 'GB', 'Europe/London'),
    ('PAR', 'París (todos los aeropuertos)', 'París', 'FR', 'Europe/Paris'),
    ('NYC', 'Nueva York (todos los aeropuertos)', 'Nueva York', 'US', 'America/New_York'),
    ('MIL', 'Milán (todos los aeropuertos)', 'Milán', 'IT', 'Europe/Rome'),
    ('ROM', 'Roma (todos los aeropuertos)', 'Roma', 'IT', 'Europe/Rome'),
    ('TYO', 'Tokio (todos los aeropuertos)', 'Tokio', 'JP', 'Asia/Tokyo'),
    ('BUE', 'Buenos Aires (todos los aeropuertos)', 'Buenos Aires', 'AR',
     'America/Argentina/Buenos_Aires'),
    ('SAO', 'São Paulo (todos los aeropuertos)', 'São Paulo', 'BR', 'America/Sao_Paulo'),
    ('WAS', 'Washington (todos los aeropuertos)', 'Washington', 'US', 'America/New_York'),
    ('CHI', 'Chicago (todos los aeropuertos)', 'Chicago', 'US', 'America/Chicago'),
    ('STO', 'Estocolmo (todos los aeropuertos)', 'Estocolmo', 'SE', 'Europe/Stockholm'),
)

#: Major rail stations, for train segments.
STATION_DEFINITIONS = (
    ('MADAT', 'Madrid Puerta de Atocha', 'Madrid', 'ES', 'Europe/Madrid'),
    ('MADCH', 'Madrid Chamartín', 'Madrid', 'ES', 'Europe/Madrid'),
    ('BCNSA', 'Barcelona Sants', 'Barcelona', 'ES', 'Europe/Madrid'),
    ('VLCJS', 'Valencia Joaquín Sorolla', 'Valencia', 'ES', 'Europe/Madrid'),
    ('SVQSJ', 'Sevilla Santa Justa', 'Sevilla', 'ES', 'Europe/Madrid'),
    ('ZAZDE', 'Zaragoza Delicias', 'Zaragoza', 'ES', 'Europe/Madrid'),
    ('PARLY', 'Paris Gare de Lyon', 'París', 'FR', 'Europe/Paris'),
    ('PARNO', 'Paris Gare du Nord', 'París', 'FR', 'Europe/Paris'),
    ('LONSP', 'London St Pancras International', 'Londres', 'GB', 'Europe/London'),
    ('BRUMI', 'Bruxelles-Midi', 'Bruselas', 'BE', 'Europe/Brussels'),
    ('AMSCS', 'Amsterdam Centraal', 'Ámsterdam', 'NL', 'Europe/Amsterdam'),
    ('FRAHB', 'Frankfurt (Main) Hauptbahnhof', 'Fráncfort', 'DE', 'Europe/Berlin'),
    ('BERHB', 'Berlin Hauptbahnhof', 'Berlín', 'DE', 'Europe/Berlin'),
    ('MILCE', 'Milano Centrale', 'Milán', 'IT', 'Europe/Rome'),
    ('ROMTE', 'Roma Termini', 'Roma', 'IT', 'Europe/Rome'),
    ('LISOR', 'Lisboa Oriente', 'Lisboa', 'PT', 'Europe/Lisbon'),
)


def seed_catalogs(commit=True):
    """Load countries and locations.

    Returns:
        ``(paises_creados, localizaciones_creadas)``.
    """
    countries_created = _seed_countries()
    locations_created = _seed_locations()
    if commit and (countries_created or locations_created):
        db.session.commit()
    return countries_created, locations_created


def _seed_countries():
    existing = {c.codigo for c in Country.query.all()}
    created = 0
    for codigo, definicion in COUNTRY_DEFINITIONS.items():
        nombre, iso3, tz, moneda = definicion[:4]
        nombre_en = definicion[4] if len(definicion) > 4 else None

        if codigo in existing:
            # Reference data catches up: a column added later stays empty on
            # every install that was seeded before it existed.
            fila = Country.query.filter_by(codigo=codigo).first()
            if fila is not None and not fila.nombre_en and nombre_en:
                fila.nombre_en = nombre_en
            continue
        db.session.add(Country(
            codigo=codigo,
            codigo_iso3=iso3,
            nombre=nombre,
            nombre_en=nombre_en,
            es_schengen=codigo in SCHENGEN_COUNTRIES,
            es_ue=codigo in EU_COUNTRIES,
            zona_horaria_principal=tz,
            moneda=moneda,
        ))
        created += 1
    return created


def _seed_locations():
    existing = {loc.codigo: loc for loc in Location.query.all() if loc.codigo}
    created = 0

    for definiciones, tipo in (
        (LOCATION_DEFINITIONS, LocationKind.AEROPUERTO),
        (STATION_DEFINITIONS, LocationKind.ESTACION_TREN),
    ):
        for codigo, nombre, ciudad, pais, tz in definiciones:
            fila = existing.get(codigo)
            if fila is None:
                db.session.add(Location(
                    codigo=codigo, nombre=nombre, ciudad=ciudad, pais_codigo=pais,
                    zona_horaria=tz, tipo=tipo, activo=True,
                    alias=list(CITY_ALIASES.get(ciudad, ())) or None,
                ))
                created += 1
                continue

            # Aliases are refreshed on an existing row, not only written on a
            # new one: they arrived after the catalogue did, and an install
            # that already ran the seed would otherwise never get them and
            # would keep failing to recognise «London» for ever.
            esperados = list(CITY_ALIASES.get(fila.ciudad or ciudad, ())) or None
            if fila.alias != esperados:
                fila.alias = esperados

    return created


def seed_ai_providers(commit=True):
    """Configure the initial local provider.

    Specification section 2.5 prioritises local deployments, so a fresh install
    is pointed at Ollama on the host. Everything about it -- endpoint, model,
    whether it is the default -- is editable from Administración → Proveedores
    de IA; these are only the values that make the system usable before anyone
    has configured anything.
    """
    from app.models.ai import AIProviderConfig
    from app.models.enums import AIProviderCode

    if AIProviderConfig.query.count():
        return 0

    from flask import current_app

    config = AIProviderConfig(
        nombre='Ollama local',
        proveedor=AIProviderCode.OLLAMA,
        base_url=current_app.config.get(
            'OLLAMA_BOOTSTRAP_URL', 'http://host.docker.internal:11434'
        ),
        modelo_por_defecto=current_app.config.get(
            'OLLAMA_BOOTSTRAP_MODEL', 'llama3.1:8b'
        ),
        activo=True,
        es_por_defecto=True,
        timeout_segundos=120,
        max_tokens=2048,
        temperatura=0.1,
    )
    db.session.add(config)
    if commit:
        db.session.commit()
    return 1


def is_schengen(country_code):
    """True when the country belongs to the Schengen area.

    Consults the catalogue first so an administrator can correct it, falling
    back to the compiled set.
    """
    if not country_code:
        return False
    code = str(country_code).upper()
    country = Country.query.filter_by(codigo=code).first()
    if country is not None:
        return bool(country.es_schengen)
    return code in SCHENGEN_COUNTRIES
