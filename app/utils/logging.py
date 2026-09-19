"""Structured logging with per-request correlation identifiers.

Specification section 3.2 requires structured logs free of unnecessary PII and
secrets, and section 7 requires request correlation. Two pieces make that work:

* ``CorrelationIdFilter`` stamps every record with the current request id.
* ``RedactingFilter`` scrubs values that must never reach a log line.
"""
import logging
import re
import uuid

from flask import g, has_request_context, request

CORRELATION_HEADER = 'X-Request-ID'

# Keys whose values are replaced wholesale when they appear in a log record's
# structured extras. Matching is case-insensitive and substring based.
SENSITIVE_KEYS = (
    'password', 'passwd', 'secret', 'token', 'api_key', 'apikey',
    'authorization', 'cookie', 'session', 'private_key', 'credential',
)

# Patterns redacted inside free-form messages.
_REDACTION_PATTERNS = (
    # Bearer / API-key style tokens
    (re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}'), r'\1[REDACTED]'),
    (re.compile(r'(?i)\b(sk-[A-Za-z0-9]{8,})'), '[REDACTED]'),
    # key=value in query strings or kwargs
    (re.compile(
        r'(?i)\b(' + '|'.join(SENSITIVE_KEYS) + r')\b(\s*[=:]\s*)("?)([^\s,&"\')]+)(\3)'
    ), r'\1\2\3[REDACTED]\5'),
)

REDACTED = '[REDACTED]'


def new_correlation_id():
    """Generate a fresh correlation identifier."""
    return uuid.uuid4().hex


def current_correlation_id():
    """Return the correlation id bound to this request, if any."""
    if has_request_context():
        return getattr(g, 'correlation_id', None)
    return None


class CorrelationIdFilter(logging.Filter):
    """Attach request metadata to every record."""

    def filter(self, record):
        record.correlation_id = current_correlation_id() or '-'
        if has_request_context():
            record.http_method = request.method
            record.http_path = request.path
            record.remote_addr = request.remote_addr or '-'
            actor = getattr(g, 'actor_id', None)
            record.actor_id = str(actor) if actor else '-'
        else:
            record.http_method = '-'
            record.http_path = '-'
            record.remote_addr = '-'
            record.actor_id = '-'
        return True


class RedactingFilter(logging.Filter):
    """Strip credentials and obvious secrets out of log records."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive, never break logging
            return True

        redacted = message
        for pattern, replacement in _REDACTION_PATTERNS:
            redacted = pattern.sub(replacement, redacted)

        if redacted != message:
            record.msg = redacted
            record.args = ()

        for key in list(vars(record)):
            if any(token in key.lower() for token in SENSITIVE_KEYS):
                setattr(record, key, REDACTED)
        return True


def build_formatter(as_json):
    """Return the log formatter for the configured output style."""
    if as_json:
        from pythonjsonlogger import jsonlogger

        return jsonlogger.JsonFormatter(
            '%(asctime)s %(levelname)s %(name)s %(correlation_id)s %(actor_id)s '
            '%(http_method)s %(http_path)s %(message)s',
            rename_fields={'asctime': 'timestamp', 'levelname': 'level'},
        )
    return logging.Formatter(
        '[%(asctime)s] %(levelname)-8s [%(correlation_id)s] %(name)s: %(message)s'
    )
