import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


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


@pytest.fixture
def clean_state(storage_stack):
    _reset()
    yield
    _reset()


def _reset() -> None:
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
