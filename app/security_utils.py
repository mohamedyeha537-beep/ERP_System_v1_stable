"""أدوات حماية مشتركة: allowlist IP وتوقيع HMAC."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
from pathlib import Path

from fastapi import HTTPException, Request, status


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client and request.client.host:
        return request.client.host
    return ""


def parse_ip_allowlist(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


def ip_allowed(client: str, allowlist: list[str]) -> bool:
    """إن كانت القائمة فارغة → السماح للجميع. وإلا يجب تطابق IP أو شبكة CIDR."""
    if not allowlist:
        return True
    host = (client or "").strip()
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host in allowlist
    for entry in allowlist:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            if host == entry:
                return True
    return False


def require_ip_allowlist(request: Request, allowlist_raw: str | None, *, label: str = "API") -> None:
    allowlist = parse_ip_allowlist(allowlist_raw)
    if not allowlist:
        return
    if not ip_allowed(client_ip(request), allowlist):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"عنوان IP غير مسموح لـ {label}.",
        )


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_hmac_header(secret: str, body: bytes, provided: str | None) -> bool:
    expected = hmac_sha256_hex(secret, body)
    got = (provided or "").strip().lower()
    if got.startswith("sha256="):
        got = got[7:]
    if not got:
        return False
    return hmac.compare_digest(expected, got)


def sign_file(path: Path, secret: str) -> Path:
    """يكتب ملف توقيع بجانب النسخة: name.sig"""
    data = path.read_bytes()
    sig = hmac_sha256_hex(secret, data)
    sig_path = path.with_suffix(path.suffix + ".sig")
    sig_path.write_text(sig + "\n", encoding="utf-8")
    return sig_path


def verify_file_signature(path: Path, secret: str, *, required: bool = False) -> None:
    """يتحقق من .sig بجانب الملف. إن required=True يرفض غياب التوقيع."""
    sig_path = path.with_suffix(path.suffix + ".sig")
    if not sig_path.is_file():
        alt = Path(str(path) + ".sig")
        if alt.is_file():
            sig_path = alt
        elif required:
            raise RuntimeError("ملف التوقيع (.sig) مفقود — رُفضت الاستعادة.")
        else:
            return
    expected = sig_path.read_text(encoding="utf-8").strip().split()[0].lower()
    actual = hmac_sha256_hex(secret, path.read_bytes())
    if not hmac.compare_digest(expected, actual):
        raise RuntimeError("توقيع النسخة الاحتياطية غير صالح — قد يكون الملف معدّلاً.")
