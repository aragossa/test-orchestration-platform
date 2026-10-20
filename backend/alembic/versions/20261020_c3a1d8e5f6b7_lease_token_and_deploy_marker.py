"""lease token and deploy-requested marker on runs

Revision ID: c3a1d8e5f6b7
Revises: b2f4c9d1e7a3
Create Date: 2026-10-20 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c3a1d8e5f6b7"
down_revision = "b2f4c9d1e7a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # lease_token: per-process fencing token; every write a worker makes to a
    # run is conditioned on it, so a worker that lost the run cannot write.
    op.add_column("runs", sa.Column("lease_token", sa.String(length=64), nullable=True))
    # deploy_requested_*: set right before the infrastructure adapter is asked
    # to deploy. A run resumed in DEPLOYING with the marker set is failed, not
    # redeployed, because the outcome of the first request is unknown.
    op.add_column(
        "runs", sa.Column("deploy_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("runs", sa.Column("deploy_requested_by", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "deploy_requested_by")
    op.drop_column("runs", "deploy_requested_at")
    op.drop_column("runs", "lease_token")
