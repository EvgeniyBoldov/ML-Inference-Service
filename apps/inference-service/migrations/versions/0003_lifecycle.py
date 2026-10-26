"""Persist model activation and runtime power state."""
from alembic import op
import sqlalchemy as sa
revision = "0003_lifecycle"
down_revision = "0002_fleet_runtime_image"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("enabled", "runtime_enabled"):
        op.add_column("deployments", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    for name in ("runtime_enabled", "enabled"):
        op.drop_column("deployments", name)
