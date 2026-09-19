"""Asynchronous task package (specification section 3.1)."""
from app.tasks.celery_app import FlaskTask, celery

__all__ = ['celery', 'FlaskTask']
