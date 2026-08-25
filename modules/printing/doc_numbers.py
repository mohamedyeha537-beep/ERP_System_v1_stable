"""أرقام تسلسلية للمستندات المالية المطبوعة."""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy.orm import Session

from modules.settings.service import get_setting, set_setting


class PrintDocKind(StrEnum):
    """إيصال قبض عند التحصيل · فاتورة نهائية عند الإقفال · إيصال صرف عند الصرف.

    StrEnum ضروري: مع str+Enum في Python 3.13 يكون str(member)='PrintDocKind.RECEIPT'
    فيفشل PrintDocKind(str(member)) ويكسر طباعة الإيصال/الفاتورة (HTTP 500).
    """

    RECEIPT = "receipt"  # إيصال قبض
    FINAL_INVOICE = "invoice"  # فاتورة نهائية
    DISBURSEMENT = "disbursement"  # إيصال صرف


DOC_KIND_LABELS = {
    PrintDocKind.RECEIPT: "إيصال قبض",
    PrintDocKind.FINAL_INVOICE: "فاتورة نهائية",
    PrintDocKind.DISBURSEMENT: "إيصال صرف",
}

DOC_KIND_PREFIX = {
    PrintDocKind.RECEIPT: "RCP",
    PrintDocKind.FINAL_INVOICE: "INV",
    PrintDocKind.DISBURSEMENT: "PAY",
}


def _coerce_doc_kind(kind: PrintDocKind | str) -> PrintDocKind:
    """قبول Enum أو قيمة نصية (receipt/invoice/…). str(Enum) يعطي الاسم لا القيمة في Python 3.13."""
    if isinstance(kind, PrintDocKind):
        return kind
    raw = getattr(kind, "value", None)
    if isinstance(raw, str) and raw in PrintDocKind._value2member_map_:
        return PrintDocKind(raw)
    text = str(kind or "").strip()
    if text in PrintDocKind._value2member_map_:
        return PrintDocKind(text)
    # تسامح مع "PrintDocKind.RECEIPT"
    if "." in text:
        tail = text.rsplit(".", 1)[-1].strip().lower()
        for member in PrintDocKind:
            if member.name.lower() == tail or member.value == tail:
                return member
    raise ValueError(f"نوع مستند غير معروف: {kind}")


def doc_kind_label(kind: PrintDocKind | str) -> str:
    try:
        k = _coerce_doc_kind(kind)
    except ValueError:
        return str(getattr(kind, "value", kind))
    return DOC_KIND_LABELS.get(k, k.value)


def next_doc_number(db: Session, kind: PrintDocKind | str, *, domain: str = "hotel") -> str:
    """يُصدر الرقم التالي ويحفظه في الإعدادات (تسلسل لكل نوع ومجال)."""
    k = _coerce_doc_kind(kind)
    dom = (domain or "hotel").strip().lower() or "hotel"
    key = f"print_doc_seq_{dom}_{k.value}"
    try:
        current = int((get_setting(db, key, "0") or "0").strip() or "0")
    except ValueError:
        current = 0
    nxt = current + 1
    set_setting(db, key, str(nxt))
    prefix = DOC_KIND_PREFIX[k]
    return f"{prefix}-{nxt:06d}"


def ensure_doc_number(
    db: Session,
    existing: str | None,
    kind: PrintDocKind | str,
    *,
    domain: str = "hotel",
) -> str:
    """يعيد الرقم الموجود أو يُصدر جديداً إن كان فارغاً."""
    raw = (existing or "").strip()
    if raw:
        return raw
    return next_doc_number(db, kind, domain=domain)
