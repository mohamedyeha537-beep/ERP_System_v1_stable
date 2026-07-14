"""purchase expiry and inventory lot columns

Revision ID: 20260610_0003
Revises: 20260610_0002
Create Date: 2026-06-10

Adds columns required by /admin/purchases/new and inventory lot tracking on MySQL/PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0003"
down_revision: Union[str, None] = "20260610_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _add_bool(table: str, name: str, *, default: str = "0") -> None:
    bind = op.get_bind()
    cols = _table_columns(bind, table)
    if name in cols:
        return
    op.add_column(
        table,
        sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text(default)),
    )


def _add_col(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    cols = _table_columns(bind, table)
    if column.name in cols:
        return
    op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    _add_bool("products", "show_in_pos", default="1")
    _add_col("products", sa.Column("line_modifier_presets", sa.Text(), nullable=True))
    _add_bool("products", "expiry_tracked", default="0")
    _add_col("products", sa.Column("expiry_production_date", sa.Date(), nullable=True))
    _add_col("products", sa.Column("expiry_date", sa.Date(), nullable=True))
    _add_col(
        "products",
        sa.Column("expiry_warn_days", sa.Integer(), nullable=False, server_default=sa.text("7")),
    )

    _add_col(
        "purchases",
        sa.Column("receipt_batch_no", sa.String(32), nullable=True),
    )

    _add_col("purchase_lines", sa.Column("lot_code", sa.String(32), nullable=True))
    _add_col("purchase_lines", sa.Column("production_date", sa.Date(), nullable=True))
    _add_col("purchase_lines", sa.Column("expiry_date", sa.Date(), nullable=True))

    lot_cols = _table_columns(bind, "inventory_lots")
    if lot_cols and "unit_cost" not in lot_cols:
        op.add_column(
            "inventory_lots",
            sa.Column(
                "unit_cost",
                sa.Numeric(14, 3),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        op.execute(
            sa.text(
                "UPDATE inventory_lots SET unit_cost = ("
                "SELECT pl.unit_cost FROM purchase_lines pl "
                "WHERE pl.id = inventory_lots.purchase_line_id"
                ") WHERE unit_cost = 0"
            )
        )

    if "receipt_batch_no" in _table_columns(bind, "purchases"):
        if dialect == "mysql":
            op.execute(
                sa.text(
                    "UPDATE purchases SET receipt_batch_no = CONCAT('GR-', LPAD(CAST(id AS CHAR), 6, '0')) "
                    "WHERE kind = 'INVENTORY' AND receipt_batch_no IS NULL"
                )
            )
        elif dialect == "postgresql":
            op.execute(
                sa.text(
                    "UPDATE purchases SET receipt_batch_no = 'GR-' || LPAD(id::text, 6, '0') "
                    "WHERE kind = 'INVENTORY' AND receipt_batch_no IS NULL"
                )
            )


def downgrade() -> None:
    bind = op.get_bind()

    lot_cols = _table_columns(bind, "inventory_lots")
    if "unit_cost" in lot_cols:
        op.drop_column("inventory_lots", "unit_cost")

    pl_cols = _table_columns(bind, "purchase_lines")
    for name in ("expiry_date", "production_date", "lot_code"):
        if name in pl_cols:
            op.drop_column("purchase_lines", name)

    p_cols = _table_columns(bind, "purchases")
    if "receipt_batch_no" in p_cols:
        op.drop_column("purchases", "receipt_batch_no")

    prod_cols = _table_columns(bind, "products")
    for name in (
        "expiry_warn_days",
        "expiry_date",
        "expiry_production_date",
        "expiry_tracked",
        "line_modifier_presets",
        "show_in_pos",
    ):
        if name in prod_cols:
            op.drop_column("products", name)
