#!/usr/bin/env python
"""Register real ZKTeco fingerprint device in ZKBioTime.

Run:
  C:\\ZKBioTime\\Python311\\python.exe tools/zkbio_add_device.py UFS2255100070

Optional IP (default 127.0.0.1 for USB/local bridge):
  ... tools/zkbio_add_device.py UFS2255100070 192.168.1.201
"""
from __future__ import annotations

import os
import sys

ZK_ROOT = r"C:\ZKBioTime"
REAL_SN_DEFAULT = "UFS2255100070"

if not os.path.isdir(ZK_ROOT):
    print("ERROR: ZKBioTime not found at", ZK_ROOT)
    sys.exit(1)

sys.path.insert(0, ZK_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")
os.chdir(ZK_ROOT)

import django  # noqa: E402

django.setup()

from mysite.iclock.models import Terminal  # noqa: E402
from mysite.personnel.models import Area, Company  # noqa: E402


def _area() -> Area | None:
    company = Company.objects.first()
    if company is None:
        return Area.objects.exclude(area_code="1").first()
    area, _ = Area.objects.get_or_create(
        area_code="CAFE",
        defaults={
            "area_name": "Roof Cafe",
            "is_default": False,
            "company": company,
            "device_count": 0,
            "employee_count": 0,
        },
    )
    if area.is_default:
        area.is_default = False
        area.save(update_fields=["is_default"])
    return area


def main() -> None:
    sn = (sys.argv[1] if len(sys.argv) > 1 else REAL_SN_DEFAULT).strip().upper()
    ip = (sys.argv[2] if len(sys.argv) > 2 else "192.168.1.201").strip() or "192.168.1.201"
    if not sn:
        print("Usage: zkbio_add_device.py SERIAL_NUMBER [IP]")
        sys.exit(1)

    area = _area()

    matches = list(Terminal.objects.filter(sn__iexact=sn))
    if len(matches) > 1:
        keep = next((t for t in matches if t.state == 1), matches[0])
        for t in matches:
            if t.id != keep.id:
                t.delete()
        term = keep
        created = False
    elif matches:
        term = matches[0]
        created = False
    else:
        term = Terminal(
            sn=sn,
            alias="Roof Cafe Fingerprint",
            ip_address=ip,
            state=0,
            heartbeat=10,
            push_protocol="2.4.1",
            is_attendance=1,
            area=area,
        )
        term.save()
        created = True

    term.sn = sn
    term.alias = "Roof Cafe Fingerprint"
    term.ip_address = ip
    term.is_attendance = 1
    if area is not None:
        term.area = area
    term.save()

    print("ZKBioTime device", "created" if created else "updated")
    print("  SN:   ", term.sn)
    print("  IP:   ", term.ip_address)
    print("  Area: ", term.area.area_name if term.area else "—")
    print("  State:", term.state, "(1=online when device talks to server)")
    print()
    print("Device cloud settings on hardware:")
    print("  Server IP: 192.168.1.10")
    print("  Port:      9098 (ZKBioTime native port — do not run portproxy unless iclock is on 80)")
    print()
    print("Next: enroll employee fingerprints in ZKBioTime → Personnel → Enroll")
    print("Map same emp codes (001, 002…) in POS employee profiles.")


if __name__ == "__main__":
    main()
