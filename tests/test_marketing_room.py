"""اختبارات أساسية لغرفة وكلاء التسويق."""
from __future__ import annotations

from modules.marketing_room.agents import AGENT_CARDS
from modules.marketing_room.pipeline import _fallback_bundle, _normalize_bundle


def test_eight_agent_cards():
    assert len(AGENT_CARDS) == 8
    roles = {a.role for a in AGENT_CARDS}
    assert "chief" in roles
    assert "customer_service" in roles
    cs = next(a for a in AGENT_CARDS if a.role == "customer_service")
    assert cs.external_href == "/admin/messaging/inbox"


def test_fallback_bundle_has_posts():
    ctx = {
        "store_name": "روف",
        "hotel_name": "بيتك",
        "products": [{"name_ar": "افطار نزيل"}],
        "rooms": [{"number": "8", "name_ar": "شقة 8"}],
    }
    b = _fallback_bundle(ctx)
    assert b["sale_ideas"]
    assert b["posts"]
    assert b["hashtags"]
    assert "روف" in b["site_brief"] or "افطار" in b["site_brief"]


def test_normalize_bundle():
    ctx = {"store_name": "X", "hotel_name": "Y", "products": [], "rooms": []}
    raw = {
        "site_brief": "brief",
        "sale_ideas": ["a"],
        "posts": [{"title": "t", "body": "b"}],
        "hashtags": ["#x"],
        "design_brief": "d",
        "chief_summary": "c",
    }
    n = _normalize_bundle(raw, ctx)
    assert n["posts"][0]["body"] == "b"
    assert "#x" in n["hashtags"]
