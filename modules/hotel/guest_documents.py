"""رفع وثائق هوية النزلاء مع الحجز."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from modules.hotel.booking_models import HotelBooking
from modules.hotel.booking_service import BookingError
from modules.hotel.uploads import save_guest_id_document

_STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "static"


def _row_value(values: list, index: int) -> str:
    if index >= len(values):
        return ""
    return str(values[index] or "").strip()


def _as_upload(value) -> UploadFile | None:
    """request.form() يعيد starlette.UploadFile وليس fastapi.UploadFile."""
    if isinstance(value, UploadFile):
        return value
    return None


def _row_has_partial_data(
    *,
    name: str,
    id_number: str,
    id_type: str,
    nationality: str,
    phone: str,
    address: str,
    file: UploadFile | None,
) -> bool:
    if name:
        return True
    if id_number or id_type or nationality or phone or address:
        return True
    return bool(file and (file.filename or "").strip())


def validate_staying_guests_form(form, *, require_documents: bool = True) -> None:
    """يتحقق من اكتمال بيانات النزلاء ووثائقهم قبل إنشاء الحجز."""
    names = form.getlist("staying_guest_name")
    id_numbers = form.getlist("staying_guest_id_number")
    id_types = form.getlist("staying_guest_id_type")
    nationalities = form.getlist("staying_guest_nationality")
    addresses = form.getlist("staying_guest_address")
    phones = form.getlist("staying_guest_phone")
    files = form.getlist("staying_guest_id_document")

    guests_found = 0
    for i, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        id_number = _row_value(id_numbers, i)
        id_type = _row_value(id_types, i)
        nationality = _row_value(nationalities, i)
        phone = _row_value(phones, i)
        address = _row_value(addresses, i)
        upload = _as_upload(files[i] if i < len(files) else None)

        if not _row_has_partial_data(
            name=name,
            id_number=id_number,
            id_type=id_type,
            nationality=nationality,
            phone=phone,
            address=address,
            file=upload,
        ):
            continue

        guests_found += 1
        label = f"نزيل {guests_found}"

        if not name:
            raise BookingError(f"{label}: الاسم الكامل مطلوب.")
        if not id_number:
            raise BookingError(f"{label} ({name}): رقم الهوية / الجواز مطلوب.")
        if not id_type:
            raise BookingError(f"{label} ({name}): نوع الوثيقة مطلوب.")
        if not nationality:
            raise BookingError(f"{label} ({name}): الجنسية مطلوبة.")
        if not phone:
            raise BookingError(f"{label} ({name}): الهاتف مطلوب.")
        if not address:
            raise BookingError(f"{label} ({name}): قادم من مطلوب.")
        if require_documents and (upload is None or not (upload.filename or "").strip()):
            raise BookingError(f"{label} ({name}): إرفاق صورة/مسح الوثيقة مطلوب.")

    if guests_found == 0:
        raise BookingError("أدخل نزيلاً واحداً على الأقل في الشقة مع بياناته كاملة.")


def attach_staying_guest_documents(
    db: Session,
    booking: HotelBooking,
    form,
    *,
    require_documents: bool = True,
) -> None:
    """يربط ملفات staying_guest_id_document بالنزلاء حسب ترتيب الصفوف."""
    names = form.getlist("staying_guest_name")
    files = form.getlist("staying_guest_id_document")
    guests = sorted(booking.guests, key=lambda g: g.id)
    gi = 0
    for i, raw_name in enumerate(names):
        if not (raw_name or "").strip():
            continue
        if gi >= len(guests):
            break
        guest = guests[gi]
        gi += 1
        if i >= len(files):
            continue
        upload = _as_upload(files[i])
        if upload is None or not (upload.filename or "").strip():
            continue
        guest.id_document_filename = save_guest_id_document(upload, _STATIC_ROOT)
    db.flush()
    if require_documents:
        missing = [g.full_name for g in guests if not (g.id_document_filename or "").strip()]
        if missing:
            raise BookingError(
                "إرفاق وثيقة الهوية مطلوب لكل نزيل: " + "، ".join(missing[:5])
            )
