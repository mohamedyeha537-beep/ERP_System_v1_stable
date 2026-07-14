#!/usr/bin/env python
"""إعداد موظفي تجربة Clikk في ZKBioTime ثم مزامنتهم إلى POS.

أكواد الموظفين: 200، 300، 400 (بدون 100 — محجوز للسوبر أدمن على الجهاز).

تشغيل:
  C:\\ZKBioTime\\Python311\\python.exe tools/clikk_zk_demo_setup.py
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZK_ROOT = Path(r"C:\ZKBioTime")

# (emp_code, first_name, last_name, mobile)
CLIKK_DEMO_EMPLOYEES = [
    ("200", "محمد", "الزاوي", "0911000200"),
    ("300", "فاطمة", "العبيدي", "0911000300"),
    ("400", "عمر", "الحسن", "0911000400"),
]

# أكواد قديمة للترقية التلقائية
_LEGACY_CODE_MAP = {
    "001": "200",
    "002": "300",
    "003": "400",
    "1": "200",
}


def _bootstrap_django() -> None:
    if not ZK_ROOT.is_dir():
        print("ERROR: ZKBioTime غير موجود في", ZK_ROOT)
        sys.exit(1)
    sys.path.insert(0, str(ZK_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")
    os.chdir(ZK_ROOT)
    import django

    django.setup()


def seed_zkbio_employees() -> list[tuple[str, str]]:
    from mysite.personnel.models import Area, Company, Department, Employee

    company = Company.objects.first()
    if company is None:
        print("ERROR: أكمل إعداد ZKBioTime أولاً (Company).")
        sys.exit(1)

    dept, _ = Department.objects.get_or_create(
        dept_code="CLIKK",
        defaults={"dept_name": "Clikk — تجربة", "is_default": False},
    )
    dept.dept_name = "Clikk — تجربة"
    dept.save(update_fields=["dept_name"])

    for old_code, new_code in _LEGACY_CODE_MAP.items():
        legacy = Employee.objects.filter(emp_code=old_code).first()
        if legacy is None:
            continue
        if Employee.objects.filter(emp_code=new_code).exclude(id=legacy.id).exists():
            legacy.is_active = False
            legacy.save(update_fields=["is_active"])
            print(f"- تعطيل كود قديم {old_code} (الهدف {new_code} موجود)")
            continue
        legacy.emp_code = new_code
        legacy.is_active = True
        legacy.status = 0
        legacy.save()
        print(f"= ترقية {old_code} → {new_code}")

    created_names: list[tuple[str, str]] = []
    for code, first, last, mobile in CLIKK_DEMO_EMPLOYEES:
        full = f"{first} {last}"
        emp, created = Employee.objects.get_or_create(
            emp_code=code,
            defaults={
                "first_name": first,
                "last_name": last,
                "department": dept,
                "company": company,
                "hire_date": date.today(),
                "enable_payroll": True,
                "is_active": True,
                "status": 0,
                "mobile": mobile,
            },
        )
        emp.first_name = first
        emp.last_name = last
        emp.department = dept
        emp.company = company
        emp.mobile = mobile
        emp.is_active = True
        emp.status = 0
        emp.save()
        tag = "+" if created else "="
        print(f"{tag} ZKBioTime {code} — {full} ({mobile})")
        created_names.append((code, full))

    _assign_clikk_area(Employee, Area)

    return created_names


def _assign_clikk_area(Employee, Area) -> None:
    """نفس Area جهاز restaurant (ress) لكل موظفي Clikk."""
    area = Area.objects.filter(area_code="2").first()
    if area is None:
        area = Area.objects.filter(area_name__icontains="ress").first()
    if area is None:
        print("! Area ress غير موجودة — عيّنها يدوياً من ZKBioTime")
        return
    for code, *_ in CLIKK_DEMO_EMPLOYEES:
        emp = Employee.objects.filter(emp_code=code).first()
        if emp is None:
            continue
        if hasattr(emp, "area"):
            emp.area.set([area])
        elif hasattr(emp, "areas"):
            emp.areas.set([area])
    print(f"= Area: {area.area_name} لـ 200، 300، 400")


def sync_to_pos() -> None:
    import subprocess

    sync_script = ROOT / "tools" / "clikk_zk_sync_employees.py"
    py = shutil_which_python()
    proc = subprocess.run([py, str(sync_script)], cwd=str(ROOT))
    if proc.returncode != 0:
        sys.exit(proc.returncode)


def shutil_which_python() -> str:
    import shutil

    for candidate in (
        shutil.which("python"),
        r"C:\Users\PIXEL\AppData\Local\Programs\Python\Python313\python.exe",
        sys.executable,
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return "python"


def main() -> None:
    print("Clikk — موظفو تجربة (200، 300، 400)")
    print("=" * 48)
    _bootstrap_django()
    seed_zkbio_employees()
    print()
    print("→ مزامنة إلى POS (Clikk)...")
    sync_to_pos()
    print()
    print("تم.")
    print("1) ZKBioTime → Personnel: أكواد 200، 300، 400")
    print("2) Device → Data Transfer → Upload employees to device")
    print("3) POS → /admin/attendance → مزامنة البصمات")


if __name__ == "__main__":
    main()
