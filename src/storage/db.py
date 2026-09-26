from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import SETTINGS


@lru_cache(maxsize=1)
def engine() -> Engine:
    return create_engine(
        SETTINGS.storage.database_url,
        pool_size=SETTINGS.storage.pool_size,
        max_overflow=SETTINGS.storage.pool_max_overflow,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=engine(), expire_on_commit=False, future=True)


@contextmanager
def session() -> Iterator[Session]:
    sess = _session_factory()()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()
