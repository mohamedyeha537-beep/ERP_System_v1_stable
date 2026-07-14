"""مساعدات نوع قاعدة البيانات وإعدادات المحرك (SQLite / PostgreSQL / MySQL)."""

from __future__ import annotations

from urllib.parse import urlparse

from infra.config import get_settings


def database_url() -> str:
    return (get_settings().database_url or "").strip()


def is_sqlite_url(url: str | None = None) -> bool:
    u = (url or database_url()).lower()
    return u.startswith("sqlite")


def is_postgresql_url(url: str | None = None) -> bool:
    u = (url or database_url()).lower()
    return u.startswith("postgresql") or u.startswith("postgres://")


def is_mysql_url(url: str | None = None) -> bool:
    u = (url or database_url()).lower()
    return u.startswith("mysql") or u.startswith("mariadb")


def is_server_database_url(url: str | None = None) -> bool:
    return is_postgresql_url(url) or is_mysql_url(url)


def database_kind(url: str | None = None) -> str:
    if is_postgresql_url(url):
        return "postgresql"
    if is_mysql_url(url):
        return "mysql"
    if is_sqlite_url(url):
        return "sqlite"
    parsed = urlparse(url or database_url())
    return (parsed.scheme or "unknown").split("+", 1)[0]


def database_label_ar(url: str | None = None) -> str:
    kind = database_kind(url)
    if kind == "postgresql":
        return "PostgreSQL"
    if kind == "mysql":
        return "MySQL"
    if kind == "sqlite":
        return "SQLite"
    return kind


def server_backup_suffix(url: str | None = None) -> str:
    if is_postgresql_url(url):
        return ".dump"
    if is_mysql_url(url):
        return ".sql"
    return ".db"


def engine_connect_args(url: str | None = None) -> dict:
    u = url or database_url()
    if is_sqlite_url(u):
        return {"check_same_thread": False}
    if is_mysql_url(u):
        return {"charset": "utf8mb4", "connect_timeout": 10}
    return {}


def engine_kwargs(url: str | None = None) -> dict:
    """خيارات create_engine — تجميع اتصالات لقواعد السيرفر على VPS."""
    u = url or database_url()
    kwargs: dict = {"connect_args": engine_connect_args(u)}
    if is_server_database_url(u):
        s = get_settings()
        kwargs.update(
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_pre_ping=s.db_pool_pre_ping,
            pool_recycle=s.db_pool_recycle,
        )
    return kwargs
