"""Administration forms."""
from wtforms import (
    BooleanField,
    DecimalField,
    IntegerField,
    PasswordField,
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField,
    widgets,
)
from wtforms.validators import (
    DataRequired,
    Length,
    NumberRange,
    Optional,
)

from app.models.enums import AIProviderCode, AITask, UserStatus
from app.utils.forms import Formulario


def _correo_valido(form, field):
    """Defer to the service, so the form cannot be stricter than the system.

    It was: the form refused the ``@corp.test`` addresses the application's own
    seed issues, which made those accounts impossible to edit.
    """
    from wtforms.validators import ValidationError as WTFValidationError

    from app.services.user_service import normalizar_email
    from app.utils.errors import ValidationError

    if not field.data:
        return
    try:
        normalizar_email(field.data)
    except ValidationError as exc:
        raise WTFValidationError(exc.mensaje) from exc



class MultiCheckboxField(SelectMultipleField):
    """Role selection rendered as checkboxes rather than a multi-select."""

    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


class UserForm(Formulario):
    """Create or edit an account."""

    username = StringField(
        'Nombre de usuario',
        validators=[DataRequired(message='Indique el nombre de usuario.'), Length(max=150)],
    )
    email = StringField(
        'Correo electrónico',
        validators=[
            DataRequired(message='Indique el correo electrónico.'),
            _correo_valido,
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


class AIProviderForm(Formulario):
    """Configure an inference endpoint.

    The stored API key is never rendered back: on an edit, leaving the field
    empty keeps the key that is already there.
    """

    nombre = StringField(
        'Nombre', validators=[DataRequired(message='Indique un nombre.'), Length(max=120)],
        description='Cómo aparecerá en los listados, p. ej. «Ollama local».',
    )
    proveedor = SelectField(
        'Proveedor', choices=AIProviderCode.choices(), validators=[DataRequired()]
    )
    base_url = StringField(
        'URL del endpoint', validators=[Optional(), Length(max=500)],
        description='Se rellena sola al elegir el proveedor; puede ajustarla.',
    )
    modelo_por_defecto = StringField(
        'Modelo', validators=[Optional(), Length(max=160)],
        description='Se eligen de los que publica el endpoint; «Otro…» '
                    'permite escribir uno que todavía no publique.',
    )
    api_key = PasswordField(
        'Clave API',
        validators=[Optional()],
        description=(
            'Solo para proveedores externos. Se guarda cifrada y nunca se '
            'vuelve a mostrar. Al editar, déjela en blanco para conservarla.'
        ),
        render_kw={'autocomplete': 'new-password'},
    )
    activo = BooleanField('Activo', default=True)
    es_por_defecto = BooleanField(
        'Usar por defecto',
        description='Atenderá las tareas que no tengan un proveedor asignado.',
    )
    timeout_segundos = IntegerField(
        'Tiempo de espera (segundos)', default=120,
        validators=[Optional(), NumberRange(min=5, max=900)],
    )
    max_tokens = IntegerField(
        'Máximo de tokens', default=2048,
        validators=[Optional(), NumberRange(min=64, max=32768)],
    )
    sin_razonamiento = BooleanField(
        'Desactivar el razonamiento',
        description=(
            'Solo si el modelo lo admite; se comprueba antes de pedirlo. Un '
            'modelo que razona gasta su presupuesto de tokens pensando y '
            'puede quedarse sin sitio para responder.'
        ),
    )
    temperatura = DecimalField(
        'Temperatura', places=2, default=0.1,
        validators=[Optional(), NumberRange(min=0, max=2)],
        description='0 para extracción de datos; más alto para redacción.',
    )
    submit = SubmitField('Guardar')

    def validate(self, extra_validators=None):
        """An external provider without a key cannot answer anything."""
        if not super().validate(extra_validators):
            return False

        from app.models.enums import EXTERNAL_AI_PROVIDERS

        proveedor = AIProviderCode.coerce(self.proveedor.data)
        es_externo = proveedor in EXTERNAL_AI_PROVIDERS
        # `_existing_key` is set by the edit view; on a create it is False.
        tiene_clave = bool(self.api_key.data) or getattr(self, '_existing_key', False)

        if es_externo and not tiene_clave:
            self.api_key.errors.append(
                'Este proveedor es externo y necesita una clave API.'
            )
            return False
        return True


class AITaskBindingForm(Formulario):
    """Assign one task to a provider (specification section 2.5)."""

    tarea = SelectField('Tarea', choices=AITask.choices(), validators=[DataRequired()])
    provider_config_id = SelectField(
        'Proveedor', validators=[Optional()],
        description='Deje «predeterminado» para que use el proveedor general.',
    )
    modelo = StringField(
        'Modelo', validators=[Optional(), Length(max=160)],
        description='Opcional: sobrescribe el modelo del proveedor para esta tarea.',
    )
    submit = SubmitField('Asignar')
