from datetime import datetime, timedelta, timezone

import jwt

from src.auth.principal import Principal
from src.core.config import SETTINGS
from src.core.errors import AuthError


def issue_access_token(principal: Principal) -> tuple[str, int]:
    cfg = SETTINGS.auth
    now = datetime.now(timezone.utc)
    ttl = cfg.access_token_ttl_s
    claims = {
        "iss": cfg.jwt_issuer,
        "sub": principal.user_id,
        "tid": principal.tenant_id,
        "email": principal.email,
        "role": principal.role,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
    }
    return jwt.encode(claims, cfg.jwt_secret, algorithm=cfg.jwt_algorithm), ttl


def decode_access_token(token: str) -> Principal:
    cfg = SETTINGS.auth
    try:
        claims = jwt.decode(
            token,
            cfg.jwt_secret,
            algorithms=[cfg.jwt_algorithm],
            issuer=cfg.jwt_issuer,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid or expired token.") from exc

    if claims["role"] not in cfg.roles:
        raise AuthError("Token carries an unknown role.")
    return Principal(
        user_id=claims["sub"],
        tenant_id=claims["tid"],
        email=claims["email"],
        role=claims["role"],
    )
