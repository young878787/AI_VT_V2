"""Store safe per-event embedding diagnostics for test reports.

Revision ID: 0003_embedding_diagnostics
Revises: 0002_job_route_finalized
"""

from alembic import op


revision = "0003_embedding_diagnostics"
down_revision = "0002_job_route_finalized"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE memory_jobs ADD COLUMN embedding_diagnostics jsonb "
        "NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.drop_column("memory_jobs", "embedding_diagnostics")
