from __future__ import annotations

import re


def render_template(body: str, variables: dict) -> str:
    """استبدال {var} في نص القالب — بدون محرك Jinja ثقيل."""
    out = body or ""

    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        val = variables.get(key, "")
        return str(val) if val is not None else ""

    out = re.sub(r"\{(\w+)\}", repl, out)
    # إزالة أي أكواد {variable} لم تُستبدل
    out = re.sub(r"\{(\w+)\}", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
