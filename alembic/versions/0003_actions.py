"""Durable action ledger, transitions, and remote order reservations.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operations",
        sa.Column("action_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("session_id", sa.String(64), nullable=False, index=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(32), nullable=False, index=True),
        sa.Column("arguments_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("order_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("order_facts_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("provider", sa.String(32), nullable=False, server_default="local"),
        sa.Column("receipt_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("policy_evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(64)),
    )
    op.create_table(
        "operation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action_id", sa.String(64), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "order_reservations",
        sa.Column("order_id", sa.String(32), primary_key=True),
        sa.Column("action_id", sa.String(64), nullable=False, unique=True),
    )


def downgrade() -> None:
    op.drop_table("order_reservations")
    op.drop_table("operation_events")
    op.drop_table("operations")
