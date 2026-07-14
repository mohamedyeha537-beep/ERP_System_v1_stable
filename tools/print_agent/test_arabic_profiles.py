#!/usr/bin/env python3
"""اختبار ملفات ترميز العربية على الطابعة — شغّل مرة واحدة لاختيار الأنسب."""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

# استيراد من نفس مجلد الوكيل
sys.path.insert(0, str(Path(__file__).resolve().parent))
from print_agent import escpos_encode, load_config  # noqa: E402

PROFILES = ("utf8", "cp720_21", "cp1256_22")
SAMPLE = "مطعم روف\nأمر تجهيز\nالكاشير: أحمد\nاختبار 123"


def send_tcp(host: str, port: int, data: bytes) -> None:
    with socket.create_connection((host, port), timeout=8) as sock:
        sock.sendall(data)


def main() -> int:
    cfg_path = Path(__file__).resolve().parent / "config.json"
    cfg = load_config(cfg_path)
    host = cfg.get("printers", {}).get("cashier", {}).get("host", "192.168.1.100")
    port = int(cfg.get("printers", {}).get("cashier", {}).get("port", 9100))
    fake_job = {"payload": json.dumps({"printer": {"local_printer_key": "cashier"}})}

    print(f"إرسال اختبار إلى {host}:{port}")
    for i, profile in enumerate(PROFILES, 1):
        cfg_run = {**cfg, "escpos_profile": profile}
        label = f"--- [{i}] {profile} ---"
        body = escpos_encode(f"{label}\n{SAMPLE}", cfg_run, fake_job)
        print(f"طباعة {profile}...")
        send_tcp(host, port, body)
        input("اضغط Enter للملف التالي...")

    print("اختر الملف الذي ظهرت العربية صحيحة فيه وضعه في config.json:")
    print('  "escpos_profile": "utf8"  أو  "cp720_21"  أو  "cp1256_22"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
