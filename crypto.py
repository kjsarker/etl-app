from cryptography.fernet import Fernet

from app_secrets import get_secret


def _get_fernet() -> Fernet:
    key = get_secret("APP_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "APP_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "and store it as a secret (never commit it to the repo)."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_text(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_text(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()
