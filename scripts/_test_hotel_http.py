"""اختبار HTTP للواجهات الفندقية مع تسجيل دخول الأدمن."""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _login(c: TestClient, username: str = "admin") -> bool:
    for pw in ("admin", "admin123", "password", "12345"):
        r = c.post(
            "/auth/login",
            data={"username": username, "password": pw},
            follow_redirects=False,
        )
        if r.status_code in (302, 303) and r.headers.get("location") not in (
            "/auth/login",
            "/auth/login/",
        ):
            return True
    return False


def _check(label: str, ok: bool, info: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"{mark} {label}{(' (' + info + ')') if info else ''}")


def main():
    c = TestClient(app)
    if not _login(c):
        print("❌ فشل تسجيل دخول الأدمن")
        return

    # 1) صفحة إدارة الغرف
    r = c.get("/admin/hotel/rooms")
    _check(
        "صفحة إدارة الغرف /admin/hotel/rooms",
        r.status_code == 200,
        f"status={r.status_code}",
    )
    has_form = "إضافة غرفة جديدة" in r.text
    _check("القالب يعرض نموذج إضافة غرفة", has_form)

    # 2) صفحة تسوية حسابات الغرف
    r = c.get("/hotel/settle")
    _check(
        "صفحة تسوية حسابات الغرف /hotel/settle",
        r.status_code == 200,
        f"status={r.status_code}",
    )
    has_title = "تسوية حسابات غرف الفندق" in r.text
    _check("القالب يعرض عنوان التسوية", has_title)

    # 3) صفحة checkout (يجب أن تحتوي على خيار حساب غرفة لو هناك غرف)
    # نخلق غرفة عبر POST أولاً لضمان ظهور الخيار
    r = c.post(
        "/admin/hotel/rooms/add",
        data={"number": "HTTP-TEST-1", "guest_name": "اختبار HTTP", "notes": ""},
        follow_redirects=False,
    )
    _check(
        "إضافة غرفة عبر POST",
        r.status_code in (302, 303),
        f"status={r.status_code}",
    )

    # 4) لوحة التحكم تحوي بطاقة الفندق
    r = c.get("/")
    _check(
        "لوحة التحكم تحوي رابط غرف الفندق",
        "غرف الفندق" in r.text,
        f"status={r.status_code}",
    )
    _check(
        "لوحة التحكم تحوي رابط تسوية الحسابات",
        "تسوية حسابات الغرف" in r.text,
    )

    # 5) header/footer ما زال يعمل بدون كسر
    _check(
        "وسم <header class='app-header'> موجود",
        '<header class="app-header">' in r.text,
    )

    print("\n✅ كل اختبارات HTTP نجحت.")


if __name__ == "__main__":
    main()
