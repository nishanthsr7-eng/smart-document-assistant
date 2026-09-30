import time

import jwt
import pytest

from src.auth import audit, service
from src.auth.passwords import hash_password, verify_password
from src.auth.principal import Principal
from src.auth.tokens import decode_access_token, issue_access_token
from src.core.config import SETTINGS
from src.core.errors import AuthError, PermissionDenied

PRINCIPAL = Principal(user_id="u1", tenant_id="t1", email="a@b.test", role="editor")


# --- passwords ---


def test_password_round_trips_and_rejects_the_wrong_one():
    stored = hash_password("correct-horse-battery")
    assert verify_password("correct-horse-battery", stored)
    assert not verify_password("correct-horse-batterz", stored)


def test_each_hash_has_its_own_salt():
    assert hash_password("same-password-x") != hash_password("same-password-x")


# --- tokens ---


def test_token_round_trips_the_principal():
    token, ttl = issue_access_token(PRINCIPAL)
    assert ttl == SETTINGS.auth.access_token_ttl_s
    assert decode_access_token(token) == PRINCIPAL


def test_token_signed_with_another_key_is_rejected():
    forged = jwt.encode(
        {
            "iss": SETTINGS.auth.jwt_issuer,
            "sub": "u1",
            "tid": "someone-elses-tenant",
            "email": "a@b.test",
            "role": "admin",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        "not-the-server-secret-but-long-enough-to-sign-with",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        decode_access_token(forged)


def test_expired_token_is_rejected():
    now = int(time.time())
    expired = jwt.encode(
        {
            "iss": SETTINGS.auth.jwt_issuer,
            "sub": "u1",
            "tid": "t1",
            "email": "a@b.test",
            "role": "viewer",
            "iat": now - 120,
            "exp": now - 60,
        },
        SETTINGS.auth.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        decode_access_token(expired)


def test_token_with_an_unknown_role_is_rejected():
    token, _ = issue_access_token(
        Principal(user_id="u1", tenant_id="t1", email="a@b.test", role="viewer")
    )
    claims = jwt.decode(
        token, SETTINGS.auth.jwt_secret, algorithms=["HS256"], issuer=SETTINGS.auth.jwt_issuer
    )
    claims["role"] = "superuser"
    tampered = jwt.encode(claims, SETTINGS.auth.jwt_secret, algorithm="HS256")
    with pytest.raises(AuthError):
        decode_access_token(tampered)


# --- roles ---


@pytest.mark.parametrize(
    "role,required,allowed",
    [
        ("viewer", "viewer", True),
        ("viewer", "editor", False),
        ("editor", "editor", True),
        ("editor", "admin", False),
        ("admin", "admin", True),
        ("admin", "viewer", True),
    ],
)
def test_role_ranking(role, required, allowed):
    assert Principal("u", "t", "admin@acme.test", role).can(required) is allowed


def test_admin_is_reserved_to_the_configured_addresses():
    assert Principal("u", "t", "stranger@acme.test", "admin").can("admin") is False
    assert Principal("u", "t", "stranger@acme.test", "admin").can("editor") is True


# --- registration and login ---


def test_register_makes_the_first_user_an_admin(clean_state):
    principal = service.register_tenant("Acme", "Admin@Acme.test", "acme-password-1")
    assert principal.role == "admin"
    assert principal.email == "admin@acme.test"


def test_register_gives_an_unreserved_signup_the_default_role(clean_state):
    principal = service.register_tenant("Initech", "stranger@initech.test", "initech-password-1")
    assert principal.role == "editor"


def test_admin_role_cannot_be_granted_to_an_unreserved_address(tenants):
    with pytest.raises(PermissionDenied):
        service.create_user(tenants.a, "stranger@acme.test", "stranger-password-1", "admin")


def test_duplicate_email_is_refused(clean_state):
    service.register_tenant("Acme", "admin@acme.test", "acme-password-1")
    with pytest.raises(AuthError):
        service.register_tenant("Globex", "admin@acme.test", "globex-password-1")


def test_short_password_is_refused(clean_state):
    with pytest.raises(AuthError):
        service.register_tenant("Acme", "admin@acme.test", "short")


def test_login_returns_the_stored_principal(clean_state):
    registered = service.register_tenant("Acme", "admin@acme.test", "acme-password-1")
    assert service.authenticate("ADMIN@acme.test", "acme-password-1") == registered


def test_login_with_a_wrong_password_and_an_unknown_email_look_the_same(clean_state):
    service.register_tenant("Acme", "admin@acme.test", "acme-password-1")
    with pytest.raises(AuthError) as wrong:
        service.authenticate("admin@acme.test", "not-the-password")
    with pytest.raises(AuthError) as unknown:
        service.authenticate("nobody@acme.test", "not-the-password")
    assert wrong.value.message == unknown.value.message


# --- user management stays inside the tenant ---


def test_admin_creates_users_only_in_its_own_tenant(tenants):
    created = service.create_user(tenants.a, "viewer@acme.test", "acme-password-3", "viewer")
    assert created.tenant_id == tenants.a.tenant_id
    assert [u["email"] for u in service.list_users(tenants.b)] == ["admin@globex.test"]


def test_non_admin_cannot_create_or_list_users(tenants):
    editor = service.create_user(tenants.a, "editor@acme.test", "acme-password-4", "editor")
    with pytest.raises(PermissionDenied):
        service.create_user(editor, "x@acme.test", "acme-password-5", "viewer")
    with pytest.raises(PermissionDenied):
        service.list_users(editor)


def test_unknown_role_is_refused(tenants):
    with pytest.raises(PermissionDenied):
        service.create_user(tenants.a, "x@acme.test", "acme-password-6", "superuser")


# --- audit log ---


def test_audit_log_is_tenant_scoped_and_admin_only(tenants):
    audit.record(tenants.a, "query", num_docs=2)
    audit.record(tenants.b, "query", num_docs=7)

    events_a = audit.read(tenants.a)
    assert [e["detail"]["num_docs"] for e in events_a] == [2]
    assert all(e["email"] == "admin@acme.test" for e in events_a)

    viewer = service.create_user(tenants.a, "viewer@acme.test", "acme-password-7", "viewer")
    with pytest.raises(PermissionDenied):
        audit.read(viewer)
