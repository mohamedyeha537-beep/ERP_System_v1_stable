"""products BOM pricing columns

Revision ID: 20260610_0002
Revises: 20260610_0001
Create Date: 2026-06-10

Adds columns required by catalog BOM save-lines (reference_unit_cost, etc.).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0002"
down_revision: Union[str, None] = "20260610_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "products"


def _table_columns(bind) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind)

    if "price_linked_to_bom" not in cols:
        op.add_column(
            TABLE,
            sa.Column(
                "price_linked_to_bom",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    bind = op.get_bind()
    cols = _table_columns(bind)
    if "bom_markup_pct" not in cols:
        op.add_column(
            TABLE,
            sa.Column("bom_markup_pct", sa.Numeric(8, 2), nullable=True),
        )

    bind = op.get_bind()
    cols = _table_columns(bind)
    if "reference_unit_cost" not in cols:
        op.add_column(
            TABLE,
            sa.Column("reference_unit_cost", sa.Numeric(14, 3), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind)

    if "reference_unit_cost" in cols:
        op.drop_column(TABLE, "reference_unit_cost")

    bind = op.get_bind()
    cols = _table_columns(bind)
    if "bom_markup_pct" in cols:
        op.drop_column(TABLE, "bom_markup_pct")

    bind = op.get_bind()
    cols = _table_columns(bind)
    if "price_linked_to_bom" in cols:
        op.drop_column(TABLE, "price_linked_to_bom")
