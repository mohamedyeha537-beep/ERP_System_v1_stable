"""ترقيع أعمدة غرفة التسويق عند التشغيل."""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

LOG = logging.getLogger("marketing_room.schema")


def ensure_marketing_room_schema(engine: Engine) -> None:
    try:
        insp = inspect(engine)
        if "marketing_artifacts" not in insp.get_table_names():
            return
        cols = {c["name"] for c in insp.get_columns("marketing_artifacts")}
        if "media_path" in cols:
            return
        dialect = engine.dialect.name
        ddl = (
            "ALTER TABLE marketing_artifacts ADD COLUMN media_path VARCHAR(500) NULL"
            if dialect != "sqlite"
            else "ALTER TABLE marketing_artifacts ADD COLUMN media_path VARCHAR(500)"
        )
        with engine.begin() as conn:
            conn.execute(text(ddl))
        LOG.info("added marketing_artifacts.media_path")
    except Exception as exc:
        LOG.warning("ensure_marketing_room_schema: %s", exc)
