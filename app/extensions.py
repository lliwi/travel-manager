"""Flask extension singletons.

Kept free of application state so they can be imported from anywhere without
triggering circular imports. They are bound to the app inside
``app.initialize_extensions``.
"""
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import MetaData

# Explicit naming convention so Alembic autogenerate produces stable, readable
# constraint names instead of database-assigned ones.
NAMING_CONVENTION = {
    'ix': 'ix_%(column_0_label)s',
    'uq': 'uq_%(table_name)s_%(column_0_name)s',
    'ck': 'ck_%(table_name)s_%(constraint_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
    'pk': 'pk_%(table_name)s',
}

db = SQLAlchemy(metadata=MetaData(naming_convention=NAMING_CONVENTION))
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
cache = Cache()
talisman = Talisman()

login_manager.login_view = 'auth.login'
login_manager.login_message = 'Por favor, inicie sesión para acceder a esta página.'
login_manager.login_message_category = 'info'
login_manager.session_protection = 'strong'


@login_manager.user_loader
def load_user(user_id):
    """Load a user by its UUID primary key.

    The business key is the immutable UUID, never the login name -- see
    specification section 3.3.
    """
    import uuid

    from app.models.user import User

    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError):
        return None

    user = db.session.get(User, uid)
    if user is None or user.is_deleted or not user.is_active_account:
        return None
    return user


@login_manager.unauthorized_handler
def handle_unauthorized():
    """Render 401 as JSON for the API surface and as a redirect for the UI."""
    from flask import flash, jsonify, redirect, request, url_for

    if request.path.startswith('/api/'):
        return jsonify({
            'error': {
                'codigo': 'unauthenticated',
                'mensaje': 'Autenticación requerida.',
            }
        }), 401

    flash(login_manager.login_message, login_manager.login_message_category)
    return redirect(url_for('auth.login', next=request.full_path))
