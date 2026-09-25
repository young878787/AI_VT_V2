"""Initial PostgreSQL and pgvector memory schema.

Revision ID: 0001_initial_memory
Revises:
"""

from pathlib import Path

from alembic import op


revision = "0001_initial_memory"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).with_suffix(".sql")
    op.get_bind().exec_driver_sql(sql_path.read_text(encoding="utf-8"))


def downgrade() -> None:
    for table in (
        "memory_audit", "memory_jobs", "memory_relations", "memory_evidence",
        "memory_sources", "memory_items", "memory_scope_state",
    ):
        op.drop_table(table)
