"""Engine and session factory for Platen's own database. `bind` exists so the
API, the worker and the test suite can each point it somewhere before the
first session is opened."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

SessionLocal: sessionmaker[Session] = sessionmaker(expire_on_commit=False)
_engine: Engine | None = None


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"timeout": 30})
        # WAL so the worker's reads never block the API's cancel write
        event.listen(eng, "connect", lambda con, _: con.execute("pragma journal_mode=wal"))
        return eng
    return create_engine(url, pool_pre_ping=True)


def bind(engine: Engine) -> None:
    global _engine
    _engine = engine
    SessionLocal.configure(bind=engine)


def engine() -> Engine:
    if _engine is None:
        from app.settings import settings

        bind(make_engine(settings.database_url))
    assert _engine is not None
    return _engine


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, rolled back on error."""
    engine()
    with SessionLocal() as s:
        yield s
