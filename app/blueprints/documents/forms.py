"""Document forms."""
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import SelectField, SubmitField
from wtforms.validators import DataRequired, Optional

from app.models.enums import DocumentType
from app.utils.forms import Formulario

#: Mirrors ``ALLOWED_DOCUMENT_EXTENSIONS``; enforced again server-side by
#: magic-byte inspection, because a client-side extension check proves nothing.
ALLOWED_EXTENSIONS = ('pdf', 'jpg', 'jpeg', 'png', 'tiff', 'tif', 'eml')



class DocumentUploadForm(Formulario):
    """Attach a file to a trip."""

    archivo = FileField(
        'Documento',
        validators=[
            FileRequired(message='Seleccione un archivo.'),
            FileAllowed(
                ALLOWED_EXTENSIONS,
                message='Formato no admitido. Se aceptan PDF, imágenes y mensajes EML.',
            ),
        ],
    )
    tipo = SelectField(
        'Tipo de documento', choices=DocumentType.choices(), validators=[DataRequired()]
    )
    trip_traveler_id = SelectField(
        'Persona viajera', validators=[Optional()],
        description='Deje en blanco si el documento afecta a todo el viaje.',
    )
    submit = SubmitField('Subir y procesar')
