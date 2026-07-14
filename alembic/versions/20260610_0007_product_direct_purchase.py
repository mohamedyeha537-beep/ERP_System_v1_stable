"""product direct purchase option

Revision ID: 20260610_0007
Revises: 20260610_0006
Create Date: 2026-06-10

Adds a flag that allows final sellable products to be purchased directly into stock.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0007"
down_revision: Union[str, None] = "20260610_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "products"


def _table_columns(bind, table: str = TABLE) -> set[str]:
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind)
    if "direct_purchase_enabled" not in cols:
        op.add_column(
            TABLE,
            sa.Column(
                "direct_purchase_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    if "direct_purchase_enabled" in _table_columns(bind) and _table_columns(bind, "bom_lines"):
        op.execute(
            sa.text(
                """
                UPDATE products
                SET direct_purchase_enabled = 1
                WHERE kind = 'FINAL_SELLABLE'
                  AND NOT EXISTS (
                    SELECT 1 FROM bom_lines
                    WHERE bom_lines.parent_product_id = products.id
                  )
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind)
    if "direct_purchase_enabled" in cols:
        op.drop_column(TABLE, "direct_purchase_enabled")
