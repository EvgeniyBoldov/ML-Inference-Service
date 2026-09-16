"""Persist the pinned image used by an active fleet revision."""

from alembic import op
import sqlalchemy as sa


revision = "0002_fleet_runtime_image"
down_revision = "0001_deployment_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("runtime_image", sa.String(1024), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "runtime_image")
