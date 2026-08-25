"""تصدير بيانات العملاء (نقاط، إحالة، محفظة) قبل التصفير."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session
from starlette.requests import Request

from modules.authz.models import User
from modules.customers.service import list_customers, resolve_customer_list_domain
from modules.platform.business_domain import BusinessDomain, is_system_admin
from modules.reporting.exports import csv_response


def resolve_customers_export_filters(
    request: Request,
    user: User,
    *,
    search: str | None = None,
    show_inactive: bool | None = None,
    customer_type: str | None = None,
    domain_raw: str | None = None,
) -> dict:
    if search is None:
        search = (request.query_params.get("q") or "").strip()
    if show_inactive is None:
        show_inactive = request.query_params.get("show_inactive") == "1"
    if customer_type is None:
        customer_type = (request.query_params.get("type") or "").strip() or None
    if domain_raw is None:
        domain_raw = (request.query_params.get("domain") or "").strip() or None
    list_domain = resolve_customer_list_domain(
        user, request.session, explicit=domain_raw if domain_raw != "all" else None
    )
    if domain_raw == "all" and is_system_admin(user):
        list_domain = None
    return {
        "search": search or None,
        "only_active": not show_inactive,
        "customer_type": customer_type,
        "business_domain": list_domain,
    }


def export_customers_csv(
    db: Session,
    *,
    search: str | None = None,
    only_active: bool = True,
    customer_type: str | None = None,
    business_domain: BusinessDomain | None = None,
):
    customers = list_customers(
        db,
        search=search or None,
        only_active=only_active,
        customer_type=customer_type,
        business_domain=business_domain,
    )
    headers = [
        "رقم",
        "الهاتف",
        "الاسم",
        "النوع",
        "المجال",
        "اسم الشركة",
        "كود الإحالة",
        "رصيد النقاط",
        "قيمة النقاط (د.ل)",
        "رصيد المحفظة",
        "إجمالي المشتريات",
        "عدد الزيارات",
        "آخر زيارة",
        "البريد",
        "نشط",
        "تاريخ التسجيل",
        "ملاحظات",
    ]
    from modules.customers.service import loyalty_settings, points_to_dinars

    loyalty = loyalty_settings(db)
    rows = []
    for c in customers:
        pts = c.points_balance or 0
        pts_value = points_to_dinars(db, pts) if loyalty.get("enabled") else 0
        dom = getattr(c.business_domain, "value", c.business_domain) or ""
        ctype = getattr(c.customer_type, "value", c.customer_type) or ""
        rows.append(
            (
                c.id,
                c.phone or "",
                c.name or "",
                ctype,
                dom,
                c.company_name or "",
                c.referral_code or "",
                pts,
                pts_value,
                c.wallet_balance or 0,
                c.total_spent or 0,
                c.visits_count or 0,
                c.last_visit_at,
                c.email or "",
                "نعم" if c.is_active else "لا",
                c.created_at,
                (c.notes or "").replace("\n", " ").strip(),
            )
        )
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return csv_response(f"customers-export-{ts}", headers, rows)


def export_customers_for_request(request: Request, db: Session, user: User):
    """تصدير من طلب HTTP (صفحة العملاء أو النسخ الاحتياطي)."""
    filters = resolve_customers_export_filters(request, user)
    return export_customers_csv(db, **filters)


def write_customers_csv_file(
    db: Session,
    output_path: str | Path,
    *,
    only_active: bool = False,
) -> Path:
    """كتابة ملف CSV على القرص — يعمل من سطر الأوامر بدون إعادة تشغيل السيرفر."""
    path = Path(output_path)
    resp = export_customers_csv(db, only_active=only_active)
    path.write_bytes(resp.body)
    return path
