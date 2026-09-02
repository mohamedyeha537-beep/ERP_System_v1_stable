"""Resource-level authorization for /uploads sensitive files."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from modules.authz.models import User
from modules.authz.permissions import (
    ADMIN_SETTINGS,
    CUSTOMERS_MANAGE,
    CUSTOMERS_VIEW,
    HOTEL_BOOKING_CREATE,
    HOTEL_BOOKING_MANAGE,
    HOTEL_BOOKING_VIEW,
    HOTEL_DEBTS_COLLECT,
    HOTEL_DEBTS_VIEW,
    MESSAGING_MANAGE,
    MESSAGING_VIEW,
    PAYMENTS_MANAGE,
    PURCHASES_MANAGE,
    PURCHASE_INVOICES_MANAGE,
    REPORTS_VIEW,
    SALES_CORRECT_PAYMENT,
    SALES_CREATE,
    SALES_EDIT_INVOICE,
    SALES_PRINT_RECEIPT,
    SALES_REFUND,
)
from modules.authz.service import user_has_permission


def normalize_upload_relative(relative: str) -> str:
    return (relative or "").strip().replace("\\", "/").lstrip("/")


def path_variants(relative: str) -> list[str]:
    """أشكال المسار كما قد تُخزَّن في قاعدة البيانات."""
    rel = normalize_upload_relative(relative)
    if not rel:
        return []
    base = Path(rel).name
    out: list[str] = []
    for candidate in (
        rel,
        base,
        f"uploads/{rel}",
        f"/uploads/{rel}",
        rel.removeprefix("uploads/"),
    ):
        c = normalize_upload_relative(candidate)
        if c and c not in out:
            out.append(c)
    return out


def _has_any(db: Session, user: User, codes: tuple[str, ...]) -> bool:
    # user_has_permission(user, code) — الصلاحيات محمّلة على كائن المستخدم
    _ = db
    return any(user_has_permission(user, code) for code in codes)


def _column_matches(column, variants: list[str]):
    clauses = []
    for v in variants:
        clauses.append(column == v)
        clauses.append(column == f"uploads/{v}")
        clauses.append(column == f"/uploads/{v}")
        base = Path(v).name
        if base and base != v:
            clauses.append(column == base)
            clauses.append(column.endswith("/" + base))
    return or_(*clauses)


def _hotel_doc_linked(db: Session, variants: list[str]) -> bool:
    from modules.hotel.booking_models import HotelBookingGuest

    q = select(HotelBookingGuest.id).where(
        _column_matches(HotelBookingGuest.id_document_filename, variants)
    ).limit(1)
    return db.execute(q).scalar_one_or_none() is not None


def _hotel_proof_linked(db: Session, variants: list[str]) -> bool:
    from modules.customers.models import CustomerWalletTransaction
    from modules.hotel.booking_models import HotelBookingPayment

    if (
        db.execute(
            select(HotelBookingPayment.id)
            .where(_column_matches(HotelBookingPayment.proof_filename, variants))
            .limit(1)
        ).scalar_one_or_none()
        is not None
    ):
        return True
    return (
        db.execute(
            select(CustomerWalletTransaction.id)
            .where(_column_matches(CustomerWalletTransaction.proof_filename, variants))
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _sale_proof_linked(db: Session, variants: list[str]) -> bool:
    from modules.payments.models import SalePayment

    return (
        db.execute(
            select(SalePayment.id)
            .where(_column_matches(SalePayment.payment_proof_image_filename, variants))
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _purchase_file_linked(db: Session, variants: list[str]) -> bool:
    from modules.payments.models import Purchase, PurchasePayment

    if (
        db.execute(
            select(Purchase.id)
            .where(
                or_(
                    _column_matches(Purchase.payment_proof_image_filename, variants),
                    _column_matches(Purchase.invoice_image_filename, variants),
                )
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    ):
        return True
    return (
        db.execute(
            select(PurchasePayment.id)
            .where(_column_matches(PurchasePayment.payment_proof_image_filename, variants))
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def user_may_access_upload(
    db: Session,
    user: User | None,
    relative: str,
) -> bool:
    """
    True إذا كان المسار عاماً أو المستخدم مخوّلاً على مستوى المورد.
    المسارات الحساسة: صلاحية فئة + وجود ربط في DB (منع تخمين UUID).
    """
    rel = normalize_upload_relative(relative)
    if not rel:
        return False

    # عام: منتجات وصور غرف
    if rel.startswith(("products/", "hotel/rooms/", "hotel/room_media/")):
        return True

    if user is None or not user.is_active:
        return False

    variants = path_variants(rel)

    if rel.startswith("hotel/guest_documents/"):
        if not _has_any(
            db,
            user,
            (HOTEL_BOOKING_VIEW, HOTEL_BOOKING_MANAGE, HOTEL_BOOKING_CREATE),
        ):
            return False
        return _hotel_doc_linked(db, variants)

    if rel.startswith("hotel/payment_proofs/"):
        if not _has_any(
            db,
            user,
            (
                HOTEL_BOOKING_VIEW,
                HOTEL_BOOKING_MANAGE,
                HOTEL_DEBTS_VIEW,
                HOTEL_DEBTS_COLLECT,
                CUSTOMERS_VIEW,
                CUSTOMERS_MANAGE,
                PAYMENTS_MANAGE,
            ),
        ):
            return False
        return _hotel_proof_linked(db, variants)

    if rel.startswith("hotel/agreement_requests/"):
        return _has_any(
            db,
            user,
            (HOTEL_BOOKING_VIEW, HOTEL_BOOKING_MANAGE, HOTEL_BOOKING_CREATE),
        )

    if rel.startswith("sale_payments/") or "/sale_payments/" in rel:
        if not _has_any(
            db,
            user,
            (
                SALES_CREATE,
                SALES_PRINT_RECEIPT,
                SALES_CORRECT_PAYMENT,
                SALES_EDIT_INVOICE,
                SALES_REFUND,
                REPORTS_VIEW,
                PAYMENTS_MANAGE,
            ),
        ):
            return False
        return _sale_proof_linked(db, variants)

    if rel.startswith("purchases/"):
        if not _has_any(db, user, (PURCHASES_MANAGE, PURCHASE_INVOICES_MANAGE, PAYMENTS_MANAGE)):
            return False
        # إن لم نجد ربطاً (مسارات قديمة) نسمح بالصلاحية فقط — لا نفتح لأي مستخدم
        return _purchase_file_linked(db, variants) or _has_any(
            db, user, (PURCHASES_MANAGE, PURCHASE_INVOICES_MANAGE)
        )

    if rel.startswith(("messaging/", "notifications/", "web_chat_guides/")):
        return _has_any(db, user, (MESSAGING_MANAGE, MESSAGING_VIEW, ADMIN_SETTINGS))

    if rel.startswith("payment_methods/"):
        return _has_any(db, user, (PAYMENTS_MANAGE, SALES_CREATE, ADMIN_SETTINGS))

    if rel.startswith("receipts/"):
        return _has_any(db, user, (PAYMENTS_MANAGE, REPORTS_VIEW, SALES_PRINT_RECEIPT))

    # غير معروف: أدمن الإعدادات فقط
    return user_has_permission(user, ADMIN_SETTINGS)
