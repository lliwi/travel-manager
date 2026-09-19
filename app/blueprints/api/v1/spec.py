"""The API's own description.

Served from the application that implements it, so the contract a client reads
is the contract that deployment actually offers.
"""
from flask import jsonify
from flask_login import login_required

from app.blueprints.api.v1 import api_v1_bp
from app.services import openapi_service
from app.utils.decorators import authenticated_only


@api_v1_bp.route('/openapi.json', methods=['GET'])
@login_required
@authenticated_only
def openapi_spec():
    """The OpenAPI 3.1 description of this API.

    Authenticated: the surface of an internal application is not something to
    hand to whoever asks. Any account may read it -- it describes what exists,
    never what any particular caller may reach.
    """
    return jsonify(openapi_service.build_spec())
