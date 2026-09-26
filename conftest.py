import sys
from pathlib import Path

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
def clean_state(storage_stack):
    _reset()
    yield
    _reset()


def _reset() -> None:
    from sqlalchemy import delete

    from src.core.cache import ANSWER_CACHE, DOC_CACHE
    from src.storage.db import session
    from src.storage.models import Document

    with session() as sess:
        sess.execute(delete(Document))
    ANSWER_CACHE.clear()
    DOC_CACHE.clear()
