from collections.abc import Generator
from threading import Lock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from infra.database import engine_kwargs, is_sqlite_url


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str):
    kwargs = engine_kwargs(database_url)
    eng = create_engine(database_url, **kwargs)
    if is_sqlite_url(database_url):

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _connection_record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.close()

    return eng


_engine = None
_session_factory: sessionmaker[Session] | None = None
_lock = Lock()


def reset_engine() -> None:
    """إعادة تهيئة المحرك بعد تغيير DATABASE_URL في .env."""
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None


def get_engine():
    global _engine, _session_factory
    from infra.config import get_settings

    with _lock:
        if _engine is None:
            _engine = make_engine(get_settings().database_url)
            _session_factory = sessionmaker(
                autocommit=False, autoflush=False, expire_on_commit=False, bind=_engine
            )
        return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()
