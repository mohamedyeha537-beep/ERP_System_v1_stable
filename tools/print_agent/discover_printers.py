#!/usr/bin/env python3
"""
اكتشاف طابعات الشبكة (منفذ 9100 ESC/POS) على نفس شبكة الكمبيوتر.

الاستخدام:
  python discover_printers.py
  python discover_printers.py 192.168.1
  python discover_printers.py 192.168.0 --timeout 0.4

ملاحظة: أوقف VPN (Norton) قبل التشغيل حتى يُفحص كابل Ethernet وليس VPN.
"""
from __future__ import annotations

import socket
import sys
import concurrent.futures
from ipaddress import ip_network


def local_ipv4_addresses() -> list[str]:
    """عناوين IPv4 لهذا الجهاز (تقريبي عبر اتصال UDP وهمي)."""
    found: set[str] = set()
    try:
        import psutil

        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if getattr(a, "family", None) == socket.AF_INET:
                    ip = (a.address or "").strip()
                    if ip and not ip.startswith("127."):
                        found.add(ip)
    except ImportError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        found.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(found)


def guess_subnet_prefix() -> str:
    ips = local_ipv4_addresses()
    for ip in ips:
        parts = ip.split(".")
        if len(parts) == 4 and parts[0] == "192" and parts[1] == "168":
            return f"{parts[0]}.{parts[1]}.{parts[2]}"
    if ips:
        p = ips[0].split(".")
        return ".".join(p[:3]) if len(p) >= 3 else "192.168.1"
    return "192.168.1"


def probe(host: str, port: int, timeout: float) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return host
    except OSError:
        return None


def scan_subnet(prefix: str, port: int = 9100, timeout: float = 0.35, workers: int = 64) -> list[str]:
    network = ip_network(f"{prefix}.0/24", strict=False)
    hosts = [str(h) for h in network.hosts()]

    hits: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(probe, h, port, timeout): h for h in hosts}
        for fut in concurrent.futures.as_completed(futs):
            ip = fut.result()
            if ip:
                hits.append(ip)
    return sorted(hits, key=lambda x: [int(p) for p in x.split(".")])


def main() -> int:
    prefix = (sys.argv[1] if len(sys.argv) > 1 else guess_subnet_prefix()).strip()
    if prefix.count(".") == 3:
        prefix = ".".join(prefix.split(".")[:3])
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 0.35
    port = 9100

    print("=" * 60)
    print("  اكتشاف طابعات حرارية (منفذ 9100)")
    print("=" * 60)
    print()
    print("عناوين هذا الكمبيوتر:")
    for ip in local_ipv4_addresses() or ["(لم يُعثر على عنوان)"]:
        print(f"  • {ip}")
    print()
    print("تحذير: إن كان Norton VPN مفعّلاً، أوقفه أولاً ثم أعد التشغيل.")
    print(f"جاري فحص {prefix}.1 — {prefix}.254 على المنفذ {port} ...")
    print("(قد يستغرق 30–90 ثانية)")
    print()

    found = scan_subnet(prefix, port=port, timeout=timeout)

    if not found:
        print("لم يُعثر على أي جهاز يستجيب على المنفذ 9100.")
        print()
        print("تحقق من:")
        print("  1) الطابعات والكمبيوتر على نفس السويتش")
        print("  2) كل طابعة لها IP ثابت (من لوحة الطابعة أو ورقة اختبار شبكة)")
        print("  3) الكمبيوتر IP مثل 192.168.1.10 والطابعات 192.168.1.100–104")
        print("  4) VPN مغلق أثناء الفحص")
        return 1

    print(f"وُجد {len(found)} جهاز(اً) يقبل الاتصال على المنفذ 9100:")
    print()
    for i, ip in enumerate(found, 1):
        print(f"  [{i}]  {ip}:9100  ← ضع هذا في config.json ولوحة الطابعات")
    print()
    print("اقتراح مفاتيح config.json (عدّل حسب عدد الطابعات):")
    keys = ["cashier", "grill", "drinks", "salad", "pasta"]
    for ip, key in zip(found, keys):
        print(f'    "{key}": {{ "host": "{ip}", "port": 9100 }}')
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
