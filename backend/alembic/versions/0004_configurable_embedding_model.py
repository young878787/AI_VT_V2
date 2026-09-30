"""Allow configured embedding models and tag buffered vectors.

Revision ID: 0004_embedding_model
Revises: 0003_embedding_diagnostics
"""

import hashlib
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "0004_embedding_model"
down_revision = "0003_embedding_diagnostics"
branch_labels = None
depends_on = None

LEGACY_MODEL = "Qwen/Qwen3-Embedding-0.6B"
LEGACY_QUERY_PREFIX = (
    "Given a user's current message, retrieve memories about the same person, preference, "
    "project, event, or correction that are relevant to answering the message.\n"
)
LEGACY_CONTRACT = hashlib.sha256(json.dumps(
    [LEGACY_MODEL, 1024, LEGACY_QUERY_PREFIX, "", "l2-v1"],
    ensure_ascii=False, separators=(",", ":"),
).encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.add_column("memory_items", sa.Column("embedding_contract", sa.Text(), nullable=True))
    op.add_column("memory_jobs", sa.Column("embedding_model", sa.Text(), nullable=True))
    op.add_column("memory_jobs", sa.Column("embedding_contract", sa.Text(), nullable=True))
    op.execute("ALTER TABLE memory_items DROP CONSTRAINT memory_items_embedding_model_check")
    op.get_bind().execute(
        text("UPDATE memory_items SET embedding_model = :model WHERE embedding IS NOT NULL AND embedding_model IS NULL"),
        {"model": LEGACY_MODEL},
    )
    op.get_bind().execute(
        text("UPDATE memory_items SET embedding_contract = :contract WHERE embedding IS NOT NULL"),
        {"contract": LEGACY_CONTRACT},
    )
    op.get_bind().execute(
        text("UPDATE memory_jobs SET embedding_model = :model WHERE embedding IS NOT NULL"),
        {"model": LEGACY_MODEL},
    )
    op.get_bind().execute(
        text("UPDATE memory_jobs SET embedding_contract = :contract WHERE embedding IS NOT NULL"),
        {"contract": LEGACY_CONTRACT},
    )


def downgrade() -> None:
    incompatible = op.get_bind().exec_driver_sql(
        f"""SELECT EXISTS (
            SELECT 1 FROM memory_items
            WHERE embedding IS NOT NULL AND (
                embedding_model IS DISTINCT FROM 'Qwen/Qwen3-Embedding-0.6B'
                OR embedding_contract IS DISTINCT FROM '{LEGACY_CONTRACT}'
            )
        ) OR EXISTS (
            SELECT 1 FROM memory_jobs
            WHERE embedding IS NOT NULL AND (
                embedding_model IS DISTINCT FROM 'Qwen/Qwen3-Embedding-0.6B'
                OR embedding_contract IS DISTINCT FROM '{LEGACY_CONTRACT}'
            )
        )"""
    ).scalar_one()
    if incompatible:
        raise RuntimeError("無法降版：資料庫已有非 Qwen embedding，請先重新產生向量")
    op.drop_column("memory_jobs", "embedding_model")
    op.drop_column("memory_jobs", "embedding_contract")
    op.drop_column("memory_items", "embedding_contract")
    op.create_check_constraint(
        "memory_items_embedding_model_check", "memory_items",
        "embedding_model IS NULL OR embedding_model = 'Qwen/Qwen3-Embedding-0.6B'",
    )
