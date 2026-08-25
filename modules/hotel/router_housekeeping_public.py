"""تأكيد انتهاء التنظيف عبر رابط واتساب (عام — بدون تسجيل دخول)."""
from __future__ import annotations

import html as html_module

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse

from app.deps import DBSession
from modules.hotel.booking_service import BookingError, mark_room_clean
from modules.hotel.dashboard import room_display_name
from modules.hotel.housekeeping_links import verify_housekeeping_done_token
from modules.hotel.models import HotelRoom

hk_public_router = APIRouter(
    prefix="/api/hotel/housekeeping", tags=["hotel-hk-public"]
)


def _page(title: str, body: str, *, ok: bool, status_code: int | None = None) -> HTMLResponse:
    color = "#065f46" if ok else "#991b1b"
    bg = "#ecfdf5" if ok else "#fef2f2"
    if status_code is None:
        status_code = 200 if ok else 400
    safe_title = html_module.escape(title)
    html = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    body {{ font-family: Tahoma, Segoe UI, sans-serif; margin: 0; padding: 1.5rem;
           background: #f8fafc; color: #0f172a; }}
    .card {{ max-width: 420px; margin: 2rem auto; padding: 1.25rem 1.4rem;
             border-radius: 14px; background: {bg}; border: 1px solid {color}33; }}
    h1 {{ margin: 0 0 0.5rem; font-size: 1.15rem; color: {color}; }}
    p {{ margin: 0.35rem 0; line-height: 1.55; }}
    .actions {{ display: flex; flex-direction: column; gap: 0.65rem; margin-top: 1.1rem; }}
    .btn {{ display: block; width: 100%; text-align: center; box-sizing: border-box;
            padding: 0.85rem 1rem; border-radius: 10px; border: none;
            font-size: 1rem; font-weight: 700; cursor: pointer; text-decoration: none; }}
    .btn-yes {{ background: #047857; color: #fff; }}
    .btn-no {{ background: #e2e8f0; color: #334155; }}
    .warn {{ color: #92400e; background: #fffbeb; border: 1px solid #fcd34d;
             border-radius: 10px; padding: 0.75rem 0.9rem; margin-top: 0.75rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{safe_title}</h1>
    {body}
  </div>
</body>
</html>"""
    return HTMLResponse(html, status_code=status_code)


def _confirm_page(*, room_id: int, token: str, name: str, number: str) -> HTMLResponse:
    safe_name = html_module.escape(name)
    safe_number = html_module.escape(number)
    safe_token = html_module.escape(token)
    body = f"""
    <p>الشقة: <strong>{safe_name}</strong> (#{safe_number})</p>
    <div class="actions">
      <form method="post" action="/api/hotel/housekeeping/done/{int(room_id)}">
        <input type="hidden" name="t" value="{safe_token}" />
        <button class="btn btn-yes" type="submit">نعم — انتهى التنظيف</button>
      </form>
      <a class="btn btn-no" href="about:blank" onclick="window.close();return false;">إلغاء — لم ينتهِ بعد</a>
    </div>
    """
    return _page("تأكيد انتهاء التنظيف", body, ok=True, status_code=200)


@hk_public_router.get("/done/{room_id}", response_class=HTMLResponse)
def housekeeping_done_link(
    room_id: int,
    db: DBSession,
    t: str = Query("", max_length=120),
):
    """عرض سؤال التأكيد أولاً — لا يُحدَّث شيء قبل الضغط على «نعم»."""
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
    return _confirm_page(
        room_id=room_id,
        token=(t or "").strip(),
        name=name,
        number=str(room.number),
    )


@hk_public_router.post("/done/{room_id}", response_class=HTMLResponse)
def housekeeping_done_confirm(
    room_id: int,
    db: DBSession,
    t: str = Form("", max_length=120),
):
    """تنفيذ التأكيد بعد موافقة الموظف صراحة."""
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
    safe_name = html_module.escape(name)
    safe_number = html_module.escape(str(room.number))
    try:
        mark_room_clean(db, room_id, user_id=None)
        db.commit()
    except BookingError as exc:
        db.rollback()
        if "ليست بحاجة" in str(exc) or "جاهز" in str(exc):
            return _page(
                "الشقة جاهزة ✓",
                f"<p><strong>{safe_name}</strong> (#{safe_number}) متاحة للحجز مسبقاً.</p>",
                ok=True,
            )
        return _page(
            "تعذّر التحديث",
            f"<p>{html_module.escape(str(exc))}</p>",
            ok=False,
        )
    return _page(
        "تم تسجيل انتهاء التنظيف ✓",
        f"<p>الشقة <strong>{safe_name}</strong> (#{safe_number}) أصبحت <strong>متاحة للحجز</strong>.</p>"
        "<p>يمكنك إغلاق هذه الصفحة.</p>",
        ok=True,
    )
