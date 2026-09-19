"""Operational ``flask`` commands.

Everything an operator needs to bring a fresh deployment up: create the schema,
seed roles, catalogues and settings, create the first administrator, verify the
audit chain and check the AI provider.
"""
import os

import click
from flask.cli import with_appcontext

from app.extensions import db


def register_commands(app):
    """Attach every command to the application."""
    app.cli.add_command(init_db)
    app.cli.add_command(seed)
    app.cli.add_command(create_admin)
    app.cli.add_command(create_user)
    app.cli.add_command(verify_audit)
    app.cli.add_command(generate_key)
    app.cli.add_command(ai_health)


@click.command('init-db')
@with_appcontext
def init_db():
    """Create every table and seed the baseline data."""
    db.create_all()
    click.echo('Esquema creado.')
    _seed_all()


@click.command('seed')
@click.option('--catalogs/--no-catalogs', default=True, help='Cargar países y aeropuertos.')
@with_appcontext
def seed(catalogs):
    """Seed roles, settings, alert rules and reference catalogues."""
    _seed_all(catalogs=catalogs)


def _seed_all(catalogs=True):
    """Idempotent seeding: existing rows are never overwritten."""
    from app.services import settings_service
    from app.services.seed_service import (
        seed_alert_rules,
        seed_catalogs,
        seed_roles,
        seed_web_sources,
    )

    click.echo(f'Roles creados: {seed_roles()}')
    click.echo(f'Ajustes creados: {settings_service.seed_defaults()}')
    click.echo(f'Reglas de alerta creadas: {seed_alert_rules()}')
    click.echo(f'Fuentes web creadas: {seed_web_sources()}')
    if catalogs:
        countries, locations = seed_catalogs()
        click.echo(f'Países creados: {countries} | Localizaciones creadas: {locations}')


#: Environment variable read when no ``--password`` is given. Preferred over
#: the flag for automation: a password on the command line ends up in the shell
#: history and is visible to anyone who can run ``ps``.
PASSWORD_ENV = 'TRAVEL_ADMIN_PASSWORD'  # noqa: S105 - nombre de variable, no una contraseña


def _interactive():
    """True when there is a terminal to prompt on."""
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


def _explain_non_interactive(rol):
    """Tell the operator how to run this without a terminal.

    Click's own failure here is a bare "Aborted!", which says nothing about
    what went wrong or how to fix it.
    """
    click.echo(click.style(
        'No hay terminal interactiva, así que no se pueden pedir los datos.',
        fg='red', bold=True,
    ))
    click.echo()
    click.echo('Esto ocurre al ejecutar con «docker compose exec -T», desde un')
    click.echo('script o a través de una tubería. Tiene dos salidas:')
    click.echo()
    click.echo(click.style('  1. Asignar una terminal (quite la opción -T):', bold=True))
    click.echo('     docker compose -f docker/docker-compose.yml exec web \\')
    click.echo(f'       flask create-{rol}')
    click.echo()
    click.echo(click.style('  2. Pasar los datos como opciones:', bold=True))
    click.echo(f'     export {PASSWORD_ENV}=\'...\'')
    click.echo('     docker compose -f docker/docker-compose.yml exec -T \\')
    click.echo(f'       -e {PASSWORD_ENV} web flask create-{rol} \\')
    click.echo('       --username admin --email admin@empresa.test --nombre Ana')
    click.echo()
    click.echo(click.style(
        f'Use la variable {PASSWORD_ENV} en lugar de --password: una contraseña '
        'en la línea de órdenes queda en el historial del intérprete y es '
        'visible con «ps».',
        fg='yellow',
    ))


def _resolve_password(password, rol):
    """Get the password from the flag, the environment or a prompt."""
    if password:
        return password

    from_env = os.environ.get(PASSWORD_ENV)
    if from_env:
        return from_env

    if not _interactive():
        _explain_non_interactive(rol)
        raise click.Abort()

    return click.prompt(
        'Contraseña', hide_input=True, confirmation_prompt=True
    )


def _resolve_field(value, etiqueta, rol, default=None, required=True):
    """Get one field from the flag or a prompt, explaining if neither works."""
    if value:
        return value
    if not required and value is not None:
        return value

    if not _interactive():
        if default is not None:
            return default
        _explain_non_interactive(rol)
        raise click.Abort()

    return click.prompt(etiqueta, default=default, show_default=default is not None)


@click.command('create-admin')
@click.option('--username', default=None, help='Nombre de usuario.')
@click.option('--email', default=None, help='Correo electrónico.')
@click.option('--nombre', default=None, help='Nombre de pila.')
@click.option('--apellidos', default=None, help='Apellidos.')
@click.option('--password', default=None,
              help=f'Contraseña. Preferible usar la variable {PASSWORD_ENV}.')
@with_appcontext
def create_admin(username, email, nombre, apellidos, password):
    """Crear una cuenta de administrador.

    Sin opciones, pide los datos por pantalla. Para automatizarlo, páselos como
    opciones y la contraseña en la variable TRAVEL_ADMIN_PASSWORD.
    """
    username = _resolve_field(username, 'Nombre de usuario', 'admin')
    email = _resolve_field(email, 'Correo electrónico', 'admin')
    nombre = _resolve_field(nombre, 'Nombre', 'admin')
    apellidos = _resolve_field(apellidos, 'Apellidos', 'admin', default='',
                               required=False)
    password = _resolve_password(password, 'admin')

    _create(username, email, nombre, apellidos, password, 'administrador')


@click.command('create-user')
@click.option('--username', default=None, help='Nombre de usuario.')
@click.option('--email', default=None, help='Correo electrónico.')
@click.option('--nombre', default=None, help='Nombre de pila.')
@click.option('--apellidos', default=None, help='Apellidos.')
@click.option('--rol', type=click.Choice(['administrador', 'gestor', 'usuario']),
              default=None, help='Rol asignado a la cuenta.')
@click.option('--password', default=None,
              help=f'Contraseña. Preferible usar la variable {PASSWORD_ENV}.')
@with_appcontext
def create_user(username, email, nombre, apellidos, rol, password):
    """Crear una cuenta con el rol indicado."""
    username = _resolve_field(username, 'Nombre de usuario', 'user')
    email = _resolve_field(email, 'Correo electrónico', 'user')
    nombre = _resolve_field(nombre, 'Nombre', 'user')
    apellidos = _resolve_field(apellidos, 'Apellidos', 'user', default='',
                               required=False)
    rol = _resolve_field(rol, 'Rol', 'user', default='usuario', required=False)
    password = _resolve_password(password, 'user')

    _create(username, email, nombre, apellidos, password, rol)


def _create(username, email, nombre, apellidos, password, rol):
    """Create the account, validating the password and the role first."""
    from app.models.enums import UserStatus
    from app.models.user import Role, User
    from app.services.identity.local import validate_password_strength
    from app.services.seed_service import seed_roles
    from app.utils.crypto import hash_password
    from app.utils.timeutil import utcnow

    problems = validate_password_strength(password)
    if problems:
        click.echo(click.style('La contraseña no cumple los requisitos:', fg='red'))
        for problem in problems:
            click.echo(click.style(f'  ✗ {problem}', fg='red'))
        raise click.Abort()

    if User.query.filter(
        db.or_(User.username == username, User.email == email)
    ).first():
        click.echo(click.style(
            'Ya existe un usuario con ese nombre o correo electrónico.', fg='red'
        ))
        raise click.Abort()

    seed_roles()
    role = Role.get(rol)
    if role is None:
        click.echo(click.style(f'El rol «{rol}» no existe.', fg='red'))
        raise click.Abort()

    user = User(
        username=username,
        email=str(email).strip().lower(),
        nombre=nombre,
        apellidos=apellidos or None,
        password_hash=hash_password(password),
        password_changed_at=utcnow(),
        estado=UserStatus.ACTIVO,
    )
    user.roles.append(role)
    db.session.add(user)
    db.session.commit()

    click.echo(click.style(
        f'Usuario «{username}» creado con el rol «{rol}».', fg='green'
    ))


@click.command('verify-audit')
@click.option('--limit', default=None, type=int, help='Número de eventos a comprobar.')
@with_appcontext
def verify_audit(limit):
    """Verify the integrity of the audit hash chain."""
    from app.services.audit_service import verify_chain

    ok, problems = verify_chain(limit=limit)
    if ok:
        click.echo(click.style('La cadena de auditoría es íntegra.', fg='green'))
        return

    click.echo(click.style(f'Se han detectado {len(problems)} anomalías:', fg='red'))
    for problem in problems[:50]:
        click.echo(f'  - evento {problem["id"]}: {problem["motivo"]}')
    raise SystemExit(1)


@click.command('generate-key')
def generate_key():
    """Print a fresh encryption key for SECRETS_ENCRYPTION_KEY."""
    from cryptography.fernet import Fernet

    click.echo(Fernet.generate_key().decode())


@click.command('ai-health')
@with_appcontext
def ai_health():
    """Check that every configured AI provider is reachable."""
    from app.services.ai import health_check_all

    results = health_check_all()
    if not results:
        click.echo(click.style('No hay proveedores de IA configurados.', fg='yellow'))
        return

    failed = False
    for name, (ok, detail) in results.items():
        if ok:
            click.echo(click.style(f'  ✓ {name}: {detail}', fg='green'))
        else:
            failed = True
            click.echo(click.style(f'  ✗ {name}: {detail}', fg='red'))
    if failed:
        raise SystemExit(1)
