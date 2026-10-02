"""Separate intake review from librarian integration.

Revision ID: 0005_memory_agents
Revises: 0004_embedding_model
"""
from alembic import op

revision = "0005_memory_agents"
down_revision = "0004_embedding_model"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE memory_jobs DROP CONSTRAINT memory_jobs_route_check")
    op.execute("ALTER TABLE memory_jobs DROP CONSTRAINT memory_jobs_check")
    op.execute("ALTER TABLE memory_jobs ALTER COLUMN route DROP NOT NULL")
    op.execute("ALTER TABLE memory_jobs ADD COLUMN stage text NOT NULL DEFAULT 'intake' CHECK (stage IN ('intake', 'librarian'))")
    op.execute("ALTER TABLE memory_jobs ADD COLUMN reviewed_candidates jsonb")
    op.execute("ALTER TABLE memory_jobs ADD COLUMN missing_context text")
    op.execute("ALTER TABLE memory_jobs ADD COLUMN instruction text NOT NULL DEFAULT 'observe'")
    op.execute("ALTER TABLE memory_jobs ADD COLUMN agent_diagnostics jsonb NOT NULL DEFAULT '[]'")
    op.execute("ALTER TABLE memory_jobs ADD COLUMN intake_attempts integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE memory_jobs ADD COLUMN librarian_attempts integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE memory_jobs ADD COLUMN expires_at timestamptz NOT NULL DEFAULT (now() + interval '24 hours')")
    op.execute("ALTER TABLE memory_jobs RENAME COLUMN buffered_job_ids TO context_job_ids")
    # 未審查的舊 PROCESS 必須回到接收端，不能視為已審查候選。
    op.execute("UPDATE memory_jobs SET route = CASE route WHEN 'buffer' THEN 'needs_context' WHEN 'process' THEN NULL ELSE route END")
    op.execute("ALTER TABLE memory_jobs ADD CONSTRAINT memory_jobs_route_check CHECK (route IN ('none', 'needs_context', 'candidate'))")
    op.execute("DROP INDEX memory_jobs_buffer_idx")
    op.execute("CREATE INDEX memory_jobs_context_idx ON memory_jobs (user_id, character_id, expires_at) WHERE status = 'buffered'")


def downgrade():
    raise RuntimeError("兩階段工作不可無損降版；請使用升版前的備份還原")
