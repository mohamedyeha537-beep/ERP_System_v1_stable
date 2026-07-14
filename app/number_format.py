"""تنسيق الأرقام: كميات بدون أصفار زائدة، ومبالغ بجزء عشري صغير."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from markupsafe import Markup, escape


def to_decimal(value: Any) -> Decimal:
    try:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def format_qty_plain(value: Any) -> str:
    """كمية نصية: 2.0000 → 2 ، 2.5 → 2.5"""
    dec = to_decimal(value)
    text = format(dec.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def format_qty_markup(value: Any) -> Markup:
    """كمية HTML — الجزء العشري (إن وُجد) بحجم أصغر."""
    plain = format_qty_plain(value)
    sign = ""
    body = plain
    if body.startswith("-"):
        sign = "-"
        body = body[1:]
    if "." not in body:
        return Markup(escape(sign + body))
    whole, frac = body.split(".", 1)
    return Markup(
        f'{escape(sign + whole)}<span class="num-frac">.{escape(frac)}</span>'
    )


def format_money_plain(value: Any) -> str:
    dec = to_decimal(value).quantize(Decimal("0.001"))
    sign = "-" if dec < 0 else ""
    raw = format(abs(dec), "f")
    whole, frac = raw.split(".")
    return f"{sign}{whole}.{frac}"


def format_money_markup(value: Any) -> Markup:
    """مبلغ HTML — ثلاث خانات عشرية والجزء العشري بحجم أصغر."""
    dec = to_decimal(value).quantize(Decimal("0.001"))
    sign = "-" if dec < 0 else ""
    raw = format(abs(dec), "f")
    whole, frac = raw.split(".")
    return Markup(
        f'{escape(sign + whole)}<span class="num-frac">.{escape(frac)}</span>'
    )
