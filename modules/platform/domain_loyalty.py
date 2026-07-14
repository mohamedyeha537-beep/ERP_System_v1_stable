"""إعدادات الولاء والإحالة حسب مجال العمل — مطعم مقابل فندق."""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from sqlalchemy.orm import Session

from modules.platform.business_domain import BusinessDomain
from modules.settings.service import get_setting, set_setting


def loyalty_domain(domain: BusinessDomain | str | None) -> BusinessDomain:
    if isinstance(domain, BusinessDomain):
        return domain if domain != BusinessDomain.SHARED else BusinessDomain.RESTAURANT
    raw = (str(domain or "").strip().lower() or BusinessDomain.RESTAURANT.value)
    if raw == BusinessDomain.HOTEL.value:
        return BusinessDomain.HOTEL
    return BusinessDomain.RESTAURANT


def _prefix(domain: BusinessDomain) -> str:
    return "hotel_" if domain == BusinessDomain.HOTEL else ""


def _loyalty_key(domain: BusinessDomain, suffix: str) -> str:
    return f"{_prefix(domain)}loyalty_{suffix}"


def _referral_key(domain: BusinessDomain, suffix: str) -> str:
    return f"{_prefix(domain)}referral_{suffix}"


def _legacy_loyalty_key(suffix: str) -> str:
    return f"loyalty_{suffix}"


def _legacy_referral_key(suffix: str) -> str:
    return f"referral_{suffix}"


def _read_bool(db: Session, key: str, legacy_key: str | None, default: bool) -> bool:
    raw = get_setting(db, key, "")
    if not raw and legacy_key:
        raw = get_setting(db, legacy_key, "1" if default else "0")
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _read_decimal(db: Session, key: str, legacy_key: str | None, default: str) -> Decimal:
    raw = get_setting(db, key, "")
    if not raw and legacy_key:
        raw = get_setting(db, legacy_key, default)
    try:
        return Decimal(str(raw or default)).quantize(Decimal("0.001"))
    except Exception:
        return Decimal(default).quantize(Decimal("0.001"))


def _max_redeem_percent(db: Session, domain: BusinessDomain) -> Decimal:
    key = _loyalty_key(domain, "max_redeem_percent")
    legacy = _legacy_loyalty_key("max_redeem_percent") if domain == BusinessDomain.RESTAURANT else None
    raw = get_setting(db, key, "")
    if not raw and legacy:
        raw = get_setting(db, legacy, "")
    if not raw:
        return Decimal("0")
    try:
        pct = Decimal(raw)
    except Exception:
        return Decimal("0")
    if pct < 0:
        return Decimal("0")
    if pct > 100:
        return Decimal("100")
    return pct


def loyalty_settings_for_domain(
    db: Session, domain: BusinessDomain | str | None = None
) -> dict:
    dom = loyalty_domain(domain)
    leg = dom == BusinessDomain.RESTAURANT
    return {
        "domain": dom.value,
        "enabled": _read_bool(
            db,
            _loyalty_key(dom, "enabled"),
            _legacy_loyalty_key("enabled") if leg else None,
            True,
        ),
        "earn_per_dinar": _read_decimal(
            db,
            _loyalty_key(dom, "earn_per_dinar"),
            _legacy_loyalty_key("earn_per_dinar") if leg else None,
            "1",
        ),
        "redeem_value_per_point": _read_decimal(
            db,
            _loyalty_key(dom, "redeem_value_per_point"),
            _legacy_loyalty_key("redeem_value_per_point") if leg else None,
            "0.1",
        ),
        "min_points_to_redeem": _read_decimal(
            db,
            _loyalty_key(dom, "min_points_to_redeem"),
            _legacy_loyalty_key("min_points_to_redeem") if leg else None,
            "50",
        ),
        "max_redeem_percent_of_sale": _max_redeem_percent(db, dom),
    }


def referral_settings_for_domain(
    db: Session, domain: BusinessDomain | str | None = None
) -> dict:
    dom = loyalty_domain(domain)
    leg = dom == BusinessDomain.RESTAURANT
    base = loyalty_settings_for_domain(db, dom)
    max_uses = _referral_max_uses(db, dom)
    return {
        **base,
        "referral_enabled": _read_bool(
            db,
            _referral_key(dom, "enabled"),
            _legacy_referral_key("enabled") if leg else None,
            False,
        ),
        "referrer_points": _read_decimal(
            db,
            _referral_key(dom, "referrer_points"),
            _legacy_referral_key("referrer_points") if leg else None,
            "50",
        ),
        "buyer_points": _read_decimal(
            db,
            _referral_key(dom, "buyer_points"),
            _legacy_referral_key("buyer_points") if leg else None,
            "25",
        ),
        "min_sale_total": _read_decimal(
            db,
            _referral_key(dom, "min_sale_total"),
            _legacy_referral_key("min_sale_total") if leg else None,
            "0",
        ),
        "referral_max_uses_per_code": str(max_uses),
        "max_uses_per_code_per_buyer": max_uses,
    }


def _referral_max_uses(db: Session, domain: BusinessDomain) -> int:
    key = _referral_key(domain, "max_uses_per_code")
    legacy = _legacy_referral_key("max_uses_per_code") if domain == BusinessDomain.RESTAURANT else None
    raw = get_setting(db, key, "")
    if not raw and legacy:
        raw = get_setting(db, legacy, "")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            return 1
    if domain == BusinessDomain.RESTAURANT:
        legacy_first = get_setting(db, "referral_first_sale_only", "")
        if legacy_first == "0":
            return 0
    return 1


def save_loyalty_settings_for_domain(
    db: Session,
    domain: BusinessDomain | str,
    *,
    enabled: bool,
    earn_per_dinar: str,
    redeem_value_per_point: str,
    min_points_to_redeem: str,
    max_redeem_percent: str,
) -> None:
    dom = loyalty_domain(domain)
    set_setting(db, _loyalty_key(dom, "enabled"), "1" if enabled else "0")
    set_setting(db, _loyalty_key(dom, "earn_per_dinar"), (earn_per_dinar or "1").strip())
    set_setting(
        db,
        _loyalty_key(dom, "redeem_value_per_point"),
        (redeem_value_per_point or "0.1").strip(),
    )
    set_setting(
        db,
        _loyalty_key(dom, "min_points_to_redeem"),
        (min_points_to_redeem or "50").strip(),
    )
    pct_raw = (max_redeem_percent or "").strip()
    if pct_raw:
        try:
            pct_val = max(0, min(100, int(float(pct_raw))))
        except ValueError:
            pct_val = 0
        set_setting(db, _loyalty_key(dom, "max_redeem_percent"), str(pct_val))
    else:
        set_setting(db, _loyalty_key(dom, "max_redeem_percent"), "")


def save_referral_settings_for_domain(
    db: Session,
    domain: BusinessDomain | str,
    *,
    enabled: bool,
    referrer_points: str,
    buyer_points: str,
    min_sale_total: str,
    max_uses_per_code: str,
) -> None:
    dom = loyalty_domain(domain)
    set_setting(db, _referral_key(dom, "enabled"), "1" if enabled else "0")
    set_setting(
        db, _referral_key(dom, "referrer_points"), (referrer_points or "0").strip()
    )
    set_setting(db, _referral_key(dom, "buyer_points"), (buyer_points or "0").strip())
    set_setting(
        db, _referral_key(dom, "min_sale_total"), (min_sale_total or "0").strip()
    )
    try:
        n = max(0, int((max_uses_per_code or "1").strip()))
    except ValueError:
        n = 1
    set_setting(db, _referral_key(dom, "max_uses_per_code"), str(n))


def points_to_dinars_for_domain(
    db: Session, points: Decimal, domain: BusinessDomain | str | None = None
) -> Decimal:
    s = loyalty_settings_for_domain(db, domain)
    return (Decimal(points) * s["redeem_value_per_point"]).quantize(Decimal("0.001"))


def loyalty_redeem_quote_for_domain(
    db: Session,
    *,
    customer,
    sale_total: Decimal,
    domain: BusinessDomain | str | None = None,
) -> dict:
    s = loyalty_settings_for_domain(db, domain)
    sale_total = Decimal(str(sale_total or 0)).quantize(Decimal("0.001"))
    balance = Decimal(str(customer.points_balance or 0)).quantize(Decimal("0.001"))
    min_pts = s["min_points_to_redeem"]
    rpp = s["redeem_value_per_point"]
    max_pct = s["max_redeem_percent_of_sale"]
    pct_cap_discount = Decimal("0")
    if max_pct > 0 and sale_total > 0:
        pct_cap_discount = (sale_total * max_pct / Decimal("100")).quantize(
            Decimal("0.001"), rounding=ROUND_DOWN
        )
    can = bool(
        s["enabled"]
        and max_pct > 0
        and sale_total > 0
        and balance >= min_pts
        and rpp > 0
    )
    max_points = Decimal("0")
    max_discount = Decimal("0")
    if can:
        points_cap = (sale_total / rpp).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
        max_points = min(balance, points_cap)
        if max_points < min_pts:
            can = False
            max_points = Decimal("0")
        else:
            max_discount = points_to_dinars_for_domain(db, max_points, domain)
            if max_pct < Decimal("100"):
                if max_discount > pct_cap_discount:
                    max_discount = pct_cap_discount
                    max_points = (max_discount / rpp).quantize(
                        Decimal("0.001"), rounding=ROUND_DOWN
                    )
                    if max_points < min_pts:
                        can = False
                        max_points = Decimal("0")
                        max_discount = Decimal("0")
    return {
        "can_redeem": can,
        "balance": balance,
        "min_points": min_pts,
        "max_points": max_points,
        "max_discount": max_discount,
        "redeem_value_per_point": rpp,
        "max_redeem_percent_of_sale": max_pct,
        "percent_cap_discount": pct_cap_discount,
        "sale_total": sale_total,
    }
