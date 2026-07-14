"""Add purchase_lines.line_kind for mixed purchase invoices.

Revision ID: 20260610_0011
Revises: 20260610_0010
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260610_0011"
down_revision = "20260610_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "purchase_lines",
        sa.Column("line_kind", sa.String(length=20), nullable=True),
    )
    op.create_index("ix_purchase_lines_line_kind", "purchase_lines", ["line_kind"])
    op.execute(
        sa.text(
            "UPDATE purchase_lines SET line_kind = 'PRODUCT' "
            "WHERE product_id IS NOT NULL AND (line_kind IS NULL OR line_kind = '')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE purchase_lines SET line_kind = 'FIXED_ASSET' "
            "WHERE product_id IS NULL AND useful_life_months > 0 "
            "AND (line_kind IS NULL OR line_kind = '')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE purchase_lines SET line_kind = 'CONSUMABLE' "
            "WHERE product_id IS NULL AND COALESCE(useful_life_months, 0) = 0 "
            "AND item_name IS NOT NULL AND TRIM(item_name) != '' "
            "AND (line_kind IS NULL OR line_kind = '')"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_purchase_lines_line_kind", table_name="purchase_lines")
    op.drop_column("purchase_lines", "line_kind")
