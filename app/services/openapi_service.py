"""The OpenAPI description of ``/api/v1``.

Derived from the application itself rather than written beside it. Paths,
methods, path parameters, the permission each endpoint requires and the error
responses it can return all come from walking the URL map and reading the
``__authz__`` marker the authorisation decorators leave behind -- so a new
endpoint appears in the contract by existing, and one that loses its guard
cannot quietly keep claiming it has one.

What cannot be derived is what a request body means. Those are declared in
``OPERACIONES`` below, and ``tests/test_openapi.py`` fails when an endpoint has
no entry, which is what stops the description drifting away from the code.
"""
import re

from flask import current_app

OPENAPI_VERSION = '3.1.0'

#: Flask's converters, as OpenAPI parameter schemas.
_CONVERSORES = {
    'string': {'type': 'string'},
    'int': {'type': 'integer'},
    'float': {'type': 'number'},
    'uuid': {'type': 'string', 'format': 'uuid'},
    'path': {'type': 'string'},
    'default': {'type': 'string'},
}

_RUTA_RE = re.compile(r'<(?:(?P<conv>[a-zA-Z_]+)(?::[^>]*)?:)?(?P<nombre>[a-zA-Z_][\w]*)>')


PREFIJO = '/api/v1'


def _ruta_openapi(regla):
    """``/api/v1/trips/<trip_id>`` becomes ``/trips/{trip_id}``.

    The prefix is dropped because it is already the server URL. Leaving it in
    both places makes every path resolve to /api/v1/api/v1/... for any client
    that reads the document literally, which is every generated client.
    """
    ruta = _RUTA_RE.sub(lambda m: '{' + m.group('nombre') + '}', str(regla))
    if ruta.startswith(PREFIJO):
        ruta = ruta[len(PREFIJO):] or '/'
    return ruta


def _parametros_de_ruta(regla):
    parametros = []
    for nombre in regla.arguments:
        conversor = regla._converters.get(nombre)
        clave = type(conversor).__name__.replace('Converter', '').lower()
        parametros.append({
            'name': nombre,
            'in': 'path',
            'required': True,
            'schema': _CONVERSORES.get(clave, _CONVERSORES['default']),
        })
    return sorted(parametros, key=lambda p: p['name'])


def _resumen(vista):
    """The first line of the view's docstring."""
    doc = (vista.__doc__ or '').strip()
    return doc.split('\n')[0] if doc else None


def _descripcion(vista):
    """The rest of the docstring, if it says more than the summary."""
    doc = (vista.__doc__ or '').strip()
    resto = '\n'.join(doc.split('\n')[1:]).strip()
    return ' '.join(resto.split()) or None


def _etiqueta(ruta):
    """Group operations by their first path segment."""
    partes = [p for p in ruta.split('/') if p and not p.startswith('{')]
    return partes[2] if len(partes) > 2 else 'general'


def build_spec(app=None):
    """Build the whole OpenAPI document."""
    app = app or current_app

    paths = {}
    for regla in app.url_map.iter_rules():
        ruta = str(regla)
        if not ruta.startswith('/api/v1'):
            continue

        vista = app.view_functions[regla.endpoint]
        nombre = regla.endpoint.rsplit('.', 1)[-1]
        declarado = OPERACIONES.get(nombre, {})

        camino = _ruta_openapi(regla)
        entrada = paths.setdefault(camino, {})
        parametros = _parametros_de_ruta(regla)
        if declarado.get('query'):
            parametros = parametros + list(declarado['query'])
        if parametros:
            entrada['parameters'] = parametros

        for metodo in sorted(regla.methods - {'HEAD', 'OPTIONS'}):
            entrada[metodo.lower()] = _operacion(
                nombre, vista, declarado, metodo, camino
            )

    return {
        'openapi': OPENAPI_VERSION,
        'info': {
            'title': 'Travel Manager — API v1',
            'version': app.config.get('APP_VERSION', '1.0.0'),
            'description': (
                'Gestión de viajes corporativos. Esta API y la interfaz web son '
                'dos representaciones de los mismos servicios y comparten la '
                'sesión, la protección CSRF y, sobre todo, las mismas decisiones '
                'de autorización: lo único que cambia es cómo se representa un '
                'error.\n\n'
                'Toda respuesta correcta viaja en el sobre `{"data": ..., '
                '"request_id": ...}`, y todo error en `{"error": {"codigo", '
                '"mensaje"}, "request_id": ...}`. El `request_id` es el '
                'identificador de correlación que aparece también en los '
                'registros del servidor.'
            ),
        },
        'servers': [{'url': '/api/v1'}],
        'tags': _TAGS,
        'components': {
            'securitySchemes': _SECURITY_SCHEMES,
            'schemas': SCHEMAS,
            'responses': RESPUESTAS_COMUNES,
        },
        'security': [{'sesion': []}, {'csrf': []}],
        'paths': dict(sorted(paths.items())),
    }


def _operacion(nombre, vista, declarado, metodo, camino):
    """One operation object."""
    authz = getattr(vista, '__authz__', None)
    publico = bool(getattr(vista, '__public__', False))

    operacion = {
        'operationId': nombre if metodo in ('GET', 'POST') else f'{nombre}',
        'tags': [_etiqueta(camino)],
        'responses': _respuestas(declarado, metodo, authz, publico),
    }

    resumen = declarado.get('summary') or _resumen(vista)
    if resumen:
        operacion['summary'] = resumen
    descripcion = declarado.get('description') or _descripcion(vista)
    if descripcion:
        operacion['description'] = descripcion

    if publico:
        # Overrides the document-level requirement: no session needed.
        operacion['security'] = []
    elif authz:
        permiso, recurso = authz
        operacion['x-permiso'] = permiso
        if recurso:
            operacion['x-recurso'] = recurso

    cuerpo = declarado.get('requestBody')
    if cuerpo and metodo in ('POST', 'PATCH', 'PUT'):
        operacion['requestBody'] = cuerpo

    return operacion


def _respuestas(declarado, metodo, authz, publico):
    """The responses an operation can produce.

    The error set is not decoration: an endpoint guarded by a resource
    permission answers 404 to someone with no relationship to that resource, so
    a probe cannot learn it exists, and 403 only to someone who is on it but
    lacks the specific permission.
    """
    exito = declarado.get('responses', {}).get(
        '200' if metodo != 'POST' else '201'
    )
    codigo = declarado.get('exito') or ('201' if metodo == 'POST' else '200')

    respuestas = {
        codigo: exito or {
            'description': 'Operación correcta.',
            'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Sobre'}}},
        },
    }

    if not publico:
        respuestas['401'] = {'$ref': '#/components/responses/NoAutenticado'}
    if metodo in ('POST', 'PATCH', 'PUT', 'DELETE'):
        respuestas['422'] = {'$ref': '#/components/responses/DatosInvalidos'}
    if authz and authz[1]:
        respuestas['403'] = {'$ref': '#/components/responses/SinPermiso'}
        respuestas['404'] = {'$ref': '#/components/responses/NoEncontrado'}
    respuestas['429'] = {'$ref': '#/components/responses/DemasiadasPeticiones'}

    return respuestas


_SECURITY_SCHEMES = {
    'sesion': {
        'type': 'apiKey',
        'in': 'cookie',
        'name': 'session',
        'description': (
            'Sesión de Flask-Login, la misma que usa la interfaz web. Se obtiene '
            'con POST /auth/login.'
        ),
    },
    'csrf': {
        'type': 'apiKey',
        'in': 'header',
        'name': 'X-CSRFToken',
        'description': (
            'Obligatorio en toda petición que modifique algo. No hay JWT en la '
            'fase 1: la API comparte la sesión con la interfaz.'
        ),
    },
}

_TAGS = [
    {'name': 'auth', 'description': 'Inicio y cierre de sesión.'},
    {'name': 'trips', 'description': 'Viajes, personas viajeras y destinos.'},
    {'name': 'documents', 'description': 'Documentación y su revisión.'},
    {'name': 'alerts', 'description': 'Incidencias logísticas detectadas.'},
    {'name': 'users', 'description': 'Cuentas y roles.'},
    {'name': 'audit-events', 'description': 'Traza de auditoría.'},
    {'name': 'security-advisories', 'description': 'Recomendaciones de seguridad.'},
]


def _obj(propiedades, requeridas=None, descripcion=None):
    esquema = {'type': 'object', 'properties': propiedades}
    if requeridas:
        esquema['required'] = list(requeridas)
    if descripcion:
        esquema['description'] = descripcion
    return esquema


def _ref(nombre):
    return {'$ref': f'#/components/schemas/{nombre}'}


_STR = {'type': 'string'}
_STR_NULL = {'type': ['string', 'null']}
_INT_NULL = {'type': ['integer', 'null']}
_NUM_NULL = {'type': ['number', 'null']}
_BOOL = {'type': 'boolean'}
_UUID = {'type': 'string', 'format': 'uuid'}
_UUID_NULL = {'type': ['string', 'null'], 'format': 'uuid'}

#: The three columns that make up every itinerary time. Kept together because
#: they are meaningless apart: the local value is what the ticket says, the
#: zone is what it means, and the UTC one is derived so margins across zones
#: can be compared at all.
_INSTANTE = _obj(
    {
        'local': {'type': ['string', 'null'], 'format': 'date-time',
                  'description': 'Hora local del lugar, tal y como figura en la reserva.'},
        'tz': {**_STR_NULL, 'description': 'Zona horaria IANA, p. ej. «Europe/Madrid».'},
        'utc': {'type': ['string', 'null'], 'format': 'date-time',
                'description': 'Derivado de los dos anteriores. Nunca se envía.'},
    },
    descripcion='Un instante, siempre como triple.',
)

_INSTANTE_ENTRADA = _obj(
    {
        'local': {**_STR_NULL, 'description': 'ISO-8601 sin zona: AAAA-MM-DDTHH:MM.'},
        'tz': {**_STR_NULL, 'description': 'Zona horaria IANA. En blanco: la del viaje.'},
    },
    descripcion=(
        'Un instante que entra. El campo «utc» se rechaza: es derivado, y '
        'aceptarlo permitiría que las tres columnas dejaran de concordar.'
    ),
)

_REFERENCIA_USUARIO = _obj({
    'id': _UUID, 'nombre_completo': _STR, 'email': _STR,
})

_LUGAR = _obj({
    'codigo': _STR_NULL, 'nombre': _STR_NULL, 'ciudad': _STR_NULL,
    'pais': _STR_NULL, 'terminal': _STR_NULL,
    'local': _STR_NULL, 'tz': _STR_NULL, 'utc': _STR_NULL,
}, descripcion='Un extremo de un trayecto: dónde y cuándo.')

SCHEMAS = {
    'Sobre': _obj(
        {'data': {}, 'request_id': _STR_NULL},
        ['data'],
        'Toda respuesta correcta. «request_id» es el identificador de '
        'correlación, que aparece igual en los registros del servidor.',
    ),
    'SobrePaginado': _obj(
        {
            'data': {'type': 'array', 'items': {}},
            'meta': _obj({
                'pagina': {'type': 'integer'}, 'por_pagina': {'type': 'integer'},
                'total': {'type': 'integer'}, 'paginas': {'type': 'integer'},
                'tiene_siguiente': _BOOL, 'tiene_anterior': _BOOL,
            }),
            'request_id': _STR_NULL,
        },
        ['data', 'meta'],
    ),
    'Error': _obj(
        {
            'error': _obj({
                'codigo': {**_STR, 'description': 'Identificador estable del error.'},
                'mensaje': {**_STR, 'description': 'Texto en español, apto para mostrar.'},
                'detalles': {'type': ['object', 'null']},
            }, ['codigo', 'mensaje']),
            'request_id': _STR_NULL,
        },
        ['error'],
    ),

    'Instante': _INSTANTE,
    'InstanteEntrada': _INSTANTE_ENTRADA,
    'ReferenciaUsuario': _REFERENCIA_USUARIO,
    'Lugar': _LUGAR,

    'Viaje': _obj({
        'id': _UUID,
        'referencia': {**_STR, 'description': 'Referencia interna, p. ej. VJ-2026-0001.'},
        'titulo': _STR,
        'estado': _STR, 'estado_label': _STR_NULL,
        'finalidad': _STR_NULL, 'finalidad_detalle': _STR_NULL,
        'observaciones': _STR_NULL,
        'inicio': _ref('Instante'), 'fin': _ref('Instante'),
        'gestor': _ref('ReferenciaUsuario'),
        'itinerary_version': {'type': 'integer'},
        'created_at': _STR_NULL, 'updated_at': _STR_NULL,
        'viajeros': {'type': 'array', 'items': _ref('Viajero')},
        'destinos': {'type': 'array', 'items': _ref('Destino')},
    }, ['id', 'referencia', 'titulo', 'estado']),

    'Viajero': _obj({
        'id': _UUID, 'trip_id': _UUID,
        'usuario': _ref('ReferenciaUsuario'),
        'rol_en_viaje': _STR, 'rol_label': _STR_NULL,
        'desde': _STR_NULL, 'hasta': _STR_NULL,
        'observaciones': _STR_NULL,
    }, ['id', 'trip_id'],
        'Una persona asignada a un viaje. «desde» y «hasta» vacíos significan '
        'que participa en todo el viaje.'),

    'Destino': _obj({
        'id': _UUID, 'orden': {'type': 'integer'},
        'pais_codigo': _STR_NULL, 'pais_nombre': _STR_NULL, 'ciudad': _STR_NULL,
        'zona_horaria': _STR_NULL, 'es_escala': _BOOL,
        'inicio': _ref('Instante'), 'fin': _ref('Instante'),
        'observaciones': _STR_NULL,
    }, ['id']),

    'Documento': _obj({
        'id': _UUID, 'trip_id': _UUID,
        'nombre_original': _STR,
        'tipo': _STR, 'tipo_label': _STR_NULL,
        'mime': _STR_NULL, 'extension': _STR_NULL,
        'tamano_bytes': _INT_NULL, 'tamano_legible': _STR_NULL,
        'paginas': _INT_NULL,
        'hash_sha256': {**_STR_NULL, 'description': 'Del original tal y como se subió.'},
        'estado_proceso': _STR, 'estado_label': _STR_NULL,
        'progreso': _obj({'paso': {'type': 'integer'}, 'total': {'type': 'integer'}}),
        'clasificacion': _STR_NULL, 'clasificacion_label': _STR_NULL,
        'clasificacion_confianza': _NUM_NULL,
        'antivirus_estado': _STR_NULL,
        'uso_ocr': _BOOL, 'idioma_detectado': _STR_NULL,
        'subido_por': _ref('ReferenciaUsuario'), 'subido_en': _STR_NULL,
        'disponible': {**_BOOL, 'description':
                       'Falso cuando el original ya se purgó por retención.'},
    }, ['id', 'trip_id', 'nombre_original', 'estado_proceso']),

    'Alerta': _obj({
        'id': _UUID, 'trip_id': _UUID, 'trip_traveler_id': _UUID_NULL,
        'tipo': _STR, 'regla': _STR,
        'severidad': _STR, 'severidad_label': _STR_NULL,
        'estado': _STR, 'estado_label': _STR_NULL,
        'titulo': _STR, 'mensaje': _STR_NULL, 'sugerencia': _STR_NULL,
        'generada_en': _STR_NULL, 'resuelta_en': _STR_NULL,
        'resuelta_por': {'oneOf': [_ref('ReferenciaUsuario'), {'type': 'null'}]},
        'responsable': {'oneOf': [_ref('ReferenciaUsuario'), {'type': 'null'}]},
        'comentario': _STR_NULL, 'cierre_automatico': _BOOL,
        'evidencia': {'type': ['object', 'null'], 'description':
                      'Los datos concretos que la produjeron: los elementos '
                      'implicados, el margen calculado y el umbral aplicado.'},
    }, ['id', 'trip_id', 'tipo', 'regla', 'severidad', 'estado', 'titulo']),

    'Tramo': _obj({
        'id': _UUID, 'tipo': _STR, 'tipo_label': _STR_NULL, 'estado': _STR_NULL,
        'numero': _STR_NULL, 'proveedor': _STR_NULL, 'operado_por': _STR_NULL,
        'clase': _STR_NULL, 'asiento': _STR_NULL, 'localizador': _STR_NULL,
        'trip_traveler_id': _UUID_NULL,
        'origen': _ref('Lugar'), 'destino': _ref('Lugar'),
        'duracion_minutos': _INT_NULL,
        'confianza_min': _NUM_NULL, 'requiere_revision': _BOOL,
        'documento_origen_id': _UUID_NULL,
        'observaciones': _STR_NULL,
    }, ['id', 'tipo']),

    'Alojamiento': _obj({
        'id': _UUID, 'nombre': _STR, 'direccion': _STR_NULL, 'ciudad': _STR_NULL,
        'pais': _STR_NULL, 'telefono': _STR_NULL,
        'localizador': _STR_NULL, 'proveedor': _STR_NULL,
        'trip_traveler_id': _UUID_NULL,
        'check_in': _ref('Instante'), 'check_out': _ref('Instante'),
        'noches': _INT_NULL, 'numero_habitaciones': _INT_NULL,
        'tipo_habitacion': _STR_NULL, 'regimen': _STR_NULL,
        'confianza_min': _NUM_NULL, 'requiere_revision': _BOOL,
        'observaciones': _STR_NULL,
    }, ['id', 'nombre']),

    'Usuario': _obj({
        'id': _UUID, 'username': _STR, 'email': _STR,
        'nombre': _STR_NULL, 'apellidos': _STR_NULL, 'nombre_completo': _STR,
        'puesto': _STR_NULL, 'departamento': _STR_NULL,
        'estado': _STR, 'identity_provider': _STR,
        'idioma': _STR_NULL, 'zona_horaria': _STR_NULL,
        'last_login_at': _STR_NULL, 'created_at': _STR_NULL,
        'roles': {'type': 'array', 'items': _STR},
    }, ['id', 'username', 'email'],
        'Nunca incluye el hash de la contraseña.'),

    'RespuestaAsistente': _obj({
        'respuesta': _STR,
        'referencias': {'type': 'array', 'items': _obj({
            'id': _STR, 'tipo': _STR_NULL, 'descripcion': _STR_NULL,
        })},
        'confianza': _NUM_NULL,
        'datos_insuficientes': _BOOL,
        'fecha_datos': _STR_NULL,
        'run_id': _STR, 'proveedor': _STR_NULL, 'modelo': _STR_NULL,
    }, ['respuesta'],
        'Las referencias se validan contra el conjunto autorizado que se '
        'entregó al modelo: una que no estuviera en él se descarta antes de '
        'responder.'),
}


def _error(descripcion):
    return {
        'description': descripcion,
        'content': {'application/json': {'schema': _ref('Error')}},
    }


#: Referenced from every operation, so the error contract is stated once.
RESPUESTAS_COMUNES = {
    'NoAutenticado': _error('Falta la sesión, o ha caducado.'),
    'SinPermiso': _error(
        'Tiene relación con el recurso pero no el permiso concreto que la '
        'operación exige.'
    ),
    'NoEncontrado': _error(
        'El recurso no existe, o no tiene ninguna relación con él. Se '
        'responde lo mismo en ambos casos a propósito: un 403 confirmaría '
        'que el recurso existe a quien solo estaba sondeando.'
    ),
    'DatosInvalidos': _error('Los datos enviados no son válidos.'),
    'DemasiadasPeticiones': _error('Ha superado el límite de peticiones.'),
}


def _datos(esquema, descripcion='Operación correcta.'):
    """A success response whose «data» is the given schema."""
    return {
        'description': descripcion,
        'content': {'application/json': {'schema': _obj(
            {'data': esquema, 'request_id': _STR_NULL}, ['data'],
        )}},
    }


def _lista(nombre, descripcion='Operación correcta.'):
    return _datos({'type': 'array', 'items': _ref(nombre)}, descripcion)


def _pagina(nombre, descripcion='Operación correcta.'):
    return {
        'description': descripcion,
        'content': {'application/json': {'schema': {
            'allOf': [
                _ref('SobrePaginado'),
                _obj({'data': {'type': 'array', 'items': _ref(nombre)}}),
            ],
        }}},
    }


def _cuerpo(esquema, requerido=True):
    return {
        'required': requerido,
        'content': {'application/json': {'schema': esquema}},
    }


_QUERY_PAGINA = [
    {'name': 'page', 'in': 'query', 'required': False,
     'schema': {'type': 'integer', 'minimum': 1, 'default': 1}},
    {'name': 'per_page', 'in': 'query', 'required': False,
     'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20}},
]

_VIAJE_ESCRIBIBLE = {
    'titulo': _STR,
    'estado': _STR,
    'finalidad': _STR_NULL,
    'finalidad_detalle': _STR_NULL,
    'observaciones': _STR_NULL,
    'inicio': _ref('InstanteEntrada'),
    'fin': _ref('InstanteEntrada'),
    'gestor_id': _UUID_NULL,
}

#: What a request body means, per endpoint. Everything else about an operation
#: is read from the application. An endpoint missing from this table fails
#: ``tests/test_openapi.py``, which is what keeps the two in step.
OPERACIONES = {
    # -- auth ---------------------------------------------------------
    'login': {
        'requestBody': _cuerpo(_obj(
            {'username': _STR, 'password': _STR, 'remember_me': _BOOL},
            ['username', 'password'],
        )),
        'exito': '200',
        'responses': {'200': _datos(_ref('Usuario'), 'Sesión iniciada.')},
    },
    'logout': {'exito': '200'},
    'me': {'responses': {'200': _datos(_ref('Usuario'))}},
    'list_roles': {'responses': {'200': _datos({'type': 'array', 'items': _obj({
        'codigo': _STR, 'nombre': _STR_NULL,
    })})}},

    # -- trips --------------------------------------------------------
    'list_trips': {
        'query': _QUERY_PAGINA + [
            {'name': 'estado', 'in': 'query', 'required': False, 'schema': _STR},
            {'name': 'buscar', 'in': 'query', 'required': False, 'schema': _STR},
        ],
        'responses': {'200': _pagina('Viaje', 'Solo los viajes que puede ver.')},
    },
    'create_trip': {
        'requestBody': _cuerpo(_obj(_VIAJE_ESCRIBIBLE, ['titulo'])),
        'responses': {'201': _datos(_ref('Viaje'), 'Viaje creado.')},
    },
    'get_trip': {'responses': {'200': _datos(_ref('Viaje'))}},
    'update_trip': {
        'requestBody': _cuerpo(_obj(_VIAJE_ESCRIBIBLE)),
        'exito': '200',
        'responses': {'200': _datos(_ref('Viaje'))},
    },
    'delete_trip': {'exito': '200'},

    # -- travellers and destinations ----------------------------------
    'list_travelers': {'responses': {'200': _lista('Viajero')}},
    'add_traveler': {
        'requestBody': _cuerpo(_obj({
            'user_id': _UUID,
            'rol_en_viaje': _STR_NULL,
            'desde': _ref('InstanteEntrada'),
            'hasta': _ref('InstanteEntrada'),
            'observaciones': _STR_NULL,
        }, ['user_id'])),
        'responses': {'201': _datos(_ref('Viajero'), 'Persona asignada.')},
    },
    'remove_traveler': {'exito': '200'},
    'proposed_traveler_window': {
        'responses': {'200': _datos(_obj({
            'desde_local': _STR_NULL, 'hasta_local': _STR_NULL,
            'origen': {'type': ['string', 'null'], 'enum': ['itinerario', 'viaje', None],
                       'description': 'De dónde sale la propuesta.'},
        }), 'Fechas que ofrecer al asignar a alguien.')},
    },
    'list_destinations': {'responses': {'200': _lista('Destino')}},
    'add_destination': {
        'requestBody': _cuerpo(_obj({
            'ciudad': _STR, 'pais_codigo': _STR_NULL, 'zona_horaria': _STR_NULL,
            'es_escala': _BOOL, 'orden': _INT_NULL,
            'inicio': _ref('InstanteEntrada'), 'fin': _ref('InstanteEntrada'),
            'observaciones': _STR_NULL,
        }, ['ciudad'])),
        'responses': {'201': _datos(_ref('Destino'), 'Destino añadido.')},
    },
    'remove_destination': {'exito': '200'},

    # -- itinerary ----------------------------------------------------
    'get_itinerary': {
        'responses': {'200': _datos(_obj({
            'entradas': {'type': 'array', 'items': {}},
            'conexiones': {'type': 'array', 'items': {}},
        }), 'Cronología consolidada, acotada a lo que puede ver quien pregunta.')},
    },
    'create_item': {
        'requestBody': _cuerpo(_obj(
            {'trip_traveler_id': _UUID_NULL, 'localizador': _STR_NULL},
            descripcion=(
                'Las columnas del elemento, más sus instantes. Cada instante '
                'va como {"local": ..., "tz": ...}; enviar un campo «_utc» se '
                'rechaza con 422, porque es derivado.'
            ),
        )),
        'responses': {'201': _datos({}, 'Elemento creado.')},
    },
    'update_item': {
        'requestBody': _cuerpo(_obj({}, descripcion='Solo los campos a cambiar.')),
        'exito': '200',
    },
    'delete_item': {'exito': '200'},

    # -- documents ----------------------------------------------------
    'list_documents': {'responses': {'200': _lista('Documento')}},
    'upload_document': {
        'requestBody': {
            'required': True,
            'content': {'multipart/form-data': {'schema': _obj({
                'file': {'type': 'string', 'format': 'binary'},
                'tipo': _STR_NULL,
                'trip_traveler_id': _UUID_NULL,
            }, ['file'])}},
        },
        'responses': {'201': _datos(_ref('Documento'), 'Documento aceptado y en proceso.')},
    },
    'get_document': {'responses': {'200': _datos(_ref('Documento'))}},
    'download_document': {
        'responses': {'200': {
            'description': 'El original, tal y como se subió.',
            'content': {'application/octet-stream': {
                'schema': {'type': 'string', 'format': 'binary'}}},
        }},
    },
    'delete_document': {'exito': '200'},
    'reprocess_document': {
        'requestBody': _cuerpo(_obj({'from_start': _BOOL}), requerido=False),
        'exito': '200',
    },
    'review_document': {
        'requestBody': _cuerpo(_obj({
            'accion': {**_STR, 'enum': ['aprobar', 'corregir', 'descartar']},
            'campos': {'type': ['object', 'null'], 'description':
                       'Correcciones, indexadas por servicio: '
                       '{"0": {"numero_vuelo": "VY7604"}}.'},
            'comentario': _STR_NULL,
        }, ['accion'])),
        'exito': '200',
    },

    # -- alerts -------------------------------------------------------
    'list_alerts': {'responses': {'200': _lista('Alerta')}},
    'recalculate_alerts': {'exito': '200'},
    'get_alert': {'responses': {'200': _datos(_ref('Alerta'))}},
    'update_alert': {
        'requestBody': _cuerpo(_obj({
            'estado': {**_STR, 'enum': ['aceptada', 'resuelta', 'descartada']},
            'comentario': _STR_NULL,
            'responsable_id': _UUID_NULL,
        }, ['estado'])),
        'exito': '200',
        'responses': {'200': _datos(_ref('Alerta'))},
    },
    'explain_alert': {
        'exito': '200',
        'responses': {'200': _datos(_obj({
            'explicacion': _STR, 'consecuencias': _STR_NULL,
            'acciones': {'type': 'array', 'items': _obj({
                'accion': _STR,
                'prioridad': {**_STR_NULL, 'enum': ['alta', 'media', 'baja', None]},
            })},
            'run_id': _STR,
        }))},
    },

    # -- assistant and research ---------------------------------------
    'ai_query': {
        'requestBody': _cuerpo(_obj({'pregunta': _STR}, ['pregunta'])),
        'exito': '200',
        'responses': {'200': _datos(_ref('RespuestaAsistente'))},
    },
    'ai_summary': {'exito': '200'},
    'research': {
        'requestBody': _cuerpo(_obj({
            'consulta': _STR, 'categoria': _STR_NULL,
        }, ['consulta'])),
        'exito': '200',
    },
    'list_advisories': {'exito': '200'},
    'generate_advisories': {'exito': '200'},
    'validate_advisory': {
        'requestBody': _cuerpo(_obj({
            'estado_validacion': _STR, 'comentario': _STR_NULL,
        }, ['estado_validacion'])),
        'exito': '200',
    },

    # -- users --------------------------------------------------------
    'list_users': {
        'query': _QUERY_PAGINA,
        'responses': {'200': _pagina('Usuario')},
    },
    'create_user': {
        'requestBody': _cuerpo(_obj({
            'username': _STR, 'email': _STR, 'nombre': _STR,
            'apellidos': _STR_NULL, 'password': _STR,
            'roles': {'type': 'array', 'items': _STR},
        }, ['username', 'email', 'nombre', 'password'])),
        'responses': {'201': _datos(_ref('Usuario'), 'Cuenta creada.')},
    },
    'get_user': {'responses': {'200': _datos(_ref('Usuario'))}},
    'update_user': {
        'requestBody': _cuerpo(_obj({
            'email': _STR_NULL, 'nombre': _STR_NULL, 'apellidos': _STR_NULL,
            'puesto': _STR_NULL, 'departamento': _STR_NULL,
            'estado': _STR_NULL, 'roles': {'type': 'array', 'items': _STR},
        })),
        'exito': '200',
        'responses': {'200': _datos(_ref('Usuario'))},
    },
    'delete_user': {'exito': '200'},
    'unlock_user': {'exito': '200'},

    # -- audit --------------------------------------------------------
    'list_audit_events': {
        'query': _QUERY_PAGINA + [
            {'name': 'accion', 'in': 'query', 'required': False, 'schema': _STR},
            {'name': 'actor_id', 'in': 'query', 'required': False, 'schema': _UUID},
        ],
        'responses': {'200': _pagina('Sobre', 'Eventos, del más reciente al más antiguo.')},
    },
    'openapi_spec': {
        'responses': {'200': _datos(
            {'type': 'object'}, 'Este mismo documento.',
        )},
    },
    'verify_audit_chain': {
        'exito': '200',
        'responses': {'200': _datos(_obj({
            'integra': _BOOL,
            'anomalias': {'type': 'array', 'items': _obj({
                'id': {'type': 'integer'}, 'motivo': _STR,
            })},
        }), 'Resultado de recorrer la cadena de hashes.')},
    },
}
