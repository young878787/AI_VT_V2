"""長期記憶連線與 Alembic 版本檢查。"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from pgvector.psycopg import register_vector_async
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from domain.memory_scope import MemoryScope


ALEMBIC_CONFIG = Path(__file__).resolve().parents[1] / "alembic.ini"


async def check_schema(pool: AsyncConnectionPool, scope: MemoryScope) -> None:
    """正式啟動僅驗證版本、pgvector 與固定向量維度。"""
    expected_revision = ScriptDirectory.from_config(Config(str(ALEMBIC_CONFIG))).get_current_head()
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(scope.schema_name)))
            vector = await (await connection.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).fetchone()
            if vector is None:
                raise RuntimeError("pgvector 尚未安裝")
            version_table = sql.SQL("{}.alembic_version").format(sql.Identifier(scope.schema_name))
            try:
                revisions = await (await connection.execute(sql.SQL("SELECT version_num FROM {}").format(version_table))).fetchall()
            except Exception as exc:
                raise RuntimeError("Memory schema 尚未套用 Alembic migration") from exc
            if [row[0] for row in revisions] != [expected_revision]:
                raise RuntimeError("Memory Alembic revision 不符")
            dimension = await (await connection.execute(
                "SELECT atttypmod FROM pg_attribute WHERE attrelid = 'memory_items'::regclass AND attname = 'embedding'"
            )).fetchone()
            if dimension is None or dimension[0] != 1024:
                raise RuntimeError("Memory embedding 維度必須為 1024")
            chat_tables = await (await connection.execute(
                "SELECT to_regclass('chat_sessions'), to_regclass('chat_messages')"
            )).fetchone()
            if chat_tables != ("chat_sessions", "chat_messages"):
                raise RuntimeError("Chat session schema 尚未套用 Alembic migration")


async def make_pool(database_url: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(database_url, open=False, configure=register_vector_async)
    await pool.open()
    return pool
