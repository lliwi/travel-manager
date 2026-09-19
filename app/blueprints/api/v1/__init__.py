"""Versioned REST API (specification sections 3.1 and 7).

The API shares the session, the CSRF protection and -- critically -- the exact
same ``authorization_service`` decisions as the Jinja surface. The only thing
that differs is how an error is rendered, which is the handler below.
"""
from flask import Blueprint, g, jsonify

from app.utils.errors import AppError

api_v1_bp = Blueprint('api_v1', __name__)


@api_v1_bp.errorhandler(AppError)
def handle_app_error(error):
    """Render any application error as the standard JSON envelope.

    Registered on the blueprint, so it takes precedence over the application
    handler for every ``/api/v1`` route.
    """
    payload = {'error': error.to_dict(), 'request_id': getattr(g, 'correlation_id', None)}
    return jsonify(payload), error.status


@api_v1_bp.errorhandler(404)
def handle_not_found(error):
    return jsonify({
        'error': {'codigo': 'not_found', 'mensaje': 'El recurso solicitado no existe.'},
        'request_id': getattr(g, 'correlation_id', None),
    }), 404


@api_v1_bp.errorhandler(405)
def handle_method_not_allowed(error):
    return jsonify({
        'error': {'codigo': 'method_not_allowed', 'mensaje': 'Método no permitido.'},
        'request_id': getattr(g, 'correlation_id', None),
    }), 405


def ok(data=None, status=200, **extra):
    """Standard success envelope."""
    payload = {'data': data}
    payload.update(extra)
    payload['request_id'] = getattr(g, 'correlation_id', None)
    return jsonify(payload), status


def paginated(pagination, serializer):
    """Standard paginated envelope."""
    return ok(
        [serializer(item) for item in pagination.items],
        meta={
            'pagina': pagination.page,
            'por_pagina': pagination.per_page,
            'total': pagination.total,
            'paginas': pagination.pages,
            'tiene_siguiente': pagination.has_next,
            'tiene_anterior': pagination.has_prev,
        },
    )


from app.blueprints.api.v1 import (  # noqa: E402,F401
    ai,
    alerts,
    audit,
    auth,
    documents,
    itinerary,
    spec,
    trips,
    users,
)
