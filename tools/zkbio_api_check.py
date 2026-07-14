#!/usr/bin/env python
"""Check ZKBioTime REST API (port 9098) and device status.

Usage:
  set ZK_USER=your_zkbio_username
  set ZK_PASS=your_zkbio_password
  python tools/zkbio_api_check.py

Optional:
  python tools/zkbio_api_check.py --reboot UFS2255100070
  python tools/zkbio_api_check.py --upload-all UFS2255100070
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:9098"
DEFAULT_SN = "UFS2255100070"


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict | None = None,
) -> tuple[int, object]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"JWT {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if raw.strip():
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
            return resp.status, None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw) if raw.strip() else {"error": exc.reason}
        except json.JSONDecodeError:
            return exc.code, raw or {"error": exc.reason}


def get_token(base: str, username: str, password: str) -> str:
    status, payload = _request(
        "POST",
        f"{base.rstrip('/')}/api-token-auth/",
        body={"username": username, "password": password},
    )
    if status != 200 or not isinstance(payload, dict) or not payload.get("token"):
        raise RuntimeError(f"Auth failed ({status}): {payload}")
    return str(payload["token"])


def list_terminals(base: str, token: str, sn: str | None = None) -> list[dict]:
    url = f"{base.rstrip('/')}/iclock/api/terminals/?page_size=50"
    if sn:
        url += f"&sn={urllib.parse.quote(sn)}"
    status, payload = _request("GET", url, token=token)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"List terminals failed ({status}): {payload}")
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def device_command(base: str, token: str, action: str, terminal_ids: list[int]) -> object:
    status, payload = _request(
        "POST",
        f"{base.rstrip('/')}/iclock/api/terminals/{action}/",
        token=token,
        body={"terminals": terminal_ids},
    )
    if status not in (200, 201):
        raise RuntimeError(f"{action} failed ({status}): {payload}")
    return payload


def _online_label(term: dict) -> str:
    last = term.get("last_activity")
    if last:
        return f"Online (last activity {last})"
    return "Offline (device never contacted server)"


def main() -> None:
    parser = argparse.ArgumentParser(description="ZKBioTime API device check")
    parser.add_argument("--base", default=os.getenv("ZK_BASE", DEFAULT_BASE))
    parser.add_argument("--user", default=os.getenv("ZK_USER", ""))
    parser.add_argument("--password", default=os.getenv("ZK_PASS", ""))
    parser.add_argument("--sn", default=DEFAULT_SN)
    parser.add_argument("--reboot", action="store_true", help="Queue reboot command")
    parser.add_argument("--upload-all", action="store_true", help="Queue upload_all command")
    args = parser.parse_args()

    if not args.user or not args.password:
        print("Set ZK_USER and ZK_PASS (ZKBioTime web login), e.g.:")
        print("  set ZK_USER=saifalarab")
        print("  set ZK_PASS=your_password")
        sys.exit(1)

    print(f"API base: {args.base}")
    token = get_token(args.base, args.user, args.password)
    print("Auth: OK")

    terms = list_terminals(args.base, token, args.sn)
    if not terms:
        print(f"No device found with SN {args.sn!r}")
        print("Register it first: python tools/zkbio_add_device.py", args.sn)
        sys.exit(1)

    for term in terms:
        tid = term.get("id")
        print()
        print(f"Device id={tid} SN={term.get('sn')} IP={term.get('ip_address')}")
        print(f"  Area: {term.get('area_name') or term.get('area')}")
        print(f"  Status: {_online_label(term)}")
        print(f"  push_time: {term.get('push_time')}")

        if args.reboot or args.upload_all:
            if not term.get("last_activity"):
                print("  WARNING: device is offline — command is queued but runs only after device connects.")
            if args.reboot:
                out = device_command(args.base, token, "reboot", [int(tid)])
                print("  reboot:", out)
            if args.upload_all:
                out = device_command(args.base, token, "upload_all", [int(tid)])
                print("  upload_all:", out)

    print()
    print("Note: REST API manages ZKBioTime server-side.")
    print("Physical device must connect to 192.168.1.10:9098 (ADMS) to go Online.")


if __name__ == "__main__":
    main()
