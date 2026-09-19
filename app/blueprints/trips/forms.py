"""Trip forms."""
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateTimeLocalField,
    DecimalField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.models.enums import TravelerRole, TripPurpose, TripStatus

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
