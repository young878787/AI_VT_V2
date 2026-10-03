"""長期記憶的 owner-scoped PostgreSQL 存取。"""

from uuid import UUID, uuid5
import re

from pgvector import Vector
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from domain.memory_routing import MemoryRouting, instruction_policy
from domain.memory_scope import MemoryScope, conversation_id, message_id
from core.prompt_logger import test_log_dir, trace, trace_event


MIN_RETRIEVAL_SIMILARITY = 0.75
_GENERIC_QUERY_WORDS = {"喜歡", "記得", "以前", "之前", "曾經", "過去", "使用者", "什麼", "知道", "我的", "你的", "使用", "用者", "現在", "平常", "最近", "幫我", "記住", "記憶", "這件", "件事", "更正", "只有", "相關", "的人"}
_QUERY_CATEGORY_ALIASES = {"健康": "health", "甜點": "dessert", "甜食": "sweet",
                           "咖啡": "coffee", "飲料": "drink", "遊戲": "game"}


class MemoryRepository:
    def __init__(
        self, pool: AsyncConnectionPool, scope: MemoryScope,
        embedding_model: str | None = None, embedding_contract: str | None = None,
    ) -> None:
        self.pool = pool
        self.scope = scope
        self.embedding_model = embedding_model
        self.embedding_contract = embedding_contract

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
        recent_dialogue: list[dict], embedding: list[float] | None = None, *, finalized: bool = True,
    ) -> bool:
        owner = (self.scope.user_id, self.scope.character_id)
        status = {"none": "ignored", "needs_context": "buffered", "candidate": "pending", None: "pending"}[routing.route]
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
                    recent_dialogue = %s, embedding = %s,
                    embedding_model = %s, embedding_contract = %s,
                    status = %s, instruction = %s, error = %s, route_finalized = %s, updated_at = now(),
                    agent_diagnostics = agent_diagnostics || %s::jsonb
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.generation = state.generation
                    AND state.user_id = job.user_id AND state.character_id = job.character_id
                    AND job.status IN ('ignored', 'pending') AND NOT job.route_finalized""",
                    (routing.route, routing.confidence,
                     Jsonb([item for item in dialogue if item["role"] == "assistant"]) if routing.route != "none" else None,
                     Vector(embedding) if embedding is not None and routing.route != "none" else None,
                     self.embedding_model if embedding is not None and routing.route != "none" else None,
                     self.embedding_contract if embedding is not None and routing.route != "none" else None,
                     status, instruction_policy(text), routing.error, finalized,
                     Jsonb([{"role": "policy" if instruction_policy(text) == "no_store" else "jev",
                             "result": routing.route or "review", "confidence": routing.confidence,
                             "error": routing.error}]) if finalized else Jsonb([]), event_id, *owner),
                )
                changed = cursor.rowcount == 1
                if changed and routing.route != "none":
                    row = await (await connection.execute(
                        "SELECT conversation_id FROM memory_jobs WHERE id = %s AND user_id = %s AND character_id = %s",
                        (event_id, *owner),
                    )).fetchone()
                    sources = [(event_id, text[:4000])]
                    sources += [(uuid5(event_id, f"context:{index}"), item["content"])
                                for index, item in enumerate(dialogue)
                                if item["role"] == "user" and instruction_policy(item["content"]) != "no_store"]
                    persisted_sources = []
                    for source_id, raw_text in sources:
                        if source_id != event_id:
                            existing = await (await connection.execute(
                                """SELECT id FROM memory_sources WHERE user_id = %s AND character_id = %s
                                AND conversation_id = %s AND speaker = 'user' AND raw_text = %s
                                ORDER BY occurred_at DESC LIMIT 1""", (*owner, row[0], raw_text),
                            )).fetchone()
                            if existing:
                                persisted_sources.append(existing[0])
                                continue
                        persisted_sources.append(source_id)
                        await connection.execute(
                            """INSERT INTO memory_sources
                            (id, user_id, character_id, conversation_id, message_id, speaker, raw_text, occurred_at)
                            VALUES (%s, %s, %s, %s, %s, 'user', %s, now()) ON CONFLICT DO NOTHING""",
                            (source_id, *owner, row[0], event_id, raw_text),
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
        """只保存向量處理中繼資料，不保存輸入文字或向量內容。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    """UPDATE memory_jobs AS job SET embedding_diagnostics =
                    job.embedding_diagnostics || %s::jsonb
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.generation = state.generation
                    AND state.user_id = job.user_id AND state.character_id = job.character_id""",
                    (Jsonb([diagnostic]), event_id, *owner),
                )

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
                        AND (job.route_finalized OR job.created_at < now() - interval '60 seconds')
                        AND job.expires_at > now()
                        AND CASE job.stage WHEN 'intake' THEN job.intake_attempts < 3 ELSE job.librarian_attempts < 3 END
                        AND (job.status IN ('pending', 'retry') OR (job.status = 'running' AND job.lease_until < now()))
                        ORDER BY job.created_at - CASE WHEN job.instruction IN ('remember', 'forget') THEN interval '5 minutes' ELSE interval '0' END FOR UPDATE OF job SKIP LOCKED LIMIT 1
                    )
                    UPDATE memory_jobs AS job SET status = 'running', attempts = attempts + 1,
                    error = CASE WHEN NOT job.route_finalized THEN 'jev_timeout' ELSE job.error END, route_finalized = true,
                    agent_diagnostics = job.agent_diagnostics || CASE WHEN NOT job.route_finalized
                    THEN '[{"role":"jev","error":"jev_timeout","result":"review"}]'::jsonb ELSE '[]'::jsonb END,
                    lease_until = now() + interval '120 seconds', updated_at = now(),
                    intake_attempts = intake_attempts + CASE WHEN job.stage = 'intake' THEN 1 ELSE 0 END,
                    librarian_attempts = librarian_attempts + CASE WHEN job.stage = 'librarian' THEN 1 ELSE 0 END
                    FROM candidate WHERE job.id = candidate.id
                    AND job.user_id = candidate.user_id AND job.character_id = candidate.character_id
                    RETURNING job.*""", owner,
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
                    return job

    async def finish(self, job: dict, status: str, decisions: object = None, error: str | None = None,
                     validation_error: str | None = None) -> bool:
        if status not in {"done", "ignored", "retry", "failed", "cancelled"}:
            raise ValueError("無效的 job terminal status")
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_jobs AS job SET status = %s, decisions = %s, error = %s,
                    lease_until = NULL, updated_at = now(),
                    agent_diagnostics = agent_diagnostics || %s::jsonb
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.status = 'running' AND job.generation = %s
                    AND job.attempts = %s
                    AND state.user_id = job.user_id AND state.character_id = job.character_id
                    AND state.generation = job.generation""",
                    (status, Jsonb(decisions) if decisions is not None else None,
                     error[:300] if error else None, Jsonb([{"role": job["stage"], "attempt": job["attempts"],
                         "result": status, "error": error, "validation_error": validation_error[:1000] if validation_error else None}]),
                     job["id"], *owner, job["generation"], job["attempts"]),
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
                    """UPDATE memory_jobs SET status = 'failed', error = 'lease_exhausted', lease_until = NULL
                    WHERE user_id = %s AND character_id = %s AND status = 'running' AND lease_until < now()
                    AND CASE stage WHEN 'intake' THEN intake_attempts >= 3 ELSE librarian_attempts >= 3 END""", owner,
                )
                cursor = await connection.execute(
                    """UPDATE memory_jobs SET status = 'discarded',
                    recent_dialogue = NULL, embedding = NULL, embedding_model = NULL,
                    embedding_contract = NULL, reviewed_candidates = NULL, missing_context = NULL, source_ids = '{}', updated_at = now()
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
                    recent_dialogue = NULL, embedding = NULL, decisions = NULL,
                    embedding_model = NULL, embedding_contract = NULL,
                    embedding_diagnostics = '[]'::jsonb, reviewed_candidates = NULL, missing_context = NULL, source_ids = '{}', agent_diagnostics = '[]',
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
            status, importance, confidence, retention_class, valid_from, valid_to, expires_at,
            exact_match, 1 - distance AS similarity, embedding IS NOT NULL AS embedding_present, embedding_contract,
            EXISTS (SELECT 1 FROM memory_items c WHERE c.user_id = ranked.user_id
                AND c.character_id = ranked.character_id AND c.group_id = ranked.group_id
                AND c.status = 'conflict') AS has_conflict,
            EXISTS (SELECT 1 FROM memory_jobs j WHERE j.user_id = ranked.user_id
                AND j.character_id = ranked.character_id AND ranked.id = ANY(j.pending_target_ids)
                AND j.status IN ('pending', 'running', 'retry', 'failed', 'buffered')) AS pending_change
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
                if test_log_dir() is not None and trace_event.get() is not None:
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

    async def related_context(self, job: dict, embedding: list[float]) -> list[dict]:
        owner = (self.scope.user_id, self.scope.character_id)
        contract_filter = "AND embedding_contract = %s" if self.embedding_contract is not None else ""
        contract_arg = (self.embedding_contract,) if self.embedding_contract is not None else ()
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    f"""SELECT id, (SELECT raw_text FROM memory_sources s WHERE
                    (s.id,s.user_id,s.character_id) = (memory_jobs.id,memory_jobs.user_id,memory_jobs.character_id)) AS source_text,
                    recent_dialogue, source_ids, missing_context FROM memory_jobs WHERE user_id = %s AND character_id = %s
                    AND status = 'buffered' AND id <> %s
                    AND embedding IS NOT NULL {contract_filter}
                    AND expires_at > now()
                    AND (embedding <=> %s <= %s OR (conversation_id = %s AND %s))
                    ORDER BY embedding <=> %s LIMIT 3""",
                    (*owner, job["id"], *contract_arg, Vector(embedding), 1 - MIN_RETRIEVAL_SIMILARITY,
                     job["conversation_id"], bool(re.search(r"這件事|那個|這個|剛才|that|it", job["source_text"], re.I)), Vector(embedding)),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(zip([col.name for col in cursor.description], row)) for row in rows]

    async def unembedded_context(self) -> list[dict]:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT id, (SELECT raw_text FROM memory_sources s WHERE
                    (s.id,s.user_id,s.character_id) = (memory_jobs.id,memory_jobs.user_id,memory_jobs.character_id)) AS source_text,
                    generation FROM memory_jobs
                    WHERE user_id = %s AND character_id = %s AND status = 'buffered'
                    AND embedding IS NULL AND cardinality(source_ids) > 0
                    ORDER BY created_at LIMIT 10""", owner,
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(zip([col.name for col in cursor.description], row)) for row in rows]

    async def save_context_embedding(self, job: dict, embedding: list[float]) -> bool:
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_jobs AS job SET embedding = %s, embedding_model = %s, embedding_contract = %s, updated_at = now()
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.status = 'buffered' AND job.embedding IS NULL
                    AND job.generation = %s AND state.user_id = job.user_id
                    AND state.character_id = job.character_id AND state.generation = job.generation""",
                    (Vector(embedding), self.embedding_model, self.embedding_contract, job["id"], *owner, job["generation"]),
                )
                return cursor.rowcount == 1

    async def intake_sources(self, job: dict, held: list[dict]) -> list[dict]:
        ids = list(set(job.get("source_ids", []) + [value for row in held for value in row["source_ids"]]))
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT id, speaker, raw_text, occurred_at FROM memory_sources
                    WHERE user_id = %s AND character_id = %s AND id = ANY(%s) AND raw_text IS NOT NULL
                    ORDER BY occurred_at LIMIT 32""", (*owner, ids),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(zip([col.name for col in cursor.description], row)) for row in rows]

    async def complete_intake(self, job, result, sources, held, diagnostic, embedding=None):
        from domain.memory_intake import validate_intake
        name, payload = {
            "candidate": ("accept_candidates", {"candidates": result.get("candidates")}),
            "needs_context": ("hold_for_context", {"missing_context": result.get("missing_context")}),
            "none": ("dismiss_input", {"reason": result.get("reason")}),
        }[result["route"]]
        owner = (self.scope.user_id, self.scope.character_id)
        used = {UUID(value) for candidate in result.get("candidates", []) for value in candidate["source_ids"]}
        adopted = [row["id"] for row in held if row["id"] in used]
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                state = await (await connection.execute("SELECT generation FROM memory_scope_state WHERE user_id = %s AND character_id = %s FOR UPDATE", owner)).fetchone()
                current = await (await connection.execute(
                    "SELECT status, attempts, stage, source_ids FROM memory_jobs WHERE id = %s AND user_id = %s AND character_id = %s FOR UPDATE",
                    (job["id"], *owner),
                )).fetchone()
                if not state or state[0] != job["generation"] or not current or current[:3] != ("running", job["attempts"], "intake"):
                    return False
                authorized = set(current[3]) or {job["id"]}
                if held:
                    held_rows = await (await connection.execute(
                        """SELECT source_ids FROM memory_jobs WHERE id = ANY(%s) AND user_id = %s AND character_id = %s
                        AND status = 'buffered' AND generation = %s AND expires_at > now() FOR UPDATE""",
                        ([row["id"] for row in held], *owner, job["generation"]),
                    )).fetchall()
                    if len(held_rows) != len(held):
                        raise ValueError("待補來源已被其他工作採用或到期")
                    authorized.update(value for row in held_rows for value in row[0])
                validate_intake(name, payload, [source for source in sources if source["id"] in authorized], job["source_text"])
                cursor = await connection.execute(
                    """UPDATE memory_jobs AS job SET route = %s, stage = %s, status = %s,
                    reviewed_candidates = %s, missing_context = %s, context_job_ids = %s,
                    source_ids = %s, embedding = %s, embedding_model = %s, embedding_contract = %s,
                    agent_diagnostics = agent_diagnostics || %s::jsonb,
                    lease_until = NULL, error = NULL, updated_at = now()
                    FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND job.status = 'running' AND job.attempts = %s AND job.generation = %s
                    AND state.user_id = job.user_id AND state.character_id = job.character_id
                    AND state.generation = job.generation""",
                    (result["route"], "librarian" if used else "intake",
                     {"candidate": "pending", "needs_context": "buffered", "none": "ignored"}[result["route"]],
                     Jsonb(result["candidates"]) if result.get("candidates") else None, result.get("missing_context"), adopted,
                     list(used | {job["id"]}) if used else [source["id"] for source in sources],
                     Vector(embedding) if embedding is not None else None,
                     self.embedding_model if embedding is not None else None,
                     self.embedding_contract if embedding is not None else None,
                     Jsonb([{**diagnostic, "result": result["route"], "committed": True}]),
                     job["id"], *owner, job["attempts"], job["generation"]),
                )
                return cursor.rowcount == 1

    async def return_for_review(self, job, reason, diagnostic):
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    """UPDATE memory_jobs SET stage = 'intake', route = 'needs_context', status = 'buffered',
                    missing_context = %s, lease_until = NULL, agent_diagnostics = agent_diagnostics || %s::jsonb
                    WHERE id = %s AND user_id = %s AND character_id = %s AND status = 'running'
                    AND attempts = %s AND generation = %s""",
                    (reason, Jsonb([{**diagnostic, "result": "returned"}]), job["id"], *owner, job["attempts"], job["generation"]),
                )

    async def memory_evidence(self, related):
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(
                    """SELECT e.memory_id, s.id AS source_id, left(s.raw_text, 500) AS raw_text, s.occurred_at
                    FROM memory_evidence e JOIN memory_sources s
                    ON (s.id, s.user_id, s.character_id) = (e.source_id, e.user_id, e.character_id)
                    WHERE e.user_id = %s AND e.character_id = %s AND e.memory_id = ANY(%s)
                    ORDER BY s.occurred_at DESC LIMIT 20""", (*owner, [row["id"] for row in related]),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(zip([col.name for col in cursor.description], row)) for row in rows]

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
