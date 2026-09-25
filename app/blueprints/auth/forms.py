"""Authentication forms."""
from wtforms import (
    BooleanField,
    DateField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, EqualTo, Length, Optional

from app.utils.forms import Formulario


class LoginForm(Formulario):
    """Credential entry."""

    username = StringField(
        'Usuario o correo electrónico',
        validators=[DataRequired(message='Introduzca su usuario.')],
        render_kw={'autocomplete': 'username', 'autofocus': True},
    )
    password = PasswordField(
        'Contraseña',
        validators=[DataRequired(message='Introduzca su contraseña.')],
        render_kw={'autocomplete': 'current-password'},
    )
    remember_me = BooleanField('Mantener la sesión iniciada')
    submit = SubmitField('Entrar')


class ChangePasswordForm(Formulario):
    """Password change from the profile screen."""

    current_password = PasswordField(
        'Contraseña actual',
        validators=[Optional()],
        render_kw={'autocomplete': 'current-password'},
    )
    new_password = PasswordField(
        'Nueva contraseña',
        validators=[
            DataRequired(message='Introduzca la nueva contraseña.'),
            Length(min=12, message='La contraseña debe tener al menos 12 caracteres.'),
        ],
        render_kw={'autocomplete': 'new-password'},
    )
    confirm_password = PasswordField(
        'Repita la nueva contraseña',
        validators=[
            DataRequired(message='Repita la nueva contraseña.'),
            EqualTo('new_password', message='Las contraseñas no coinciden.'),
        ],
        render_kw={'autocomplete': 'new-password'},
    )
    submit = SubmitField('Cambiar contraseña')


class MFAForm(Formulario):
    """The six digits, or a recovery code.

    One field for both because they are the same question -- «prove it is
    you» -- and asking somebody to first classify what they are holding is a
    step that only exists for our convenience.
    """

    codigo = StringField(
        'Código',
        validators=[DataRequired(message='Escriba el código.'), Length(max=32)],
        description='Los seis dígitos de su aplicación, o uno de sus códigos '
                    'de recuperación.',
        render_kw={'autocomplete': 'one-time-code', 'autofocus': True,
                   'inputmode': 'text'},
    )
    submit = SubmitField('Entrar')


class ProfileForm(Formulario):
    """Editable profile fields."""

    nombre = StringField('Nombre', validators=[DataRequired(), Length(max=150)])
    apellidos = StringField('Apellidos', validators=[Optional(), Length(max=200)])
    telefono = StringField('Teléfono', validators=[Optional(), Length(max=50)])
    puesto = StringField('Puesto', validators=[Optional(), Length(max=150)])
    departamento = StringField('Departamento', validators=[Optional(), Length(max=150)])
    zona_horaria = StringField(
        'Zona horaria', validators=[Optional(), Length(max=64)],
        description='Nombre IANA, por ejemplo Europe/Madrid.',
    )
    submit = SubmitField('Guardar cambios')


class TravelerDocumentForm(Formulario):
    """A passport, visa or policy somebody carries.

    The number is write-only: it is never rendered back, so leaving it empty
    when editing means «unchanged» rather than «delete it». Only its last four
    characters are shown anywhere.
    """

    tipo = SelectField('Documento', validators=[DataRequired()])
    numero = StringField(
        'Número', validators=[Optional(), Length(max=60)],
        description='Se guarda cifrado. Al editar, déjelo en blanco para '
                    'conservar el que ya hay.',
        render_kw={'autocomplete': 'off'},
    )
    pais_emisor = StringField(
        'País emisor', validators=[Optional(), Length(min=2, max=2)],
        description='Código ISO de dos letras, por ejemplo ES.',
    )
    fecha_emision = DateField('Fecha de emisión', validators=[Optional()])
    fecha_caducidad = DateField(
        'Fecha de caducidad', validators=[Optional()],
        description='De esta fecha salen los avisos de caducidad próxima.',
    )
    notas = TextAreaField('Notas', validators=[Optional(), Length(max=2000)])
    submit = SubmitField('Guardar')
