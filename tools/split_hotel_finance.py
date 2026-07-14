"""Prepare separate hotel treasuries without moving restaurant balances.

Usage:
  python tools/split_hotel_finance.py          # dry-run
  python tools/split_hotel_finance.py --apply  # write changes

The script keeps current wallet balances in their existing payment method IDs.
It only changes the business_domain of existing shared cash/bank wallets to
"restaurant" and creates zero-balance hotel cash/bank wallets.
"""
from __future__ import annotations

import argparse
from decimal import Decimal

from infra.model_registry import import_all_models

import_all_models()

from infra.db import get_session_factory
from infra.schema_bootstrap import ensure_schema_patched
from modules.payments.models import (
    HOTEL_TREASURY_BANK_PM_NAME,
    HOTEL_TREASURY_CASH_PM_NAME,
    PaymentMethod,
    PaymentMethodDomain,
    PaymentMethodKind,
)
from modules.payments.service import (
    ensure_hotel_treasury_payment_methods,
    is_treasury_wallet_method,
    method_current_balance,
)


def _domain_value(value) -> str:
    return getattr(value, "value", value) or PaymentMethodDomain.SHARED.value


def _money(value: Decimal) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.001'))}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes")
    args = parser.parse_args()

    ensure_schema_patched(force=True)
    db = get_session_factory()()
    try:
        rows = list(db.query(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.id).all())
        hotel_names = {HOTEL_TREASURY_CASH_PM_NAME, HOTEL_TREASURY_BANK_PM_NAME}
        to_restaurant: list[PaymentMethod] = []
        for pm in rows:
            if pm.name_ar in hotel_names:
                continue
            if not is_treasury_wallet_method(pm):
                continue
            if _domain_value(pm.business_domain) == PaymentMethodDomain.SHARED.value:
                to_restaurant.append(pm)

        print("Hotel finance split preview")
        print("===========================")
        if to_restaurant:
            print("Existing shared wallets that will become restaurant wallets:")
            for pm in to_restaurant:
                print(f"- #{pm.id} {pm.name_ar}: balance={_money(method_current_balance(db, pm.id))}")
        else:
            print("No shared restaurant wallets need tagging.")

        if args.apply:
            for pm in to_restaurant:
                pm.business_domain = PaymentMethodDomain.RESTAURANT
            hotel = ensure_hotel_treasury_payment_methods(db)
            try:
                from modules.gl.seed import ensure_hotel_wallet_gl_maps

                ensure_hotel_wallet_gl_maps(db)
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: could not link hotel wallets to GL: {exc}")
            db.commit()
            print("Applied.")
            for key, pm in hotel.items():
                print(f"- hotel {key}: #{pm.id} {pm.name_ar} balance={_money(method_current_balance(db, pm.id))}")
        else:
            print("Dry-run only. Re-run with --apply to write changes.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
