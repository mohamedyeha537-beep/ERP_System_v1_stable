"""product category show_in_pos for POS visibility

Revision ID: 20260610_0005
Revises: 20260610_0004
Create Date: 2026-06-11

Allows admin to hide inventory categories (vegetables, spices) from POS session.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0005"
down_revision: Union[str, None] = "20260610_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _add_col(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    if column.name not in _table_columns(bind, table):
        op.add_column(table, column)


def upgrade() -> None:
    _add_col(
        "product_categories",
        sa.Column("show_in_pos", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE product_categories SET show_in_pos = TRUE WHERE show_in_pos IS NULL")


def downgrade() -> None:
    bind = op.get_bind()
    if "show_in_pos" in _table_columns(bind, "product_categories"):
        op.drop_column("product_categories", "show_in_pos")
