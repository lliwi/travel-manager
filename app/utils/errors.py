"""Application error hierarchy.

Every layer raises these instead of calling ``abort()`` directly, so the Jinja
surface and the ``/api/v1`` surface can render the same failure in their own
idiom without duplicating any logic. See specification section 7: "Todas las
respuestas deberán usar un esquema consistente de errores".
"""


class AppError(Exception):
    """Base class for every expected application failure."""

    codigo = 'error'
    status = 500
    mensaje_por_defecto = 'Se ha producido un error inesperado.'

    def __init__(self, mensaje=None, detalles=None):
        self.mensaje = mensaje or self.mensaje_por_defecto
        self.detalles = detalles or {}
        super().__init__(self.mensaje)

    def to_dict(self):
        """Serialise for the API error envelope."""
        payload = {'codigo': self.codigo, 'mensaje': self.mensaje}
        if self.detalles:
            payload['detalles'] = self.detalles
        return payload


class AuthenticationRequired(AppError):
    codigo = 'unauthenticated'
    status = 401
    mensaje_por_defecto = 'Autenticación requerida.'


class AuthenticationFailed(AppError):
    codigo = 'invalid_credentials'
    status = 401
    mensaje_por_defecto = 'Credenciales no válidas.'


class AccountLocked(AppError):
    codigo = 'account_locked'
    status = 423
    mensaje_por_defecto = 'La cuenta está bloqueada temporalmente.'


class AuthorizationError(AppError):
    codigo = 'forbidden'
    status = 403
    mensaje_por_defecto = 'No tiene permisos para realizar esta operación.'


class ResourceNotFound(AppError):
    codigo = 'not_found'
    status = 404
    mensaje_por_defecto = 'El recurso solicitado no existe.'


class ValidationError(AppError):
    codigo = 'validation_error'
    status = 422
    mensaje_por_defecto = 'Los datos enviados no son válidos.'


class ConflictError(AppError):
    codigo = 'conflict'
    status = 409
    mensaje_por_defecto = 'La operación entra en conflicto con el estado actual.'


class InvalidTransition(ConflictError):
    codigo = 'invalid_transition'
    mensaje_por_defecto = 'La transición de estado solicitada no está permitida.'


class RateLimited(AppError):
    codigo = 'rate_limited'
    status = 429
    mensaje_por_defecto = 'Demasiadas peticiones. Inténtelo de nuevo más tarde.'


class PayloadTooLarge(AppError):
    codigo = 'payload_too_large'
    status = 413
    mensaje_por_defecto = 'El archivo supera el tamaño máximo permitido.'


# ----------------------------------------------------------------------
# Processing errors -- distinguish retryable from terminal, which is what
# the Celery pipeline keys its retry policy on.
# ----------------------------------------------------------------------
class ProcessingError(AppError):
    """Base for document pipeline failures."""

    codigo = 'processing_error'
    status = 500


class TransientError(ProcessingError):
    """A failure worth retrying (network blip, service restarting)."""

    codigo = 'transient_error'


class PermanentError(ProcessingError):
    """A failure that retrying cannot fix (infected file, wrong type)."""

    codigo = 'permanent_error'


class StorageError(ProcessingError):
    codigo = 'storage_error'


class AntivirusError(ProcessingError):
    codigo = 'antivirus_error'


class InfectedFileError(PermanentError):
    codigo = 'infected_file'
    status = 422
    mensaje_por_defecto = 'El archivo ha sido rechazado por el antivirus.'


class AIError(AppError):
    codigo = 'ai_error'
    status = 502
    mensaje_por_defecto = 'El proveedor de IA no ha podido completar la operación.'


class AIContractError(PermanentError):
    """The model returned something that does not satisfy the output schema."""

    codigo = 'ai_contract_error'
    status = 502
    mensaje_por_defecto = 'La respuesta del modelo no cumple el contrato esperado.'


class AIPolicyBlocked(AppError):
    """The data egress policy refused to send this payload (section 2.5)."""

    codigo = 'ai_policy_blocked'
    status = 403
    mensaje_por_defecto = (
        'La política de salida de datos impide enviar esta información al proveedor seleccionado.'
    )


class WebResearchError(AppError):
    codigo = 'web_research_error'
    status = 502


class SSRFBlocked(WebResearchError):
    codigo = 'ssrf_blocked'
    status = 400
    mensaje_por_defecto = 'El destino solicitado no está permitido.'
