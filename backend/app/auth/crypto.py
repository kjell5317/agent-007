"""Symmetric encryption for OAuth tokens at rest.

Tokens are stored as Fernet ciphertext and only decrypted at point of use.
Key comes from `TOKEN_ENCRYPTION_KEY` (generate with `Fernet.generate_key()`).
"""

from cryptography.fernet import Fernet

from app.config import get_settings


def _cipher() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        # TODO: fail loudly at startup instead of lazily here
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plaintext: str) -> bytes:
    return _cipher().encrypt(plaintext.encode())


def decrypt_token(ciphertext: bytes) -> str:
    return _cipher().decrypt(ciphertext).decode()
