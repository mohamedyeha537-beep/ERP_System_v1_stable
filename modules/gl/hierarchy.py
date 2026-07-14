"""شجرة الحسابات — رصيد تجميعي للعرض دون تكرار في التقارير."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.gl.models import GlAccount

_ZERO = Decimal("0")


def build_children_map(db: Session) -> dict[int, list[int]]:
    rows = db.execute(
        select(GlAccount.parent_id, GlAccount.id).where(GlAccount.parent_id.isnot(None))
    ).all()
    out: dict[int, list[int]] = {}
    for pid, cid in rows:
        if pid is None:
            continue
        out.setdefault(int(pid), []).append(int(cid))
    for kids in out.values():
        kids.sort()
    return out


def header_account_ids(
    db: Session, children_map: dict[int, list[int]] | None = None
) -> set[int]:
    cm = children_map if children_map is not None else build_children_map(db)
    return {pid for pid, kids in cm.items() if kids}


def is_header_account(
    db: Session, account_id: int, children_map: dict[int, list[int]] | None = None
) -> bool:
    cm = children_map if children_map is not None else build_children_map(db)
    return account_id in cm and len(cm[account_id]) > 0


def assert_postable_account(
    db: Session, account_id: int, children_map: dict[int, list[int]] | None = None
) -> None:
    if is_header_account(db, account_id, children_map):
        acc = db.get(GlAccount, account_id)
        label = f"{acc.code} — {acc.name_ar}" if acc else str(account_id)
        raise ValueError(
            f"حساب تجميعي ({label}) — سجّل الحركة على حساب فرعي لتجنب تكرار الأرصدة."
        )


def rollup_balance(
    account_id: int,
    raw: dict[int, Decimal],
    children_map: dict[int, list[int]],
) -> Decimal:
    total = Decimal(str(raw.get(account_id, _ZERO)))
    for cid in children_map.get(account_id, []):
        total += rollup_balance(cid, raw, children_map)
    return total.quantize(Decimal("0.001"))


def display_balances_map(
    db: Session, raw: dict[int, Decimal] | None = None
) -> dict[int, Decimal]:
    if raw is None:
        from modules.gl.service import account_balances_map

        raw = account_balances_map(db)
    children_map = build_children_map(db)
    ids = {int(aid) for aid in db.scalars(select(GlAccount.id)).all()}
    ids.update(raw.keys())
    ids.update(children_map.keys())
    return {aid: rollup_balance(aid, raw, children_map) for aid in ids}


def filter_leaf_accounts(
    accounts: list[GlAccount], header_ids: set[int]
) -> list[GlAccount]:
    return [a for a in accounts if a.id not in header_ids]


def direct_children(
    db: Session, account_id: int, children_map: dict[int, list[int]] | None = None
) -> list[GlAccount]:
    cm = children_map if children_map is not None else build_children_map(db)
    ids = cm.get(account_id, [])
    if not ids:
        return []
    return list(
        db.scalars(
            select(GlAccount).where(GlAccount.id.in_(ids)).order_by(GlAccount.sort_order, GlAccount.code)
        ).all()
    )


def account_tree_depth(db: Session, acc: GlAccount) -> int:
    depth = 0
    current = acc
    seen: set[int] = set()
    while current.parent_id is not None:
        if current.parent_id in seen:
            break
        seen.add(int(current.parent_id))
        parent = db.get(GlAccount, int(current.parent_id))
        if parent is None:
            break
        depth += 1
        current = parent
    return depth
