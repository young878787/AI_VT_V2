"""Converge background memory into one bounded agent.

Revision ID: 0006_single_memory_agent
Revises: 0005_memory_agents
"""
from alembic import op

revision = "0006_single_memory_agent"
down_revision = "0005_memory_agents"
branch_labels = None
depends_on = None


def upgrade():
    # 移除舊 claim 的 generation，讓升版後的舊 worker 無法提交。
    op.execute("""INSERT INTO memory_scope_state (user_id, character_id, generation)
        SELECT user_id, character_id, max(generation) FROM memory_jobs GROUP BY user_id, character_id
        ON CONFLICT DO NOTHING""")
    op.execute("UPDATE memory_scope_state SET generation = generation + 1")
    op.execute("ALTER TABLE memory_jobs DROP CONSTRAINT memory_jobs_route_check")
    op.execute("""UPDATE memory_jobs AS j SET generation = s.generation, attempts = 0,
        lease_until = NULL, pending_target_ids = '{}',
        route = CASE WHEN j.status = 'buffered' THEN 'needs_context' ELSE 'process' END,
        status = CASE WHEN NOT EXISTS (SELECT 1 FROM memory_sources src WHERE
            (src.id,src.user_id,src.character_id) = (j.id,j.user_id,j.character_id)
            AND src.speaker = 'user' AND src.raw_text IS NOT NULL) THEN 'failed'
            WHEN j.status = 'buffered' THEN 'buffered' ELSE 'pending' END,
        error = CASE WHEN NOT EXISTS (SELECT 1 FROM memory_sources src WHERE
            (src.id,src.user_id,src.character_id) = (j.id,j.user_id,j.character_id)
            AND src.raw_text IS NOT NULL) THEN 'missing_source' ELSE NULL END
        FROM memory_scope_state s WHERE s.user_id = j.user_id AND s.character_id = j.character_id
        AND j.status IN ('pending','retry','running','buffered')""")
    op.execute("UPDATE memory_jobs SET route = 'process' WHERE route IS NULL OR route = 'candidate'")
    op.execute("ALTER TABLE memory_jobs ALTER COLUMN route SET NOT NULL")
    op.execute("ALTER TABLE memory_jobs ADD CONSTRAINT memory_jobs_route_check CHECK (route IN ('none','process','needs_context'))")
    # 已完成操作若尚未有 audit，轉接唯一證據後才移除重複 job JSON。
    op.execute("""INSERT INTO memory_audit (id,user_id,character_id,operation_key,action,source_event_id,reason_class,decision)
        SELECT md5(j.id::text || ':' || d.ordinality::text || j.user_id::text || j.character_id::text)::uuid,
        j.user_id,j.character_id,j.id::text || ':' || (d.ordinality - 1)::text,
        d.value->>'action',j.id,'migration',d.value FROM memory_jobs j,
        jsonb_array_elements(CASE WHEN jsonb_typeof(j.decisions) = 'array' THEN j.decisions ELSE '[]' END)
        WITH ORDINALITY d(value,ordinality) WHERE j.status = 'done' AND d.value->>'action' <> 'IGNORE'
        ON CONFLICT (user_id,character_id,operation_key) DO NOTHING""")
    op.execute("UPDATE memory_jobs SET agent_diagnostics = '{}', embedding_diagnostics = '{}'")
    op.execute("ALTER TABLE memory_jobs ALTER COLUMN agent_diagnostics SET DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE memory_jobs ALTER COLUMN embedding_diagnostics SET DEFAULT '{}'::jsonb")
    for column in ("stage", "intake_attempts", "librarian_attempts", "reviewed_candidates", "decisions",
                   "embedding", "embedding_model", "embedding_contract"):
        op.execute(f"ALTER TABLE memory_jobs DROP COLUMN {column}")


def downgrade():
    raise RuntimeError("單一 agent 工作不可無損降版；請還原升版前的程式與 DB 備份")
