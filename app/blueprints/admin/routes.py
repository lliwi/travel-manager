"""Administration routes.

Specification section 2.1: global configuration, users and roles, AI providers,
catalogues, retention policy, auditing and the AD/LDAP integration parameters.
"""
import logging

from flask import (
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.blueprints.admin import admin_bp
from app.extensions import db
from app.models.alert import AlertRuleSetting
from app.models.enums import AuditResourceType, AuditResult, UserStatus
from app.models.user import Role, User
from app.services import audit_service, settings_service
from app.utils.decorators import require_admin
from app.utils.errors import AppError

logger = logging.getLogger(__name__)


@admin_bp.route('/')
@login_required
@require_admin
def index():
    """Administration landing page."""
    from app.models.ai import AIProviderConfig
    from app.models.audit import AuditEvent
    from app.models.trip import Trip

    resumen = {
        'usuarios': User.query.filter(User.is_deleted.is_(False)).count(),
        'viajes': Trip.query.filter(Trip.is_deleted.is_(False)).count(),
        'proveedores_ia': AIProviderConfig.query.filter_by(activo=True).count(),
        'eventos_auditoria': AuditEvent.query.count(),
    }
    return render_template('admin/index.html', resumen=resumen)


# ======================================================================
# Users and roles
# ======================================================================
@admin_bp.route('/usuarios')
@login_required
@require_admin
def users():
    """List accounts."""
    page = request.args.get('page', 1, type=int)
    buscar = request.args.get('buscar', '').strip()

    query = User.query.filter(User.is_deleted.is_(False))
    if buscar:
        needle = f'%{buscar.lower()}%'
        query = query.filter(
            db.or_(
                db.func.lower(User.username).like(needle),
                db.func.lower(User.email).like(needle),
                db.func.lower(User.nombre).like(needle),
                db.func.lower(User.apellidos).like(needle),
            )
        )

    pagination = query.order_by(User.nombre, User.apellidos).paginate(
        page=page, per_page=current_app.config['ITEMS_PER_PAGE'], error_out=False
    )
    return render_template(
        'admin/users.html',
        users=pagination.items,
        pagination=pagination,
        buscar=buscar,
        roles=Role.query.all(),
        UserStatus=UserStatus,
    )


@admin_bp.route('/usuarios/nuevo', methods=['GET', 'POST'])
@login_required
@require_admin
def create_user():
    """Create a local account."""
    from app.blueprints.admin.forms import UserForm
    from app.services import user_service

    form = UserForm()
    form.roles.choices = [(str(r.codigo), r.nombre) for r in Role.query.all()]

    if form.validate_on_submit():
        try:
            user = user_service.create_user(
                actor=current_user._get_current_object(),
                username=form.username.data,
                email=form.email.data,
                nombre=form.nombre.data,
                apellidos=form.apellidos.data or None,
                password=form.password.data,
                role_codes=form.roles.data,
                puesto=form.puesto.data or None,
                departamento=form.departamento.data or None,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return render_template('admin/user_form.html', form=form, user=None)

        flash(f'Usuario «{user.username}» creado.', 'success')
        return redirect(url_for('admin.users'))

    return render_template('admin/user_form.html', form=form, user=None)


@admin_bp.route('/usuarios/<user_id>/editar', methods=['GET', 'POST'])
@login_required
@require_admin
def edit_user(user_id):
    """Edit an account and its roles."""
    import uuid

    from app.blueprints.admin.forms import UserForm
    from app.services import user_service
    from app.utils.errors import ResourceNotFound

    user = db.session.get(User, uuid.UUID(str(user_id)))
    if user is None or user.is_deleted:
        raise ResourceNotFound('El usuario indicado no existe.')

    form = UserForm(obj=user)
    form.roles.choices = [(str(r.codigo), r.nombre) for r in Role.query.all()]
    form.password.validators = []

    if not user.es_local:
        # These fields are not rendered for a directory account, so they arrive
        # empty and their «required» validators would fail the whole form --
        # the symptom being a save button that silently does nothing.
        for campo in ('username', 'email', 'nombre'):
            getattr(form, campo).validators = []

    if form.validate_on_submit():
        # What this application owns, and is editable whoever the account
        # belongs to: whether it may be used, and what it may do.
        cambios = {
            'estado': UserStatus.coerce(form.estado.data, user.estado),
            'role_codes': form.roles.data,
        }
        # The rest describes the person, and for a directory account the
        # directory describes them. The template hides those fields, but a form
        # arrives from wherever the sender likes: what is never read here
        # cannot be changed by sending it anyway.
        if user.es_local:
            cambios.update(
                email=form.email.data,
                nombre=form.nombre.data,
                apellidos=form.apellidos.data or None,
                puesto=form.puesto.data or None,
                departamento=form.departamento.data or None,
                password=form.password.data or None,
            )

        try:
            user_service.update_user(
                actor=current_user._get_current_object(),
                user=user,
                **cambios,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return render_template('admin/user_form.html', form=form, user=user)

        flash('Usuario actualizado.', 'success')
        return redirect(url_for('admin.users'))

    if request.method == 'GET':
        form.roles.data = user.role_codes
        form.estado.data = str(user.estado)

    return render_template('admin/user_form.html', form=form, user=user)


@admin_bp.route('/usuarios/<user_id>/mfa/quitar', methods=['POST'])
@login_required
@require_admin
def reset_user_mfa(user_id):
    """Clear somebody's second factor, for when the phone is gone.

    The one action here that removes a protection from an account that is not
    the actor's own, so it is audited with who did it. Whoever does this must
    be as sure of who they are talking to as they would be before resetting a
    password: from here on, that account is back to one factor.
    """
    import uuid

    from app.services import mfa_service
    from app.utils.errors import ResourceNotFound

    user = db.session.get(User, uuid.UUID(str(user_id)))
    if user is None or user.is_deleted:
        raise ResourceNotFound('El usuario indicado no existe.')

    mfa_service.desactivar(
        user, actor=current_user._get_current_object(), motivo='administrador',
    )
    # Their live sessions go too: whoever holds one got in with the factor we
    # have just removed, and that is the case worth cutting short.
    user.bump_session_epoch()
    db.session.commit()

    flash(
        f'Segundo factor retirado de {user.nombre_completo}. Tendrá que '
        'volver a configurarlo.',
        'info',
    )
    return redirect(url_for('admin.edit_user', user_id=user.id))


@admin_bp.route('/usuarios/<user_id>/eliminar', methods=['POST'])
@login_required
@require_admin
def delete_user(user_id):
    """Soft-delete an account."""
    import uuid

    from app.services import user_service
    from app.utils.errors import ResourceNotFound

    user = db.session.get(User, uuid.UUID(str(user_id)))
    if user is None or user.is_deleted:
        raise ResourceNotFound('El usuario indicado no existe.')

    try:
        user_service.delete_user(current_user._get_current_object(), user)
        flash(f'Usuario «{user.username}» desactivado.', 'info')
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('admin.users'))


# ======================================================================
# Settings and alert thresholds
# ======================================================================
@admin_bp.route('/directorio/probar')
@login_required
@require_admin
def test_directory():
    """Check the directory answers, and say precisely why when it does not.

    Worth its own button because every other way of finding out is a person
    failing to log in. The message is specific -- wrong service account, path
    that resolves to nothing, unreachable host -- because it goes to an
    administrator fixing their own configuration, not to a stranger at a login
    form who must never learn which half was wrong.
    """
    from app.services.identity.ldap import LDAPIdentityProvider

    try:
        ok, detalle = LDAPIdentityProvider().health_check()
    except Exception:
        logger.exception('Falló la prueba del directorio')
        ok, detalle = False, 'La prueba no se pudo completar. Revise los registros.'

    audit_service.record(
        'directory.tested',
        recurso_tipo=AuditResourceType.CONFIGURACION,
        actor=current_user._get_current_object(),
        resultado=AuditResult.EXITO if ok else AuditResult.ERROR,
        metadatos={'detalle': detalle},
    )
    flash(detalle, 'success' if ok else 'danger')
    return redirect(url_for('admin.settings', _anchor='directorio'))


@admin_bp.route('/ajustes', methods=['GET', 'POST'])
@login_required
@require_admin
def settings():
    """Edit the runtime settings."""
    if request.method == 'POST':
        actor = current_user._get_current_object()
        cambiados = []
        for setting in settings_service.all_settings():
            field = f'ajuste__{setting.clave}'
            if field not in request.form and setting.tipo != 'bool':
                continue
            raw = request.form.get(field)

            if setting.tipo == 'secreto':
                # Blank means «leave it», not «clear it»: the field arrives
                # empty on every load, so treating that as a change would wipe
                # the password every time anybody saved the page.
                if not (raw or '').strip():
                    continue
                settings_service.set_value(
                    setting.clave, raw.strip(), actor=actor, commit=False,
                )
                cambiados.append(setting.clave)
                continue

            valor = _coerce_setting(setting.tipo, raw, field in request.form)
            if valor != settings_service.get(setting.clave):
                settings_service.set_value(setting.clave, valor, actor=actor, commit=False)
                cambiados.append(setting.clave)

        db.session.commit()
        if cambiados:
            audit_service.record(
                'settings.updated',
                recurso_tipo=AuditResourceType.CONFIGURACION,
                actor=actor,
                metadatos={'claves': sorted(cambiados)},
            )
        flash(f'{len(cambiados)} ajustes actualizados.', 'success')
        return redirect(url_for('admin.settings'))

    settings_service.seed_defaults()

    por_grupo = {}
    for setting in settings_service.all_settings():
        por_grupo.setdefault(setting.grupo or 'general', []).append(setting)

    # In the declared order, with the names a person reads. A group with no
    # entry in the taxonomy still shows, at the end: a setting nobody can find
    # is a setting nobody administers.
    bloques = [
        (clave, nombre, ayuda, por_grupo.pop(clave))
        for clave, nombre, ayuda in settings_service.GRUPOS
        if por_grupo.get(clave)
    ]
    bloques.extend(
        (clave, clave.capitalize(), None, ajustes)
        for clave, ajustes in sorted(por_grupo.items())
    )

    from app.services import ai_provider_service

    return render_template(
        'admin/settings.html',
        bloques=bloques,
        get=_valor_para_pantalla,
        proveedores=ai_provider_service.list_providers(),
        bindings=ai_provider_service.list_bindings(),
    )


def _valor_para_pantalla(clave):
    """What the settings screen shows for a setting.

    A secret is never rendered back, not even to the administrator who typed
    it: the screen says one exists, and the field stays empty so that saving
    the form without touching it keeps it.
    """
    if settings_service.es_secreto(clave):
        return '' if settings_service.get(clave) in (None, '') else '__GUARDADO__'
    return settings_service.get(clave)


def _coerce_setting(tipo, raw, present):
    """Turn a form value into the setting's declared type."""
    if tipo == 'bool':
        return present and str(raw).lower() in ('1', 'true', 'on', 'yes')
    if tipo == 'int':
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    if tipo == 'float':
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return raw


@admin_bp.route('/alertas/reglas', methods=['GET', 'POST'])
@login_required
@require_admin
def alert_rules():
    """Configure alert thresholds (specification section 2.4)."""
    from app.services.seed_service import seed_alert_rules

    seed_alert_rules()
    rules = AlertRuleSetting.query.order_by(AlertRuleSetting.nombre).all()

    if request.method == 'POST':
        actor = current_user._get_current_object()
        for rule in rules:
            rule.activa = f'activa__{rule.regla}' in request.form
            parametros = dict(rule.parametros or {})
            for key in list(parametros):
                field = f'param__{rule.regla}__{key}'
                if field in request.form:
                    parametros[key] = _coerce_param(parametros[key], request.form[field])
            rule.parametros = parametros
            rule.actualizada_por_id = actor.id

        db.session.commit()
        audit_service.record(
            'alert_rules.updated',
            recurso_tipo=AuditResourceType.CONFIGURACION,
            actor=actor,
            metadatos={'reglas': [r.regla for r in rules]},
        )
        flash('Umbrales actualizados. Se aplicarán en el próximo recálculo.', 'success')
        return redirect(url_for('admin.alert_rules'))

    return render_template('admin/alert_rules.html', rules=rules)


def _coerce_param(current, raw):
    """Keep a threshold's original type when it is edited."""
    if isinstance(current, bool):
        return str(raw).lower() in ('1', 'true', 'on', 'yes')
    if isinstance(current, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return current
    if isinstance(current, float):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return current
    return raw


# ======================================================================
# AI providers
# ======================================================================
@admin_bp.route('/ajustes/proveedores')
@login_required
@require_admin
def ai_providers():
    """List configured providers and the per-task assignment."""
    from app.services import ai_provider_service

    return render_template(
        'admin/ai_providers.html',
        providers=ai_provider_service.list_providers(),
        bindings=ai_provider_service.list_bindings(),
        sugerencias=ai_provider_service.SUGERENCIAS,
    )


@admin_bp.route('/ajustes/proveedores/nuevo', methods=['GET', 'POST'])
@login_required
@require_admin
def create_ai_provider():
    """Configure a new inference endpoint."""
    from app.blueprints.admin.forms import AIProviderForm
    from app.services import ai_provider_service

    form = AIProviderForm()

    if form.validate_on_submit():
        try:
            config = ai_provider_service.create(
                actor=current_user._get_current_object(),
                nombre=form.nombre.data,
                proveedor=form.proveedor.data,
                base_url=form.base_url.data,
                modelo=form.modelo_por_defecto.data,
                api_key=form.api_key.data or None,
                activo=form.activo.data,
                por_defecto=form.es_por_defecto.data,
                timeout=form.timeout_segundos.data or 120,
                max_tokens=form.max_tokens.data or 2048,
                temperatura=form.temperatura.data,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return render_template('admin/ai_provider_form.html', form=form,
                                   provider=None,
                                   sugerencias=ai_provider_service.SUGERENCIAS)

        flash(f'Proveedor «{config.nombre}» configurado.', 'success')
        return redirect(url_for('admin.ai_providers'))

    return render_template('admin/ai_provider_form.html', form=form, provider=None,
                           sugerencias=ai_provider_service.SUGERENCIAS)


@admin_bp.route('/ajustes/proveedores/<provider_id>/editar', methods=['GET', 'POST'])
@login_required
@require_admin
def edit_ai_provider(provider_id):
    """Change a provider's configuration."""
    from app.blueprints.admin.forms import AIProviderForm
    from app.services import ai_provider_service

    config = ai_provider_service.get_or_404(provider_id)
    form = AIProviderForm(obj=config)
    # Lets the form accept an empty key field on a provider that already has one.
    form._existing_key = bool(config.api_key_encrypted)

    if form.validate_on_submit():
        try:
            ai_provider_service.update(
                actor=current_user._get_current_object(),
                config=config,
                nombre=form.nombre.data,
                base_url=form.base_url.data,
                modelo=form.modelo_por_defecto.data,
                api_key=form.api_key.data or None,
                activo=form.activo.data,
                por_defecto=form.es_por_defecto.data,
                timeout=form.timeout_segundos.data,
                max_tokens=form.max_tokens.data,
                temperatura=form.temperatura.data,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return render_template('admin/ai_provider_form.html', form=form,
                                   provider=config,
                                   sugerencias=ai_provider_service.SUGERENCIAS)

        flash('Proveedor actualizado.', 'success')
        return redirect(url_for('admin.ai_providers'))

    if request.method == 'GET':
        form.proveedor.data = str(config.proveedor)
        form.es_por_defecto.data = config.es_por_defecto

    return render_template('admin/ai_provider_form.html', form=form, provider=config,
                           sugerencias=ai_provider_service.SUGERENCIAS)


@admin_bp.route('/ajustes/proveedores/<provider_id>/eliminar', methods=['POST'])
@login_required
@require_admin
def delete_ai_provider(provider_id):
    """Remove a provider."""
    from app.services import ai_provider_service

    config = ai_provider_service.get_or_404(provider_id)
    try:
        ai_provider_service.delete(current_user._get_current_object(), config)
        flash(f'Proveedor «{config.nombre}» eliminado.', 'info')
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('admin.ai_providers'))


@admin_bp.route('/ajustes/proveedores/<provider_id>/predeterminado', methods=['POST'])
@login_required
@require_admin
def set_default_ai_provider(provider_id):
    """Make this the provider used when a task has no binding."""
    from app.services import ai_provider_service

    config = ai_provider_service.get_or_404(provider_id)
    try:
        ai_provider_service.set_default(current_user._get_current_object(), config)
        flash(f'«{config.nombre}» es ahora el proveedor predeterminado.', 'success')
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('admin.ai_providers'))


@admin_bp.route('/ajustes/proveedores/<provider_id>/probar', methods=['POST'])
@login_required
@require_admin
def test_ai_provider(provider_id):
    """Check that a provider is reachable and holds its model."""
    from app.services import ai_provider_service
    from app.services.ai import health_check

    provider = ai_provider_service.get_or_404(provider_id)
    ok, detail = health_check(provider)
    flash(f'{provider.nombre}: {detail}', 'success' if ok else 'danger')
    return redirect(url_for('admin.ai_providers'))


@admin_bp.route('/ajustes/proveedores/<provider_id>/modelos')
@login_required
@require_admin
def ai_provider_models(provider_id):
    """The models this provider offers, for the model picker.

    Answers JSON because the form asks for it without reloading. The API key is
    never part of the exchange: the endpoint is consulted server side with the
    key already stored, so it is not handed back to the browser.
    """
    from app.services import ai_provider_service

    provider = ai_provider_service.get_or_404(provider_id)
    modelos, error = ai_provider_service.available_models(provider)
    return jsonify({
        'modelos': modelos,
        'error': error,
        'actual': provider.modelo_por_defecto,
    })


@admin_bp.route('/ajustes/proveedores/tareas', methods=['POST'])
@login_required
@require_admin
def set_ai_binding():
    """Assign one task to a provider (specification section 2.5)."""
    from app.services import ai_provider_service

    tarea = request.form.get('tarea')
    provider_id = request.form.get('provider_config_id') or None
    modelo = request.form.get('modelo') or None

    try:
        config = ai_provider_service.get_or_404(provider_id) if provider_id else None
        ai_provider_service.set_binding(
            current_user._get_current_object(), tarea, config, modelo=modelo
        )
        flash(
            f'Tarea asignada a «{config.nombre}».' if config
            else 'La tarea usará el proveedor predeterminado.',
            'success',
        )
    except AppError as error:
        flash(error.mensaje, 'danger')

    return redirect(url_for('admin.ai_providers'))


# ======================================================================
# Audit
# ======================================================================
@admin_bp.route('/auditoria')
@login_required
@require_admin
def audit():
    """Browse the audit trail."""
    from app.models.audit import AuditEvent

    page = request.args.get('page', 1, type=int)
    query = AuditEvent.query

    if request.args.get('accion'):
        query = query.filter(AuditEvent.accion.like(f'%{request.args["accion"]}%'))
    if request.args.get('actor'):
        query = query.filter(AuditEvent.actor_username == request.args['actor'])
    if request.args.get('resultado'):
        query = query.filter(AuditEvent.resultado == request.args['resultado'])

    pagination = query.order_by(AuditEvent.id.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    return render_template(
        'admin/audit.html', events=pagination.items, pagination=pagination
    )


@admin_bp.route('/auditoria/verificar', methods=['POST'])
@login_required
@require_admin
def verify_audit():
    """Verify the integrity of the audit hash chain."""
    ok, problems = audit_service.verify_chain()
    if ok:
        flash('La cadena de auditoría es íntegra.', 'success')
    else:
        flash(
            f'Se han detectado {len(problems)} anomalías en la cadena de auditoría.',
            'danger',
        )
    return redirect(url_for('admin.audit'))
