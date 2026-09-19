"""Alembic environment for Travel Manager.

``compare_type`` is on because several columns are constrained VARCHARs backing
enums: without it, adding an enum member and widening the column would not be
detected. ``render_as_batch`` lets the same migrations run against SQLite, which
the test suite uses.
"""
import logging
from logging.config import fileConfig

from alembic import context
from flask import current_app

config = context.config
fileConfig(config.config_file_name)
logger = logging.getLogger('alembic.env')


def get_engine():
    """The application's SQLAlchemy engine."""
    try:
        return current_app.extensions['migrate'].db.get_engine()
    except (TypeError, AttributeError):
        return current_app.extensions['migrate'].db.engine


def get_engine_url():
    """The database URL, with the password hidden from the log output."""
    try:
        return get_engine().url.render_as_string(hide_password=False).replace('%', '%%')
    except AttributeError:
        return str(get_engine().url).replace('%', '%%')


config.set_main_option('sqlalchemy.url', get_engine_url())
target_db = current_app.extensions['migrate'].db


def get_metadata():
    if hasattr(target_db, 'metadatas'):
        return target_db.metadatas[None]
    return target_db.metadata


def render_item(type_, obj, autogen_context):
    """Render the project's custom column types in a migration script.

    ``EnumType`` carries the Python enum class so that reading a row gives back
    a member, but at DDL level it is only a constrained VARCHAR. Rendering it as
    ``sa.String`` keeps the generated migration importable and independent of
    the enum's later evolution -- a migration must describe the schema as it was,
    not follow the application's enums as they change.
    """
    if type_ == 'type':
        from app.models.base import EnumType, JSONBType

        if isinstance(obj, EnumType):
            autogen_context.imports.add('import sqlalchemy as sa')
            return f'sa.String(length={obj.impl.length})'
        if isinstance(obj, JSONBType):
            autogen_context.imports.add('import app.models.base')
            return 'app.models.base.JSONBType()'
    return False


def run_migrations_offline():
    """Emit SQL to a script rather than running it."""
    context.configure(
        url=config.get_main_option('sqlalchemy.url'),
        target_metadata=get_metadata(),
        literal_binds=True,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run the migrations against a live connection."""

    def process_revision_directives(context_, revision, directives):
        """Do not write an empty migration when nothing changed."""
        if getattr(config.cmd_opts, 'autogenerate', False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info('No se han detectado cambios en el esquema.')

    conf_args = dict(current_app.extensions['migrate'].configure_args)
    conf_args.setdefault('process_revision_directives', process_revision_directives)
    conf_args['compare_type'] = True
    conf_args['render_item'] = render_item
    # Batch mode lets ALTER-shy backends (SQLite) run the same scripts.
    conf_args['render_as_batch'] = True

    connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=get_metadata(),
            **conf_args,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
