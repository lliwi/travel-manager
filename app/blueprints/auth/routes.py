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
from app.blueprints.auth.forms import ChangePasswordForm, LoginForm, ProfileForm
from app.extensions import db, limiter
from app.models.enums import AuditResourceType
from app.services import audit_service
from app.services.identity import authenticate, get_provider
from app.utils.decorators import public_endpoint
from app.utils.errors import AccountLocked, AppError, AuthenticationFailed

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

        login_user(user, remember=form.remember_me.data)
        session.permanent = bool(form.remember_me.data)

        if user.must_change_password:
            flash('Debe cambiar su contraseña antes de continuar.', 'warning')
            return redirect(url_for('auth.change_password'))

        destination = _safe_next(request.args.get('next'))
        flash(f'Bienvenido/a, {user.nombre_completo}.', 'success')
        return redirect(destination or url_for('dashboard.index'))

    return render_template('auth/login.html', form=form)


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
    """View and edit one's own profile."""
    user = current_user._get_current_object()
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

    return render_template('auth/profile.html', form=form, user=user)


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
