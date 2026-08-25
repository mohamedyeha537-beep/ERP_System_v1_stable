"""حقول SEO على الكيانات + قراءة/كتابة آمنة للتطبيق والـ rollback."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

SEO_FIELDS = (
    "seo_title",
    "seo_description",
    "seo_h1",
    "seo_slug",
    "seo_keywords",
    "seo_schema_json",
    "seo_og_title",
    "seo_og_description",
    "seo_og_image",
    "seo_indexable",
    "seo_canonical_url",
    "seo_updated_at",
)

# fix_type → اسم الحقل على الكيان
FIX_FIELD_MAP = {
    "meta_title": "seo_title",
    "seo_title": "seo_title",
    "meta_description": "seo_description",
    "seo_description": "seo_description",
    "h1": "seo_h1",
    "seo_h1": "seo_h1",
    "image_alt": "seo_og_image",  # placeholder — alt يُخزَّن كنص مقترح إن لم يوجد حقل صورة
    "schema": "seo_schema_json",
    "seo_schema_json": "seo_schema_json",
    "og_title": "seo_og_title",
    "og_description": "seo_og_description",
    "og_image": "seo_og_image",
    "canonical": "seo_canonical_url",
    "seo_canonical_url": "seo_canonical_url",
    "keywords": "seo_keywords",
    "seo_keywords": "seo_keywords",
    "indexable": "seo_indexable",
}

# أنواع لا تُطبَّق تلقائياً (مراجعة فقط)
MANUAL_ONLY_FIX_TYPES = frozenset(
    {"internal_link", "content_rewrite", "content_expansion", "faq", "h2"}
)


def load_entity(db: Session, entity_type: str | None, entity_id: int | None) -> Any | None:
    if not entity_type or not entity_id:
        return None
    et = entity_type.strip().lower()
    if et in ("hotel_room", "room"):
        from modules.hotel.models import HotelRoom

        return db.get(HotelRoom, int(entity_id))
    if et in ("product", "menu_item"):
        from modules.catalog.models import Product

        return db.get(Product, int(entity_id))
    if et in ("product_category", "category"):
        from modules.catalog.models import ProductCategory

        return db.get(ProductCategory, int(entity_id))
    if et in ("hotel_service", "hotel_service_catalog", "service"):
        from modules.hotel.booking_models import HotelServiceCatalog

        return db.get(HotelServiceCatalog, int(entity_id))
    return None


def read_seo_snapshot(entity: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in SEO_FIELDS:
        if hasattr(entity, f):
            val = getattr(entity, f)
            if isinstance(val, datetime):
                out[f] = val.isoformat()
            else:
                out[f] = val
    return out


def apply_field_to_entity(entity: Any, field: str, value: Any) -> None:
    if not hasattr(entity, field):
        raise ValueError(f"الكيان لا يدعم الحقل {field}.")
    if field == "seo_indexable":
        if isinstance(value, str):
            setattr(entity, field, value.strip().lower() in ("1", "true", "yes", "on"))
        else:
            setattr(entity, field, bool(value))
    elif field == "seo_updated_at":
        setattr(entity, field, datetime.now(timezone.utc))
    else:
        setattr(entity, field, None if value is None else str(value))
        if hasattr(entity, "seo_updated_at"):
            entity.seo_updated_at = datetime.now(timezone.utc)


def restore_snapshot(entity: Any, snapshot: dict[str, Any]) -> None:
    for f, val in (snapshot or {}).items():
        if f not in SEO_FIELDS or not hasattr(entity, f):
            continue
        if f == "seo_updated_at":
            if val:
                try:
                    setattr(entity, f, datetime.fromisoformat(str(val)))
                except ValueError:
                    setattr(entity, f, datetime.now(timezone.utc))
            else:
                setattr(entity, f, None)
        elif f == "seo_indexable":
            setattr(entity, f, bool(val) if val is not None else True)
        else:
            setattr(entity, f, val)
