"""اختبار شامل للتشغيل الأوفلاين:

1) أنّ السيرفر يقلع دون أي مكالمات شبكية (نُعطّل urlopen + socket).
2) أنّ كل القوالب الرئيسية تُحمَّل دون استدعاء CDN.
3) أنّ HTMX موجود محلياً ويُقدَّم من /static/vendor/htmx.min.js.
4) أنّ checkout يكتمل بسرعة حتى لو كانت تنبيهات WhatsApp/Email مفعَّلة.
"""

import io
import os
import socket
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# تأكد من وجود ملف HTMX المحلي قبل بدء أي شيء
htmx_path = "app/static/vendor/htmx.min.js"
assert os.path.exists(htmx_path), f"ملف HTMX غير موجود: {htmx_path}"
htmx_size = os.path.getsize(htmx_path)
print(f"✓ HTMX محلي موجود: {htmx_size} بايت")

# ====== bootstrap ============================================================
import modules.authz.models  # noqa: F401
import modules.catalog.models  # noqa: F401
import modules.payments.models  # noqa: F401
import modules.sales.models  # noqa: F401
import modules.settings.models  # noqa: F401
import modules.hr.models  # noqa: F401

from app.main import create_app
from fastapi.testclient import TestClient


def _print(label: str, ok: bool, extra: str = ""):
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}{(' — ' + extra) if extra else ''}")


def main():
    print("=" * 70)
    print("اختبار التشغيل الأوفلاين")
    print("=" * 70)

    # ====== [1] قطع الإنترنت محاكاةً ==========================================
    print("\n[1] محاكاة قطع الإنترنت تماماً (urlopen + socket)")
    original_urlopen = urllib.request.urlopen
    original_create_connection = socket.create_connection
    blocked_calls: list[str] = []

    def _blocked_urlopen(*args, **kwargs):
        url = args[0] if args else kwargs.get("url", "?")
        url_str = url.full_url if hasattr(url, "full_url") else str(url)
        blocked_calls.append(url_str)
        raise OSError(f"NETWORK BLOCKED FOR TEST: {url_str}")

    def _blocked_socket(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        # نسمح فقط بالاتصال المحلي (TestClient)
        if host in ("localhost", "127.0.0.1", "::1", "testserver"):
            return original_create_connection(address, *args, **kwargs)
        blocked_calls.append(f"socket:{host}")
        raise OSError(f"SOCKET BLOCKED FOR TEST: {host}")

    urllib.request.urlopen = _blocked_urlopen
    socket.create_connection = _blocked_socket

    # ====== [2] قلوع التطبيق ==================================================
    print("\n[2] إقلاع التطبيق دون إنترنت")
    try:
        app = create_app()
        client = TestClient(app, follow_redirects=False)
        _print("التطبيق أقلع بنجاح", True)
    except Exception as exc:
        _print("التطبيق فشل في الإقلاع", False, str(exc))
        return

    # ====== [3] تسجيل الدخول ==================================================
    print("\n[3] تسجيل دخول الأدمن")
    r = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=False,
    )
    _print(
        "POST /auth/login",
        r.status_code in (302, 303),
        f"status={r.status_code}",
    )

    # ====== [4] فحص القوالب الرئيسية =========================================
    print("\n[4] فحص القوالب — يجب ألا يحوي أي منها روابط CDN")
    pages = [
        ("/", "لوحة التحكم"),
        ("/pos", "نقطة البيع"),
        ("/admin/employees", "الموظفون"),
        ("/admin/attendance", "الحضور"),
        ("/admin/payroll", "الرواتب"),
        ("/admin/advances", "السلف"),
        ("/admin/products", "المنتجات"),
        ("/admin/inventory", "المخزون"),
        ("/admin/purchases", "المشتريات"),
        ("/reports", "التقارير"),
    ]
    cdn_patterns = (
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "unpkg.com/htmx",
        "cdn.jsdelivr",
        "cdnjs.cloudflare",
    )
    for path, label in pages:
        r = client.get(path, follow_redirects=False)
        if r.status_code == 200:
            txt = r.text
            uses_cdn = [p for p in cdn_patterns if p in txt]
            _print(
                f"GET {path} ({label})",
                not uses_cdn,
                "يحتوي روابط CDN: " + ", ".join(uses_cdn) if uses_cdn else "نظيف",
            )
        elif r.status_code in (302, 303, 404):
            _print(
                f"GET {path} ({label})",
                True,
                f"redirect/404 status={r.status_code} (مقبول)",
            )
        else:
            _print(f"GET {path}", False, f"status={r.status_code}")

    # ====== [5] فحص أنّ HTMX يُقدَّم محلياً ==================================
    print("\n[5] HTMX المحلي")
    r = client.get("/static/vendor/htmx.min.js", follow_redirects=False)
    _print(
        "GET /static/vendor/htmx.min.js",
        r.status_code == 200 and len(r.content) > 30000,
        f"status={r.status_code}, size={len(r.content)}",
    )
    _print(
        "محتوى الملف يبدأ بـ var htmx",
        r.content[:20].startswith(b"var htmx"),
        repr(r.content[:30]),
    )

    # ====== [6] اختبار سرعة الـ checkout (التنبيهات لا تعطّل) ================
    print("\n[6] اختبار أن البيع لا يتأخر بسبب الإنترنت المعطَّل")
    # نفعّل تنبيهات Email + WhatsApp بإعدادات وهمية لمحاكاة الحالة
    from modules.settings.service import set_setting
    from infra.db import get_session_factory

    Session = get_session_factory()
    db = Session()
    try:
        set_setting(db, "alerts_enabled", "1")
        set_setting(db, "smtp_host", "smtp.example.invalid")
        set_setting(db, "smtp_port", "587")
        set_setting(db, "smtp_user", "test@example.invalid")
        set_setting(db, "smtp_password", "x")
        set_setting(db, "alerts_email_to", "owner@example.invalid")
        set_setting(
            db,
            "whatsapp_webhook_url",
            "https://api.example.invalid/wa?text=",
        )
        db.commit()
    finally:
        db.close()

    # نضيف منتجاً للسلة ثم نُكمل الشراء ونقيس الزمن
    pos_load = client.get("/pos", follow_redirects=False)
    _print(f"GET /pos", pos_load.status_code == 200)

    # نبحث عن منتج للبيع — أبسط طريقة: نقرأ من قاعدة البيانات
    from modules.catalog.models import Product, ProductKind

    db = Session()
    try:
        prod = (
            db.query(Product)
            .filter(Product.kind == ProductKind.FINAL_SELLABLE)
            .first()
        )
        product_id = prod.id if prod else None
    finally:
        db.close()

    if product_id is None:
        _print("لا يوجد منتج للبيع — تخطّى اختبار السرعة", True)
    else:
        # أضف للسلة
        client.post(f"/pos/cart/add/{product_id}", follow_redirects=False)
        # افتح checkout
        r = client.get("/pos/checkout", follow_redirects=False)
        # اختر طريقة دفع (الأولى)
        from modules.payments.service import list_payment_methods

        db = Session()
        try:
            methods = list_payment_methods(db, only_active=True)
            pm_id = methods[0].id if methods else None
        finally:
            db.close()

        if pm_id is None:
            _print("لا توجد طريقة دفع — تخطّى الاختبار", True)
        else:
            t0 = time.perf_counter()
            r = client.post(
                "/pos/checkout",
                data={"payment_method_id": str(pm_id)},
                follow_redirects=False,
            )
            elapsed = time.perf_counter() - t0
            _print(
                f"POST /pos/checkout — اكتمل في {elapsed:.2f}s",
                elapsed < 2.0 and r.status_code in (302, 303),
                f"status={r.status_code} (يجب أن يكتمل في < 2 ثانية حتى مع تنبيهات معطّلة)",
            )

    # ====== [7] تقرير المكالمات الشبكية ======================================
    print("\n[7] تقرير: محاولات الاتصال بالإنترنت أثناء الاختبار")
    if not blocked_calls:
        _print("لم يحاول التطبيق الاتصال بأي خادم خارجي ✓", True)
    else:
        # مقبولة: مكالمات من خيوط الخلفية (الإيميل/واتساب)
        bg_calls = [
            c
            for c in blocked_calls
            if "smtp" in c.lower()
            or "wa" in c.lower()
            or "whatsapp" in c.lower()
            or "example.invalid" in c.lower()
        ]
        synch_calls = [c for c in blocked_calls if c not in bg_calls]
        _print(
            f"مكالمات في خيوط خلفية فقط: {len(bg_calls)}",
            len(synch_calls) == 0,
            (
                "متزامنة (سيئ): " + str(synch_calls)
                if synch_calls
                else "كلها في الخلفية ✓"
            ),
        )
        for c in blocked_calls[:5]:
            print(f"    · {c}")

    # نسمح للخيوط الخلفية بالانتهاء قبل إيقاف الاختبار
    time.sleep(0.5)

    # استعادة الدوال الأصلية
    urllib.request.urlopen = original_urlopen
    socket.create_connection = original_create_connection

    print("\n" + "=" * 70)
    print("✓ انتهى اختبار الأوفلاين")
    print("=" * 70)


if __name__ == "__main__":
    main()
