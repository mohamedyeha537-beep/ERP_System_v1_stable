"""تنسيق عمل وكلاء غرفة التسويق كفريق واحد."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.marketing_room.agents import AGENT_BY_ROLE, AGENT_CARDS
from modules.marketing_room.models import MarketingArtifact, MarketingRun
from modules.marketing_room.service import MarketingRoomError
from modules.settings.service import get_setting, set_setting

TEAM_ROLES = (
    "chief",
    "website_manager",
    "website_seller",
    "content_manager",
    "hashtag",
    "design",
    "campaign_manager",
    "delivery",
)

ROLE_AR = {a.role: a.title_ar for a in AGENT_CARDS}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_meta(art: MarketingArtifact) -> dict[str, Any]:
    if not art.meta_json:
        return {}
    try:
        data = json.loads(art.meta_json)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_meta(art: MarketingArtifact, meta: dict[str, Any]) -> None:
    art.meta_json = json.dumps(meta, ensure_ascii=False)


def set_active_plan_run(db: Session, run_id: int) -> None:
    set_setting(db, "marketing_active_plan_run_id", str(int(run_id)))


def get_active_plan_run_id(db: Session) -> int | None:
    raw = (get_setting(db, "marketing_active_plan_run_id", "") or "").strip()
    if raw.isdigit():
        return int(raw)
    # سقوط: آخر خطة محتوى
    art = db.scalar(
        select(MarketingArtifact)
        .where(MarketingArtifact.kind == "content_plan")
        .order_by(MarketingArtifact.id.desc())
        .limit(1)
    )
    return int(art.run_id) if art else None


def get_active_plan_artifact(db: Session) -> MarketingArtifact | None:
    run_id = get_active_plan_run_id(db)
    if not run_id:
        return None
    return db.scalar(
        select(MarketingArtifact)
        .where(
            MarketingArtifact.run_id == run_id,
            MarketingArtifact.kind == "content_plan",
        )
        .order_by(MarketingArtifact.id.desc())
        .limit(1)
    )


def parse_plan_payload(art: MarketingArtifact | None) -> dict[str, Any]:
    if art is None or not (art.body_text or "").strip():
        return {}
    try:
        data = json.loads(art.body_text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def publish_team_plan_from_run(
    db: Session,
    run: MarketingRun,
    plan_data: dict[str, Any],
    *,
    focus: str = "",
) -> list[MarketingArtifact]:
    """بعد إنشاء الخطة: يفعّلها للفريق ويوزّع مهام على الوكلاء."""
    set_active_plan_run(db, int(run.id))
    created: list[MarketingArtifact] = []
    days = list(plan_data.get("days") or [])[:7]
    for d in days:
        posts = list(d.get("posts") or [])[:2]
        for p in posts:
            topic = str(p.get("title") or d.get("theme") or "منشور من الخطة").strip()
            platform = str(p.get("platform") or "instagram")
            day = d.get("day")
            theme = str(d.get("theme") or "")
            angle = str(p.get("angle") or "")
            cta = str(p.get("cta") or "")
            base_meta = {
                "from_role": "content_manager",
                "plan_run_id": run.id,
                "day": day,
                "theme": theme,
                "topic": topic,
                "platform": platform,
                "angle": angle,
                "cta": cta,
                "focus": focus,
                "task_status": "open",
            }
            for target, kind_hint in (
                ("design", "إنشاء منشور + صورة"),
                ("hashtag", "هاشتاجات للمنشور"),
                ("website_seller", "زاوية بيع داعمة"),
            ):
                art = MarketingArtifact(
                    run_id=run.id,
                    agent_role="chief",
                    kind="team_task",
                    status="pending_approval",
                    title=f"مهمة فريق ← {ROLE_AR.get(target, target)}: {topic}"[:240],
                    body_text=(
                        f"من خطة المحتوى (اليوم {day}): {theme}\n"
                        f"المطلوب: {kind_hint}\n"
                        f"الموضوع: {topic}\n"
                        f"الزاوية: {angle}\n"
                        f"CTA: {cta}\n"
                        f"المنصة: {platform}"
                    ),
                    meta_json=json.dumps(
                        {**base_meta, "target_role": target, "hint": kind_hint},
                        ensure_ascii=False,
                    ),
                )
                db.add(art)
                created.append(art)
    # ملخص للوكيل الرئيسي
    db.add(
        MarketingArtifact(
            run_id=run.id,
            agent_role="chief",
            kind="team_brief",
            status="pending_approval",
            title="إحاطة الفريق — خطة نشطة",
            body_text=(
                f"تم تفعيل خطة «{plan_data.get('plan_title') or run.title}» للفريق.\n"
                f"وُزّعت {len(created)} مهمة على الديزاين والهاشتاج والبياع.\n"
                "أي وكيل يمكنه طلب تعديل من آخر عند الحاجة."
            ),
            meta_json=json.dumps(
                {"plan_run_id": run.id, "tasks": len(created)}, ensure_ascii=False
            ),
        )
    )
    run.chief_summary = (
        (run.chief_summary or "")
        + f"\n· فُعّلت الخطة للفريق ووُزّعت {len(created)} مهمة مشتركة."
    ).strip()
    db.flush()
    return created


def list_open_tasks_for_role(
    db: Session, role: str, *, limit: int = 30
) -> list[MarketingArtifact]:
    rows = list(
        db.scalars(
            select(MarketingArtifact)
            .where(MarketingArtifact.kind == "team_task")
            .order_by(MarketingArtifact.id.desc())
            .limit(120)
        ).all()
    )
    out: list[MarketingArtifact] = []
    for a in rows:
        meta = _parse_meta(a)
        if meta.get("target_role") != role:
            continue
        if meta.get("task_status", "open") != "open":
            continue
        out.append(a)
        if len(out) >= limit:
            break
    return out


def list_open_requests_for_role(
    db: Session, role: str, *, limit: int = 20
) -> list[MarketingArtifact]:
    rows = list(
        db.scalars(
            select(MarketingArtifact)
            .where(MarketingArtifact.kind == "team_request")
            .order_by(MarketingArtifact.id.desc())
            .limit(100)
        ).all()
    )
    out: list[MarketingArtifact] = []
    for a in rows:
        meta = _parse_meta(a)
        if meta.get("to_role") != role:
            continue
        if a.status in ("rejected",) or meta.get("request_status") == "done":
            continue
        if a.status == "approved" and meta.get("request_status") == "done":
            continue
        out.append(a)
        if len(out) >= limit:
            break
    return out


def _infer_request_action(message: str, to_role: str) -> str:
    """يستنتج إجراء التنفيذ من نص الطلب والدور المستهدف."""
    msg = (message or "").strip().lower()
    wants_image = any(k in msg for k in ("صورة", "صور", "image", "img", "ديزاين بصري"))
    wants_text = any(k in msg for k in ("نص", "كتابة", "كابشن", "منشور", "text"))
    if to_role == "design":
        if wants_image and not wants_text:
            return "regen_image"
        if wants_text and not wants_image:
            return "regen_text"
        if wants_image:
            return "regen_image"
        return "regen_text"
    if to_role == "hashtag":
        return "regen_hashtags"
    if to_role in ("content_manager", "website_seller", "website_manager"):
        return "regen_text" if to_role == "content_manager" else "manual"
    return "manual"


def _find_target_post(
    db: Session,
    *,
    related_artifact_id: int | None,
    message: str,
    prefer_image: bool,
) -> MarketingArtifact | None:
    if related_artifact_id:
        rel = db.get(MarketingArtifact, int(related_artifact_id))
        if rel and rel.kind in ("post", "post_with_image", "sale_idea", "design_brief"):
            return rel
        # إن كان الطلب نفسه أو مهمة — ابحث عن منشور مرتبط
        if rel:
            meta = _parse_meta(rel)
            rid = meta.get("result_artifact_id") or meta.get("related_artifact_id")
            if rid:
                found = db.get(MarketingArtifact, int(rid))
                if found:
                    return found

    q = (
        select(MarketingArtifact)
        .where(MarketingArtifact.kind.in_(("post_with_image", "post")))
        .order_by(MarketingArtifact.id.desc())
        .limit(40)
    )
    rows = list(db.scalars(q).all())
    msg = (message or "").strip()
    # فضّل منشور فيه كلمة من الرسالة (مثل برجر)
    keywords = [w for w in msg.replace("،", " ").split() if len(w) >= 3]
    scored: list[tuple[int, MarketingArtifact]] = []
    for a in rows:
        score = 0
        blob = f"{a.title or ''} {a.body_text or ''}".lower()
        for w in keywords:
            if w.lower() in blob:
                score += 3
        if prefer_image and (a.media_path or a.kind == "post_with_image"):
            score += 2
        if a.agent_role == "design":
            score += 1
        scored.append((score, a))
    scored.sort(key=lambda x: (x[0], x[1].id), reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    # آخر منشور ديزاين بصورة
    for a in rows:
        if a.agent_role == "design" and (a.media_path or a.kind == "post_with_image"):
            return a
    return rows[0] if rows else None


def create_team_request(
    db: Session,
    *,
    user_id: int | None,
    from_role: str,
    to_role: str,
    message: str,
    related_artifact_id: int | None = None,
    auto_execute: bool = True,
) -> MarketingArtifact:
    if from_role not in TEAM_ROLES or to_role not in TEAM_ROLES:
        raise MarketingRoomError("دور الوكيل غير صالح.")
    if from_role == to_role:
        raise MarketingRoomError("اختر وكيلاً آخر لطلب التعديل.")
    msg = (message or "").strip()
    if not msg:
        raise MarketingRoomError("اكتب طلب التعديل.")
    plan_run_id = get_active_plan_run_id(db)
    action = _infer_request_action(msg, to_role)
    target = _find_target_post(
        db,
        related_artifact_id=related_artifact_id,
        message=msg,
        prefer_image=(action == "regen_image"),
    )
    run = None
    if target:
        run = db.get(MarketingRun, target.run_id)
    if run is None and related_artifact_id:
        rel = db.get(MarketingArtifact, int(related_artifact_id))
        if rel:
            run = db.get(MarketingRun, rel.run_id)
    if run is None and plan_run_id:
        run = db.get(MarketingRun, plan_run_id)
    if run is None:
        run = MarketingRun(
            business_domain="shared",
            status="awaiting_approval",
            title=f"طلب فريق — {ROLE_AR.get(from_role, from_role)} → {ROLE_AR.get(to_role, to_role)}",
            triggered_by_id=user_id,
        )
        db.add(run)
        db.flush()

    art = MarketingArtifact(
        run_id=run.id,
        agent_role=from_role,
        kind="team_request",
        status="pending_approval",
        title=(
            f"طلب تنفيذ ← {ROLE_AR.get(to_role, to_role)} "
            f"(من {ROLE_AR.get(from_role, from_role)})"
        )[:240],
        body_text=msg,
        meta_json=json.dumps(
            {
                "from_role": from_role,
                "to_role": to_role,
                "related_artifact_id": related_artifact_id or (target.id if target else None),
                "target_artifact_id": target.id if target else None,
                "plan_run_id": plan_run_id,
                "request_status": "open",
                "inferred_action": action,
                "created_by": user_id,
            },
            ensure_ascii=False,
        ),
    )
    db.add(art)
    db.flush()
    run.status = "awaiting_approval"
    run.chief_summary = (
        (run.chief_summary or "")
        + f"\n· طلب فريق للتنفيذ: {ROLE_AR.get(from_role)} → {ROLE_AR.get(to_role)}: {msg[:80]}"
    ).strip()
    db.flush()

    if auto_execute and action != "manual":
        try:
            execute_team_request(db, art.id, user_id=user_id)
        except MarketingRoomError as exc:
            meta = _parse_meta(art)
            meta["execution_error"] = str(exc)[:500]
            meta["request_status"] = "needs_retry"
            _save_meta(art, meta)
            db.flush()
    return art


def execute_team_request(
    db: Session,
    request_id: int,
    *,
    user_id: int | None = None,
) -> MarketingArtifact:
    """ينفّذ طلب الفريق فعلياً (إعادة صورة/نص/هاشتاج) وليس مجرد رسالة."""
    from modules.marketing_room.agent_workspace import (
        regenerate_artifact_image,
        regenerate_artifact_text,
        run_hashtags,
    )

    art = db.get(MarketingArtifact, int(request_id))
    if art is None or art.kind != "team_request":
        raise MarketingRoomError("طلب الفريق غير موجود.")
    meta = _parse_meta(art)
    if meta.get("request_status") == "done":
        return art

    to_role = str(meta.get("to_role") or "")
    action = str(meta.get("inferred_action") or _infer_request_action(art.body_text or "", to_role))
    target_id = meta.get("target_artifact_id") or meta.get("related_artifact_id")
    target = db.get(MarketingArtifact, int(target_id)) if target_id else None
    if target is None:
        target = _find_target_post(
            db,
            related_artifact_id=None,
            message=art.body_text or "",
            prefer_image=(action == "regen_image"),
        )
    if target is None and action in ("regen_image", "regen_text"):
        raise MarketingRoomError(
            "لا يوجد منشور مستهدف للتنفيذ — أنشئ منشوراً أولاً أو اربط الطلب بمنشور."
        )

    result_note = ""
    result_artifact_id = None
    if action == "regen_image" and target:
        # مرّر تعليمات الطلب كموضوع إن أمكن
        tmeta = _parse_meta(target)
        if art.body_text:
            tmeta["topic"] = (
                (tmeta.get("topic") or target.title or "") + " — " + (art.body_text or "")[:120]
            ).strip(" —")
            _save_meta(target, tmeta)
        updated = regenerate_artifact_image(db, int(target.id))
        result_artifact_id = updated.id
        result_note = f"أُعيد توليد الصورة للمنشور #{updated.id}"
        # انسخ الصورة إلى بطاقة الطلب ليظهر الناتج فوراً
        art.media_path = updated.media_path
    elif action == "regen_text" and target:
        updated = regenerate_artifact_text(
            db,
            int(target.id),
            instruction=art.body_text or "",
            content_focus=None,
        )
        result_artifact_id = updated.id
        result_note = f"أُعيد توليد النص للمنشور #{updated.id}"
        art.body_text = (
            f"{art.body_text}\n\n—\nالنتيجة بعد التنفيذ:\n{updated.body_text}"
        )[:8000]
    elif action == "regen_hashtags":
        topic = (art.body_text or "").strip() or (target.title if target else "منشور")
        run = run_hashtags(db, user_id=user_id, topic=topic)
        tag_arts = [
            a
            for a in db.scalars(
                select(MarketingArtifact).where(MarketingArtifact.run_id == run.id)
            ).all()
            if a.kind == "hashtags"
        ]
        if tag_arts:
            result_artifact_id = tag_arts[0].id
            result_note = f"هاشتاجات جديدة #{tag_arts[0].id}: {tag_arts[0].body_text[:200]}"
            art.body_text = f"{art.body_text}\n\n—\n{tag_arts[0].body_text}"[:8000]
    else:
        raise MarketingRoomError(
            "هذا الطلب يحتاج تنفيذاً يدوياً من مساحة عمل الوكيل المستهدف."
        )

    meta["request_status"] = "done"
    meta["executed_action"] = action
    meta["result_artifact_id"] = result_artifact_id
    meta["execution_note"] = result_note
    meta["completed_at"] = _utcnow().isoformat()
    meta.pop("execution_error", None)
    _save_meta(art, meta)
    art.status = "approved"
    art.title = f"✓ نُفّذ: {ROLE_AR.get(to_role, to_role)} — {action}"[:240]
    run = db.get(MarketingRun, art.run_id)
    if run:
        run.chief_summary = ((run.chief_summary or "") + f"\n· {result_note}").strip()
        run.status = "awaiting_approval"
    db.flush()
    return art


def complete_team_request(db: Session, request_id: int, *, note: str = "") -> MarketingArtifact:
    art = db.get(MarketingArtifact, int(request_id))
    if art is None or art.kind not in ("team_request", "team_request_inbox"):
        raise MarketingRoomError("طلب الفريق غير موجود.")
    meta = _parse_meta(art)
    parent_id = meta.get("parent_request_id") or art.id
    parent = db.get(MarketingArtifact, int(parent_id)) or art
    pmeta = _parse_meta(parent)
    pmeta["request_status"] = "done"
    pmeta["completed_at"] = _utcnow().isoformat()
    if note.strip():
        pmeta["completion_note"] = note.strip()[:500]
    _save_meta(parent, pmeta)
    parent.status = "approved"
    # حدّث النسخ المرتبطة
    siblings = list(
        db.scalars(
            select(MarketingArtifact).where(
                MarketingArtifact.run_id == parent.run_id,
                MarketingArtifact.kind.in_(("team_request", "team_request_inbox")),
            )
        ).all()
    )
    for s in siblings:
        sm = _parse_meta(s)
        if s.id == parent.id or sm.get("parent_request_id") == parent.id:
            sm["request_status"] = "done"
            _save_meta(s, sm)
            s.status = "approved"
    db.flush()
    return parent


def complete_team_task(db: Session, task_id: int, *, result_artifact_id: int | None = None) -> None:
    art = db.get(MarketingArtifact, int(task_id))
    if art is None or art.kind != "team_task":
        return
    meta = _parse_meta(art)
    meta["task_status"] = "done"
    meta["completed_at"] = _utcnow().isoformat()
    if result_artifact_id:
        meta["result_artifact_id"] = result_artifact_id
    _save_meta(art, meta)
    art.status = "approved"
    db.flush()


def get_task(db: Session, task_id: int) -> MarketingArtifact | None:
    art = db.get(MarketingArtifact, int(task_id))
    if art and art.kind == "team_task":
        return art
    return None


def team_board(db: Session, *, for_role: str | None = None) -> dict[str, Any]:
    plan = get_active_plan_artifact(db)
    plan_data = parse_plan_payload(plan)
    plan_run_id = get_active_plan_run_id(db)
    themes: list[str] = []
    for d in list(plan_data.get("days") or [])[:5]:
        t = str(d.get("theme") or "").strip()
        if t:
            themes.append(f"يوم {d.get('day')}: {t}")
    tasks = list_open_tasks_for_role(db, for_role, limit=20) if for_role else []
    requests_in = list_open_requests_for_role(db, for_role, limit=15) if for_role else []
    peers = [
        {"role": a.role, "title_ar": a.title_ar}
        for a in AGENT_CARDS
        if a.role in TEAM_ROLES and a.role != for_role and a.role != "customer_service"
    ]
    return {
        "has_plan": plan is not None,
        "plan_run_id": plan_run_id,
        "plan_title": (plan.title if plan else None)
        or plan_data.get("plan_title")
        or None,
        "themes": themes,
        "open_tasks": tasks,
        "open_requests": requests_in,
        "peers": peers,
        "role_ar": ROLE_AR.get(for_role or "", for_role or ""),
    }


def build_team_prompt_context(db: Session) -> dict[str, Any]:
    """سياق يُمرَّر لتوليد النصوص حتى يتبع الوكلاء الخطة النشطة."""
    plan = get_active_plan_artifact(db)
    data = parse_plan_payload(plan)
    directives: list[str] = []
    recent_dir = list(
        db.scalars(
            select(MarketingArtifact)
            .where(MarketingArtifact.kind == "campaign_directive")
            .order_by(MarketingArtifact.id.desc())
            .limit(6)
        ).all()
    )
    for d in recent_dir:
        directives.append(f"{d.title}: {(d.body_text or '')[:220]}")
    return {
        "active_plan_title": (plan.title if plan else None) or data.get("plan_title"),
        "plan_run_id": plan.run_id if plan else None,
        "plan_days": list(data.get("days") or [])[:7],
        "campaign_directives": directives,
    }
