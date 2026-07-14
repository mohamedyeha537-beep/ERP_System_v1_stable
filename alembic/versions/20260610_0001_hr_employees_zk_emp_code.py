"""hr_employees zk_emp_code for ZKBioTime sync

Revision ID: 20260610_0001
Revises: 20260504_0001
Create Date: 2026-06-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0001"
down_revision: Union[str, None] = "20260504_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "hr_employees"
COLUMN = "zk_emp_code"
INDEX = "ix_hr_employees_zk_emp_code"


def _table_columns(bind) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(TABLE)}


def _table_indexes(bind) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if COLUMN not in _table_columns(bind):
        op.add_column(
            TABLE,
            sa.Column(COLUMN, sa.String(64), nullable=True),
        )

    bind = op.get_bind()
    if INDEX not in _table_indexes(bind):
        op.create_index(INDEX, TABLE, [COLUMN], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if INDEX in _table_indexes(bind):
        op.drop_index(INDEX, table_name=TABLE)

    bind = op.get_bind()
    if COLUMN in _table_columns(bind):
        op.drop_column(TABLE, COLUMN)
