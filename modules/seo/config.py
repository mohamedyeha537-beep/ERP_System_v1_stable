"""إعدادات SEO من البيئة — لا مفاتيح في الكود."""
from __future__ import annotations

import os
from functools import lru_cache


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def seo_config() -> dict:
    env = (os.environ.get("SEO_ENVIRONMENT") or "staging").strip().lower()
    if env not in ("staging", "production"):
        env = "staging"
    public = (os.environ.get("SEO_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not public:
        public = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return {
        "enabled": _env_bool("SEO_AGENT_ENABLED", False),
        "environment": env,
        "api_key": (os.environ.get("SEO_AGENT_API_KEY") or "").strip(),
        "allow_auto_apply": _env_bool("SEO_ALLOW_AUTO_APPLY", False),
        "require_production_approval": _env_bool(
            "SEO_REQUIRE_PRODUCTION_APPROVAL", True
        ),
        "public_base_url": public,
        "n8n_webhook_url": (os.environ.get("SEO_N8N_WEBHOOK_URL") or "").strip(),
        "n8n_callback_secret": (os.environ.get("SEO_N8N_CALLBACK_SECRET") or "").strip(),
    }


def clear_seo_config_cache() -> None:
    seo_config.cache_clear()


def is_production() -> bool:
    return seo_config()["environment"] == "production"
