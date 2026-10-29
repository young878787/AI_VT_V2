"""Clear per-turn emotion states before switching to mood_v1 records.

Revision ID: 0009_mood_v1_emotion
Revises: 0008_chat_context_convergence
"""

from alembic import op


revision = "0009_mood_v1_emotion"
down_revision = "0008_chat_context_convergence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old six-field states are per-turn probabilities, not accumulated mood;
    # sessions restart from the mood baseline instead of converting them.
    op.execute("UPDATE chat_sessions SET emotion_state = NULL")


def downgrade() -> None:
    # Cleared states cannot be restored; the schema itself is unchanged.
    pass
