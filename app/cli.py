"""Operational ``flask`` commands.

Everything an operator needs to bring a fresh deployment up: create the schema,
seed roles, catalogues and settings, create the first administrator, verify the
audit chain and check the AI provider.
"""
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


@click.command('create-admin')
@click.option('--username', prompt='Nombre de usuario')
@click.option('--email', prompt='Correo electrónico')
@click.option('--nombre', prompt='Nombre')
@click.option('--apellidos', default='', prompt='Apellidos')
@click.option('--password', prompt='Contraseña', hide_input=True, confirmation_prompt=True)
@with_appcontext
def create_admin(username, email, nombre, apellidos, password):
    """Create an administrator account."""
    _create(username, email, nombre, apellidos, password, 'administrador')


@click.command('create-user')
@click.option('--username', prompt='Nombre de usuario')
@click.option('--email', prompt='Correo electrónico')
@click.option('--nombre', prompt='Nombre')
@click.option('--apellidos', default='', prompt='Apellidos')
@click.option('--rol', type=click.Choice(['administrador', 'gestor', 'usuario']),
              default='usuario', prompt='Rol')
@click.option('--password', prompt='Contraseña', hide_input=True, confirmation_prompt=True)
@with_appcontext
def create_user(username, email, nombre, apellidos, rol, password):
    """Create an account with the chosen role."""
    _create(username, email, nombre, apellidos, password, rol)


def _create(username, email, nombre, apellidos, password, rol):
    from app.models.enums import UserStatus
    from app.models.user import Role, User
    from app.services.identity.local import validate_password_strength
    from app.services.seed_service import seed_roles
    from app.utils.crypto import hash_password
    from app.utils.timeutil import utcnow

    problems = validate_password_strength(password)
    if problems:
        for problem in problems:
            click.echo(click.style(f'  ✗ {problem}', fg='red'))
        raise click.Abort()

    if User.query.filter(
        db.or_(User.username == username, User.email == email)
    ).first():
        click.echo(click.style('Ya existe un usuario con ese nombre o correo.', fg='red'))
        raise click.Abort()

    seed_roles()
    role = Role.get(rol)
    if role is None:
        click.echo(click.style(f'El rol «{rol}» no existe.', fg='red'))
        raise click.Abort()

    user = User(
        username=username,
        email=email,
        nombre=nombre,
        apellidos=apellidos or None,
        password_hash=hash_password(password),
        password_changed_at=utcnow(),
        estado=UserStatus.ACTIVO,
    )
    user.roles.append(role)
    db.session.add(user)
    db.session.commit()

    click.echo(click.style(f'Usuario «{username}» creado con el rol «{rol}».', fg='green'))


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
