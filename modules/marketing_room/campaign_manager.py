"""مدير حملات فيسبوك: مزامنة رؤى Meta → تحليل → توجيه الوكلاء."""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.common.safe_http_url import assert_safe_http_url, is_safe_http_url
from modules.marketing_room.ai_client import chat_json
from modules.marketing_room.config import marketing_meta_settings
from modules.marketing_room.models import MarketingArtifact, MarketingRun
from modules.marketing_room.service import MarketingRoomError

LOG = logging.getLogger("marketing_room.campaigns")

# أدوار تستقبل توجيهات التحسين
DIRECTIVE_ROLES = (
    "content_manager",
    "design",
    "hashtag",
    "website_seller",
    "website_manager",
    "delivery",
    "chief",
)

ROLE_AR = {
    "content_manager": "مدير المحتوى",
    "design": "منشئ المحتوى / الديزاين",
    "hashtag": "وكيل الهاشتاجات",
    "website_seller": "بياع الموقع",
    "website_manager": "مدير الموقع",
    "delivery": "موظف النشر",
    "chief": "الوكيل الرئيسي",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _http_get_json(url: str, *, timeout: int = 60) -> dict[str, Any]:
    assert_safe_http_url(url, allow_http=False, allowed_hosts=frozenset({"graph.facebook.com"}))
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_campaign_rows(raw: Any) -> list[dict[str, Any]]:
    """يوحّد بيانات حملات من Meta API أو حمولة MCP/n8n."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        if isinstance(raw.get("campaigns"), list):
            items = raw["campaigns"]
        elif isinstance(raw.get("data"), list):
            items = raw["data"]
        else:
            items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        insights = item.get("insights") or item.get("metrics") or {}
        if isinstance(insights, dict) and isinstance(insights.get("data"), list) and insights["data"]:
            insights = insights["data"][0] if isinstance(insights["data"][0], dict) else {}
        if not isinstance(insights, dict):
            insights = {}
        out.append(
            {
                "id": str(item.get("id") or item.get("campaign_id") or "")[:80],
                "name": str(item.get("name") or item.get("campaign_name") or "حملة")[:200],
                "status": str(item.get("status") or item.get("effective_status") or "")[:40],
                "objective": str(item.get("objective") or "")[:80],
                "impressions": _num(insights.get("impressions") or item.get("impressions")),
                "clicks": _num(insights.get("clicks") or item.get("clicks")),
                "spend": _num(insights.get("spend") or item.get("spend")),
                "ctr": _num(insights.get("ctr") or item.get("ctr")),
                "cpc": _num(insights.get("cpc") or item.get("cpc")),
                "reach": _num(insights.get("reach") or item.get("reach")),
                "raw": item,
            }
        )
    return out


def _num(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_meta_campaigns(db: Session, *, date_preset: str = "last_7d") -> list[dict[str, Any]]:
    """جلب الحملات + insights من Meta Marketing API."""
    cfg = marketing_meta_settings(db)
    token = cfg["access_token"]
    ad_account = cfg["ad_account_id"]
    if not token or not ad_account:
        raise MarketingRoomError(
            "أضف Meta Access Token و Ad Account ID من إعدادات غرفة التسويق، "
            "أو استورد بيانات الحملات عبر MCP/n8n."
        )
    act = ad_account if ad_account.startswith("act_") else f"act_{ad_account}"
    fields = (
        "name,status,effective_status,objective,"
        f"insights.date_preset({date_preset})"
        "{impressions,clicks,spend,ctr,cpc,reach,actions}"
    )
    qs = urllib.parse.urlencode(
        {
            "fields": fields,
            "limit": "50",
            "access_token": token,
        }
    )
    version = cfg["api_version"] or "v21.0"
    url = f"https://graph.facebook.com/{version}/{act}/campaigns?{qs}"
    try:
        raw = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        LOG.warning("meta campaigns fetch failed: %s %s", exc.code, detail)
        raise MarketingRoomError(f"فشل جلب حملات Meta ({exc.code}): {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        LOG.warning("meta campaigns fetch error: %s", exc)
        raise MarketingRoomError(f"فشل الاتصال بـ Meta: {exc}") from exc
    if raw.get("error"):
        raise MarketingRoomError(str(raw["error"])[:300])
    return normalize_campaign_rows(raw)


def run_campaign_review(
    db: Session,
    *,
    user_id: int | None,
    campaigns: list[dict[str, Any]] | None = None,
    source: str = "meta_api",
    notes: str = "",
    date_preset: str = "last_7d",
) -> MarketingRun:
    """
    يحلّل الحملات ويُنشئ توجيهات لكل وكيل (kind=campaign_directive).
    campaigns=None → جلب من Meta API.
    """
    if campaigns is None:
        campaigns = fetch_meta_campaigns(db, date_preset=date_preset)
    else:
        campaigns = normalize_campaign_rows(campaigns)
    if not campaigns:
        raise MarketingRoomError("لا توجد حملات للتحليل — اربط Meta أو ارفع بيانات من MCP.")

    run = MarketingRun(
        business_domain="shared",
        status="awaiting_approval",
        title=f"مراجعة حملات فيسبوك — {datetime.now().strftime('%Y-%m-%d %H:%M')}"[:200],
        triggered_by_id=user_id,
        context_json=json.dumps(
            {"source": source, "date_preset": date_preset, "campaign_count": len(campaigns)},
            ensure_ascii=False,
        ),
    )
    db.add(run)
    db.flush()

    # ملخص خام للحملات
    summary_lines = []
    for c in campaigns[:20]:
        summary_lines.append(
            f"• {c['name']} [{c['status']}] "
            f"impressions={c['impressions']:.0f} clicks={c['clicks']:.0f} "
            f"spend={c['spend']:.2f} CTR={c['ctr']:.3f}"
        )
    snapshot_body = "\n".join(summary_lines)
    if notes.strip():
        snapshot_body = f"ملاحظات المشغّل:\n{notes.strip()}\n\n{snapshot_body}"

    db.add(
        MarketingArtifact(
            run_id=run.id,
            agent_role="campaign_manager",
            kind="campaign_snapshot",
            status="pending_approval",
            title="لقطة أداء الحملات",
            body_text=snapshot_body,
            meta_json=json.dumps(
                {"campaigns": campaigns[:30], "source": source},
                ensure_ascii=False,
            )[:15000],
        )
    )

    directives = _analyze_to_directives(db, campaigns, notes=notes)
    for d in directives:
        target = d.get("target_role") or "content_manager"
        if target not in DIRECTIVE_ROLES:
            target = "content_manager"
        title = f"توجيه ← {ROLE_AR.get(target, target)}: {d.get('title') or 'تحسين'}"
        body = (
            f"{d.get('recommendation') or ''}\n\n"
            f"السبب: {d.get('reason') or ''}\n"
            f"الأولوية: {d.get('priority') or 'medium'}"
        ).strip()
        db.add(
            MarketingArtifact(
                run_id=run.id,
                agent_role="campaign_manager",
                kind="campaign_directive",
                status="pending_approval",
                title=title[:240],
                body_text=body,
                meta_json=json.dumps(
                    {
                        "target_role": target,
                        "priority": d.get("priority") or "medium",
                        "campaign_ids": d.get("campaign_ids") or [],
                        "metrics": d.get("metrics") or {},
                    },
                    ensure_ascii=False,
                ),
            )
        )

    run.chief_summary = (
        f"مدير حملات فيسبوك راجع {len(campaigns)} حملة ({source}) "
        f"وأصدر {len(directives)} توجيهاً للوكلاء."
    )
    db.flush()
    return run


def _analyze_to_directives(
    db: Session,
    campaigns: list[dict[str, Any]],
    *,
    notes: str = "",
) -> list[dict[str, Any]]:
    compact = [
        {
            "id": c["id"],
            "name": c["name"],
            "status": c["status"],
            "objective": c["objective"],
            "impressions": c["impressions"],
            "clicks": c["clicks"],
            "spend": c["spend"],
            "ctr": c["ctr"],
            "cpc": c["cpc"],
        }
        for c in campaigns[:25]
    ]
    data = chat_json(
        db,
        system=(
            "أنت مدير حملات إعلانات فيسبوك لمطعم/كافيه وشقق فندقية في ليبيا. "
            "حلّل الأداء وأرجع JSON فقط بهذا الشكل:\n"
            '{"directives":[{"target_role":"content_manager|design|hashtag|website_seller|'
            'website_manager|delivery|chief","title":"...","recommendation":"...",'
            '"reason":"...","priority":"high|medium|low","campaign_ids":[],"metrics":{}}]}\n'
            "وزّع التوجيهات حسب الدور: المحتوى للنصوص، الديزاين للصور، الهاشتاج للكلمات، "
            "البياع للعروض، النشر لجدولة/استهداف، المدير للموقع. "
            "3 إلى 8 توجيهات عملية وقابلة للتنفيذ."
        ),
        user=json.dumps(
            {"campaigns": compact, "operator_notes": notes},
            ensure_ascii=False,
        )[:7000],
        max_tokens=2200,
    )
    dirs = (data or {}).get("directives") if data else None
    if isinstance(dirs, list) and dirs:
        return [d for d in dirs if isinstance(d, dict)][:10]
    return _heuristic_directives(campaigns)


def _heuristic_directives(campaigns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """قواعد محلية إن لم يتوفر AI."""
    active = [c for c in campaigns if str(c.get("status", "")).upper() in ("ACTIVE", "ENABLED", "")]
    pool = active or campaigns
    by_ctr = sorted(pool, key=lambda c: c.get("ctr") or 0)
    weak = by_ctr[0] if by_ctr else pool[0]
    strong = by_ctr[-1] if by_ctr else pool[0]
    spend_heavy = sorted(pool, key=lambda c: c.get("spend") or 0, reverse=True)[0]
    return [
        {
            "target_role": "content_manager",
            "title": "تحسين نصوص الإعلانات ضعيفة CTR",
            "recommendation": (
                f"أعد كتابة زاوية البيع لحملة «{weak['name']}» — ركّز على عرض واضح وCTA للمطعم "
                "بدون خلط الشقق إلا إذا كانت الحملة فندقية."
            ),
            "reason": f"CTR منخفض نسبياً ({weak.get('ctr')})",
            "priority": "high",
            "campaign_ids": [weak.get("id")],
            "metrics": {"ctr": weak.get("ctr"), "spend": weak.get("spend")},
        },
        {
            "target_role": "design",
            "title": "تجديد الصورة الإبداعية",
            "recommendation": (
                f"ولّد صورة جديدة أقرب للمنتج الحقيقي لحملة «{weak['name']}» "
                "بدون نص عربي على الصورة."
            ),
            "reason": "تحسين الإبداع البصري غالباً يرفع CTR",
            "priority": "high",
            "campaign_ids": [weak.get("id")],
        },
        {
            "target_role": "hashtag",
            "title": "هاشتاجات أدق للجمهور المحلي",
            "recommendation": "اقترح مجموعة هاشتاجات ليبية + كلمات عرض محدّدة بالبرجر/الطبق الرائج.",
            "reason": "مواءمة الكلمات مع الإبداع الناجح",
            "priority": "medium",
            "campaign_ids": [strong.get("id")],
        },
        {
            "target_role": "website_seller",
            "title": "عرض يطابق الحملة الأعلى إنفاقاً",
            "recommendation": (
                f"صِغ عرضاً يومياً مرتبطاً بحملة «{spend_heavy['name']}» "
                f"(إنفاق {spend_heavy.get('spend')}) لرفع التحويل من الإعلان للمطعم."
            ),
            "reason": "ربط الإنفاق بعرض واضح",
            "priority": "medium",
            "campaign_ids": [spend_heavy.get("id")],
        },
        {
            "target_role": "delivery",
            "title": "مراجعة الاستهداف والجدولة",
            "recommendation": (
                "راجع أوقات النشر والجمهور لحملات CTR المنخفض؛ قلّل الإنفاق على الضعيف "
                "وزِد الميزانية على الأفضل أداءً بعد اعتماد المحتوى الجديد."
            ),
            "reason": "تحسين مستمر للتوزيع",
            "priority": "medium",
            "campaign_ids": [weak.get("id"), strong.get("id")],
        },
    ]


def list_directives_for_role(
    db: Session,
    role: str,
    *,
    limit: int = 20,
    pending_only: bool = True,
) -> list[MarketingArtifact]:
    q = (
        select(MarketingArtifact)
        .where(
            MarketingArtifact.kind == "campaign_directive",
            MarketingArtifact.agent_role == "campaign_manager",
        )
        .order_by(MarketingArtifact.id.desc())
        .limit(80)
    )
    rows = list(db.scalars(q).all())
    out: list[MarketingArtifact] = []
    for a in rows:
        meta = {}
        if a.meta_json:
            try:
                meta = json.loads(a.meta_json) or {}
            except json.JSONDecodeError:
                meta = {}
        if meta.get("target_role") != role:
            continue
        if pending_only and a.status not in ("pending_approval", "approved"):
            continue
        out.append(a)
        if len(out) >= limit:
            break
    return out


def meta_connection_status(db: Session) -> dict[str, Any]:
    cfg = marketing_meta_settings(db)
    return {
        "configured": bool(cfg["access_token"] and cfg["ad_account_id"]),
        "ad_account_id": cfg["ad_account_id"],
        "page_id": cfg["page_id"],
        "api_version": cfg["api_version"],
        "has_token": bool(cfg["access_token"]),
        "mcp_note": (
            "MCP في Cursor مفيد أثناء الإعداد والجلب اليدوي. "
            "للمتابعة المستمرة داخل النظام استخدم Meta API هنا "
            "أو أرسل الرؤى إلى /api/marketing-room/campaigns/insights"
        ),
    }
