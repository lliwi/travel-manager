"""Deciding whether queued work runs in a worker or inline.

Two situations call for running inline rather than queueing:

* the test suite, which must not depend on a broker being up; and
* a broker outage, where computing alerts slowly is better than not at all.

Everything that hands work to Celery goes through :func:`is_eager` so that
choice is made in one place.
"""
import logging

from flask import current_app, has_app_context

logger = logging.getLogger(__name__)


def is_eager():
    """True when queued work should run inline in this process."""
    if not has_app_context():
        return False
    return bool(
        current_app.config.get('CELERY_TASK_ALWAYS_EAGER')
        or current_app.config.get('TESTING')
    )
