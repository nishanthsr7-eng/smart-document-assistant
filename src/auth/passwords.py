import hmac
import os
from hashlib import scrypt

from src.core.config import SETTINGS

_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    cfg = SETTINGS.auth
    salt = os.urandom(cfg.scrypt_salt_bytes)
    digest = _derive(password, salt)
    return f"{_PREFIX}${cfg.scrypt_n}${cfg.scrypt_r}${cfg.scrypt_p}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
    if scheme != _PREFIX:
        raise ValueError(f"Unknown password hash scheme '{scheme}'.")
    candidate = _derive(password, bytes.fromhex(salt_hex), int(n), int(r), int(p))
    return hmac.compare_digest(candidate, bytes.fromhex(digest_hex))


def _derive(password: str, salt: bytes, n: int = 0, r: int = 0, p: int = 0) -> bytes:
    cfg = SETTINGS.auth
    return scrypt(
        password.encode(),
        salt=salt,
        n=n or cfg.scrypt_n,
        r=r or cfg.scrypt_r,
        p=p or cfg.scrypt_p,
        # 128 * N * r bytes are needed; OpenSSL's 32 MB default is just under it at N=2**15.
        maxmem=128 * 1024 * 1024,
        dklen=32,
    )
