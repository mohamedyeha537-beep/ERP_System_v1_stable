"""استدعاءات OpenAI-compatible للنصوص + صور OpenAI أو fal.ai."""
from __future__ import annotations

import base64
import json
import logging
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy.orm import Session

from infra.config import get_settings
from modules.common.safe_http_url import assert_safe_http_url, is_safe_http_url
from modules.marketing_room.config import (
    marketing_ai_settings,
    marketing_higgsfield_settings,
    marketing_image_settings,
)

LOG = logging.getLogger("marketing_room.ai")


def _allow_http_dev() -> bool:
    return get_settings().app_env != "production"


def _normalize_base_url(base_url: str) -> str:
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    return raw


def _assert_api_base_url(base_url: str) -> str:
    base = _normalize_base_url(base_url)
    assert_safe_http_url(base, allow_http=_allow_http_dev())
    return base


def _assert_download_url(url: str) -> str:
    raw = (url or "").strip()
    assert_safe_http_url(raw, allow_http=False)
    return raw


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def chat_json(db: Session, *, system: str, user: str, max_tokens: int = 2000) -> dict[str, Any] | None:
    cfg = marketing_ai_settings(db)
    if not cfg["api_key"]:
        return None
    try:
        base = _assert_api_base_url(cfg["base_url"])
    except ValueError as exc:
        LOG.warning("chat_json blocked base_url: %s", exc)
        return None
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "max_tokens": max_tokens,
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
        with urllib.request.urlopen(req, timeout=70, context=_ssl_context()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        content = (raw["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        LOG.warning("chat_json failed: %s", exc)
        return None
    return _parse_json_object(content)


def chat_text(db: Session, *, system: str, user: str, max_tokens: int = 1200) -> str | None:
    cfg = marketing_ai_settings(db)
    if not cfg["api_key"]:
        return None
    try:
        base = _assert_api_base_url(cfg["base_url"])
    except ValueError as exc:
        LOG.warning("chat_text blocked base_url: %s", exc)
        return None
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.75,
        "max_tokens": max_tokens,
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
        with urllib.request.urlopen(req, timeout=70, context=_ssl_context()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        return (raw["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as exc:
        LOG.warning("chat_text failed: %s", exc)
        return None


def generate_image_file(
    db: Session,
    *,
    prompt: str,
    dest: Path,
) -> Path | None:
    """يولّد صورة مع سلسلة احتياط: المزوّد المختار → fal → Higgsfield → OpenAI."""
    import os

    from modules.settings.service import get_setting

    primary = marketing_image_settings(db)
    hg = marketing_higgsfield_settings(db)
    text = marketing_ai_settings(db)
    size = primary.get("size") or "1024x1024"

    fal_key = (
        get_setting(db, "marketing_fal_api_key", "")
        or (os.environ.get("FAL_KEY") or "")
        or (primary.get("api_key") if primary.get("provider") == "fal" else "")
        or ""
    ).strip()
    fal_model = (
        primary.get("model")
        if primary.get("provider") == "fal"
        else "fal-ai/flux/schnell"
    )

    chain: list[tuple[str, dict[str, str]]] = []
    order = [primary.get("provider") or "fal", "fal", "higgsfield", "openai"]
    seen: set[str] = set()
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        if name == "fal" and fal_key:
            chain.append(
                (
                    "fal",
                    {
                        "api_key": fal_key,
                        "model": fal_model or "fal-ai/flux/schnell",
                        "size": size,
                    },
                )
            )
        elif name == "higgsfield" and hg.get("api_key"):
            chain.append(
                (
                    "higgsfield",
                    {
                        "api_key": hg["api_key"],
                        "model": hg["image_model"],
                        "size": size,
                        "base_url": hg["base_url"],
                    },
                )
            )
        elif name == "openai" and text.get("api_key"):
            chain.append(
                (
                    "openai",
                    {
                        "api_key": text["api_key"],
                        "model": "dall-e-3",
                        "size": size,
                        "base_url": text["base_url"],
                    },
                )
            )

    for name, cfg in chain:
        try:
            if name == "fal":
                saved = _generate_fal_image(cfg, prompt=prompt, dest=dest)
            elif name == "higgsfield":
                saved = _generate_higgsfield_image(cfg, prompt=prompt, dest=dest)
            else:
                saved = _generate_openai_image(cfg, prompt=prompt, dest=dest)
            if saved is not None:
                LOG.info("image generated via %s", name)
                return saved
        except Exception as exc:  # noqa: BLE001
            LOG.warning("image provider %s failed: %s", name, exc)
    return None


def generate_video_file(
    db: Session,
    *,
    prompt: str,
    dest: Path,
    image_url: str | None = None,
) -> Path | None:
    """توليد فيديو عبر Higgsfield إن وُجد المفتاح."""
    hg = marketing_higgsfield_settings(db)
    if not hg.get("api_key"):
        return None
    safe_image_url = None
    if image_url:
        if not is_safe_http_url(image_url, allow_http=False):
            LOG.warning("blocked unsafe image_url for video: %s", image_url[:120])
            return None
        safe_image_url = image_url.strip()
    return _generate_higgsfield_video(hg, prompt=prompt, dest=dest, image_url=safe_image_url)


def _generate_openai_image(cfg: dict[str, str], *, prompt: str, dest: Path) -> Path | None:
    try:
        base = _assert_api_base_url(cfg["base_url"])
    except ValueError as exc:
        LOG.warning("openai image blocked base_url: %s", exc)
        return None
    payload = {
        "model": cfg["model"],
        "prompt": prompt[:3500],
        "n": 1,
        "size": cfg["size"],
        "response_format": "b64_json",
    }
    req = urllib.request.Request(
        f"{base}/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120, context=_ssl_context()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        b64 = raw["data"][0]["b64_json"]
    except Exception as exc:
        LOG.warning("openai image generation failed: %s", exc)
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(b64))
    return dest


def _generate_fal_image(cfg: dict[str, str], *, prompt: str, dest: Path) -> Path | None:
    """POST https://fal.run/{model_id}  Authorization: Key …"""
    model_id = (cfg.get("model") or "fal-ai/flux/schnell").strip().lstrip("/")
    url = f"https://fal.run/{quote(model_id, safe='/')}"
    # أحجام شائعة لـ Flux — اختياري
    size = (cfg.get("size") or "1024x1024").replace("×", "x")
    image_size = "square_hd"
    if size in ("1792x1024", "1024x1792"):
        image_size = "landscape_16_9" if size.startswith("1792") else "portrait_16_9"
    payload: dict[str, Any] = {
        "prompt": prompt[:3500],
        "image_size": image_size,
        "num_images": 1,
        "num_inference_steps": 8 if "schnell" in model_id else 28,
        "enable_safety_checker": True,
        # بعض موديلات fal تتجاهله — لا يضر إن وُجد
        "negative_prompt": (
            "text, letters, words, typography, watermark, logo, caption, "
            "arabic text, calligraphy, writing, signboard, subtitle, garbled text"
        ),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Key {cfg['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180, context=_ssl_context()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        LOG.warning("fal.ai image generation failed: %s", exc)
        return None

    image_url = _extract_fal_image_url(raw)
    if not image_url:
        LOG.warning("fal.ai response without image url: %s", str(raw)[:400])
        return None
    if not is_safe_http_url(image_url, allow_http=False):
        LOG.warning("blocked unsafe fal image_url: %s", image_url[:120])
        return None
    try:
        img_req = urllib.request.Request(image_url, method="GET")
        with urllib.request.urlopen(img_req, timeout=90, context=_ssl_context()) as resp:
            data = resp.read()
    except Exception as exc:
        LOG.warning("fal.ai image download failed: %s", exc)
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def _generate_higgsfield_image(cfg: dict[str, str], *, prompt: str, dest: Path) -> Path | None:
    """Higgsfield platform — Authorization: Key key_id:secret."""
    import time

    model_id = (cfg.get("model") or "bytedance/seedream/v4/text-to-image").strip().lstrip("/")
    try:
        base = _assert_api_base_url(cfg.get("base_url") or "https://platform.higgsfield.ai")
    except ValueError as exc:
        LOG.warning("higgsfield image blocked base_url: %s", exc)
        return None
    url = f"{base}/{quote(model_id, safe='/')}"
    payload: dict[str, Any] = {
        "prompt": prompt[:3500],
        "aspect_ratio": "1:1",
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Key {cfg['api_key']}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180, context=_ssl_context()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        LOG.warning("higgsfield image submit failed: %s", exc)
        return None

    image_url = _extract_fal_image_url(raw) or _extract_media_url(raw, kind="image")
    if not image_url:
        # طابور / طلب غير متزامن
        req_id = (
            raw.get("request_id")
            or raw.get("id")
            or (raw.get("data") or {}).get("request_id")
            if isinstance(raw.get("data"), dict)
            else None
        )
        if req_id:
            image_url = _higgsfield_poll_media(
                cfg, request_id=str(req_id), kind="image", timeout_sec=120
            )
    if not image_url:
        LOG.warning("higgsfield image response without url: %s", str(raw)[:400])
        return None
    return _download_to(dest, image_url)


def _generate_higgsfield_video(
    cfg: dict[str, str],
    *,
    prompt: str,
    dest: Path,
    image_url: str | None = None,
) -> Path | None:
    import time

    model_id = (cfg.get("video_model") or "higgsfield-ai/soul/standard").strip().lstrip("/")
    try:
        base = _assert_api_base_url(cfg.get("base_url") or "https://platform.higgsfield.ai")
    except ValueError as exc:
        LOG.warning("higgsfield video blocked base_url: %s", exc)
        return None
    url = f"{base}/{quote(model_id, safe='/')}"
    payload: dict[str, Any] = {"prompt": prompt[:2000]}
    if image_url:
        payload["image_url"] = image_url
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Key {cfg['api_key']}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180, context=_ssl_context()) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        LOG.warning("higgsfield video submit failed: %s", exc)
        return None

    media_url = _extract_media_url(raw, kind="video")
    if not media_url:
        req_id = raw.get("request_id") or raw.get("id")
        if req_id:
            media_url = _higgsfield_poll_media(
                cfg, request_id=str(req_id), kind="video", timeout_sec=240
            )
    if not media_url:
        LOG.warning("higgsfield video response without url: %s", str(raw)[:400])
        return None
    return _download_to(dest, media_url)


def _higgsfield_poll_media(
    cfg: dict[str, str],
    *,
    request_id: str,
    kind: str,
    timeout_sec: int = 120,
) -> str | None:
    import time

    try:
        base = _assert_api_base_url(cfg.get("base_url") or "https://platform.higgsfield.ai")
    except ValueError as exc:
        LOG.warning("higgsfield poll blocked base_url: %s", exc)
        return None
    status_url = f"{base}/requests/{quote(request_id, safe='')}/status"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        req = urllib.request.Request(
            status_url,
            headers={"Authorization": f"Key {cfg['api_key']}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=45, context=_ssl_context()) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            LOG.warning("higgsfield poll failed: %s", exc)
            time.sleep(3)
            continue
        status = str(raw.get("status") or raw.get("state") or "").lower()
        url = _extract_media_url(raw, kind=kind)
        if url:
            return url
        if status in ("failed", "error", "cancelled"):
            return None
        time.sleep(3)
    return None


def _extract_media_url(raw: dict[str, Any], *, kind: str = "image") -> str | None:
    if not isinstance(raw, dict):
        return None
    # أشكال شائعة
    for key in ("video_url", "url", "video"):
        if kind == "video" and isinstance(raw.get(key), str) and raw[key].startswith("http"):
            return str(raw[key])
    videos = raw.get("videos") or raw.get("video")
    if isinstance(videos, list) and videos:
        v0 = videos[0]
        if isinstance(v0, dict) and v0.get("url"):
            return str(v0["url"])
        if isinstance(v0, str) and v0.startswith("http"):
            return v0
    if kind == "image":
        return _extract_fal_image_url(raw)
    # تداخل data/result
    for nest in ("data", "result", "output"):
        nested = raw.get(nest)
        if isinstance(nested, dict):
            found = _extract_media_url(nested, kind=kind)
            if found:
                return found
    return None


def _download_to(dest: Path, url: str) -> Path | None:
    try:
        safe_url = _assert_download_url(url)
    except ValueError as exc:
        LOG.warning("media download blocked: %s", exc)
        return None
    try:
        img_req = urllib.request.Request(safe_url, method="GET")
        with urllib.request.urlopen(img_req, timeout=120, context=_ssl_context()) as resp:
            data = resp.read()
    except Exception as exc:
        LOG.warning("media download failed: %s", exc)
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def _extract_fal_image_url(raw: dict[str, Any]) -> str | None:
    images = raw.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"])
        if isinstance(first, str) and first.startswith("http"):
            return first
    # بعض الموديلات ترجع image.url
    image = raw.get("image")
    if isinstance(image, dict) and image.get("url"):
        return str(image["url"])
    if isinstance(image, str) and image.startswith("http"):
        return image
    return None


def _parse_json_object(text: str) -> dict[str, Any] | None:
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:].strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(t[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
