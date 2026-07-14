"""assets purchase columns and dashboard activity resolution

Revision ID: 20260610_0004
Revises: 20260610_0003
Create Date: 2026-06-11

Adds columns required by /admin/assets list and asset invoice lines on MySQL/PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0004"
down_revision: Union[str, None] = "20260610_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _add_col(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    cols = _table_columns(bind, table)
    if column.name in cols:
        return
    op.add_column(table, column)


def upgrade() -> None:
    _add_col(
        "purchases",
        sa.Column("kind", sa.String(20), nullable=False, server_default=sa.text("'EXPENSE'")),
    )
    _add_col("purchases", sa.Column("expense_category", sa.String(80), nullable=True))
    _add_col("purchases", sa.Column("supplier_invoice_ref", sa.String(120), nullable=True))
    _add_col("purchases", sa.Column("invoice_image_filename", sa.String(255), nullable=True))
    _add_col(
        "purchases",
        sa.Column("payment_proof_image_filename", sa.String(255), nullable=True),
    )
    _add_col("purchases", sa.Column("supplier_phone", sa.String(40), nullable=True))

    _add_col("purchase_lines", sa.Column("item_name", sa.String(255), nullable=True))
    _add_col("purchase_lines", sa.Column("unit", sa.String(32), nullable=True))
    _add_col(
        "purchase_lines",
        sa.Column(
            "useful_life_months",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    _add_col(
        "purchase_lines",
        sa.Column(
            "salvage_value",
            sa.Numeric(14, 3),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    _add_col(
        "purchase_lines",
        sa.Column("disposal_date", sa.DateTime(timezone=True), nullable=True),
    )

    bind = op.get_bind()
    if "dashboard_activities" in inspect(bind).get_table_names():
        _add_col(
            "dashboard_activities",
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()

    da_cols = _table_columns(bind, "dashboard_activities")
    if "resolved_at" in da_cols:
        op.drop_column("dashboard_activities", "resolved_at")

    pl_cols = _table_columns(bind, "purchase_lines")
    for name in (
        "disposal_date",
        "salvage_value",
        "useful_life_months",
        "unit",
        "item_name",
    ):
        if name in pl_cols:
            op.drop_column("purchase_lines", name)

    p_cols = _table_columns(bind, "purchases")
    for name in (
        "supplier_phone",
        "payment_proof_image_filename",
        "invoice_image_filename",
        "supplier_invoice_ref",
        "expense_category",
        "kind",
    ):
        if name in p_cols:
            op.drop_column("purchases", name)
