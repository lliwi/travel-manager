"""Administration forms."""
from flask_wtf import FlaskForm
from wtforms import (
    PasswordField,
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField,
    widgets,
)
from wtforms.validators import DataRequired, Email, Length, Optional

from app.models.enums import UserStatus


class MultiCheckboxField(SelectMultipleField):
    """Role selection rendered as checkboxes rather than a multi-select."""

    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


class UserForm(FlaskForm):
    """Create or edit an account."""

    username = StringField(
        'Nombre de usuario',
        validators=[DataRequired(message='Indique el nombre de usuario.'), Length(max=150)],
    )
    email = StringField(
        'Correo electrónico',
        validators=[
            DataRequired(message='Indique el correo electrónico.'),
            Email(message='El correo electrónico no es válido.'),
            Length(max=255),
        ],
    )
    nombre = StringField('Nombre', validators=[DataRequired(), Length(max=150)])
    apellidos = StringField('Apellidos', validators=[Optional(), Length(max=200)])
    puesto = StringField('Puesto', validators=[Optional(), Length(max=150)])
    departamento = StringField('Departamento', validators=[Optional(), Length(max=150)])
    estado = SelectField('Estado', choices=UserStatus.choices(), validators=[Optional()])
    roles = MultiCheckboxField('Roles', validators=[Optional()])
    password = PasswordField(
        'Contraseña',
        validators=[Optional(), Length(min=12, message='Mínimo 12 caracteres.')],
        description='Al editar, deje en blanco para no cambiarla.',
        render_kw={'autocomplete': 'new-password'},
    )
    submit = SubmitField('Guardar')


class AIProviderForm(FlaskForm):
    """Configure an inference endpoint."""

    nombre = StringField('Nombre', validators=[DataRequired(), Length(max=120)])
    proveedor = SelectField('Proveedor', validators=[DataRequired()])
    base_url = StringField('URL base', validators=[Optional(), Length(max=500)])
    modelo_por_defecto = StringField(
        'Modelo por defecto', validators=[Optional(), Length(max=160)]
    )
    api_key = PasswordField(
        'Clave API',
        validators=[Optional()],
        description='Se almacena cifrada. Deje en blanco para conservar la actual.',
        render_kw={'autocomplete': 'new-password'},
    )
    submit = SubmitField('Guardar')
