"""اختبار شامل لدورة إدارة السلف:
1) إنشاء سلفة لموظف
2) خصم تلقائي عند إنشاء دفعة رواتب
3) تسجيل الاسترداد عند الدفع
4) التحقق من استرداد آخر متبقّ يدوياً
5) فحص الواجهات HTTP
"""
import io
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# ====== bootstrap models =====================================================
import modules.authz.models  # noqa: F401
import modules.catalog.models  # noqa: F401
import modules.payments.models  # noqa: F401
import modules.sales.models  # noqa: F401
import modules.settings.models  # noqa: F401
import modules.hr.models  # noqa: F401

from infra.db import get_engine, get_session_factory
from sqlalchemy.orm import Session

from modules.hr import service as hr
from modules.hr.models import (
    AdvanceStatus,
    Employee,
    EmployeeStatus,
    PayType,
    PayrollStatus,
)
from modules.payments.models import PaymentMethod
from modules.payments.service import (
    ensure_default_payment_methods,
    list_payment_methods,
)


def _ensure_employee(db: Session) -> Employee:
    emp = db.query(Employee).filter(
        Employee.full_name_ar == "موظف اختبار سلف"
    ).one_or_none()
    if emp is None:
        emp = Employee(
            full_name_ar="موظف اختبار سلف",
            job_title="مختبر",
            pay_type=PayType.MONTHLY,
            base_monthly_salary=Decimal("1500.000"),
            status=EmployeeStatus.ACTIVE,
        )
        db.add(emp)
        db.flush()
        db.commit()
    return emp


def _ensure_method(db: Session) -> PaymentMethod:
    methods = list_payment_methods(db, only_active=True)
    if methods:
        return methods[0]
    ensure_default_payment_methods(db)
    db.commit()
    return list_payment_methods(db, only_active=True)[0]


def _print(label: str, ok: bool, extra: str = ""):
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}{(' — ' + extra) if extra else ''}")


def main():
    Base = get_engine()
    Session = get_session_factory()
    db: Session = Session()

    print("=" * 70)
    print("اختبار دورة كاملة لإدارة السلف")
    print("=" * 70)

    emp = _ensure_employee(db)
    pm = _ensure_method(db)
    print(f"موظف: {emp.full_name_ar} (id={emp.id})")
    print(f"محفظة الدفع: {pm.name_ar} (id={pm.id})")

    # تنظيف السلف القديمة لجعل الاختبار قابلاً لإعادة التشغيل
    from modules.hr.models import SalaryAdvance
    leftover = db.query(SalaryAdvance).filter(
        SalaryAdvance.employee_id == emp.id
    ).all()
    for adv in leftover:
        # امسح كل الاستردادات أولاً ثم السلفة
        for r in list(adv.repayments):
            db.delete(r)
        db.delete(adv)
    db.commit()
    print(f"تم تنظيف {len(leftover)} سلفة قديمة لهذا الموظف")

    # ----- 1) صرف سلفتين ------------------------------------------------------
    print("\n[1] صرف سلفتين بقيمة 200 و 300 د.ل")
    a1 = hr.grant_advance(db, employee_id=emp.id, amount=Decimal("200"),
                          payment_method_id=pm.id, given_by_id=None,
                          notes="سلفة اختبار 1")
    a2 = hr.grant_advance(db, employee_id=emp.id, amount=Decimal("300"),
                          payment_method_id=pm.id, given_by_id=None,
                          notes="سلفة اختبار 2")
    db.commit()
    _print("سلفة 1 منشأة بقيمة المتبقي 200", a1.remaining_amount == Decimal("200.000"))
    _print("سلفة 2 منشأة بقيمة المتبقي 300", a2.remaining_amount == Decimal("300.000"))
    _print("سلفة 1 لها قيد مصروف", a1.purchase_id is not None,
           f"purchase_id={a1.purchase_id}")

    outstanding = hr.outstanding_advances_for(db, emp.id)
    _print(f"إجمالي القائمة = 500", outstanding == Decimal("500.000"),
           f"المحسوب: {outstanding}")

    # ----- 2) إنشاء دفعة رواتب وفحص الخصم التلقائي --------------------------
    print("\n[2] إنشاء دفعة رواتب — يجب أن يُملأ حقل advances تلقائياً")
    # ابحث عن أول شهر ليس له دفعة، حتى لا نصطدم بسجل سابق مدفوع
    from modules.hr.models import PayrollRun
    base_year = 2099
    base_month = 12
    while True:
        existing = db.query(PayrollRun).filter(
            PayrollRun.period_year == base_year,
            PayrollRun.period_month == base_month,
        ).one_or_none()
        if existing is None:
            break
        # حاول حذفه إن أمكن
        try:
            hr.delete_run(db, existing.id)
            db.commit()
            break
        except hr.HRError:
            base_month -= 1
            if base_month < 1:
                base_month = 12
                base_year -= 1
    run = hr.create_payroll_run(db, year=base_year, month=base_month)
    db.commit()
    entry = next((e for e in run.entries if e.employee_id == emp.id), None)
    _print(f"دفعة منشأة id={run.id} لشهر {run.label}", entry is not None)
    if entry:
        _print(f"حقل advances = 500 (مجموع السلف القائمة)",
               entry.advances == Decimal("500.000"),
               f"المحسوب: {entry.advances}")
        # net_pay = 1500 - 500 = 1000
        expected_net = entry.base_salary + entry.overtime_pay - entry.advances
        _print(f"net_pay = base - advances = {expected_net}",
               entry.net_pay == expected_net,
               f"المحسوب: {entry.net_pay}")

    # ----- 3) دفع الدفعة ------------------------------------------------------
    print("\n[3] دفع الدفعة — يجب أن يُسجَّل استرداد على السلف بـ FIFO")
    hr.pay_run(db, run_id=run.id, payment_method_id=pm.id)
    db.commit()
    db.refresh(a1)
    db.refresh(a2)
    _print("سلفة 1 (200) مسددة بالكامل",
           a1.status == AdvanceStatus.FULLY_REPAID,
           f"الحالة: {a1.status.value}, متبقي: {a1.remaining_amount}")
    _print("سلفة 2 (300) متبقي 0 (مسددة بالكامل أيضاً لأن 500 = 200+300)",
           a2.status == AdvanceStatus.FULLY_REPAID,
           f"الحالة: {a2.status.value}, متبقي: {a2.remaining_amount}")
    _print("لا توجد سلف قائمة الآن",
           hr.outstanding_advances_for(db, emp.id) == Decimal("0"))

    # ----- 4) دورة مع استرداد جزئي + يدوي ------------------------------------
    print("\n[4] دورة جديدة: سلفة 800 — خصم 600 من الراتب + 200 يدوي")
    a3 = hr.grant_advance(db, employee_id=emp.id, amount=Decimal("800"),
                          payment_method_id=pm.id, notes="سلفة كبيرة")
    db.commit()
    _print("سلفة 3 منشأة بـ 800", a3.remaining_amount == Decimal("800.000"))

    # خصم يدوي 200 (كأنه دفع نقداً)
    hr.repay_advance_manual(db, advance_id=a3.id, amount=Decimal("200"),
                            notes="استرداد يدوي قبل الراتب")
    db.commit()
    db.refresh(a3)
    _print("بعد استرداد يدوي 200 → متبقي 600 + حالة جزئية",
           a3.remaining_amount == Decimal("600.000")
           and a3.status == AdvanceStatus.PARTIALLY_REPAID,
           f"المتبقي: {a3.remaining_amount}, الحالة: {a3.status.value}")

    # ----- 5) ملخص وتقرير الأدمن ---------------------------------------------
    print("\n[5] تقرير الأدمن قبل صرف الرواتب")
    summary = hr.outstanding_advances_summary(db)
    _print(f"الموظف يظهر في القائمة بمتبقي 600",
           any(e.id == emp.id and rem == Decimal("600.000") for e, rem, c in summary),
           f"summary[0]={summary[0] if summary else None}")
    grand = hr.grand_total_outstanding_advances(db)
    _print(f"الإجمالي العام = 600", grand == Decimal("600.000"),
           f"المحسوب: {grand}")

    # ----- 6) فحص HTTP --------------------------------------------------------
    print("\n[6] فحص واجهات HTTP (TestClient)")
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    client = TestClient(app, follow_redirects=False)

    # تسجيل دخول كأدمن
    login_ok = False
    r = client.post("/auth/login",
                    data={"username": "admin", "password": "admin123"},
                    follow_redirects=False)
    if r.status_code in (200, 302, 303):
        # تحقق بالوصول لصفحة محمية
        r2 = client.get("/admin/advances", follow_redirects=False)
        login_ok = r2.status_code == 200
    _print(f"تسجيل دخول كأدمن", login_ok,
           f"login_status={r.status_code}, cookies={list(client.cookies.keys())}")

    if login_ok:
        for path in [
            "/admin/advances",
            "/admin/advances?only_outstanding=1",
            f"/admin/advances?employee_id={emp.id}",
        ]:
            r = client.get(path, follow_redirects=False)
            _print(f"GET {path}", r.status_code == 200,
                   f"status={r.status_code}")
            if r.status_code == 200:
                txt = r.text
                _print(
                    "  الصفحة تحتوي على عنوان الإدارة",
                    "إدارة سلف الموظفين" in txt,
                )

        # CSV export
        r = client.get("/admin/advances/export.csv", follow_redirects=False)
        _print(f"GET /admin/advances/export.csv", r.status_code == 200,
               f"status={r.status_code}, ct={r.headers.get('content-type','')}")

        # تأكد من وجود البطاقة الجديدة في لوحة التحكم
        r = client.get("/", follow_redirects=False)
        if r.status_code == 200:
            txt = r.text
            _print("بطاقة «سلف الموظفين» في لوحة التحكم",
                   "/admin/advances" in txt and "سلف الموظفين" in txt)
            _print("بطاقة «تسجيل سريع» للحضور في لوحة التحكم",
                   "تسجيل سريع" in txt)
        else:
            _print(f"GET /dashboard", False, f"status={r.status_code}")

        # لوحة التحكم تحتوي على بطاقة السلف
        r = client.get("/dashboard", follow_redirects=False)
        if r.status_code == 200:
            _print("بطاقة «سلف الموظفين» في لوحة التحكم",
                   "/admin/advances" in r.text and "سلف الموظفين" in r.text)

    print("\n" + "=" * 70)
    print("✓ انتهى الاختبار")
    print("=" * 70)


if __name__ == "__main__":
    main()
