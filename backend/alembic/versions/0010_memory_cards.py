"""Add chat cards above atomic memory items.

Revision ID: 0010_memory_cards
Revises: 0009_mood_v1_emotion
"""

from alembic import op


revision = "0010_memory_cards"
down_revision = "0009_mood_v1_emotion"
branch_labels = None
depends_on = None

_DOMAINS = "('self','preference','daily','relation','project','event','bond','other')"
_MOODS = "('calm','shy','pleased','genuinely_angry','sad_or_hurt')"


def upgrade() -> None:
    op.execute(f"""CREATE TABLE memory_cards (
        id uuid NOT NULL,
        user_id uuid NOT NULL,
        character_id uuid NOT NULL,
        domain text NOT NULL CHECK (domain IN {_DOMAINS}),
        title text NOT NULL CHECK (char_length(title) BETWEEN 2 AND 16),
        title_key text NOT NULL CHECK (title_key <> ''),
        summary text CHECK (summary IS NULL OR char_length(summary) <= 200),
        summary_fingerprint text,
        summary_stale boolean NOT NULL DEFAULT true,
        mood text CHECK (mood IS NULL OR mood IN {_MOODS}),
        embedding vector(1024),
        embedding_contract text,
        active_count integer NOT NULL DEFAULT 0 CHECK (active_count >= 0),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (id, user_id, character_id),
        UNIQUE (user_id, character_id, domain, title_key)
    )""")
    op.execute("CREATE INDEX memory_cards_embedding_idx ON memory_cards USING hnsw (embedding vector_cosine_ops)")
    # 卡片只在沒有任何要點時由後端刪除；FK 不帶 cascade，避免刪卡連帶刪掉事實。
    op.execute("ALTER TABLE memory_items ADD COLUMN card_id uuid")
    op.execute("""ALTER TABLE memory_items ADD CONSTRAINT memory_items_card_fk
        FOREIGN KEY (card_id, user_id, character_id) REFERENCES memory_cards (id, user_id, character_id)""")
    op.execute("CREATE INDEX memory_items_card_idx ON memory_items (user_id, character_id, card_id)")
    op.execute("ALTER TABLE memory_sources ADD COLUMN embedding vector(1024)")
    op.execute("ALTER TABLE memory_sources ADD COLUMN embedding_contract text")
    op.execute(f"ALTER TABLE memory_sources ADD COLUMN mood text CHECK (mood IS NULL OR mood IN {_MOODS})")
    op.execute("CREATE INDEX memory_sources_embedding_idx ON memory_sources USING hnsw (embedding vector_cosine_ops)")
    op.execute(f"ALTER TABLE memory_jobs ADD COLUMN turn_mood text CHECK (turn_mood IS NULL OR turn_mood IN {_MOODS})")


def downgrade() -> None:
    # 要點的卡片歸屬無法由其他資料重建；降版前須備份。
    op.execute("ALTER TABLE memory_jobs DROP COLUMN turn_mood")
    op.execute("DROP INDEX memory_sources_embedding_idx")
    op.execute("ALTER TABLE memory_sources DROP COLUMN mood")
    op.execute("ALTER TABLE memory_sources DROP COLUMN embedding_contract")
    op.execute("ALTER TABLE memory_sources DROP COLUMN embedding")
    op.execute("DROP INDEX memory_items_card_idx")
    op.execute("ALTER TABLE memory_items DROP CONSTRAINT memory_items_card_fk")
    op.execute("ALTER TABLE memory_items DROP COLUMN card_id")
    op.execute("DROP TABLE memory_cards")
