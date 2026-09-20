"""Trip routes (Jinja surface).

Every route is guarded by ``require_trip_access``, which delegates to
``authorization_service`` -- the same decision function ``/api/v1`` uses.
"""
import logging
import uuid

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.blueprints.trips import trips_bp
from app.blueprints.trips.forms import (
    ITINERARY_FORMS,
    AssistantForm,
    DestinationForm,
    PlanningForm,
    TravelerForm,
    TripFilterForm,
    TripForm,
)
from app.extensions import db
from app.models.catalog import Country
from app.models.enums import DocumentType, RoleCode, TripPurpose, TripStatus
from app.models.user import User
from app.services import trip_service
from app.services.authorization_service import Permiso, permissions_for
from app.utils.decorators import rate_limited, require_permiso, require_trip_access
from app.utils.errors import AppError, ResourceNotFound

logger = logging.getLogger(__name__)


def _user_choices(role_codes=None):
    """Active users for a select field, optionally filtered by role."""
    query = User.query.filter(User.is_deleted.is_(False))
    users = query.order_by(User.nombre, User.apellidos).all()
    if role_codes:
        users = [u for u in users if u.has_any_role(*role_codes)]
    return [(str(u.id), f'{u.nombre_completo} ({u.username})') for u in users]


def _adjuntar_documentos(actor, trip, archivos):
    """Attach the documents submitted with the trip form.

    Returns ``(adjuntados, fallidos)`` where ``fallidos`` is a list of
    ``(nombre, motivo)``. Failures are collected rather than raised: one
    unreadable file should not cost the manager the rest of the upload, nor the
    trip itself.
    """
    from app.services import document_service

    adjuntados = 0
    fallidos = []

    for archivo in archivos or []:
        # An empty file input submits a FileStorage with no filename.
        if archivo is None or not getattr(archivo, 'filename', ''):
            continue
        try:
            document_service.upload(
                actor, trip, archivo, tipo=DocumentType.RESERVA
            )
            adjuntados += 1
        except AppError as error:
            logger.info(
                'No se pudo adjuntar «%s» al viaje %s: %s',
                archivo.filename, trip.referencia, error.mensaje,
            )
            fallidos.append((archivo.filename, error.mensaje))
        except Exception:
            logger.exception(
                'Fallo inesperado al adjuntar «%s» al viaje %s',
                archivo.filename, trip.referencia,
            )
            fallidos.append((archivo.filename, 'error inesperado al procesarlo'))

    return adjuntados, fallidos


def _country_choices():
    countries = Country.query.order_by(Country.nombre).all()
    return [('', '— Sin especificar —')] + [(c.codigo, c.nombre) for c in countries]


@trips_bp.route('/')
@login_required
def index():
    """List the trips this actor may see."""
    form = TripFilterForm(request.args)
    page = request.args.get('page', 1, type=int)

    pagination = trip_service.list_trips(
        current_user._get_current_object(),
        page=page,
        per_page=current_app.config['ITEMS_PER_PAGE'],
        estado=form.estado.data or None,
        buscar=form.buscar.data or None,
        orden=form.orden.data or 'inicio_desc',
    )

    return render_template(
        'trips/index.html',
        form=form,
        trips=pagination.items,
        pagination=pagination,
        TripStatus=TripStatus,
    )


@trips_bp.route('/nuevo', methods=['GET', 'POST'])
@login_required
@require_permiso(Permiso.CREAR_VIAJE)
def create():
    """Create a trip (specification flow 5.1 step 1)."""
    form = TripForm()
    form.gestor_id.choices = [('', '— Yo mismo —')] + _user_choices(
        (RoleCode.GESTOR, RoleCode.ADMINISTRADOR)
    )

    if form.validate_on_submit():
        actor = current_user._get_current_object()
        gestor = None
        if form.gestor_id.data:
            gestor = db.session.get(User, uuid.UUID(form.gestor_id.data))

        try:
            trip = trip_service.create_trip(
                actor,
                titulo=form.titulo.data,
                gestor=gestor,
                estado=TripStatus.coerce(form.estado.data, TripStatus.BORRADOR),
                finalidad=TripPurpose.coerce(form.finalidad.data),
                finalidad_detalle=form.finalidad_detalle.data or None,
                observaciones=form.observaciones.data or None,
                inicio_local=form.inicio_local.data,
                inicio_tz=form.inicio_tz.data or None,
                fin_local=form.fin_local.data,
                fin_tz=form.fin_tz.data or None,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return render_template('trips/form.html', form=form, trip=None)

        flash(f'Viaje {trip.referencia} creado.', 'success')

        # Documents are attached after the trip exists, because each one needs
        # a trip to hang off. A file that fails does not undo the trip: the
        # manager keeps what was created and is told which file to retry.
        adjuntados, fallidos = _adjuntar_documentos(actor, trip, form.documentos.data)

        if adjuntados:
            flash(
                f'{adjuntados} documento(s) recibido(s). Se están procesando en '
                'segundo plano; podrá revisar los datos extraídos en unos '
                'instantes.',
                'info',
            )
        for nombre, motivo in fallidos:
            flash(f'No se pudo adjuntar «{nombre}»: {motivo}', 'warning')

        return redirect(url_for('trips.detail', trip_id=trip.id))

    if request.method == 'GET':
        form.estado.data = TripStatus.BORRADOR.value
        from app.services import settings_service

        zona = settings_service.zona_horaria_por_defecto()
        form.inicio_tz.data = zona
        form.fin_tz.data = zona

    return render_template('trips/form.html', form=form, trip=None)


@trips_bp.route('/<trip_id>')
@login_required
@require_trip_access(Permiso.VER_VIAJE)
def detail(trip_id, trip):
    """Trip detail: itinerary, travellers, documents, alerts and advisories."""
    actor = current_user._get_current_object()
    permisos = permissions_for(actor, trip)

    from app.services import itinerary_service

    timeline = itinerary_service.build_timeline(actor, trip)

    return render_template(
        'trips/detail.html',
        trip=trip,
        timeline=timeline,
        permisos=permisos,
        Permiso=Permiso,
    )


@trips_bp.route('/<trip_id>/editar', methods=['GET', 'POST'])
@login_required
@require_trip_access(Permiso.EDITAR_VIAJE)
def edit(trip_id, trip):
    """Edit a trip."""
    form = TripForm(obj=trip)
    form.gestor_id.choices = [('', '— Sin cambios —')] + _user_choices(
        (RoleCode.GESTOR, RoleCode.ADMINISTRADOR)
    )

    if form.validate_on_submit():
        actor = current_user._get_current_object()
        campos = {
            'titulo': form.titulo.data,
            'estado': TripStatus.coerce(form.estado.data, trip.estado),
            'finalidad': TripPurpose.coerce(form.finalidad.data),
            'finalidad_detalle': form.finalidad_detalle.data or None,
            'observaciones': form.observaciones.data or None,
            'inicio_local': form.inicio_local.data,
            'inicio_tz': form.inicio_tz.data or None,
            'fin_local': form.fin_local.data,
            'fin_tz': form.fin_tz.data or None,
        }
        if form.gestor_id.data:
            campos['gestor_id'] = uuid.UUID(form.gestor_id.data)

        try:
            trip_service.update_trip(actor, trip, **campos)
        except AppError as error:
            flash(error.mensaje, 'danger')
            return render_template('trips/form.html', form=form, trip=trip)

        flash('Viaje actualizado.', 'success')
        return redirect(url_for('trips.detail', trip_id=trip.id))

    if request.method == 'GET':
        form.estado.data = str(trip.estado)
        form.finalidad.data = str(trip.finalidad) if trip.finalidad else ''
        form.gestor_id.data = ''

    return render_template('trips/form.html', form=form, trip=trip)


@trips_bp.route('/<trip_id>/eliminar', methods=['POST'])
@login_required
@require_trip_access(Permiso.ELIMINAR_VIAJE)
def delete(trip_id, trip):
    """Soft-delete a trip."""
    trip_service.delete_trip(current_user._get_current_object(), trip)
    flash(f'Viaje {trip.referencia} eliminado.', 'info')
    return redirect(url_for('trips.index'))


# ======================================================================
# Travellers
# ======================================================================
@trips_bp.route('/<trip_id>/viajeros', methods=['GET', 'POST'])
@login_required
@require_trip_access(Permiso.GESTIONAR_VIAJEROS)
def travelers(trip_id, trip):
    """Assign and unassign travellers (specification flow 5.1 step 2)."""
    form = TravelerForm()
    assigned = {str(t.user_id) for t in trip.active_travelers}
    form.user_id.choices = [
        choice for choice in _user_choices() if choice[0] not in assigned
    ]

    if form.validate_on_submit():
        from app.models.enums import TravelerRole

        try:
            trip_service.add_traveler(
                current_user._get_current_object(),
                trip,
                form.user_id.data,
                rol_en_viaje=TravelerRole.coerce(
                    form.rol_en_viaje.data, TravelerRole.VIAJERO
                ),
                desde_local=form.desde_local.data,
                hasta_local=form.hasta_local.data,
                observaciones=form.observaciones.data or None,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return redirect(url_for('trips.travelers', trip_id=trip.id))

        flash('Persona viajera asignada.', 'success')
        return redirect(url_for('trips.travelers', trip_id=trip.id))

    # Offer the dates the system already knows, but only on a fresh form: on a
    # failed submit the manager's own input is what belongs in the fields.
    origen_fechas = None
    if not form.is_submitted():
        desde, hasta, origen_fechas = trip_service.propose_traveler_window(trip)
        form.desde_local.data = desde
        form.hasta_local.data = hasta

    return render_template(
        'trips/travelers.html', trip=trip, form=form, origen_fechas=origen_fechas
    )


@trips_bp.route('/<trip_id>/viajeros/<user_id>/eliminar', methods=['POST'])
@login_required
@require_trip_access(Permiso.GESTIONAR_VIAJEROS)
def remove_traveler(trip_id, user_id, trip):
    """Unassign a traveller."""
    try:
        trip_service.remove_traveler(current_user._get_current_object(), trip, user_id)
        flash('Persona viajera desasignada.', 'info')
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('trips.travelers', trip_id=trip.id))


# ======================================================================
# Destinations
# ======================================================================
# ======================================================================
# Planning assistant
# ======================================================================
@trips_bp.route('/planificar', methods=['GET', 'POST'])
@login_required
@require_permiso(Permiso.CREAR_VIAJE)
@rate_limited('10 per minute; 60 per hour')
def plan():
    """Suggest how to make a journey, before there is a trip to attach it to.

    Deliberately not a search: this application has no availability or pricing
    connector, so what comes back says which modes make sense and what the
    journey will run into -- never that a particular service exists on a
    particular day at a particular price.
    """
    from app.services import ai_service

    form = PlanningForm()
    plan = None

    if form.validate_on_submit():
        try:
            plan = ai_service.plan_trip(
                actor=current_user._get_current_object(),
                origen=form.origen.data.strip(),
                destino=form.destino.data.strip(),
                ida=form.ida.data.isoformat() if form.ida.data else None,
                vuelta=form.vuelta.data.isoformat() if form.vuelta.data else None,
                viajeros=form.viajeros.data or 1,
                preferencias=(form.preferencias.data or '').strip() or None,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
        except Exception:
            logger.exception('Falló la planificación de viaje')
            flash(
                'El asistente no está disponible en este momento. '
                'Inténtelo de nuevo en unos minutos.',
                'danger',
            )

    return render_template('trips/plan.html', form=form, plan=plan)


# ======================================================================
# Assistant
# ======================================================================
#: Questions worth one click, phrased the way a manager would ask them. They
#: are a shortcut into the same field, not a menu: anything else can be typed.
PREGUNTAS_SUGERIDAS = (
    '¿A qué hora salgo y desde dónde?',
    '¿Cuánto margen tengo entre un trayecto y el siguiente?',
    '¿Dónde me alojo cada noche?',
    '¿Queda alguna noche sin alojamiento?',
    '¿Qué documentación necesito para este destino?',
)


@trips_bp.route('/<trip_id>/asistente', methods=['GET', 'POST'])
@login_required
@require_trip_access(Permiso.CONSULTAR_IA)
@rate_limited('10 per minute; 60 per hour')
def assistant(trip_id, trip):
    """Answer one concrete question about this trip.

    Not a conversation: one question in, one answer out, nothing kept between
    them. The model only ever sees what this asker may already see, because
    ``build_ai_context`` builds its view from the same scoped queries the
    timeline uses -- which is why a traveller cannot ask their way into another
    traveller's itinerary.
    """
    from app.services import ai_service

    form = AssistantForm()
    respuesta = None

    if form.validate_on_submit():
        try:
            respuesta = ai_service.answer_trip_question(
                actor=current_user._get_current_object(),
                trip=trip,
                pregunta=form.pregunta.data.strip(),
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
        except Exception:
            logger.exception('Fallo del asistente en el viaje %s', trip.id)
            flash(
                'El asistente no está disponible en este momento. '
                'Inténtelo de nuevo en unos minutos.',
                'danger',
            )

    return render_template(
        'trips/assistant.html',
        trip=trip, form=form, respuesta=respuesta,
        sugeridas=PREGUNTAS_SUGERIDAS,
    )


# ======================================================================
# Itinerary
# ======================================================================
def _itinerary_form(kind, item=None):
    """The form for an itinerary kind, bound to an item when editing."""
    entry = ITINERARY_FORMS.get(str(kind))
    if entry is None:
        raise ResourceNotFound('Ese tipo de elemento de itinerario no existe.')
    form_cls, etiqueta = entry
    return form_cls(obj=item), etiqueta


def _traveler_choices(trip):
    """Travellers on this trip, plus the "applies to everyone" option."""
    return [('', '— Todo el viaje —')] + [
        (str(t.id), t.user.nombre_completo) for t in trip.active_travelers
    ]


def _itinerary_payload(kind, form):
    """Split the submitted form into instant triples and plain columns.

    The same split ``/api/v1`` does, for the same reason: a timestamp only ever
    reaches the database as a local value plus its zone, so no caller can set a
    UTC column and let the three drift apart.
    """
    from app.services.itinerary_service import ENTITY_MAP

    _, prefixes = ENTITY_MAP[kind]

    instants = {}
    for prefijo in prefixes:
        local = getattr(form, f'{prefijo}_local', None)
        if local is None:
            continue
        tz = getattr(form, f'{prefijo}_tz', None)
        instants[prefijo] = (local.data, (tz.data or None) if tz else None)

    modelo, _ = ENTITY_MAP[kind]
    columnas = modelo.__table__.columns

    omitidos = {'submit', 'csrf_token'}
    campos = {}
    for field in form:
        nombre = field.name
        if nombre in omitidos or nombre.endswith(('_local', '_tz')):
            continue

        valor = field.data
        if isinstance(valor, str):
            valor = valor.strip() or None

        if valor is None:
            columna = columnas.get(nombre)
            # A blank field means "no value", which a nullable column stores as
            # NULL. On a column that is not nullable it means "leave it alone":
            # writing NULL there fails, and an unfilled select is not the
            # manager saying the segment has no kind.
            if columna is not None and not columna.nullable:
                continue

        campos[nombre] = valor

    return instants, campos


@trips_bp.route('/<trip_id>/itinerario/<kind>/nuevo', methods=['GET', 'POST'])
@login_required
@require_trip_access(Permiso.EDITAR_ITINERARIO)
def create_itinerary_item(trip_id, kind, trip):
    """Add an itinerary item by hand."""
    from app.services import itinerary_service

    form, etiqueta = _itinerary_form(kind)
    form.trip_traveler_id.choices = _traveler_choices(trip)

    if form.validate_on_submit():
        instants, campos = _itinerary_payload(kind, form)
        try:
            itinerary_service.create_item(
                current_user._get_current_object(), trip, kind,
                instants=instants, **campos,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
        else:
            flash(f'Se ha añadido el {etiqueta} al itinerario.', 'success')
            return redirect(url_for('trips.detail', trip_id=trip.id))

    return render_template(
        'trips/itinerary_form.html',
        trip=trip, form=form, kind=kind, etiqueta=etiqueta, item=None,
    )


@trips_bp.route('/<trip_id>/itinerario/<kind>/<item_id>/editar', methods=['GET', 'POST'])
@login_required
@require_trip_access(Permiso.EDITAR_ITINERARIO)
def edit_itinerary_item(trip_id, kind, item_id, trip):
    """Edit an itinerary item by hand."""
    from app.services import itinerary_service

    item = itinerary_service.get_item(trip, kind, item_id)
    form, etiqueta = _itinerary_form(kind, item if request.method == 'GET' else None)
    form.trip_traveler_id.choices = _traveler_choices(trip)
    if request.method == 'GET':
        form.trip_traveler_id.data = str(item.trip_traveler_id or '')

    if form.validate_on_submit():
        instants, campos = _itinerary_payload(kind, form)
        try:
            itinerary_service.update_item(
                current_user._get_current_object(), trip, kind, item_id,
                instants=instants, **campos,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
        else:
            flash(f'Se ha actualizado el {etiqueta}.', 'success')
            return redirect(url_for('trips.detail', trip_id=trip.id))

    return render_template(
        'trips/itinerary_form.html',
        trip=trip, form=form, kind=kind, etiqueta=etiqueta, item=item,
    )


@trips_bp.route('/<trip_id>/itinerario/<kind>/<item_id>/eliminar', methods=['POST'])
@login_required
@require_trip_access(Permiso.EDITAR_ITINERARIO)
def delete_itinerary_item(trip_id, kind, item_id, trip):
    """Remove an itinerary item. Soft-deleted, like every other record."""
    from app.services import itinerary_service

    try:
        itinerary_service.delete_item(
            current_user._get_current_object(), trip, kind, item_id
        )
        flash('Elemento del itinerario eliminado.', 'info')
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('trips.detail', trip_id=trip.id))


@trips_bp.route('/<trip_id>/destinos', methods=['GET', 'POST'])
@login_required
@require_trip_access(Permiso.EDITAR_VIAJE)
def destinations(trip_id, trip):
    """Manage the trip's destinations."""
    form = DestinationForm()
    form.pais_codigo.choices = _country_choices()

    if form.validate_on_submit():
        country = (
            Country.query.filter_by(codigo=form.pais_codigo.data).first()
            if form.pais_codigo.data else None
        )
        trip_service.add_destination(
            current_user._get_current_object(),
            trip,
            ciudad=form.ciudad.data,
            pais_codigo=form.pais_codigo.data or None,
            pais_nombre=country.nombre if country else None,
            zona_horaria=(
                form.zona_horaria.data
                or (country.zona_horaria_principal if country else None)
            ),
            inicio_local=form.inicio_local.data,
            fin_local=form.fin_local.data,
            es_escala=form.es_escala.data,
            orden=form.orden.data,
            observaciones=form.observaciones.data or None,
        )
        flash('Destino añadido.', 'success')
        return redirect(url_for('trips.destinations', trip_id=trip.id))

    return render_template('trips/destinations.html', trip=trip, form=form)


@trips_bp.route('/<trip_id>/destinos/<destination_id>/eliminar', methods=['POST'])
@login_required
@require_trip_access(Permiso.EDITAR_VIAJE)
def remove_destination(trip_id, destination_id, trip):
    """Remove a destination."""
    try:
        trip_service.remove_destination(
            current_user._get_current_object(), trip, destination_id
        )
        flash('Destino eliminado.', 'info')
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('trips.destinations', trip_id=trip.id))
