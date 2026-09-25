"""長期記憶的 owner-scoped PostgreSQL 存取。"""

from uuid import UUID
import re

from pgvector import Vector
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from domain.memory_routing import MemoryRouting
from domain.memory_scope import MemoryScope, conversation_id, message_id


class MemoryRepository:
    def __init__(self, pool: AsyncConnectionPool, scope: MemoryScope) -> None:
        self.pool = pool
        self.scope = scope

    async def accept(self, session_id: str, turn_id: str) -> UUID:
        """先建立無原文 terminal record；JEV 中斷時保持安全的 NONE。"""
        event_id = message_id(session_id, turn_id)
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    "INSERT INTO memory_scope_state (user_id, character_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", owner,
                )
                generation = (await (await connection.execute(
                    "SELECT generation FROM memory_scope_state WHERE user_id = %s AND character_id = %s FOR SHARE", owner,
                )).fetchone())[0]
                await connection.execute(
                    """INSERT INTO memory_jobs
                    (id, user_id, character_id, generation, conversation_id, message_id, route, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'none', 'ignored')
                    ON CONFLICT (user_id, character_id, message_id) DO NOTHING""",
                    (event_id, *owner, generation, conversation_id(session_id), event_id),
                )
        return event_id

    async def route(
        self, event_id: UUID, routing: MemoryRouting, text: str,
        recent_dialogue: list[dict], embedding: list[float] | None = None,
    ) -> bool:
        owner = (self.scope.user_id, self.scope.character_id)
        status = {"none": "ignored", "buffer": "buffered", "process": "pending"}[routing.route]
        dialogue = [
            {"role": item["role"], "content": item["content"][:500]}
            for item in recent_dialogue[-16:]
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ]
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_jobs AS job SET route = %s, route_confidence = %s,
                    memory_type_hint = %s, importance_hint = %s, explicit_memory = %s,
                    source_text = %s, recent_dialogue = %s, embedding = %s,
                    status = %s, route_finalized = true, updated_at = now()
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.generation = state.generation
                    AND state.user_id = job.user_id AND state.character_id = job.character_id
                    AND job.route = 'none' AND job.status = 'ignored' AND NOT job.route_finalized""",
                    (routing.route, routing.confidence,
                     routing.memory_type if routing.route != "none" else None,
                     routing.importance if routing.route != "none" else None,
                     routing.explicit_memory if routing.route != "none" else None,
                     text[:4000] if routing.route != "none" else None,
                     Jsonb(dialogue) if routing.route != "none" else None,
                     Vector(embedding) if embedding is not None and routing.route != "none" else None,
                     status, event_id, *owner),
                )
                return cursor.rowcount == 1

    async def claim(self) -> dict | None:
        """多 worker 透過 lease 與 SKIP LOCKED 排他取得同 owner 的工作。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """WITH candidate AS (
                        SELECT job.id, job.user_id, job.character_id FROM memory_jobs AS job
                        JOIN memory_scope_state AS state ON state.user_id = job.user_id
                        AND state.character_id = job.character_id AND state.generation = job.generation
                        WHERE job.user_id = %s AND job.character_id = %s
                        AND (job.status IN ('pending', 'retry') OR (job.status = 'running' AND job.lease_until < now()))
                        ORDER BY job.created_at FOR UPDATE OF job SKIP LOCKED LIMIT 1
                    )
                    UPDATE memory_jobs AS job SET status = 'running', attempts = attempts + 1,
                    lease_until = now() + interval '120 seconds', updated_at = now()
                    FROM candidate WHERE job.id = candidate.id
                    AND job.user_id = candidate.user_id AND job.character_id = candidate.character_id
                    RETURNING job.*""", owner,
                )
                async with cursor:
                    row = await cursor.fetchone()
                    return dict(zip([col.name for col in cursor.description], row)) if row else None

    async def finish(self, job: dict, status: str, decisions: object = None, error: str | None = None) -> bool:
        if status not in {"done", "ignored", "retry", "failed", "cancelled"}:
            raise ValueError("無效的 job terminal status")
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_jobs AS job SET status = %s, decisions = %s, error = %s,
                    lease_until = NULL, updated_at = now()
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.status = 'running' AND job.generation = %s
                    AND job.attempts = %s
                    AND state.user_id = job.user_id AND state.character_id = job.character_id
                    AND state.generation = job.generation""",
                    (status, Jsonb(decisions) if decisions is not None else None,
                     error[:300] if error else None, job["id"], *owner, job["generation"], job["attempts"]),
                )
                return cursor.rowcount == 1

    async def expire_buffers(self) -> int:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_jobs SET status = 'discarded', source_text = NULL,
                    recent_dialogue = NULL, embedding = NULL, updated_at = now()
                    WHERE user_id = %s AND character_id = %s AND status = 'buffered'
                    AND created_at < now() - interval '24 hours'""", owner,
                )
                return cursor.rowcount

    async def expire_temporary(self) -> int:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_items SET status = 'expired', valid_to = now(), updated_at = now()
                    WHERE user_id = %s AND character_id = %s AND status = 'active'
                    AND retention_class = 'temporary' AND expires_at <= now()""", owner,
                )
                return cursor.rowcount

    async def reset(self) -> None:
        """generation 鎖與刪除同 transaction，阻止已在 LLM 呼叫的舊 worker 回寫。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    "INSERT INTO memory_scope_state (user_id, character_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", owner,
                )
                await connection.execute(
                    """UPDATE memory_scope_state SET generation = generation + 1
                    WHERE user_id = %s AND character_id = %s""", owner,
                )
                await connection.execute(
                    """UPDATE memory_jobs SET status = 'cancelled', source_text = NULL,
                    recent_dialogue = NULL, embedding = NULL, decisions = NULL,
                    lease_until = NULL, updated_at = now()
                    WHERE user_id = %s AND character_id = %s""", owner,
                )
                await connection.execute("DELETE FROM memory_items WHERE user_id = %s AND character_id = %s", owner)
                await connection.execute("DELETE FROM memory_sources WHERE user_id = %s AND character_id = %s", owner)
                await connection.execute("DELETE FROM memory_audit WHERE user_id = %s AND character_id = %s", owner)

    async def related_items(self, text: str, embedding: list[float] | None, limit: int = 20,
                            mode: str = "current") -> list[dict]:
        """同 owner 的有界相關記憶；目前事實與歷史分開篩選。"""
        if mode not in {"current", "history"}:
            raise ValueError("Memory query mode 無效")
        owner = (self.scope.user_id, self.scope.character_id)
        words = re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]{2,4}", text.lower())[:6]
        term_sql = sql.SQL(" OR ").join(
            sql.SQL("canonical_text ILIKE %s OR COALESCE(subject_key, '') ILIKE %s") for _ in words
        )
        exact_sql = sql.SQL("({} OR keywords && %s::text[])").format(term_sql) if words else sql.SQL("FALSE")
        vector_sql = sql.SQL("embedding IS NOT NULL") if embedding is not None else sql.SQL("FALSE")
        ranking = sql.SQL("embedding <=> %s") if embedding is not None else sql.SQL("NULL::float")
        terms = tuple(value for word in words for value in (f"%{word}%", f"%{word}%"))
        exact_args = (*terms, words) if words else ()
        status_sql = (sql.SQL("status = 'active' AND (expires_at IS NULL OR expires_at > now())")
                      if mode == "current" else sql.SQL("status IN ('active', 'superseded', 'expired', 'archived')"))
        query = sql.SQL(
            """SELECT id, group_id, memory_type, canonical_text, subject_key, keywords,
            status, importance, confidence, retention_class, valid_from, valid_to, expires_at
            FROM (
                SELECT *, {} AS exact_match, {} AS distance FROM memory_items
                WHERE user_id = %s AND character_id = %s AND {}
            ) AS ranked WHERE exact_match OR {}
            ORDER BY exact_match DESC, distance ASC NULLS LAST, importance DESC LIMIT %s"""
        ).format(exact_sql, ranking, status_sql, vector_sql)
        args = (*exact_args, *((Vector(embedding),) if embedding is not None else ()), *owner,
                min(max(limit, 1), 20))
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(query, args) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(zip([col.name for col in cursor.description], row)) for row in rows]

    async def profile_items(self, limit: int = 30) -> list[dict]:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT subject_key, canonical_text FROM memory_items
                    WHERE user_id = %s AND character_id = %s AND memory_type = 'profile'
                    AND status = 'active' AND (expires_at IS NULL OR expires_at > now())
                    ORDER BY importance DESC, updated_at DESC LIMIT %s""",
                    (*owner, min(max(limit, 1), 30)),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(zip([col.name for col in cursor.description], row)) for row in rows]

    async def related_buffers(self, memory_type: str, embedding: list[float]) -> list[dict]:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT id, source_text, recent_dialogue, memory_type_hint, importance_hint
                    FROM memory_jobs WHERE user_id = %s AND character_id = %s
                    AND status = 'buffered' AND memory_type_hint = %s
                    AND embedding IS NOT NULL AND created_at >= now() - interval '24 hours'
                    ORDER BY embedding <=> %s LIMIT 3""",
                    (*owner, memory_type, Vector(embedding)),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(zip([col.name for col in cursor.description], row)) for row in rows]

    async def unembedded_buffers(self) -> list[dict]:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT id, source_text, generation FROM memory_jobs
                    WHERE user_id = %s AND character_id = %s AND status = 'buffered'
                    AND embedding IS NULL AND source_text IS NOT NULL
                    ORDER BY created_at LIMIT 10""", owner,
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(zip([col.name for col in cursor.description], row)) for row in rows]

    async def save_buffer_embedding(self, job: dict, embedding: list[float]) -> bool:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_jobs AS job SET embedding = %s, updated_at = now()
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.status = 'buffered' AND job.embedding IS NULL
                    AND job.generation = %s AND state.user_id = job.user_id
                    AND state.character_id = job.character_id AND state.generation = job.generation""",
                    (Vector(embedding), job["id"], *owner, job["generation"]),
                )
                return cursor.rowcount == 1
