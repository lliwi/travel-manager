"""Travel Manager application factory.

Corporate travel management with AI-assisted document extraction, itinerary
consolidation, logistics alerting and destination security advisories.

See ``requerimiento_tecnico_gestor_viajes_ia.md`` for the full specification.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import config
from app.extensions import cache, csrf, db, limiter, login_manager, migrate, talisman
from app.utils.errors import AppError
from app.utils.logging import (
    CORRELATION_HEADER,
    NOISY_LIBRARIES,
    CorrelationIdFilter,
    RedactingFilter,
    build_formatter,
    new_correlation_id,
)

__version__ = '1.0.0'


def create_app(config_name=None):
    """Application factory.

    Args:
        config_name: One of 'development', 'testing', 'production'. Defaults to
            the FLASK_ENV environment variable, then 'production'.

    Returns:
        A configured Flask application instance.
    """
    if config_name is None:
        config_name = os.getenv('FLASK_ENV', 'production')

    app = Flask(__name__)
    app.config.from_object(config[config_name])
    config[config_name].init_app(app)

    _validate_runtime_configuration(app)

    # x_for=2: the request passes through an external proxy and our own Nginx,
    # so the client IP recorded in the audit trail must look two hops back.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=2, x_proto=1, x_host=1)

    configure_logging(app)
    # Registered before the extensions: Flask runs before_request handlers in
    # registration order, and a request rejected by CSRF must still carry a
    # correlation id so the failure can be found in the logs.
    register_request_hooks(app)
    initialize_extensions(app)
    register_blueprints(app)
    register_error_handlers(app)
    register_template_helpers(app)
    register_cli_commands(app)

    app.logger.info('Travel Manager %s started in %s mode', __version__, config_name)
    return app


def _validate_runtime_configuration(app):
    """Fail fast on configuration that is unsafe outside development.

    A production deployment running with the well-known development SECRET_KEY
    would let anyone forge session cookies and CSRF tokens, and a missing
    encryption key would silently leave AI provider credentials in plain text.
    """
    if app.config['DEBUG'] or app.config['TESTING']:
        return

    insecure = (None, '', 'dev-secret-key-change-in-production')
    if app.config.get('SECRET_KEY') in insecure:
        raise RuntimeError(
            'SECRET_KEY must be set to a strong, unique value in production. '
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    if not app.config.get('SECRETS_ENCRYPTION_KEY'):
        raise RuntimeError(
            'SECRETS_ENCRYPTION_KEY must be set in production; AI provider API keys '
            'and traveller document numbers are encrypted at rest with it. '
            'Generate one with: '
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )


def initialize_extensions(app):
    """Bind every Flask extension to the application."""
    db.init_app(app)
    migrate.init_app(app, db, directory='migrations')
    login_manager.init_app(app)
    csrf.init_app(app)
    cache.init_app(app)

    if app.config.get('RATELIMIT_ENABLED', True):
        limiter.init_app(app)

    if not app.config['DEBUG'] and not app.config['TESTING']:
        csp = {
            'default-src': "'self'",
            'script-src': ["'self'", 'https://cdn.jsdelivr.net'],
            'style-src': ["'self'", 'https://cdn.jsdelivr.net', "'unsafe-inline'"],
            'font-src': ["'self'", 'https://cdn.jsdelivr.net', 'data:'],
            'img-src': ["'self'", 'data:'],
            'connect-src': "'self'",
            'frame-ancestors': "'none'",
            'base-uri': "'self'",
            'form-action': "'self'",
            'object-src': "'none'",
        }
        talisman.init_app(
            app,
            content_security_policy=csp,
            force_https=True,
            # HSTS is asserted by Nginx, which terminates TLS and can therefore
            # also set it on responses it generates itself (a 502 while the app
            # is restarting). Setting it in both places just duplicates it.
            strict_transport_security=False,
            referrer_policy='strict-origin-when-cross-origin',
            session_cookie_secure=True,
        )

    # Import the model package so SQLAlchemy and Alembic see every table.
    with app.app_context():
        from app import models  # noqa: F401


def register_request_hooks(app):
    """Correlation id in, correlation id out, plus baseline security headers."""

    @app.before_request
    def _assign_correlation_id():
        incoming = request.headers.get(CORRELATION_HEADER, '')
        # Only trust an upstream id when it looks like one of ours; otherwise a
        # caller could inject newlines or unbounded text into every log line.
        if incoming and len(incoming) <= 64 and incoming.replace('-', '').isalnum():
            g.correlation_id = incoming
        else:
            g.correlation_id = new_correlation_id()

    @app.after_request
    def _emit_security_headers(response):
        response.headers[CORRELATION_HEADER] = getattr(g, 'correlation_id', '-')
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault(
            'Permissions-Policy', 'geolocation=(), microphone=(), camera=()'
        )
        return response

    @app.teardown_appcontext
    def _remove_session(exception=None):
        if exception is not None:
            db.session.rollback()
        db.session.remove()


def register_blueprints(app):
    """Register the Jinja surface and the versioned REST API."""
    from app.blueprints.admin import admin_bp
    from app.blueprints.advisories import advisories_bp
    from app.blueprints.alerts import alerts_bp
    from app.blueprints.api.v1 import api_v1_bp
    from app.blueprints.auth import auth_bp
    from app.blueprints.dashboard import dashboard_bp
    from app.blueprints.documents import documents_bp
    from app.blueprints.trips import trips_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
    app.register_blueprint(trips_bp, url_prefix='/trips')
    app.register_blueprint(documents_bp, url_prefix='/documents')
    app.register_blueprint(alerts_bp, url_prefix='/alerts')
    app.register_blueprint(advisories_bp, url_prefix='/advisories')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(api_v1_bp, url_prefix='/api/v1')

    @app.route('/')
    def index():
        """Send authenticated callers to their dashboard, everyone else to login."""
        from flask import redirect, url_for
        from flask_login import current_user

        if current_user.is_authenticated:
            return redirect(url_for('dashboard.index'))
        return redirect(url_for('auth.login'))

    @app.route('/healthz')
    @limiter.exempt
    @talisman(force_https=False)
    def healthz():
        """Liveness probe: the process is up. Never touches dependencies.

        Exempt from the HTTPS redirect: TLS is terminated at Nginx, and a probe
        that talks to the container directly has no forwarded-proto header. A
        302 here would make the container look unhealthy while it is fine.

        Exempt from rate limiting too, and for the same reason: the container
        healthcheck runs every 30 seconds, which is 120 requests an hour
        against a default allowance of 100. Left limited, the container starts
        reporting itself unhealthy after an hour of being perfectly fine.
        """
        return jsonify({'status': 'ok', 'version': __version__})

    @app.route('/readyz')
    @limiter.exempt
    @talisman(force_https=False)
    def readyz():
        """Readiness probe: the dependencies this process needs are reachable.

        Exempt from the HTTPS redirect and from rate limiting, for the same
        reasons as ``/healthz``.
        """
        from sqlalchemy import text

        checks = {}
        healthy = True

        try:
            db.session.execute(text('SELECT 1'))
            checks['database'] = 'ok'
        except Exception as exc:
            checks['database'] = 'error'
            healthy = False
            app.logger.warning('Readiness check failed for database: %s', exc)

        try:
            import redis

            redis.Redis.from_url(app.config['REDIS_URL'], socket_timeout=2).ping()
            checks['redis'] = 'ok'
        except Exception as exc:
            checks['redis'] = 'error'
            healthy = False
            app.logger.warning('Readiness check failed for redis: %s', exc)

        return jsonify({
            'status': 'ok' if healthy else 'degraded',
            'checks': checks,
            'version': __version__,
        }), (200 if healthy else 503)


def register_error_handlers(app):
    """Render failures as HTML for the UI and as JSON for the API.

    The ``/api/v1`` blueprint installs its own AppError handler, which takes
    precedence for API routes; this pair covers the Jinja surface and anything
    that escapes a blueprint.
    """

    def _wants_json():
        return request.path.startswith('/api/') or (
            request.accept_mimetypes.best == 'application/json'
        )

    def _json_error(codigo, mensaje, status, detalles=None):
        payload = {'error': {'codigo': codigo, 'mensaje': mensaje}}
        if detalles:
            payload['error']['detalles'] = detalles
        payload['request_id'] = getattr(g, 'correlation_id', None)
        return jsonify(payload), status

    @app.errorhandler(AppError)
    def _handle_app_error(error):
        if error.status >= 500:
            app.logger.error('Application error: %s', error.mensaje, exc_info=error)
        if _wants_json():
            return _json_error(error.codigo, error.mensaje, error.status, error.detalles)

        from flask import flash, redirect, url_for
        from flask_login import current_user

        template = f'errors/{error.status}.html'
        if error.status == 401 and not current_user.is_authenticated:
            flash(error.mensaje, 'warning')
            return redirect(url_for('auth.login', next=request.path))
        if error.status in (403, 404, 413, 422, 429):
            try:
                return render_template(template, error=error), error.status
            except Exception:
                # No dedicated page for this status; fall through to the
                # generic one rather than turning a 403 into a 500.
                app.logger.debug('Sin plantilla para %s', template)
        return render_template('errors/500.html', error=error), error.status

    @app.errorhandler(HTTPException)
    def _handle_http_exception(error):
        if _wants_json():
            return _json_error(
                error.name.lower().replace(' ', '_'), error.description, error.code
            )
        try:
            return render_template(f'errors/{error.code}.html', error=error), error.code
        except Exception:
            return render_template('errors/500.html', error=error), error.code

    @app.errorhandler(Exception)
    def _handle_unexpected(error):
        db.session.rollback()
        app.logger.exception('Unhandled exception: %s', error)
        if _wants_json():
            return _json_error(
                'internal_error', 'Se ha producido un error inesperado.', 500
            )
        return render_template('errors/500.html', error=None), 500


def register_template_helpers(app):
    """Expose enum labels, feature flags and formatting helpers to Jinja."""
    from app.models import enums
    from app.utils import timeutil

    @app.context_processor
    def _inject_globals():
        return {
            'app_name': app.config['APP_NAME'],
            'app_version': __version__,
            'enums': enums,
            'label': enums.label,
            'feature_flags': _flags(),
            'notificaciones_sin_leer': _sin_leer(),
        }

    def _flags():
        """What the interface shows, decided in the panel.

        These were read from the environment while the switches that turn them
        on lived in Ajustes, so turning costs on there did nothing to the
        screens. The environment is the bootstrap value until the row exists,
        as everywhere else.
        """
        try:
            from app.services import settings_service

            return {
                'costs': settings_service.get_bool(
                    'COSTES_HABILITADOS', app.config['COSTS_ENABLED'],
                ),
                'traveler_documents': settings_service.get_bool(
                    'DOCUMENTOS_VIAJERO_HABILITADOS',
                    app.config['TRAVELER_DOCUMENTS_ENABLED'],
                ),
                'web_research': settings_service.get_bool(
                    'INVESTIGACION_WEB_HABILITADA', app.config['WEB_RESEARCH_ENABLED'],
                ),
            }
        except Exception:
            return {
                'costs': app.config['COSTS_ENABLED'],
                'traveler_documents': app.config['TRAVELER_DOCUMENTS_ENABLED'],
                'web_research': app.config['WEB_RESEARCH_ENABLED'],
            }

    def _sin_leer():
        """The badge on the bell, on every page.

        Swallows its own failures: a count that cannot be read is a reason to
        show no badge, never a reason for the page around it not to render.
        """
        from flask_login import current_user

        if not getattr(current_user, 'is_authenticated', False):
            return 0
        try:
            from app.services import notification_service

            return notification_service.contar_sin_leer(
                current_user._get_current_object()
            )
        except Exception:
            return 0

    app.jinja_env.filters['local_dt'] = timeutil.format_local
    app.jinja_env.filters['local_date'] = timeutil.format_local_date
    app.jinja_env.filters['local_time'] = timeutil.format_local_time
    app.jinja_env.filters['duration'] = timeutil.format_duration


def register_cli_commands(app):
    """Register the operational ``flask`` commands."""
    from app.cli import register_commands

    register_commands(app)


def configure_logging(app):
    """Structured logging to stdout (containers) or a rotating file."""
    formatter = build_formatter(app.config['LOG_JSON'])
    correlation_filter = CorrelationIdFilter()
    redacting_filter = RedactingFilter()

    if app.config['LOG_TO_STDOUT']:
        handler = logging.StreamHandler(sys.stdout)
    else:
        os.makedirs(app.config['LOG_DIR'], exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(app.config['LOG_DIR'], 'travel-manager.log'),
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
        )

    handler.setFormatter(formatter)
    handler.addFilter(correlation_filter)
    handler.addFilter(redacting_filter)

    level = getattr(logging, app.config['LOG_LEVEL'].upper(), logging.INFO)

    app.logger.handlers.clear()
    app.logger.addHandler(handler)
    app.logger.setLevel(level)
    app.logger.propagate = False

    for name in ('app', 'celery', 'sqlalchemy.engine'):
        target = logging.getLogger(name)
        target.handlers.clear()
        target.addHandler(handler)
        target.setLevel(level if name != 'sqlalchemy.engine' else logging.WARNING)
        target.propagate = False

    # Third-party libraries stay at WARNING whatever the configured level.
    # Otherwise running at DEBUG buries every message this application emits --
    # and botocore prints the Authorization header of each S3 request, which
    # puts signing material in the log for no benefit at all.
    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
