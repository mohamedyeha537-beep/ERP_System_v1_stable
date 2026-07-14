#!/usr/bin/env python
"""تعيين موظفي Clikk (200، 300، 400) لنفس Area مثل جهاز restaurant."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZK_ROOT = Path(r"C:\ZKBioTime")

# Area جهاز restaurant (من ZKBioTime)
TARGET_AREA_CODE = "2"  # area_name: ress
CLIKK_EMP_CODES = ("200", "300", "400")


def _bootstrap_django() -> None:
    if not ZK_ROOT.is_dir():
        print("ERROR: ZKBioTime غير موجود")
        sys.exit(1)
    sys.path.insert(0, str(ZK_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")
    os.chdir(ZK_ROOT)
    import django

    django.setup()


def assign_areas() -> None:
    from mysite.personnel.models import Area, Employee

    area = Area.objects.filter(area_code=TARGET_AREA_CODE).first()
    if area is None:
        area = Area.objects.filter(area_name__icontains="ress").first()
    if area is None:
        print("ERROR: Area غير موجودة")
        sys.exit(1)

    print(f"Area: {area.area_code} — {area.area_name} (id={area.id})")

    for code in CLIKK_EMP_CODES:
        emp = Employee.objects.filter(emp_code=code).first()
        if emp is None:
            print(f"! موظف {code} غير موجود")
            continue
        # personnel_employee_area — M2M through Employee.area or similar
        if hasattr(emp, "area"):
            emp.area.set([area])
            print(f"✓ {code} — {emp.first_name} → {area.area_name}")
        elif hasattr(emp, "areas"):
            emp.areas.set([area])
            print(f"✓ {code} — {emp.first_name} → {area.area_name}")
        else:
            _assign_via_raw(emp.id, area.id, code, emp.first_name)


def _assign_via_raw(employee_id: int, area_id: int, code: str, name: str) -> None:
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            "DELETE FROM personnel_employee_area WHERE employee_id = %s",
            [employee_id],
        )
        cur.execute(
            """
            INSERT INTO personnel_employee_area (employee_id, area_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            [employee_id, area_id],
        )
    print(f"✓ {code} — {name} → area id {area_id} (SQL)")


def main() -> None:
    _bootstrap_django()
    assign_areas()
    print("تم — حدّث صفحة Personnel (F5)")


if __name__ == "__main__":
    main()
