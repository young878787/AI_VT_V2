"""長期記憶的 owner-scoped PostgreSQL 存取。"""

from uuid import UUID
import re

from pgvector import Vector
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from domain.memory_routing import MemoryRouting, instruction_policy
from domain.memory_scope import MemoryScope, conversation_id, message_id
from domain.memory_source import MemoryEventConflict, MemoryEventReplay, read_memory_source
from core.prompt_logger import test_log_dir, trace, trace_event, trace_turn


MIN_RETRIEVAL_SIMILARITY = 0.75
_GENERIC_QUERY_WORDS = {"喜歡", "記得", "以前", "之前", "曾經", "過去", "使用者", "什麼", "知道", "我的", "你的", "使用", "用者", "現在", "平常", "最近", "幫我", "記住", "記憶", "這件", "件事", "更正", "只有", "相關", "的人"}
_QUERY_CATEGORY_ALIASES = {"健康": "health", "甜點": "dessert", "甜食": "sweet",
                           "咖啡": "coffee", "飲料": "drink", "飲品": "飲料", "蛋糕": "甜點", "遊戲": "game"}


class MemoryRepository:
    def __init__(
        self, pool: AsyncConnectionPool, scope: MemoryScope,
        embedding_model: str | None = None, embedding_contract: str | None = None,
    ) -> None:
        self.pool = pool
        self.scope = scope
        self.embedding_model = embedding_model
        self.embedding_contract = embedding_contract

    async def accept_event(
        self, session_id: str, turn_id: str, text: str | None = None,
    ) -> tuple[UUID, int]:
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
                inserted = await connection.execute(
                    """INSERT INTO memory_jobs
                    (id, user_id, character_id, generation, conversation_id, message_id, route, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'none', 'ignored')
                    ON CONFLICT (user_id, character_id, message_id) DO NOTHING""",
                    (event_id, *owner, generation, conversation_id(session_id), event_id),
                )
                if inserted.rowcount == 0 and text is not None:
                    existing = await (await connection.execute(
                        """SELECT job.generation, job.status, source.raw_text
                        FROM memory_jobs AS job LEFT JOIN memory_sources AS source
                        ON source.id = job.id AND source.user_id = job.user_id
                        AND source.character_id = job.character_id
                        WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s""",
                        (event_id, *owner),
                    )).fetchone()
                    if (existing is None or existing[0] != generation
                            or existing[1] in {"cancelled", "discarded"}):
                        raise MemoryEventConflict("turn_id 已屬於失效的 memory generation")
                    raw_text = existing[2]
                    if raw_text == text and len(text) <= 4001:
                        raise MemoryEventReplay("turn_id 與內容已接收")
                    raise MemoryEventConflict("turn_id 已綁定其他或無法驗證的內容")
        return event_id, generation

    async def accept(self, session_id: str, turn_id: str) -> UUID:
        event_id, _ = await self.accept_event(session_id, turn_id)
        return event_id

    async def route(
        self, event_id: UUID, routing: MemoryRouting, text: str,
        recent_dialogue: list[dict], *, finalized: bool = True,
    ) -> bool:
        owner = (self.scope.user_id, self.scope.character_id)
        policy = instruction_policy(text)
        # 持久化邊界也拒絕禁存原文，不能依賴呼叫端提供正確 route。
        if policy == "no_store":
            routing = MemoryRouting("none")
            finalized = True
        status = {"none": "ignored", "needs_context": "buffered", "process": "pending", None: "pending"}[routing.route]
        history = [
            item for item in recent_dialogue[-8:]
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ]
        dialogue = [
            {"role": item["role"], "content": item["content"][:500]}
            for item in history
        ]
        history_sources = []
        for item in history:
            source = read_memory_source(item)
            if source is not None and source.policy != "no_store":
                history_sources.append((
                    source.source_id, item["content"][:4001], source.source_id,
                    source.occurred_at, source.generation,
                ))
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_jobs AS job SET route = %s, route_confidence = %s,
                    recent_dialogue = %s,
                    status = %s, instruction = %s, error = %s, route_finalized = %s, updated_at = now(),
                    agent_diagnostics = %s::jsonb
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.generation = state.generation
                    AND state.user_id = job.user_id AND state.character_id = job.character_id
                    AND job.status IN ('ignored', 'pending') AND NOT job.route_finalized""",
                    (routing.route or "process", routing.confidence,
                     Jsonb([item for item in dialogue if item["role"] == "assistant"][-2:]) if routing.route != "none" else None,
                     status, policy, routing.error, finalized,
                     Jsonb({"role": "policy" if policy == "no_store" else "jev",
                             "result": routing.route or "review", "confidence": routing.confidence,
                             "error": routing.error}) if finalized else Jsonb({}), event_id, *owner),
                )
                changed = cursor.rowcount == 1
                if changed and routing.route != "none":
                    row = await (await connection.execute(
                        "SELECT conversation_id, created_at, generation FROM memory_jobs WHERE id = %s AND user_id = %s AND character_id = %s",
                        (event_id, *owner),
                    )).fetchone()
                    eligible_history = set()
                    if history_sources:
                        eligible_rows = await (await connection.execute(
                            """SELECT job.id FROM memory_jobs AS job
                            JOIN memory_scope_state AS state ON state.user_id = job.user_id
                            AND state.character_id = job.character_id AND state.generation = job.generation
                            WHERE job.user_id = %s AND job.character_id = %s
                            AND job.conversation_id = %s AND job.id = ANY(%s)
                            AND job.status NOT IN ('cancelled', 'discarded')""",
                            (*owner, row[0], [source[0] for source in history_sources]),
                        )).fetchall()
                        eligible_history = {value[0] for value in eligible_rows}
                    sources = [(event_id, text[:4001], event_id, row[1])]
                    sources += [source[:4] for source in history_sources[-4:]
                                if source[0] in eligible_history and source[4] == row[2]]
                    persisted_sources = []
                    for source_id, raw_text, source_message_id, occurred_at in sources:
                        persisted_sources.append(source_id)
                        await connection.execute(
                            """INSERT INTO memory_sources
                            (id, user_id, character_id, conversation_id, message_id, speaker, raw_text, occurred_at)
                            VALUES (%s, %s, %s, %s, %s, 'user', %s, %s) ON CONFLICT DO NOTHING""",
                            (source_id, *owner, row[0], source_message_id, raw_text, occurred_at),
                        )
                    await connection.execute(
                        "UPDATE memory_jobs SET source_ids = %s WHERE id = %s AND user_id = %s AND character_id = %s",
                        (persisted_sources, event_id, *owner),
                    )
                if changed and routing.route == "none":
                    await connection.execute(
                        """DELETE FROM memory_sources s WHERE s.user_id = %s AND s.character_id = %s
                        AND s.message_id = %s AND NOT EXISTS (SELECT 1 FROM memory_evidence e
                        WHERE (e.source_id,e.user_id,e.character_id) = (s.id,s.user_id,s.character_id))""",
                        (*owner, event_id),
                    )
                return changed

    async def record_embedding_diagnostic(self, event_id: UUID, diagnostic: dict) -> None:
        """只保存有界彙總，不追加每次 embedding 的輸入或向量。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    """UPDATE memory_jobs AS job SET embedding_diagnostics = jsonb_build_object(
                    'calls', COALESCE((embedding_diagnostics->>'calls')::int, 0) + 1,
                    'failures', COALESCE((embedding_diagnostics->>'failures')::int, 0) + %s,
                    'duration_ms', COALESCE((embedding_diagnostics->>'duration_ms')::float, 0) + %s,
                    'model', %s::text, 'serving_model', %s::text, 'served_model', %s::text,
                    'dimension', %s::int, 'normalized', %s::boolean)
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.generation = state.generation
                    AND state.user_id = job.user_id AND state.character_id = job.character_id""",
                    (int(diagnostic["status"] == "failed"), diagnostic["duration_ms"], diagnostic["model"],
                     diagnostic.get("serving_model"), diagnostic.get("served_model"), diagnostic["dimension"],
                     diagnostic["normalized"], event_id, *owner),
                )

    async def claim(self) -> dict | None:
        """多 worker 透過 lease 與 SKIP LOCKED 排他取得同 owner 的工作。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """WITH candidate AS (
                        SELECT job.id, job.user_id, job.character_id,
                        CASE WHEN job.status = 'running' THEN
                            EXTRACT(EPOCH FROM now() - job.lease_until)::float ELSE NULL END AS recovered_lease_age_sec
                        FROM memory_jobs AS job
                        JOIN memory_scope_state AS state ON state.user_id = job.user_id
                        AND state.character_id = job.character_id AND state.generation = job.generation
                        WHERE job.user_id = %s AND job.character_id = %s
                        AND (job.route_finalized OR job.created_at < now() - interval '60 seconds')
                        AND job.expires_at > now()
                        AND job.attempts < 3
                        AND (job.status <> 'retry' OR job.updated_at <= now() -
                            CASE WHEN job.attempts = 1 THEN interval '2 seconds' ELSE interval '4 seconds' END)
                        AND (job.status IN ('pending', 'retry') OR (job.status = 'running' AND job.lease_until < now()))
                        ORDER BY job.created_at - CASE WHEN job.instruction IN ('remember', 'forget') THEN interval '5 minutes' ELSE interval '0' END FOR UPDATE OF job SKIP LOCKED LIMIT 1
                    )
                    UPDATE memory_jobs AS job SET status = 'running', attempts = attempts + 1,
                    error = CASE WHEN NOT job.route_finalized THEN 'jev_timeout' ELSE job.error END, route_finalized = true,
                    pending_target_ids = '{}',
                    lease_until = now() + interval '120 seconds', updated_at = now()
                    FROM candidate WHERE job.id = candidate.id
                    AND job.user_id = candidate.user_id AND job.character_id = candidate.character_id
                    RETURNING job.*, candidate.recovered_lease_age_sec""", owner,
                )
                async with cursor:
                    row = await cursor.fetchone()
                    if row is None:
                        return None
                    job = dict(zip([col.name for col in cursor.description], row))
                    source = await (await connection.execute(
                        "SELECT raw_text FROM memory_sources WHERE id = %s AND user_id = %s AND character_id = %s",
                        (job["id"], *owner),
                    )).fetchone()
                    job["source_text"] = source[0] if source else None
                    if job["source_text"]:
                        policy = instruction_policy(job["source_text"])
                        if policy != job["instruction"]:
                            # 舊 migration 的來源可能尚未帶入 instruction；以保存原文重建政策。
                            await connection.execute(
                                "UPDATE memory_jobs SET instruction = %s WHERE id = %s AND user_id = %s AND character_id = %s",
                                (policy, job["id"], *owner),
                            )
                            job["instruction"] = policy
                    return job

    async def finish(self, job: dict, status: str, *, error: str | None = None,
                     validation_error: str | None = None, diagnostic: dict | None = None,
                     missing_context: str | None = None) -> bool:
        if status not in {"done", "ignored", "retry", "failed", "cancelled", "buffered"}:
            raise ValueError("無效的 job terminal status")
        owner = (self.scope.user_id, self.scope.character_id)
        summary = {**(diagnostic or {}), "attempt": job["attempts"], "result": status,
                   "error": error, "validation_error": validation_error[:300] if validation_error else None}
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_jobs AS job SET status = %s, error = %s,
                    route = CASE WHEN %s = 'buffered' THEN 'needs_context' ELSE route END,
                    missing_context = %s, pending_target_ids = '{}', lease_until = NULL, updated_at = now(),
                    agent_diagnostics = %s::jsonb
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.status = 'running' AND job.generation = %s AND job.attempts = %s
                    AND job.lease_until > now() AND job.expires_at > now()
                    AND state.user_id = job.user_id AND state.character_id = job.character_id
                    AND state.generation = job.generation""",
                    (status, error[:300] if error else None, status, missing_context,
                     Jsonb(summary), job["id"], *owner, job["generation"], job["attempts"]),
                )
                return cursor.rowcount == 1

    async def expire_context(self) -> int:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    "SELECT generation FROM memory_scope_state WHERE user_id = %s AND character_id = %s FOR UPDATE", owner,
                )
                await connection.execute(
                    """UPDATE memory_jobs SET status = 'failed', error = 'lease_exhausted', lease_until = NULL,
                    pending_target_ids = '{}', updated_at = now(),
                    agent_diagnostics = agent_diagnostics || jsonb_build_object(
                        'attempt', attempts, 'result', 'failed', 'error', 'lease_exhausted', 'retry_exhausted', true)
                    WHERE user_id = %s AND character_id = %s AND status = 'running' AND lease_until < now()
                    AND attempts >= 3""", owner,
                )
                cursor = await connection.execute(
                    """UPDATE memory_jobs SET status = 'discarded',
                    recent_dialogue = NULL, missing_context = NULL, source_ids = '{}', pending_target_ids = '{}', lease_until = NULL, updated_at = now()
                    WHERE user_id = %s AND character_id = %s AND status IN ('buffered', 'pending', 'retry', 'running', 'failed', 'ignored')
                    AND expires_at <= now()""", owner,
                )
                count = cursor.rowcount
                await connection.execute(
                    """DELETE FROM memory_sources s WHERE s.user_id = %s AND s.character_id = %s
                    AND NOT EXISTS (SELECT 1 FROM memory_evidence e WHERE
                    (e.source_id,e.user_id,e.character_id) = (s.id,s.user_id,s.character_id))
                    AND NOT EXISTS (SELECT 1 FROM memory_jobs j WHERE j.user_id = s.user_id
                    AND j.character_id = s.character_id AND s.id = ANY(j.source_ids)
                    AND j.expires_at > now() AND j.status IN ('pending','running','retry','buffered','failed'))""", owner,
                )
                return count

    async def queue_health(self) -> dict:
        """只量測目前 owner／generation 的有效工作，不讀來源或模型對話。"""
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """SELECT count(*) FILTER (WHERE status IN ('pending', 'retry', 'running')),
                    COALESCE(EXTRACT(EPOCH FROM now() - min(job.created_at)
                        FILTER (WHERE status IN ('pending', 'retry', 'running'))), 0)::float,
                    count(*) FILTER (WHERE status = 'buffered'),
                    count(*) FILTER (WHERE status = 'running' AND lease_until < now()),
                    count(*) FILTER (WHERE status = 'failed' AND attempts >= 3)
                    FROM memory_jobs AS job JOIN memory_scope_state AS state
                    ON (job.user_id, job.character_id, job.generation) =
                        (state.user_id, state.character_id, state.generation)
                    WHERE job.user_id = %s AND job.character_id = %s AND job.expires_at > now()""",
                    (self.scope.user_id, self.scope.character_id),
                )
                row = await cursor.fetchone()
                return dict(zip(("active_jobs", "oldest_age_sec", "buffered_jobs", "expired_leases", "retry_exhausted_jobs"), row))

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
                    """UPDATE memory_jobs SET status = 'cancelled',
                    recent_dialogue = NULL, embedding_diagnostics = '{}'::jsonb, missing_context = NULL,
                    source_ids = '{}', pending_target_ids = '{}', agent_diagnostics = '{}',
                    lease_until = NULL, updated_at = now()
                    WHERE user_id = %s AND character_id = %s""", owner,
                )
                await connection.execute("DELETE FROM memory_items WHERE user_id = %s AND character_id = %s", owner)
                await connection.execute("DELETE FROM memory_sources WHERE user_id = %s AND character_id = %s", owner)
                await connection.execute("DELETE FROM memory_audit WHERE user_id = %s AND character_id = %s", owner)
                await connection.execute("DELETE FROM memory_forget_barriers WHERE user_id = %s AND character_id = %s", owner)

    async def related_items(self, text: str, embedding: list[float] | None, limit: int = 20,
                            mode: str = "current", subject_keys: tuple[str, ...] = ()) -> list[dict]:
        """同 owner 的有界相關記憶；目前事實與歷史分開篩選。"""
        if mode not in {"current", "history", "future", "management"}:
            raise ValueError("Memory query mode 無效")
        owner = (self.scope.user_id, self.scope.character_id)
        tokens = re.findall(r"[a-z0-9_.-]{2,}|[\u3400-\u9fff]{2,}", text.lower())
        words = []
        for token in tokens:
            parts = [token] if token.isascii() else [token[index:index + 2] for index in range(len(token) - 1)]
            for word in parts:
                if word not in _GENERIC_QUERY_WORDS and word not in words:
                    words.append(word)
        aliases = {word: alias for word, alias in _QUERY_CATEGORY_ALIASES.items() if word in text}
        words = list(dict.fromkeys([*aliases.values(), *words]))[:24]
        term_sql = sql.SQL(" OR ").join(
            sql.SQL("canonical_text ILIKE %s OR COALESCE(subject_key, '') ILIKE %s") for _ in words
        )
        exact_sql = sql.SQL("({} OR keywords && %s::text[])").format(term_sql) if words else sql.SQL("FALSE")
        if embedding is not None:
            ranking = sql.SQL("embedding <=> %s")
            ranking_args = (Vector(embedding),)
            vector_sql = sql.SQL("embedding IS NOT NULL")
            vector_args = ()
            if self.embedding_contract is not None:
                ranking = sql.SQL("CASE WHEN embedding_contract = %s THEN embedding <=> %s ELSE NULL::float END")
                ranking_args = (self.embedding_contract, Vector(embedding))
                vector_sql = sql.SQL("embedding IS NOT NULL AND embedding_contract = %s")
                vector_args = (self.embedding_contract,)
        else:
            ranking = sql.SQL("NULL::float")
            ranking_args = ()
            vector_sql = sql.SQL("FALSE")
            vector_args = ()
        terms = tuple(value for word in words for value in (f"%{word}%", f"%{word}%"))
        exact_args = (*terms, words) if words else ()
        if mode == "management" and subject_keys:
            exact_sql = sql.SQL("({} OR subject_key = ANY(%s))").format(exact_sql)
            exact_args = (*exact_args, list(subject_keys[:12]))
        status_sql = {
            "current": sql.SQL("(status IN ('active', 'conflict') OR (status = 'superseded' AND valid_to > now())) AND (expires_at IS NULL OR expires_at > now()) "
                               "AND (valid_from IS NULL OR valid_from <= now()) AND (valid_to IS NULL OR valid_to > now())"),
            "future": sql.SQL("status IN ('active', 'conflict') AND valid_from > now() AND (expires_at IS NULL OR expires_at > now())"),
            "history": sql.SQL("status IN ('active', 'conflict', 'superseded', 'expired', 'archived')"),
            "management": sql.SQL("status IN ('active', 'conflict', 'superseded', 'expired', 'archived')"),
        }[mode]
        query_template = sql.SQL(
            """SELECT id, group_id, memory_type, canonical_text, subject_key, keywords,
            status, importance, confidence, retention_class, valid_from, valid_to, expires_at, observed_at, updated_at,
            exact_match, 1 - distance AS similarity, embedding IS NOT NULL AS embedding_present, embedding_contract,
            EXISTS (SELECT 1 FROM memory_items c WHERE c.user_id = ranked.user_id
                AND c.character_id = ranked.character_id AND c.group_id = ranked.group_id
                AND c.status = 'conflict') AS has_conflict,
            EXISTS (SELECT 1 FROM memory_jobs j WHERE j.user_id = ranked.user_id
                AND j.character_id = ranked.character_id AND ranked.id = ANY(j.pending_target_ids)
                AND j.status IN ('pending', 'running', 'retry')) AS pending_change
            FROM (
                SELECT *, {} AS exact_match, {} AS distance FROM memory_items
                WHERE user_id = %s AND character_id = %s AND {}
            ) AS ranked WHERE {}
            ORDER BY exact_match DESC, distance ASC NULLS LAST, importance DESC LIMIT %s"""
        )
        query = query_template.format(exact_sql, ranking, status_sql,
            sql.SQL("exact_match OR ({} AND distance <= %s)").format(vector_sql))
        args = (*exact_args, *ranking_args, *owner, *vector_args,
                1 - MIN_RETRIEVAL_SIMILARITY, min(max(limit, 1), 20))
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(query, args) as cursor:
                    rows = await cursor.fetchall()
                    selected = [dict(zip([col.name for col in cursor.description], row)) for row in rows]
                if mode == "management" and embedding is not None:
                    vector_template = sql.SQL(query_template.as_string().replace(
                        "ORDER BY exact_match DESC, distance ASC", "ORDER BY distance ASC"))
                    vector_query = vector_template.format(exact_sql, ranking, status_sql,
                        sql.SQL("{} AND distance <= %s").format(vector_sql))
                    async with await connection.execute(vector_query, args) as cursor:
                        vectors = [dict(zip([col.name for col in cursor.description], row)) for row in await cursor.fetchall()]
                    balanced = {}
                    for index in range(max(len(selected), len(vectors))):
                        for pool in (vectors, selected):
                            if index < len(pool):
                                balanced.setdefault(pool[index]["id"], pool[index])
                    selected = list(balanced.values())[:min(max(limit, 1), 20)]
                if test_log_dir() is not None and (trace_event.get() is not None or trace_turn.get() is not None):
                    excluded_query = query_template.format(exact_sql, ranking, status_sql,
                        sql.SQL("NOT (exact_match OR COALESCE(({} AND distance <= %s), FALSE))").format(vector_sql))
                    async with await connection.execute(excluded_query, args) as cursor:
                        excluded = [dict(zip([col.name for col in cursor.description], row))
                                    for row in await cursor.fetchall()]
                    for row in excluded:
                        row["rejection_reason"] = (
                            "query_embedding_unavailable" if embedding is None else
                            "memory_embedding_missing" if not row["embedding_present"] else
                            "embedding_contract_mismatch" if self.embedding_contract is not None
                                and row["embedding_contract"] != self.embedding_contract else
                            "below_similarity_threshold"
                        )
                    trace("retrieval_filter", {"query": text, "mode": mode, "query_terms": words,
                        "category_aliases": aliases,
                        "min_similarity": MIN_RETRIEVAL_SIMILARITY, "excluded_limit": min(max(limit, 1), 20),
                        "excluded_candidates": excluded})
                return selected

    async def context_jobs(self, job: dict) -> list[dict]:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT id, source_ids, missing_context FROM memory_jobs
                    WHERE user_id = %s AND character_id = %s AND conversation_id = %s
                    AND generation = %s AND status = 'buffered' AND id <> %s AND expires_at > now()
                    ORDER BY created_at DESC LIMIT 3""",
                    (*owner, job["conversation_id"], job["generation"], job["id"]),
                ) as cursor:
                    return [dict(zip([col.name for col in cursor.description], row)) for row in await cursor.fetchall()]

    async def job_sources(self, job: dict, held: list[dict]) -> list[dict]:
        ids = set(job["source_ids"]) | {value for row in held for value in row["source_ids"]}
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT id, speaker, raw_text, occurred_at,
                    (length(raw_text) >= 4001 OR (length(raw_text) >= 500 AND id <> message_id)) AS truncated
                    FROM memory_sources
                    WHERE user_id = %s AND character_id = %s AND id = ANY(%s) AND raw_text IS NOT NULL
                    AND speaker = 'user' ORDER BY occurred_at LIMIT 32""", (*owner, list(ids)),
                ) as cursor:
                    return [dict(zip([col.name for col in cursor.description], row)) for row in await cursor.fetchall()]

    async def read_memories(self, ids: set[UUID], *, versions: bool = False) -> list[dict]:
        if not ids:
            return []
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT id, group_id, canonical_text, subject_key, status, valid_from, valid_to,
                    observed_at, updated_at FROM memory_items WHERE user_id = %s AND character_id = %s
                    AND status IN ('active','conflict','superseded','archived','expired')
                    AND (id = ANY(%s) OR (%s AND group_id IN (SELECT group_id FROM memory_items
                    WHERE user_id = %s AND character_id = %s AND id = ANY(%s))))
                    ORDER BY (id = ANY(%s)) DESC, observed_at DESC LIMIT 12""",
                    (*owner, list(ids), versions, *owner, list(ids), list(ids)),
                ) as cursor:
                    return [dict(zip([col.name for col in cursor.description], row)) for row in await cursor.fetchall()]

    async def memory_evidence(self, related):
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT memory_id, source_id, left(raw_text, 500) AS raw_text, occurred_at FROM (
                    SELECT e.memory_id, s.id AS source_id, s.raw_text, s.occurred_at,
                    row_number() OVER (PARTITION BY e.memory_id ORDER BY s.occurred_at DESC) AS rank
                    FROM memory_evidence e JOIN memory_sources s
                    ON (s.id,s.user_id,s.character_id) = (e.source_id,e.user_id,e.character_id)
                    WHERE e.user_id = %s AND e.character_id = %s AND e.memory_id = ANY(%s)
                    ) evidence WHERE rank <= 2 ORDER BY memory_id, occurred_at DESC""",
                    (*owner, [row["id"] for row in related]),
                ) as cursor:
                    return [dict(zip([col.name for col in cursor.description], row)) for row in await cursor.fetchall()]

    async def agent_candidates(self, text, embedding, limit=6, exclude=()):
        rows = await self.related_items(text, embedding, limit=20, mode="management")
        selected, groups = [], set()
        excluded = {UUID(str(value)) for value in exclude}
        # 強詞面優先；在剩餘候選中讓向量與詞面各有入選機會。
        entities = re.findall(r"[a-z0-9_.-]{3,}", text.lower())
        def score(row):
            strong = any(word in row["canonical_text"].lower() for word in entities)
            active = row["status"] in {"active", "conflict"}
            return (strong, active, row.get("similarity") or -1, row.get("exact_match", False))
        for row in sorted(rows, key=score, reverse=True):
            group = row["group_id"]
            if row["id"] in excluded or group in groups:
                continue
            selected.append(row)
            groups.add(group)
            if len(selected) >= limit:
                break
        trace("memory_candidates", {"candidates": [{"id": str(row["id"]),
            "similarity": row.get("similarity"), "exact_match": row["exact_match"]} for row in selected]})
        return selected

    async def mark_pending_targets(self, job, decisions, allowed):
        targets = {UUID(value) for item in decisions if item["action"] in {"SUPERSEDE", "FORGET"}
                   for value in item["target_memory_ids"]}
        if not targets <= allowed:
            raise ValueError("待更新標記不可擴大目標集合")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    """UPDATE memory_jobs SET pending_target_ids = %s
                    WHERE id = %s AND user_id = %s AND character_id = %s AND status = 'running'
                    AND attempts = %s AND generation = %s""",
                    (list(targets), job["id"], self.scope.user_id, self.scope.character_id, job["attempts"], job["generation"]),
                )
