"""إنشاء/ترقية مخطط قاعدة البيانات (مشترك بين التطبيق وسكربت النقل)."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from infra.database import is_server_database_url, is_sqlite_url
from infra.db import Base, get_engine
from infra.server_schema_patch import patch_server_schema
from infra.sqlite_patch import patch_sqlite_schema

_schema_patched_once = False


def ensure_schema_patched(*, force: bool = False) -> None:
    """ترقية idempotent — تُستدعى عند الإقلاع وأول طلب كatalog إن لزم."""
    global _schema_patched_once
    if _schema_patched_once and not force:
        return
    engine = get_engine()
    url = str(engine.url)
    if is_sqlite_url(url):
        patch_sqlite_schema(engine)
    elif is_server_database_url(url):
        patch_server_schema(engine)
    _schema_patched_once = True


def reset_schema_patch_flag() -> None:
    """إعادة محاولة الترقية (بعد خطأ عمود ناقص)."""
    global _schema_patched_once
    _schema_patched_once = False


def bootstrap_schema(engine: Engine) -> None:
    """ينشئ الجداول من النماذج، ثم يطبّق ترقيات SQLite القديمة إن لزم."""
    Base.metadata.create_all(bind=engine)
    if is_sqlite_url(str(engine.url)):
        patch_sqlite_schema(engine)
    elif is_server_database_url(str(engine.url)):
        patch_server_schema(engine)
    global _schema_patched_once
    _schema_patched_once = True