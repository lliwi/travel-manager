"""Second factor: time-based one-time codes (TOTP, RFC 6238).

Chosen over the alternatives for reasons that are mostly about failure modes.
A code from an authenticator app costs nothing per use, works with no signal
and no SMS gateway, and cannot be intercepted by taking over a phone number.
Email codes would arrive through a mailbox that is often protected by the same
password we are trying to supplement.

Three properties this module exists to guarantee:

- **A code works once.** TOTP accepts the same six digits for a whole
  thirty-second window, so without remembering which step the last accepted
  code belonged to, a code read over somebody's shoulder is valid a second
  time -- which is most of what a second factor is for.
- **Nothing readable is stored.** The secret is encrypted and the recovery
  codes are hashed. Either in the clear would be a permanent bypass for anyone
  who can read the table.
- **Losing the phone is not losing the account.** Recovery codes are issued at
  enrolment, an administrator can clear somebody's second factor, and
  ``flask mfa-reset`` does it without a browser for the day the only
  administrator is the one locked out.

It applies to directory accounts too. The directory checks the password; this
is our factor, on top, and independent of where the first one was verified.
"""

import logging
import secrets

import pyotp

from app.extensions import db
from app.models.enums import AuditResourceType, AuditResult
from app.utils.crypto import hash_password, verify_password
from app.utils.errors import ValidationError
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: How many recovery codes are issued at enrolment. Enough to survive a lost
#: phone more than once, few enough that somebody keeps them somewhere sensible.
CODIGOS = 10

#: One step either side of the current one. Phones drift and people type
#: slowly; refusing a correct code because a clock is twenty seconds out
#: teaches people that the feature is broken.
VENTANA = 1


def secreto_nuevo():
    """A fresh base32 secret, as the authenticator apps expect it."""
    return pyotp.random_base32()


def uri_de_alta(usuario, secreto, emisor):
    """The ``otpauth://`` URI an authenticator scans.

    The label carries the account name and the issuer so somebody with six
    work apps on their phone can tell which code belongs here.
    """
    return pyotp.TOTP(secreto).provisioning_uri(
        name=usuario.email or usuario.username, issuer_name=emisor,
    )


def qr_svg(uri):
    """The enrolment URI as an inline SVG.

    SVG rather than PNG so no image library is needed, and inline rather than a
    file so a secret never becomes a URL somebody could share by accident or
    that could sit in a proxy cache.
    """
    import io

    import qrcode
    import qrcode.image.svg

    imagen = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
    salida = io.BytesIO()
    imagen.save(salida)
    return salida.getvalue().decode('utf-8')


def _totp(secreto):
    return pyotp.TOTP(secreto)


def secreto_de(user):
    """The stored secret in the clear, or None."""
    if not user.mfa_secret_cifrado:
        return None

    from app.utils.crypto import decrypt_secret

    try:
        return decrypt_secret(user.mfa_secret_cifrado)
    except Exception:
        logger.warning('No se pudo descifrar el secreto MFA de %s', user.id)
        return None


def _paso_actual(secreto, codigo):
    """Which TOTP step a code belongs to, or None when it matches no step.

    Returned rather than a bare boolean because the step is what makes a code
    single-use: accepting one and not recording which window it came from
    leaves it valid for the rest of that window.
    """
    totp = _totp(secreto)
    ahora = int(utcnow().timestamp())
    for desplazamiento in range(-VENTANA, VENTANA + 1):
        instante = ahora + desplazamiento * totp.interval
        if totp.verify(codigo, for_time=instante, valid_window=0):
            return instante // totp.interval
    return None


def verificar_codigo(user, codigo, consumir=True):
    """Whether this code is valid now, and has not been used already.

    ``consumir`` is False only while enrolling, where the code proves the
    authenticator was scanned correctly and the secret is not yet in force.
    """
    secreto = secreto_de(user)
    if not secreto or not codigo:
        return False

    limpio = str(codigo).strip().replace(' ', '')
    if not limpio.isdigit():
        return False

    paso = _paso_actual(secreto, limpio)
    if paso is None:
        return False

    if consumir:
        if user.mfa_ultimo_paso is not None and paso <= user.mfa_ultimo_paso:
            logger.warning('Código MFA reutilizado por %s', user.id)
            return False
        user.mfa_ultimo_paso = paso
        db.session.commit()

    return True


# ======================================================================
# Recovery codes
# ======================================================================
def _codigo_legible():
    """A recovery code somebody can read off paper without ambiguity.

    No vowels, so it cannot spell anything; no 0/O or 1/I, which are the two
    pairs people transcribe wrongly and then conclude the code did not work.
    """
    alfabeto = '23456789BCDFGHJKMNPQRSTVWXYZ'
    mitad = lambda: ''.join(secrets.choice(alfabeto) for _ in range(5))  # noqa: E731
    return f'{mitad()}-{mitad()}'


def emitir_codigos(user, commit=True):
    """Replace this account's recovery codes and return the new ones in clear.

    The only time they are readable: they are stored hashed, so this return
    value is the single opportunity anybody has to write them down.
    """
    from app.models.user import MFARecoveryCode

    for anterior in list(user.mfa_recovery_codes):
        db.session.delete(anterior)

    codigos = [_codigo_legible() for _ in range(CODIGOS)]
    for codigo in codigos:
        db.session.add(MFARecoveryCode(
            user_id=user.id, code_hash=hash_password(codigo),
        ))

    if commit:
        db.session.commit()
    return codigos


def consumir_codigo_de_recuperacion(user, codigo):
    """Spend one recovery code, if it matches an unused one.

    Marked as used rather than deleted: «somebody used a recovery code» is the
    signal that a phone was lost or that somebody else holds the codes, and it
    has to stay answerable afterwards.
    """
    if not codigo:
        return False

    limpio = str(codigo).strip().upper().replace(' ', '')
    for fila in user.mfa_recovery_codes:
        if not fila.disponible:
            continue
        # Unpacked, never truth-tested: it returns «(válido, necesita_rehash)»,
        # and a two-element tuple is truthy whatever is in it -- which accepted
        # any string somebody typed as a valid recovery code.
        valido, _ = verify_password(fila.code_hash, limpio)
        if valido:
            fila.usado_en = utcnow()
            db.session.commit()
            logger.info('Código de recuperación usado por %s', user.id)
            return True
    return False


# ======================================================================
# Enrolment and removal
# ======================================================================
def iniciar_alta(user):
    """Store a not-yet-confirmed secret and return it with its QR.

    Not in force until :func:`confirmar_alta`: a secret somebody scanned wrong
    and could not produce a code for would lock them out of their own account
    on the next sign-in.
    """
    from flask import current_app

    from app.utils.crypto import encrypt_secret

    secreto = secreto_nuevo()
    user.mfa_secret_cifrado = encrypt_secret(secreto)
    user.mfa_activado_en = None
    user.mfa_ultimo_paso = None
    db.session.commit()

    uri = uri_de_alta(user, secreto, current_app.config['APP_NAME'])
    return {'secreto': secreto, 'uri': uri, 'qr': qr_svg(uri)}


def confirmar_alta(user, codigo):
    """Turn the second factor on, once the person has produced a valid code."""
    from app.services import audit_service

    if user.mfa_activo:
        raise ValidationError('El segundo factor ya está activo en esta cuenta.')
    if not user.mfa_secret_cifrado:
        raise ValidationError('Primero hay que empezar el alta del segundo factor.')

    if not verificar_codigo(user, codigo, consumir=False):
        raise ValidationError(
            'El código no es correcto. Compruebe que la hora del teléfono sea '
            'la real y vuelva a intentarlo.'
        )

    user.mfa_activado_en = utcnow()
    # The confirming code must not work again either: it was typed on a screen
    # somebody may have been looking at.
    user.mfa_ultimo_paso = _paso_actual(secreto_de(user), str(codigo).strip())
    codigos = emitir_codigos(user, commit=False)
    db.session.commit()

    audit_service.record(
        'user.mfa_enabled',
        recurso_tipo=AuditResourceType.USUARIO,
        recurso_id=str(user.id),
        actor=user,
    )
    return codigos


def desactivar(user, actor=None, motivo='propio'):
    """Remove the second factor and every recovery code.

    Audited with who did it, because an administrator clearing somebody else's
    second factor is the one action here that removes a protection from an
    account that is not theirs.
    """
    from app.services import audit_service

    user.mfa_secret_cifrado = None
    user.mfa_activado_en = None
    user.mfa_ultimo_paso = None
    for fila in list(user.mfa_recovery_codes):
        db.session.delete(fila)
    db.session.commit()

    audit_service.record(
        'user.mfa_disabled',
        recurso_tipo=AuditResourceType.USUARIO,
        recurso_id=str(user.id),
        actor=actor,
        resultado=AuditResult.EXITO,
        metadatos={'motivo': motivo},
    )


# ======================================================================
# Policy
# ======================================================================
#: What the setting can require, from least to most.
NIVELES = ('ninguno', 'administradores', 'gestion', 'todos')


def nivel_exigido():
    from app.services import settings_service

    valor = settings_service.get('MFA_OBLIGATORIO', 'ninguno')
    return valor if valor in NIVELES else 'ninguno'


def es_obligatorio(user):
    """Whether this account must have a second factor configured.

    The requirement is deliberately by role rather than per person: an
    administrator can grant themselves anything, so an administrator without a
    second factor is the account worth protecting first.
    """
    nivel = nivel_exigido()
    if nivel == 'todos':
        return True
    if nivel == 'gestion':
        return bool(user.is_administrador or user.is_gestor)
    if nivel == 'administradores':
        return bool(user.is_administrador)
    return False


def debe_configurarlo(user):
    """True when this account is required to have one and does not."""
    return es_obligatorio(user) and not user.mfa_activo
