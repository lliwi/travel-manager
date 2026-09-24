"""Passports, visas and policies a traveller carries.

The alert engine has read this table since the beginning and nothing ever
wrote to it: the rule «documento del viajero por caducar» could not fire
because the data had no way in. This is that way in.

**Whose data this is.** A passport belongs to a person, not to a trip, which is
why the row hangs off ``users.id`` and its expiry matters across every trip
they take. It follows that the person owns it: they may add, correct and delete
their own, and an administrator may too because they administer the accounts. A
manager is not on that list -- planning needs to know that a passport expires
before the trip, which is what the alert says, and that is a different thing
from reading somebody's passport number.

**What is stored in clear.** The last four characters and nothing else. The
number is encrypted at rest and only decrypted when its owner or an
administrator asks for it, which is recorded.
"""
import logging
from datetime import date

from app.extensions import db
from app.models.enums import AuditResourceType, RoleCode
from app.models.settings import TravelerDocument
from app.utils.crypto import decrypt_secret, encrypt_secret, secret_hint
from app.utils.errors import AuthorizationError, ResourceNotFound, ValidationError

logger = logging.getLogger(__name__)

#: What a traveller can register. Kept here rather than as an enum column: the
#: list is a convenience for the form, and an installation that files something
#: else should not need a migration to do it.
TIPOS = (
    ('pasaporte', 'Pasaporte'),
    ('dni', 'DNI o documento de identidad'),
    ('visado', 'Visado'),
    ('permiso_conducir', 'Permiso de conducción'),
    ('seguro', 'Seguro de viaje'),
    ('otro', 'Otro'),
)


def esta_habilitado():
    """Whether this deployment keeps traveller documents at all."""
    from app.services import settings_service

    return settings_service.get_bool('DOCUMENTOS_VIAJERO_HABILITADOS', False)


def puede_gestionar(actor, propietario):
    """Whether ``actor`` may see and change ``propietario``'s documents.

    The owner and an administrator. Deliberately not a manager: the alert tells
    them a passport expires before the trip, and that is what planning needs.
    """
    if actor is None or propietario is None:
        return False
    if str(actor.id) == str(propietario.id):
        return True
    return actor.has_role(RoleCode.ADMINISTRADOR)


def _exigir(actor, propietario):
    if not esta_habilitado():
        raise ValidationError(
            'El registro de documentos personales está desactivado. Un '
            'administrador puede activarlo en Ajustes.'
        )
    if not puede_gestionar(actor, propietario):
        raise AuthorizationError(
            'Solo la propia persona o un administrador pueden gestionar estos '
            'documentos.'
        )


def listar(actor, propietario, incluir_inactivos=False):
    """The documents on file, newest expiry first."""
    _exigir(actor, propietario)

    consulta = TravelerDocument.query.filter_by(user_id=propietario.id)
    if not incluir_inactivos:
        consulta = consulta.filter_by(activo=True)
    return consulta.order_by(TravelerDocument.fecha_caducidad.asc().nulls_last()).all()


def obtener(actor, propietario, documento_id):
    documento = TravelerDocument.query.filter_by(
        id=documento_id, user_id=propietario.id,
    ).first()
    if documento is None:
        raise ResourceNotFound('Ese documento no existe.')
    _exigir(actor, propietario)
    return documento


def crear(actor, propietario, tipo, numero=None, pais_emisor=None,
          fecha_emision=None, fecha_caducidad=None, notas=None, commit=True):
    """Register a document for somebody."""
    _exigir(actor, propietario)
    _validar(tipo, fecha_emision, fecha_caducidad)

    documento = TravelerDocument(user_id=propietario.id, tipo=tipo.strip()[:40])
    _aplicar(documento, numero, pais_emisor, fecha_emision, fecha_caducidad, notas)

    db.session.add(documento)
    if commit:
        db.session.commit()
    _auditar(actor, 'traveler_document.created', documento, propietario)
    return documento


def actualizar(actor, propietario, documento, tipo=None, numero=None,
               pais_emisor=None, fecha_emision=None, fecha_caducidad=None,
               notas=None, commit=True):
    """Correct a document already on file.

    An empty ``numero`` keeps the stored one: the form never renders the number
    back, so blank means «unchanged», not «delete it».
    """
    _exigir(actor, propietario)
    _validar(tipo or documento.tipo, fecha_emision, fecha_caducidad)

    if tipo:
        documento.tipo = tipo.strip()[:40]
    _aplicar(documento, numero, pais_emisor, fecha_emision, fecha_caducidad, notas)

    if commit:
        db.session.commit()
    _auditar(actor, 'traveler_document.updated', documento, propietario)
    return documento


def eliminar(actor, propietario, documento, commit=True):
    """Take a document off the list.

    Really deleted, not soft-deleted: it is personal data somebody asked to
    remove, and keeping a copy to be tidy is the opposite of what they asked.
    The audit entry records that it happened, without the number.
    """
    _exigir(actor, propietario)

    datos = {'tipo': documento.tipo, 'ultimos4': documento.numero_ultimos4}
    db.session.delete(documento)
    if commit:
        db.session.commit()

    from app.services import audit_service

    audit_service.record(
        'traveler_document.deleted',
        actor=actor,
        recurso_tipo=AuditResourceType.USUARIO,
        recurso_id=str(propietario.id),
        metadatos=datos,
    )


def numero_en_claro(actor, propietario, documento):
    """The full number, decrypted, and recorded as having been read.

    Separate from reading the row because it is a different act: the list shows
    «****1234» and needs no record, and somebody asking for the whole number
    does.
    """
    _exigir(actor, propietario)
    if not documento.numero_encrypted:
        return None

    _auditar(actor, 'traveler_document.number_read', documento, propietario)
    return decrypt_secret(documento.numero_encrypted)


def _aplicar(documento, numero, pais_emisor, fecha_emision, fecha_caducidad, notas):
    if numero:
        limpio = str(numero).strip()
        documento.numero_encrypted = encrypt_secret(limpio)
        documento.numero_ultimos4 = secret_hint(limpio)
    if pais_emisor is not None:
        documento.pais_emisor = (str(pais_emisor).strip().upper()[:2]) or None
    if fecha_emision is not None:
        documento.fecha_emision = fecha_emision
    if fecha_caducidad is not None:
        documento.fecha_caducidad = fecha_caducidad
    if notas is not None:
        documento.notas = (str(notas).strip() or None)


def _validar(tipo, fecha_emision, fecha_caducidad):
    if not tipo or not str(tipo).strip():
        raise ValidationError('Indique de qué documento se trata.')

    if fecha_emision and fecha_caducidad and fecha_caducidad < fecha_emision:
        raise ValidationError('La caducidad no puede ser anterior a la emisión.')

    if fecha_caducidad and fecha_caducidad.year > date.today().year + 50:
        # Un año tecleado de más convierte un pasaporte caducado en uno válido
        # medio siglo, y la alerta deja de saltar sin que nada falle.
        raise ValidationError('Revise la fecha de caducidad: parece demasiado lejana.')


def _auditar(actor, accion, documento, propietario):
    from app.services import audit_service

    audit_service.record(
        accion,
        actor=actor,
        recurso_tipo=AuditResourceType.USUARIO,
        recurso_id=str(propietario.id),
        # Nunca el número: el registro dice qué pasó, no qué pone en el
        # documento.
        metadatos={'documento_id': str(documento.id), 'tipo': documento.tipo},
    )
