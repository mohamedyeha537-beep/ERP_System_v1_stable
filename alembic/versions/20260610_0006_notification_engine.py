"""Alembic migration: notification engine tables

Revision ID: 20260610_0006
Revises: 20260610_0005
Create Date: 2026-06-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260610_0006"
down_revision: Union[str, None] = "20260610_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    return name in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "notification_events"):
        op.create_table(
            "notification_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_key", sa.String(64), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.Integer(), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("error_message", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_notification_events_event_key", "notification_events", ["event_key"])
        op.create_index("ix_notification_events_status", "notification_events", ["status"])

    if not _has_table(bind, "notification_templates"):
        op.create_table(
            "notification_templates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("event_key", sa.String(64), nullable=False),
            sa.Column("channel", sa.String(32), nullable=False, server_default="whatsapp"),
            sa.Column("recipient_type", sa.String(32), nullable=False),
            sa.Column("message_type", sa.String(20), nullable=False, server_default="text"),
            sa.Column("title", sa.String(160), nullable=True),
            sa.Column("body_template", sa.Text(), nullable=False),
            sa.Column("buttons_json", sa.Text(), nullable=True),
            sa.Column("image_url", sa.String(500), nullable=True),
            sa.Column("document_url", sa.String(500), nullable=True),
            sa.Column("language", sa.String(8), nullable=False, server_default="ar"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_notification_templates_event_key", "notification_templates", ["event_key"])

    if not _has_table(bind, "notification_rules"):
        op.create_table(
            "notification_rules",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_key", sa.String(64), nullable=False),
            sa.Column("recipient_type", sa.String(32), nullable=False),
            sa.Column("channel", sa.String(32), nullable=False, server_default="whatsapp"),
            sa.Column("template_id", sa.Integer(), sa.ForeignKey("notification_templates.id", ondelete="CASCADE"), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
            sa.Column("delay_seconds", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("condition_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("throttle_minutes", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_notification_rules_event_key", "notification_rules", ["event_key"])

    if not _has_table(bind, "notification_logs"):
        op.create_table(
            "notification_logs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_key", sa.String(64), nullable=False),
            sa.Column("notification_event_id", sa.Integer(), sa.ForeignKey("notification_events.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.Integer(), nullable=True),
            sa.Column("recipient_type", sa.String(32), nullable=False),
            sa.Column("recipient_name", sa.String(160), nullable=True),
            sa.Column("recipient_phone", sa.String(40), nullable=True),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("message_type", sa.String(20), nullable=False),
            sa.Column("template_id", sa.Integer(), sa.ForeignKey("notification_templates.id", ondelete="SET NULL"), nullable=True),
            sa.Column("body_rendered", sa.Text(), nullable=False),
            sa.Column("provider", sa.String(32), nullable=True),
            sa.Column("provider_message_id", sa.String(120), nullable=True),
            sa.Column("outbox_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
            sa.Column("error_message", sa.String(500), nullable=True),
            sa.Column("idempotency_key", sa.String(200), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("idempotency_key", name="uq_notification_logs_idempotency"),
        )
        op.create_index("ix_notification_logs_status", "notification_logs", ["status"])
        op.create_index("ix_notification_logs_event_key", "notification_logs", ["event_key"])

    if not _has_table(bind, "notification_actions"):
        op.create_table(
            "notification_actions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("notification_log_id", sa.Integer(), sa.ForeignKey("notification_logs.id", ondelete="CASCADE"), nullable=True),
            sa.Column("action_key", sa.String(64), nullable=False),
            sa.Column("action_payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("received_from_phone", sa.String(40), nullable=True),
            sa.Column("handled_status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("result_message", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "notification_actions",
        "notification_logs",
        "notification_rules",
        "notification_templates",
        "notification_events",
    ):
        if _has_table(bind, table):
            op.drop_table(table)
