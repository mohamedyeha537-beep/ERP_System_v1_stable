"""إعدادات غرفة التسويق من البيئة وAppSetting."""
from __future__ import annotations

import os

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting

# مزوّدو الصور المدعومون
IMAGE_PROVIDERS = (
    ("fal", "fal.ai (الأساسي — مع احتياطي Higgsfield)"),
    ("higgsfield", "Higgsfield.ai (صور/فيديو)"),
    ("openai", "OpenAI Images / DALL·E (أو متوافق)"),
)

DEFAULT_FAL_MODEL = "fal-ai/flux/schnell"
DEFAULT_HIGGS_IMAGE_MODEL = "bytedance/seedream/v4/text-to-image"
DEFAULT_HIGGS_VIDEO_MODEL = "higgsfield-ai/soul/standard"


def marketing_n8n_webhook_url(db: Session | None = None) -> str:
    env = (os.environ.get("MARKETING_N8N_WEBHOOK_URL") or "").strip()
    if env:
        return env
    if db is not None:
        return (get_setting(db, "marketing_n8n_webhook_url", "") or "").strip()
    return ""


def marketing_ai_settings(db: Session) -> dict[str, str]:
    """مفاتيح النص — إعدادات التسويق ثم سقوط على محادثة الويب."""
    api_key = (
        get_setting(db, "marketing_ai_api_key", "")
        or get_setting(db, "web_chat_ai_api_key", "")
        or ""
    ).strip()
    base = (
        get_setting(db, "marketing_ai_base_url", "")
        or get_setting(db, "web_chat_ai_base_url", "https://api.openai.com/v1")
        or "https://api.openai.com/v1"
    ).strip().rstrip("/")
    model = (
        get_setting(db, "marketing_ai_model", "")
        or get_setting(db, "web_chat_ai_model", "gpt-4o-mini")
        or "gpt-4o-mini"
    ).strip()
    return {"api_key": api_key, "base_url": base, "model": model}


def marketing_higgsfield_settings(db: Session) -> dict[str, str]:
    """مفتاح Higgsfield — غالباً بصيغة key_id:key_secret."""
    api_key = (
        get_setting(db, "marketing_higgsfield_api_key", "")
        or (os.environ.get("HF_KEY") or "")
        or (os.environ.get("HIGGSFIELD_API_KEY") or "")
        or ""
    ).strip()
    image_model = (
        get_setting(db, "marketing_higgsfield_image_model", DEFAULT_HIGGS_IMAGE_MODEL)
        or DEFAULT_HIGGS_IMAGE_MODEL
    ).strip()
    video_model = (
        get_setting(db, "marketing_higgsfield_video_model", DEFAULT_HIGGS_VIDEO_MODEL)
        or DEFAULT_HIGGS_VIDEO_MODEL
    ).strip()
    return {
        "api_key": api_key,
        "image_model": image_model,
        "video_model": video_model,
        "base_url": "https://platform.higgsfield.ai",
    }


def marketing_image_settings(db: Session) -> dict[str, str]:
    """مفاتيح توليد الصور — fal / Higgsfield / OpenAI."""
    text = marketing_ai_settings(db)
    provider = (get_setting(db, "marketing_image_provider", "fal") or "fal").strip().lower()
    if provider not in ("openai", "fal", "higgsfield"):
        provider = "fal"
    size = get_setting(db, "marketing_image_size", "1024x1024") or "1024x1024"

    if provider == "fal":
        api_key = (
            get_setting(db, "marketing_fal_api_key", "")
            or get_setting(db, "marketing_image_api_key", "")
            or (os.environ.get("FAL_KEY") or "")
            or ""
        ).strip()
        model = (
            get_setting(db, "marketing_image_model", DEFAULT_FAL_MODEL) or DEFAULT_FAL_MODEL
        ).strip()
        if model.startswith("dall-e") or "/images" in model or model.startswith("bytedance/"):
            model = DEFAULT_FAL_MODEL
        return {
            "provider": "fal",
            "api_key": api_key,
            "base_url": "https://fal.run",
            "model": model,
            "size": size,
        }

    if provider == "higgsfield":
        hg = marketing_higgsfield_settings(db)
        return {
            "provider": "higgsfield",
            "api_key": hg["api_key"],
            "base_url": hg["base_url"],
            "model": hg["image_model"],
            "size": size,
        }

    api_key = (
        get_setting(db, "marketing_image_api_key", "") or text["api_key"] or ""
    ).strip()
    base = (
        get_setting(db, "marketing_image_base_url", "") or text["base_url"] or ""
    ).strip().rstrip("/")
    model = (get_setting(db, "marketing_image_model", "dall-e-3") or "dall-e-3").strip()
    return {
        "provider": "openai",
        "api_key": api_key,
        "base_url": base,
        "model": model,
        "size": size,
    }


def save_marketing_settings(
    db: Session,
    *,
    ai_api_key: str,
    ai_base_url: str,
    ai_model: str,
    image_provider: str,
    fal_api_key: str,
    higgsfield_api_key: str = "",
    higgsfield_image_model: str = "",
    higgsfield_video_model: str = "",
    image_api_key: str,
    image_base_url: str,
    image_model: str,
    image_size: str,
    n8n_webhook: str,
) -> None:
    if ai_api_key.strip() and not ai_api_key.strip().startswith("••••"):
        set_setting(db, "marketing_ai_api_key", ai_api_key.strip()[:500])
    if ai_base_url.strip():
        set_setting(db, "marketing_ai_base_url", ai_base_url.strip()[:300])
    if ai_model.strip():
        set_setting(db, "marketing_ai_model", ai_model.strip()[:80])

    prov = (image_provider or "fal").strip().lower()
    if prov not in ("openai", "fal", "higgsfield"):
        prov = "fal"
    set_setting(db, "marketing_image_provider", prov)

    if fal_api_key.strip() and not fal_api_key.strip().startswith("••••"):
        set_setting(db, "marketing_fal_api_key", fal_api_key.strip()[:500])
        set_setting(db, "marketing_image_api_key", fal_api_key.strip()[:500])
    elif image_api_key.strip() and not image_api_key.strip().startswith("••••"):
        set_setting(db, "marketing_image_api_key", image_api_key.strip()[:500])

    if higgsfield_api_key.strip() and not higgsfield_api_key.strip().startswith("••••"):
        set_setting(db, "marketing_higgsfield_api_key", higgsfield_api_key.strip()[:500])
    if higgsfield_image_model.strip():
        set_setting(db, "marketing_higgsfield_image_model", higgsfield_image_model.strip()[:160])
    if higgsfield_video_model.strip():
        set_setting(db, "marketing_higgsfield_video_model", higgsfield_video_model.strip()[:160])

    if image_base_url.strip():
        set_setting(db, "marketing_image_base_url", image_base_url.strip()[:300])
    if image_model.strip():
        set_setting(db, "marketing_image_model", image_model.strip()[:120])
    if image_size.strip():
        set_setting(db, "marketing_image_size", image_size.strip()[:32])
    set_setting(db, "marketing_n8n_webhook_url", (n8n_webhook or "").strip()[:500])


def marketing_meta_settings(db: Session) -> dict[str, str]:
    """إعدادات Meta Marketing API لمدير حملات فيسبوك."""
    return {
        "access_token": (
            get_setting(db, "marketing_meta_access_token", "")
            or (os.environ.get("META_ACCESS_TOKEN") or "")
            or ""
        ).strip(),
        "ad_account_id": (
            get_setting(db, "marketing_meta_ad_account_id", "")
            or (os.environ.get("META_AD_ACCOUNT_ID") or "")
            or ""
        ).strip(),
        "page_id": (get_setting(db, "marketing_meta_page_id", "") or "").strip(),
        "api_version": (
            get_setting(db, "marketing_meta_api_version", "v21.0") or "v21.0"
        ).strip(),
    }


def save_marketing_meta_settings(
    db: Session,
    *,
    access_token: str,
    ad_account_id: str,
    page_id: str,
    api_version: str,
) -> None:
    if access_token.strip() and not access_token.strip().startswith("••••"):
        set_setting(db, "marketing_meta_access_token", access_token.strip()[:500])
    if ad_account_id.strip():
        set_setting(db, "marketing_meta_ad_account_id", ad_account_id.strip()[:80])
    set_setting(db, "marketing_meta_page_id", (page_id or "").strip()[:80])
    if api_version.strip():
        set_setting(db, "marketing_meta_api_version", api_version.strip()[:20])
