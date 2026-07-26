"""تأكيد انتهاء التنظيف عبر رابط واتساب (عام — بدون تسجيل دخول)."""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.deps import DBSession
from modules.hotel.booking_service import BookingError, mark_room_clean
from modules.hotel.dashboard import room_display_name
from modules.hotel.housekeeping_links import verify_housekeeping_done_token
from modules.hotel.models import HotelRoom

hk_public_router = APIRouter(
    prefix="/api/hotel/housekeeping", tags=["hotel-hk-public"]
)


def _page(title: str, body: str, *, ok: bool) -> HTMLResponse:
    color = "#065f46" if ok else "#991b1b"
    bg = "#ecfdf5" if ok else "#fef2f2"
    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{ font-family: Tahoma, Segoe UI, sans-serif; margin: 0; padding: 1.5rem;
           background: #f8fafc; color: #0f172a; }}
    .card {{ max-width: 420px; margin: 2rem auto; padding: 1.25rem 1.4rem;
             border-radius: 14px; background: {bg}; border: 1px solid {color}33; }}
    h1 {{ margin: 0 0 0.5rem; font-size: 1.15rem; color: {color}; }}
    p {{ margin: 0.35rem 0; line-height: 1.55; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    {body}
  </div>
</body>
</html>"""
    return HTMLResponse(html, status_code=200 if ok else 400)


@hk_public_router.get("/done/{room_id}", response_class=HTMLResponse)
def housekeeping_done_link(
    room_id: int,
    db: DBSession,
    t: str = Query("", max_length=80),
):
    if not verify_housekeeping_done_token(db, room_id, t):
        return _page(
            "رابط غير صالح",
            "<p>هذا الرابط غير صحيح أو منتهي. اطلب إعادة إرسال مهمة التنظيف من النظام.</p>",
            ok=False,
        )
    room = db.get(HotelRoom, room_id)
    if room is None:
        return _page("الشقة غير موجودة", "<p>تعذّر العثور على الشقة.</p>", ok=False)
    name = room_display_name(room)
    try:
        mark_room_clean(db, room_id, user_id=None)
        db.commit()
    except BookingError as exc:
        db.rollback()
        # إن كانت متاحة مسبقاً نعرض نجاحاً لتجنّب لبس العامل
        if "ليست بحاجة" in str(exc) or "جاهز" in str(exc):
            return _page(
                "الشقة جاهزة ✓",
                f"<p><strong>{name}</strong> (#{room.number}) متاحة للحجز مسبقاً.</p>",
                ok=True,
            )
        return _page("تعذّر التحديث", f"<p>{exc}</p>", ok=False)
    return _page(
        "تم تسجيل انتهاء التنظيف ✓",
        f"<p>الشقة <strong>{name}</strong> (#{room.number}) أصبحت <strong>متاحة للحجز</strong>.</p>"
        "<p>يمكنك إغلاق هذه الصفحة.</p>",
        ok=True,
    )
