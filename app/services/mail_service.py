"""Sending mail, configured from the panel rather than from the environment.

Host, port, credentials and sender live in Ajustes for the same reason the AI
provider does: changing where the organisation's mail goes out is an
administrative decision, not a redeploy, and a password in an environment file
is a password in plain text. The one here is stored encrypted.

Nothing is sent while ``CORREO_HABILITADO`` is off, and that is the shipped
state: a deployment that has not been told where to send mail must not start
writing to whatever address it finds.
"""
import logging
import smtplib
from email.message import EmailMessage

from app.services import settings_service
from app.utils.errors import AppError

logger = logging.getLogger(__name__)


class MailError(AppError):
    codigo = 'correo'
    status = 502
    mensaje_por_defecto = 'No se pudo enviar el correo.'


def configuracion():
    """What the panel says about the outgoing server."""
    return {
        'habilitado': settings_service.get_bool('CORREO_HABILITADO', False),
        'host': settings_service.get('CORREO_HOST', '') or '',
        'puerto': settings_service.get_int('CORREO_PUERTO', 1025),
        'usuario': settings_service.get('CORREO_USUARIO', '') or '',
        'contrasena': settings_service.get('CORREO_CONTRASENA', '') or '',
        'tls': settings_service.get_bool('CORREO_TLS', False),
        'remitente': settings_service.get('CORREO_REMITENTE', '') or '',
    }


def esta_configurado():
    """True when there is somewhere to send to."""
    config = configuracion()
    return bool(config['habilitado'] and config['host'] and config['remitente'])


def enviar(destinatarios, asunto, cuerpo, actor=None):
    """Send one message, or explain why it was not sent.

    Returns True when it left, False when mail is off or unconfigured. Only a
    real failure raises: «no está configurado» is a state a deployment is
    allowed to be in, and turning it into an error would make every caller
    handle a situation that is not a problem.
    """
    destinatarios = [d for d in (destinatarios or []) if d]
    if not destinatarios:
        return False

    config = configuracion()
    if not esta_configurado():
        logger.info(
            'Correo no enviado a %s: el envío está desactivado o sin configurar.',
            ', '.join(destinatarios),
        )
        return False

    mensaje = EmailMessage()
    mensaje['From'] = config['remitente']
    mensaje['To'] = ', '.join(destinatarios)
    mensaje['Subject'] = asunto
    mensaje.set_content(cuerpo)

    try:
        with smtplib.SMTP(config['host'], config['puerto'], timeout=15) as servidor:
            if config['tls']:
                servidor.starttls()
            if config['usuario']:
                servidor.login(config['usuario'], config['contrasena'])
            servidor.send_message(mensaje)
    except Exception as exc:
        logger.warning('Falló el envío de correo a %s: %s', destinatarios, exc)
        raise MailError(f'No se pudo enviar el correo: {exc}') from exc

    logger.info('Correo enviado a %s: %s', ', '.join(destinatarios), asunto)
    return True


def comprobar(destinatario, actor=None):
    """Send a test message, so «¿funciona?» has an answer before it matters."""
    return enviar(
        [destinatario],
        'Prueba de configuración — Travel Manager',
        'Si lee esto, el servidor de correo configurado en Ajustes funciona.\n',
        actor=actor,
    )
