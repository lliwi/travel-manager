"""Trip forms."""
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    DecimalField,
    IntegerField,
    MultipleFileField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.models.enums import (
    SegmentType,
    ServiceType,
    TravelerRole,
    TripPurpose,
    TripStatus,
)

#: HTML datetime-local inputs submit this format.
DATETIME_FORMAT = '%Y-%m-%dT%H:%M'


def _optional_choices(enum_cls, blank='— Sin especificar —'):
    return [('', blank)] + enum_cls.choices()


class TripForm(FlaskForm):
    """Create or edit a trip.

    Dates are captured as *local* wall-clock values plus a timezone; the service
    layer derives the UTC instant. Capturing a bare datetime with no zone is what
    makes connection calculations wrong later.
    """

    titulo = StringField(
        'Título o referencia interna',
        validators=[DataRequired(message='Indique un título.'), Length(max=300)],
    )
    estado = SelectField('Estado', choices=TripStatus.choices(), validators=[DataRequired()])
    finalidad = SelectField(
        'Finalidad', choices=_optional_choices(TripPurpose), validators=[Optional()]
    )
    finalidad_detalle = StringField(
        'Detalle de la finalidad', validators=[Optional(), Length(max=500)]
    )

    inicio_local = DateTimeLocalField(
        'Inicio', format=DATETIME_FORMAT, validators=[Optional()],
    )
    inicio_tz = StringField(
        'Zona horaria de inicio', validators=[Optional(), Length(max=64)],
        description='Nombre IANA, por ejemplo Europe/Madrid.',
    )
    fin_local = DateTimeLocalField(
        'Fin', format=DATETIME_FORMAT, validators=[Optional()],
    )
    fin_tz = StringField(
        'Zona horaria de fin', validators=[Optional(), Length(max=64)],
    )

    gestor_id = SelectField('Gestor responsable', validators=[Optional()])
    observaciones = TextAreaField('Observaciones', validators=[Optional()])

    coste_estimado = DecimalField(
        'Coste estimado', places=2, validators=[Optional(), NumberRange(min=0)]
    )
    moneda = StringField('Moneda', validators=[Optional(), Length(min=3, max=3)])

    #: Booking documents attached while creating the trip. Each one starts the
    #: usual pipeline -- validation, antivirus, OCR, classification, extraction --
    #: so the itinerary can come out of the documents instead of being retyped.
    #: The accepted extensions here only keep the file picker tidy; the real
    #: check inspects the file's own bytes once it is uploaded.
    documentos = MultipleFileField(
        'Documentos de reserva',
        validators=[Optional()],
        description=(
            'Opcional. Adjunte las reservas que ya tenga (PDF, imágenes o '
            'mensajes EML) y el sistema extraerá de ellas los datos del '
            'itinerario. Los revisará antes de que se apliquen.'
        ),
        render_kw={'accept': '.pdf,.jpg,.jpeg,.png,.tiff,.tif,.eml'},
    )

    submit = SubmitField('Guardar')

    def validate(self, extra_validators=None):
        """Reject a trip that ends before it starts, and invalid timezones."""
        if not super().validate(extra_validators):
            return False

        from app.utils.timeutil import is_valid_timezone

        ok = True
        for field in (self.inicio_tz, self.fin_tz):
            if field.data and not is_valid_timezone(field.data):
                field.errors.append('Zona horaria no válida (use un nombre IANA).')
                ok = False

        if self.inicio_local.data and self.fin_local.data:
            if self.fin_local.data < self.inicio_local.data:
                self.fin_local.errors.append(
                    'La fecha de fin no puede ser anterior a la de inicio.'
                )
                ok = False
        return ok


class TravelerForm(FlaskForm):
    """Assign a person to a trip."""

    user_id = SelectField(
        'Persona viajera',
        validators=[DataRequired(message='Seleccione a la persona viajera.')],
    )
    rol_en_viaje = SelectField(
        'Rol en el viaje', choices=TravelerRole.choices(), validators=[DataRequired()]
    )
    desde_local = DateTimeLocalField(
        'Desde', format=DATETIME_FORMAT, validators=[Optional()],
        description='Deje en blanco si participa durante todo el viaje.',
    )
    hasta_local = DateTimeLocalField(
        'Hasta', format=DATETIME_FORMAT, validators=[Optional()],
    )
    observaciones = TextAreaField('Observaciones', validators=[Optional()])
    submit = SubmitField('Asignar')


class DestinationForm(FlaskForm):
    """Add a destination to a trip."""

    ciudad = StringField(
        'Ciudad', validators=[DataRequired(message='Indique la ciudad.'), Length(max=160)]
    )
    pais_codigo = SelectField('País', validators=[Optional()])
    zona_horaria = StringField(
        'Zona horaria', validators=[Optional(), Length(max=64)],
        description='Si se deja en blanco se deduce del país.',
    )
    inicio_local = DateTimeLocalField('Llegada', format=DATETIME_FORMAT, validators=[Optional()])
    fin_local = DateTimeLocalField('Salida', format=DATETIME_FORMAT, validators=[Optional()])
    es_escala = BooleanField('Es una escala')
    orden = IntegerField('Orden', validators=[Optional(), NumberRange(min=0)])
    observaciones = TextAreaField('Observaciones', validators=[Optional()])
    submit = SubmitField('Añadir destino')


class TripFilterForm(FlaskForm):
    """Trip list filters. GET form, so CSRF is not applicable."""

    class Meta:
        csrf = False

    buscar = StringField('Buscar', validators=[Optional(), Length(max=200)])
    estado = SelectField(
        'Estado', choices=[('', 'Todos los estados')] + TripStatus.choices(),
        validators=[Optional()],
    )
    orden = SelectField(
        'Ordenar por',
        choices=[
            ('inicio_desc', 'Inicio (más reciente primero)'),
            ('inicio_asc', 'Inicio (más próximo primero)'),
            ('referencia_desc', 'Referencia'),
            ('creado_desc', 'Fecha de alta'),
        ],
        default='inicio_desc',
        validators=[Optional()],
    )


def _tz_field(label, description=None):
    """A timezone field that refuses a name the runtime does not know.

    Rejecting «CEST» or a typo here is the cheapest place to do it: accepted,
    it would produce an instant whose UTC column is wrong and every connection
    margin computed from it with it.
    """
    from wtforms.validators import ValidationError as WTFValidationError

    def _valid(form, field):
        from app.utils.timeutil import is_valid_timezone

        if field.data and not is_valid_timezone(field.data):
            raise WTFValidationError(
                'Zona horaria desconocida. Use un nombre IANA, '
                'por ejemplo «Europe/Madrid».'
            )

    return StringField(
        label,
        validators=[Optional(), Length(max=64), _valid],
        description=description or 'En blanco: se usa la del viaje.',
    )


class ItineraryItemForm(FlaskForm):
    """Fields every itinerary item shares.

    Manual entry captures wall-clock time plus its zone, exactly as extraction
    does: the service layer derives the UTC column from the pair, and nothing
    else ever writes it.
    """

    trip_traveler_id = SelectField(
        'Persona viajera', validators=[Optional()],
        description='En blanco: aplica a todo el viaje.',
    )
    localizador = StringField('Localizador', validators=[Optional(), Length(max=60)])
    proveedor = StringField('Proveedor', validators=[Optional(), Length(max=200)])
    observaciones = TextAreaField('Observaciones', validators=[Optional()])
    submit = SubmitField('Guardar')


class SegmentForm(ItineraryItemForm):
    """A flight, train, bus or ferry leg."""

    tipo = SelectField('Medio', choices=_optional_choices(SegmentType), validators=[Optional()])
    numero = StringField('Número', validators=[Optional(), Length(max=30)])
    operado_por = StringField('Operado por', validators=[Optional(), Length(max=200)])
    clase = StringField('Clase', validators=[Optional(), Length(max=60)])
    asiento = StringField('Asiento', validators=[Optional(), Length(max=30)])

    origen_codigo = StringField('Código de origen', validators=[Optional(), Length(max=10)])
    origen_nombre = StringField('Origen', validators=[Optional(), Length(max=200)])
    origen_ciudad = StringField('Ciudad de origen', validators=[Optional(), Length(max=160)])
    terminal_origen = StringField('Terminal de origen', validators=[Optional(), Length(max=30)])
    salida_local = DateTimeLocalField('Salida', format=DATETIME_FORMAT, validators=[Optional()])
    salida_tz = _tz_field('Zona horaria de salida')

    destino_codigo = StringField('Código de destino', validators=[Optional(), Length(max=10)])
    destino_nombre = StringField('Destino', validators=[Optional(), Length(max=200)])
    destino_ciudad = StringField('Ciudad de destino', validators=[Optional(), Length(max=160)])
    terminal_destino = StringField('Terminal de destino', validators=[Optional(), Length(max=30)])
    llegada_local = DateTimeLocalField('Llegada', format=DATETIME_FORMAT, validators=[Optional()])
    llegada_tz = _tz_field('Zona horaria de llegada')


class AccommodationForm(ItineraryItemForm):
    """A hotel or apartment stay."""

    nombre = StringField(
        'Nombre', validators=[DataRequired(message='Indique el alojamiento.'), Length(max=200)]
    )
    direccion = StringField('Dirección', validators=[Optional(), Length(max=300)])
    ciudad = StringField('Ciudad', validators=[Optional(), Length(max=160)])
    telefono = StringField('Teléfono', validators=[Optional(), Length(max=60)])
    email = StringField('Correo', validators=[Optional(), Length(max=200)])

    check_in_local = DateTimeLocalField(
        'Entrada', format=DATETIME_FORMAT, validators=[Optional()]
    )
    check_in_tz = _tz_field('Zona horaria de entrada')
    check_out_local = DateTimeLocalField(
        'Salida', format=DATETIME_FORMAT, validators=[Optional()]
    )
    check_out_tz = _tz_field('Zona horaria de salida')

    numero_habitaciones = IntegerField(
        'Habitaciones', validators=[Optional(), NumberRange(min=1)]
    )
    tipo_habitacion = StringField('Tipo de habitación', validators=[Optional(), Length(max=120)])
    regimen = StringField('Régimen', validators=[Optional(), Length(max=120)])


class VehicleRentalForm(ItineraryItemForm):
    """A rental car."""

    categoria = StringField('Categoría', validators=[Optional(), Length(max=120)])
    modelo = StringField('Modelo', validators=[Optional(), Length(max=160)])
    matricula = StringField('Matrícula', validators=[Optional(), Length(max=30)])
    transmision = StringField('Transmisión', validators=[Optional(), Length(max=60)])

    recogida_lugar = StringField('Lugar de recogida', validators=[Optional(), Length(max=200)])
    recogida_ciudad = StringField('Ciudad de recogida', validators=[Optional(), Length(max=160)])
    recogida_local = DateTimeLocalField(
        'Recogida', format=DATETIME_FORMAT, validators=[Optional()]
    )
    recogida_tz = _tz_field('Zona horaria de recogida')

    devolucion_lugar = StringField('Lugar de devolución', validators=[Optional(), Length(max=200)])
    devolucion_ciudad = StringField(
        'Ciudad de devolución', validators=[Optional(), Length(max=160)]
    )
    devolucion_local = DateTimeLocalField(
        'Devolución', format=DATETIME_FORMAT, validators=[Optional()]
    )
    devolucion_tz = _tz_field('Zona horaria de devolución')

    conductor_nombre = StringField('Conductor', validators=[Optional(), Length(max=200)])
    franquicia = StringField('Franquicia', validators=[Optional(), Length(max=120)])


class OtherServiceForm(ItineraryItemForm):
    """Anything else on the itinerary: a transfer, an insurance, a visa."""

    tipo = SelectField('Tipo', choices=_optional_choices(ServiceType), validators=[Optional()])
    nombre = StringField(
        'Nombre', validators=[DataRequired(message='Indique el servicio.'), Length(max=200)]
    )
    lugar = StringField('Lugar', validators=[Optional(), Length(max=200)])
    ciudad = StringField('Ciudad', validators=[Optional(), Length(max=160)])

    inicio_local = DateTimeLocalField('Inicio', format=DATETIME_FORMAT, validators=[Optional()])
    inicio_tz = _tz_field('Zona horaria de inicio')
    fin_local = DateTimeLocalField('Fin', format=DATETIME_FORMAT, validators=[Optional()])
    fin_tz = _tz_field('Zona horaria de fin')


#: The form and the field groups the template renders, per itinerary kind.
ITINERARY_FORMS = {
    'segmento': (SegmentForm, 'tramo'),
    'alojamiento': (AccommodationForm, 'alojamiento'),
    'vehiculo': (VehicleRentalForm, 'vehículo'),
    'servicio': (OtherServiceForm, 'servicio'),
}
