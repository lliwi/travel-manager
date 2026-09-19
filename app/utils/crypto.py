"""Password hashing and symmetric encryption of secrets at rest.

Specification section 3.2 asks for Argon2id with current parameters, and section
2.5 requires AI provider API keys to be stored encrypted and never exposed in the
client, the logs or a prompt.
"""
import base64
import contextlib
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from flask import current_app


# ----------------------------------------------------------------------
# Passwords -- Argon2id
# ----------------------------------------------------------------------
def _hasher():
    """Build a hasher from the current application's parameters."""
    config = current_app.config
    return PasswordHasher(
        time_cost=config['ARGON2_TIME_COST'],
        memory_cost=config['ARGON2_MEMORY_COST'],
        parallelism=config['ARGON2_PARALLELISM'],
        hash_len=config['ARGON2_HASH_LEN'],
        salt_len=config['ARGON2_SALT_LEN'],
        type=Type.ID,
    )


def hash_password(password):
    """Hash a plaintext password with Argon2id."""
    if not password:
        raise ValueError('La contraseña no puede estar vacía.')
    return _hasher().hash(password)


def verify_password(stored_hash, password):
    """Check a password against its stored hash.

    Returns ``(valido, necesita_rehash)``. The second element is True when the
    hash was produced with weaker parameters than the current configuration, so
    the caller can transparently upgrade it on a successful login.
    """
    if not stored_hash or not password:
        return False, False
    hasher = _hasher()
    try:
        hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    try:
        return True, hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True, True


def dummy_verify():
    """Burn roughly one password verification's worth of time.

    Called when a login names an account that does not exist, so the response
    time does not reveal which usernames are real.
    """
    # The verification is expected to fail; the point is to spend the time.
    with contextlib.suppress(Exception):
        _hasher().verify(
            '$argon2id$v=19$m=8192,t=1,p=1$'
            'c29tZXNhbHRzb21lc2FsdA$5nWXAPBCjHgQJZK7QIGY7mrGEhJKLSxLM7rE0NxBxJU',
            'not-the-password',
        )


def generate_password(length=16):
    """Generate a random password for an administrator-created account."""
    alphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%*'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ----------------------------------------------------------------------
# Secrets at rest -- Fernet
# ----------------------------------------------------------------------
def _fernet():
    """Build the Fernet (or MultiFernet) used to encrypt stored secrets.

    When ``SECRETS_ENCRYPTION_KEY_PREVIOUS`` is set, both keys are accepted for
    decryption while new values are written with the current key -- which is what
    makes key rotation possible without downtime.
    """
    key = current_app.config.get('SECRETS_ENCRYPTION_KEY')
    if not key:
        raise RuntimeError(
            'SECRETS_ENCRYPTION_KEY no está configurada; no es posible cifrar secretos.'
        )
    keys = [Fernet(_normalise_key(key))]
    previous = current_app.config.get('SECRETS_ENCRYPTION_KEY_PREVIOUS')
    if previous:
        keys.append(Fernet(_normalise_key(previous)))
    return MultiFernet(keys) if len(keys) > 1 else keys[0]


def _normalise_key(raw):
    """Accept either a Fernet key or any passphrase, deriving 32 url-safe bytes."""
    raw = raw.strip() if isinstance(raw, str) else raw
    candidate = raw.encode() if isinstance(raw, str) else raw

    # Already a Fernet key? Use it as-is. Anything else is a passphrase, from
    # which a key is derived, so an operator cannot accidentally configure a
    # weak one by pasting a short string.
    with contextlib.suppress(Exception):
        if len(base64.urlsafe_b64decode(candidate)) == 32:
            return candidate

    digest = hashlib.sha256(candidate).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plaintext):
    """Encrypt a secret for storage. Returns None for empty input."""
    if plaintext in (None, ''):
        return None
    return _fernet().encrypt(str(plaintext).encode('utf-8')).decode('ascii')


def decrypt_secret(ciphertext):
    """Decrypt a stored secret, returning None when it cannot be read.

    A key rotated without re-encrypting leaves unreadable rows; returning None
    lets the caller report "no key configured" instead of crashing a request.
    """
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(str(ciphertext).encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError, TypeError):
        return None


def secret_hint(plaintext, visible=4):
    """Last few characters of a secret, so an administrator can recognise it."""
    if not plaintext or len(plaintext) <= visible:
        return None
    return str(plaintext)[-visible:]


def generate_fernet_key():
    """Generate a fresh encryption key, for setup scripts and the CLI."""
    return Fernet.generate_key().decode('ascii')
