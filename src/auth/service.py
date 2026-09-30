import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.auth.passwords import hash_password, verify_password
from src.auth.principal import Principal
from src.core.config import SETTINGS
from src.core.errors import AuthError, PermissionDenied
from src.storage.db import session
from src.storage.models import Tenant, User


def register_tenant(tenant_name: str, email: str, password: str) -> Principal:
    """Sign up: creates the tenant and its first user. Admin is reserved for the configured
    addresses, so an ordinary signup gets `signup_role`, not the run of the instance."""
    _validate_password(password)
    tenant_id = str(uuid.uuid4())
    address = _normalize(email)
    principal = Principal(
        user_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        email=address,
        role="admin" if SETTINGS.auth.is_admin_email(address) else SETTINGS.auth.signup_role,
    )
    try:
        with session() as sess:
            sess.add(Tenant(tenant_id=tenant_id, name=tenant_name.strip()))
            sess.add(_user_row(principal, password))
    except IntegrityError as exc:
        raise AuthError("That organization name or email is already registered.") from exc
    return principal


def create_user(actor: Principal, email: str, password: str, role: str) -> Principal:
    """Admin-only, and only inside the actor's own tenant — the tenant is never a parameter."""
    if not actor.can("admin"):
        raise PermissionDenied("Only an admin can create users.")
    if role not in SETTINGS.auth.roles:
        raise PermissionDenied(f"Unknown role '{role}'.")
    address = _normalize(email)
    if role == "admin" and not SETTINGS.auth.is_admin_email(address):
        raise PermissionDenied("The admin role is reserved and cannot be granted.")
    _validate_password(password)
    principal = Principal(
        user_id=str(uuid.uuid4()),
        tenant_id=actor.tenant_id,
        email=address,
        role=role,
    )
    try:
        with session() as sess:
            sess.add(_user_row(principal, password))
    except IntegrityError as exc:
        raise AuthError("That email is already registered.") from exc
    return principal


def authenticate(email: str, password: str) -> Principal:
    row = _find_user(_normalize(email))
    # Same message either way: a distinct "no such user" reply is a user-enumeration oracle.
    if row is None or not verify_password(password, row.password_hash):
        raise AuthError("Incorrect email or password.")
    return Principal(
        user_id=row.user_id, tenant_id=row.tenant_id, email=row.email, role=row.role
    )


def list_users(actor: Principal) -> list[dict]:
    if not actor.can("admin"):
        raise PermissionDenied("Only an admin can list users.")
    with session() as sess:
        stmt = select(User).where(User.tenant_id == actor.tenant_id).order_by(User.created_at)
        return [
            {"user_id": u.user_id, "email": u.email, "role": u.role} for u in sess.scalars(stmt)
        ]


def tenant_name(tenant_id: str) -> str:
    with session() as sess:
        name = sess.scalar(select(Tenant.name).where(Tenant.tenant_id == tenant_id))
    if name is None:
        raise AuthError("Tenant no longer exists.")
    return name


def _user_row(principal: Principal, password: str) -> User:
    return User(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        email=principal.email,
        password_hash=hash_password(password),
        role=principal.role,
    )


def _find_user(email: str) -> Optional[User]:
    with session() as sess:
        return sess.scalars(select(User).where(User.email == email)).first()


def _validate_password(password: str) -> None:
    if len(password) < SETTINGS.auth.min_password_chars:
        raise AuthError(
            f"Password must be at least {SETTINGS.auth.min_password_chars} characters."
        )


def _normalize(email: str) -> str:
    return email.strip().lower()
