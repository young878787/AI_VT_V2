"""Add durable cursors and turn state for rolling Chat context.

Revision ID: 0008_chat_context_convergence
Revises: 0007_chat_sessions
"""

from alembic import op


revision = "0008_chat_context_convergence"
down_revision = "0007_chat_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE chat_sessions
        ADD COLUMN summary_through_sequence bigint NOT NULL DEFAULT -1
            CHECK (summary_through_sequence >= -1),
        ADD COLUMN next_message_sequence bigint NOT NULL DEFAULT 0
            CHECK (next_message_sequence >= 0)""")
    op.execute("""UPDATE chat_sessions AS session
        SET next_message_sequence = COALESCE((
            SELECT max(message.sequence) + 1
            FROM chat_messages AS message
            WHERE message.session_id = session.id
              AND message.user_id = session.user_id
              AND message.character_id = session.character_id
              AND message.generation = session.generation
        ), 0)""")

    op.execute("""ALTER TABLE chat_messages
        ADD COLUMN turn_state text""")
    # Existing rows were written by the old paired replace path.  Only a
    # reliable user/assistant pair is promoted to completed; all other user
    # rows remain conservatively interrupted instead of inventing completion.
    op.execute("""UPDATE chat_messages AS user_message
        SET turn_state = CASE WHEN EXISTS (
            SELECT 1 FROM chat_messages AS assistant_message
            WHERE assistant_message.session_id = user_message.session_id
              AND assistant_message.user_id = user_message.user_id
              AND assistant_message.character_id = user_message.character_id
              AND assistant_message.generation = user_message.generation
              AND assistant_message.turn_id = user_message.turn_id
              AND assistant_message.role = 'assistant'
              AND assistant_message.status = 'complete'
        ) THEN 'completed' ELSE 'interrupted' END
        WHERE user_message.role = 'user'""")
    op.execute("""ALTER TABLE chat_messages
        ADD CONSTRAINT chat_messages_turn_state_check CHECK (
            (role = 'user' AND turn_state IN ('pending', 'completed', 'interrupted', 'failed'))
            OR (role = 'assistant' AND turn_state IS NULL)
        )""")


def downgrade() -> None:
    raise RuntimeError(
        "chat context convergence migration is data-bearing; restore a backup before downgrade"
    )
