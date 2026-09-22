"""Application configuration classes.

Everything is read from the environment so the same image runs in every
environment. Values that an administrator must be able to change at runtime
(alert thresholds, AI provider bindings, retention windows) do NOT live here --
they live in the ``system_settings`` table, see ``app.services.settings_service``.
"""
import os
from datetime import timedelta


def _bool(name, default=False):
    """Read a boolean environment variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _int(name, default):
    """Read an integer environment variable, falling back on malformed input."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    """Base configuration shared by every environment."""

    # ------------------------------------------------------------------
    # Flask core
    # ------------------------------------------------------------------
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    APP_NAME = 'Travel Manager'
    APP_PUBLIC_URL = os.environ.get('APP_PUBLIC_URL', 'http://localhost')
    DEFAULT_TIMEZONE = os.environ.get('DEFAULT_TIMEZONE', 'Europe/Madrid')
    DEFAULT_LOCALE = os.environ.get('DEFAULT_LOCALE', 'es')

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        'postgresql://travel:travel@postgres:5432/travel_manager',
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    _base_engine_options = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'pool_size': 10,
        'max_overflow': 20,
    }
    DATABASE_SSL_MODE = os.environ.get('DATABASE_SSL_MODE')
    if DATABASE_SSL_MODE:
        _base_engine_options['connect_args'] = {'sslmode': DATABASE_SSL_MODE}
    SQLALCHEMY_ENGINE_OPTIONS = _base_engine_options

    # ------------------------------------------------------------------
    # How long a web request may take
    #
    # The same number Gunicorn is started with, so the application knows the
    # ceiling it is working against and can cut its outbound calls before the
    # server cuts the worker. Two sources for one deadline is how a request
    # ends up killed mid-flight with nothing recorded: see
    # ``utils/http.presupuesto_restante``. Raise both together or neither.
    # ------------------------------------------------------------------
    WEB_REQUEST_TIMEOUT = _int('WEB_REQUEST_TIMEOUT', 180)

    # ------------------------------------------------------------------
    # Session & CSRF
    # ------------------------------------------------------------------
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=_int('SESSION_LIFETIME_HOURS', 8))
    WTF_CSRF_TIME_LIMIT = None

    # ------------------------------------------------------------------
    # Passwords (Argon2id -- specification section 3.2)
    # ------------------------------------------------------------------
    ARGON2_TIME_COST = _int('ARGON2_TIME_COST', 3)
    ARGON2_MEMORY_COST = _int('ARGON2_MEMORY_COST', 65536)   # 64 MiB
    ARGON2_PARALLELISM = _int('ARGON2_PARALLELISM', 4)
    ARGON2_HASH_LEN = 32
    ARGON2_SALT_LEN = 16
    LOGIN_MAX_ATTEMPTS = _int('LOGIN_MAX_ATTEMPTS', 5)
    LOGIN_LOCKOUT_MINUTES = _int('LOGIN_LOCKOUT_MINUTES', 15)

    # ------------------------------------------------------------------
    # Identity provider (section 3.3: local now, LDAP/AD later)
    # ------------------------------------------------------------------
    IDENTITY_PROVIDER = os.environ.get('IDENTITY_PROVIDER', 'local')
    IDENTITY_JIT_PROVISIONING = _bool('IDENTITY_JIT_PROVISIONING', False)

    # ------------------------------------------------------------------
    # Encryption of secrets at rest (AI API keys, traveller document numbers)
    # ------------------------------------------------------------------
    SECRETS_ENCRYPTION_KEY = os.environ.get('SECRETS_ENCRYPTION_KEY')
    SECRETS_ENCRYPTION_KEY_PREVIOUS = os.environ.get('SECRETS_ENCRYPTION_KEY_PREVIOUS')

    # ------------------------------------------------------------------
    # Rate limiting & caching
    # ------------------------------------------------------------------
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/0')
    RATELIMIT_STORAGE_URI = os.environ.get('RATELIMIT_STORAGE_URI') or REDIS_URL
    RATELIMIT_DEFAULT = os.environ.get('RATELIMIT_DEFAULT', '400 per day, 100 per hour')
    RATELIMIT_HEADERS_ENABLED = True
    CACHE_TYPE = 'RedisCache'
    CACHE_REDIS_URL = REDIS_URL
    CACHE_DEFAULT_TIMEOUT = 300

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    #: Bearer token a Prometheus scraper must present. Optional: with none set,
    #: /metrics still refuses anything from outside the private network, which
    #: is what a single-host Compose deployment relies on. Set it when the
    #: scraper lives somewhere else.
    METRICS_TOKEN = os.environ.get('METRICS_TOKEN')

    # ------------------------------------------------------------------
    # Celery
    # ------------------------------------------------------------------
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://redis:6379/1')
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://redis:6379/2')
    CELERY_TIMEZONE = os.environ.get('CELERY_TIMEZONE', 'UTC')
    # When true, queued work runs inline in the calling process instead of
    # reaching the broker. Used by the test suite, which must never depend
    # on Redis being up.
    CELERY_TASK_ALWAYS_EAGER = _bool('CELERY_TASK_ALWAYS_EAGER', False)

    # ------------------------------------------------------------------
    # Document storage (S3 compatible / MinIO -- section 3.1)
    # ------------------------------------------------------------------
    STORAGE_BACKEND = os.environ.get('STORAGE_BACKEND', 's3')
    S3_ENDPOINT_URL = os.environ.get('S3_ENDPOINT_URL', 'http://minio:9000')
    S3_PUBLIC_ENDPOINT_URL = os.environ.get('S3_PUBLIC_ENDPOINT_URL')
    S3_ACCESS_KEY = os.environ.get('S3_ACCESS_KEY', 'minioadmin')
    S3_SECRET_KEY = os.environ.get('S3_SECRET_KEY', 'minioadmin')
    S3_REGION = os.environ.get('S3_REGION', 'us-east-1')
    S3_BUCKET_DOCUMENTS = os.environ.get('S3_BUCKET_DOCUMENTS', 'travel-documents')
    S3_BUCKET_QUARANTINE = os.environ.get('S3_BUCKET_QUARANTINE', 'travel-quarantine')
    S3_USE_SSL = _bool('S3_USE_SSL', False)
    STORAGE_PRESIGNED_TTL = _int('STORAGE_PRESIGNED_TTL', 60)
    STORAGE_STREAM_THROUGH_APP = _bool('STORAGE_STREAM_THROUGH_APP', True)

    # ------------------------------------------------------------------
    # Upload limits & allow-list (section 3.2)
    # ------------------------------------------------------------------
    MAX_CONTENT_LENGTH = _int('MAX_DOCUMENT_BYTES', 32 * 1024 * 1024)
    MAX_DOCUMENT_BYTES = MAX_CONTENT_LENGTH
    ALLOWED_DOCUMENT_EXTENSIONS = tuple(
        e.strip().lower()
        for e in os.environ.get(
            'ALLOWED_DOCUMENT_EXTENSIONS', 'pdf,jpg,jpeg,png,tiff,tif,eml'
        ).split(',')
        if e.strip()
    )
    ALLOWED_DOCUMENT_MIMETYPES = (
        'application/pdf',
        'image/jpeg',
        'image/png',
        'image/tiff',
        'message/rfc822',
    )

    # ------------------------------------------------------------------
    # Antivirus (ClamAV -- section 6)
    # ------------------------------------------------------------------
    ANTIVIRUS_BACKEND = os.environ.get('ANTIVIRUS_BACKEND', 'clamav')
    CLAMAV_HOST = os.environ.get('CLAMAV_HOST', 'clamav')
    CLAMAV_PORT = _int('CLAMAV_PORT', 3310)
    CLAMAV_TIMEOUT = _int('CLAMAV_TIMEOUT', 120)

    # ------------------------------------------------------------------
    # OCR (section 6)
    # ------------------------------------------------------------------
    OCR_BACKEND = os.environ.get('OCR_BACKEND', 'tesseract')
    OCR_LANGUAGES = os.environ.get('OCR_LANGUAGES', 'spa+eng')
    OCR_DPI = _int('OCR_DPI', 300)
    OCR_MIN_CHARS_PER_PAGE = _int('OCR_MIN_CHARS_PER_PAGE', 80)

    # ------------------------------------------------------------------
    # AI layer (section 2.5)
    #
    # Providers, endpoints, models, keys and the per-task bindings are
    # configured in the database, from Administración → Proveedores de IA.
    # They are deliberately *not* environment variables: changing a model is an
    # administrative decision, and an API key in the environment is an API key
    # in plain text.
    #
    # What remains here is bootstrap only -- the values used to seed the first
    # provider so a fresh install answers before anyone has configured
    # anything -- plus the transport timeout.
    # ------------------------------------------------------------------
    AI_BOOTSTRAP_PROVIDER = os.environ.get('AI_BOOTSTRAP_PROVIDER', 'ollama')
    OLLAMA_BOOTSTRAP_URL = os.environ.get(
        'OLLAMA_BOOTSTRAP_URL', 'http://host.docker.internal:11434'
    )
    OLLAMA_BOOTSTRAP_MODEL = os.environ.get('OLLAMA_BOOTSTRAP_MODEL', 'llama3.1:8b')
    AI_REQUEST_TIMEOUT = _int('AI_REQUEST_TIMEOUT', 120)
    AI_MAX_RETRIES = _int('AI_MAX_RETRIES', 2)

    # ------------------------------------------------------------------
    # Web research (section 2.6) -- SSRF hardening
    # ------------------------------------------------------------------
    WEB_RESEARCH_ENABLED = _bool('WEB_RESEARCH_ENABLED', True)
    WEB_FETCH_TIMEOUT = _int('WEB_FETCH_TIMEOUT', 15)
    WEB_FETCH_MAX_BYTES = _int('WEB_FETCH_MAX_BYTES', 2 * 1024 * 1024)
    WEB_FETCH_CACHE_HOURS = _int('WEB_FETCH_CACHE_HOURS', 24)

    # ------------------------------------------------------------------
    # Feature flags (see the plan's "assumptions" section)
    # ------------------------------------------------------------------
    COSTS_ENABLED = _bool('COSTS_ENABLED', False)
    TRAVELER_DOCUMENTS_ENABLED = _bool('TRAVELER_DOCUMENTS_ENABLED', False)
    TRAVELER_DOWNLOAD_ORIGINALS = _bool('TRAVELER_DOWNLOAD_ORIGINALS', True)
    DOCUMENTS_AUTO_APPROVE = _bool('DOCUMENTS_AUTO_APPROVE', False)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_TO_STDOUT = _bool('LOG_TO_STDOUT', True)
    LOG_JSON = _bool('LOG_JSON', True)
    LOG_DIR = os.environ.get('LOG_DIR', os.path.join(os.getcwd(), 'logs'))

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------
    ITEMS_PER_PAGE = _int('ITEMS_PER_PAGE', 20)
    API_MAX_PAGE_SIZE = _int('API_MAX_PAGE_SIZE', 100)

    @staticmethod
    def init_app(app):
        """Hook for environment-specific initialisation."""


class DevelopmentConfig(Config):
    """Local development: verbose, no TLS requirement on the session cookie."""

    DEBUG = True
    TESTING = False
    SESSION_COOKIE_SECURE = False
    ARGON2_TIME_COST = 1
    ARGON2_MEMORY_COST = 8192
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG')
    LOG_JSON = _bool('LOG_JSON', False)


class TestingConfig(Config):
    """Test suite: in-process SQLite, no CSRF, no external services."""

    DEBUG = False
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get('TEST_DATABASE_URL', 'sqlite:///:memory:')
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    RATELIMIT_ENABLED = False
    CACHE_TYPE = 'SimpleCache'
    ARGON2_TIME_COST = 1
    ARGON2_MEMORY_COST = 8192
    ARGON2_PARALLELISM = 1
    SECRETS_ENCRYPTION_KEY = 'dGVzdC1rZXktZm9yLXVuaXQtdGVzdHMtMzJieXRlcy0hIQ=='
    CELERY_TASK_ALWAYS_EAGER = True
    STORAGE_BACKEND = 'memory'
    ANTIVIRUS_BACKEND = 'noop'
    OCR_BACKEND = 'noop'
    AI_BOOTSTRAP_PROVIDER = 'stub'
    WEB_RESEARCH_ENABLED = False
    LOG_JSON = False


class ProductionConfig(Config):
    """Production: strict security posture."""

    DEBUG = False
    TESTING = False

    #: Whether to redirect every request to HTTPS. True unless somebody says
    #: otherwise, because a production deployment that quietly serves HTTP is
    #: the failure worth defaulting against.
    #:
    #: It exists because the same decision is also taken by Nginx, and the two
    #: disagreeing is worse than either: with a self-signed certificate, Nginx
    #: served HTTP while the application answered 302 to an HTTPS the browser
    #: refuses, so the site was unreachable by both doors. «./start.sh» now
    #: sets this alongside the Nginx redirect, from one look at the certificate.
    FORCE_HTTPS = _bool('FORCE_HTTPS', True)

    @staticmethod
    def init_app(app):
        Config.init_app(app)
        app.config['PREFERRED_URL_SCHEME'] = 'https'


config = {
    'development': DevelopmentConfig,
    'testing': TestingConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig,
}
