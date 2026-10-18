"""workers heartbeat table

Revision ID: b2f4c9d1e7a3
Revises: 6d7e7a808a3d
Create Date: 2026-10-18 20:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b2f4c9d1e7a3"
down_revision = "6d7e7a808a3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("workers")
