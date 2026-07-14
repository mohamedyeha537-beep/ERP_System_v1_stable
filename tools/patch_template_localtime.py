"""Replace .strftime() with pos_format_dt() in Jinja templates."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app" / "templates"
PAT = re.compile(r"([\w\.]+)\.strftime\((['\"])([^'\"]+)\2\)")


def patch_line(line: str) -> str:
    if ".strftime(" not in line:
        return line

    def repl(m: re.Match[str]) -> str:
        obj = m.group(1)
        fmt = m.group(3)
        return f"pos_format_dt({obj}, '{fmt}')"

    return PAT.sub(repl, line)


def main() -> None:
    count = 0
    for path in ROOT.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        new_text = "\n".join(patch_line(l) for l in text.splitlines())
        if text.endswith("\n") and not new_text.endswith("\n"):
            new_text += "\n"
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            count += 1
            print(path.relative_to(ROOT.parent.parent))
    print(f"updated {count} files")


if __name__ == "__main__":
    main()
