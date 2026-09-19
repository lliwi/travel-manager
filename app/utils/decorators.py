"""Route decorators.

One authorisation decorator serves both the Jinja blueprints and ``/api/v1``.
The surfaces differ only in how the raised exception is rendered, which happens
in the two error handlers -- the decision logic lives once, in
``authorization_service``.
"""
import functools
import logging

from flask import request
from flask_login import current_user

from app.models.enums import AuditResourceType, AuditResult
from app.services import authorization_service
from app.utils.errors import AuthenticationRequired, AuthorizationError

logger = logging.getLogger(__name__)


def _actor():
    """The acting user as a real object rather than a proxy."""
    if not current_user or not current_user.is_authenticated:
        return None
    return current_user._get_current_object()


def require_permiso(permiso, recurso_arg=None, loader=None, inject_as=None,
                    recurso_tipo=None):
    """Require a permission, optionally on a resource loaded from the URL.

    Args:
        permiso: The :class:`Permiso` needed.
        recurso_arg: Name of the view argument holding the resource id.
        loader: Callable ``(actor, id, permiso) -> resource`` that both loads and
            authorises. Usually one of ``authorization_service.authorize_*``.
        inject_as: When set, the loaded resource is passed to the view under this
            keyword, so the view does not fetch it a second time.
        recurso_tipo: Resource type recorded on a denial.

    The wrapper exposes ``__authz__``, which ``tests/test_api_parity.py`` walks
    the URL map for -- that test is what stops an unguarded endpoint shipping.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            actor = _actor()
            if actor is None:
                raise AuthenticationRequired()

            if loader is not None and recurso_arg is not None:
                recurso = loader(actor, kwargs.get(recurso_arg), permiso)
                if inject_as:
                    kwargs[inject_as] = recurso
            else:
                authorization_service.require(
                    actor, permiso, None, recurso_tipo=recurso_tipo
                )

            return view(*args, **kwargs)

        wrapper.__authz__ = (str(permiso), recurso_arg)
        return wrapper

    return decorator


def require_trip_access(permiso, inject_as='trip', recurso_arg='trip_id'):
    """Require a permission on the trip named in the URL."""
    return require_permiso(
        permiso,
        recurso_arg=recurso_arg,
        loader=authorization_service.authorize_trip,
        inject_as=inject_as,
        recurso_tipo=AuditResourceType.VIAJE,
    )


def require_document_access(permiso, inject_as='document', recurso_arg='document_id'):
    """Require a permission on the document named in the URL."""
    return require_permiso(
        permiso,
        recurso_arg=recurso_arg,
        loader=authorization_service.authorize_document,
        inject_as=inject_as,
        recurso_tipo=AuditResourceType.DOCUMENTO,
    )


def require_alert_access(permiso, inject_as='alert', recurso_arg='alert_id'):
    """Require a permission on the alert named in the URL."""
    return require_permiso(
        permiso,
        recurso_arg=recurso_arg,
        loader=authorization_service.authorize_alert,
        inject_as=inject_as,
        recurso_tipo=AuditResourceType.ALERTA,
    )


def require_advisory_access(permiso, inject_as='advisory', recurso_arg='advisory_id'):
    """Require a permission on the advisory named in the URL."""
    return require_permiso(
        permiso,
        recurso_arg=recurso_arg,
        loader=authorization_service.authorize_advisory,
        inject_as=inject_as,
        recurso_tipo=AuditResourceType.RECOMENDACION,
    )


def require_role(*role_codes):
    """Require any one of the given roles, independently of a resource."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            actor = _actor()
            if actor is None:
                raise AuthenticationRequired()
            if not actor.has_any_role(*role_codes):
                from app.services import audit_service

                audit_service.record_denied(
                    f'authz.denied.rol.{request.endpoint}',
                    actor=actor,
                    motivo=f'requiere_rol:{",".join(str(r) for r in role_codes)}',
                )
                raise AuthorizationError()
            return view(*args, **kwargs)

        wrapper.__authz__ = (f'rol:{",".join(str(r) for r in role_codes)}', None)
        return wrapper

    return decorator


def require_admin(view):
    """Require the administrator role."""
    from app.models.enums import RoleCode

    return require_role(RoleCode.ADMINISTRADOR)(view)


def require_gestor(view):
    """Require the manager role (administrators also pass)."""
    from app.models.enums import RoleCode

    return require_role(RoleCode.GESTOR, RoleCode.ADMINISTRADOR)(view)


def scoped_by_query(view):
    """Mark an endpoint whose authorisation is the scoping of its own query.

    A listing endpoint has no single resource to check: it is authorised by
    building its query through ``visible_trips_query`` or an equivalent, so the
    caller can only ever be handed rows they may see. Marking it makes that an
    explicit, reviewable decision rather than a missing decorator, and the API
    parity test accepts the marker in place of a permission.
    """
    view.__authz__ = ('scoped_by_query', None)
    return view


def authenticated_only(view):
    """Mark an endpoint that needs authentication and nothing more.

    Used for operations that act on the caller's own session or profile, where
    there is no resource to authorise against.
    """
    view.__authz__ = ('autenticado', None)
    return view


def public_endpoint(view):
    """Mark an endpoint as deliberately unauthenticated.

    Only login, the health probes and the static error pages carry this. The API
    parity test accepts ``__public__`` in place of ``__authz__``, so marking an
    endpoint public is an explicit, reviewable decision rather than an omission.
    """
    view.__public__ = True
    return view


def audit_action(accion, recurso_tipo=None, recurso_arg=None):
    """Write an audit entry after the view runs.

    Logs after the fact so the entry reflects what actually happened, and records
    a failure result when the view raised.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            from app.services import audit_service

            recurso_id = None
            if recurso_arg:
                recurso_id = kwargs.get(recurso_arg)
            else:
                for candidate in ('trip_id', 'document_id', 'alert_id', 'user_id',
                                  'advisory_id', 'extraction_id'):
                    if candidate in kwargs:
                        recurso_id = kwargs[candidate]
                        break

            try:
                result = view(*args, **kwargs)
            except Exception as exc:
                audit_service.record(
                    accion,
                    recurso_tipo=recurso_tipo,
                    recurso_id=recurso_id,
                    resultado=AuditResult.ERROR,
                    metadatos={'excepcion': type(exc).__name__},
                )
                raise

            audit_service.record(
                accion, recurso_tipo=recurso_tipo, recurso_id=recurso_id
            )
            return result

        return wrapper

    return decorator


def rate_limited(limit_string):
    """Apply a per-endpoint rate limit, tolerating the limiter being disabled."""

    def decorator(view):
        from app.extensions import limiter

        try:
            return limiter.limit(limit_string)(view)
        except Exception:
            logger.debug('Limitador no disponible para %s', view.__name__)
            return view

    return decorator
