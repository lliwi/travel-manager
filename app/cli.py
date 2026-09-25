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
    app.cli.add_command(apply_retention)
    app.cli.add_command(openapi)
    app.cli.add_command(evaluar)
    app.cli.add_command(ai_stats)
    app.cli.add_command(autoajustar)
    app.cli.add_command(mfa_reset)


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
        seed_ai_providers,
        seed_alert_rules,
        seed_catalogs,
        seed_roles,
        seed_web_sources,
    )

    click.echo(f'Roles creados: {seed_roles()}')
    click.echo(f'Ajustes creados: {settings_service.seed_defaults()}')
    click.echo(f'Reglas de alerta creadas: {seed_alert_rules()}')
    click.echo(f'Fuentes web creadas: {seed_web_sources()}')
    click.echo(f'Proveedores de IA creados: {seed_ai_providers()}')
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


def _password_problems(password):
    """Spanish descriptions of why a password is unacceptable, if any."""
    from app.services.identity.local import validate_password_strength

    return validate_password_strength(password)


def _report_problems(problems):
    for problem in problems:
        click.echo(click.style(f'  ✗ {problem}', fg='red'))


def _precheck_password(password, rol):
    """Validate a supplied password before anything else is asked for.

    A password given on the command line or in the environment is known up
    front, so rejecting it here costs the operator nothing. Checking it last
    would mean typing four fields only to be told the password was never going
    to be accepted.
    """
    supplied = password or os.environ.get(PASSWORD_ENV)
    if not supplied:
        return None

    problems = _password_problems(supplied)
    if problems:
        click.echo(click.style('La contraseña no cumple los requisitos:', fg='red', bold=True))
        _report_problems(problems)
        click.echo()
        click.echo('Debe tener al menos 12 caracteres e incluir mayúsculas, '
                   'minúsculas y algún dígito.')
        raise click.Abort()

    return supplied


def _resolve_password(password, rol):
    """Get the password, prompting again when the one typed is too weak."""
    if password:
        return password

    if not _interactive():
        _explain_non_interactive(rol)
        raise click.Abort()

    # Interactive: ask again rather than abort. Losing the four fields already
    # typed because of a typo in the password would be needlessly punishing.
    for intento in range(3):
        candidata = click.prompt(
            'Contraseña', hide_input=True, confirmation_prompt=True
        )
        problems = _password_problems(candidata)
        if not problems:
            return candidata

        click.echo(click.style('La contraseña no cumple los requisitos:', fg='red'))
        _report_problems(problems)
        if intento < 2:
            click.echo(click.style('Inténtelo de nuevo.', fg='yellow'))

    click.echo(click.style('Demasiados intentos.', fg='red'))
    raise click.Abort()


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
@click.option('--overwrite', is_flag=True, default=False,
              help='Sobrescribir la cuenta si ya existe, sin preguntar.')
@with_appcontext
def create_admin(username, email, nombre, apellidos, password, overwrite):
    """Crear una cuenta de administrador, o sobrescribir la existente.

    Sin opciones, pide los datos por pantalla. Para automatizarlo, páselos como
    opciones y la contraseña en la variable TRAVEL_ADMIN_PASSWORD.

    Si ya existe una cuenta con ese nombre o correo, se ofrece sobrescribirla:
    contraseña nueva, roles reemplazados y sesiones abiertas invalidadas. Sin
    terminal hace falta --overwrite para confirmarlo.
    """
    # Checked first: a password supplied up front that will be rejected should
    # not cost the operator four fields of typing.
    password = _precheck_password(password, 'admin')

    username = _resolve_field(username, 'Nombre de usuario', 'admin')
    email = _resolve_field(email, 'Correo electrónico', 'admin')
    nombre = _resolve_field(nombre, 'Nombre', 'admin')
    apellidos = _resolve_field(apellidos, 'Apellidos', 'admin', default='',
                               required=False)
    password = _resolve_password(password, 'admin')

    _create(username, email, nombre, apellidos, password, 'administrador',
            overwrite=overwrite)


@click.command('create-user')
@click.option('--username', default=None, help='Nombre de usuario.')
@click.option('--email', default=None, help='Correo electrónico.')
@click.option('--nombre', default=None, help='Nombre de pila.')
@click.option('--apellidos', default=None, help='Apellidos.')
@click.option('--rol', type=click.Choice(['administrador', 'gestor', 'usuario']),
              default=None, help='Rol asignado a la cuenta.')
@click.option('--password', default=None,
              help=f'Contraseña. Preferible usar la variable {PASSWORD_ENV}.')
@click.option('--overwrite', is_flag=True, default=False,
              help='Sobrescribir la cuenta si ya existe, sin preguntar.')
@with_appcontext
def create_user(username, email, nombre, apellidos, rol, password, overwrite):
    """Crear una cuenta con el rol indicado, o sobrescribir la existente."""
    password = _precheck_password(password, 'user')

    username = _resolve_field(username, 'Nombre de usuario', 'user')
    email = _resolve_field(email, 'Correo electrónico', 'user')
    nombre = _resolve_field(nombre, 'Nombre', 'user')
    apellidos = _resolve_field(apellidos, 'Apellidos', 'user', default='',
                               required=False)
    rol = _resolve_field(rol, 'Rol', 'user', default='usuario', required=False)
    password = _resolve_password(password, 'user')

    _create(username, email, nombre, apellidos, password, rol, overwrite=overwrite)


def _find_existing(username, email):
    """Locate an account matching the username or the email.

    Returns ``(usuario, motivo)``. Raises when the two identifiers point at
    *different* accounts: overwriting then would mean silently choosing one and
    leaving the other holding a now-duplicate email.
    """
    from app.models.user import User

    por_username = User.query.filter(
        db.func.lower(User.username) == username.strip().lower()
    ).first()
    por_email = User.query.filter(
        db.func.lower(User.email) == email.strip().lower()
    ).first()

    if por_username and por_email and por_username.id != por_email.id:
        click.echo(click.style(
            'El nombre de usuario y el correo pertenecen a cuentas distintas:',
            fg='red', bold=True,
        ))
        click.echo(f'  «{username}» → {por_username.email}')
        click.echo(f'  «{email}» → {por_email.username}')
        click.echo()
        click.echo('No está claro cuál debería sobrescribirse. Corrija uno de los dos '
                   'datos, o edite las cuentas desde Administración → Usuarios.')
        raise click.Abort()

    if por_username:
        return por_username, 'nombre de usuario'
    if por_email:
        return por_email, 'correo electrónico'
    return None, None


def _confirm_overwrite(user, rol, overwrite):
    """Decide whether to overwrite an existing account.

    Overwriting replaces the password and the roles and ends every open
    session, so it is not something to do by accident. Interactively the
    operator is shown what changes and asked; from a script it takes an
    explicit ``--overwrite``, because a deployment job should not be able to
    reset an administrator's password because a username happened to collide.
    """
    click.echo(click.style(
        f'Ya existe la cuenta «{user.username}» ({user.email}).', fg='yellow', bold=True
    ))
    click.echo('  Sobrescribirla supone:')
    click.echo('    · establecer una contraseña nueva')
    click.echo(f'    · dejar los roles en: {rol}'
               f'   (ahora: {", ".join(user.role_codes) or "ninguno"})')
    click.echo('    · cerrar todas sus sesiones abiertas')
    if user.is_deleted:
        click.echo('    · reactivar la cuenta, que está eliminada')
    click.echo()

    if overwrite:
        return True

    if not _interactive():
        click.echo(click.style(
            'Añada --overwrite para confirmarlo sin terminal.', fg='red'
        ))
        raise click.Abort()

    return click.confirm('¿Sobrescribir la cuenta?', default=False)


def _create(username, email, nombre, apellidos, password, rol, overwrite=False):
    """Create the account, or overwrite it when one already exists."""
    from app.models.enums import AuditResourceType, UserStatus
    from app.models.user import Role, User
    from app.services import audit_service
    from app.services.seed_service import seed_roles
    from app.utils.crypto import hash_password
    from app.utils.timeutil import utcnow

    problems = _password_problems(password)
    if problems:
        click.echo(click.style('La contraseña no cumple los requisitos:', fg='red'))
        _report_problems(problems)
        raise click.Abort()

    seed_roles()
    role = Role.get(rol)
    if role is None:
        click.echo(click.style(f'El rol «{rol}» no existe.', fg='red'))
        raise click.Abort()

    existing, _motivo = _find_existing(username, email)

    if existing is not None:
        if not _confirm_overwrite(existing, rol, overwrite):
            click.echo('Operación cancelada; la cuenta no se ha tocado.')
            raise click.Abort()

        antes = {
            'email': existing.email,
            'roles': list(existing.role_codes),
            'estado': str(existing.estado),
        }

        existing.username = username
        existing.email = str(email).strip().lower()
        existing.nombre = nombre
        existing.apellidos = apellidos or None
        existing.password_hash = hash_password(password)
        existing.password_changed_at = utcnow()
        existing.must_change_password = False
        existing.estado = UserStatus.ACTIVO
        existing.roles = [role]
        # A new password and a new role set must not leave old sessions valid.
        existing.bump_session_epoch()
        # Clear any lockout: the operator has just proven physical access.
        existing.failed_login_count = 0
        existing.locked_until = None
        if existing.is_deleted:
            existing.restore()

        db.session.commit()

        audit_service.record(
            'user.overwritten',
            recurso_tipo=AuditResourceType.USUARIO,
            recurso_id=str(existing.id),
            actor=None,
            metadatos={
                'username': existing.username,
                'via': 'cli',
                'antes': antes,
                'despues': {
                    'email': existing.email,
                    'roles': list(existing.role_codes),
                    'estado': str(existing.estado),
                },
            },
        )

        click.echo(click.style(
            f'Usuario «{username}» sobrescrito con el rol «{rol}».', fg='green'
        ))
        click.echo(click.style(
            '  Sus sesiones abiertas han quedado invalidadas.', fg='yellow'
        ))
        return existing

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

    audit_service.record(
        'user.created',
        recurso_tipo=AuditResourceType.USUARIO,
        recurso_id=str(user.id),
        actor=None,
        metadatos={'username': user.username, 'roles': user.role_codes, 'via': 'cli'},
    )

    click.echo(click.style(
        f'Usuario «{username}» creado con el rol «{rol}».', fg='green'
    ))
    return user


@click.command('mfa-reset')
@click.argument('usuario')
@with_appcontext
def mfa_reset(usuario):
    """Quitar el segundo factor de una cuenta, sin pasar por el navegador.

    La salida de emergencia. La ruta normal es que un administrador lo retire
    desde Usuarios; esto es para el día en que quien está fuera es el único
    administrador, que es precisamente cuando nadie puede entrar a arreglarlo.
    """
    from app.extensions import db
    from app.models.user import User
    from app.services import mfa_service

    texto = str(usuario).strip()
    user = User.query.filter(
        db.or_(User.username == texto, User.email == texto.lower()),
        User.is_deleted.is_(False),
    ).first()

    if user is None:
        click.echo(f'No existe ninguna cuenta «{texto}».', err=True)
        raise SystemExit(1)

    if not user.mfa_activo:
        click.echo(f'{user.username} no tiene segundo factor configurado.')
        return

    mfa_service.desactivar(user, actor=None, motivo='cli')
    user.bump_session_epoch()
    db.session.commit()
    click.echo(
        f'Segundo factor retirado de {user.username}. '
        'Sus sesiones abiertas se han cerrado.'
    )


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


@click.command('apply-retention')
@click.option(
    '--execute', is_flag=True,
    help='Borra realmente los originales. Sin este indicador solo se informa.',
)
@click.option('--yes', is_flag=True, help='No pedir confirmación.')
@with_appcontext
def apply_retention(execute, yes):
    """Purge document originals whose retention period has elapsed.

    The nightly task only ever reports: erasing an original is irreversible and
    how long to keep one is an organisational decision, not a default. This is
    the lever that actually does it, and it asks before pulling.
    """
    from app.tasks.maintenance_tasks import apply_retention as tarea

    if not execute:
        resultado = tarea.run(dry_run=True)
        candidatos = resultado['candidatos']
        if not candidatos:
            click.echo('Ningún documento ha superado su plazo de conservación.')
            return
        click.echo(click.style(
            f'{candidatos} documentos han superado su plazo de conservación.',
            fg='yellow',
        ))
        click.echo('Ejecute con --execute para borrar sus originales.')
        return

    if not yes:
        click.confirm(
            'Se borrarán los ficheros originales de forma irreversible. '
            'La ficha, su hash y la traza de auditoría se conservan. ¿Continuar?',
            abort=True,
        )

    resultado = tarea.run(dry_run=False)
    click.echo(click.style(
        f'Retención aplicada: {resultado["purgados"]} originales eliminados.',
        fg='green',
    ))


@click.command('openapi')
@click.option(
    '--output', '-o', default=None,
    help='Fichero donde escribirlo. Por omisión, la salida estándar.',
)
@with_appcontext
def openapi(output):
    """Write the OpenAPI description of /api/v1."""
    import json

    from app.services.openapi_service import build_spec

    documento = json.dumps(build_spec(), ensure_ascii=False, indent=2)
    if output:
        with open(output, 'w', encoding='utf-8') as destino:
            destino.write(documento + '\n')
        click.echo(click.style(f'Escrito en {output}.', fg='green'))
        return
    click.echo(documento)


@click.command('evaluar')
@click.option('--modelo', default=None, help='Probar otro modelo, sin cambiar la configuración.')
@click.option('--json', 'como_json', is_flag=True, help='Salida en JSON.')
@with_appcontext
def evaluar(modelo, como_json):
    """Run the golden set and score the extraction."""
    import json as _json

    from app.services import evaluation_service

    casos = evaluation_service.casos_dorados()
    if not casos:
        click.echo(click.style(
            'No hay casos en data/evaluacion. Sin ellos, cambiar de modelo es '
            'un acto de fe disfrazado de cambio de configuración.', fg='yellow',
        ))
        raise SystemExit(1)

    click.echo(f'Evaluando {len(casos)} casos…')
    resultado = evaluation_service.evaluar(modelo=modelo, casos=casos)

    if como_json:
        click.echo(_json.dumps(resultado, ensure_ascii=False, indent=2))
        return

    for caso in resultado['casos']:
        color = 'green' if caso['porcentaje'] == 100 else (
            'red' if caso['error'] or caso['porcentaje'] < 50 else 'yellow'
        )
        click.echo(click.style(
            f"  {caso['caso']:28} {caso['porcentaje']:3}%  "
            f"{len(caso['aciertos'])}/{caso['total']} campos  "
            f"{caso['servicios_obtenidos']}/{caso['servicios_esperados']} servicios  "
            f"{caso['duracion_s']}s",
            fg=color,
        ))
        if caso['error']:
            click.echo(f'      error: {caso["error"]}')
        for fallo in caso['fallos']:
            click.echo(
                f'      {fallo["campo"]}: esperaba «{fallo["esperado"]}», '
                f'devolvió «{fallo["obtenido"]}»'
            )
        if caso['ausentes']:
            click.echo(f'      sin responder: {", ".join(caso["ausentes"])}')

    click.echo()
    click.echo(click.style(
        f"Total: {resultado['porcentaje']}% "
        f"({resultado['aciertos']}/{resultado['total']} campos) "
        f"en {resultado['duracion_s']}s",
        fg='green' if resultado['porcentaje'] >= 90 else 'yellow',
    ))


@click.command('autoajustar')
@click.option('--tarea', required=True, help='Tarea que ajustar, p. ej. extract_document.')
@click.option('--modelo', default=None, help='Otro modelo del mismo proveedor.')
@click.option('--parametro', 'claves', multiple=True,
              help='Parámetro que ajustar; repetible. Por defecto, los habituales.')
@click.option('--ensayos', default=12, show_default=True, help='Ensayos como máximo.')
@click.option('--repeticiones', default=1, show_default=True, help='Repeticiones por caso.')
@click.option('--aplicar', is_flag=True, help='Aplicar si mejora lo bastante.')
@with_appcontext
def autoajustar(tarea, modelo, claves, ensayos, repeticiones, aplicar):
    """Search for better sampling parameters for a task, and say what it found."""
    from app.services import autotune_service
    from app.services.ai import parametros as catalogo
    from app.utils.errors import AppError

    click.echo(f'Estimación: hasta {autotune_service.estimar_llamadas(tarea, ensayos, repeticiones)} '
               f'llamadas al modelo.')
    try:
        # Inline: whoever runs this from a shell wants the answer on the shell.
        ajuste = autotune_service.lanzar(
            None, tarea, modelo=modelo, claves=list(claves) or None,
            presupuesto=ensayos, repeticiones=repeticiones,
            aplicar_si_mejora=aplicar, en_linea=True,
        )
    except AppError as error:
        click.echo(click.style(error.mensaje, fg='red'))
        raise SystemExit(1) from None

    for i, ensayo in enumerate(ajuste.ensayos or []):
        parametros = ', '.join(f'{e}={v}' for e, v in catalogo.describir(ensayo['parametros']))
        marca = '*' if ensayo['parametros'] == ajuste.parametros_mejores else ' '
        click.echo(f"{marca} {i:2} {ensayo['calidad']:5.1f}  {ensayo['duracion_ms'] / 1000:6.1f}s  "
                   f"{ensayo['errores']} err  {parametros or '(los actuales)'}")

    if ajuste.error:
        click.echo(click.style(f'Error: {ajuste.error}', fg='red'))
        raise SystemExit(1)
    click.echo()
    click.echo(f'Calidad: {ajuste.calidad_base} → {ajuste.calidad_mejor}. '
               + ('Aplicado.' if ajuste.aplicado else
                  'Propuesta pendiente en Administración → Autoajuste.' if ajuste.hay_propuesta
                  else 'Nada mejor que lo actual.'))


@click.command('ai-stats')
@click.option('--dias', default=30, help='Ventana a resumir.')
@with_appcontext
def ai_stats(dias):
    """How each model has behaved lately, from the runs already recorded."""
    from app.services import evaluation_service

    filas = evaluation_service.rendimiento_por_modelo(dias=dias)
    if not filas:
        click.echo(f'No hay ejecuciones en los últimos {dias} días.')
        return

    click.echo(f'Últimos {dias} días:')
    for fila in filas:
        color = 'red' if fila['tasa_error'] > 10 else 'green'
        click.echo(click.style(
            f"  {fila['proveedor']}/{fila['modelo']:22} "
            f"{fila['total']:4} ejecuciones  "
            f"{fila['tasa_error']:5}% error  "
            f"{fila['duracion_media_ms']:6} ms de media  "
            f"{fila['tokens_entrada'] + fila['tokens_salida']:7} tokens",
            fg=color,
        ))
        click.echo(f"      tareas: {', '.join(fila['tareas'])}")


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
