"""Authentication forms."""
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, Optional


class LoginForm(FlaskForm):
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


class ChangePasswordForm(FlaskForm):
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


class ProfileForm(FlaskForm):
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
