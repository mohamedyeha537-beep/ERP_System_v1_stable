"""Sync attendance punches from local ZKBioTime PostgreSQL into POS HR."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from modules.hr.models import AttendanceRecord, AttendanceSource, Employee, EmployeeStatus, ZkProcessedPunch
from modules.hr import service as hr
from modules.hr.zkbio_config import (
    ZkBioDbConfig,
    get_zkbio_db_config,
    zkbio_install_detected,
    zkbio_settings_configured,
)
from modules.settings.service import get_bool, get_int, get_setting, set_setting

log = logging.getLogger("hr.zkbio_sync")

_sync_lock = threading.Lock()
_conn_cache: tuple[float, bool, str] | None = None
_max_tx_cache: tuple[float, int] | None = None
_CACHE_TTL = 30.0

_CHECK_IN_STATES = frozenset({"0", "I", "i", "CHECK_IN", "IN"})
_CHECK_OUT_STATES = frozenset({"1", "O", "o", "CHECK_OUT", "OUT"})
# أجهزة ZKT كثيراً ترسل 255 أو فارغ — نُبدّل دخول/خروج حسب جلسة الموظف المفتوحة
_TOGGLE_STATES = frozenset({"255", "2", "3", ""})
# تُعاد معالجتها تلقائياً
_REPROCESS_ACTIONS = frozenset({
    "unknown_state",
    "unknown_emp",
    "error",
    "already_open",
    "no_open",
})
# تُعاد معالجتها فقط إذا لم تُطبَّق فعلياً على سجل الحضور
_MISMATCH_ACTIONS = frozenset({"check_in", "check_out"})


@dataclass
class ZkSyncResult:
    ok: bool
    message: str
    processed: int = 0
    check_ins: int = 0
    check_outs: int = 0
    skipped: int = 0
    unknown_emp: int = 0
    errors: list[str] = field(default_factory=list)


def _normalize_punch_state(punch_state) -> str:
    if punch_state is None:
        return ""
    return str(punch_state).strip()


def _day_punch_index(
    cfg: ZkBioDbConfig, emp_code: str, tx_id: int, punch_time: datetime
) -> int:
    """ترتيب البصمة في اليوم (1=أول بصمة → دخول، 2=خروج، …)."""
    when = punch_time if punch_time.tzinfo else punch_time.replace(tzinfo=timezone.utc)
    punch_day = when.date()
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM iclock_transaction
                WHERE TRIM(COALESCE(emp_code, '')) = %s
                  AND id <= %s
                  AND punch_time::date = %s::date
                """,
                (emp_code, tx_id, punch_day),
            )
            return int(cur.fetchone()[0] or 0)
    finally:
        conn.close()


def _attendance_has_zk_ref(db: Session, employee_id: int, tx_id: int) -> bool:
    needle = f"ZK:{tx_id}"
    row = db.scalar(
        select(AttendanceRecord.id)
        .where(
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.notes.isnot(None),
            AttendanceRecord.notes.contains(needle),
        )
        .limit(1)
    )
    return row is not None


def _coerce_check_out_time(when: datetime, rec: AttendanceRecord) -> datetime:
    when = _ensure_aware(when)
    rec_in = rec.check_in
    if rec_in.tzinfo is None:
        rec_in = rec_in.replace(tzinfo=timezone.utc)
    else:
        rec_in = rec_in.astimezone(timezone.utc)
    if when <= rec_in:
        return rec_in + timedelta(seconds=1)
    return when


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _connect(cfg: ZkBioDbConfig):
    import psycopg2

    return psycopg2.connect(cfg.dsn())


def test_zkbio_connection(db: Session, *, use_cache: bool = False) -> tuple[bool, str]:
    global _conn_cache
    if use_cache and _conn_cache is not None:
        age = time.monotonic() - _conn_cache[0]
        if age < _CACHE_TTL:
            return _conn_cache[1], _conn_cache[2]

    cfg = get_zkbio_db_config(db)
    if cfg is None:
        return False, "إعدادات ZKBioTime غير مكتملة (تحقق من attsite.ini)."
    try:
        conn = _connect(cfg)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        ok, msg = False, f"تعذّر الاتصال بـ ZKBioTime: {exc}"
    else:
        ok, msg = True, "الاتصال ناجح ✓"
    if use_cache:
        _conn_cache = (time.monotonic(), ok, msg)
    return ok, msg


def _cached_max_transaction_id(cfg: ZkBioDbConfig) -> int:
    global _max_tx_cache
    if _max_tx_cache is not None:
        age = time.monotonic() - _max_tx_cache[0]
        if age < _CACHE_TTL:
            return _max_tx_cache[1]
    max_tx = _max_transaction_id(cfg)
    _max_tx_cache = (time.monotonic(), max_tx)
    return max_tx


def _employees_by_zk_code(db: Session) -> dict[str, Employee]:
    rows = db.scalars(
        select(Employee).where(
            Employee.zk_emp_code.isnot(None),
            Employee.zk_emp_code != "",
            Employee.status == EmployeeStatus.ACTIVE,
        )
    ).all()
    return {(e.zk_emp_code or "").strip(): e for e in rows if (e.zk_emp_code or "").strip()}


def _max_transaction_id(cfg: ZkBioDbConfig) -> int:
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM iclock_transaction")
            return int(cur.fetchone()[0] or 0)
    finally:
        conn.close()


def _normalize_after_id(cfg: ZkBioDbConfig, after_id: int) -> int:
    """إذا تجاوز المؤشر آخر بصمة في ZKBio (بعد تنظيف أو تجربة) — أعد من الصفر."""
    max_tx = _max_transaction_id(cfg)
    if max_tx <= 0:
        return 0
    if after_id > max_tx:
        log.warning("zk cursor %s > max tx %s — reset to 0", after_id, max_tx)
        return 0
    return after_id


def _fetch_new_punches(cfg: ZkBioDbConfig, after_id: int, limit: int = 500) -> list[tuple]:
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, emp_code, punch_time, punch_state, terminal_sn
                FROM iclock_transaction
                WHERE id > %s
                ORDER BY id ASC
                LIMIT %s
                """,
                (after_id, limit),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def _already_processed(db: Session, tx_id: int) -> bool:
    row = db.get(ZkProcessedPunch, tx_id)
    if row is None:
        return False
    return (row.action or "") not in _REPROCESS_ACTIONS


def _clear_reprocess_marker(
    db: Session, tx_id: int, *, employee_id: int | None = None
) -> None:
    row = db.get(ZkProcessedPunch, tx_id)
    if row is None:
        return
    action = row.action or ""
    if action in _MISMATCH_ACTIONS:
        if employee_id is None or _attendance_has_zk_ref(db, employee_id, tx_id):
            return
        db.delete(row)
        db.flush()
        return
    if action in _REPROCESS_ACTIONS:
        db.delete(row)
        db.flush()


def _mark_processed(
    db: Session,
    tx_id: int,
    *,
    employee_id: int | None,
    action: str,
    detail: str | None = None,
) -> None:
    if _already_processed(db, tx_id):
        return
    db.add(
        ZkProcessedPunch(
            zk_transaction_id=tx_id,
            employee_id=employee_id,
            action=action,
            detail=(detail or "")[:500] or None,
        )
    )


def _apply_check_in(
    db: Session,
    emp: Employee,
    when: datetime,
    tx_id: int,
) -> str:
    if hr.open_attendance_for(db, emp.id) is not None:
        return "already_open"
    hr.check_in(
        db,
        employee_id=emp.id,
        when=when,
        source=AttendanceSource.AUTO,
        notes=f"ZK:{tx_id}",
    )
    return "check_in"


def _apply_check_out(
    db: Session,
    emp: Employee,
    when: datetime,
    tx_id: int,
) -> str:
    open_rec = hr.open_attendance_for(db, emp.id)
    if open_rec is None:
        return "no_open"
    when = _coerce_check_out_time(when, open_rec)
    hr.check_out(db, employee_id=emp.id, when=when, notes=f"ZK:{tx_id}")
    return "check_out"


def _apply_toggle_punch(
    db: Session,
    emp: Employee,
    when: datetime,
    tx_id: int,
    *,
    cfg: ZkBioDbConfig,
    emp_code: str,
) -> str:
    """بصمة 255 — دخول/خروج بالتناوب حسب ترتيب البصمة في اليوم + الجلسة المفتوحة."""
    if _attendance_has_zk_ref(db, emp.id, tx_id):
        return "skipped"

    punch_no = _day_punch_index(cfg, emp_code, tx_id, when)
    prefer_check_in = punch_no % 2 == 1
    open_rec = hr.open_attendance_for(db, emp.id)

    if prefer_check_in:
        if open_rec is None:
            return _apply_check_in(db, emp, when, tx_id)
        return _apply_check_out(db, emp, when, tx_id)

    if open_rec is not None:
        return _apply_check_out(db, emp, when, tx_id)
    return _apply_check_in(db, emp, when, tx_id)


def _purge_processed_through(db: Session, max_tx_id: int) -> int:
    """يسمح بإعادة معالجة بصمات ZKBio بعد تصحيح المؤشر."""
    if max_tx_id <= 0:
        return 0
    res = db.execute(
        delete(ZkProcessedPunch).where(ZkProcessedPunch.zk_transaction_id <= max_tx_id)
    )
    db.flush()
    return int(res.rowcount or 0)


def sync_zkbio_punches(db: Session, *, force_from_id: int | None = None) -> ZkSyncResult:
    """Import new iclock_transaction rows into POS attendance."""
    if not _sync_lock.acquire(blocking=False):
        return ZkSyncResult(ok=True, message="مزامنة ZKBioTime جارية بالفعل — انتظر.")

    try:
        return _sync_zkbio_punches_locked(db, force_from_id=force_from_id)
    finally:
        _sync_lock.release()


def _sync_zkbio_punches_locked(
    db: Session, *, force_from_id: int | None = None
) -> ZkSyncResult:
    if not get_bool(db, "zk_sync_enabled", zkbio_install_detected()):
        return ZkSyncResult(ok=False, message="مزامنة ZKBioTime معطّلة.")

    cfg = get_zkbio_db_config(db)
    if cfg is None:
        return ZkSyncResult(ok=False, message="إعدادات قاعدة ZKBioTime غير متوفرة.")

    stored_id = force_from_id if force_from_id is not None else get_int(db, "zk_last_transaction_id", 0)
    emp_map = _employees_by_zk_code(db)
    if not emp_map:
        return ZkSyncResult(
            ok=False,
            message="لا يوجد موظفون نشطون بكود بصمة (zk_emp_code) في POS.",
        )

    db.commit()

    after_id = _normalize_after_id(cfg, stored_id)
    purged = 0
    if after_id == 0 and stored_id > 0:
        max_tx = _max_transaction_id(cfg)
        purged = _purge_processed_through(db, max_tx)
        if purged:
            log.info("purged %s stale zk processed punches (cursor was %s)", purged, stored_id)
        db.commit()

    result = ZkSyncResult(ok=True, message="")
    last_id = after_id

    try:
        rows = _fetch_new_punches(cfg, after_id)
    except Exception as exc:  # noqa: BLE001
        msg = f"خطأ قراءة بصمات ZKBioTime: {exc}"
        set_setting(db, "zk_last_sync_error", msg)
        db.commit()
        log.warning(msg)
        return ZkSyncResult(ok=False, message=msg)

    for tx_id, emp_code, punch_time, punch_state, terminal_sn in rows:
        tx_id = int(tx_id)
        code = (emp_code or "").strip()
        emp = emp_map.get(code)
        _clear_reprocess_marker(db, tx_id, employee_id=emp.id if emp else None)
        if _already_processed(db, tx_id):
            result.skipped += 1
            last_id = tx_id
            set_setting(db, "zk_last_transaction_id", str(last_id))
            db.commit()
            continue

        if emp is None:
            _mark_processed(
                db,
                tx_id,
                employee_id=None,
                action="unknown_emp",
                detail=f"code={code}",
            )
            result.unknown_emp += 1
            result.processed += 1
            last_id = tx_id
            set_setting(db, "zk_last_transaction_id", str(last_id))
            db.commit()
            continue

        when = _ensure_aware(punch_time)
        state = _normalize_punch_state(punch_state)

        if _attendance_has_zk_ref(db, emp.id, tx_id):
            _mark_processed(db, tx_id, employee_id=emp.id, action="skipped")
            result.skipped += 1
            result.processed += 1
            last_id = tx_id
            set_setting(db, "zk_last_transaction_id", str(last_id))
            db.commit()
            continue

        try:
            if state in _CHECK_IN_STATES:
                action = _apply_check_in(db, emp, when, tx_id)
            elif state in _CHECK_OUT_STATES:
                action = _apply_check_out(db, emp, when, tx_id)
            elif state in _TOGGLE_STATES or state.isdigit():
                action = _apply_toggle_punch(
                    db, emp, when, tx_id, cfg=cfg, emp_code=code
                )
            else:
                action = _apply_toggle_punch(
                    db, emp, when, tx_id, cfg=cfg, emp_code=code
                )
            if action == "check_in":
                result.check_ins += 1
            elif action == "check_out":
                result.check_outs += 1
            else:
                result.skipped += 1
            _mark_processed(db, tx_id, employee_id=emp.id, action=action)
        except hr.HRError as exc:
            _mark_processed(db, tx_id, employee_id=emp.id, action="error", detail=str(exc))
            result.errors.append(f"#{tx_id} {code}: {exc}")
            result.skipped += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"#{tx_id} {code}: {exc}")
            log.exception("zk sync punch %s failed", tx_id)
            set_setting(db, "zk_last_sync_error", str(exc)[:500])
            db.commit()
            break

        result.processed += 1
        last_id = tx_id
        set_setting(db, "zk_last_transaction_id", str(last_id))
        db.commit()

    set_setting(db, "zk_last_sync_at", datetime.now(timezone.utc).isoformat())
    if result.errors:
        set_setting(db, "zk_last_sync_error", result.errors[0][:500])
    else:
        set_setting(db, "zk_last_sync_error", "")

    db.commit()

    parts = [
        f"دخول {result.check_ins}",
        f"خروج {result.check_outs}",
    ]
    if result.skipped:
        parts.append(f"تخطي {result.skipped}")
    if result.unknown_emp:
        parts.append(f"كود غير مربوط {result.unknown_emp}")
    if purged:
        parts.insert(0, f"أُعيدت معالجة {purged}")
    result.message = "تمت المزامنة (كل الموظفين): " + "، ".join(parts)
    return result


def get_sync_status(db: Session) -> dict:
    cfg = get_zkbio_db_config(db)
    ok, conn_msg = (
        test_zkbio_connection(db, use_cache=True) if cfg else (False, "لا إعدادات")
    )
    linked = len(_employees_by_zk_code(db))
    max_tx = 0
    if cfg and ok:
        try:
            max_tx = _cached_max_transaction_id(cfg)
        except Exception as exc:  # noqa: BLE001
            ok = False
            conn_msg = f"تعذّر قراءة بصمات ZKBioTime: {exc}"
    stored_id = get_int(db, "zk_last_transaction_id", 0)
    return {
        "enabled": get_bool(db, "zk_sync_enabled", zkbio_install_detected()),
        "detected": zkbio_install_detected(),
        "configured": zkbio_settings_configured(db),
        "show_panel": zkbio_settings_configured(db)
        or get_bool(db, "zk_sync_enabled", False),
        "local_install": zkbio_install_detected(),
        "connected": ok,
        "connection_message": conn_msg,
        "interval_seconds": max(15, get_int(db, "zk_sync_interval_seconds", 30)),
        "last_transaction_id": stored_id,
        "max_transaction_id": max_tx,
        "cursor_ok": stored_id <= max_tx if max_tx else True,
        "last_sync_at": get_setting(db, "zk_last_sync_at", ""),
        "last_sync_error": get_setting(db, "zk_last_sync_error", ""),
        "linked_employees": linked,
        "host": cfg.host if cfg else "",
        "port": cfg.port if cfg else 0,
        "dbname": cfg.dbname if cfg else "",
        "db_user": cfg.user if cfg else "",
    }
