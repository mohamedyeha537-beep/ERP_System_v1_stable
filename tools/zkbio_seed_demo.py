#!/usr/bin/env python
"""Seed demo data into local ZKBioTime (employees, department, terminal, punches).

Run from repo:
  C:\\ZKBioTime\\Python311\\python.exe tools/zkbio_seed_demo.py

Requires ZKBioTime installed at C:\\ZKBioTime and services running.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

ZK_ROOT = r"C:\ZKBioTime"
if not os.path.isdir(ZK_ROOT):
    print("ERROR: ZKBioTime not found at", ZK_ROOT)
    sys.exit(1)

sys.path.insert(0, ZK_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")
os.chdir(ZK_ROOT)

import django  # noqa: E402

django.setup()

from django.utils import timezone  # noqa: E402

from mysite.iclock.models import Terminal, Transaction  # noqa: E402
from mysite.personnel.models import Area, Company, Department, Employee  # noqa: E402


DEMO_DEPT = ("CAFE", "Roof Cafe")
DEMO_TERMINAL_SN = "DEMO-T001"
DEMO_TERMINAL_IP = "127.0.0.1"

DEMO_EMPLOYEES = [
    ("001", "Ahmed", "Cashier"),
    ("002", "Sara", "Barista"),
    ("003", "Omar", "Kitchen"),
    ("004", "Fatima", "Waiter"),
    ("005", "Khalid", "Manager"),
]


def _dept() -> Department:
    dept, created = Department.objects.get_or_create(
        dept_code=DEMO_DEPT[0],
        defaults={"dept_name": DEMO_DEPT[1], "is_default": True},
    )
    if created:
        print(f"+ Department {dept.dept_code} — {dept.dept_name}")
    else:
        print(f"= Department {dept.dept_code} exists")
    return dept


def _area() -> Area:
    company = Company.objects.first()
    if company is None:
        raise RuntimeError("ZKBioTime has no company record — complete initial setup first.")
    area, created = Area.objects.get_or_create(
        area_code="CAFE",
        defaults={
            "area_name": "Roof Cafe",
            "is_default": False,
            "company": company,
            "device_count": 0,
            "employee_count": 0,
        },
    )
    if created:
        print(f"+ Area {area.area_code} — {area.area_name}")
    else:
        print(f"= Area {area.area_code} exists")
    return area


def _terminal(area: Area) -> Terminal:
    term, created = Terminal.objects.get_or_create(
        sn=DEMO_TERMINAL_SN,
        defaults={
            "alias": "Demo Fingerprint",
            "ip_address": DEMO_TERMINAL_IP,
            "state": 1,
            "heartbeat": 10,
            "push_protocol": "2.4.1",
            "is_attendance": 1,
            "area": area,
        },
    )
    if not created:
        term.state = 1
        term.alias = "Demo Fingerprint"
        term.area = area
        term.save(update_fields=["state", "alias", "area"])
        print(f"= Terminal {term.sn} updated (online)")
    else:
        print(f"+ Terminal {term.sn} — {term.alias}")
    return term


def _employees(dept: Department) -> list[Employee]:
    company = Company.objects.first()
    if company is None:
        raise RuntimeError("ZKBioTime has no company record — complete initial setup first.")
    out: list[Employee] = []
    for code, first, job in DEMO_EMPLOYEES:
        emp, created = Employee.objects.get_or_create(
            emp_code=code,
            defaults={
                "first_name": first,
                "last_name": job,
                "department": dept,
                "company": company,
                "hire_date": date.today(),
                "enable_payroll": True,
                "is_active": True,
                "status": 0,
            },
        )
        if not created:
            emp.first_name = first
            emp.last_name = job
            emp.department = dept
            emp.company = company
            emp.is_active = True
            emp.status = 0
            emp.save()
            print(f"= Employee {code} — {first} ({job})")
        else:
            print(f"+ Employee {code} — {first} ({job})")
        out.append(emp)
    return out


def _punch(
    emp_code: str,
    when: datetime,
    state: str,
    terminal: Terminal,
) -> None:
    exists = Transaction.objects.filter(
        emp_code=emp_code,
        punch_time=when,
        punch_state=state,
    ).exists()
    if exists:
        return
    Transaction.objects.create(
        emp_code=emp_code,
        punch_time=when,
        punch_state=state,
        verify_type=1,
        terminal_sn=terminal.sn,
        terminal_alias=terminal.alias,
        upload_time=timezone.now(),
    )


def _seed_punches(terminal: Terminal) -> int:
    """Today + yesterday check-in/out for demo employees."""
    today = date.today()
    days = [today - timedelta(days=1), today]
    schedules = {
        "001": ("08:02", "16:05"),
        "002": ("08:10", "16:00"),
        "003": ("07:55", "15:50"),
        "004": ("09:00", "17:10"),
        "005": ("08:00", "16:30"),
    }
    n = 0
    for d in days:
        for code, (t_in, t_out) in schedules.items():
            hi, mi = map(int, t_in.split(":"))
            ho, mo = map(int, t_out.split(":"))
            cin = timezone.make_aware(datetime(d.year, d.month, d.day, hi, mi, 0))
            cout = timezone.make_aware(datetime(d.year, d.month, d.day, ho, mo, 0))
            _punch(code, cin, "0", terminal)
            _punch(code, cout, "1", terminal)
            n += 2
    return n


def main() -> None:
    print("ZKBioTime demo seed — Roof Cafe")
    print("=" * 40)
    dept = _dept()
    area = _area()
    terminal = _terminal(area)
    _employees(dept)
    added = _seed_punches(terminal)
    print(f"+ Attendance punches ensured ({added} new pairs target)")
    print()
    print("Done. Refresh ZKBioTime dashboard (F5).")
    print("Personnel → Employees: codes 001–005")
    print("Attendance → Transactions: today/yesterday punches")
    print()
    print("Map same emp_code in POS when HR sync is enabled.")


if __name__ == "__main__":
    main()
