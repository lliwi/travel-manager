"""Identity provider layer (specification section 3.3)."""
from app.services.identity.base import AuthResult, IdentityProvider, IdentityRecord
from app.services.identity.local import (
    MIN_PASSWORD_LENGTH,
    LocalIdentityProvider,
    validate_password_strength,
)
from app.services.identity.registry import (
    authenticate,
    available_providers,
    get_provider,
    provision_user,
    register_provider,
)

__all__ = [
    'IdentityProvider', 'IdentityRecord', 'AuthResult',
    'LocalIdentityProvider', 'validate_password_strength', 'MIN_PASSWORD_LENGTH',
    'authenticate', 'get_provider', 'register_provider', 'available_providers',
    'provision_user',
]
