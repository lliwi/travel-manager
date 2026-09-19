"""Documents blueprint: upload, processing status, review and download."""
from flask import Blueprint

documents_bp = Blueprint('documents', __name__)

from app.blueprints.documents import routes  # noqa: E402,F401
