"""Create deployment, routing, and idempotency state tables."""

from alembic import op
import sqlalchemy as sa

revision = "0001_deployment_state"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("uri", sa.String(1024), nullable=False),
        sa.Column("slot", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("runtime_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("activated_at", sa.BigInteger(), nullable=True),
        sa.Column("failed_at", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_message", sa.String(2048), nullable=True),
    )
    op.create_index("ix_deployments_model", "deployments", ["model"])
    op.create_index("ix_deployments_status", "deployments", ["status"])
    op.create_table("model_routes", sa.Column("model", sa.String(255), primary_key=True), sa.Column("active_deployment_id", sa.String(64)), sa.Column("previous_deployment_id", sa.String(64)))
    op.create_table("deployment_idempotency", sa.Column("key", sa.String(255), primary_key=True), sa.Column("request_fingerprint", sa.String(140), nullable=False), sa.Column("deployment_id", sa.String(64), nullable=False, unique=True))


def downgrade() -> None:
    op.drop_table("deployment_idempotency")
    op.drop_table("model_routes")
    op.drop_index("ix_deployments_status", table_name="deployments")
    op.drop_index("ix_deployments_model", table_name="deployments")
    op.drop_table("deployments")
