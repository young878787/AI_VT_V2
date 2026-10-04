"""Track when JEV routing has finished for each memory job.

Revision ID: 0002_job_route_finalized
Revises: 0001_initial_memory
"""

from alembic import op


revision = "0002_job_route_finalized"
down_revision = "0001_initial_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE memory_jobs ADD COLUMN route_finalized boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.drop_column("memory_jobs", "route_finalized")
