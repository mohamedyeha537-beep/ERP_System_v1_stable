"""تسميات عربية لحالات الحجز وعروض الأسعار."""
from __future__ import annotations

from modules.hotel.booking_models import GuestType, QuotationStatus, RecordKind

GUEST_TYPE_LABELS = {
    GuestType.INDIVIDUAL: "فرد",
    GuestType.COMPANY: "شركة",
}

RECORD_KIND_LABELS = {
    RecordKind.BOOKING: "حجز",
    RecordKind.QUOTATION: "عرض سعر",
}

QUOTATION_STATUS_LABELS = {
    QuotationStatus.DRAFT.value: "مسودة",
    QuotationStatus.SENT.value: "مُرسَل للشركة",
    QuotationStatus.UNDER_REVIEW.value: "قيد المراجعة",
    QuotationStatus.ACCEPTED.value: "موافقة الشركة",
    QuotationStatus.REJECTED.value: "مرفوض",
    QuotationStatus.EXPIRED.value: "منتهي الصلاحية",
    QuotationStatus.CONVERTED.value: "تحوّل إلى حجز",
}

QUOTATION_STATUS_COLORS = {
    QuotationStatus.DRAFT.value: "#64748b",
    QuotationStatus.SENT.value: "#2563eb",
    QuotationStatus.UNDER_REVIEW.value: "#d97706",
    QuotationStatus.ACCEPTED.value: "#16a34a",
    QuotationStatus.REJECTED.value: "#dc2626",
    QuotationStatus.EXPIRED.value: "#9ca3af",
    QuotationStatus.CONVERTED.value: "#059669",
}

QUOTATION_NEXT_STATUSES = {
    QuotationStatus.DRAFT: (QuotationStatus.SENT, QuotationStatus.REJECTED),
    QuotationStatus.SENT: (
        QuotationStatus.UNDER_REVIEW,
        QuotationStatus.ACCEPTED,
        QuotationStatus.REJECTED,
        QuotationStatus.EXPIRED,
    ),
    QuotationStatus.UNDER_REVIEW: (
        QuotationStatus.ACCEPTED,
        QuotationStatus.REJECTED,
        QuotationStatus.EXPIRED,
    ),
    QuotationStatus.ACCEPTED: (QuotationStatus.CONVERTED,),
    QuotationStatus.REJECTED: (),
    QuotationStatus.EXPIRED: (),
    QuotationStatus.CONVERTED: (),
}


def guest_type_label(value) -> str:
    if isinstance(value, str):
        try:
            value = GuestType(value)
        except ValueError:
            return value
    return GUEST_TYPE_LABELS.get(value, str(value))


def quotation_status_label(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, QuotationStatus):
        value = value.value
    if isinstance(value, str):
        try:
            value = QuotationStatus(value).value
        except ValueError:
            return value
    return QUOTATION_STATUS_LABELS.get(value, str(value))