"""متجر إلكتروني واحد — نفس منتجات POS ووسائل الدفع وتدفق الطلبات."""
from __future__ import annotations

import json
import secrets
import time
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from modules.catalog.models import Product, ProductCategory, ProductKind
from modules.catalog.service import assert_product_sellable
from modules.customers.service import require_valid_phone
from modules.delivery.service import list_zones
from modules.shop.models import ShopProductRating
from modules.messaging.chat_order_service import (
    PHASE_AWAIT_RECEIPT,
    PHASE_SUBMITTED,
    add_product_to_cart,
    attach_receipt_and_submit,
    bank_payment_instructions,
    cart_count,
    cart_lines,
    cart_total,
    clear_cart,
    create_or_update_chat_sale,
    customer_total,
    delivery_fee,
    list_chat_payment_options,
    load_order_data,
    order_confirmation_message,
    order_payment_amount,
    save_guest_phone,
    set_order_phase,
    submit_cash_order,
)
from modules.messaging.models import WebChatSession, WebChatSessionStatus
from modules.messaging.service import MessagingError
from modules.messaging.web_chat_service import get_session_by_token
from modules.payments.models import PaymentMethodKind
from modules.sales.models import Sale
from modules.settings.service import get_bool, get_setting


class ShopError(Exception):
    pass


_CATALOG_CACHE: dict[str, Any] = {"mono": 0.0, "payload": None}
_CATALOG_TTL_SEC = 45


def _invalidate_catalog_cache() -> None:
    _CATALOG_CACHE["mono"] = 0.0
    _CATALOG_CACHE["payload"] = None


def invalidate_shop_catalog_cache() -> None:
    """يُستدعى بعد تعديل الفئات أو الأصناف."""
    _invalidate_catalog_cache()


def _category_children_map(cats: list[ProductCategory]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for cat in cats:
        if cat.parent_id is not None:
            out.setdefault(int(cat.parent_id), []).append(int(cat.id))
    return out


def _expand_category_ids(root_id: int, children_map: dict[int, list[int]]) -> set[int]:
    ids = {int(root_id)}
    pending = [int(root_id)]
    while pending:
        pid = pending.pop()
        for cid in children_map.get(pid, []):
            if cid not in ids:
                ids.add(cid)
                pending.append(cid)
    return ids


def _shop_visible_category_ids_from(cats: list[ProductCategory]) -> frozenset[int]:
    by_id = {int(c.id): c for c in cats}
    visible_ids: set[int] = set()
    for cat in cats:
        cur: ProductCategory | None = cat
        visible = True
        while cur is not None:
            if not cur.show_in_shop:
                visible = False
                break
            cur = by_id.get(int(cur.parent_id)) if cur.parent_id else None
        if visible:
            visible_ids.add(int(cat.id))
    return frozenset(visible_ids)


def _category_ids_with_children(
    db: Session, root_id: int, *, children_map: dict[int, list[int]] | None = None
) -> set[int]:
    if children_map is not None:
        return _expand_category_ids(int(root_id), children_map)
    ids = {int(root_id)}
    pending = [int(root_id)]
    while pending:
        pid = pending.pop()
        for cid in db.scalars(
            select(ProductCategory.id).where(ProductCategory.parent_id == pid)
        ).all():
            cid = int(cid)
            if cid not in ids:
                ids.add(cid)
                pending.append(cid)
    return ids


def _shop_visible_category_ids(db: Session) -> frozenset[int]:
    rows = list(db.scalars(select(ProductCategory)).all())
    return _shop_visible_category_ids_from(rows)


def shop_enabled(db: Session) -> bool:
    raw = (get_setting(db, "shop_enabled", "") or "").strip()
    if raw == "0":
        return False
    if raw == "1":
        return True
    return get_bool(db, "shop_enabled", True)


def _product_image_url(product: Product) -> str | None:
    from modules.catalog.uploads import product_image_public_url

    return product_image_public_url(product.image_filename)


def _assert_product_shop_visible(db: Session, product: Product) -> None:
    if product.category_id is not None and int(product.category_id) not in _shop_visible_category_ids(db):
        raise ShopError("هذا الصنف غير معروض في المتجر الإلكتروني.")


def list_shop_products(db: Session, *, category_id: int | None = None) -> list[Product]:
    stmt = (
        select(Product)
        .options(selectinload(Product.category))
        .where(
            Product.kind == ProductKind.FINAL_SELLABLE,
            Product.is_active.is_(True),
            Product.show_in_pos.is_(True),
            Product.sell_price.isnot(None),
        )
        .order_by(Product.name_ar.asc())
    )
    visible_cat_ids = _shop_visible_category_ids(db)
    products = [
        p
        for p in db.scalars(stmt).all()
        if p.category_id is None or int(p.category_id) in visible_cat_ids
    ]
    if category_id is None:
        return products
    cat_ids = _category_ids_with_children(db, int(category_id))
    return [p for p in products if p.category_id in cat_ids]


def _product_ratings_map(db: Session, product_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not product_ids:
        return {}
    try:
        rows = db.execute(
            select(
                ShopProductRating.product_id,
                func.avg(ShopProductRating.stars),
                func.count(ShopProductRating.id),
            )
            .where(ShopProductRating.product_id.in_(product_ids))
            .group_by(ShopProductRating.product_id)
        ).all()
    except Exception:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for pid, avg_stars, cnt in rows:
        avg = Decimal(str(avg_stars or 0)).quantize(Decimal("0.1"))
        out[int(pid)] = {"avg_rating": str(avg), "rating_count": int(cnt or 0)}
    return out


def _fetch_shop_products(db: Session) -> list[Product]:
    stmt = (
        select(Product)
        .options(selectinload(Product.category))
        .where(
            Product.kind == ProductKind.FINAL_SELLABLE,
            Product.is_active.is_(True),
            Product.show_in_pos.is_(True),
            Product.sell_price.isnot(None),
        )
        .order_by(Product.name_ar.asc())
    )
    all_cats = list(db.scalars(select(ProductCategory)).all())
    visible_cat_ids = _shop_visible_category_ids_from(all_cats)
    return [
        p
        for p in db.scalars(stmt).all()
        if p.category_id is None or int(p.category_id) in visible_cat_ids
    ]


def _visible_subtree_ids(
    root_id: int,
    children_map: dict[int, list[int]],
    visible_ids: frozenset[int],
) -> set[int]:
    return {cid for cid in _expand_category_ids(root_id, children_map) if cid in visible_ids}


def _build_sections_and_categories(
    products: list[Product], all_cats: list[ProductCategory]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[int]]]:
    """أقسام + فئات + خريطة تصفية — كل الفئات الظاهرة في المتجر (وليس فقط من لها منتجات)."""
    children_map = _category_children_map(all_cats)
    visible_ids = _shop_visible_category_ids_from(all_cats)
    if not visible_ids:
        return [], [], {}

    roots = [
        c
        for c in all_cats
        if c.parent_id is None and int(c.id) in visible_ids
    ]

    filter_map: dict[str, list[int]] = {}
    sections: list[dict[str, Any]] = []
    categories: list[dict[str, Any]] = []

    for root in sorted(roots, key=lambda c: (c.sort_order, c.name_ar or "")):
        rid = int(root.id)
        tree_ids = _visible_subtree_ids(rid, children_map, visible_ids)
        if not tree_ids:
            continue
        filter_map[str(rid)] = sorted(tree_ids)
        root_product_count = sum(
            1 for p in products if p.category_id is not None and int(p.category_id) in tree_ids
        )

        direct_children = [
            c
            for c in all_cats
            if c.parent_id == rid and int(c.id) in visible_ids
        ]

        subs: list[dict[str, Any]] = []
        for sub in sorted(direct_children, key=lambda c: (c.sort_order, c.name_ar or "")):
            sid = int(sub.id)
            sub_tree = _visible_subtree_ids(sid, children_map, visible_ids)
            filter_map[str(sid)] = sorted(sub_tree)
            sub_count = sum(
                1 for p in products if p.category_id is not None and int(p.category_id) in sub_tree
            )
            subs.append(
                {
                    "id": sid,
                    "name_ar": sub.name_ar,
                    "product_count": sub_count,
                }
            )

        if not root_product_count and not subs:
            continue

        sections.append(
            {
                "id": rid,
                "name_ar": root.name_ar,
                "product_count": root_product_count,
                "subcategories": subs,
            }
        )
        categories.append(
            {
                "id": rid,
                "name_ar": root.name_ar,
                "product_count": root_product_count,
            }
        )

    return sections, categories, filter_map


def _build_full_catalog(db: Session) -> dict[str, Any]:
    now = time.monotonic()
    cached = _CATALOG_CACHE.get("payload")
    if cached is not None and (now - float(_CATALOG_CACHE.get("mono") or 0)) < _CATALOG_TTL_SEC:
        return cached

    products = _fetch_shop_products(db)
    all_cats = list(db.scalars(select(ProductCategory)).all())
    sections, categories, filter_map = _build_sections_and_categories(products, all_cats)
    pids = [int(p.id) for p in products]
    ratings = _product_ratings_map(db, pids)
    from modules.customers.referral_service import referral_settings

    ref = referral_settings(db)
    payload = {
        "sections": sections,
        "categories": categories,
        "referral_enabled": bool(ref.get("referral_enabled")),
        "filter_map": filter_map,
        "products": [
            product_to_dict(p, rating=ratings.get(int(p.id))) for p in products
        ],
    }
    _CATALOG_CACHE["mono"] = now
    _CATALOG_CACHE["payload"] = payload
    return payload


def list_shop_sections(db: Session) -> list[dict[str, Any]]:
    return _build_full_catalog(db)["sections"]


def list_shop_categories(db: Session) -> list[dict[str, Any]]:
    return _build_full_catalog(db)["categories"]


def _shop_product_description(notes: str | None) -> str:
    """ملاحظات الصنف إدارية (استيراد Odoo، مخزون…) — لا تُعرض للزبائن."""
    return ""


def product_to_dict(product: Product, *, rating: dict[str, Any] | None = None) -> dict[str, Any]:
    price = Decimal(str(product.sell_price or 0)).quantize(Decimal("0.001"))
    desc = _shop_product_description(product.notes)
    if len(desc) > 160:
        desc = desc[:157] + "…"
    r = rating or {}
    payload = {
        "id": int(product.id),
        "name_ar": product.name_ar,
        "description": desc,
        "unit": product.unit or "قطعة",
        "sell_price": str(price),
        "image_url": _product_image_url(product),
        "category_id": int(product.category_id) if product.category_id else None,
        "category_name": product.category.name_ar if product.category else None,
        "avg_rating": r.get("avg_rating"),
        "rating_count": r.get("rating_count", 0),
    }
    return payload


def catalog_payload(db: Session, *, category_id: int | None = None) -> dict[str, Any]:
    full = _build_full_catalog(db)
    if category_id is None:
        return {
            "sections": full["sections"],
            "categories": full["categories"],
            "referral_enabled": full["referral_enabled"],
            "filter_map": full.get("filter_map") or {},
            "products": full["products"],
        }
    key = str(int(category_id))
    allowed = set((full.get("filter_map") or {}).get(key, [int(category_id)]))
    products = [
        p
        for p in full["products"]
        if p.get("category_id") is not None and int(p["category_id"]) in allowed
    ]
    return {
        "sections": full["sections"],
        "categories": full["categories"],
        "referral_enabled": full["referral_enabled"],
        "filter_map": full.get("filter_map") or {},
        "products": products,
    }


def create_shop_session(db: Session) -> WebChatSession:
    token = secrets.token_urlsafe(32)
    session = WebChatSession(
        token=token,
        status=WebChatSessionStatus.OPEN.value,
        department="shop",
        order_phase="browse",
    )
    db.add(session)
    db.flush()
    return session


def get_shop_session(db: Session, token: str) -> WebChatSession:
    session = get_session_by_token(db, token)
    if session is None:
        raise ShopError("جلسة المتجر غير موجودة.")
    if (session.department or "").strip().lower() != "shop":
        raise ShopError("جلسة غير صالحة للمتجر.")
    return session


def shop_add_to_cart(db: Session, session: WebChatSession, product_id: int, qty: Decimal) -> None:
    product = db.get(Product, int(product_id))
    if product is None:
        raise ShopError("المنتج غير موجود.")
    try:
        assert_product_sellable(product)
    except Exception as exc:
        raise ShopError(str(exc)) from exc
    _assert_product_shop_visible(db, product)
    if product.sell_price is None:
        raise ShopError("المنتج بدون سعر.")
    try:
        add_product_to_cart(db, session, product, qty)
    except MessagingError as exc:
        raise ShopError(str(exc)) from exc


def shop_set_cart_qty(
    db: Session, session: WebChatSession, product_id: int, qty: Decimal
) -> None:
    data = load_order_data(session)
    cart: list[dict[str, Any]] = list(data.get("cart") or [])
    pid = int(product_id)
    if qty <= 0:
        cart = [ln for ln in cart if int(ln.get("product_id") or 0) != pid]
    else:
        product = db.get(Product, pid)
        if product is None or product.sell_price is None:
            raise ShopError("المنتج غير متاح.")
        _assert_product_shop_visible(db, product)
        unit = Decimal(str(product.sell_price)).quantize(Decimal("0.001"))
        qty = qty.quantize(Decimal("0.0001"))
        found = False
        for ln in cart:
            if int(ln.get("product_id") or 0) == pid:
                ln["qty"] = str(qty)
                ln["line_total"] = str((qty * unit).quantize(Decimal("0.001")))
                ln["name_ar"] = product.name_ar or ln.get("name_ar") or ""
                ln["unit_price"] = str(unit)
                found = True
                break
        if not found and qty > 0:
            shop_add_to_cart(db, session, pid, qty)
            return
    data["cart"] = cart
    session.order_json = json.dumps(data, ensure_ascii=False)
    db.flush()


def shop_set_fulfillment(session: WebChatSession, fulfillment: str) -> None:
    kind = (fulfillment or "PICKUP").strip().upper()
    if kind not in ("PICKUP", "DELIVERY"):
        raise ShopError("نوع الاستلام غير صالح.")
    data = load_order_data(session)
    data["fulfillment"] = kind
    if kind != "DELIVERY":
        data["delivery_address"] = ""
        data["delivery_zone_id"] = None
        data["delivery_zone_name"] = ""
        data["delivery_fee"] = "0"
    session.order_json = json.dumps(data, ensure_ascii=False)


def shop_set_delivery_zone(db: Session, session: WebChatSession, zone_id: int) -> None:
    from modules.delivery.service import get_zone

    zone = get_zone(db, int(zone_id))
    if zone is None or not zone.is_active:
        raise ShopError("منطقة التوصيل غير متاحة.")
    data = load_order_data(session)
    data["delivery_zone_id"] = int(zone.id)
    data["delivery_zone_name"] = zone.name_ar
    data["delivery_fee"] = str(Decimal(str(zone.fee or 0)).quantize(Decimal("0.001")))
    data["fulfillment"] = "DELIVERY"
    session.order_json = json.dumps(data, ensure_ascii=False)


def shop_set_delivery_address(session: WebChatSession, address: str) -> None:
    """اختياري — لم يعد مطلوباً في المتجر؛ يُحفظ إن وُجد."""
    addr = (address or "").strip()
    data = load_order_data(session)
    data["delivery_address"] = addr
    data["fulfillment"] = "DELIVERY"
    session.order_json = json.dumps(data, ensure_ascii=False)


def shop_set_payment_method(db: Session, session: WebChatSession, payment_method_id: int) -> None:
    opts = {int(o["id"]): o for o in list_chat_payment_options(db)}
    pm = opts.get(int(payment_method_id))
    if pm is None:
        raise ShopError("وسيلة الدفع غير متاحة.")
    data = load_order_data(session)
    data["payment_method_id"] = int(pm["id"])
    data["payment_method_name"] = pm["name"]
    data["payment_kind"] = pm["kind"]
    session.order_json = json.dumps(data, ensure_ascii=False)


def shop_set_guest(session: WebChatSession, *, name: str | None, phone: str) -> None:
    try:
        phone_clean = require_valid_phone((phone or "").strip())
    except Exception as exc:
        raise ShopError("رقم الهاتف غير صالح.") from exc
    session.guest_phone = phone_clean
    if (name or "").strip():
        session.guest_name = (name or "").strip()[:120]


def shop_set_referral_code(session: WebChatSession, code: str) -> None:
    from modules.customers.referral_service import normalize_referral_code_input

    data = load_order_data(session)
    norm = normalize_referral_code_input((code or "").strip())
    if norm:
        data["referral_code"] = norm
    else:
        data.pop("referral_code", None)
    session.order_json = json.dumps(data, ensure_ascii=False)


def shop_apply_referral_to_draft_if_ready(db: Session, session: WebChatSession) -> None:
    """يربط الإحالة بمسودة البيع إن وُجدت الهاتف والكود."""
    from modules.messaging.chat_order_service import (
        get_draft_sale,
        try_apply_session_referral_to_sale,
    )

    if not session.sale_id or not (session.guest_phone or "").strip():
        return
    data = load_order_data(session)
    if not (data.get("referral_code") or "").strip():
        return
    sale = get_draft_sale(db, int(session.sale_id))
    if sale is not None:
        try_apply_session_referral_to_sale(db, session, sale)


def _apply_referral_to_shop_sale(db: Session, session: WebChatSession, sale: Sale) -> None:
    from modules.messaging.chat_order_service import try_apply_session_referral_to_sale

    try_apply_session_referral_to_sale(db, session, sale)


def build_product_share_payload(
    db: Session,
    *,
    product_id: int,
    phone: str | None,
    base_url: str,
) -> dict[str, Any]:
    product = db.get(Product, int(product_id))
    if product is None:
        raise ShopError("المنتج غير موجود.")
    store = (get_setting(db, "store_name", "") or "").strip()
    base = (base_url or "").rstrip("/")
    code = ""
    url = f"{base}/shop?product={int(product.id)}"
    from modules.customers.referral_service import ensure_referral_code, referral_settings
    from modules.customers.service import get_or_create_by_phone

    ref = referral_settings(db)
    consent_new = False
    customer_id: int | None = None
    if (phone or "").strip():
        try:
            phone_clean = require_valid_phone((phone or "").strip())
            cust = get_or_create_by_phone(db, phone=phone_clean, name=None)
            customer_id = int(cust.id)
            from modules.messaging.service import grant_shop_share_consent

            consent_new = grant_shop_share_consent(db, customer_id)
            if ref.get("referral_enabled"):
                code = ensure_referral_code(db, cust)
                url = f"{base}/shop?ref={code}&product={int(product.id)}"
        except Exception:
            pass
    name = (product.name_ar or "").strip()
    price = Decimal(str(product.sell_price or 0)).quantize(Decimal("0.001"))
    message = f"جرّب {name} من {store} — {price} د.ل\n{url}"
    if code:
        message += f"\n🎁 استخدم كود الإحالة: {code}"
    return {
        "url": url,
        "message": message,
        "referral_code": code,
        "product_name": name,
        "consent_new": consent_new,
        "customer_id": customer_id,
    }


def rate_shop_product(
    db: Session,
    *,
    product_id: int,
    stars: int,
) -> dict[str, Any]:
    if stars < 1 or stars > 5:
        raise ShopError("التقييم من 1 إلى 5.")
    product = db.get(Product, int(product_id))
    if product is None:
        raise ShopError("المنتج غير موجود.")
    try:
        db.add(
            ShopProductRating(
                product_id=int(product_id),
                guest_phone=None,
                stars=int(stars),
            )
        )
        db.flush()
    except Exception as exc:
        raise ShopError("تعذّر حفظ التقييم.") from exc
    row = db.execute(
        select(func.avg(ShopProductRating.stars), func.count(ShopProductRating.id)).where(
            ShopProductRating.product_id == int(product_id)
        )
    ).one()
    avg = Decimal(str(row[0] or 0)).quantize(Decimal("0.1"))
    return {
        "avg_rating": str(avg),
        "rating_count": int(row[1] or 0),
    }


def _touch_session(session: WebChatSession) -> None:
    from datetime import datetime, timezone

    session.last_message_at = datetime.now(timezone.utc)


def finalize_shop_order(
    db: Session,
    session: WebChatSession,
    *,
    proof_filename: str | None = None,
) -> tuple[Sale | None, str]:
    """إتمام الطلب — يُرسل لنقطة البيع كطلب أونلاين."""
    if not cart_lines(session):
        raise ShopError("السلة فارغة.")
    if not (session.guest_phone or "").strip():
        raise ShopError("رقم الهاتف مطلوب.")
    save_guest_phone(db, session, session.guest_phone or "")
    data = load_order_data(session)
    fulfillment = (data.get("fulfillment") or "").strip().upper()
    if fulfillment not in ("PICKUP", "DELIVERY"):
        raise ShopError("اختر الاستلام من المطعم أو التوصيل.")
    if fulfillment == "DELIVERY":
        if not data.get("delivery_zone_id"):
            raise ShopError("اختر منطقة التوصيل.")
    try:
        if proof_filename:
            sale = attach_receipt_and_submit(db, session, proof_filename=proof_filename)
        else:
            sale = submit_cash_order(db, session)
    except MessagingError as exc:
        raise ShopError(str(exc)) from exc
    _apply_referral_to_shop_sale(db, session, sale)
    set_order_phase(session, PHASE_SUBMITTED)
    _touch_session(session)
    return sale, "submitted"


def delivery_zones_payload(db: Session) -> list[dict[str, Any]]:
    return [
        {
            "id": int(z.id),
            "name_ar": z.name_ar,
            "fee": str(Decimal(str(z.fee or 0)).quantize(Decimal("0.001"))),
        }
        for z in list_zones(db, only_active=True)
    ]


def session_state(db: Session, session: WebChatSession) -> dict[str, Any]:
    data = load_order_data(session)
    kind = (data.get("payment_kind") or "").strip()
    pm_id = data.get("payment_method_id")
    bank_instructions = None
    if pm_id and kind == PaymentMethodKind.BANK.value:
        bank_instructions = bank_payment_instructions(
            db, method_name=(data.get("payment_method_name") or "")
        )
    sale_id = session.sale_id
    confirmation = None
    if session.order_phase in (PHASE_SUBMITTED, PHASE_AWAIT_RECEIPT) and sale_id:
        sale = db.get(Sale, int(sale_id))
        if sale is not None:
            confirmation = order_confirmation_message(db, session, sale)
    from modules.customers.referral_service import referral_settings

    ref = referral_settings(db)
    return {
        "token": session.token,
        "order_phase": session.order_phase or "browse",
        "guest_name": session.guest_name,
        "guest_phone": session.guest_phone,
        "referral_code": (data.get("referral_code") or "").strip() or None,
        "referral_enabled": bool(ref.get("referral_enabled")),
        "cart": cart_lines(session),
        "cart_count": cart_count(session),
        "cart_total": str(cart_total(session)),
        "delivery_fee": str(delivery_fee(session)),
        "customer_total": str(customer_total(session)),
        "payment_amount": str(order_payment_amount(session)),
        "fulfillment": data.get("fulfillment"),
        "delivery_zone_id": data.get("delivery_zone_id"),
        "delivery_zone_name": data.get("delivery_zone_name"),
        "delivery_address": data.get("delivery_address"),
        "payment_method_id": data.get("payment_method_id"),
        "payment_method_name": data.get("payment_method_name"),
        "payment_kind": kind or None,
        "payment_methods": list_chat_payment_options(db),
        "delivery_zones": delivery_zones_payload(db),
        "bank_instructions": bank_instructions,
        "sale_id": sale_id,
        "confirmation_message": confirmation,
    }
