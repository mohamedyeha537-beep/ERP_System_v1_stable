"""اختبارات أمنية للمرحلة A: uploads IDOR، SSRF webhooks، CSRF متصفح مقابل API."""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

_DB_NAME = f"./tests/test_security_phase_a_{uuid.uuid4().hex[:8]}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_NAME}"
os.environ.setdefault("SECRET_KEY", "test-secret-key-32-characters-long")
os.environ.setdefault("DEFAULT_ADMIN_USERNAME", "admin")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "admin123")
os.environ.setdefault("ONLINE_SYNC_API_KEY", "sync-key-phase-a")

from app.main import create_app
from app.rate_limit import reset_rate_limits_for_tests
from infra.config import get_settings
from infra.db import get_session_factory, reset_engine
from modules.authz.models import Role, User
from modules.authz.pos_wallet_access import user_may_use_pos_bank, user_may_use_pos_cash
from modules.authz.service import hash_password
from modules.common.safe_http_url import assert_safe_http_url, is_safe_http_url


def _fresh_db() -> None:
    reset_engine()
    get_settings.cache_clear()
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
    app = create_app()
    with TestClient(app) as c:
        yield c


def _csrf(client: TestClient, path: str = "/auth/login") -> str:
    page = client.get(path)
    m = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    if not m:
        m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, f"csrf missing on {path}"
    return m.group(1)


def _login(client: TestClient, username: str = "admin", password: str = "admin123") -> None:
    r = client.post(
        "/auth/login",
        data={"username": username, "password": password, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text[:400]


def _uploads_root() -> Path:
    return Path(__file__).resolve().parents[1] / "app" / "static" / "uploads"


def _create_cashier(username: str = "cashier_phase_a", password: str = "cashier123") -> None:
    Session = get_session_factory()
    with Session() as db:
        role = db.query(Role).filter(Role.name_ar == "كاشير").one()
        existing = db.query(User).filter(User.username == username).one_or_none()
        if existing:
            existing.password_hash = hash_password(password)
            existing.is_active = True
            existing.roles = [role]
        else:
            db.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    is_active=True,
                    roles=[role],
                )
            )
        db.commit()


class TestSafeHttpUrlSsrf:
    def test_blocks_localhost_and_private(self) -> None:
        assert not is_safe_http_url("http://127.0.0.1/x", allow_http=True)
        assert not is_safe_http_url("http://localhost/hook", allow_http=True)
        assert not is_safe_http_url("http://10.0.0.5/hook", allow_http=True)
        assert not is_safe_http_url("http://192.168.1.1/hook", allow_http=True)
        assert not is_safe_http_url("http://[::1]/hook", allow_http=True)
        assert not is_safe_http_url("http://169.254.169.254/latest/meta-data", allow_http=True)
        assert not is_safe_http_url("file:///etc/passwd", allow_http=True)
        assert not is_safe_http_url("ftp://example.com/x", allow_http=True)
        with pytest.raises(ValueError):
            assert_safe_http_url("http://127.0.0.1/", allow_http=True)

    def test_blocks_ipv4_mapped_loopback(self) -> None:
        assert not is_safe_http_url("http://[::ffff:127.0.0.1]/hook", allow_http=True)
        assert not is_safe_http_url("http://[::ffff:10.1.2.3]/hook", allow_http=True)
        assert not is_safe_http_url("http://[::ffff:169.254.169.254]/meta", allow_http=True)

    def test_blocks_userinfo_and_link_local(self) -> None:
        assert not is_safe_http_url("https://user:pass@example.com/", allow_http=False)
        assert not is_safe_http_url("http://169.254.1.1/x", allow_http=True)
        assert not is_safe_http_url("http://foo.local/hook", allow_http=True)

    def test_allows_public_https(self) -> None:
        assert is_safe_http_url("https://example.com/webhook") is True

    def test_redirect_to_private_blocked(self) -> None:
        import urllib.request

        from modules.common.safe_http import _redirect_handler

        handler = _redirect_handler(allow_http=True, allowed_hosts=None)
        req = urllib.request.Request("https://example.com/x")
        with pytest.raises(ValueError):
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://127.0.0.1/secret"
            )
        with pytest.raises(ValueError):
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data"
            )
        with pytest.raises(ValueError):
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://localhost/admin"
            )


class TestDoorLockEncoderUrl:
    def test_allows_loopback_default_port(self) -> None:
        from modules.hotel.lock_cards import assert_local_encoder_base_url

        assert assert_local_encoder_base_url("http://127.0.0.1:9199") == "http://127.0.0.1:9199"
        assert assert_local_encoder_base_url("http://localhost:9200").endswith(":9200")

    def test_rejects_public_and_metadata(self) -> None:
        from modules.hotel.lock_cards import LockCardError, assert_local_encoder_base_url

        with pytest.raises(LockCardError):
            assert_local_encoder_base_url("http://example.com:9199")
        with pytest.raises(LockCardError):
            assert_local_encoder_base_url("http://169.254.169.254/")
        with pytest.raises(LockCardError):
            assert_local_encoder_base_url("https://127.0.0.1:9199")
        with pytest.raises(LockCardError):
            assert_local_encoder_base_url("http://127.0.0.1:80")
        with pytest.raises(LockCardError):
            assert_local_encoder_base_url("http://127.0.0.1:9199/proxy?url=http://x")


class TestUploadIdor:
    def test_public_product_image_ok_without_login(self, client: TestClient) -> None:
        root = _uploads_root() / "products"
        root.mkdir(parents=True, exist_ok=True)
        f = root / "phase_a_public.jpg"
        f.write_bytes(b"\xff\xd8\xff\xd9")
        try:
            r = client.get("/uploads/products/phase_a_public.jpg")
            assert r.status_code == 200
        finally:
            f.unlink(missing_ok=True)

    def test_sensitive_requires_login(self, client: TestClient) -> None:
        root = _uploads_root() / "hotel" / "guest_documents"
        root.mkdir(parents=True, exist_ok=True)
        f = root / "orphan_doc.pdf"
        f.write_bytes(b"%PDF-1.4")
        try:
            r = client.get("/uploads/hotel/guest_documents/orphan_doc.pdf")
            assert r.status_code == 401
        finally:
            f.unlink(missing_ok=True)

    def test_logged_in_without_resource_link_gets_404(self, client: TestClient) -> None:
        _login(client)
        root = _uploads_root() / "hotel" / "guest_documents"
        root.mkdir(parents=True, exist_ok=True)
        f = root / "unlinked_doc.pdf"
        f.write_bytes(b"%PDF-1.4")
        try:
            r = client.get("/uploads/hotel/guest_documents/unlinked_doc.pdf")
            assert r.status_code == 404
        finally:
            f.unlink(missing_ok=True)

    def test_cashier_cannot_access_guest_documents(self, client: TestClient) -> None:
        """كاشير بلا صلاحيات فندق — لا يصل لوثائق الضيوف حتى مع معرفة المسار."""
        _create_cashier()
        _login(client, "cashier_phase_a", "cashier123")
        root = _uploads_root() / "hotel" / "guest_documents"
        root.mkdir(parents=True, exist_ok=True)
        f = root / "other_tenant_doc.pdf"
        f.write_bytes(b"%PDF-1.4")
        try:
            r = client.get("/uploads/hotel/guest_documents/other_tenant_doc.pdf")
            assert r.status_code in (401, 403, 404)
        finally:
            f.unlink(missing_ok=True)

    def test_cashier_cannot_access_receipts_folder(self, client: TestClient) -> None:
        _create_cashier()
        _login(client, "cashier_phase_a", "cashier123")
        root = _uploads_root() / "receipts"
        root.mkdir(parents=True, exist_ok=True)
        f = root / "secret_receipt.pdf"
        f.write_bytes(b"%PDF-1.4")
        try:
            r = client.get("/uploads/receipts/secret_receipt.pdf")
            assert r.status_code in (401, 403, 404)
        finally:
            f.unlink(missing_ok=True)

    def test_path_traversal_blocked(self, client: TestClient) -> None:
        r = client.get("/uploads/../csrf.py")
        assert r.status_code in (404, 401)
        r2 = client.get("/uploads/products/../../uploads_router.py")
        assert r2.status_code in (404, 401)


class TestWalletAclFailClosed:
    def test_broken_user_attrs_deny(self) -> None:
        class Broken:
            @property
            def pos_show_cash(self):
                raise RuntimeError("boom")

            @property
            def pos_show_bank(self):
                raise RuntimeError("boom")

        u = Broken()
        assert user_may_use_pos_cash(u) is False
        assert user_may_use_pos_bank(u) is False

    def test_explicit_flags(self) -> None:
        assert user_may_use_pos_cash(SimpleNamespace(pos_show_cash=False)) is False
        assert user_may_use_pos_bank(SimpleNamespace(pos_show_bank=True)) is True
        assert user_may_use_pos_cash(None) is True


class TestCsrfBrowserVsApi:
    def test_login_still_works_without_csrf(self, client: TestClient) -> None:
        # مسار الدخول معفى — جسم بلا csrf_token يجب ألا يكسر تسجيل الدخول
        client.get("/auth/login")
        r = client.post(
            "/auth/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_login_works_with_csrf_token(self, client: TestClient) -> None:
        _login(client)
        # جلسة صالحة بعد الدخول
        home = client.get("/", follow_redirects=False)
        assert home.status_code in (200, 302, 303)

    def test_browser_form_requires_csrf(self, client: TestClient) -> None:
        _login(client)
        r = client.post("/admin/settings", data={}, follow_redirects=False)
        assert r.status_code in (403, 303, 302)

    def test_sync_api_with_key_still_works(self, client: TestClient) -> None:
        r = client.get(
            "/api/sync/pull",
            headers={"X-Sync-API-Key": "sync-key-phase-a"},
        )
        assert r.status_code != 403 or "csrf" not in (r.text or "").lower()
        bad = client.get("/api/sync/pull", headers={"X-Sync-API-Key": "wrong"})
        assert bad.status_code in (401, 403, 404, 405, 422, 500)

    def test_session_api_seo_without_csrf_blocked(self, client: TestClient) -> None:
        _login(client)
        r = client.post(
            "/api/seo/fixes/1/approve",
            json={},
            follow_redirects=False,
        )
        assert r.status_code in (403, 303)
        if r.status_code == 303:
            loc = r.headers.get("location") or ""
            assert loc in ("/", "/auth/login?e=csrf") or loc.startswith("/")
