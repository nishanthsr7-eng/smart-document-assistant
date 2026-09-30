import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The admin role is reserved to ADMIN_EMAILS; the fixtures below sign up as admins, so the
# reservation has to name them before src.core.config is first imported.
os.environ.setdefault("ADMIN_EMAILS", "admin@acme.test,admin@globex.test")


def _stack_available() -> bool:
    from sqlalchemy import text

    from src.storage import objects
    from src.storage.db import engine
    from src.storage.redis_client import client

    try:
        with engine().connect() as conn:
            conn.execute(text("select 1"))
        client().ping()
        objects.ensure_bucket()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def storage_stack():
    """Real Postgres/Redis/MinIO from docker-compose; integration tests skip without them."""
    if not _stack_available():
        pytest.skip("shared-state stack unavailable (run: docker compose up -d)")

    from alembic import command
    from alembic.config import Config

    from src.core.config import ROOT_DIR

    config = Config(str(ROOT_DIR / "alembic.ini"))
    command.upgrade(config, "head")
    yield


@pytest.fixture
def tenants(clean_state):
    """Two registered tenants. The isolation tests run every assertion across this pair."""
    from src.auth import service

    return SimpleNamespace(
        a=service.register_tenant("Acme", "admin@acme.test", "acme-password-1"),
        b=service.register_tenant("Globex", "admin@globex.test", "globex-password-1"),
    )


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Rate-limit buckets are tenant-keyed shared state that outlives a test. Without this the
    order the suite runs in would decide which requests come back 429."""
    import redis

    from src.storage.redis_client import client

    try:
        keys = client().keys("ratelimit:*") + client().keys("budget:*")
    except redis.RedisError:
        return
    if keys:
        client().delete(*keys)


@pytest.fixture
def clean_state(storage_stack):
    _reset()
    yield
    _reset()


def _reset() -> None:
    # Every tenant, including the evaluation corpus: do not run the suite against a stack that
    # an evaluation run is using.
    from sqlalchemy import delete

    from src.core.cache import ANSWER_CACHE, DOC_CACHE
    from src.storage.db import session
    from src.storage.models import AuditEvent, Document, Tenant

    with session() as sess:
        # Tenants cascade to users and documents, and documents cascade to chunks.
        sess.execute(delete(Document))
        sess.execute(delete(Tenant))
        sess.execute(delete(AuditEvent))
    ANSWER_CACHE.clear()
    DOC_CACHE.clear()
