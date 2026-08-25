"""تطبيق / Rollback لإصلاحات SEO مع لقطات قبل التغيير."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.seo.config import is_production, seo_config
from modules.seo.entity_seo import (
    FIX_FIELD_MAP,
    MANUAL_ONLY_FIX_TYPES,
    apply_field_to_entity,
    load_entity,
    read_seo_snapshot,
    restore_snapshot,
)
from modules.seo.logging_service import log_seo_action
from modules.seo.models import SeoFixRequest, SeoPage, SeoSuggestion


class SeoApplyError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def ensure_fix_from_suggestion(
    db: Session,
    suggestion: SeoSuggestion,
) -> SeoFixRequest:
    """إنشاء طلب إصلاح من اقتراح إن لم يوجد."""
    existing = db.scalars(
        select(SeoFixRequest)
        .where(SeoFixRequest.suggestion_id == suggestion.id)
        .order_by(SeoFixRequest.id.desc())
    ).first()
    if existing:
        return existing
    payload = {
        "value": suggestion.suggested_value,
        "suggestion_type": suggestion.suggestion_type,
    }
    fix = SeoFixRequest(
        environment=suggestion.environment,
        page_id=suggestion.page_id,
        suggestion_id=suggestion.id,
        fix_type=suggestion.suggestion_type,
        payload_json=json.dumps(payload, ensure_ascii=False),
        status="pending",
    )
    db.add(fix)
    db.flush()
    return fix


def approve_fix(db: Session, fix: SeoFixRequest, user_id: int) -> SeoFixRequest:
    if fix.status in ("applied", "rolled_back"):
        raise SeoApplyError("لا يمكن الموافقة على إصلاح مطبّق أو متراجع عنه.")
    fix.status = "approved"
    fix.approved_by_id = user_id
    fix.approved_at = datetime.now(timezone.utc)
    log_seo_action(
        db,
        environment=fix.environment,
        agent_name="admin",
        action="approve_fix",
        status="ok",
        message=f"fix_id={fix.id}",
        payload={"approved_by_id": user_id},
    )
    return fix


def approve_fix_for_production(
    db: Session, fix: SeoFixRequest, user_id: int
) -> SeoFixRequest:
    cfg = seo_config()
    if cfg["environment"] != "production" and not cfg["require_production_approval"]:
        # ما زال مسموحاً وضع العلم حتى في staging للاختبار
        pass
    if fix.status not in ("approved", "pending"):
        raise SeoApplyError("يجب أن يكون الإصلاح معلّقاً أو معتمداً أولاً.")
    if fix.status == "pending":
        fix.status = "approved"
        fix.approved_by_id = user_id
        fix.approved_at = datetime.now(timezone.utc)
    fix.approved_for_production = True
    log_seo_action(
        db,
        environment=fix.environment,
        agent_name="admin",
        action="approve_production",
        status="ok",
        message=f"fix_id={fix.id}",
        payload={"approved_by_id": user_id},
    )
    return fix


def _resolve_entity(db: Session, fix: SeoFixRequest):
    page = db.get(SeoPage, fix.page_id) if fix.page_id else None
    entity_type = page.entity_type if page else None
    entity_id = page.entity_id if page else None
    payload = _parse_json(fix.payload_json)
    entity_type = payload.get("entity_type") or entity_type
    entity_id = payload.get("entity_id") or entity_id
    if entity_id is not None:
        try:
            entity_id = int(entity_id)
        except (TypeError, ValueError):
            entity_id = None
    return load_entity(db, entity_type, entity_id), entity_type, entity_id, payload


def apply_fix(db: Session, fix: SeoFixRequest, user_id: int | None) -> SeoFixRequest:
    cfg = seo_config()
    env = cfg["environment"]
    if fix.environment != env:
        raise SeoApplyError(
            f"بيئة الإصلاح ({fix.environment}) لا تطابق بيئة السيرفر ({env}).",
            403,
        )
    if fix.status == "applied":
        # idempotent
        return fix
    if fix.fix_type in MANUAL_ONLY_FIX_TYPES:
        raise SeoApplyError(
            f"نوع الإصلاح '{fix.fix_type}' للمراجعة فقط ولا يُطبَّق تلقائياً."
        )
    if fix.status != "approved":
        raise SeoApplyError("يجب اعتماد الإصلاح قبل التطبيق.", 403)
    if is_production() or env == "production":
        if cfg.get("allow_auto_apply"):
            raise SeoApplyError("auto-apply ممنوع في production.", 403)
        if cfg.get("require_production_approval", True) and not fix.approved_for_production:
            raise SeoApplyError(
                "تطبيق production يتطلب approved_for_production=true.",
                403,
            )

    entity, entity_type, entity_id, payload = _resolve_entity(db, fix)
    if entity is None:
        raise SeoApplyError("لم يُعثر على الكيان المرتبط بالتعديل.")

    field = FIX_FIELD_MAP.get(fix.fix_type)
    if not field:
        raise SeoApplyError(f"نوع إصلاح غير مدعوم للتطبيق: {fix.fix_type}")

    new_value = payload.get("value")
    if new_value is None:
        new_value = payload.get("suggested_value")
    if new_value is None:
        raise SeoApplyError("قيمة التطبيق مفقودة في payload.")

    # idempotent: إن كانت القيمة الحالية مطابقة — علّم مطبّقاً دون تغيير
    snapshot = read_seo_snapshot(entity)
    current = snapshot.get(field)
    if str(current or "") == str(new_value or "") and fix.rollback_json:
        fix.status = "applied"
        fix.applied_by_id = user_id
        fix.applied_at = datetime.now(timezone.utc)
        return fix

    fix.rollback_json = json.dumps(
        {"entity_type": entity_type, "entity_id": entity_id, "seo": snapshot},
        ensure_ascii=False,
        default=str,
    )
    try:
        apply_field_to_entity(entity, field, new_value)
        fix.status = "applied"
        fix.applied_by_id = user_id
        fix.applied_at = datetime.now(timezone.utc)
        fix.error_message = None
        if fix.suggestion_id:
            sug = db.get(SeoSuggestion, fix.suggestion_id)
            if sug:
                sug.status = "applied"
        log_seo_action(
            db,
            environment=fix.environment,
            agent_name="admin",
            action="apply_fix",
            status="ok",
            message=f"fix_id={fix.id} field={field}",
            payload={"entity_type": entity_type, "entity_id": entity_id, "field": field},
        )
    except Exception as exc:  # noqa: BLE001
        fix.error_message = str(exc)[:500]
        fix.status = "failed"
        log_seo_action(
            db,
            environment=fix.environment,
            agent_name="admin",
            action="apply_fix",
            status="error",
            message=str(exc)[:300],
        )
        raise SeoApplyError(f"فشل التطبيق: {exc}") from exc
    return fix


def rollback_fix(db: Session, fix: SeoFixRequest, user_id: int | None) -> SeoFixRequest:
    if fix.status != "applied":
        raise SeoApplyError("يمكن التراجع فقط عن إصلاح مطبّق.")
    rb = _parse_json(fix.rollback_json)
    seo_snap = rb.get("seo") or {}
    entity_type = rb.get("entity_type")
    entity_id = rb.get("entity_id")
    entity = load_entity(db, entity_type, entity_id)
    if entity is None:
        raise SeoApplyError("الكيان غير موجود للتراجع.")
    restore_snapshot(entity, seo_snap)
    fix.status = "rolled_back"
    fix.rolled_back_by_id = user_id
    fix.rolled_back_at = datetime.now(timezone.utc)
    if fix.suggestion_id:
        sug = db.get(SeoSuggestion, fix.suggestion_id)
        if sug:
            sug.status = "rolled_back"
    log_seo_action(
        db,
        environment=fix.environment,
        agent_name="admin",
        action="rollback_fix",
        status="ok",
        message=f"fix_id={fix.id}",
        payload={"entity_type": entity_type, "entity_id": entity_id},
    )
    return fix
