"""page snapshots, source state, notifications

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.add_column(sa.Column("state", sa.JSON(), nullable=True))
    op.create_table(
        "page_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("monitor_runs.id"), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("added", sa.JSON(), nullable=False),
        sa.Column("removed", sa.JSON(), nullable=False),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_page_snapshots_source_captured", "page_snapshots", ["source_id", "captured_at"])
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("recipient_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_notifications_recipient_created", "notifications", ["recipient_key", "created_at"])
    op.create_table(
        "notification_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("notification_id", sa.Integer(), sa.ForeignKey("notifications.id"), nullable=False),
        sa.Column("recipient_key", sa.String(64), nullable=False),
        sa.Column("item_type", sa.String(20), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("recipient_key", "item_type", "item_id", name="uq_notification_items"),
    )


def downgrade() -> None:
    op.drop_table("notification_items")
    op.drop_index("ix_notifications_recipient_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_page_snapshots_source_captured", table_name="page_snapshots")
    op.drop_table("page_snapshots")
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("state")
