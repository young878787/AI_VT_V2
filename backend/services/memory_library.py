"""記憶圖書館的 owner-scoped 查詢、匯出與破壞性操作。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from psycopg import sql

from core.utils import normalize_session_id
from infrastructure.chat_session_repository import ChatSessionRepository
from infrastructure.memory_repository import MemoryRepository


CURRENT_MEMORY_STATUSES = {"active", "conflict"}
HISTORICAL_MEMORY_STATUSES = {"superseded", "merged", "expired", "archived"}
ALL_MEMORY_STATUSES = CURRENT_MEMORY_STATUSES | HISTORICAL_MEMORY_STATUSES


def _row_dicts(cursor, rows: list[tuple]) -> list[dict[str, Any]]:
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def _json_value(value: Any) -> Any:
    """將 psycopg／pgvector 值轉成穩定的 JSON primitive。"""
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
        return value.isoformat()
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _vector_values(value: Any) -> list[float] | None:
    if value is None:
        return None
    if hasattr(value, "to_list"):
        value = value.to_list()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = value.strip("[]").split(",")
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [float(item) for item in value]


class MemoryLibraryService:
    """所有查詢都固定到 runtime 的 MemoryRepository scope。"""

    def __init__(self, repository: MemoryRepository,
                 chat_repository: ChatSessionRepository | None = None) -> None:
        self.repository = repository
        self.chat_repository = chat_repository or ChatSessionRepository(repository.pool, repository.scope)
        self.pool = repository.pool
        self.scope = repository.scope

    async def _set_search_path(self, connection) -> None:
        await connection.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(
                sql.Identifier(self.scope.schema_name)
            )
        )

    async def overview(self) -> dict[str, Any]:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                memory_cursor = await connection.execute(
                    """SELECT count(*) AS total,
                    count(*) FILTER (WHERE status IN ('active', 'conflict')) AS current,
                    count(*) FILTER (WHERE status NOT IN ('active', 'conflict')) AS historical,
                    count(*) FILTER (WHERE status = 'conflict') AS conflict,
                    count(*) FILTER (WHERE embedding IS NOT NULL) AS vectorized
                    FROM memory_items WHERE user_id = %s AND character_id = %s""",
                    owner,
                )
                memory = dict(zip([column.name for column in memory_cursor.description], await memory_cursor.fetchone()))
                source_cursor = await connection.execute(
                    """SELECT count(*) FROM memory_sources
                    WHERE user_id = %s AND character_id = %s AND raw_text IS NOT NULL""",
                    owner,
                )
                source_count = (await source_cursor.fetchone())[0]

        queue = await self.repository.queue_health()
        sessions = await self.chat_repository.list_sessions()
        return {
            "contract_version": 1,
            "long_term": {
                "total": int(memory["total"]),
                "current": int(memory["current"]),
                "historical": int(memory["historical"]),
                "conflict": int(memory["conflict"]),
                "vectorized": int(memory["vectorized"]),
                "source_count": int(source_count),
            },
            "sessions": {
                "count": len(sessions),
                "message_count": sum(item["message_count"] for item in sessions),
            },
            "queue": {key: int(value or 0) if key != "oldest_age_sec" else float(value or 0)
                      for key, value in queue.items()},
        }

    async def list_memories(
        self, *, query: str = "", status: str = "current", memory_type: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        if status not in {"current", "history", "all", *ALL_MEMORY_STATUSES}:
            raise ValueError("無效的 memory status")
        limit = min(max(int(limit), 1), 100)
        offset = min(max(int(offset), 0), 10000)
        owner = (self.scope.user_id, self.scope.character_id)
        conditions = ["m.user_id = %s", "m.character_id = %s"]
        args: list[Any] = [*owner]
        display_args: list[Any] = []
        display_filter = "TRUE"
        if status == "current":
            display_filter = "r.status IN ('active', 'conflict')"
        elif status == "history":
            display_filter = "r.status NOT IN ('active', 'conflict')"
        elif status in ALL_MEMORY_STATUSES:
            display_filter = "r.status = %s"
            display_args.append(status)
        if memory_type:
            conditions.append("m.memory_type = %s")
            args.append(memory_type)
        if query.strip():
            pattern = f"%{query.strip()}%"
            conditions.append(
                "(m.canonical_text ILIKE %s OR COALESCE(m.subject_key, '') ILIKE %s "
                "OR array_to_string(m.keywords, ' ') ILIKE %s)"
            )
            args.extend([pattern, pattern, pattern])

        where = " AND ".join(conditions)
        query_args = [*args, *display_args, limit, offset]
        statement = f"""
            WITH ranked AS (
                SELECT m.*,
                    count(*) OVER (PARTITION BY m.group_id) AS version_count,
                    bool_or(m.status = 'conflict') OVER (PARTITION BY m.group_id) AS has_conflict,
                    row_number() OVER (
                        PARTITION BY m.group_id
                        ORDER BY CASE WHEN m.status IN ('active', 'conflict') THEN 0 ELSE 1 END,
                                 m.observed_at DESC, m.updated_at DESC, m.id DESC
                    ) AS group_rank
                FROM memory_items AS m
                WHERE {where}
            )
            SELECT r.id, r.group_id, r.memory_type, r.canonical_text, r.subject_key,
                r.keywords, r.status, r.importance, r.confidence, r.retention_class,
                r.valid_from, r.valid_to, r.expires_at, r.observed_at, r.updated_at,
                r.version_count, r.has_conflict, r.embedding IS NOT NULL AS embedding_present,
                r.embedding_model, r.embedding_contract,
                (SELECT count(*) FROM memory_evidence e
                 WHERE e.user_id = r.user_id AND e.character_id = r.character_id
                 AND e.memory_id = r.id) AS evidence_count
            FROM ranked AS r
            WHERE r.group_rank = 1 AND {display_filter}
            ORDER BY r.updated_at DESC, r.id DESC
            LIMIT %s OFFSET %s
        """
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(statement, query_args)
                rows = _row_dicts(cursor, await cursor.fetchall())
        return {"items": [_json_value(row) for row in rows], "limit": limit, "offset": offset}

    async def memory_detail(self, group_id: UUID) -> dict[str, Any] | None:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(
                    """SELECT id, group_id, memory_type, canonical_text, subject_key, keywords,
                    status, importance, confidence, retention_class, emotion_metadata,
                    valid_from, valid_to, expires_at, observed_at, created_at, updated_at,
                    embedding IS NOT NULL AS embedding_present, embedding_model, embedding_contract
                    FROM memory_items WHERE user_id = %s AND character_id = %s AND group_id = %s
                    ORDER BY observed_at DESC, updated_at DESC, id DESC""",
                    (*owner, group_id),
                )
                versions = _row_dicts(cursor, await cursor.fetchall())
                if not versions:
                    return None
                ids = [row["id"] for row in versions]
                evidence_cursor = await connection.execute(
                    """SELECT e.memory_id, e.source_id, e.kind, s.speaker,
                    left(COALESCE(s.raw_text, ''), 1000) AS excerpt, s.occurred_at
                    FROM memory_evidence AS e
                    JOIN memory_sources AS s ON (s.id, s.user_id, s.character_id) =
                        (e.source_id, e.user_id, e.character_id)
                    WHERE e.user_id = %s AND e.character_id = %s AND e.memory_id = ANY(%s)
                    ORDER BY s.occurred_at DESC""",
                    (*owner, ids),
                )
                evidence = _row_dicts(evidence_cursor, await evidence_cursor.fetchall())
                relation_cursor = await connection.execute(
                    """SELECT from_id, to_id, kind FROM memory_relations
                    WHERE user_id = %s AND character_id = %s
                    AND (from_id = ANY(%s) OR to_id = ANY(%s))""",
                    (*owner, ids, ids),
                )
                relations = _row_dicts(relation_cursor, await relation_cursor.fetchall())
        return {
            "group_id": str(group_id),
            "versions": [_json_value(row) for row in versions],
            "evidence": [_json_value(row) for row in evidence],
            "relations": [_json_value(row) for row in relations],
        }

    async def delete_memory_group(self, group_id: UUID) -> dict[str, Any] | None:
        """以與 FORGET 相同的來源清理與 barrier 語意刪除整個記憶家族。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                seed_cursor = await connection.execute(
                    """SELECT id, subject_key, canonical_text FROM memory_items
                    WHERE user_id = %s AND character_id = %s AND group_id = %s FOR UPDATE""",
                    (*owner, group_id),
                )
                seed_rows = await seed_cursor.fetchall()
                if not seed_rows:
                    return None
                family_cursor = await connection.execute(
                    """WITH RECURSIVE family(id) AS (
                        SELECT id FROM memory_items
                        WHERE user_id = %s AND character_id = %s AND group_id = %s
                        UNION
                        SELECT CASE WHEN relation.from_id = family.id
                                    THEN relation.to_id ELSE relation.from_id END
                        FROM memory_relations AS relation
                        JOIN family ON relation.from_id = family.id OR relation.to_id = family.id
                        WHERE relation.user_id = %s AND relation.character_id = %s
                        AND relation.kind IN ('supersedes', 'merged_into', 'contradicts')
                    ) SELECT DISTINCT id FROM family""",
                    (*owner, group_id, *owner),
                )
                target_ids = [row[0] for row in await family_cursor.fetchall()]
                target_rows_cursor = await connection.execute(
                    """SELECT id, subject_key, canonical_text FROM memory_items
                    WHERE user_id = %s AND character_id = %s AND id = ANY(%s) FOR UPDATE""",
                    (*owner, target_ids),
                )
                target_rows = await target_rows_cursor.fetchall()

                source_cursor = await connection.execute(
                    """SELECT DISTINCT e.source_id, s.message_id
                    FROM memory_evidence AS e
                    JOIN memory_sources AS s ON (s.id, s.user_id, s.character_id) =
                        (e.source_id, e.user_id, e.character_id)
                    WHERE e.user_id = %s AND e.character_id = %s AND e.memory_id = ANY(%s)""",
                    (*owner, target_ids),
                )
                source_rows = await source_cursor.fetchall()
                source_ids = list({row[0] for row in source_rows})
                message_ids = list({row[1] for row in source_rows if row[1] is not None})

                jobs_cursor = await connection.execute(
                    """SELECT id, message_id FROM memory_jobs
                    WHERE user_id = %s AND character_id = %s
                    AND (source_ids && %s::uuid[] OR pending_target_ids && %s::uuid[]
                         OR id = ANY(%s))""",
                    (*owner, source_ids, target_ids, source_ids),
                )
                job_rows = await jobs_cursor.fetchall()
                affected_job_ids = list({row[0] for row in job_rows})
                message_ids = list({*message_ids, *(row[1] for row in job_rows if row[1] is not None)})

                duplicate_cursor = await connection.execute(
                    """SELECT id FROM memory_sources
                    WHERE user_id = %s AND character_id = %s
                    AND (id = ANY(%s) OR message_id = ANY(%s)
                         OR raw_text IN (SELECT raw_text FROM memory_sources
                                         WHERE user_id = %s AND character_id = %s
                                         AND id = ANY(%s) AND raw_text IS NOT NULL))""",
                    (*owner, source_ids, message_ids, *owner, source_ids),
                )
                erased_source_ids = list({*source_ids, *(row[0] for row in await duplicate_cursor.fetchall())})

                for _, subject_key, canonical_text in target_rows:
                    await connection.execute(
                        """INSERT INTO memory_forget_barriers
                        (user_id, character_id, subject_hash, fact_hash)
                        VALUES (%s, %s, md5(%s), md5(%s))""",
                        (*owner, subject_key, canonical_text),
                    )

                if erased_source_ids:
                    await connection.execute(
                        """UPDATE memory_sources SET raw_text = NULL
                        WHERE user_id = %s AND character_id = %s AND id = ANY(%s)""",
                        (*owner, erased_source_ids),
                    )
                if target_ids or message_ids:
                    await connection.execute(
                        """UPDATE memory_audit SET decision = NULL, target_id = NULL,
                        reason_class = 'forgotten'
                        WHERE user_id = %s AND character_id = %s
                        AND (target_id = ANY(%s) OR source_event_id = ANY(%s))""",
                        (*owner, target_ids, message_ids),
                    )
                if affected_job_ids or message_ids:
                    await connection.execute(
                        """UPDATE memory_jobs SET status = 'cancelled', recent_dialogue = NULL,
                        missing_context = NULL, source_ids = '{}', pending_target_ids = '{}',
                        agent_diagnostics = '{}'::jsonb, embedding_diagnostics = '{}'::jsonb,
                        lease_until = NULL, updated_at = now()
                        WHERE user_id = %s AND character_id = %s
                        AND (id = ANY(%s) OR message_id = ANY(%s))""",
                        (*owner, affected_job_ids, message_ids),
                    )

                deleted_cursor = await connection.execute(
                    """DELETE FROM memory_items
                    WHERE user_id = %s AND character_id = %s AND id = ANY(%s)""",
                    (*owner, target_ids),
                )
                if erased_source_ids:
                    await connection.execute(
                        """DELETE FROM memory_sources AS source
                        WHERE source.user_id = %s AND source.character_id = %s
                        AND source.id = ANY(%s)
                        AND NOT EXISTS (SELECT 1 FROM memory_evidence AS evidence
                            WHERE evidence.source_id = source.id
                            AND evidence.user_id = source.user_id
                            AND evidence.character_id = source.character_id)""",
                        (*owner, erased_source_ids),
                    )
                await connection.execute(
                    """INSERT INTO memory_audit
                    (id, user_id, character_id, operation_key, action, reason_class, deleted_count)
                    VALUES (%s, %s, %s, %s, 'FORGET', 'user_request', %s)""",
                    (uuid4(), *owner, f"ui:forget:{group_id}:{uuid4()}", deleted_cursor.rowcount),
                )
                return {
                    "group_id": str(group_id),
                    "deleted_memory_count": deleted_cursor.rowcount,
                    "affected_job_count": len(affected_job_ids),
                    "redacted_source_count": len(erased_source_ids),
                }

    async def purge_owner_data(self) -> dict[str, Any]:
        """清除 owner 的所有長期資料，保留 schema 與 generation safety row。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                await connection.execute(
                    """INSERT INTO memory_scope_state (user_id, character_id)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING""", owner,
                )
                await connection.execute(
                    """UPDATE memory_scope_state SET generation = generation + 1
                    WHERE user_id = %s AND character_id = %s""", owner,
                )
                counts: dict[str, int] = {}
                for table in ("memory_jobs", "memory_items", "memory_sources", "memory_audit", "memory_forget_barriers"):
                    cursor = await connection.execute(
                        sql.SQL("DELETE FROM {} WHERE user_id = %s AND character_id = %s").format(sql.Identifier(table)),
                        owner,
                    )
                    counts[table] = cursor.rowcount
                return {"purged": counts}

    async def export_jsonl(self) -> AsyncIterator[str]:
        """以 bounded fetch 將 owner 的向量記憶串流為 JSONL。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                count_cursor = await connection.execute(
                    "SELECT count(*) FROM memory_items WHERE user_id = %s AND character_id = %s", owner,
                )
                item_count = (await count_cursor.fetchone())[0]
                yield json.dumps({
                    "record_type": "manifest",
                    "schema_version": 1,
                    "embedding_dimension": 1024,
                    "embedding_model": self.repository.embedding_model,
                    "embedding_contract": self.repository.embedding_contract,
                    "item_count": item_count,
                }, ensure_ascii=False) + "\n"
                cursor = await connection.execute(
                    """SELECT id, group_id, memory_type, canonical_text, subject_key, keywords,
                    status, importance, confidence, retention_class, emotion_metadata,
                    observed_at, valid_from, valid_to, expires_at, created_at, updated_at,
                    embedding, embedding_model, embedding_contract
                    FROM memory_items WHERE user_id = %s AND character_id = %s
                    ORDER BY created_at, id""",
                    owner,
                )
                while True:
                    rows = await cursor.fetchmany(50)
                    if not rows:
                        break
                    for row in rows:
                        record = dict(zip([column.name for column in cursor.description], row))
                        record["record_type"] = "memory"
                        record["embedding"] = _vector_values(record["embedding"])
                        yield json.dumps(_json_value(record), ensure_ascii=False) + "\n"

    async def sessions(self) -> list[dict[str, Any]]:
        return await self.chat_repository.list_sessions()

    async def session(self, session_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any] | None:
        normalized = normalize_session_id(session_id)
        if normalized is None:
            raise ValueError("無效的 session_id")
        if hasattr(self.chat_repository, "load_session_messages"):
            return await self.chat_repository.load_session_messages(
                normalized, limit=limit, offset=offset,
            )
        return await self.chat_repository.load(normalized)

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        normalized = normalize_session_id(session_id)
        if normalized is None:
            raise ValueError("無效的 session_id")
        return await self.chat_repository.delete(normalized)

    async def delete_all_sessions(self) -> dict[str, Any]:
        return await self.chat_repository.delete_all()
