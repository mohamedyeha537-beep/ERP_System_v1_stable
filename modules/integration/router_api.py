import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db_session
from app.security_utils import require_ip_allowlist
from infra.config import get_settings
from modules.integration.schemas import OnlineOrderIn, OnlineOrderOut
from modules.sales.models import Sale, SaleStatus
from modules.sales.service import SalesError, create_online_sale_completed

router = APIRouter(prefix="/api/integration", tags=["integration"])


def verify_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    settings = get_settings()
    require_ip_allowlist(
        request,
        settings.integration_api_ip_allowlist,
        label="Integration API",
    )
    expected = (settings.integration_api_key or "").strip()
    if not x_api_key or not expected or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="مفتاح API غير صالح.")
    return True


@router.post("/orders", response_model=OnlineOrderOut)
def post_online_order(
    body: OnlineOrderIn,
    _: bool = Depends(verify_api_key),
    db: Session = Depends(get_db_session),
):
    existing = db.execute(
        select(Sale).where(Sale.external_order_id == body.external_order_id)
    ).scalar_one_or_none()
    if existing and existing.status == SaleStatus.COMPLETED:
        return OnlineOrderOut(
            sale_id=existing.id,
            status=existing.status.value,
            total=existing.total,
            message="الطلب مسجّل مسبقاً (منع التكرار).",
        )
    if existing and existing.status == SaleStatus.DRAFT:
        db.delete(existing)
        db.flush()

    try:
        lines = [(ln.product_id, ln.quantity) for ln in body.lines]
        sale = create_online_sale_completed(
            db,
            external_order_id=body.external_order_id,
            lines=lines,
            user_id=None,
        )
        db.commit()
    except SalesError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e

    return OnlineOrderOut(sale_id=sale.id, status=sale.status.value, total=sale.total)
