"""Administration routes.

Specification section 2.1: global configuration, users and roles, AI providers,
catalogues, retention policy, auditing and the AD/LDAP integration parameters.
"""
import logging

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.admin import admin_bp
from app.extensions import db
from app.models.alert import AlertRuleSetting
from app.models.enums import AuditResourceType, UserStatus
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

    if form.validate_on_submit():
        try:
            user_service.update_user(
                actor=current_user._get_current_object(),
                user=user,
                email=form.email.data,
                nombre=form.nombre.data,
                apellidos=form.apellidos.data or None,
                puesto=form.puesto.data or None,
                departamento=form.departamento.data or None,
                estado=UserStatus.coerce(form.estado.data, user.estado),
                role_codes=form.roles.data,
                password=form.password.data or None,
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
    grouped = {}
    for setting in settings_service.all_settings():
        grouped.setdefault(setting.grupo or 'general', []).append(setting)

    return render_template(
        'admin/settings.html', grouped=grouped, get=settings_service.get
    )


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
@admin_bp.route('/ia/proveedores')
@login_required
@require_admin
def ai_providers():
    """List configured AI providers and their task bindings."""
    from app.models.ai import AIProviderConfig, AITaskBinding

    return render_template(
        'admin/ai_providers.html',
        providers=AIProviderConfig.query.order_by(AIProviderConfig.nombre).all(),
        bindings=AITaskBinding.query.all(),
    )


@admin_bp.route('/ia/proveedores/<provider_id>/probar', methods=['POST'])
@login_required
@require_admin
def test_ai_provider(provider_id):
    """Check that a provider is reachable."""
    import uuid

    from app.models.ai import AIProviderConfig
    from app.services.ai import health_check
    from app.utils.errors import ResourceNotFound

    provider = db.session.get(AIProviderConfig, uuid.UUID(str(provider_id)))
    if provider is None:
        raise ResourceNotFound('El proveedor indicado no existe.')

    ok, detail = health_check(provider)
    flash(
        f'{provider.nombre}: {detail}',
        'success' if ok else 'danger',
    )
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
