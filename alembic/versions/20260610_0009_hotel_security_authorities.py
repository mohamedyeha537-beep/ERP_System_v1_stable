"""hotel security authorities

Revision ID: 20260610_0009
Revises: 20260610_0008
Create Date: 2026-07-07

Adds security authority profiles for hotel guest reports.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0009"
down_revision: Union[str, None] = "20260610_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "hotel_security_authorities"


def _table_exists(bind, table: str) -> bool:
    return table in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name_ar", sa.String(160), nullable=False),
        sa.Column(
            "contact_channel",
            sa.Enum("WHATSAPP", "EMAIL", name="securitycontactchannel"),
            nullable=False,
        ),
        sa.Column("contact_value", sa.String(200), nullable=False),
        sa.Column(
            "guest_scope",
            sa.Enum("FOREIGN", "LIBYAN", "BOTH", name="securityguestscope"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_hotel_security_authorities_contact_channel",
        TABLE,
        ["contact_channel"],
    )
    op.create_index(
        "ix_hotel_security_authorities_guest_scope",
        TABLE,
        ["guest_scope"],
    )
    op.create_index(
        "ix_hotel_security_authorities_is_active",
        TABLE,
        ["is_active"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, TABLE):
        return
    op.drop_table(TABLE)
    op.execute("DROP TYPE IF EXISTS securitycontactchannel")
    op.execute("DROP TYPE IF EXISTS securityguestscope")
