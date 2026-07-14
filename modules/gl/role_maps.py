"""ربط الأدوار التشغيلية (أصول، إهلاك…) بحسابات GL."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from modules.gl.models import GlAccount, GlAccountType, GlOperationalRoleMap

# role_key → (رمز افتراضي، وصف عربي)
OPERATIONAL_ROLES: tuple[tuple[str, str, str], ...] = (
    ("fixed_asset", "1410", "أصول ثابتة — شراء فرن، أثاث، معدات"),
    ("accumulated_depreciation", "1420", "مجمع إهلاك — خصم تراكمي على الأصول"),
    ("depreciation_expense", "5400", "مصروف إهلاك — يظهر في قائمة الدخل"),
    ("consumable_asset", "5250", "مستلزمات استهلاكية — تُخصم فوراً"),
)


def role_label(role_key: str) -> str:
    for key, _code, label in OPERATIONAL_ROLES:
        if key == role_key:
            return label
    return role_key


def list_role_maps(db: Session) -> list[GlOperationalRoleMap]:
    try:
        return list(
            db.scalars(
                select(GlOperationalRoleMap)
                .options(selectinload(GlOperationalRoleMap.gl_account))
                .order_by(GlOperationalRoleMap.role_key)
            ).all()
        )
    except (OperationalError, SQLAlchemyError):
        return []


def role_maps_by_key(db: Session) -> dict[str, GlOperationalRoleMap]:
    return {m.role_key: m for m in list_role_maps(db)}


def role_account_code(db: Session, role_key: str) -> str:
    row = db.scalar(
        select(GlOperationalRoleMap)
        .options(selectinload(GlOperationalRoleMap.gl_account))
        .where(GlOperationalRoleMap.role_key == role_key)
    )
    if row is not None and row.gl_account is not None and row.gl_account.is_active:
        return str(row.gl_account.code)
    for key, code, _label in OPERATIONAL_ROLES:
        if key == role_key:
            return code
    return "5200"


def set_role_map(db: Session, *, role_key: str, gl_account_id: int) -> GlOperationalRoleMap:
    from modules.gl.service import GLError

    valid_keys = {k for k, _c, _l in OPERATIONAL_ROLES}
    if role_key not in valid_keys:
        raise GLError("دور تشغيلي غير معروف.")
    acc = db.get(GlAccount, gl_account_id)
    if acc is None:
        raise GLError("حساب GL غير موجود.")
    if role_key in ("fixed_asset", "accumulated_depreciation"):
        expected_type = GlAccountType.ASSET
    else:
        expected_type = GlAccountType.EXPENSE
    if acc.account_type != expected_type:
        raise GLError(
            "نوع الحساب لا يطابق الدور — "
            + ("اختر حساب مصروفات." if expected_type == GlAccountType.EXPENSE else "اختر حساب أصول.")
        )
    from modules.gl.hierarchy import assert_postable_account

    try:
        assert_postable_account(db, gl_account_id)
    except ValueError as exc:
        raise GLError(str(exc)) from exc
    row = db.scalar(
        select(GlOperationalRoleMap).where(GlOperationalRoleMap.role_key == role_key)
    )
    if row is None:
        row = GlOperationalRoleMap(role_key=role_key, gl_account_id=gl_account_id)
        db.add(row)
    else:
        row.gl_account_id = gl_account_id
    db.flush()
    return row


def asset_roles_gl_summary(db: Session, from_dt, to_dt) -> dict:
    """أرصدة وحركة الفترة لحسابات الأصول/الإهلاك المربوطة — للمقارنة مع السجل التشغيلي."""
    from datetime import date

    from modules.gl.reports import account_balance_as_of, account_period_movement
    from modules.gl.service import is_gl_enabled

    if not is_gl_enabled(db):
        return {"enabled": False, "roles": []}
    from_date = from_dt.date() if hasattr(from_dt, "date") else from_dt
    to_date = to_dt.date() if hasattr(to_dt, "date") else to_dt
    if not isinstance(from_date, date):
        from_date = date.fromisoformat(str(from_date)[:10])
    if not isinstance(to_date, date):
        to_date = date.fromisoformat(str(to_date)[:10])

    roles_out: list[dict] = []
    for role_key, default_code, desc in OPERATIONAL_ROLES:
        code = role_account_code(db, role_key)
        acc = db.scalar(select(GlAccount).where(GlAccount.code == code))
        if acc is None:
            roles_out.append(
                {
                    "key": role_key,
                    "desc": desc,
                    "code": code,
                    "name": "—",
                    "account_id": None,
                    "balance": None,
                    "period_debit": None,
                    "period_credit": None,
                }
            )
            continue
        pd, pc = account_period_movement(db, int(acc.id), from_date, to_date)
        bal = account_balance_as_of(db, int(acc.id), to_date)
        roles_out.append(
            {
                "key": role_key,
                "desc": desc,
                "code": acc.code,
                "name": acc.name_ar,
                "account_id": acc.id,
                "balance": bal,
                "period_debit": pd,
                "period_credit": pc,
            }
        )
    return {"enabled": True, "roles": roles_out}
