"""مزامنة موظفي ZKBioTime ↔ POS (Clikk)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.hr.models import Employee, EmployeeStatus, PayType
from modules.hr import service as hr
from modules.hr.zkbio_config import ZkBioDbConfig, get_zkbio_db_config

log = logging.getLogger("hr.zkbio_employee_sync")


@dataclass
class ZkPersonRow:
    emp_code: str
    first_name: str
    last_name: str
    mobile: str
    is_active: bool
    department_name: str | None = None


@dataclass
class EmployeeSyncResult:
    ok: bool
    message: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    codes_assigned: int = 0
    errors: list[str] = field(default_factory=list)


def _connect(cfg: ZkBioDbConfig):
    import psycopg2

    return psycopg2.connect(cfg.dsn())


def fetch_zkbio_employees(cfg: ZkBioDbConfig) -> list[ZkPersonRow]:
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.emp_code, e.first_name, e.last_name,
                       COALESCE(NULLIF(TRIM(e.mobile), ''), NULLIF(TRIM(e.contact_tel), ''), ''),
                       e.is_active, d.dept_name
                FROM personnel_employee e
                LEFT JOIN personnel_department d ON d.id = e.department_id
                WHERE TRIM(COALESCE(e.emp_code, '')) <> ''
                ORDER BY e.emp_code
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    out: list[ZkPersonRow] = []
    for code, fn, ln, mob, active, dept in rows:
        out.append(
            ZkPersonRow(
                emp_code=(code or "").strip(),
                first_name=(fn or "").strip(),
                last_name=(ln or "").strip(),
                mobile=(mob or "").strip(),
                is_active=bool(active),
                department_name=(dept or "").strip() or None,
            )
        )
    return out


def _display_name(row: ZkPersonRow) -> str:
    parts = [row.first_name, row.last_name]
    name = " ".join(p for p in parts if p).strip()
    return name or f"موظف {row.emp_code}"


def _default_shift_id(db: Session) -> int | None:
    shifts = hr.list_work_shifts(db, only_active=True)
    return shifts[0].id if shifts else None


def sync_employees_from_zkbio(db: Session) -> EmployeeSyncResult:
    """يستورد/يحدّث موظفي POS من personnel_employee في ZKBioTime."""
    cfg = get_zkbio_db_config(db)
    if cfg is None:
        return EmployeeSyncResult(ok=False, message="إعدادات ZKBioTime غير متوفرة.")

    try:
        zk_rows = fetch_zkbio_employees(cfg)
    except Exception as exc:  # noqa: BLE001
        msg = f"تعذّر قراءة موظفي ZKBioTime: {exc}"
        log.warning(msg)
        return EmployeeSyncResult(ok=False, message=msg)

    if not zk_rows:
        return EmployeeSyncResult(ok=False, message="لا يوجد موظفون في ZKBioTime.")

    result = EmployeeSyncResult(ok=True, message="")
    shift_id = _default_shift_id(db)

    for row in zk_rows:
        code = row.emp_code
        name = _display_name(row)
        try:
            existing = db.scalar(
                select(Employee).where(Employee.zk_emp_code == code)
            )
            if existing is None:
                existing = db.scalar(
                    select(Employee).where(Employee.full_name_ar == name)
                )
            status = EmployeeStatus.ACTIVE if row.is_active else EmployeeStatus.SUSPENDED
            job = row.department_name or "موظف"
            if existing is None:
                hr.upsert_employee(
                    db,
                    emp_id=None,
                    full_name_ar=name,
                    job_title=job,
                    national_id=None,
                    phone=row.mobile or None,
                    pay_type=PayType.MONTHLY,
                    base_monthly_salary=Decimal("1000"),
                    hourly_rate=Decimal("0"),
                    standard_hours_per_day=Decimal("8"),
                    overtime_multiplier=Decimal("1.5"),
                    status=status,
                    hired_at=datetime.now(timezone.utc),
                    work_shift_id=shift_id,
                    zk_emp_code=code,
                    notes="Clikk ← ZKBioTime",
                )
                result.created += 1
            else:
                hr.upsert_employee(
                    db,
                    emp_id=existing.id,
                    full_name_ar=name,
                    job_title=existing.job_title or job,
                    national_id=existing.national_id,
                    phone=row.mobile or existing.phone,
                    pay_type=existing.pay_type,
                    base_monthly_salary=existing.base_monthly_salary,
                    hourly_rate=existing.hourly_rate,
                    standard_hours_per_day=existing.standard_hours_per_day,
                    overtime_multiplier=existing.overtime_multiplier,
                    status=status,
                    hired_at=existing.hired_at,
                    user_id=existing.user_id,
                    notes=existing.notes,
                    is_pos_cashier=existing.is_pos_cashier,
                    is_pos_supervisor=existing.is_pos_supervisor,
                    work_shift_id=existing.work_shift_id or shift_id,
                    zk_emp_code=code,
                )
                result.updated += 1
        except hr.HRError as exc:
            result.errors.append(f"{code} {name}: {exc}")
            result.skipped += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{code}: {exc}")
            result.skipped += 1
            log.exception("sync employee %s failed", code)

    parts = []
    if result.created:
        parts.append(f"جديد {result.created}")
    if result.updated:
        parts.append(f"تحديث {result.updated}")
    if result.skipped:
        parts.append(f"تخطي {result.skipped}")
    result.message = "مزامنة الموظفين: " + ("، ".join(parts) if parts else "لا تغيير")
    return result


_RESERVED_ZK_CODES = frozenset({"100"})


def _split_ar_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split(None, 1)
    if not parts:
        return "موظف", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _emp_code_digit(code: str) -> int | None:
    code = (code or "").strip()
    if code.isdigit():
        return int(code)
    return None


def _collect_used_zk_codes(cur) -> set[str]:
    cur.execute(
        "SELECT TRIM(emp_code) FROM personnel_employee WHERE TRIM(COALESCE(emp_code, '')) <> ''"
    )
    return {str(r[0]).strip() for r in cur.fetchall() if r[0]}


def _next_free_code(used: set[str], *, start: int = 200) -> str:
    used.update(_RESERVED_ZK_CODES)
    n = max(start, 200)
    while str(n) in used:
        n += 1
    code = str(n)
    used.add(code)
    return code


def _zk_defaults(cur) -> dict:
    cur.execute(
        """
        SELECT company_id, department_id, position_id
        FROM personnel_employee
        WHERE company_id IS NOT NULL
        ORDER BY id
        LIMIT 1
        """
    )
    row = cur.fetchone()
    company_id = int(row[0] or 1) if row else 1
    department_id = int(row[1] or 1) if row else 1
    position_id = int(row[2] or 1) if row else 1

    cur.execute(
        "SELECT id FROM personnel_department WHERE dept_code = %s LIMIT 1",
        ("CLIKK",),
    )
    dept = cur.fetchone()
    if dept:
        department_id = int(dept[0])

    cur.execute(
        """
        SELECT id FROM personnel_area
        WHERE area_code = %s OR area_name ILIKE %s
        ORDER BY id
        LIMIT 1
        """,
        ("2", "%ress%"),
    )
    area = cur.fetchone()
    area_id = int(area[0]) if area else None

    return {
        "company_id": company_id,
        "department_id": department_id,
        "position_id": position_id,
        "area_id": area_id,
    }


def _assign_zk_area(cur, employee_id: int, area_id: int) -> None:
    cur.execute(
        "DELETE FROM personnel_employee_area WHERE employee_id = %s",
        (employee_id,),
    )
    cur.execute(
        """
        INSERT INTO personnel_employee_area (employee_id, area_id)
        VALUES (%s, %s)
        """,
        (employee_id, area_id),
    )


def _upsert_zk_employee(
    cur,
    *,
    code: str,
    first_name: str,
    last_name: str,
    mobile: str,
    is_active: bool,
    defaults: dict,
) -> tuple[int, bool]:
    cur.execute(
        "SELECT id FROM personnel_employee WHERE TRIM(emp_code) = %s",
        (code,),
    )
    row = cur.fetchone()
    status = 0 if is_active else 1
    mobile = (mobile or "").strip()
    digit = _emp_code_digit(code)

    if row:
        emp_id = int(row[0])
        cur.execute(
            """
            UPDATE personnel_employee SET
                first_name = %s,
                last_name = %s,
                mobile = %s,
                contact_tel = %s,
                is_active = %s,
                status = %s,
                department_id = %s,
                company_id = %s,
                enable_payroll = TRUE,
                update_time = NOW()
            WHERE id = %s
            """,
            (
                first_name,
                last_name,
                mobile,
                mobile,
                is_active,
                status,
                defaults["department_id"],
                defaults["company_id"],
                emp_id,
            ),
        )
        return emp_id, False

    cur.execute(
        """
        INSERT INTO personnel_employee (
            emp_code, first_name, last_name, mobile, contact_tel,
            is_active, status, company_id, department_id, position_id,
            enable_payroll, emp_type, verify_mode, app_status, app_role,
            emp_code_digit, hire_date, create_time, update_time
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            TRUE, 1, 0, 0, 1,
            %s, CURRENT_DATE, NOW(), NOW()
        )
        RETURNING id
        """,
        (
            code,
            first_name,
            last_name,
            mobile,
            mobile,
            is_active,
            status,
            defaults["company_id"],
            defaults["department_id"],
            defaults["position_id"],
            digit,
        ),
    )
    return int(cur.fetchone()[0]), True


def sync_employees_to_zkbio(db: Session) -> EmployeeSyncResult:
    """يدفع موظفي Clikk النشطين إلى ZKBioTime (ثم تُرفَع يدوياً إلى جهاز البصمة)."""
    cfg = get_zkbio_db_config(db)
    if cfg is None:
        return EmployeeSyncResult(ok=False, message="إعدادات ZKBioTime غير متوفرة.")

    employees = list(
        db.scalars(
            select(Employee).where(Employee.status == EmployeeStatus.ACTIVE).order_by(Employee.id)
        ).all()
    )
    if not employees:
        return EmployeeSyncResult(ok=False, message="لا يوجد موظفون نشطون في Clikk.")

    conn = _connect(cfg)
    result = EmployeeSyncResult(ok=True, message="")
    try:
        with conn.cursor() as cur:
            defaults = _zk_defaults(cur)
            if defaults["area_id"] is None:
                return EmployeeSyncResult(
                    ok=False,
                    message="Area جهاز البصمة (ress) غير موجودة في ZKBioTime.",
                )

            used_codes = _collect_used_zk_codes(cur)
            for emp in employees:
                if emp.zk_emp_code:
                    used_codes.add(str(emp.zk_emp_code).strip())

            for emp in employees:
                code = (emp.zk_emp_code or "").strip()
                if not code:
                    code = _next_free_code(used_codes)
                    emp.zk_emp_code = code
                    result.codes_assigned += 1
                elif code in _RESERVED_ZK_CODES:
                    result.errors.append(f"{emp.full_name_ar}: الكود {code} محجوز للجهاز")
                    result.skipped += 1
                    continue

                first, last = _split_ar_name(emp.full_name_ar)
                mobile = (emp.phone or "").strip()
                try:
                    emp_id, created = _upsert_zk_employee(
                        cur,
                        code=code,
                        first_name=first,
                        last_name=last,
                        mobile=mobile,
                        is_active=True,
                        defaults=defaults,
                    )
                    _assign_zk_area(cur, emp_id, defaults["area_id"])
                    if created:
                        result.created += 1
                    else:
                        result.updated += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"{code} {emp.full_name_ar}: {exc}")
                    result.skipped += 1
                    log.exception("push employee %s to zkbio failed", code)

            conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        msg = f"تعذّر دفع الموظفين إلى ZKBioTime: {exc}"
        log.warning(msg)
        return EmployeeSyncResult(ok=False, message=msg)
    finally:
        conn.close()

    parts = []
    if result.created:
        parts.append(f"جديد في ZKBio {result.created}")
    if result.updated:
        parts.append(f"تحديث {result.updated}")
    if result.codes_assigned:
        parts.append(f"أكواد جديدة {result.codes_assigned}")
    if result.skipped:
        parts.append(f"تخطي {result.skipped}")
    result.message = "دفع إلى ZKBioTime: " + ("، ".join(parts) if parts else "لا تغيير")
    result.message += " — ثم من ZKBioTime: Device → Data Transfer → Upload employees"
    return result
