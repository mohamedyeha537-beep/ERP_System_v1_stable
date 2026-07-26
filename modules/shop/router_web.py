"""واجهة المتجر الإلكتروني /shop"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.deps import DBSession
from app.jinja_env import templates
from modules.messaging.chat_order_service import clear_cart
from modules.messaging.web_chat_service import web_chat_enabled
from modules.shop.service import (
    ShopError,
    build_product_share_payload,
    catalog_payload,
    create_shop_session,
    finalize_shop_order,
    get_shop_session,
    lookup_guest_name_by_phone,
    rate_shop_product,
    session_state,
    shop_add_to_cart,
    shop_enabled,
    shop_set_cart_qty,
    shop_set_delivery_address,
    shop_set_delivery_zone,
    shop_set_fulfillment,
    shop_set_guest,
    shop_set_payment_method,
    shop_set_referral_code,
    shop_apply_referral_to_draft_if_ready,
)

router = APIRouter(tags=["shop"])
api_router = APIRouter(prefix="/api/shop", tags=["shop-api"])


def _require_shop(db: DBSession) -> None:
    if not shop_enabled(db):
        raise HTTPException(status_code=403, detail="المتجر غير متاح حالياً.")


class TokenPayload(BaseModel):
    token: str


class CartAddPayload(BaseModel):
    token: str
    product_id: int
    qty: str = "1"


class CartUpdatePayload(BaseModel):
    token: str
    product_id: int
    qty: str


class FulfillmentPayload(BaseModel):
    token: str
    fulfillment: str = Field(..., description="PICKUP or DELIVERY")


class DeliveryZonePayload(BaseModel):
    token: str
    zone_id: int


class DeliveryAddressPayload(BaseModel):
    token: str
    address: str = Field("", max_length=500)


class PaymentPayload(BaseModel):
    token: str
    payment_method_id: int


class GuestPayload(BaseModel):
    token: str
    phone: str = Field(..., min_length=9, max_length=40)
    name: str | None = Field(None, max_length=120)


class ReferralPayload(BaseModel):
    token: str
    referral_code: str = Field("", max_length=32)


class SharePayload(BaseModel):
    token: str
    product_id: int
    phone: str | None = Field(None, max_length=40)
    notify: bool = False


class RatePayload(BaseModel):
    product_id: int
    stars: int = Field(..., ge=1, le=5)


@router.get("/shop", response_class=HTMLResponse)
def shop_page(request: Request, db: DBSession):
    from modules.branding.service import get_shop_branding
    from modules.web_marketing.service import (
        SURFACE_RESTAURANT_SHOP,
        get_public_web_config,
    )

    enabled = shop_enabled(db)
    shop_brand = get_shop_branding(db)
    store = shop_brand["store_name"]
    web_mkt = get_public_web_config(
        db,
        SURFACE_RESTAURANT_SHOP,
        page_path="/shop",
        default_title=f"{store} — المتجر",
    )
    return templates.TemplateResponse(
        "shop.html",
        {
            "request": request,
            "enabled": enabled,
            "store_name": store,
            "chat_enabled": web_chat_enabled(db),
            "shop_brand": shop_brand,
            "web_mkt": web_mkt,
        },
    )


def _shop_info_response(request: Request, db: DBSession, page: str):
    from modules.branding.service import get_shop_info_page
    from modules.web_marketing.service import (
        SURFACE_RESTAURANT_SHOP,
        get_public_web_config,
    )

    info = get_shop_info_page(db, page)
    store = info["store_name"]
    web_mkt = get_public_web_config(
        db,
        SURFACE_RESTAURANT_SHOP,
        page_path=info["path"],
        default_title=f"{info['page_title']} — {store}",
    )
    return templates.TemplateResponse(
        "shop_info_page.html",
        {
            "request": request,
            "store_name": store,
            "chat_enabled": web_chat_enabled(db),
            "shop_brand": info["shop_brand"],
            "web_mkt": web_mkt,
            "active_nav": info["active_nav"],
            "page_title": info["page_title"],
            "page_body": info["page_body"],
            "page_available": info["available"],
        },
    )


@router.get("/offers", response_class=HTMLResponse)
def shop_offers_page(request: Request, db: DBSession):
    return _shop_info_response(request, db, "offers")


@router.get("/about", response_class=HTMLResponse)
def shop_about_page(request: Request, db: DBSession):
    return _shop_info_response(request, db, "about")


@api_router.post("/session")
def shop_create_session(db: DBSession):
    _require_shop(db)
    session = create_shop_session(db)
    db.commit()
    return {"ok": True, "token": session.token}


@api_router.get("/session")
def shop_get_session(token: str, db: DBSession):
    _require_shop(db)
    try:
        session = get_shop_session(db, token)
    except ShopError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.get("/catalog")
def shop_catalog(db: DBSession, category_id: int | None = None):
    _require_shop(db)
    payload = catalog_payload(db, category_id=category_id)
    return JSONResponse(
        content={"ok": True, **payload},
        headers={"Cache-Control": "public, max-age=45, stale-while-revalidate=120"},
    )


@api_router.post("/cart/add")
def shop_cart_add(payload: CartAddPayload, db: DBSession):
    _require_shop(db)
    try:
        qty = Decimal((payload.qty or "1").strip().replace(",", "."))
        session = get_shop_session(db, payload.token)
        shop_add_to_cart(db, session, payload.product_id, qty)
        db.commit()
    except (ShopError, InvalidOperation) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.post("/cart/update")
def shop_cart_update(payload: CartUpdatePayload, db: DBSession):
    _require_shop(db)
    try:
        qty = Decimal((payload.qty or "0").strip().replace(",", "."))
        session = get_shop_session(db, payload.token)
        shop_set_cart_qty(db, session, payload.product_id, qty)
        db.commit()
    except (ShopError, InvalidOperation) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.post("/cart/clear")
def shop_cart_clear(payload: TokenPayload, db: DBSession):
    _require_shop(db)
    try:
        session = get_shop_session(db, payload.token)
        clear_cart(session)
        db.commit()
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.post("/checkout/guest")
def shop_checkout_guest(payload: GuestPayload, db: DBSession):
    _require_shop(db)
    try:
        session = get_shop_session(db, payload.token)
        shop_set_guest(session, name=payload.name, phone=payload.phone, db=db)
        shop_apply_referral_to_draft_if_ready(db, session)
        db.commit()
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.post("/checkout/lookup-guest")
def shop_checkout_lookup_guest(payload: GuestPayload, db: DBSession):
    """يعيد اسم الزبون المسجّل لنفس رقم الهاتف (بدون بيانات حسّاسة أخرى)."""
    _require_shop(db)
    try:
        get_shop_session(db, payload.token)
    except ShopError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    name = lookup_guest_name_by_phone(db, payload.phone)
    return {"ok": True, "found": bool(name), "name": name or ""}


@api_router.post("/checkout/referral")
def shop_checkout_referral(payload: ReferralPayload, db: DBSession):
    _require_shop(db)
    try:
        session = get_shop_session(db, payload.token)
        shop_set_referral_code(session, payload.referral_code)
        shop_apply_referral_to_draft_if_ready(db, session)
        db.commit()
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.post("/share")
def shop_share(payload: SharePayload, request: Request, db: DBSession):
    _require_shop(db)
    try:
        get_shop_session(db, payload.token)
        base = str(request.base_url).rstrip("/")
        data = build_product_share_payload(
            db,
            product_id=payload.product_id,
            phone=payload.phone,
            base_url=base,
        )
        data.pop("consent_new", None)
        customer_id = data.pop("customer_id", None)
        referral_code = (data.get("referral_code") or "").strip()
        share_message = (data.get("message") or "").strip()
        db.commit()
        if customer_id:
            from modules.customers.service import get_customer

            cust = get_customer(db, int(customer_id))
            if cust is not None:
                if payload.notify and referral_code:
                    from modules.messaging.service import notify_referral_product_shared

                    notify_referral_product_shared(
                        db,
                        customer=cust,
                        product_id=int(payload.product_id),
                        product_name=(data.get("product_name") or "").strip(),
                        referral_code=referral_code,
                        share_url=(data.get("url") or "").strip(),
                        share_message=share_message,
                    )
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **data}


@api_router.post("/rate")
def shop_rate(payload: RatePayload, db: DBSession):
    _require_shop(db)
    try:
        data = rate_shop_product(
            db,
            product_id=payload.product_id,
            stars=payload.stars,
        )
        db.commit()
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **data}


@api_router.post("/checkout/fulfillment")
def shop_checkout_fulfillment(payload: FulfillmentPayload, db: DBSession):
    _require_shop(db)
    try:
        session = get_shop_session(db, payload.token)
        shop_set_fulfillment(session, payload.fulfillment)
        db.commit()
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.post("/checkout/delivery-zone")
def shop_checkout_zone(payload: DeliveryZonePayload, db: DBSession):
    _require_shop(db)
    try:
        session = get_shop_session(db, payload.token)
        shop_set_delivery_zone(db, session, payload.zone_id)
        db.commit()
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.post("/checkout/delivery-address")
def shop_checkout_address(payload: DeliveryAddressPayload, db: DBSession):
    _require_shop(db)
    try:
        session = get_shop_session(db, payload.token)
        shop_set_delivery_address(session, payload.address)
        db.commit()
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.post("/checkout/payment")
def shop_checkout_payment(payload: PaymentPayload, db: DBSession):
    _require_shop(db)
    try:
        session = get_shop_session(db, payload.token)
        shop_set_payment_method(db, session, payload.payment_method_id)
        db.commit()
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "state": session_state(db, session)}


@api_router.post("/checkout/submit")
def shop_checkout_submit(payload: TokenPayload, db: DBSession):
    _require_shop(db)
    try:
        session = get_shop_session(db, payload.token)
        sale, status = finalize_shop_order(db, session)
        if sale is not None:
            from modules.web_marketing.analytics import record_purchase

            st = session_state(db, session)
            record_purchase(
                db,
                sale_id=sale.id,
                value=st.get("customer_total"),
                page_path="/shop",
            )
        db.commit()
    except ShopError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = session_state(db, session)
    return {
        "ok": True,
        "status": status,
        "sale_id": sale.id if sale else state.get("sale_id"),
        "state": state,
    }

