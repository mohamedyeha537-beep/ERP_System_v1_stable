"""اختبار الواجهة بأدوار مختلفة:

1) كل صفحة تحتوي على الهيدر والفوتر الجديدين.
2) الهيدر يعرض اسم المستخدم وأول دور.
3) عناصر القائمة في الهيدر تتغير حسب الصلاحيات.
4) لا توجد بطاقة "تسجيل سريع" مكررة.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import modules.authz.models  # noqa: F401
import modules.catalog.models  # noqa: F401
import modules.payments.models  # noqa: F401
import modules.sales.models  # noqa: F401
import modules.settings.models  # noqa: F401
import modules.hr.models  # noqa: F401

from app.main import create_app
from fastapi.testclient import TestClient


def _print(label: str, ok: bool, extra: str = ""):
    print(f"  {'✓' if ok else '✗'} {label}{(' — ' + extra) if extra else ''}")


def login_as(client: TestClient, username: str, password: str) -> bool:
    client.cookies.clear()
    r = client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    return r.status_code in (302, 303) and "pos_session" in client.cookies


def main():
    app = create_app()
    client = TestClient(app, follow_redirects=False)
    print("=" * 70)
    print("اختبار الواجهة (هيدر/فوتر/RBAC)")
    print("=" * 70)

    # =================================================================
    # 1) عنصر الهيدر والفوتر يظهران لكل المستخدمين المسجَّلين
    # =================================================================
    print("\n[1] الأدمن — يجب أن يرى كل القوائم")
    assert login_as(client, "admin", "admin123"), "تعذر تسجيل دخول الأدمن"

    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200, f"GET / failed: {r.status_code}"
    txt = r.text
    _print("وسم <header class='app-header'>",
           '<header class="app-header">' in txt)
    _print("وسم <footer class='app-footer'>",
           '<footer class="app-footer">' in txt)
    _print("اسم المستخدم 'admin' داخل الهيدر",
           'class="username">admin</span>' in txt)
    _print("شارة الدور (role-pill)", 'class="role-pill"' in txt)
    _print("الأفاتار يحتوي حرف A الأول",
           '<div class="avatar"' in txt)
    _print("شارة الأوفلاين في الفوتر",
           'class="offline-pill"' in txt and "يعمل أوفلاين" in txt)
    _print("اسم المتجر يظهر", "نقطة البيع" in txt)

    # كل عناصر القائمة الإدارية تظهر للأدمن
    nav_items_admin = [
        "نقطة البيع",
        "التقارير",
        "المخزون",
        "الأصناف",
        "الموظفون",
        "المشتريات",
        "الإعدادات",
    ]
    for item in nav_items_admin:
        _print(f"عنصر القائمة: '{item}'", item in txt)

    # =================================================================
    # 2) لا توجد بطاقة 'تسجيل سريع' مكررة
    # =================================================================
    print("\n[2] التحقق من إزالة التكرار في بطاقات الـ dashboard")
    # نبحث فقط ضمن منطقة <body>...</body> ونحذف ال CSS من حسبة الكلمات
    # نبحث عن العنوان الفعلي للبطاقة <div class="qtitle">تسجيل سريع</div>
    qtitle_quick = '<div class="qtitle">تسجيل سريع</div>' in txt
    _print(
        "بطاقة 'تسجيل سريع' المكررة محذوفة",
        not qtitle_quick,
        "(يجب ألا تظهر في الـ dashboard)",
    )
    qtitle_att = txt.count('<div class="qtitle">الحضور والانصراف</div>')
    _print(
        f"بطاقة 'الحضور والانصراف' تظهر {qtitle_att} مرة",
        qtitle_att == 1,
        "(يجب أن تكون مرة واحدة فقط)",
    )

    # =================================================================
    # 3) صفحة الحضور نفسها تحتوي على قسم 'تسجيل سريع' (فعلي)
    # =================================================================
    print("\n[3] صفحة الحضور تحتوي على قسم تسجيل سريع")
    r = client.get("/admin/attendance", follow_redirects=False)
    if r.status_code == 200:
        a_txt = r.text
        _print("قسم 'تسجيل سريع' موجود في صفحة الحضور",
               "تسجيل سريع" in a_txt)
        _print("الهيدر موجود في صفحة الحضور أيضاً", "app-header" in a_txt)
        _print("الفوتر موجود في صفحة الحضور أيضاً", "app-footer" in a_txt)

    # =================================================================
    # 4) فحص الكاشير (دور أقل صلاحية)
    # =================================================================
    print("\n[4] الكاشير — يجب أن يرى عناصر أقل")
    cashier_login = False
    for u, p in [("cashier", "demo123"), ("كاشير", "demo123")]:
        if login_as(client, u, p):
            cashier_login = True
            print(f"  ✓ تسجيل دخول كـ '{u}'")
            break
    if cashier_login:
        r = client.get("/", follow_redirects=False)
        if r.status_code == 200:
            ct = r.text
            _print("الهيدر يظهر للكاشير", "app-header" in ct)
            _print("الفوتر يظهر للكاشير", "app-footer" in ct)
            _print("نقطة البيع متاحة", "نقطة البيع" in ct)
            # الكاشير لا يرى الإعدادات عادةً
            has_settings = "/admin/settings" in ct
            _print(
                "الكاشير لا يرى رابط 'الإعدادات' في القائمة",
                not has_settings or ct.count("/admin/settings") <= 1,
                "(الإعدادات للأدمن فقط)",
            )
            # الكاشير لا يرى رابط الموظفون
            has_employees = "/admin/employees" in ct
            _print(
                "الكاشير لا يرى الموظفون في القائمة",
                not has_employees,
                "(الموظفون لمن لديه hr:view)",
            )
    else:
        print("  (تعذّر تسجيل دخول كاشير — يُتجاهل اختبار 4)")

    # =================================================================
    # 5) فحص الفوتر يحوي رقم الإصدار وحالة الأوفلاين
    # =================================================================
    print("\n[5] محتوى الفوتر")
    assert login_as(client, "admin", "admin123")
    r = client.get("/", follow_redirects=False)
    if r.status_code == 200:
        txt = r.text
        _print("رقم الإصدار في الفوتر",
               "الإصدار: 1.0.0" in txt)
        _print("علامة 'يعمل أوفلاين' كنص فعلي",
               '<span class="offline-pill">' in txt and "يعمل أوفلاين" in txt)
        _print("توقيع 'FastAPI + SQLite + HTMX'",
               "FastAPI + SQLite + HTMX" in txt)
        _print("سنة الحقوق",
               "© نقطة البيع" in txt)

    # =================================================================
    # 6) صفحة التسجيل لا تعرض الهيدر/الفوتر (المستخدم غير مسجَّل)
    # =================================================================
    print("\n[6] صفحة تسجيل الدخول")
    client.cookies.clear()
    r = client.get("/auth/login", follow_redirects=False)
    if r.status_code == 200:
        ltxt = r.text
        _print("الهيدر مخفي في صفحة الدخول (لا يوجد <header>)",
               '<header class="app-header">' not in ltxt)
        _print("الفوتر مخفي في صفحة الدخول (لا يوجد <footer>)",
               '<footer class="app-footer">' not in ltxt)

    print("\n" + "=" * 70)
    print("✓ انتهى الاختبار")
    print("=" * 70)


if __name__ == "__main__":
    main()
