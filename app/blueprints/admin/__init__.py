"""Administration blueprint: users, roles, AI providers, settings and audit."""
from flask import Blueprint

admin_bp = Blueprint('admin', __name__)

from app.blueprints.admin import routes  # noqa: E402,F401
