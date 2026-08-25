"""جمع الصفحات العامة القابلة للفحص من وكلاء SEO."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.seo.config import seo_config
from modules.settings.service import get_public_base_url
from modules.web_marketing.service import (
    SURFACE_HOTEL_PORTAL,
    SURFACE_RESTAURANT_SHOP,
    get_surface_admin_config,
)


def _abs(base: str, path: str) -> str:
    p = (path or "/").strip() or "/"
    if not p.startswith("/"):
        p = "/" + p
    if base.startswith("http"):
        return f"{base.rstrip('/')}{p}"
    return p


def _item(
    *,
    env: str,
    page_type: str,
    entity_type: str | None,
    entity_id: int | None,
    url: str,
    title: str | None = None,
    meta_description: str | None = None,
    h1: str | None = None,
    last_modified_at: datetime | None = None,
    language: str = "ar",
) -> dict[str, Any]:
    return {
        "id": f"{page_type}:{entity_type or 'static'}:{entity_id or 0}:{url}",
        "environment": env,
        "url": url,
        "page_type": page_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "last_modified_at": (last_modified_at or datetime.now(timezone.utc)).isoformat(),
        "title": title,
        "meta_description": meta_description,
        "h1": h1,
        "language": language,
    }


def list_public_pages(db: Session) -> list[dict[str, Any]]:
    cfg = seo_config()
    env = cfg["environment"]
    base = (cfg.get("public_base_url") or "").strip() or get_public_base_url(db)
    shop = get_surface_admin_config(db, SURFACE_RESTAURANT_SHOP)
    hotel = get_surface_admin_config(db, SURFACE_HOTEL_PORTAL)
    out: list[dict[str, Any]] = []

    if not (shop.get("robots") or "").startswith("noindex"):
        out.append(
            _item(
                env=env,
                page_type="restaurant_home",
                entity_type="surface",
                entity_id=None,
                url=_abs(base, "/shop"),
                title=shop.get("meta_title") or "المطعم",
                meta_description=shop.get("meta_description"),
                h1=shop.get("meta_title"),
            )
        )
        out.append(
            _item(
                env=env,
                page_type="offers",
                entity_type="surface",
                entity_id=None,
                url=_abs(base, "/offers"),
                title="العروض",
                h1="العروض",
            )
        )
        out.append(
            _item(
                env=env,
                page_type="about",
                entity_type="surface",
                entity_id=None,
                url=_abs(base, "/about"),
                title="من نحن",
                h1="من نحن",
            )
        )
        try:
            from modules.catalog.models import Product, ProductCategory

            cats = db.scalars(
                select(ProductCategory).where(ProductCategory.show_in_shop.is_(True))
            ).all()
            for c in cats:
                title = getattr(c, "seo_title", None) or c.name_ar
                out.append(
                    _item(
                        env=env,
                        page_type="category",
                        entity_type="product_category",
                        entity_id=c.id,
                        url=_abs(base, f"/shop?category={c.id}"),
                        title=title,
                        meta_description=getattr(c, "seo_description", None),
                        h1=getattr(c, "seo_h1", None) or c.name_ar,
                    )
                )
            products = db.scalars(
                select(Product).where(
                    Product.is_active.is_(True),
                    Product.show_in_shop.is_(True),
                )
            ).all()
            for p in products:
                title = getattr(p, "seo_title", None) or p.name_ar
                out.append(
                    _item(
                        env=env,
                        page_type="product",
                        entity_type="product",
                        entity_id=p.id,
                        url=_abs(base, f"/shop?product={p.id}"),
                        title=title,
                        meta_description=getattr(p, "seo_description", None),
                        h1=getattr(p, "seo_h1", None) or p.name_ar,
                    )
                )
        except Exception:  # noqa: BLE001
            pass

    if not (hotel.get("robots") or "").startswith("noindex"):
        out.append(
            _item(
                env=env,
                page_type="hotel_home",
                entity_type="surface",
                entity_id=None,
                url=_abs(base, "/suites"),
                title=hotel.get("meta_title") or "الفندق",
                meta_description=hotel.get("meta_description"),
                h1=hotel.get("meta_title"),
            )
        )
        try:
            from modules.hotel.store_service import list_store_rooms, store_enabled

            if store_enabled(db):
                for card in list_store_rooms(db):
                    room = getattr(card, "room", None)
                    if room is None:
                        continue
                    title = getattr(room, "seo_title", None) or room.name_ar or room.number
                    out.append(
                        _item(
                            env=env,
                            page_type="hotel_room",
                            entity_type="hotel_room",
                            entity_id=room.id,
                            url=_abs(base, f"/suites/room/{int(room.id)}"),
                            title=title,
                            meta_description=getattr(room, "seo_description", None)
                            or room.online_description,
                            h1=getattr(room, "seo_h1", None) or title,
                            last_modified_at=getattr(room, "updated_at", None),
                        )
                    )
        except Exception:  # noqa: BLE001
            pass

    return out


def get_public_page_detail(db: Session, page_key: str) -> dict[str, Any] | None:
    """page_key من list_public_pages أو معرف رقمي لـ seo_pages."""
    pages = list_public_pages(db)
    for p in pages:
        if str(p["id"]) == str(page_key):
            entity = None
            if p.get("entity_type") and p.get("entity_id"):
                from modules.seo.entity_seo import load_entity, read_seo_snapshot

                ent = load_entity(db, p["entity_type"], int(p["entity_id"]))
                if ent is not None:
                    entity = {
                        "type": p["entity_type"],
                        "id": p["entity_id"],
                        "seo": read_seo_snapshot(ent),
                    }
            return {**p, "source_entity": entity}
    # fallback: seo_pages.id
    try:
        pid = int(page_key)
    except (TypeError, ValueError):
        return None
    from modules.seo.models import SeoPage

    row = db.get(SeoPage, pid)
    if not row:
        return None
    entity = None
    if row.entity_type and row.entity_id:
        from modules.seo.entity_seo import load_entity, read_seo_snapshot

        ent = load_entity(db, row.entity_type, int(row.entity_id))
        if ent is not None:
            entity = {
                "type": row.entity_type,
                "id": row.entity_id,
                "seo": read_seo_snapshot(ent),
            }
    return {
        "id": row.id,
        "environment": row.environment,
        "url": row.url,
        "page_type": row.page_type,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "title": row.title,
        "meta_description": row.meta_description,
        "h1": row.h1,
        "language": row.language,
        "source_entity": entity,
    }
