"""تسمية موحّدة للمحافظ: رقم حساب GL + اسمه من شجرة الحسابات."""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from modules.gl.models import GlPaymentMethodMap
from modules.payments.models import PaymentMethod  # noqa: F401 — علاقة الربط


def format_wallet_display(*, code: str | None, gl_name: str | None, fallback: str) -> str:
    code = (code or "").strip()
    name = ((gl_name or "").strip() or (fallback or "").strip())
    if code and name:
        return f"{code} — {name}"
    return name or fallback or "—"


def wallet_gl_info_map(db: Session) -> dict[int, dict[str, str]]:
    """payment_method_id → {code, name, label}."""
    out: dict[int, dict[str, str]] = {}
    try:
        rows = db.scalars(
            select(GlPaymentMethodMap).options(
                selectinload(GlPaymentMethodMap.gl_account),
                selectinload(GlPaymentMethodMap.payment_method),
            )
        ).all()
        for row in rows:
            acc = row.gl_account
            if acc is None or row.payment_method_id is None:
                continue
            code = str(acc.code or "").strip()
            name = str(acc.name_ar or "").strip()
            fallback = ""
            if row.payment_method is not None:
                fallback = str(row.payment_method.name_ar or "")
            out[int(row.payment_method_id)] = {
                "code": code,
                "name": name or fallback,
                "label": format_wallet_display(code=code, gl_name=name, fallback=fallback),
            }
    except Exception:
        out = {}
    if out:
        return out
    try:
        raw = db.execute(
            text(
                "SELECT m.payment_method_id, a.code, a.name_ar, p.name_ar "
                "FROM gl_payment_method_maps m "
                "JOIN gl_accounts a ON a.id = m.gl_account_id "
                "LEFT JOIN payment_methods p ON p.id = m.payment_method_id"
            )
        ).all()
        for pmid, code, gname, pname in raw:
            if pmid is None:
                continue
            code_s = str(code or "").strip()
            name_s = str(gname or "").strip()
            fallback = str(pname or "")
            out[int(pmid)] = {
                "code": code_s,
                "name": name_s or fallback,
                "label": format_wallet_display(
                    code=code_s, gl_name=name_s, fallback=fallback
                ),
            }
    except Exception:
        return out
    return out


def label_from_info_map(
    info: dict[int, dict[str, str]],
    pm,
    *,
    fallback: str | None = None,
) -> str:
    raw = fallback if fallback is not None else str(getattr(pm, "name_ar", "") or "")
    pm_id = getattr(pm, "id", None)
    if pm_id is None:
        return raw or "—"
    row = info.get(int(pm_id))
    if not row:
        return raw or "—"
    return row["label"]


def wallet_label_for_pm(db: Session, pm, *, fallback: str | None = None) -> str:
    return label_from_info_map(wallet_gl_info_map(db), pm, fallback=fallback)
