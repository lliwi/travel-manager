"""Authentication routes.

Login is rate limited, every outcome is audited, and the message shown on
failure is deliberately identical whatever went wrong -- a wrong password, an
unknown user and a disabled account are indistinguishable from outside.
"""
import logging
from urllib.parse import urlparse

from flask import (
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from app.blueprints.auth import auth_bp
from app.blueprints.auth.forms import (
    ChangePasswordForm,
    LoginForm,
    MFAForm,
    ProfileForm,
)
from app.extensions import db, limiter
from app.models.enums import AuditResourceType, AuditResult
from app.services import audit_service
from app.services.identity import authenticate, get_provider
from app.utils.decorators import public_endpoint
from app.utils.errors import AccountLocked, AppError, AuthenticationFailed
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Shown for every failed login, whatever the underlying reason.
GENERIC_LOGIN_ERROR = 'Usuario o contraseña incorrectos.'


def _safe_next(next_url):
    """Return ``next_url`` only when it is a relative path.

    Guards against an open redirect: an absolute URL in ``?next=`` would let a
    phishing page bounce a freshly authenticated user off-site.
    """
    if not next_url:
        return None
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return None
    if not next_url.startswith('/'):
        return None
    return next_url


@auth_bp.route('/login', methods=['GET', 'POST'])
@public_endpoint
@limiter.limit('10 per minute; 40 per hour', methods=['POST'])
def login():
    """Authenticate a user against the configured identity provider."""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    form = LoginForm()
    if form.validate_on_submit():
        try:
            user = authenticate(form.username.data, form.password.data)
        except AccountLocked:
            flash(
                'La cuenta está bloqueada temporalmente por intentos fallidos. '
                'Inténtelo de nuevo más tarde.',
                'danger',
            )
            return render_template('auth/login.html', form=form), 423
        except AuthenticationFailed:
            flash(GENERIC_LOGIN_ERROR, 'danger')
            return render_template('auth/login.html', form=form), 401
        except AppError:
            raise
        except Exception:
            logger.exception('Fallo inesperado durante la autenticación.')
            flash('No ha sido posible completar el inicio de sesión.', 'danger')
            return render_template('auth/login.html', form=form), 500

        # The second factor happens before the session exists. Logging the
        # person in and then asking would mean a valid session for somebody who
        # has only proved half of what we ask, and every «are you logged in»
        # check in the application would already say yes.
        if user.mfa_activo:
            session['mfa_pendiente'] = {
                'user_id': str(user.id),
                'desde': utcnow().timestamp(),
                'remember': bool(form.remember_me.data),
                'next': _safe_next(request.args.get('next')),
            }
            return redirect(url_for('auth.mfa_verify'))

        login_user(user, remember=form.remember_me.data)
        session.permanent = bool(form.remember_me.data)

        if user.must_change_password:
            flash('Debe cambiar su contraseña antes de continuar.', 'warning')
            return redirect(url_for('auth.change_password'))

        destination = _safe_next(request.args.get('next'))
        flash(f'Bienvenido/a, {user.nombre_completo}.', 'success')
        return redirect(destination or url_for('dashboard.index'))

    return render_template('auth/login.html', form=form)


#: How long the half-finished sign-in stays valid. Long enough to open an
#: authenticator and type six digits, short enough that walking away from an
#: unlocked machine does not leave the second factor pending all afternoon.
MFA_PENDIENTE_SEGUNDOS = 300


def _pendiente():
    """The half-finished sign-in, or None when there is none or it expired."""
    datos = session.get('mfa_pendiente')
    if not datos:
        return None
    if utcnow().timestamp() - float(datos.get('desde', 0)) > MFA_PENDIENTE_SEGUNDOS:
        session.pop('mfa_pendiente', None)
        return None
    return datos


@auth_bp.route('/mfa', methods=['GET', 'POST'])
@public_endpoint
@limiter.limit('10 per minute; 30 per hour', methods=['POST'])
def mfa_verify():
    """The second step of signing in.

    Rate limited harder than the password form, and for a different reason:
    six digits is a million possibilities, which is a lot for a person and not
    much for a script left running overnight.
    """
    import uuid

    from app.models.user import User
    from app.services import mfa_service

    datos = _pendiente()
    if not datos:
        flash('Vuelva a iniciar sesión.', 'info')
        return redirect(url_for('auth.login'))

    user = db.session.get(User, uuid.UUID(datos['user_id']))
    if user is None or user.is_deleted:
        session.pop('mfa_pendiente', None)
        return redirect(url_for('auth.login'))

    form = MFAForm()
    if form.validate_on_submit():
        codigo = (form.codigo.data or '').strip()
        # Either factor proves the same thing; a recovery code additionally
        # says the authenticator is gone, which is worth recording.
        por_recuperacion = False
        valido = mfa_service.verificar_codigo(user, codigo)
        if not valido:
            valido = mfa_service.consumir_codigo_de_recuperacion(user, codigo)
            por_recuperacion = valido

        if not valido:
            audit_service.record(
                'auth.mfa_failed',
                recurso_tipo=AuditResourceType.SESION,
                recurso_id=str(user.id),
                actor=None,
                resultado=AuditResult.DENEGADO,
            )
            flash('El código no es correcto.', 'danger')
            return render_template('auth/mfa.html', form=form), 401

        session.pop('mfa_pendiente', None)
        login_user(user, remember=datos.get('remember'))
        session.permanent = bool(datos.get('remember'))

        audit_service.record(
            'auth.mfa_ok',
            recurso_tipo=AuditResourceType.SESION,
            recurso_id=str(user.id),
            actor=user,
            metadatos={'por_recuperacion': por_recuperacion},
        )
        if por_recuperacion:
            flash(
                'Ha entrado con un código de recuperación; ya no se puede usar '
                f'otra vez. Le quedan {user.mfa_codigos_disponibles}.',
                'warning',
            )

        if user.must_change_password:
            return redirect(url_for('auth.change_password'))

        destination = _safe_next(datos.get('next'))
        flash(f'Bienvenido/a, {user.nombre_completo}.', 'success')
        return redirect(destination or url_for('dashboard.index'))

    return render_template('auth/mfa.html', form=form)


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    """End the session.

    POST only, with a CSRF token: a GET logout link can be triggered by any page
    that embeds it, logging the user out without their intent.
    """
    audit_service.record(
        'auth.logout',
        recurso_tipo=AuditResourceType.SESION,
        recurso_id=str(current_user.id),
    )
    logout_user()
    session.clear()
    flash('Sesión finalizada.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/perfil', methods=['GET', 'POST'])
@login_required
def profile():
    """View and edit one's own profile.

    Read-only for a directory-backed account. Every field here comes from the
    directory and is refreshed from it on the next sign-in, so an edit would
    appear to work and then quietly revert -- and the person would have no way
    of knowing which of the two versions the application believed.
    """
    from app.services import mfa_service

    user = current_user._get_current_object()
    obligatorio = mfa_service.es_obligatorio(user)

    if not user.es_local:
        return render_template(
            'auth/profile.html', form=None, user=user,
            mfa_obligatorio=obligatorio,
        )

    form = ProfileForm(obj=user)

    if form.validate_on_submit():
        from app.utils.timeutil import is_valid_timezone

        if form.zona_horaria.data and not is_valid_timezone(form.zona_horaria.data):
            form.zona_horaria.errors.append(
                'La zona horaria no es válida. Use un nombre IANA, por ejemplo Europe/Madrid.'
            )
        else:
            form.populate_obj(user)
            db.session.commit()
            audit_service.record(
                'user.profile_updated',
                recurso_tipo=AuditResourceType.USUARIO,
                recurso_id=str(user.id),
            )
            flash('Perfil actualizado.', 'success')
            return redirect(url_for('auth.profile'))

    return render_template(
        'auth/profile.html', form=form, user=user, mfa_obligatorio=obligatorio,
    )


@auth_bp.route('/mfa/activar', methods=['GET', 'POST'])
@login_required
def mfa_setup():
    """Enrol an authenticator, and only turn it on once it has been proved.

    The secret is stored unconfirmed while this runs. Turning it on at the
    moment the QR is shown would lock somebody out of their own account the
    first time a scan went wrong -- and the only person who could tell them why
    would be looking at the same blank screen.
    """
    from app.services import mfa_service

    user = current_user._get_current_object()
    if user.mfa_activo:
        flash('Su cuenta ya tiene segundo factor.', 'info')
        return redirect(url_for('auth.profile'))

    form = MFAForm()
    form.submit.label.text = 'Activar'

    if form.validate_on_submit():
        try:
            codigos = mfa_service.confirmar_alta(user, form.codigo.data)
        except AppError as error:
            flash(error.mensaje, 'danger')
        else:
            # Shown once and never again: they are stored hashed, so this is
            # the only moment anybody can write them down.
            return render_template('auth/mfa_codes.html', codigos=codigos)

    alta = mfa_service.iniciar_alta(user)
    return render_template('auth/mfa_setup.html', form=form, alta=alta)


@auth_bp.route('/mfa/desactivar', methods=['POST'])
@login_required
def mfa_disable():
    """Turn one's own second factor off, unless policy requires it."""
    from app.services import mfa_service

    user = current_user._get_current_object()
    if mfa_service.es_obligatorio(user):
        flash(
            'Su organización exige segundo factor para su perfil, así que no '
            'puede desactivarlo.',
            'warning',
        )
        return redirect(url_for('auth.profile'))

    mfa_service.desactivar(user, actor=user, motivo='propio')
    flash('Segundo factor desactivado.', 'info')
    return redirect(url_for('auth.profile'))


@auth_bp.route('/mfa/codigos', methods=['POST'])
@login_required
def mfa_regenerate():
    """Issue a fresh set of recovery codes, invalidating the old ones."""
    from app.services import mfa_service

    user = current_user._get_current_object()
    if not user.mfa_activo:
        return redirect(url_for('auth.profile'))

    codigos = mfa_service.emitir_codigos(user)
    audit_service.record(
        'user.mfa_codes_reissued',
        recurso_tipo=AuditResourceType.USUARIO,
        recurso_id=str(user.id),
    )
    return render_template('auth/mfa_codes.html', codigos=codigos)


@auth_bp.route('/cambiar-contrasena', methods=['GET', 'POST'])
@login_required
def change_password():
    """Change one's own password."""
    provider = get_provider(current_user.identity_provider)
    if not provider.supports_password_change():
        flash(
            'Su contraseña se gestiona en el directorio corporativo y no puede '
            'cambiarse desde esta aplicación.',
            'info',
        )
        return redirect(url_for('auth.profile'))

    form = ChangePasswordForm()
    if form.validate_on_submit():
        try:
            provider.change_password(
                str(current_user.id),
                form.current_password.data,
                form.new_password.data,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return render_template('auth/change_password.html', form=form)

        audit_service.record(
            'user.password_changed',
            recurso_tipo=AuditResourceType.USUARIO,
            recurso_id=str(current_user.id),
        )
        # The session epoch changed, so this session is no longer valid either.
        logout_user()
        session.clear()
        flash('Contraseña actualizada. Inicie sesión de nuevo.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/change_password.html', form=form)
