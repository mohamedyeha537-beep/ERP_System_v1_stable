"""خط أنابيب وكلاء التسويق: سياق → AI/n8n أو قوالب → آثار للموافقة."""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from modules.common.safe_http import open_safe_http
from modules.common.safe_http_url import assert_safe_http_url
from modules.marketing_room.config import marketing_ai_settings, marketing_n8n_webhook_url
from modules.marketing_room.context_service import build_marketing_context
from modules.marketing_room.models import MarketingAgentLog, MarketingArtifact, MarketingRun

LOG = logging.getLogger("marketing_room.pipeline")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _allow_http_dev() -> bool:
    from infra.config import get_settings

    return get_settings().app_env != "production"


def _log(db: Session, run_id: int, role: str, message: str, *, level: str = "info") -> None:
    db.add(
        MarketingAgentLog(
            run_id=run_id, agent_role=role, level=level, message=message[:4000]
        )
    )


def _fallback_bundle(ctx: dict[str, Any]) -> dict[str, Any]:
    store = ctx.get("store_name") or "المطعم"
    hotel = ctx.get("hotel_name") or "الفندق"
    products = ctx.get("products") or []
    rooms = ctx.get("rooms") or []
    names = [p.get("name_ar") for p in products[:5] if p.get("name_ar")]
    room_bits = [str(r.get("name_ar") or r.get("number") or "") for r in rooms[:4]]

    hero = names[0] if names else "عرض اليوم"
    sale_ideas = [
        f"اليوم ركّز على «{hero}» — عرض سريع للزبائن المترددين.",
        f"اربط زيارة {store} بإقامة مريحة في {hotel}." if rooms else f"قائمة اليوم من {store}.",
    ]
    if len(names) > 1:
        sale_ideas.append(f"ثنائي مميز: {names[0]} + {names[1]} بسعر جذاب.")

    posts = [
        {
            "title": f"عرض {hero}",
            "body": (
                f"🍽️ من {store}\n"
                f"جرّبوا «{hero}» اليوم — طعم يستاهل الزيارة.\n"
                f"{'الشقق: ' + ' · '.join(room_bits[:2]) if room_bits else 'حجوزات وطلبات مرحّب بها.'}\n"
                "راسلونا أو اطلبوا عبر التطبيق."
            ),
        },
        {
            "title": "لحظات الضيافة",
            "body": (
                f"أهلاً بكم في {store}"
                + (f" و{hotel}" if rooms else "")
                + ".\nيومكم أحلى مع قائمة طازجة وخدمة سريعة.\nاحجزوا طاولة أو شقة — نحن جاهزون."
            ),
        },
    ]
    tags = [
        "#ليبيا",
        "#طرابلس",
        f"#{store.replace(' ', '')[:24]}",
        "#مطعم",
        "#كافيه",
        "#فطور",
        "#عشاء",
        "#حجز",
    ]
    if rooms:
        tags.extend(["#شقق_فندقية", "#إقامة", "#عطلة"])

    site_brief = (
        f"العلامة: {store}"
        + (f" / {hotel}" if rooms else "")
        + f".\nأصناف بارزة: {', '.join(names) or 'حدّث القائمة في النظام'}."
        + (f"\nشقق/غرف: {', '.join(x for x in room_bits if x)}." if room_bits else "")
        + "\nالجمهور: عائلات، شباب، نزلاء إقامة قصيرة، طلب توصيل/استلام."
    )
    design = (
        "موجز تصميم:\n"
        "- ألوان العلامة من إعدادات الهوية\n"
        f"- عنوان كبير: {hero}\n"
        "- صورة طبق أو واجهة الشقة إن وُجدت\n"
        "- نص قصير عربي واضح + شعار في الزاوية"
    )
    chief = (
        f"تشغيل تسويقي جاهز للمراجعة: {len(sale_ideas)} زاوية بيع، "
        f"{len(posts)} مسودة منشور، وهاشتاجات مقترحة. "
        "اعتمد ما يناسب اليوم ثم انسخ للنشر يدوياً."
    )
    return {
        "site_brief": site_brief,
        "sale_ideas": sale_ideas,
        "posts": posts,
        "hashtags": tags,
        "design_brief": design,
        "chief_summary": chief,
    }


def _parse_ai_json(raw_text: str) -> dict[str, Any] | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return data


def _call_openai_bundle(db: Session, ctx: dict[str, Any]) -> dict[str, Any] | None:
    cfg = marketing_ai_settings(db)
    if not cfg["api_key"]:
        return None
    try:
        base = (cfg["base_url"] or "").strip().rstrip("/")
        assert_safe_http_url(f"{base}/chat/completions", allow_http=_allow_http_dev())
    except ValueError as exc:
        LOG.warning("marketing AI blocked base_url: %s", exc)
        return None
    prompt = (
        "أنت فريق تسويق لمطعم وشقق فندقية في ليبيا. "
        "أرجع JSON فقط بالمفاتيح: "
        "site_brief (نص)، sale_ideas (مصفوفة نصوص)، posts (مصفوفة {title,body})، "
        "hashtags (مصفوفة)، design_brief (نص)، chief_summary (نص).\n"
        "اللغة عربية واضحة، بدون إيموجي مفرط، مناسبة لفيسبوك/إنستغرام.\n"
        f"السياق:\n{json.dumps(ctx, ensure_ascii=False)[:6000]}"
    )
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "أرجع JSON صالحاً فقط بدون شرح."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 1800,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    try:
        with open_safe_http(req, timeout=55, allow_http=_allow_http_dev()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        content = (raw["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        LOG.warning("marketing AI failed: %s", exc)
        return None
    return _parse_ai_json(content)


def _call_n8n_bundle(db: Session, ctx: dict[str, Any], run_id: int) -> dict[str, Any] | None:
    url = marketing_n8n_webhook_url(db)
    if not url:
        return None
    try:
        assert_safe_http_url(url, allow_http=_allow_http_dev())
    except ValueError as exc:
        LOG.warning("marketing n8n blocked webhook: %s", exc)
        return None
    payload = {"run_id": run_id, "context": ctx, "action": "marketing_daily_pipeline"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with open_safe_http(req, timeout=60, allow_http=_allow_http_dev()) as resp:
            body = resp.read().decode("utf-8")
        data = json.loads(body) if body.strip() else None
    except Exception as exc:
        LOG.warning("marketing n8n failed: %s", exc)
        return None
    if isinstance(data, dict) and "site_brief" in data:
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return _parse_ai_json(body) if isinstance(body, str) else None


def _normalize_bundle(raw: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    fb = _fallback_bundle(ctx)
    posts_in = raw.get("posts") or fb["posts"]
    posts: list[dict[str, str]] = []
    for p in posts_in:
        if isinstance(p, dict):
            posts.append(
                {
                    "title": str(p.get("title") or "منشور")[:200],
                    "body": str(p.get("body") or p.get("text") or "")[:4000],
                }
            )
        elif isinstance(p, str) and p.strip():
            posts.append({"title": "منشور", "body": p.strip()[:4000]})
    ideas = raw.get("sale_ideas") or fb["sale_ideas"]
    if isinstance(ideas, str):
        ideas = [ideas]
    tags = raw.get("hashtags") or fb["hashtags"]
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    return {
        "site_brief": str(raw.get("site_brief") or fb["site_brief"])[:6000],
        "sale_ideas": [str(x)[:800] for x in list(ideas)[:8]],
        "posts": posts[:6] or fb["posts"],
        "hashtags": [str(t)[:80] for t in list(tags)[:24]],
        "design_brief": str(raw.get("design_brief") or fb["design_brief"])[:4000],
        "chief_summary": str(raw.get("chief_summary") or fb["chief_summary"])[:4000],
    }


def _persist_artifacts(db: Session, run: MarketingRun, bundle: dict[str, Any]) -> None:
    def add(role: str, kind: str, title: str, body: str, meta: dict | None = None) -> None:
        db.add(
            MarketingArtifact(
                run_id=run.id,
                agent_role=role,
                kind=kind,
                status="pending_approval",
                title=title[:240],
                body_text=body,
                meta_json=json.dumps(meta, ensure_ascii=False) if meta else None,
            )
        )

    add("website_manager", "site_brief", "موجز الموقع والجمهور", bundle["site_brief"])
    for i, idea in enumerate(bundle["sale_ideas"], 1):
        add("website_seller", "sale_idea", f"زاوية بيع {i}", idea)
    for i, post in enumerate(bundle["posts"], 1):
        add(
            "content_manager",
            "post",
            post.get("title") or f"منشور {i}",
            post.get("body") or "",
        )
    tags_body = " ".join(bundle["hashtags"])
    add(
        "hashtag",
        "hashtags",
        "هاشتاجات مقترحة",
        tags_body,
        {"tags": bundle["hashtags"]},
    )
    add("design", "design_brief", "موجز التصميم", bundle["design_brief"])
    add("chief", "chief_report", "ملخص الوكيل الرئيسي", bundle["chief_summary"])
    run.chief_summary = bundle["chief_summary"]


def start_daily_pipeline(
    db: Session,
    *,
    user_id: int | None,
    business_domain: str = "shared",
    title: str = "",
) -> MarketingRun:
    ctx = build_marketing_context(db, business_domain=business_domain)
    run = MarketingRun(
        business_domain=ctx["business_domain"],
        status="running",
        title=(title or f"تشغيل تسويق {datetime.now().strftime('%Y-%m-%d %H:%M')}")[:200],
        triggered_by_id=user_id,
        context_json=json.dumps(ctx, ensure_ascii=False),
    )
    db.add(run)
    db.flush()
    _log(db, run.id, "chief", "بدء خط الأنابيب")

    bundle_raw: dict[str, Any] | None = None
    try:
        bundle_raw = _call_n8n_bundle(db, ctx, run.id)
        if bundle_raw:
            _log(db, run.id, "chief", "استجابة n8n")
        else:
            bundle_raw = _call_openai_bundle(db, ctx)
            if bundle_raw:
                _log(db, run.id, "chief", "استجابة نموذج الذكاء")
            else:
                bundle_raw = _fallback_bundle(ctx)
                _log(db, run.id, "chief", "قوالب محلية (بدون AI/n8n)", level="warn")

        bundle = _normalize_bundle(bundle_raw, ctx)
        _persist_artifacts(db, run, bundle)
        run.status = "awaiting_approval"
        run.finished_at = _utcnow()
        _log(db, run.id, "chief", "اكتمل — بانتظار موافقة بشرية")
    except Exception as exc:
        LOG.exception("pipeline failed")
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = _utcnow()
        _log(db, run.id, "chief", f"فشل: {exc}", level="error")

    db.flush()
    return run
