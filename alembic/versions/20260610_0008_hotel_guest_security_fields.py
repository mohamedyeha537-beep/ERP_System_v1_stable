"""hotel guest security fields

Revision ID: 20260610_0008
Revises: 20260610_0007
Create Date: 2026-06-27

Adds guest document fields used for the security authority report.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0008"
down_revision: Union[str, None] = "20260610_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "hotel_bookings"


def _table_columns(bind, table: str = TABLE) -> set[str]:
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind)
    if "guest_id_type" not in cols:
        op.add_column(TABLE, sa.Column("guest_id_type", sa.String(80), nullable=True))
    if "guest_address" not in cols:
        op.add_column(TABLE, sa.Column("guest_address", sa.Text(), nullable=True))
    if "guest_nationality" not in cols:
        op.add_column(TABLE, sa.Column("guest_nationality", sa.String(80), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _table_columns(bind)
    if "guest_nationality" in cols:
        op.drop_column(TABLE, "guest_nationality")
    if "guest_address" in cols:
        op.drop_column(TABLE, "guest_address")
    if "guest_id_type" in cols:
        op.drop_column(TABLE, "guest_id_type")
