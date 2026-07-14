#!/usr/bin/env python3
"""التحقق من أن إعدادات الطباعة الحرارية لا تزال مطابقة للنسخة المرجعية."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).resolve().parent / "reference" / "MANIFEST.json"


def _read(rel: str) -> str:
    p = ROOT / rel.replace("/", "\\")
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")


def main() -> int:
    if not MANIFEST.is_file():
        print(f"MANIFEST مفقود: {MANIFEST}")
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    errors: list[str] = []

    for rel, markers in manifest.get("required_markers", {}).items():
        text = _read(rel)
        if not text:
            errors.append(f"ملف مفقود: {rel}")
            continue
        for m in markers:
            if m not in text:
                errors.append(f"{rel}: لم يُعثر على «{m}»")

    forbidden = manifest.get("forbidden_patterns", [])
    capture = _read("app/static/receipt_capture.js")
    for bad in forbidden:
        if bad in capture:
            errors.append(
                f"receipt_capture.js: يحتوي على «{bad}» — يسبب تصغير/قص الفاتورة"
            )

    if errors:
        print("Receipt print settings verification FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    vid = manifest.get("id", "?")
    print(f"OK - receipt print settings match reference ({vid})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
