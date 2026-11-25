"""Задания, снимки входа, итог и ключи идемпотентности."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("dataset_id", sa.String(64), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("items", sa.JSON, nullable=False),
        sa.Column("catalog_version", sa.String, nullable=False),
        sa.Column("analysis_version", sa.String, nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("started_at", sa.String),
        sa.Column("finished_at", sa.String),
        sa.Column("result", sa.JSON(none_as_null=True)),
        sa.Column("error", sa.JSON(none_as_null=True)),
        sa.CheckConstraint(
            "(state = 'created' AND started_at IS NULL AND finished_at IS NULL)"
            " OR (state = 'running' AND started_at IS NOT NULL AND finished_at IS NULL)"
            " OR (state = 'cancelled' AND started_at IS NULL AND finished_at IS NOT NULL)"
            " OR (state IN ('done', 'failed') AND started_at IS NOT NULL AND finished_at IS NOT NULL)",
            name="job_state_timestamps",
        ),
        sa.CheckConstraint("(state = 'done') = (result IS NOT NULL)", name="job_result"),
        sa.CheckConstraint("(state = 'failed') = (error IS NOT NULL)", name="job_error"),
    )
    op.create_index("jobs_queue", "jobs", ["state", "created_at", "id"])


def downgrade():
    op.drop_table("jobs")
