"""تجهيز النص العربي لطابعات ESC/POS (اتجاه وربط الحروف)."""
from __future__ import annotations

import re

# علامات اتجاه Unicode — لا تُرمَّز في cp1256/cp864
_BIDI_CTRL = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def _strip_bidi_marks(text: str) -> str:
    return _BIDI_CTRL.sub("", text)


def shape_arabic_escpos(text: str) -> str:
    """نص عربي للطابعة الحرارية — كما في قاعدة البيانات (بدون bidi/reshaper).

    bidi يعكس ترتيب البايتات فيظهر النص معكوساً أو علامات ? على POS-80.
    """
    return _strip_bidi_marks(text) if text else text


def shape_arabic(text: str) -> str:
    """للعرض على الشاشة فقط — reshaper + bidi."""
    if not text or not text.strip():
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except ImportError:
        return text
