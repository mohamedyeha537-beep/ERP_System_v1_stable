"""استيراد بيانات العملاء من CSV (نقاط، إحالة، محفظة) بعد التصدير أو التصفير."""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from modules.customers.referral_service import ReferralError, set_customer_referral_code
from modules.customers.service import (
    CustomersError,
    adjust_points,
    adjust_wallet,
    create_customer,
    get_by_phone_any,
    is_company_customer,
    parse_customer_domain,
    require_valid_phone,
    restore_customer,
    update_customer,
)


@dataclass
class CustomerImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    points_restored: int = 0
    wallet_restored: int = 0
    referral_set: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"{self.created} جديد",
            f"{self.updated} محدّث",
        ]
        if self.skipped:
            parts.append(f"{self.skipped} تخطّي")
        if self.points_restored:
            parts.append(f"{self.points_restored} نقاط")
        if self.wallet_restored:
            parts.append(f"{self.wallet_restored} محفظة")
        if self.referral_set:
            parts.append(f"{self.referral_set} إحالة")
        return "، ".join(parts)


def _norm_row(row: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in row.items():
        key = (k or "").strip().lstrip("\ufeff")
        out[key] = ("" if v is None else str(v)).strip()
    return out


def _cell(row: dict[str, str], *keys: str) -> str:
    for k in keys:
        val = row.get(k, "")
        if val:
            return val
    return ""


def _parse_decimal(raw: str) -> Decimal | None:
    text = (raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.001"))
    except InvalidOperation:
        return None


def _parse_bool(raw: str) -> bool | None:
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in ("1", "true", "yes", "y", "نعم", "نشط"):
        return True
    if text in ("0", "false", "no", "n", "لا", "غير نشط"):
        return False
    return None


def _parse_customer_type(raw: str) -> str | None:
    text = (raw or "").strip().upper()
    if not text:
        return None
    if text in ("INDIVIDUAL", "فرد", "IND"):
        return "INDIVIDUAL"
    if text in ("COMPANY", "شركة", "CO"):
        return "COMPANY"
    return text if text in ("INDIVIDUAL", "COMPANY") else None


def _parse_domain(raw: str) -> str | None:
    text = (raw or "").strip().lower()
    if not text:
        return None
    mapping = {
        "restaurant": "restaurant",
        "hotel": "hotel",
        "shared": "shared",
        "مطعم": "restaurant",
        "فندق": "hotel",
        "مشترك": "shared",
        "مشتركون": "shared",
    }
    return mapping.get(text, text if parse_customer_domain(text) else None)


def import_customers_csv(
    db: Session,
    text: str,
    *,
    user_id: int | None = None,
    update_existing: bool = True,
    create_missing: bool = True,
    restore_points: bool = True,
    restore_wallet: bool = True,
    restore_referral: bool = True,
) -> CustomerImportResult:
    result = CustomerImportResult()
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        result.errors.append("الملف فارغ أو بدون عناوين أعمدة.")
        return result

    for i, raw_row in enumerate(reader, start=2):
        row = _norm_row(raw_row)
        phone_raw = _cell(row, "الهاتف", "phone", "Phone")
        if not phone_raw:
            result.skipped += 1
            continue
        try:
            phone = require_valid_phone(phone_raw)
        except CustomersError as e:
            result.errors.append(f"سطر {i}: {e}")
            continue

        name = _cell(row, "الاسم", "name", "Name")
        company_name = _cell(row, "اسم الشركة", "company_name", "company")
        customer_type = _parse_customer_type(_cell(row, "النوع", "customer_type", "type"))
        domain = _parse_domain(_cell(row, "المجال", "business_domain", "domain"))
        email = _cell(row, "البريد", "email", "Email") or None
        notes = _cell(row, "ملاحظات", "notes", "Notes") or None
        active = _parse_bool(_cell(row, "نشط", "is_active", "active"))
        referral_code = _cell(row, "كود الإحالة", "referral_code", "referral")
        target_points = _parse_decimal(_cell(row, "رصيد النقاط", "points_balance", "points"))
        target_wallet = _parse_decimal(_cell(row, "رصيد المحفظة", "wallet_balance", "wallet"))

        customer = get_by_phone_any(db, phone)
        is_new = customer is None

        if is_new:
            if not create_missing:
                result.skipped += 1
                continue
            if not name and not company_name:
                result.errors.append(f"سطر {i}: الاسم مطلوب لإنشاء عميل جديد ({phone}).")
                continue
            try:
                customer = create_customer(
                    db,
                    phone=phone,
                    name=name or company_name,
                    email=email,
                    notes=notes,
                    customer_type=customer_type or "INDIVIDUAL",
                    company_name=company_name or None,
                    business_domain=domain,
                )
                result.created += 1
            except CustomersError:
                archived = get_by_phone_any(db, phone)
                if archived is None or archived.is_active:
                    result.errors.append(
                        f"سطر {i}: رقم الهاتف {phone} مسجَّل بالفعل أو غير صالح."
                    )
                    continue
                restore_customer(db, archived.id)
                customer = archived
                is_new = False
                try:
                    update_customer(
                        db,
                        customer.id,
                        phone=phone,
                        name=name or None,
                        email=email,
                        notes=notes,
                        customer_type=customer_type,
                        company_name=company_name or None,
                        business_domain=domain,
                        is_active=True,
                    )
                    result.updated += 1
                except CustomersError as e:
                    result.errors.append(f"سطر {i}: {e}")
                    continue
        else:
            if not update_existing:
                result.skipped += 1
                continue
            if not customer.is_active:
                restore_customer(db, customer.id)
            try:
                update_customer(
                    db,
                    customer.id,
                    phone=phone,
                    name=name or None,
                    email=email,
                    notes=notes,
                    customer_type=customer_type,
                    company_name=company_name or None,
                    business_domain=domain,
                    is_active=active if active is not None else None,
                )
                result.updated += 1
            except CustomersError as e:
                result.errors.append(f"سطر {i}: {e}")
                continue

        if restore_referral and referral_code and customer is not None:
            try:
                set_customer_referral_code(db, customer.id, referral_code)
                result.referral_set += 1
            except ReferralError as e:
                result.warnings.append(f"سطر {i} ({phone}): {e}")

        if restore_points and target_points is not None and customer is not None:
            if is_company_customer(customer):
                if target_points != 0:
                    result.warnings.append(
                        f"سطر {i} ({phone}): تخطّي النقاط — حساب شركة."
                    )
            else:
                current = Decimal(customer.points_balance or 0).quantize(Decimal("0.001"))
                delta = target_points - current
                if delta != 0:
                    try:
                        adjust_points(
                            db,
                            customer_id=customer.id,
                            delta_points=delta,
                            note="استيراد CSV",
                            user_id=user_id,
                        )
                        result.points_restored += 1
                    except CustomersError as e:
                        result.warnings.append(f"سطر {i} ({phone}): {e}")

        if restore_wallet and target_wallet is not None and customer is not None:
            current = Decimal(customer.wallet_balance or 0).quantize(Decimal("0.001"))
            delta = target_wallet - current
            if delta != 0:
                try:
                    adjust_wallet(
                        db,
                        customer.id,
                        amount=delta,
                        note="استيراد CSV",
                        user_id=user_id,
                        allow_company_debt=True,
                    )
                    result.wallet_restored += 1
                except CustomersError as e:
                    result.warnings.append(f"سطر {i} ({phone}): {e}")

    db.flush()
    return result
