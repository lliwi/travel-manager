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


def _extraction_schema(campos):
    """Build an extraction schema around a field specification.

    A document describes one *or more* services: a return booking is a single
    confirmation email covering two flights, and a hotel confirmation can cover
    two rooms with different dates. Forcing one service per document meant
    silently dropping everything after the first.

    So the payload is a list. Confidence and provenance live inside each
    service, because they are per-value facts and a flat map could not say
    which flight a locator belonged to.
    """
    servicio = {
        'type': 'object',
        'properties': {
            'campos': {
                'type': 'object',
                'properties': campos,
                'additionalProperties': True,
            },
            'confianzas': _CONFIANZAS,
            'procedencias': _PROCEDENCIAS,
        },
        'required': ['campos'],
    }

    return {
        'type': 'object',
        'properties': {
            'servicios': {
                'type': 'array',
                'items': servicio,
                'minItems': 1,
                'description': (
                    'Un elemento por cada servicio que describa el documento: '
                    'cada vuelo de un billete de ida y vuelta, cada habitación '
                    'de una reserva de hotel.'
                ),
            },
            'confianza_global': {'type': 'number', 'minimum': 0, 'maximum': 1},
            'avisos': _AVISOS,
        },
        'required': ['servicios'],
    }


#: The field specification of each extraction schema, kept accessible so the
#: decoding schema can be built from the fields alone.
def campos_de(esquema):
    """The field properties an extraction schema expects, or None."""
    servicios = (esquema.get('properties') or {}).get('servicios')
    if not isinstance(servicios, dict):
        return None
    campos = ((servicios.get('items') or {}).get('properties') or {}).get('campos')
    return (campos or {}).get('properties')


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

#: A booking covers people, and each of them has their own seat on each leg.
#: One «pasajero» and one «asiento» per service could hold one of the three
#: names the confirmation listed and one of the six seats, so the model left
#: both empty rather than choose -- the data was in the document all along and
#: had nowhere to go.
_PASAJEROS = {
    'type': ['array', 'null'],
    'description': (
        'Personas de la reserva, con su asiento EN ESTE trayecto. Una reserva '
        'con varias personas lista un asiento por persona y por trayecto.'
    ),
    'items': {
        'type': 'object',
        'properties': {
            'nombre': _STR,
            'asiento': _STR,
            'equipaje': dict(_STR, description='Tal y como lo describa el documento.'),
        },
    },
}

EXTRACT_VUELO = _extraction_schema({
    'localizador': _STR,
    'aerolinea': _STR,
    'numero_vuelo': _STR,
    'operado_por': _STR,
    'pasajero': dict(_STR, description='Si la reserva es de una sola persona.'),
    'pasajeros': _PASAJEROS,
    'clase': _STR,
    'asiento': dict(_STR, description='Si la reserva es de una sola persona.'),
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
    'pasajero': dict(_STR, description='Si la reserva es de una sola persona.'),
    'pasajeros': _PASAJEROS,
    'clase': _STR,
    'coche': _STR,
    'asiento': dict(_STR, description='Si la reserva es de una sola persona.'),
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
    # Instants, not strings: the validity window is what the expiry rule
    # compares against the trip's dates, and a bare string cannot be compared
    # with anything.
    'fecha_emision': _INSTANTE,
    'fecha_caducidad': _INSTANTE,
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


def esquema_de_generacion(esquema):
    """What the model is actually asked to produce.

    Two reductions from the validation schema. The envelope of confidences and
    provenance is dropped: a small model's self-reported confidence is a guess,
    while whether a value appears literally in the document is something we
    check ourselves. And the nesting goes, because the grammar for the full
    schema takes longer to apply than the answer is worth -- measured on a real
    booking email, the difference between an answer in seconds and one that
    does not arrive.

    What remains is a list of services, each a flat map of fields. This is the
    one shape the model ever sees: the prompt shows it and constrained decoding
    enforces it. Showing one shape while enforcing another is worse than either
    alone -- the model plans the answer it was shown, the grammar admits only
    the other, and the fields it never planned get filled with invention.
    """
    if not isinstance(esquema, dict):
        return None

    campos = campos_de(esquema)
    if not campos:
        return _podar(esquema)

    propiedades = _podar(campos)
    return {
        'type': 'object',
        'properties': {
            'servicios': {
                'type': 'array',
                'minItems': 1,
                'items': {
                    'type': 'object',
                    'properties': propiedades,
                    # Every field required, nulls allowed: a model that must
                    # emit the key says "null" for what it cannot find, and an
                    # omitted field is indistinguishable from one it never
                    # looked for.
                    'required': sorted(propiedades),
                },
            },
        },
        'required': ['servicios'],
    }


def _podar(node):
    """Remove what constrained decoding cannot express.

    Anything dropped here is still checked by the validator once the answer is
    back, so nothing is lost; leaving it in risks the provider rejecting the
    schema outright and falling back to unconstrained generation.
    """
    if isinstance(node, dict):
        limpio = {}
        for clave, valor in node.items():
            if clave in ('additionalProperties', 'description'):
                continue
            limpio[clave] = _podar(valor)

        if limpio.get('type') == 'object' and not limpio.get('properties'):
            limpio.pop('required', None)
        return limpio

    if isinstance(node, list):
        return [_podar(v) for v in node]

    return node
