"""اختبارات لوحدة الهويّة البصرية (Branding):

- دالة الجلب get_branding تُعيد الافتراضيات بشكل صحيح
- save_branding يحفظ الألوان مع تطبيع #abc → #aabbcc
- save_logo يرفض الامتدادات غير المسموحة والملفات الكبيرة
- save_logo + delete_logo يعملان على القرص + الإعدادات
- reset_to_defaults يُعيد كل القيم
- HTTP: /admin/branding يفتح للأدمن، يتطلب صلاحية، الحفظ يعمل
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# ====== bootstrap models =====================================================
import modules.authz.models  # noqa: F401
import modules.kds.models  # noqa: F401 — KitchenDepartment يُستخدم في Product
import modules.printing.models  # noqa: F401 — KitchenSection يُستخدم في KitchenTicket
import modules.catalog.models  # noqa: F401
import modules.inventory.models  # noqa: F401
import modules.payments.models  # noqa: F401
import modules.sales.models  # noqa: F401
import modules.settings.models  # noqa: F401
import modules.hr.models  # noqa: F401
import modules.hotel.models  # noqa: F401
import modules.customers.models  # noqa: F401

from infra.db import Base, get_engine, get_session_factory  # noqa: E402

from modules.branding import service as bsvc  # noqa: E402
from modules.branding.service import (  # noqa: E402
    LogoError,
    delete_logo,
    get_branding,
    reset_to_defaults,
    save_branding,
    save_logo,
)

from fastapi.testclient import TestClient  # noqa: E402

# ====== setup db =============================================================
engine = get_engine()
Base.metadata.create_all(bind=engine)
Session = get_session_factory()
db = Session()


def assert_eq(actual, expected, msg: str) -> None:
    if actual != expected:
        raise AssertionError(f"{msg} — توقّع {expected!r} وحصل {actual!r}")
    print(f"  ✓ {msg}")


def assert_true(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(f"{msg} — كان False")
    print(f"  ✓ {msg}")


passed = 0
failed = 0


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def step(fn):
    """ديكوريتر يعدّ النجاح/الفشل ويواصل."""
    def wrapper():
        global passed, failed
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  ✗ فشل: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ استثناء: {type(e).__name__}: {e}")
    return wrapper


# =============================================================================
# 1) الافتراضيات
# =============================================================================
section("1) get_branding يُعيد الافتراضيات بشكل آمن")


@step
def test_defaults():
    # نظّف أي إعدادات قديمة
    reset_to_defaults(db)
    b = get_branding(db)
    assert_eq(b["header_bg_from"], "#0f172a", "header_bg_from الافتراضي")
    assert_eq(b["primary_color"], "#3b82f6", "primary_color الافتراضي")
    assert_eq(b["logo_filename"], "", "لا يوجد شعار افتراضياً")
    assert_eq(b["logo_url"], "", "logo_url فارغ")
    assert_eq(b["print_logo_filename"], "", "لا يوجد شعار طباعة افتراضياً")
    assert_eq(b["print_logo_url"], "", "print_logo_url فارغ")
    assert_true(b["show_logo_in_header"], "show_logo_in_header افتراضياً True")
    assert_true(b["show_name_in_header"], "show_name_in_header افتراضياً True")
    assert_eq(b["pos_label"], "نقطة البيع — بيتك", "pos_label الافتراضي")
    assert_eq(b["header_tagline"], "Bayatak — Roof Caffee", "header_tagline الافتراضي")
    assert_eq(b["receipt_title"], "فاتورة بيع", "receipt_title الافتراضي")
    assert_eq(
        b["receipt_footer_text"],
        "شكراً لزيارتكم بيتك — نتمنى لكم يوماً سعيداً",
        "receipt_footer_text الافتراضي",
    )


test_defaults()


# =============================================================================
# 2) حفظ + تطبيع الألوان
# =============================================================================
section("2) save_branding يحفظ ويطبّع #abc → #aabbcc")


@step
def test_save_normalize():
    save_branding(
        db,
        header_bg_from="#abc",  # سيُحوَّل إلى #aabbcc
        header_bg_to="#FF0000",  # uppercase → lowercase
        primary_color="#g00",  # غير صحيح → fallback
        pos_label="مطعم القصر",
        header_tagline="مطعم ومقهى القصر",
        receipt_title="فاتورة مطعم",
        receipt_footer_text="زيارتكم تسعدنا دائماً",
    )
    db.commit()
    b = get_branding(db)
    assert_eq(b["header_bg_from"], "#aabbcc", "تطبيع #abc")
    assert_eq(b["header_bg_to"], "#ff0000", "lowercase")
    assert_eq(
        b["primary_color"],
        "#3b82f6",
        "fallback عند لون غير صحيح",
    )
    assert_eq(b["pos_label"], "مطعم القصر", "pos_label محفوظ")
    assert_eq(b["header_tagline"], "مطعم ومقهى القصر", "header_tagline محفوظ")
    assert_eq(b["receipt_title"], "فاتورة مطعم", "receipt_title محفوظ")
    assert_eq(
        b["receipt_footer_text"],
        "زيارتكم تسعدنا دائماً",
        "receipt_footer_text محفوظ",
    )


test_save_normalize()


# =============================================================================
# 3) رفض الامتدادات غير المسموحة
# =============================================================================
section("3) save_logo يرفض الامتدادات الخطأ والملفات الفارغة")


@step
def test_logo_reject_ext():
    try:
        save_logo(db, filename="hack.exe", content=b"binary")
    except LogoError:
        return
    raise AssertionError("كان يجب رفض .exe")


test_logo_reject_ext()


@step
def test_logo_reject_empty():
    try:
        save_logo(db, filename="logo.png", content=b"")
    except LogoError:
        return
    raise AssertionError("كان يجب رفض الملف الفارغ")


test_logo_reject_empty()


@step
def test_logo_reject_too_big():
    try:
        save_logo(db, filename="big.png", content=b"x" * (2 * 1024 * 1024))
    except LogoError:
        return
    raise AssertionError("كان يجب رفض الملف الكبير")


test_logo_reject_too_big()


# =============================================================================
# 4) رفع + حذف فعلي
# =============================================================================
section("4) رفع شعار حقيقي + حذفه يحدث في القرص والإعدادات")

# PNG صغير 1x1 شفّاف صالح
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xff"
    b"\xff?\x00\x05\xfe\x02\xfe\xa3>\xb4{\x00\x00\x00\x00IEND\xaeB`\x82"
)

LOGO_DIR = (
    Path(bsvc.__file__).resolve().parents[2]
    / "app"
    / "static"
    / "uploads"
    / "branding"
)


@step
def test_logo_save():
    fname = save_logo(db, filename="my-logo.png", content=TINY_PNG)
    db.commit()
    assert_true(fname.endswith(".png"), "اسم الملف يحتوي امتداد")
    assert_true((LOGO_DIR / fname).exists(), "الملف على القرص")
    b = get_branding(db)
    assert_eq(b["logo_filename"], fname, "الإعداد يحتوي اسم الملف")
    assert_eq(b["logo_url"], f"/static/uploads/branding/{fname}", "logo_url صحيح")


test_logo_save()


@step
def test_print_logo_save():
    fname = save_logo(
        db,
        filename="print-logo.png",
        content=TINY_PNG,
        setting_key="brand_print_logo_filename",
    )
    db.commit()
    assert_true(fname.endswith(".png"), "اسم شعار الطباعة يحتوي امتداد")
    assert_true((LOGO_DIR / fname).exists(), "ملف شعار الطباعة على القرص")
    b = get_branding(db)
    assert_eq(b["print_logo_filename"], fname, "الإعداد يحتوي اسم شعار الطباعة")
    assert_eq(
        b["print_logo_url"],
        f"/static/uploads/branding/{fname}",
        "print_logo_url صحيح",
    )


test_print_logo_save()


@step
def test_logo_replace_removes_old():
    """رفع شعار جديد يحذف القديم تلقائياً."""
    b = get_branding(db)
    old_name = b["logo_filename"]
    assert_true(bool(old_name), "هناك شعار قديم")

    new_name = save_logo(db, filename="other.png", content=TINY_PNG)
    db.commit()
    assert_true(new_name != old_name, "اسم الملف الجديد مختلف")
    assert_true((LOGO_DIR / new_name).exists(), "الملف الجديد موجود")
    assert_true(not (LOGO_DIR / old_name).exists(), "الملف القديم حُذف")


test_logo_replace_removes_old()


@step
def test_logo_delete():
    delete_logo(db)
    delete_logo(db, setting_key="brand_print_logo_filename")
    b = get_branding(db)
    assert_eq(b["logo_filename"], "", "الإعداد فُرّغ بعد الحذف")
    assert_eq(b["logo_url"], "", "logo_url فارغ بعد الحذف")
    assert_eq(b["print_logo_filename"], "", "شعار الطباعة فُرّغ بعد الحذف")
    assert_eq(b["print_logo_url"], "", "print_logo_url فارغ بعد الحذف")


test_logo_delete()


# =============================================================================
# 5) reset_to_defaults
# =============================================================================
section("5) reset_to_defaults يعيد كل شيء")


@step
def test_reset():
    save_branding(db, header_bg_from="#ff00ff", primary_color="#00ff00")
    save_logo(db, filename="t.png", content=TINY_PNG)
    db.commit()

    reset_to_defaults(db)
    b = get_branding(db)
    assert_eq(b["header_bg_from"], "#0f172a", "اللون رجع للافتراضي")
    assert_eq(b["primary_color"], "#3b82f6", "primary رجع للافتراضي")
    assert_eq(b["logo_filename"], "", "الشعار حُذف")


test_reset()


# =============================================================================
# 6) HTTP integration
# =============================================================================
section("6) HTTP: صفحة /admin/branding + الحفظ + الصلاحيات")

from app.main import app  # noqa: E402

client = TestClient(app)


def login_as(username: str, password: str) -> None:
    r = client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), f"login failed: {r.status_code}"


@step
def test_http_admin_can_open():
    login_as("admin", "admin123")
    r = client.get("/admin/branding")
    assert_eq(r.status_code, 200, "فتح الصفحة كأدمن")
    assert_true("الهويّة البصرية" in r.text, "العنوان موجود")
    assert_true("معاينة مباشرة" in r.text, "قسم المعاينة")
    assert_true("شعار المتجر" in r.text, "قسم الشعار")
    assert_true("عنوان الفاتورة المطبوعة" in r.text, "حقول النصوص الجديدة موجودة")


test_http_admin_can_open()


@step
def test_http_save_colors():
    login_as("admin", "admin123")
    r = client.post(
        "/admin/branding/save",
        data={
            "header_bg_from": "#123456",
            "header_bg_to": "#abcdef",
            "header_fg": "#ffffff",
            "footer_bg": "#222222",
            "footer_fg": "#cccccc",
            "primary_color": "#ff6600",
            "accent_color": "#00ff99",
            "show_logo_in_header": "on",
            "show_name_in_header": "on",
            "pos_label": "مطعم القصر",
            "header_tagline": "مطعم ومقهى القصر",
            "footer_tagline": "أفضل وجبات ومشروبات يومياً",
            "receipt_title": "فاتورة مطعم",
            "receipt_footer_text": "زيارتكم تسعدنا دائماً",
        },
        follow_redirects=False,
    )
    assert_eq(r.status_code, 302, "إعادة توجيه بعد الحفظ")

    # تحقّق فعلي من القاعدة
    b = get_branding(db)
    assert_eq(b["header_bg_from"], "#123456", "اللون مُخزَّن")
    assert_eq(b["primary_color"], "#ff6600", "primary مُخزَّن")
    assert_eq(b["pos_label"], "مطعم القصر", "pos_label مُخزَّن")
    assert_eq(b["receipt_title"], "فاتورة مطعم", "receipt_title مُخزَّن")


test_http_save_colors()


@step
def test_http_cashier_blocked():
    """الكاشير لا يملك صلاحية branding:manage."""
    # سجّل خروج أولاً
    client.get("/auth/logout")
    login_as("cashier", "demo123")
    r = client.get("/admin/branding", follow_redirects=False)
    # 302 إلى /auth/login أو 403 — كلاهما مقبول لمنع الوصول
    assert_true(r.status_code in (302, 403), f"وصول الكاشير ممنوع (status={r.status_code})")


test_http_cashier_blocked()


@step
def test_http_brand_in_base_html():
    """الهيدر/الفوتر يستخدمان متغيّرات الهويّة المُخزَّنة."""
    login_as("admin", "admin123")
    r = client.get("/")
    assert_eq(r.status_code, 200, "الصفحة الرئيسية")
    # ابحث عن ألواننا داخل CSS variables
    assert_true("--brand-header-from: #123456" in r.text, "متغيّر الهيدر FROM في base.html")
    assert_true("--brand-primary: #ff6600" in r.text, "متغيّر primary في base.html")
    assert_true("مطعم القصر" in r.text, "pos_label يظهر في base.html")
    assert_true("أفضل وجبات ومشروبات يومياً" in r.text, "footer_tagline يظهر في base.html")


test_http_brand_in_base_html()


# =============================================================================
# 7) cleanup — أعد الكل للافتراضي
# =============================================================================
section("7) cleanup — إعادة كل شيء للافتراضي")
try:
    reset_to_defaults(db)
    print("  ✓ تنظيف ناجح")
except Exception as e:
    print(f"  ⚠ تحذير: فشل التنظيف: {e}")
finally:
    db.close()

# =============================================================================
# نتيجة نهائية
# =============================================================================
print(f"\n{'=' * 50}")
print(f"  نجح: {passed}    فشل: {failed}")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
