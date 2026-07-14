from __future__ import annotations

import json
import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def render_template(body: str, variables: dict[str, Any]) -> str:
    out = body or ""
    for key, val in variables.items():
        s = str(val) if val is not None else ""
        out = out.replace("{{" + key + "}}", s)
        out = out.replace("{" + key + "}", s)
    # إزالة أي أكواد {variable} لم تُستبدل — حتى لا تصل للعميل
    out = _PLACEHOLDER_RE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def parse_buttons_json(raw: str | None, variables: dict[str, Any]) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data[:3]:
        if not isinstance(item, dict):
            continue
        text = render_template(str(item.get("text") or item.get("label") or ""), variables)
        action = render_template(str(item.get("id") or item.get("action") or ""), variables)
        if text and action:
            out.append({"text": text, "id": action})
    return out
