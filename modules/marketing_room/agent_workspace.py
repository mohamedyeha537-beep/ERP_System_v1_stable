"""مساحات عمل الوكلاء: خطة محتوى، نصوص، صور."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.marketing_room.ai_client import (
    chat_json,
    chat_text,
    generate_image_file,
    generate_video_file,
)
from modules.marketing_room.agents import AGENT_BY_ROLE
from modules.marketing_room.config import (
    marketing_ai_settings,
    marketing_higgsfield_settings,
    marketing_image_settings,
)
from modules.marketing_room.context_service import build_marketing_context
from modules.marketing_room.image_prompt import build_marketing_image_prompt
from modules.marketing_room.models import MarketingArtifact, MarketingRun
from modules.marketing_room.service import MarketingRoomError

STATIC_MARKETING = Path(__file__).resolve().parents[2] / "app" / "static" / "uploads" / "marketing"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_run(db: Session, *, user_id: int | None, role: str, title: str) -> MarketingRun:
    run = MarketingRun(
        business_domain="shared",
        status="awaiting_approval",
        title=title[:200],
        triggered_by_id=user_id,
        context_json=json.dumps(
            build_marketing_context(db, business_domain="shared"), ensure_ascii=False
        ),
        finished_at=_utcnow(),
    )
    db.add(run)
    db.flush()
    return run


def _add_art(
    db: Session,
    run: MarketingRun,
    *,
    role: str,
    kind: str,
    title: str,
    body: str,
    meta: dict | None = None,
    media_path: str | None = None,
) -> MarketingArtifact:
    art = MarketingArtifact(
        run_id=run.id,
        agent_role=role,
        kind=kind,
        status="pending_approval",
        title=title[:240],
        body_text=body or "",
        meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
        media_path=(media_path or None),
    )
    db.add(art)
    db.flush()
    return art


def list_agent_artifacts(db: Session, role: str, *, limit: int = 40) -> list[MarketingArtifact]:
    return list(
        db.scalars(
            select(MarketingArtifact)
            .where(MarketingArtifact.agent_role == role)
            .order_by(MarketingArtifact.id.desc())
            .limit(limit)
        ).all()
    )


def agent_capabilities(db: Session, role: str) -> dict[str, Any]:
    from modules.marketing_room.campaign_manager import meta_connection_status

    ai = marketing_ai_settings(db)
    img = marketing_image_settings(db)
    card = AGENT_BY_ROLE.get(role)
    meta = meta_connection_status(db)
    return {
        "role": role,
        "title_ar": card.title_ar if card else role,
        "text_ai": bool(ai["api_key"]),
        "image_ai": bool(img["api_key"])
        or bool(marketing_higgsfield_settings(db).get("api_key")),
        "video_ai": bool(marketing_higgsfield_settings(db).get("api_key")),
        "has_workspace": role
        in (
            "content_manager",
            "design",
            "website_seller",
            "hashtag",
            "website_manager",
            "chief",
            "campaign_manager",
        ),
        "meta_ads": bool(meta.get("configured")),
        "meta": meta,
    }


def run_content_plan(
    db: Session,
    *,
    user_id: int | None,
    days: int = 7,
    focus: str = "",
) -> MarketingRun:
    """مدير المحتوى: خطة محتوى لعدة أيام."""
    days = max(3, min(14, int(days or 7)))
    ctx = build_marketing_context(db, business_domain="shared")
    focus_s = (focus or "").strip()
    data = chat_json(
        db,
        system=(
            "أنت مدير محتوى لمطعم وشقق فندقية في ليبيا. "
            "أرجع JSON فقط: {\"plan_title\":\"...\",\"days\":[{\"day\":1,\"theme\":\"...\","
            "\"posts\":[{\"platform\":\"instagram|facebook\",\"title\":\"...\",\"angle\":\"...\","
            "\"cta\":\"...\"}]}]} "
            f"بعدد أيام = {days}."
        ),
        user=json.dumps(
            {"focus": focus_s, "context": ctx, "days": days}, ensure_ascii=False
        )[:7000],
    )
    if not data:
        # قالب محلي
        products = [p.get("name_ar") for p in (ctx.get("products") or [])[:5] if p.get("name_ar")]
        days_list = []
        for i in range(1, days + 1):
            hero = products[(i - 1) % len(products)] if products else "عرض اليوم"
            days_list.append(
                {
                    "day": i,
                    "theme": f"يوم {i}: تسليط على {hero}",
                    "posts": [
                        {
                            "platform": "instagram",
                            "title": f"منشور {hero}",
                            "angle": "جذب بصري + دعوة للزيارة",
                            "cta": "احجز أو اطلب الآن",
                        },
                        {
                            "platform": "facebook",
                            "title": f"قصة ضيافة — {hero}",
                            "angle": "عائلات ونزلاء",
                            "cta": "راسلونا للحجز",
                        },
                    ],
                }
            )
        data = {
            "plan_title": f"خطة محتوى {days} أيام — {ctx.get('store_name')}",
            "days": days_list,
        }

    run = _new_run(
        db,
        user_id=user_id,
        role="content_manager",
        title=f"خطة محتوى — {data.get('plan_title') or days} أيام",
    )
    body = json.dumps(data, ensure_ascii=False, indent=2)
    _add_art(
        db,
        run,
        role="content_manager",
        kind="content_plan",
        title=str(data.get("plan_title") or "خطة محتوى")[:240],
        body=body,
        meta={"days": days, "focus": focus_s},
    )
    # مسودات منشورات أولية من الخطة
    for d in list(data.get("days") or [])[:days]:
        for p in list(d.get("posts") or [])[:3]:
            title = str(p.get("title") or "منشور")
            draft = (
                f"{title}\n\n"
                f"الزاوية: {p.get('angle') or ''}\n"
                f"المنصة: {p.get('platform') or ''}\n"
                f"CTA: {p.get('cta') or ''}\n\n"
                f"(مسودّة من خطة اليوم {d.get('day')}: {d.get('theme')})"
            )
            _add_art(
                db,
                run,
                role="content_manager",
                kind="post",
                title=title[:240],
                body=draft,
                meta={"from_plan": True, "day": d.get("day")},
            )
    run.chief_summary = f"أنشأ مدير المحتوى خطة لـ {days} أيام مع مسودات أولية."
    db.flush()
    # تفعيل الخطة للفريق وتوزيع مهام على الديزاين/الهاشتاج/البياع
    from modules.marketing_room.team_service import publish_team_plan_from_run

    publish_team_plan_from_run(db, run, data, focus=focus_s)
    db.flush()
    return run


def _normalize_focus(focus: str | None) -> str:
    f = (focus or "restaurant").strip().lower()
    if f not in ("restaurant", "hotel", "shared"):
        return "restaurant"
    return f


def _post_system_prompt(focus: str) -> str:
    if focus == "restaurant":
        return (
            "اكتب منشور تسويقي عربي قصير وجذاب لمطعم/كافيه فقط. "
            "لا تذكر الشقق الفندقية ولا الحجز السكني ولا الفندق. "
            "ادعُ لزيارة المطعم أو طلب الأكل/الطاولة فقط. "
            "بدون علامات اقتباس خارجية. 4–8 أسطر + سطر هاشتاجات في النهاية."
        )
    if focus == "hotel":
        return (
            "اكتب منشور تسويقي عربي قصير للشقق الفندقية/الإقامة فقط. "
            "بدون علامات اقتباس خارجية. 4–8 أسطر + سطر هاشتاجات في النهاية."
        )
    return (
        "اكتب منشور تسويقي عربي قصير وجذاب لمطعم وشقق فندقية. "
        "بدون علامات اقتباس خارجية. 4–8 أسطر + سطر هاشتاجات في النهاية."
    )


def _fallback_post_text(topic: str, store: str, focus: str) -> str:
    if focus == "restaurant":
        return (
            f"✨ {topic}\n\n"
            f"من {store} — نكهة تستاهل الزيارة.\n"
            "تعال جرّب الطبق وخلّي يومك ألذ.\n\n"
            "#ليبيا #مطعم #كافيه #روف"
        )
    if focus == "hotel":
        return (
            f"✨ {topic}\n\n"
            f"من {store} — إقامة مريحة بانتظاركم.\n"
            "احجزوا شقتكم وخلّوا إقامتكم أحلى.\n\n"
            "#ليبيا #شقق_فندقية #إقامة #حجز"
        )
    return (
        f"✨ {topic}\n\n"
        f"من {store} — تجربة تستاهل الزيارة.\n"
        "زورونا للمطعم أو احجزوا إقامتكم.\n\n"
        "#ليبيا #مطعم #كافيه #حجز"
    )


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


def _sync_linked_post_bodies(db: Session, art: MarketingArtifact, body: str) -> None:
    """حدّث نسخ النص المرتبطة بنفس التشغيل (content_manager)."""
    siblings = list(
        db.scalars(
            select(MarketingArtifact).where(
                MarketingArtifact.run_id == art.run_id,
                MarketingArtifact.id != art.id,
                MarketingArtifact.kind.in_(("post", "post_with_image")),
            )
        ).all()
    )
    for s in siblings:
        meta = _parse_meta(s)
        if meta.get("linked_design") or s.agent_role == "content_manager":
            s.body_text = body
            if s.status == "approved":
                s.status = "pending_approval"


def run_create_post(
    db: Session,
    *,
    user_id: int | None,
    topic: str,
    platform: str = "instagram",
    with_text: bool = True,
    with_image: bool = False,
    with_video: bool = False,
    image_prompt_custom: str = "",
    content_focus: str = "restaurant",
    team_task_id: int | None = None,
    fulfill_request_id: int | None = None,
) -> MarketingRun:
    """منشئ المحتوى: نص و/أو صورة و/أو فيديو حسب اختيار الأدمن."""
    from modules.marketing_room.team_service import (
        build_team_prompt_context,
        complete_team_request,
        complete_team_task,
        create_team_request,
        get_task,
    )

    if not (with_text or with_image or with_video):
        raise MarketingRoomError("اختر مهمة واحدة على الأقل: نص أو صورة أو فيديو.")

    topic_s = (topic or "").strip()
    task_meta: dict[str, Any] = {}
    if team_task_id:
        task = get_task(db, int(team_task_id))
        if task:
            task_meta = _parse_meta(task)
            if not topic_s:
                topic_s = str(task_meta.get("topic") or task.title or "").strip()
            if task_meta.get("platform"):
                platform = str(task_meta["platform"])
            if task_meta.get("focus"):
                content_focus = str(task_meta["focus"])
    if not topic_s:
        raise MarketingRoomError("أدخل موضوع المنشور أو اختر مهمة من خطة الفريق.")
    focus = _normalize_focus(content_focus)
    ctx = build_marketing_context(db, business_domain="shared")
    team_ctx = build_team_prompt_context(db)
    platform = (platform or "instagram").strip().lower()

    text = ""
    if with_text:
        text = chat_text(
            db,
            system=_post_system_prompt(focus)
            + " التزم بخطة الفريق والزوايا المعتمدة إن وُجدت. لا تخرج عن موضوع المهمة.",
            user=json.dumps(
                {
                    "topic": topic_s,
                    "platform": platform,
                    "content_focus": focus,
                    "brand": ctx,
                    "team_plan": team_ctx,
                    "task": {
                        "theme": task_meta.get("theme"),
                        "angle": task_meta.get("angle"),
                        "cta": task_meta.get("cta"),
                        "day": task_meta.get("day"),
                    },
                },
                ensure_ascii=False,
            )[:5500],
        )
        if not text:
            store = ctx.get("store_name") or "المطعم"
            text = _fallback_post_text(topic_s, store, focus)
    else:
        text = f"(لم يُطلب توليد نص)\nالموضوع: {topic_s}"

    run = _new_run(
        db,
        user_id=user_id,
        role="design",
        title=f"منشور — {topic_s[:80]}",
    )
    media_rel = None
    video_rel = None
    image_prompt = None
    custom_prompt = (image_prompt_custom or "").strip()

    if with_image or with_video:
        # برومبت الصورة منفصل عن العنوان دائماً
        if custom_prompt:
            image_prompt = (
                custom_prompt[:3500]
                + " Absolutely NO text, NO letters, NO Arabic writing on the image."
            )
        else:
            image_prompt = build_marketing_image_prompt(
                db,
                topic=topic_s,
                brand=str(ctx.get("store_name") or ""),
            )

    if with_image:
        hg = marketing_higgsfield_settings(db)
        img_cfg = marketing_image_settings(db)
        if not img_cfg.get("api_key") and not hg.get("api_key"):
            raise MarketingRoomError(
                "توليد الصور يحتاج مفتاح fal.ai أو Higgsfield أو OpenAI من إعدادات الغرفة."
            )
        fname = f"mkt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        dest = STATIC_MARKETING / fname
        saved = generate_image_file(db, prompt=image_prompt or topic_s, dest=dest)
        if saved is None:
            raise MarketingRoomError(
                "فشل توليد الصورة — جُرّب fal ثم Higgsfield ثم OpenAI. تحقق من المفاتيح."
            )
        media_rel = f"/static/uploads/marketing/{fname}"

    if with_video:
        hg = marketing_higgsfield_settings(db)
        if not hg.get("api_key"):
            raise MarketingRoomError(
                "توليد الفيديو يتطلب مفتاح Higgsfield من إعدادات غرفة التسويق."
            )
        vname = f"mkt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        vdest = STATIC_MARKETING / vname
        # إن وُجدت صورة محلية — لا نمرّر URL عام؛ نعتمد برومبت نصي
        vsaved = generate_video_file(
            db,
            prompt=(custom_prompt or image_prompt or topic_s)[:2000],
            dest=vdest,
            image_url=None,
        )
        if vsaved is None:
            raise MarketingRoomError(
                "فشل توليد الفيديو عبر Higgsfield — تحقق من المفتاح/الموديل أو جرّب لاحقاً."
            )
        video_rel = f"/static/uploads/marketing/{vname}"

    kind = "post"
    if with_video and video_rel:
        kind = "post_with_video"
    elif with_image and media_rel:
        kind = "post_with_image"

    design_art = _add_art(
        db,
        run,
        role="design",
        kind=kind,
        title=topic_s[:240],
        body=text,
        meta={
            "platform": platform,
            "topic": topic_s,
            "content_focus": focus,
            "image_prompt": image_prompt,
            "custom_image_prompt": custom_prompt or None,
            "tasks": {
                "text": with_text,
                "image": with_image,
                "video": with_video,
            },
            "image_note": "الصورة بدون نص؛ العربي في الكابشن فقط" if with_image else None,
            "video_path": video_rel,
            "team_task_id": team_task_id,
            "plan_run_id": task_meta.get("plan_run_id") or team_ctx.get("plan_run_id"),
            "from_team_plan": bool(team_task_id or team_ctx.get("plan_run_id")),
        },
        media_path=media_rel or video_rel,
    )
    if with_text:
        _add_art(
            db,
            run,
            role="content_manager",
            kind="post",
            title=f"نص منشور: {topic_s}"[:240],
            body=text,
            meta={
                "platform": platform,
                "linked_design": True,
                "content_focus": focus,
                "team_task_id": team_task_id,
            },
        )
    if team_task_id:
        complete_team_task(db, int(team_task_id), result_artifact_id=design_art.id)
        if with_text:
            create_team_request(
                db,
                user_id=user_id,
                from_role="design",
                to_role="hashtag",
                message=(
                    f"اكتمل منشور الفريق عن «{topic_s}». "
                    "رجاءً اقترح هاشتاجات متوافقة مع نص المنشور والخطة النشطة."
                ),
                related_artifact_id=design_art.id,
            )
    if fulfill_request_id:
        complete_team_request(db, int(fulfill_request_id), note="تم التنفيذ من الديزاين")
    bits = []
    if with_text:
        bits.append("نص")
    if media_rel:
        bits.append("صورة")
    if video_rel:
        bits.append("فيديو")
    run.chief_summary = ("منشور فريق" if team_task_id else "تم إنشاء منشور") + (
        " — " + " + ".join(bits) if bits else ""
    )
    db.flush()
    return run


def update_artifact_body(
    db: Session,
    artifact_id: int,
    body: str,
    *,
    title: str | None = None,
) -> MarketingArtifact:
    art = db.get(MarketingArtifact, int(artifact_id))
    if art is None:
        raise MarketingRoomError("المسودّة غير موجودة.")
    body_s = (body or "").strip()
    if not body_s:
        raise MarketingRoomError("النص لا يمكن أن يكون فارغاً.")
    art.body_text = body_s
    if title is not None and title.strip():
        art.title = title.strip()[:240]
    if art.status == "approved":
        art.status = "pending_approval"
    meta = _parse_meta(art)
    meta["manually_edited"] = True
    _save_meta(art, meta)
    _sync_linked_post_bodies(db, art, body_s)
    run = db.get(MarketingRun, art.run_id)
    if run and run.status == "completed":
        run.status = "awaiting_approval"
    db.flush()
    return art


def regenerate_artifact_text(
    db: Session,
    artifact_id: int,
    *,
    instruction: str = "",
    content_focus: str | None = None,
) -> MarketingArtifact:
    art = db.get(MarketingArtifact, int(artifact_id))
    if art is None:
        raise MarketingRoomError("المسودّة غير موجودة.")
    if art.kind not in ("post", "post_with_image", "sale_idea", "design_brief"):
        raise MarketingRoomError("إعادة توليد النص متاحة للمنشورات والمسودات النصية.")
    meta = _parse_meta(art)
    focus = _normalize_focus(content_focus or meta.get("content_focus") or "restaurant")
    topic = (meta.get("topic") or art.title or "").strip() or "منشور تسويقي"
    platform = str(meta.get("platform") or "instagram")
    ctx = build_marketing_context(db, business_domain="shared")
    instr = (instruction or "").strip()
    text = chat_text(
        db,
        system=_post_system_prompt(focus)
        + (
            f" تعليمات إضافية من المحرّر: {instr}" if instr else ""
        ),
        user=json.dumps(
            {
                "topic": topic,
                "platform": platform,
                "content_focus": focus,
                "previous_text": (art.body_text or "")[:2000],
                "editor_instruction": instr,
                "brand": {"store_name": ctx.get("store_name")},
            },
            ensure_ascii=False,
        )[:5000],
    )
    if not text:
        store = ctx.get("store_name") or "المطعم"
        text = _fallback_post_text(topic, store, focus)
    art.body_text = text
    art.status = "pending_approval"
    meta["content_focus"] = focus
    meta["regen_instruction"] = instr or None
    meta["text_regenerated_at"] = _utcnow().isoformat()
    _save_meta(art, meta)
    _sync_linked_post_bodies(db, art, text)
    run = db.get(MarketingRun, art.run_id)
    if run:
        run.status = "awaiting_approval"
        run.chief_summary = (run.chief_summary or "") + "\n· أُعيد توليد النص"
    db.flush()
    return art


def regenerate_artifact_image(db: Session, artifact_id: int) -> MarketingArtifact:
    art = db.get(MarketingArtifact, int(artifact_id))
    if art is None:
        raise MarketingRoomError("المسودّة غير موجودة.")
    img_cfg = marketing_image_settings(db)
    if not img_cfg["api_key"]:
        raise MarketingRoomError(
            "توليد الصور يحتاج مفتاح من إعدادات غرفة التسويق (fal.ai أو OpenAI)."
        )
    meta = _parse_meta(art)
    topic = (meta.get("topic") or art.title or "").strip() or "restaurant food"
    ctx = build_marketing_context(db, business_domain="shared")
    image_prompt = build_marketing_image_prompt(
        db,
        topic=topic,
        brand=str(ctx.get("store_name") or ""),
    )
    fname = f"mkt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{art.id}.png"
    dest = STATIC_MARKETING / fname
    saved = generate_image_file(db, prompt=image_prompt, dest=dest)
    if saved is None:
        raise MarketingRoomError("فشل توليد الصورة — تحقق من مفتاح/موديل الصور.")
    art.media_path = f"/static/uploads/marketing/{fname}"
    if art.kind == "post":
        art.kind = "post_with_image"
    art.status = "pending_approval"
    meta["image_prompt"] = image_prompt
    meta["image_regenerated_at"] = _utcnow().isoformat()
    meta["image_note"] = "الصورة بدون نص؛ العربي في الكابشن فقط"
    _save_meta(art, meta)
    run = db.get(MarketingRun, art.run_id)
    if run:
        run.status = "awaiting_approval"
        run.chief_summary = (run.chief_summary or "") + "\n· أُعيد توليد الصورة"
    db.flush()
    return art


def run_hashtags(
    db: Session,
    *,
    user_id: int | None,
    topic: str,
    team_task_id: int | None = None,
    fulfill_request_id: int | None = None,
) -> MarketingRun:
    from modules.marketing_room.team_service import (
        build_team_prompt_context,
        complete_team_request,
        complete_team_task,
        get_task,
    )

    topic_s = (topic or "").strip()
    if team_task_id:
        task = get_task(db, int(team_task_id))
        if task and not topic_s:
            topic_s = str(_parse_meta(task).get("topic") or "").strip()
    if not topic_s:
        team_ctx = build_team_prompt_context(db)
        days = team_ctx.get("plan_days") or []
        if days and isinstance(days[0], dict):
            posts = list(days[0].get("posts") or [])
            if posts:
                topic_s = str(posts[0].get("title") or days[0].get("theme") or "").strip()
    topic_s = topic_s or "منشور اليوم"
    ctx = build_marketing_context(db)
    team_ctx = build_team_prompt_context(db)
    data = chat_json(
        db,
        system=(
            "أرجع JSON فقط: {\"hashtags\":[\"#...\",...],\"notes\":\"...\"} "
            "بين 12 و20 هاشتاج عربي/إنجليزي مناسب لليبيا ومتوافق مع خطة الفريق."
        ),
        user=json.dumps(
            {
                "topic": topic_s,
                "brand": ctx.get("store_name"),
                "team_plan": team_ctx,
            },
            ensure_ascii=False,
        ),
    )
    tags = (data or {}).get("hashtags") if data else None
    if not tags:
        tags = [
            "#ليبيا",
            "#طرابلس",
            "#مطعم",
            "#كافيه",
            "#فطور",
            "#عشاء",
            "#حجز",
            "#شقق_فندقية",
            "#ضيافة",
            "#عرض_اليوم",
        ]
    body = " ".join(str(t) for t in tags)
    run = _new_run(db, user_id=user_id, role="hashtag", title=f"هاشتاجات — {topic_s[:60]}")
    tag_art = _add_art(
        db,
        run,
        role="hashtag",
        kind="hashtags",
        title="هاشتاجات",
        body=body,
        meta={
            "tags": list(tags),
            "notes": (data or {}).get("notes"),
            "team_task_id": team_task_id,
            "from_team_plan": True,
        },
    )
    if team_task_id:
        complete_team_task(db, int(team_task_id), result_artifact_id=tag_art.id)
    if fulfill_request_id:
        complete_team_request(db, int(fulfill_request_id), note="تم اقتراح الهاشتاجات")
    db.flush()
    return run


def run_seller_angles(
    db: Session,
    *,
    user_id: int | None,
    team_task_id: int | None = None,
    fulfill_request_id: int | None = None,
) -> MarketingRun:
    from modules.marketing_room.team_service import (
        build_team_prompt_context,
        complete_team_request,
        complete_team_task,
        get_task,
    )

    ctx = build_marketing_context(db)
    team_ctx = build_team_prompt_context(db)
    task_topic = ""
    if team_task_id:
        task = get_task(db, int(team_task_id))
        if task:
            task_topic = str(_parse_meta(task).get("topic") or "")
    data = chat_json(
        db,
        system=(
            "أرجع JSON: {\"angles\":[{\"title\":\"...\",\"pitch\":\"...\",\"offer\":\"...\"}]} "
            "بين 4 و6 زوايا بيع متوافقة مع خطة فريق التسويق إن وُجدت."
        ),
        user=json.dumps(
            {"catalog": ctx, "team_plan": team_ctx, "task_topic": task_topic},
            ensure_ascii=False,
        )[:6500],
    )
    angles = (data or {}).get("angles") if data else None
    if not angles:
        names = [p.get("name_ar") for p in (ctx.get("products") or [])[:4] if p.get("name_ar")]
        angles = [
            {
                "title": f"عرض {n}",
                "pitch": f"ركّز اليوم على {n} للزبائن المترددين.",
                "offer": "خصم أو إضافة مشروب",
            }
            for n in (names or ["طبق اليوم"])
        ]
    run = _new_run(db, user_id=user_id, role="website_seller", title="زوايا بيع اليوم")
    first_id = None
    for a in angles:
        art = _add_art(
            db,
            run,
            role="website_seller",
            kind="sale_idea",
            title=str(a.get("title") or "زاوية")[:240],
            body=f"{a.get('pitch') or ''}\n\nعرض مقترح: {a.get('offer') or ''}",
            meta={"team_task_id": team_task_id, "from_team_plan": True},
        )
        if first_id is None:
            first_id = art.id
    if team_task_id and first_id:
        complete_team_task(db, int(team_task_id), result_artifact_id=first_id)
    if fulfill_request_id:
        complete_team_request(db, int(fulfill_request_id), note="تم توليد زوايا البيع")
    db.flush()
    return run
