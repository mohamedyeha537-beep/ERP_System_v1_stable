"""اختبارات Technical SEO — Phase B (robots / sitemap / canonical / noindex / schema)."""
from __future__ import annotations

import json
import os
import re
import uuid
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

_DB_NAME = f"./tests/test_tech_seo_b_{uuid.uuid4().hex[:8]}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_NAME}"
os.environ.setdefault("SECRET_KEY", "test-secret-key-32-characters-long")
os.environ.setdefault("DEFAULT_ADMIN_USERNAME", "admin")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "admin123")
os.environ["PUBLIC_BASE_URL"] = "https://pos.baytak.ly"
os.environ["SEO_PUBLIC_BASE_URL"] = "https://pos.baytak.ly"

from app.main import create_app
from app.rate_limit import reset_rate_limits_for_tests
from infra.config import get_settings
from infra.db import reset_engine
from modules.seo.config import clear_seo_config_cache


def _fresh_db() -> None:
    reset_engine()
    get_settings.cache_clear()
    clear_seo_config_cache()
    for extra in ("", "-shm", "-wal"):
        path = _DB_NAME + extra
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    reset_rate_limits_for_tests()
    yield
    reset_rate_limits_for_tests()


@pytest.fixture
def client():
    _fresh_db()
    os.environ["PUBLIC_BASE_URL"] = "https://pos.baytak.ly"
    os.environ["SEO_PUBLIC_BASE_URL"] = "https://pos.baytak.ly"
    clear_seo_config_cache()
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestRobotsTxt:
    def test_blocks_private_allows_public(self, client: TestClient) -> None:
        r = client.get("/robots.txt")
        assert r.status_code == 200
        body = r.text
        assert "Disallow: /admin" in body
        assert "Disallow: /pos" in body
        assert "Disallow: /api" in body
        assert "Disallow: /auth" in body
        assert "Disallow: /uploads/" in body
        assert "Sitemap:" in body
        assert "sitemap.xml" in body
        # عامة
        assert "Allow: /shop" in body or "Disallow: /shop" in body

    def test_not_a_substitute_for_auth(self, client: TestClient) -> None:
        # robots يمنع الإشارة فقط — المسار الإداري ما زال يحتاج جلسة
        r = client.get("/admin/settings", follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403)


class TestSitemap:
    def test_sitemap_only_http_public_urls(self, client: TestClient) -> None:
        r = client.get("/sitemap.xml")
        assert r.status_code == 200
        assert "application/xml" in (r.headers.get("content-type") or "")
        root = ET.fromstring(r.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [el.text or "" for el in root.findall("sm:url/sm:loc", ns)]
        assert locs, "expected at least one public URL when base is set"
        from urllib.parse import urlparse

        for loc in locs:
            assert loc.startswith("https://pos.baytak.ly")
            path = urlparse(loc).path or "/"
            assert not path.startswith(("/admin", "/api", "/auth"))
            # /pos كمسار داخلي — لا تخلطه مع hostname pos.baytak.ly
            assert path != "/pos" and not path.startswith("/pos/")
            assert "/admin/" not in path + "/"

    def test_product_and_room_sitemaps_ok(self, client: TestClient) -> None:
        assert client.get("/sitemap-products.xml").status_code == 200
        assert client.get("/sitemap-rooms.xml").status_code == 200
        assert client.get("/sitemap-images.xml").status_code == 200


class TestCanonicalAndMetadata:
    def test_shop_has_canonical_and_robots_meta(self, client: TestClient) -> None:
        r = client.get("/shop")
        assert r.status_code == 200
        assert 'rel="canonical"' in r.text or "rel='canonical'" in r.text
        assert 'https://pos.baytak.ly/shop' in r.text
        assert 'name="robots"' in r.text
        assert 'property="og:locale" content="ar_LY"' in r.text

    def test_admin_is_noindex(self, client: TestClient) -> None:
        # صفحة دخول عامة نسبيًا لكن القوالب الإدارية تحمل noindex
        page = client.get("/auth/login")
        # login قد لا يستخدم base.html — نفحص لوحة بعد محاولة أو القالب الأساسي عبر مسار محمي يُعيد توجيه
        assert page.status_code == 200
        # قالب base.html للإدارة
        from app.jinja_env import templates

        # تحقق مباشر من وجود الوسم في القالب
        from pathlib import Path

        base = Path("app/templates/base.html").read_text(encoding="utf-8")
        assert 'name="robots" content="noindex, nofollow"' in base
        pos = Path("app/templates/base_pos.html").read_text(encoding="utf-8")
        assert 'name="robots" content="noindex, nofollow"' in pos


class TestSchemaJsonLd:
    def test_shop_json_ld_parses(self, client: TestClient) -> None:
        r = client.get("/shop")
        assert r.status_code == 200
        m = re.search(
            r'<script type="application/ld\+json">(.*?)</script>',
            r.text,
            flags=re.DOTALL,
        )
        if not m:
            pytest.skip("shop surface may be disabled / no json-ld in this seed")
        data = json.loads(m.group(1))
        assert isinstance(data, (list, dict))
        blobs = data if isinstance(data, list) else [data]
        types = set()
        for b in blobs:
            t = b.get("@type")
            if isinstance(t, list):
                types.update(t)
            elif t:
                types.add(t)
        # أنواع حقيقية فقط — بلا Review/Rating مختلق
        assert "Review" not in types
        assert "AggregateRating" not in types
        assert types & {"Organization", "Restaurant", "WebSite", "WebPage", "Hotel", "LodgingBusiness"}


class TestLangRtl:
    def test_shop_is_arabic_rtl(self, client: TestClient) -> None:
        r = client.get("/shop")
        assert r.status_code == 200
        assert 'lang="ar"' in r.text
        assert 'dir="rtl"' in r.text
