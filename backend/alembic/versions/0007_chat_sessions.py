"""Store bounded Chat session state in PostgreSQL.

Revision ID: 0007_chat_sessions
Revises: 0006_single_memory_agent
"""

from alembic import op


revision = "0007_chat_sessions"
down_revision = "0006_single_memory_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE chat_sessions (
        id text NOT NULL,
        user_id uuid NOT NULL,
        character_id uuid NOT NULL,
        status text NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'closed')),
        generation integer NOT NULL DEFAULT 0 CHECK (generation >= 0),
        revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
        summary text NOT NULL DEFAULT '' CHECK (char_length(summary) <= 4000),
        emotion_state jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (id, user_id, character_id)
    )""")
    op.execute("""CREATE UNIQUE INDEX chat_sessions_one_active_owner
        ON chat_sessions (user_id, character_id) WHERE status = 'active'""")
    op.execute("""CREATE INDEX chat_sessions_owner_updated
        ON chat_sessions (user_id, character_id, updated_at DESC)""")
    op.execute("""CREATE TABLE chat_messages (
        session_id text NOT NULL,
        user_id uuid NOT NULL,
        character_id uuid NOT NULL,
        generation integer NOT NULL CHECK (generation >= 0),
        sequence bigint NOT NULL CHECK (sequence >= 0),
        turn_id text,
        role text NOT NULL CHECK (role IN ('user', 'assistant')),
        content text NOT NULL CHECK (content <> ''),
        status text NOT NULL DEFAULT 'complete'
            CHECK (status IN ('complete', 'interrupted')),
        memory_source jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, user_id, character_id, generation, sequence),
        FOREIGN KEY (session_id, user_id, character_id)
            REFERENCES chat_sessions (id, user_id, character_id) ON DELETE CASCADE,
        CHECK (status = 'complete' OR role = 'assistant'),
        CHECK (memory_source IS NULL OR role = 'user')
    )""")
    op.execute("""CREATE UNIQUE INDEX chat_messages_turn_role
        ON chat_messages (session_id, user_id, character_id, generation, turn_id, role)
        WHERE turn_id IS NOT NULL""")
    op.execute("""CREATE INDEX chat_messages_recent
        ON chat_messages (session_id, user_id, character_id, generation, sequence DESC)""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM chat_sessions LIMIT 1) THEN
            RAISE EXCEPTION 'chat session 資料存在；請先備份或明確清除後再降版';
        END IF;
    END $$""")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
