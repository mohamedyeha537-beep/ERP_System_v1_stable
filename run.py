import os
import sys

import uvicorn


def _resolve_port() -> int:
    """يشغّل التطبيق على 8010 افتراضياً مع إمكانية تمرير بورت آخر."""
    raw = os.getenv("POS_PORT") or os.getenv("PORT") or "8010"
    if len(sys.argv) > 1:
        raw = sys.argv[1]
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 8010


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=_resolve_port(), reload=True)
