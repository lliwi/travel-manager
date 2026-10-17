"""Rotating the key secrets are encrypted with.

Changing ``SECRETS_ENCRYPTION_KEY`` alone loses data quietly: every stored
secret -- AI provider keys, the directory password, MFA secrets, travellers'
document numbers -- stays encrypted with the old key, and ``decrypt_secret``
answers None for each of them. Nothing fails; things just stop working.

Rotation is therefore two keys for a while: the new one in
``SECRETS_ENCRYPTION_KEY`` and the old one in
``SECRETS_ENCRYPTION_KEY_PREVIOUS``. :func:`recifrar` rewrites everything the
old key opens with the new one, after which the old key can go -- and must,
after a compromise, or rotating achieved nothing.

What neither key opens is reported and never touched: it is the sign that the
old key was not provided, and the one thing to do then is stop.
"""
import logging
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from app.extensions import db
from app.models.enums import AuditResourceType
from app.services import audit_service

logger = logging.getLogger(__name__)

#: How an encrypted column is called on screen, by table.
_QUE_ES = {
    'ai_provider_configs': 'clave de API de un proveedor de IA',
    'traveler_documents': 'número de documento de un viajero',
    'users': 'segundo factor (MFA) de un usuario',
}


@dataclass
class Estado:
    con_la_actual: int = 0
    con_la_anterior: int = 0
    #: Descriptions of what neither key opens.
    ilegibles: list = field(default_factory=list)

    @property
    def hay_que_recifrar(self):
        return self.con_la_anterior > 0

    def to_dict(self):
        return {
            'con_la_actual': self.con_la_actual,
            'con_la_anterior': self.con_la_anterior,
            'ilegibles': list(self.ilegibles),
        }


def _fernet(clave):
    from app.utils.crypto import _normalise_key

    return Fernet(_normalise_key(clave)) if clave else None


def _cifrados():
    """Every stored encrypted value: ``(descripcion, leer, escribir)``."""
    from app.models.settings import SystemSetting
    from app.services import settings_service
    from app.services.backup_service import _campos_cifrados

    for modelo, campo in _campos_cifrados():
        que = _QUE_ES.get(modelo.__tablename__, f'{modelo.__tablename__}.{campo}')
        for fila in modelo.query.filter(getattr(modelo, campo).isnot(None)).all():
            yield (
                f'{que} ({fila.id})',
                lambda f=fila, c=campo: getattr(f, c),
                lambda valor, f=fila, c=campo: setattr(f, c, valor),
            )

    for ajuste in SystemSetting.query.all():
        if not settings_service.es_secreto(ajuste.clave):
            continue
        if not (isinstance(ajuste.valor, dict) and ajuste.valor.get('v')):
            continue
        yield (
            f'ajuste {ajuste.clave}',
            lambda a=ajuste: a.valor['v'],
            lambda valor, a=ajuste: setattr(a, 'valor', {'v': valor}),
        )


def _abre(fernet, token):
    if fernet is None:
        return None
    try:
        return fernet.decrypt(token.encode('ascii'))
    except (InvalidToken, ValueError, TypeError):
        return None


def estado():
    """Which key opens each stored secret. Changes nothing."""
    actual = _fernet(current_app.config.get('SECRETS_ENCRYPTION_KEY'))
    anterior = _fernet(current_app.config.get('SECRETS_ENCRYPTION_KEY_PREVIOUS'))

    resultado = Estado()
    for descripcion, leer, _escribir in _cifrados():
        token = leer()
        if _abre(actual, token) is not None:
            resultado.con_la_actual += 1
        elif _abre(anterior, token) is not None:
            resultado.con_la_anterior += 1
        else:
            resultado.ilegibles.append(descripcion)
    return resultado


def recifrar(actor=None):
    """Rewrite with the current key everything the previous key opens.

    One transaction: halfway would leave secrets split between two keys with
    nothing to say which. Refuses, without writing, when something opens with
    neither -- the old key is missing, and re-encrypting the rest would hide
    that.
    """
    actual = _fernet(current_app.config.get('SECRETS_ENCRYPTION_KEY'))
    anterior = _fernet(current_app.config.get('SECRETS_ENCRYPTION_KEY_PREVIOUS'))

    antes = estado()
    if antes.ilegibles:
        return antes

    recifrados = 0
    for _descripcion, leer, escribir in _cifrados():
        token = leer()
        if _abre(actual, token) is not None:
            continue
        claro = _abre(anterior, token)
        escribir(actual.encrypt(claro).decode('ascii'))
        recifrados += 1

    if recifrados:
        db.session.commit()
        audit_service.record(
            'crypto.key_rotated',
            recurso_tipo=AuditResourceType.CONFIGURACION,
            actor=actor,
            metadatos={'recifrados': recifrados},
        )
        logger.info('Secretos recifrados con la clave nueva: %s', recifrados)
    return estado()
