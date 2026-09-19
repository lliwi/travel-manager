"""Security advisories blueprint: generation and manager validation."""
from flask import Blueprint

advisories_bp = Blueprint('advisories', __name__)

from app.blueprints.advisories import routes  # noqa: E402,F401
