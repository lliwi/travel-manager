"""JSON schemas constraining every model answer.

Requiring a schema is the second half of the prompt-injection defence: an
injected "ignore your instructions" has nowhere to land when the only accepted
output is an object with these fields, and an answer that fails validation is
rejected as a contract error rather than quietly written to the database.

Each extraction schema carries its fields alongside a parallel ``confianzas``
map and a ``procedencias`` map, because specification section 2.3 requires the
origin, the confidence and the page for every extracted value.
"""

#: Reusable fragment: per-field confidence between 0 and 1.
_CONFIANZAS = {
    'type': 'object',
    'description': 'Confianza entre 0 y 1 para cada campo extraído.',
    'additionalProperties': {'type': 'number', 'minimum': 0, 'maximum': 1},
}

#: Reusable fragment: where each value came from in the document.
_PROCEDENCIAS = {
    'type': 'object',
    'description': (
        'Para cada campo, la página y el fragmento literal que lo respalda. '
        'Deja «fragmento» a null si el valor se infirió y no aparece literal.'
    ),
    'additionalProperties': {
        'type': 'object',
        'properties': {
            'pagina': {'type': ['integer', 'null']},
            'fragmento': {'type': ['string', 'null']},
        },
    },
}

_AVISOS = {
    'type': 'array',
    'description': 'Problemas encontrados: datos ambiguos, ilegibles o contradictorios.',
    'items': {'type': 'string'},
}


def _extraction_schema(campos, required=('campos', 'confianzas')):
    """Build an extraction schema around a field specification."""
    return {
        'type': 'object',
        'properties': {
            'campos': {
                'type': 'object',
                'properties': campos,
                'additionalProperties': True,
            },
            'confianzas': _CONFIANZAS,
            'procedencias': _PROCEDENCIAS,
            'confianza_global': {'type': 'number', 'minimum': 0, 'maximum': 1},
            'avisos': _AVISOS,
        },
        'required': list(required),
    }


_STR = {'type': ['string', 'null']}
_NUM = {'type': ['number', 'null']}
_INT = {'type': ['integer', 'null']}

#: A local wall-clock instant plus its IANA zone. Never a bare datetime: the
#: alert engine cannot compare instants without knowing the zone.
_INSTANTE = {
    'type': 'object',
    'properties': {
        'local': {
            'type': ['string', 'null'],
            'description': 'AAAA-MM-DDTHH:MM en hora local, tal como figura en el documento.',
        },
        'zona_horaria': {
            'type': ['string', 'null'],
            'description': 'Nombre IANA, por ejemplo Europe/Madrid. null si no se deduce.',
        },
    },
}

EXTRACT_VUELO = _extraction_schema({
    'localizador': _STR,
    'aerolinea': _STR,
    'numero_vuelo': _STR,
    'operado_por': _STR,
    'pasajero': _STR,
    'clase': _STR,
    'asiento': _STR,
    'origen_codigo': dict(_STR, description='Código IATA de tres letras.'),
    'origen_nombre': _STR,
    'origen_ciudad': _STR,
    'terminal_origen': _STR,
    'salida': _INSTANTE,
    'destino_codigo': dict(_STR, description='Código IATA de tres letras.'),
    'destino_nombre': _STR,
    'destino_ciudad': _STR,
    'terminal_destino': _STR,
    'llegada': _INSTANTE,
    'importe': _NUM,
    'moneda': _STR,
})

EXTRACT_TREN = _extraction_schema({
    'localizador': _STR,
    'operador': _STR,
    'numero_tren': _STR,
    'pasajero': _STR,
    'clase': _STR,
    'coche': _STR,
    'asiento': _STR,
    'origen_nombre': _STR,
    'origen_ciudad': _STR,
    'salida': _INSTANTE,
    'destino_nombre': _STR,
    'destino_ciudad': _STR,
    'llegada': _INSTANTE,
    'importe': _NUM,
    'moneda': _STR,
})

EXTRACT_HOTEL = _extraction_schema({
    'localizador': _STR,
    'nombre': _STR,
    'cadena': _STR,
    'direccion': _STR,
    'ciudad': _STR,
    'pais': dict(_STR, description='Código ISO de dos letras.'),
    'telefono': _STR,
    'huesped': _STR,
    'check_in': _INSTANTE,
    'check_out': _INSTANTE,
    'noches': _INT,
    'numero_habitaciones': _INT,
    'tipo_habitacion': _STR,
    'regimen': _STR,
    'importe': _NUM,
    'moneda': _STR,
})

EXTRACT_VEHICULO = _extraction_schema({
    'localizador': _STR,
    'proveedor': _STR,
    'categoria': _STR,
    'modelo': _STR,
    'transmision': _STR,
    'conductor': _STR,
    'recogida_lugar': _STR,
    'recogida_ciudad': _STR,
    'recogida_pais': _STR,
    'recogida': _INSTANTE,
    'devolucion_lugar': _STR,
    'devolucion_ciudad': _STR,
    'devolucion_pais': _STR,
    'devolucion': _INSTANTE,
    'franquicia': _NUM,
    'importe': _NUM,
    'moneda': _STR,
})

EXTRACT_SEGURO = _extraction_schema({
    'localizador': _STR,
    'aseguradora': _STR,
    'numero_poliza': _STR,
    'asegurado': _STR,
    'cobertura': _STR,
    'telefono_asistencia': _STR,
    'inicio': _INSTANTE,
    'fin': _INSTANTE,
    'importe': _NUM,
    'moneda': _STR,
})

EXTRACT_VISADO = _extraction_schema({
    'numero': _STR,
    'tipo': _STR,
    'pais_emisor': _STR,
    'titular': _STR,
    'entradas': _STR,
    'fecha_emision': _STR,
    'fecha_caducidad': _STR,
    'estancia_maxima_dias': _INT,
})

EXTRACT_OTRO = _extraction_schema({
    'localizador': _STR,
    'tipo_servicio': _STR,
    'nombre': _STR,
    'proveedor': _STR,
    'lugar': _STR,
    'ciudad': _STR,
    'pais': _STR,
    'inicio': _INSTANTE,
    'fin': _INSTANTE,
    'importe': _NUM,
    'moneda': _STR,
})

CLASSIFY_DOCUMENT = {
    'type': 'object',
    'properties': {
        'clasificacion': {
            'type': 'string',
            'enum': ['vuelo', 'tren', 'hotel', 'vehiculo', 'seguro', 'visado', 'otro'],
        },
        'confianza': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'justificacion': {'type': 'string'},
    },
    'required': ['clasificacion', 'confianza'],
}

ANSWER_TRIP_QUESTION = {
    'type': 'object',
    'properties': {
        'respuesta': {'type': 'string'},
        'referencias': {
            'type': 'array',
            'description': (
                'Identificadores de los elementos del viaje usados para responder.'
            ),
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string'},
                    'tipo': {'type': 'string'},
                    'descripcion': {'type': 'string'},
                },
                'required': ['id'],
            },
        },
        'confianza': {'type': 'number', 'minimum': 0, 'maximum': 1},
        'datos_insuficientes': {'type': 'boolean'},
    },
    'required': ['respuesta'],
}

SUMMARIZE_TRIP = {
    'type': 'object',
    'properties': {
        'resumen': {'type': 'string'},
        'itinerario': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'fecha': {'type': 'string'},
                    'hora': {'type': ['string', 'null']},
                    'zona_horaria': {'type': ['string', 'null']},
                    'descripcion': {'type': 'string'},
                },
                'required': ['descripcion'],
            },
        },
        'puntos_atencion': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['resumen'],
}

ANALYZE_RISKS = {
    'type': 'object',
    'properties': {
        'nivel_global': {
            'type': 'string',
            'enum': ['normal', 'precaucion', 'alto_riesgo', 'desaconsejado'],
        },
        'riesgos': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'categoria': {
                        'type': 'string',
                        'enum': ['seguridad', 'sanidad', 'entrada', 'transporte',
                                 'conectividad', 'meteorologia', 'otro'],
                    },
                    'nivel': {
                        'type': 'string',
                        'enum': ['normal', 'precaucion', 'alto_riesgo', 'desaconsejado'],
                    },
                    'titulo': {'type': 'string'},
                    'descripcion': {'type': 'string'},
                    'fuente_url': {'type': ['string', 'null']},
                    'confianza': {'type': 'number', 'minimum': 0, 'maximum': 1},
                },
                'required': ['titulo', 'descripcion', 'nivel'],
            },
        },
    },
    'required': ['riesgos'],
}

RESEARCH_PUBLIC_INFO = {
    'type': 'object',
    'properties': {
        'resumen': {'type': 'string'},
        'hallazgos': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'afirmacion': {'type': 'string'},
                    'fuente_url': {'type': ['string', 'null']},
                },
                'required': ['afirmacion'],
            },
        },
        'fuentes_no_responden': {'type': 'boolean'},
    },
    'required': ['resumen'],
}

EXPLAIN_ALERT = {
    'type': 'object',
    'properties': {
        'explicacion': {'type': 'string'},
        'consecuencias': {'type': 'string'},
        'acciones': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'accion': {'type': 'string'},
                    'prioridad': {'type': 'string', 'enum': ['alta', 'media', 'baja']},
                },
                'required': ['accion'],
            },
        },
    },
    'required': ['explicacion'],
}

GENERATE_ADVISORY = {
    'type': 'object',
    'properties': {
        'recomendaciones': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'categoria': {
                        'type': 'string',
                        'enum': ['seguridad', 'sanidad', 'entrada', 'transporte',
                                 'conectividad', 'meteorologia', 'otro'],
                    },
                    'nivel': {
                        'type': 'string',
                        'enum': ['normal', 'precaucion', 'alto_riesgo', 'desaconsejado'],
                    },
                    'titulo': {'type': 'string'},
                    'contenido': {'type': 'string'},
                    'acciones': {'type': 'array', 'items': {'type': 'string'}},
                    'fuente_url': {'type': ['string', 'null']},
                    'confianza': {'type': 'number', 'minimum': 0, 'maximum': 1},
                },
                'required': ['titulo', 'contenido', 'categoria', 'nivel'],
            },
        },
        'fuentes_insuficientes': {'type': 'boolean'},
    },
    'required': ['recomendaciones'],
}

SCHEMAS = {
    'extract_vuelo': EXTRACT_VUELO,
    'extract_tren': EXTRACT_TREN,
    'extract_hotel': EXTRACT_HOTEL,
    'extract_vehiculo': EXTRACT_VEHICULO,
    'extract_seguro': EXTRACT_SEGURO,
    'extract_visado': EXTRACT_VISADO,
    'extract_otro': EXTRACT_OTRO,
    'extract_desconocido': EXTRACT_OTRO,
    'classify_document': CLASSIFY_DOCUMENT,
    'answer_trip_question': ANSWER_TRIP_QUESTION,
    'summarize_trip': SUMMARIZE_TRIP,
    'analyze_risks': ANALYZE_RISKS,
    'research_public_info': RESEARCH_PUBLIC_INFO,
    'explain_alert': EXPLAIN_ALERT,
    'generate_advisory': GENERATE_ADVISORY,
}

__all__ = ['SCHEMAS']
