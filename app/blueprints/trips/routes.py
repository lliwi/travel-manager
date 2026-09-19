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
    DestinationForm,
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
from app.utils.decorators import require_permiso, require_trip_access
from app.utils.errors import AppError

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
        form.inicio_tz.data = current_app.config['DEFAULT_TIMEZONE']
        form.fin_tz.data = current_app.config['DEFAULT_TIMEZONE']

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
