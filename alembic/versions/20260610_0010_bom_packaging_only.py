"""bom packaging_only flag

Revision ID: 20260610_0010
Revises: 20260610_0009
Create Date: 2026-07-08

Packaging-only BOM lines for external/online orders.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0010"
down_revision: Union[str, None] = "20260610_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "bom_lines"


def _table_columns(bind, table: str = TABLE) -> set[str]:
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind)
    if "packaging_only" not in cols:
        op.add_column(
            TABLE,
            sa.Column("packaging_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index("ix_bom_lines_packaging_only", TABLE, ["packaging_only"])


def downgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind)
    if "packaging_only" in cols:
        op.drop_index("ix_bom_lines_packaging_only", table_name=TABLE)
        op.drop_column(TABLE, "packaging_only")
