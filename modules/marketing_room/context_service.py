"""سياق الأعمال للمطعم/الفندق يُمرَّر لوكلاء التسويق."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.catalog.models import Product, ProductKind
from modules.settings.service import get_setting


def build_marketing_context(db: Session, *, business_domain: str = "shared") -> dict[str, Any]:
    store = (get_setting(db, "store_name", "") or "").strip() or "المطعم"
    hotel = (get_setting(db, "brand_hotel_name", "") or "").strip() or "الشقق الفندقية"
    tagline = (get_setting(db, "brand_header_tagline", "") or "").strip()
    ig = (get_setting(db, "brand_shop_social_instagram", "") or "").strip()
    fb = (get_setting(db, "brand_shop_social_facebook", "") or "").strip()

    products: list[dict[str, Any]] = []
    try:
        rows = db.scalars(
            select(Product)
            .where(
                Product.is_active.is_(True),
                Product.kind == ProductKind.FINAL_SELLABLE,
                Product.show_in_pos.is_(True),
            )
            .order_by(Product.id.desc())
            .limit(24)
        ).all()
        for p in rows:
            products.append(
                {
                    "id": p.id,
                    "name_ar": p.name_ar,
                    "sell_price": str(p.sell_price) if p.sell_price is not None else None,
                    "is_hotel_breakfast": bool(getattr(p, "is_hotel_breakfast", False)),
                }
            )
    except Exception:
        products = []

    rooms: list[dict[str, Any]] = []
    try:
        from modules.hotel.models import HotelRoom

        room_rows = db.scalars(
            select(HotelRoom).where(HotelRoom.is_active.is_(True)).order_by(HotelRoom.number).limit(20)
        ).all()
        for r in room_rows:
            rooms.append(
                {
                    "id": r.id,
                    "number": getattr(r, "number", None),
                    "name_ar": getattr(r, "name_ar", None) or str(getattr(r, "number", "")),
                    "nightly_price": str(r.nightly_price) if getattr(r, "nightly_price", None) is not None else None,
                }
            )
    except Exception:
        rooms = []

    domain = (business_domain or "shared").strip().lower()
    if domain not in ("restaurant", "hotel", "shared"):
        domain = "shared"

    return {
        "business_domain": domain,
        "store_name": store,
        "hotel_name": hotel,
        "tagline": tagline,
        "social": {"instagram": ig, "facebook": fb},
        "products": products,
        "rooms": rooms,
        "product_count": len(products),
        "room_count": len(rooms),
    }
