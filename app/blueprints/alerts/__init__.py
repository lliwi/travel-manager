"""Alerts blueprint: listing, detail and state management."""
from flask import Blueprint

alerts_bp = Blueprint('alerts', __name__)

from app.blueprints.alerts import routes  # noqa: E402,F401
