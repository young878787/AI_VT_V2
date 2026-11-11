"""長期記憶的 owner-scoped PostgreSQL 存取。"""

from uuid import UUID
import re

from pgvector import Vector
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from domain.memory.cards import CARD_MOODS, aggregate_mood, member_fingerprint, normalize_key
from domain.memory.routing import MemoryRouting, instruction_policy
from domain.memory.scope import MemoryScope, conversation_id, message_id
from domain.memory.source import MemoryEventConflict, MemoryEventReplay, read_memory_source
from core.prompt_logger import test_log_dir, trace, trace_event, trace_turn


MIN_RETRIEVAL_SIMILARITY = 0.75
# Chat 召回：粗篩取候選卡（不漏），JEV 判斷是否注入（不多）；JEV 失敗時以 fallback 門檻判斷。
CHAT_RETRIEVAL_MIN_SIMILARITY = 0.50
CHAT_CARD_CANDIDATE_LIMIT = 12
CHAT_CARD_INJECT_LIMIT = 5
CHAT_CARD_MIN_SCORE = 0.25
CHAT_UNCARDED_LIMIT = 6
AGENT_CARD_LIMIT = 8
CURRENT_SQL = "status IN ('active', 'conflict') AND (expires_at IS NULL OR expires_at > now())"


def _owner_sql(alias: str) -> str:
    return f"{alias}.user_id = %(user)s AND {alias}.character_id = %(character)s"


async def sync_cards(connection, owner: tuple, card_ids, *, clear_summary: bool = False) -> dict:
    """在呼叫端交易內同步卡片：有效要點數、心情、概要新鮮度；沒有任何要點的卡片直接刪除。

    clear_summary 用於遺忘：清空概要與卡片向量，避免已遺忘內容經由卡片殘留。
    """
    ids = sorted({UUID(str(value)) for value in card_ids if value is not None}, key=str)
    if not ids:
        return {"deleted": [], "updated": []}
    await connection.execute(
        "SELECT id FROM memory_cards WHERE user_id = %s AND character_id = %s AND id = ANY(%s) ORDER BY id FOR UPDATE",
        (*owner, ids),
    )
    async with await connection.execute(
        f"""SELECT card_id, id, canonical_text, status, valid_from, valid_to, {CURRENT_SQL} AS is_current
        FROM memory_items WHERE user_id = %s AND character_id = %s AND card_id = ANY(%s)""",
        (*owner, ids),
    ) as cursor:
        members = [dict(zip([column.name for column in cursor.description], row)) for row in await cursor.fetchall()]
    async with await connection.execute(
        """SELECT DISTINCT item.card_id, source.id, source.mood, source.occurred_at
        FROM memory_items AS item JOIN memory_evidence AS evidence
        ON (evidence.memory_id, evidence.user_id, evidence.character_id) = (item.id, item.user_id, item.character_id)
        JOIN memory_sources AS source
        ON (source.id, source.user_id, source.character_id) = (evidence.source_id, evidence.user_id, evidence.character_id)
        WHERE item.user_id = %s AND item.character_id = %s AND item.card_id = ANY(%s)
        AND source.mood IS NOT NULL""",
        (*owner, ids),
    ) as cursor:
        moods = await cursor.fetchall()
    deleted, updated = [], []
    for card_id in ids:
        rows = [row for row in members if row["card_id"] == card_id]
        if not rows:
            await connection.execute(
                "DELETE FROM memory_cards WHERE id = %s AND user_id = %s AND character_id = %s", (card_id, *owner),
            )
            deleted.append(card_id)
            continue
        mood = aggregate_mood([(row[2], row[3]) for row in moods if row[0] == card_id])
        await connection.execute(
            """UPDATE memory_cards SET active_count = %s, mood = %s,
            summary = CASE WHEN %s THEN NULL ELSE summary END,
            embedding = CASE WHEN %s THEN NULL ELSE embedding END,
            embedding_contract = CASE WHEN %s THEN NULL ELSE embedding_contract END,
            summary_stale = %s OR summary_fingerprint IS DISTINCT FROM %s, updated_at = now()
            WHERE id = %s AND user_id = %s AND character_id = %s""",
            (sum(row["is_current"] for row in rows), mood if mood in CARD_MOODS else None,
             clear_summary, clear_summary, clear_summary, clear_summary, member_fingerprint(rows), card_id, *owner),
        )
        updated.append(card_id)
    return {"deleted": deleted, "updated": updated}
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
                    AND retention_class = 'temporary' AND expires_at <= now() RETURNING card_id""", owner,
                )
                cards = [row[0] for row in await cursor.fetchall()]
                await sync_cards(connection, owner, cards)
                return len(cards)

    async def reset(self, *, connection=None) -> None:
        """generation 鎖與刪除同 transaction，阻止已在 LLM 呼叫的舊 worker 回寫。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async def execute(active_connection) -> None:
            await active_connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
            await active_connection.execute(
                "INSERT INTO memory_scope_state (user_id, character_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", owner,
            )
            await active_connection.execute(
                """UPDATE memory_scope_state SET generation = generation + 1
                WHERE user_id = %s AND character_id = %s""", owner,
            )
            await active_connection.execute(
                """UPDATE memory_jobs SET status = 'cancelled',
                recent_dialogue = NULL, embedding_diagnostics = '{}'::jsonb, missing_context = NULL,
                source_ids = '{}', pending_target_ids = '{}', agent_diagnostics = '{}',
                lease_until = NULL, updated_at = now()
                WHERE user_id = %s AND character_id = %s""", owner,
            )
            await active_connection.execute("DELETE FROM memory_items WHERE user_id = %s AND character_id = %s", owner)
            await active_connection.execute("DELETE FROM memory_cards WHERE user_id = %s AND character_id = %s", owner)
            await active_connection.execute("DELETE FROM memory_sources WHERE user_id = %s AND character_id = %s", owner)
            await active_connection.execute("DELETE FROM memory_audit WHERE user_id = %s AND character_id = %s", owner)
            await active_connection.execute("DELETE FROM memory_forget_barriers WHERE user_id = %s AND character_id = %s", owner)
        if connection is not None:
            await execute(connection)
            return
        async with self.pool.connection() as own_connection:
            async with own_connection.transaction():
                await execute(own_connection)

    async def related_items(self, text: str, embedding: list[float] | None, limit: int = 20,
                            mode: str = "current", subject_keys: tuple[str, ...] = ()) -> list[dict]:
        """同 owner 的有界相關記憶；目前事實與歷史分開篩選。Chat 召回改用 locate_cards()。"""
        if mode not in {"current", "history", "future", "management"}:
            raise ValueError("Memory query mode 無效")
        threshold = MIN_RETRIEVAL_SIMILARITY
        owner = (self.scope.user_id, self.scope.character_id)
        tokens = re.findall(r"[a-z0-9_.-]{2,}|[\u3400-\u9fff]{2,}", text.lower())
        words = []
        for token in tokens:
            parts = [token] if token.isascii() else [token[index:index + 2] for index in range(len(token) - 1)]
            for word in parts:
                if word not in _GENERIC_QUERY_WORDS and word not in words:
                    words.append(word)
        # 原詞與 alias 都優先保留，避免長情境截掉真正可命中的中文主題。
        # 英文以完整 token 反查；蛋糕→甜點等中文分類關係仍維持單向。
        aliases = {word: alias for word, alias in _QUERY_CATEGORY_ALIASES.items()
                   if word in text or (alias.isascii() and alias in tokens)}
        words = list(dict.fromkeys([*aliases.keys(), *aliases.values(), *words]))[:24]
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
            card_id, (SELECT card.title FROM memory_cards card WHERE card.id = ranked.card_id
                AND card.user_id = ranked.user_id AND card.character_id = ranked.character_id) AS card_title,
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
                1 - threshold, min(max(limit, 1), 20))
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
                        "min_similarity": threshold, "excluded_limit": min(max(limit, 1), 20),
                        "excluded_candidates": excluded})
                return selected

    def _contract_sql(self, alias: str) -> tuple[sql.Composable, dict]:
        if self.embedding_contract is None:
            return sql.SQL(""), {}
        # alias 只來自本模組固定的 SQL 別名，不接受外部輸入。
        return sql.SQL(f" AND {alias}.embedding_contract = %(contract)s"), {"contract": self.embedding_contract}

    async def locate_cards(self, text: str, embedding: list[float] | None, *,
                           limit: int = CHAT_CARD_CANDIDATE_LIMIT, min_score: float = CHAT_CARD_MIN_SCORE,
                           previous_card_ids: tuple = ()) -> list[dict]:
        """Chat 粗篩：一次 SQL 往返取四路聯集（證據、要點、卡片向量、名稱命中）加連結與上一輪卡片。

        每張卡取各路最高分；所有路徑都過濾 owner、embedding contract 與保留期限。回傳要點列，
        每列帶所屬卡片欄位；未歸卡要點只經要點向量定位，card_id 為 NULL。
        """
        if not -1 <= min_score <= 1:
            raise ValueError("候選卡門檻無效")
        item_contract, params = self._contract_sql("i")
        source_contract, _ = self._contract_sql("s")
        card_contract, _ = self._contract_sql("c")
        query = sql.SQL("""WITH q AS (SELECT %(vector)s::vector AS v),
        eligible AS (
            SELECT i.id, i.card_id, i.embedding FROM memory_items i
            WHERE """ + _owner_sql("i") + """
            AND i.status IN ('active', 'conflict', 'superseded', 'expired', 'archived')
            AND (i.expires_at IS NULL OR i.expires_at > now()) AND i.embedding IS NOT NULL{item_contract}
        ),
        hits AS (
            SELECT i.card_id, i.id AS item_id, 'source'::text AS route, max(1 - (s.embedding <=> q.v)) AS score
            FROM q, memory_sources s
            JOIN memory_evidence e ON e.source_id = s.id AND e.user_id = s.user_id AND e.character_id = s.character_id
            JOIN eligible i ON i.id = e.memory_id
            WHERE q.v IS NOT NULL AND """ + _owner_sql("s") + """
            AND s.raw_text IS NOT NULL AND s.embedding IS NOT NULL{source_contract}
            GROUP BY i.card_id, i.id
            UNION ALL
            SELECT i.card_id, i.id, 'item', 1 - (i.embedding <=> q.v) FROM q, eligible i WHERE q.v IS NOT NULL
            UNION ALL
            SELECT c.id, NULL::uuid, 'card', 1 - (c.embedding <=> q.v) FROM q, memory_cards c
            WHERE q.v IS NOT NULL AND """ + _owner_sql("c") + """ AND c.embedding IS NOT NULL{card_contract}
            UNION ALL
            SELECT c.id, NULL::uuid, 'name', 1.0::float FROM memory_cards c
            WHERE """ + _owner_sql("c") + """ AND strpos(%(name_text)s, c.title_key) > 0
        ),
        scored AS (
            SELECT h.card_id, max(h.score) AS score, array_agg(DISTINCT h.route) AS routes FROM hits h
            WHERE h.card_id IS NOT NULL AND EXISTS (SELECT 1 FROM eligible i WHERE i.card_id = h.card_id)
            GROUP BY h.card_id
        ),
        primary_cards AS (
            SELECT card_id, score, routes FROM scored
            WHERE score >= %(min_score)s OR 'name' = ANY(routes)
            ORDER BY score DESC, card_id LIMIT %(limit)s
        ),
        extra AS (
            SELECT i2.card_id, 'link'::text AS route FROM primary_cards p
            JOIN eligible i1 ON i1.card_id = p.card_id
            JOIN memory_evidence e1 ON e1.memory_id = i1.id AND """ + _owner_sql("e1") + """
            JOIN memory_evidence e2 ON e2.source_id = e1.source_id AND e2.user_id = e1.user_id
                AND e2.character_id = e1.character_id
            JOIN eligible i2 ON i2.id = e2.memory_id
            WHERE i2.card_id IS NOT NULL AND i2.card_id <> p.card_id
            UNION
            SELECT i.card_id, 'session' FROM eligible i WHERE i.card_id = ANY(%(previous)s::uuid[])
        ),
        chosen AS (
            SELECT card_id, score, routes, 0 AS tier FROM primary_cards
            UNION ALL
            SELECT x.card_id, s.score, array_agg(DISTINCT x.route) || COALESCE(s.routes, '{{}}'::text[]), 1
            FROM extra x LEFT JOIN scored s ON s.card_id = x.card_id
            WHERE x.card_id NOT IN (SELECT card_id FROM primary_cards)
            GROUP BY x.card_id, s.score, s.routes
        ),
        final_cards AS (
            SELECT * FROM (SELECT *, row_number() OVER (ORDER BY tier, score DESC NULLS LAST, card_id) AS card_rank
                           FROM chosen) ranked WHERE card_rank <= %(limit)s
        ),
        item_scores AS (
            SELECT item_id, max(score) AS similarity FROM hits WHERE item_id IS NOT NULL GROUP BY item_id
        ),
        uncarded AS (
            SELECT i.id, s.similarity FROM eligible i JOIN item_scores s ON s.item_id = i.id
            WHERE i.card_id IS NULL AND s.similarity >= %(min_score)s
            ORDER BY s.similarity DESC, i.id LIMIT %(uncarded_limit)s
        ),
        members AS (
            SELECT m.id, m.card_id, fc.score AS card_score, fc.routes AS card_routes, fc.card_rank,
                row_number() OVER (PARTITION BY m.card_id, m.status IN ('active', 'conflict')
                                   ORDER BY m.observed_at DESC, m.id) AS version_rank,
                m.status IN ('active', 'conflict') AS is_current
            FROM final_cards fc JOIN memory_items m ON m.card_id = fc.card_id AND """ + _owner_sql("m") + """
            WHERE m.id IN (SELECT id FROM eligible)
            UNION ALL
            SELECT u.id, NULL::uuid, u.similarity, ARRAY['item']::text[], NULL::bigint, 1::bigint,
                m.status IN ('active', 'conflict')
            FROM uncarded u JOIN memory_items m ON m.id = u.id AND """ + _owner_sql("m") + """
        )
        SELECT m.id, m.group_id, m.memory_type, m.canonical_text, m.subject_key, m.status, m.importance,
            m.valid_from, m.valid_to, m.expires_at, m.observed_at, m.updated_at, m.card_id,
            score.similarity, card.domain AS card_domain, card.title AS card_title, card.summary AS card_summary,
            card.summary_stale AS card_summary_stale, card.mood AS card_mood, card.active_count AS card_active_count,
            member.card_score, member.card_routes, member.card_rank,
            EXISTS (SELECT 1 FROM memory_items c WHERE c.user_id = m.user_id AND c.character_id = m.character_id
                AND c.group_id = m.group_id AND c.status = 'conflict') AS has_conflict,
            EXISTS (SELECT 1 FROM memory_jobs j WHERE j.user_id = m.user_id AND j.character_id = m.character_id
                AND m.id = ANY(j.pending_target_ids) AND j.status IN ('pending', 'running', 'retry')) AS pending_change
        FROM members member
        JOIN memory_items m ON m.id = member.id AND """ + _owner_sql("m") + """
        LEFT JOIN item_scores score ON score.item_id = m.id
        LEFT JOIN memory_cards card ON card.id = m.card_id AND card.user_id = m.user_id AND card.character_id = m.character_id
        WHERE member.is_current OR member.version_rank <= 3
        ORDER BY member.card_rank NULLS LAST, member.is_current DESC, score.similarity DESC NULLS LAST,
            m.observed_at DESC, m.id""").format(
            item_contract=item_contract, source_contract=source_contract, card_contract=card_contract)
        params.update({
            "vector": Vector(embedding) if embedding is not None else None,
            "user": self.scope.user_id, "character": self.scope.character_id,
            "name_text": normalize_key(text), "min_score": min_score,
            "limit": min(max(limit, 1), 20), "uncarded_limit": CHAT_UNCARDED_LIMIT,
            "previous": [UUID(str(value)) for value in previous_card_ids],
        })
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(query, params) as cursor:
                    return [dict(zip([col.name for col in cursor.description], row)) for row in await cursor.fetchall()]

    async def agent_cards(self, embedding: list[float] | None, limit: int = AGENT_CARD_LIMIT) -> list[dict]:
        """Memory agent 的候選卡：與候選要點同一個向量，取前幾張有有效要點的卡。"""
        if embedding is None:
            return []
        item_contract, params = self._contract_sql("i")
        card_contract, _ = self._contract_sql("c")
        query = sql.SQL("""WITH q AS (SELECT %(vector)s::vector AS v),
        current_items AS (
            SELECT i.id, i.card_id, i.canonical_text, i.embedding, i.observed_at, i.embedding_contract
            FROM memory_items i WHERE """ + _owner_sql("i") + """ AND i.card_id IS NOT NULL
            AND i.status IN ('active', 'conflict') AND (i.expires_at IS NULL OR i.expires_at > now())
        ),
        scores AS (
            SELECT i.card_id, 1 - (i.embedding <=> q.v) AS score FROM q, current_items i
            WHERE i.embedding IS NOT NULL{item_contract}
            UNION ALL
            SELECT c.id, 1 - (c.embedding <=> q.v) FROM q, memory_cards c
            WHERE """ + _owner_sql("c") + """ AND c.embedding IS NOT NULL{card_contract}
        ),
        top AS (
            SELECT card_id, max(score) AS score FROM scores
            WHERE card_id IN (SELECT card_id FROM current_items) GROUP BY card_id
            ORDER BY max(score) DESC, card_id LIMIT %(limit)s
        )
        SELECT c.id, c.domain, c.title, c.title_key, c.active_count, top.score,
            (SELECT array_agg(i.canonical_text ORDER BY i.observed_at DESC, i.id) FROM current_items i
             WHERE i.card_id = c.id) AS texts
        FROM top JOIN memory_cards c ON c.id = top.card_id AND """ + _owner_sql("c") + """
        ORDER BY top.score DESC, c.id""").format(item_contract=item_contract, card_contract=card_contract)
        params.update({"vector": Vector(embedding), "user": self.scope.user_id,
                       "character": self.scope.character_id, "limit": min(max(limit, 1), 12)})
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                async with await connection.execute(query, params) as cursor:
                    rows = [dict(zip([col.name for col in cursor.description], row)) for row in await cursor.fetchall()]
        for row in rows:
            texts = row.pop("texts") or []
            row["points"] = texts[:2]
            row["member_keys"] = {normalize_key(value) for value in texts}
        trace("memory_card_candidates", {"cards": [{"id": str(row["id"]), "domain": row["domain"],
            "title": row["title"], "score": row["score"]} for row in rows]})
        return rows

    async def record_turn_mood(self, event_id: UUID, mood: str) -> bool:
        """聊天端算出本輪 mood_v1 主要情緒後記到工作列；commit 時再複製到證據。"""
        if mood not in CARD_MOODS:
            raise ValueError("當輪心情無效")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(
                    """UPDATE memory_jobs AS job SET turn_mood = %s FROM memory_scope_state AS state
                    WHERE job.id = %s AND job.user_id = %s AND job.character_id = %s
                    AND state.user_id = job.user_id AND state.character_id = job.character_id
                    AND state.generation = job.generation""",
                    (mood, event_id, self.scope.user_id, self.scope.character_id),
                )
                return cursor.rowcount == 1

    async def card_refresh_state(self, card_id: UUID) -> dict | None:
        """概要重建的輸入：卡片欄位、有效要點與目前成員指紋、owner generation。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                return await self._card_state(connection, owner, card_id)

    async def _card_state(self, connection, owner, card_id, *, lock: bool = False) -> dict | None:
        generation = await (await connection.execute(
            "SELECT generation FROM memory_scope_state WHERE user_id = %s AND character_id = %s"
            + (" FOR SHARE" if lock else ""), owner,
        )).fetchone()
        async with await connection.execute(
            """SELECT id, domain, title, summary, summary_fingerprint, summary_stale,
            embedding IS NOT NULL AND embedding_contract IS NOT DISTINCT FROM %s AS embedding_current
            FROM memory_cards WHERE id = %s AND user_id = %s AND character_id = %s""" + (" FOR UPDATE" if lock else ""),
            (self.embedding_contract, card_id, *owner),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None or generation is None:
                return None
            card = dict(zip([col.name for col in cursor.description], row))
        async with await connection.execute(
            f"""SELECT id, canonical_text, status, valid_from, valid_to, {CURRENT_SQL} AS is_current
            FROM memory_items WHERE user_id = %s AND character_id = %s AND card_id = %s
            ORDER BY observed_at, id""",
            (*owner, card_id),
        ) as cursor:
            members = [dict(zip([col.name for col in cursor.description], row)) for row in await cursor.fetchall()]
        card.update(generation=generation[0], fingerprint=member_fingerprint(members),
                    points=[row["canonical_text"] for row in members if row["is_current"]])
        return card

    async def save_card_refresh(self, card_id: UUID, generation: int, fingerprint: str,
                                summary: str | None, embedding: list[float] | None) -> bool:
        """成員指紋與 generation 未變時才寫入；reset 或期間有新變更就放棄，交由下一次重建。"""
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                state = await self._card_state(connection, owner, card_id, lock=True)
                if state is None or state["generation"] != generation or state["fingerprint"] != fingerprint:
                    return False
                await connection.execute(
                    """UPDATE memory_cards SET
                    summary = CASE WHEN %s THEN %s ELSE summary END,
                    summary_fingerprint = CASE WHEN %s THEN %s ELSE summary_fingerprint END,
                    summary_stale = CASE WHEN %s THEN false ELSE summary_stale END,
                    embedding = COALESCE(%s, embedding),
                    embedding_contract = CASE WHEN %s THEN %s ELSE embedding_contract END,
                    updated_at = now()
                    WHERE id = %s AND user_id = %s AND character_id = %s""",
                    (summary is not None, summary, summary is not None, fingerprint, summary is not None,
                     Vector(embedding) if embedding is not None else None,
                     embedding is not None, self.embedding_contract, card_id, *owner),
                )
                return True

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
