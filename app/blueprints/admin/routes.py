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
from app.extensions import csrf, db, limiter
from app.models.alert import AlertRuleSetting
from app.models.enums import AIProviderCode, AuditResourceType, AuditResult, UserStatus
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
    from app.utils import http

    return render_template(
        'admin/settings.html',
        bloques=bloques,
        salida_actual=http.descripcion(),
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
                sin_razonamiento=form.sin_razonamiento.data,
                parametros_avanzados=_avanzados_del_formulario(),
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return _formulario_de_proveedor(form, None, _avanzados_del_formulario())

        flash(f'Proveedor «{config.nombre}» configurado.', 'success')
        return redirect(url_for('admin.ai_providers'))

    return _formulario_de_proveedor(form, None)


def _avanzados_del_formulario():
    from app.services.ai import parametros as catalogo

    return {
        clave: valor for clave, valor in catalogo.desde_formulario(request.form).items()
        if clave not in ('temperatura', 'max_tokens')
    }


def _formulario_de_proveedor(form, config, avanzados=None):
    """Render the provider form with the catalogue of advanced parameters."""
    from app.services import ai_provider_service
    from app.services.ai import parametros as catalogo

    if avanzados is None:
        avanzados = (config.parametros_avanzados or {}) if config is not None else {}
    rechazados = {}
    if config is not None:
        rechazados = (config.parametros or {}).get('no_admitidos') or {}
    return render_template(
        'admin/ai_provider_form.html', form=form, provider=config,
        sugerencias=ai_provider_service.SUGERENCIAS,
        parametros_catalogo=[
            p for p in catalogo.CATALOGO if p.clave not in ('temperatura', 'max_tokens')
        ],
        avanzados=avanzados,
        rechazados=rechazados if isinstance(rechazados, dict) else {},
        familias={c.value: catalogo.familia(c.value) for c in AIProviderCode},
    )


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
                sin_razonamiento=form.sin_razonamiento.data,
                parametros_avanzados=_avanzados_del_formulario(),
            )
        except AppError as error:
            db.session.rollback()
            flash(error.mensaje, 'danger')
            return _formulario_de_proveedor(form, config, _avanzados_del_formulario())

        flash('Proveedor actualizado.', 'success')
        return redirect(url_for('admin.ai_providers'))

    if request.method == 'GET':
        form.proveedor.data = str(config.proveedor)
        form.es_por_defecto.data = config.es_por_defecto

    return _formulario_de_proveedor(form, config)


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
# AI parameters: per model and task, and the tuner
# ======================================================================
@admin_bp.route('/ajustes/proveedores/<provider_id>/parametros')
@login_required
@require_admin
def ai_profiles(provider_id):
    """The parameter profiles of one provider's models."""
    from app.models.enums import AITask
    from app.services import ai_provider_service
    from app.services.ai import parametros as catalogo
    from app.services.ai.selector import _del_proveedor

    config = ai_provider_service.get_or_404(provider_id)
    editando = None
    if request.args.get('perfil'):
        editando = ai_provider_service.get_profile_or_404(config, request.args['perfil'])

    return render_template(
        'admin/ai_profiles.html',
        provider=config,
        perfiles=ai_provider_service.list_profiles(config),
        editando=editando,
        del_proveedor=catalogo.describir(_del_proveedor(config)),
        parametros_catalogo=catalogo.aplicables(config.proveedor),
        tareas=list(AITask),
        describir=catalogo.describir,
    )


@admin_bp.route('/ajustes/proveedores/<provider_id>/parametros', methods=['POST'])
@login_required
@require_admin
def save_ai_profile(provider_id):
    """Create or replace a model's profile."""
    from app.services import ai_provider_service
    from app.services.ai import parametros as catalogo

    config = ai_provider_service.get_or_404(provider_id)
    try:
        perfil = ai_provider_service.save_profile(
            current_user._get_current_object(), config,
            modelo=request.form.get('modelo'),
            tarea=request.form.get('tarea') or None,
            parametros=catalogo.desde_formulario(request.form),
        )
    except AppError as error:
        db.session.rollback()
        flash(error.mensaje, 'danger')
        return redirect(url_for('admin.ai_profiles', provider_id=config.id))

    flash('Perfil guardado.' if perfil else
          'El perfil no tenía ningún valor y se ha eliminado.', 'success')
    return redirect(url_for('admin.ai_profiles', provider_id=config.id))


@admin_bp.route('/ajustes/proveedores/<provider_id>/parametros/<profile_id>/eliminar',
                methods=['POST'])
@login_required
@require_admin
def delete_ai_profile(provider_id, profile_id):
    from app.services import ai_provider_service

    config = ai_provider_service.get_or_404(provider_id)
    perfil = ai_provider_service.get_profile_or_404(config, profile_id)
    ai_provider_service.delete_profile(current_user._get_current_object(), perfil)
    flash('Perfil eliminado.', 'info')
    return redirect(url_for('admin.ai_profiles', provider_id=config.id))


@admin_bp.route('/ajustes/ia/autoajuste')
@login_required
@require_admin
def ai_autotune():
    """What each task is sent, and the parameter searches."""
    from app.models.enums import AITask
    from app.services import ai_provider_service, autotune_service
    from app.services.ai import explicar
    from app.services.ai import parametros as catalogo

    ajustables = [p for p in catalogo.CATALOGO if p.rejilla]
    return render_template(
        'admin/ai_autotune.html',
        efectivos=[(tarea, explicar(tarea)) for tarea in AITask],
        evaluables=autotune_service.tareas_evaluables(),
        proveedores=ai_provider_service.list_providers(),
        ajustables=ajustables,
        familias={c.value: catalogo.familia(c.value) or '' for c in AIProviderCode},
        ajustes=autotune_service.recientes(),
        presupuesto_por_defecto=autotune_service.PRESUPUESTO_POR_DEFECTO,
        presupuesto_maximo=autotune_service.PRESUPUESTO_MAXIMO,
        repeticiones_maximas=autotune_service.REPETICIONES_MAXIMAS,
        mejora_minima=autotune_service.MEJORA_MINIMA_PARA_APLICAR,
    )


@admin_bp.route('/ajustes/ia/autoajuste', methods=['POST'])
@login_required
@require_admin
def launch_ai_autotune():
    from app.services import ai_provider_service, autotune_service
    from app.services.ai import parametros as catalogo

    provider_id = request.form.get('provider_config_id') or None
    try:
        config = ai_provider_service.get_or_404(provider_id) if provider_id else None
        claves = request.form.getlist('claves') or None
        if config is not None and claves:
            # The form offers every tunable parameter; the ones this provider
            # does not take are hidden there and ignored here.
            admitidos = {p.clave for p in catalogo.aplicables(config.proveedor)}
            claves = [c for c in claves if c in admitidos] or None
        ajuste = autotune_service.lanzar(
            current_user._get_current_object(),
            request.form.get('tarea'),
            config=config,
            modelo=request.form.get('modelo') or None,
            claves=claves,
            presupuesto=request.form.get('presupuesto', type=int),
            repeticiones=request.form.get('repeticiones', type=int),
            aplicar_si_mejora=bool(request.form.get('aplicar_si_mejora')),
        )
    except AppError as error:
        db.session.rollback()
        flash(error.mensaje, 'danger')
        return redirect(url_for('admin.ai_autotune'))

    flash('Autoajuste lanzado. Esta página muestra su progreso.', 'success')
    return redirect(url_for('admin.ai_autotune_detail', ajuste_id=ajuste.id))


@admin_bp.route('/ajustes/ia/autoajuste/<ajuste_id>')
@login_required
@require_admin
def ai_autotune_detail(ajuste_id):
    from app.services import autotune_service
    from app.services.ai import parametros as catalogo

    ajuste = autotune_service.get_or_404(ajuste_id)
    return render_template(
        'admin/ai_autotune_detail.html',
        ajuste=ajuste,
        parado=autotune_service.parece_parado(ajuste),
        parado_minutos=autotune_service.PARADO_TRAS_MINUTOS,
        describir=catalogo.describir,
        mejora_minima=autotune_service.MEJORA_MINIMA_PARA_APLICAR,
    )


@admin_bp.route('/ajustes/ia/autoajuste/<ajuste_id>/estado')
@login_required
@require_admin
@limiter.limit('600 per hour')
def ai_autotune_status(ajuste_id):
    """How far a search has got, for the detail page to poll.

    Its own limit because the page asks every 15 seconds while a search runs
    -- 240 requests an hour, against a default allowance of 100 per route.
    Reloading the whole page on that schedule got the administrator a 429
    half an hour into a search; asking this instead, the page only reloads
    when there is a new trial to show.
    """
    from app.services import autotune_service

    ajuste = autotune_service.get_or_404(ajuste_id)
    return jsonify({
        'estado': str(ajuste.estado),
        'activo': ajuste.activo,
        'ensayos': len(ajuste.ensayos or []),
        'parado': autotune_service.parece_parado(ajuste),
    })


def _accion_de_autoajuste(ajuste_id, accion, mensaje):
    from app.services import autotune_service

    ajuste = autotune_service.get_or_404(ajuste_id)
    try:
        getattr(autotune_service, accion)(current_user._get_current_object(), ajuste)
        flash(mensaje, 'success')
    except AppError as error:
        db.session.rollback()
        flash(error.mensaje, 'danger')
    return redirect(url_for('admin.ai_autotune_detail', ajuste_id=ajuste.id))


@admin_bp.route('/ajustes/ia/autoajuste/<ajuste_id>/aplicar', methods=['POST'])
@login_required
@require_admin
def apply_ai_autotune(ajuste_id):
    return _accion_de_autoajuste(
        ajuste_id, 'aplicar', 'Aplicado: la tarea usa ya estos parámetros con este modelo.',
    )


@admin_bp.route('/ajustes/ia/autoajuste/<ajuste_id>/revertir', methods=['POST'])
@login_required
@require_admin
def revert_ai_autotune(ajuste_id):
    return _accion_de_autoajuste(
        ajuste_id, 'revertir', 'Revertido: se ha restaurado el perfil anterior.',
    )


@admin_bp.route('/ajustes/ia/autoajuste/<ajuste_id>/reanudar', methods=['POST'])
@login_required
@require_admin
def resume_ai_autotune(ajuste_id):
    return _accion_de_autoajuste(
        ajuste_id, 'reanudar', 'Reanudado desde el último ensayo guardado.',
    )


@admin_bp.route('/ajustes/ia/autoajuste/<ajuste_id>/cancelar', methods=['POST'])
@login_required
@require_admin
def cancel_ai_autotune(ajuste_id):
    return _accion_de_autoajuste(ajuste_id, 'cancelar', 'Autoajuste cancelado.')


# ======================================================================
# Backups
# ======================================================================
@admin_bp.route('/copias')
@login_required
@require_admin
def backups():
    """Make a full backup, download it, or restore one."""
    from app.services import backup_service

    return render_template(
        'admin/backups.html',
        trabajos=backup_service.recientes(),
        longitud_minima=backup_service.LONGITUD_MINIMA_FRASE,
        confirmacion=backup_service.CONFIRMACION,
        tamano_maximo=backup_service.TAMANO_MAXIMO_IMPORTACION,
        activo=any(t.activo for t in backup_service.recientes(5)),
    )


@admin_bp.route('/copias/crear', methods=['POST'])
@login_required
@require_admin
def create_backup():
    from app.services import backup_service

    try:
        backup_service.solicitar_copia(
            current_user._get_current_object(),
            request.form.get('frase', ''), request.form.get('frase2', ''),
        )
        flash('Copia en preparación. Aparecerá aquí para descargarla al terminar.',
              'success')
    except AppError as error:
        db.session.rollback()
        flash(error.mensaje, 'danger')
    return redirect(url_for('admin.backups'))


@admin_bp.route('/copias/importar', methods=['POST'])
@csrf.exempt
@login_required
@require_admin
def import_backup():
    """Receive a backup ZIP to restore.

    Exempt from the global CSRF hook and checked here instead: the hook reads
    the form before the view runs, and reading the form enforces the 32 MB
    request limit -- which a backup with its documents always exceeds. The
    limit is raised first, then the token is checked, so the protection is
    the same and only the order changes.
    """
    from flask_wtf.csrf import ValidationError as CSRFError
    from flask_wtf.csrf import validate_csrf

    from app.services import backup_service

    request.max_content_length = backup_service.TAMANO_MAXIMO_IMPORTACION
    try:
        if current_app.config.get('WTF_CSRF_ENABLED', True):
            validate_csrf(request.form.get('csrf_token'))
    except CSRFError:
        flash('La sesión del formulario ha caducado. Vuelva a intentarlo.', 'danger')
        return redirect(url_for('admin.backups'))

    try:
        backup_service.solicitar_restauracion(
            current_user._get_current_object(),
            request.files.get('fichero'),
            request.form.get('frase', ''),
            request.form.get('confirmacion', ''),
        )
        flash('Copia comprobada. La restauración está en marcha; al terminar '
              'puede que tenga que volver a iniciar sesión.', 'warning')
    except AppError as error:
        db.session.rollback()
        flash(error.mensaje, 'danger')
    return redirect(url_for('admin.backups'))


@admin_bp.route('/copias/<trabajo_id>/descargar')
@login_required
@require_admin
def download_backup(trabajo_id):
    from flask import Response, stream_with_context

    from app.services import backup_service

    trabajo = backup_service.get_or_404(trabajo_id)
    try:
        flujo = backup_service.descargar(current_user._get_current_object(), trabajo)
    except AppError as error:
        flash(error.mensaje, 'danger')
        return redirect(url_for('admin.backups'))

    cabeceras = {
        'Content-Disposition': f'attachment; filename="{trabajo.nombre_fichero}"',
        'Cache-Control': 'no-store',
    }
    if trabajo.tamano:
        cabeceras['Content-Length'] = str(trabajo.tamano)
    return Response(stream_with_context(flujo), mimetype='application/zip',
                    headers=cabeceras)


@admin_bp.route('/copias/<trabajo_id>/eliminar', methods=['POST'])
@login_required
@require_admin
def delete_backup(trabajo_id):
    from app.services import backup_service

    try:
        backup_service.eliminar(current_user._get_current_object(),
                                backup_service.get_or_404(trabajo_id))
        flash('Copia eliminada del servidor.', 'info')
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('admin.backups'))


@admin_bp.route('/copias/<trabajo_id>/estado')
@login_required
@require_admin
@limiter.limit('600 per hour')
def backup_status(trabajo_id):
    """Polled by the page while a job runs. Own limit: see ai_autotune_status."""
    from app.services import backup_service

    return jsonify(backup_service.get_or_404(trabajo_id).to_dict())


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
