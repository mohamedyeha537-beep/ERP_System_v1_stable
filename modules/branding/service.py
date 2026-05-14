"""خدمة الهويّة البصرية: الشعار + الألوان.

تُحفظ القيم في `AppSetting` (جدول key/value) كي لا تحتاج هجرات مستقلة،
ومستقبلاً عند تفعيل المالتي-فندور يصبح كل tenant له صف خاص في هذا الجدول.

كل القيم لها افتراضيات آمنة → النظام يعمل قبل أي تخصيص.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

# ============================================================
# Constants & defaults
# ============================================================

LOGO_DIR_NAME = "branding"  # داخل static/uploads
ALLOWED_LOGO_EXT = {"png", "jpg", "jpeg", "webp", "svg", "gif"}
MAX_LOGO_BYTES = 1 * 1024 * 1024  # 1 MiB

# هذه القيم تطابق الـ CSS الافتراضي السابق في base.html
# (gradient أزرق-بنفسجي للهيدر + slate للفوتر)
DEFAULTS: dict[str, str] = {
    "brand_logo_filename": "",
    "brand_print_logo_filename": "",
    # الهيدر: نخزّن لونين (يُستخدمان كـ gradient) + لون النص
    "brand_header_bg_from": "#0f172a",
    "brand_header_bg_to": "#1e293b",
    "brand_header_fg": "#ffffff",
    # الفوتر
    "brand_footer_bg": "#0f172a",
    "brand_footer_fg": "#94a3b8",
    # الألوان الأساسية (الأزرار/الروابط/الـ accent)
    "brand_primary_color": "#3b82f6",
    "brand_accent_color": "#8b5cf6",
    # عرض الشعار في الهيدر بدل الأيقونة الافتراضية
    "brand_show_logo_in_header": "1",
    # عرض اسم المتجر بجانب الشعار
    "brand_show_name_in_header": "1",
    # نصوص الواجهة والطباعة
    "brand_pos_label": "نقطة البيع",
    "brand_header_tagline": "نظام نقطة البيع",
    "brand_footer_tagline": (
        "نظام نقطة بيع متكامل يعمل أوفلاين بالكامل مع إدارة للمخزون "
        "والمشتريات والموظفين والرواتب والتقارير المحاسبية."
    ),
    "brand_receipt_title": "فاتورة بيع",
    "brand_receipt_footer_text": "شكراً لتعاملكم معنا",
}


# ============================================================
# Helpers
# ============================================================

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _normalize_hex(raw: str | None, fallback: str) -> str:
    """يُحوّل #abc → #aabbcc، ويُرجع fallback إن لم يكن لوناً صحيحاً."""
    if not raw:
        return fallback
    s = raw.strip()
    if not _HEX_RE.match(s):
        return fallback
    if len(s) == 4:  # #abc → #aabbcc
        return "#" + "".join(c * 2 for c in s[1:])
    return s.lower()


def _normalize_text(raw: str | None, fallback: str, max_len: int = 240) -> str:
    """ينظّف النصوص الحرة مع حد أقصى حتى لا تكسر الواجهة والطباعة."""
    if raw is None:
        return fallback
    text = " ".join(str(raw).strip().split())
    if not text:
        return fallback
    return text[:max_len]


def _logo_dir() -> Path:
    """مجلّد رفع الشعارات (داخل static/uploads/branding)."""
    base = Path(__file__).resolve().parents[2] / "app" / "static" / "uploads" / LOGO_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _logo_url(filename: str) -> str:
    filename = (filename or "").strip()
    if not filename:
        return ""
    return f"/static/uploads/{LOGO_DIR_NAME}/{filename}"


# ============================================================
# Public API
# ============================================================


def get_branding(db: Session) -> dict:
    """يعيد جميع إعدادات الهويّة كقاموس قابل للاستخدام مباشرة في القوالب."""
    raw: dict[str, str] = {}
    for k, default in DEFAULTS.items():
        raw[k] = get_setting(db, k, default) or default

    # تطبيع الألوان لضمان صحّتها قبل وصولها للـ CSS
    return {
        "logo_filename": raw["brand_logo_filename"].strip(),
        "logo_url": _logo_url(raw["brand_logo_filename"]),
        "print_logo_filename": raw["brand_print_logo_filename"].strip(),
        "print_logo_url": _logo_url(raw["brand_print_logo_filename"]),
        "header_bg_from": _normalize_hex(
            raw["brand_header_bg_from"], DEFAULTS["brand_header_bg_from"]
        ),
        "header_bg_to": _normalize_hex(
            raw["brand_header_bg_to"], DEFAULTS["brand_header_bg_to"]
        ),
        "header_fg": _normalize_hex(
            raw["brand_header_fg"], DEFAULTS["brand_header_fg"]
        ),
        "footer_bg": _normalize_hex(
            raw["brand_footer_bg"], DEFAULTS["brand_footer_bg"]
        ),
        "footer_fg": _normalize_hex(
            raw["brand_footer_fg"], DEFAULTS["brand_footer_fg"]
        ),
        "primary_color": _normalize_hex(
            raw["brand_primary_color"], DEFAULTS["brand_primary_color"]
        ),
        "accent_color": _normalize_hex(
            raw["brand_accent_color"], DEFAULTS["brand_accent_color"]
        ),
        "show_logo_in_header": raw["brand_show_logo_in_header"].strip() in (
            "1",
            "true",
            "on",
            "yes",
        ),
        "show_name_in_header": raw["brand_show_name_in_header"].strip() in (
            "1",
            "true",
            "on",
            "yes",
        ),
        "pos_label": _normalize_text(
            raw["brand_pos_label"], DEFAULTS["brand_pos_label"], 80
        ),
        "header_tagline": _normalize_text(
            raw["brand_header_tagline"], DEFAULTS["brand_header_tagline"], 120
        ),
        "footer_tagline": _normalize_text(
            raw["brand_footer_tagline"], DEFAULTS["brand_footer_tagline"], 400
        ),
        "receipt_title": _normalize_text(
            raw["brand_receipt_title"], DEFAULTS["brand_receipt_title"], 120
        ),
        "receipt_footer_text": _normalize_text(
            raw["brand_receipt_footer_text"],
            DEFAULTS["brand_receipt_footer_text"],
            240,
        ),
    }


def save_branding(
    db: Session,
    *,
    header_bg_from: str | None = None,
    header_bg_to: str | None = None,
    header_fg: str | None = None,
    footer_bg: str | None = None,
    footer_fg: str | None = None,
    primary_color: str | None = None,
    accent_color: str | None = None,
    show_logo_in_header: bool | None = None,
    show_name_in_header: bool | None = None,
    pos_label: str | None = None,
    header_tagline: str | None = None,
    footer_tagline: str | None = None,
    receipt_title: str | None = None,
    receipt_footer_text: str | None = None,
) -> None:
    """يحفظ ألوان الهويّة (مع تطبيع آمن لكل قيمة)."""

    def _save(key: str, raw: str | None, default: str) -> None:
        if raw is None:
            return
        set_setting(db, key, _normalize_hex(raw, default))

    _save("brand_header_bg_from", header_bg_from, DEFAULTS["brand_header_bg_from"])
    _save("brand_header_bg_to", header_bg_to, DEFAULTS["brand_header_bg_to"])
    _save("brand_header_fg", header_fg, DEFAULTS["brand_header_fg"])
    _save("brand_footer_bg", footer_bg, DEFAULTS["brand_footer_bg"])
    _save("brand_footer_fg", footer_fg, DEFAULTS["brand_footer_fg"])
    _save("brand_primary_color", primary_color, DEFAULTS["brand_primary_color"])
    _save("brand_accent_color", accent_color, DEFAULTS["brand_accent_color"])

    if show_logo_in_header is not None:
        set_setting(db, "brand_show_logo_in_header", "1" if show_logo_in_header else "0")
    if show_name_in_header is not None:
        set_setting(db, "brand_show_name_in_header", "1" if show_name_in_header else "0")
    if pos_label is not None:
        normalized_name = _normalize_text(pos_label, DEFAULTS["brand_pos_label"], 80)
        set_setting(
            db,
            "brand_pos_label",
            normalized_name,
        )
        # وحّد اسم العرض العام حتى لا يضطر الأدمن لتغييره من مكانين.
        set_setting(db, "store_name", normalized_name)
    if header_tagline is not None:
        set_setting(
            db,
            "brand_header_tagline",
            _normalize_text(
                header_tagline,
                DEFAULTS["brand_header_tagline"],
                120,
            ),
        )
    if footer_tagline is not None:
        set_setting(
            db,
            "brand_footer_tagline",
            _normalize_text(
                footer_tagline,
                DEFAULTS["brand_footer_tagline"],
                400,
            ),
        )
    if receipt_title is not None:
        set_setting(
            db,
            "brand_receipt_title",
            _normalize_text(receipt_title, DEFAULTS["brand_receipt_title"], 120),
        )
    if receipt_footer_text is not None:
        set_setting(
            db,
            "brand_receipt_footer_text",
            _normalize_text(
                receipt_footer_text,
                DEFAULTS["brand_receipt_footer_text"],
                240,
            ),
        )


# ----- Logo -----

class LogoError(Exception):
    """خطأ في رفع/معالجة الشعار."""


def save_logo(
    db: Session,
    *,
    filename: str,
    content: bytes,
    setting_key: str = "brand_logo_filename",
) -> str:
    """يحفظ ملف الشعار في القرص ويسجّل اسمه في الإعدادات.

    يُرجع اسم الملف النهائي. يرفع `LogoError` لأي مشكلة (امتداد غير مسموح،
    حجم أكبر من اللازم، اسم فارغ).
    """
    if not filename:
        raise LogoError("لم يُحدَّد اسم ملف.")
    if not content:
        raise LogoError("الملف فارغ.")
    if len(content) > MAX_LOGO_BYTES:
        raise LogoError(
            f"حجم الملف أكبر من {MAX_LOGO_BYTES // 1024}KB."
        )
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_LOGO_EXT:
        raise LogoError(
            f"الامتداد .{ext} غير مدعوم. المدعومة: {', '.join(sorted(ALLOWED_LOGO_EXT))}"
        )

    # احذف الشعار القديم إن وجد
    delete_logo(db, setting_key=setting_key, _commit=False)

    new_name = f"logo-{uuid.uuid4().hex[:12]}.{ext}"
    path = _logo_dir() / new_name
    path.write_bytes(content)

    set_setting(db, setting_key, new_name)
    return new_name


def delete_logo(
    db: Session,
    *,
    setting_key: str = "brand_logo_filename",
    _commit: bool = True,
) -> bool:
    """يحذف الشعار الحالي من القرص ويُفرّغ الإعداد. يعيد True إن حُذف فعلاً."""
    current = (get_setting(db, setting_key, "") or "").strip()
    removed = False
    if current:
        path = _logo_dir() / current
        try:
            if path.exists():
                path.unlink()
                removed = True
        except OSError:
            pass
        set_setting(db, setting_key, "")
    if _commit:
        db.commit()
    return removed


def reset_to_defaults(db: Session) -> None:
    """يُعيد كل ألوان الهويّة لقيمها الافتراضية ويحذف الشعار."""
    delete_logo(db, setting_key="brand_logo_filename", _commit=False)
    delete_logo(db, setting_key="brand_print_logo_filename", _commit=False)
    for k, v in DEFAULTS.items():
        if k in ("brand_logo_filename", "brand_print_logo_filename"):
            continue
        set_setting(db, k, v)
    db.commit()
