"""Trips blueprint: trip listing, creation, detail and travellers."""
from flask import Blueprint

trips_bp = Blueprint('trips', __name__)

from app.blueprints.trips import routes  # noqa: E402,F401
