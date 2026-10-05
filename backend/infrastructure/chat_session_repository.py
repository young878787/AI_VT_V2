"""Owner-scoped PostgreSQL storage for durable Chat session state."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from core.config import CHAT_CONTEXT_RECENT_MESSAGES
from core.utils import get_msg_field, normalize_session_id
from domain.emotion_state import validate_emotion_state
from domain.memory_scope import MemoryScope
from domain.memory_source import MEMORY_SOURCE_FIELD, read_memory_source


class ChatSessionStaleError(RuntimeError):
    """The caller is attempting to write a deleted or reset session generation."""


class ChatSessionTurnReplay(RuntimeError):
    """A durable turn already exists with the same content."""


class ChatSessionTurnConflict(RuntimeError):
    """A durable turn id is already bound to different content."""


class ChatSessionRepository:
    def __init__(self, pool: AsyncConnectionPool, scope: MemoryScope) -> None:
        self.pool = pool
        self.scope = scope

    async def _set_search_path(self, connection) -> None:
        await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(
            sql.Identifier(self.scope.schema_name)
        ))

    @property
    def owner(self) -> tuple:
        return self.scope.user_id, self.scope.character_id

    @staticmethod
    def _session_select() -> str:
        return """SELECT id, generation, revision, summary, emotion_state,
            summary_through_sequence, next_message_sequence, created_at, updated_at
            FROM chat_sessions"""

    async def get_or_create_active(self) -> dict[str, Any]:
        """Serialize owner selection so concurrent connects cannot create two active sessions."""
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"chat:{self.scope.user_id}:{self.scope.character_id}",),
                )
                row = await (await connection.execute(
                    self._session_select() + """ WHERE user_id = %s AND character_id = %s
                    AND status = 'active' FOR UPDATE""", self.owner,
                )).fetchone()
                if row is None:
                    session_id = uuid4().hex
                    row = await (await connection.execute(
                        """INSERT INTO chat_sessions (id, user_id, character_id)
                        VALUES (%s, %s, %s)
                        RETURNING id, generation, revision, summary, emotion_state,
                            summary_through_sequence, next_message_sequence, created_at, updated_at""",
                        (session_id, *self.owner),
                    )).fetchone()
                row = await self._recover_pending(connection, row)
                return await self._snapshot(connection, row, CHAT_CONTEXT_RECENT_MESSAGES)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        """Load the complete current generation for management/read-only views."""
        normalized = normalize_session_id(session_id)
        if normalized != session_id:
            raise ValueError("無效的 session_id")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                row = await (await connection.execute(
                    self._session_select() + """ WHERE id = %s AND user_id = %s
                    AND character_id = %s""", (session_id, *self.owner),
                )).fetchone()
                return await self._snapshot(connection, row, None) if row else None

    async def load_context(self, session_id: str, *, limit: int = CHAT_CONTEXT_RECENT_MESSAGES) -> dict[str, Any] | None:
        """Load summary plus only the newest uncovered short-term messages."""
        normalized = normalize_session_id(session_id)
        if normalized != session_id:
            raise ValueError("無效的 session_id")
        limit = max(1, int(limit))
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                row = await (await connection.execute(
                    self._session_select() + """ WHERE id = %s AND user_id = %s
                    AND character_id = %s AND status = 'active'""", (session_id, *self.owner),
                )).fetchone()
                return await self._snapshot(connection, row, limit) if row else None

    async def activate_for_test(self, session_id: str) -> dict[str, Any]:
        """Select a deterministic isolated session; callers enforce test-instance policy."""
        normalized = normalize_session_id(session_id)
        if normalized != session_id:
            raise ValueError("無效的 test session alias")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"chat:{self.scope.user_id}:{self.scope.character_id}",),
                )
                await connection.execute(
                    """UPDATE chat_sessions SET status = 'closed', updated_at = now()
                    WHERE user_id = %s AND character_id = %s AND status = 'active' AND id <> %s""",
                    (*self.owner, session_id),
                )
                row = await (await connection.execute(
                    """INSERT INTO chat_sessions (id,user_id,character_id,status)
                    VALUES (%s,%s,%s,'active')
                    ON CONFLICT (id,user_id,character_id) DO UPDATE
                    SET status = 'active', updated_at = now()
                    RETURNING id,generation,revision,summary,emotion_state,
                        summary_through_sequence,next_message_sequence,created_at,updated_at""",
                    (session_id, *self.owner),
                )).fetchone()
                row = await self._recover_pending(connection, row)
                return await self._snapshot(connection, row, CHAT_CONTEXT_RECENT_MESSAGES)

    async def _recover_pending(self, connection, row) -> tuple:
        """Close pending user rows left by a disconnected or crashed writer."""
        updated = await connection.execute(
            """UPDATE chat_messages SET turn_state = 'interrupted'
            WHERE session_id = %s AND user_id = %s AND character_id = %s
            AND generation = %s AND role = 'user' AND turn_state = 'pending'""",
            (row[0], *self.owner, row[1]),
        )
        if updated.rowcount == 0:
            return row
        return (await (await connection.execute(
            """UPDATE chat_sessions SET revision = revision + 1, updated_at = now()
            WHERE id = %s AND user_id = %s AND character_id = %s AND generation = %s
            RETURNING id, generation, revision, summary, emotion_state,
                summary_through_sequence, next_message_sequence, created_at, updated_at""",
            (row[0], *self.owner, row[1]),
        )).fetchone())

    async def _snapshot(self, connection, row, message_limit: int | None) -> dict[str, Any]:
        (
            session_id, generation, revision, summary, emotion, summary_cursor,
            next_sequence, created_at, updated_at,
        ) = row
        conditions = """session_id = %s AND user_id = %s AND character_id = %s
            AND generation = %s"""
        params: list[Any] = [session_id, *self.owner, generation]
        if message_limit is not None:
            conditions += """ AND sequence > %s
                AND (role = 'assistant' OR turn_state <> 'pending')"""
            params.append(summary_cursor)
            statement = f"""SELECT sequence, turn_id, role, content, status, turn_state,
                memory_source, created_at FROM chat_messages WHERE {conditions}
                ORDER BY sequence DESC LIMIT %s"""
            # Fetch one complete turn beyond the visible budget so the Python
            # projection can drop an entire oldest turn instead of splitting it.
            params.append(message_limit + 2)
        else:
            statement = f"""SELECT sequence, turn_id, role, content, status, turn_state,
                memory_source, created_at FROM chat_messages WHERE {conditions}
                ORDER BY sequence"""
        cursor = await connection.execute(statement, params)
        rows = await cursor.fetchall()
        if message_limit is not None:
            rows.reverse()
            groups: list[list[Any]] = []
            current_key = object()
            for row_item in rows:
                key = row_item[1] if row_item[1] is not None else ("sequence", row_item[0])
                if key != current_key:
                    groups.append([])
                    current_key = key
                groups[-1].append(row_item)
            selected: list[Any] = []
            selected_count = 0
            for group in reversed(groups):
                if selected_count + len(group) > message_limit:
                    break
                selected[0:0] = group
                selected_count += len(group)
            rows = selected
        messages = [self._message_item(row) for row in rows]

        count_params = [session_id, *self.owner, generation, summary_cursor]
        backlog_cursor = await connection.execute(
            """SELECT count(*) FROM chat_messages WHERE session_id = %s AND user_id = %s
            AND character_id = %s AND generation = %s AND sequence > %s
            AND (role = 'assistant' OR turn_state <> 'pending')""", count_params,
        )
        backlog = (await backlog_cursor.fetchone())[0]
        return {
            "session_id": session_id,
            "generation": generation,
            "revision": revision,
            "summary": summary,
            "summary_through_sequence": summary_cursor,
            "next_message_sequence": next_sequence,
            "uncompressed_message_count": backlog,
            "emotion_state": validate_emotion_state(emotion),
            "messages": messages,
            "created_at": created_at.isoformat(),
            "updated_at": updated_at.isoformat(),
        }

    @staticmethod
    def _message_item(row) -> dict[str, Any]:
        sequence, turn_id, role, content, status, turn_state, source, created_at = row
        item: dict[str, Any] = {"role": role, "content": content, "sequence": sequence}
        if turn_id is not None:
            item["turn_id"] = turn_id
        if role == "assistant" and status == "interrupted":
            item["status"] = status
        if role == "user" and turn_state is not None:
            item["turn_state"] = turn_state
        if role == "user" and source is not None:
            candidate = {**item, MEMORY_SOURCE_FIELD: source}
            if read_memory_source(candidate) is not None:
                item[MEMORY_SOURCE_FIELD] = source
        item["created_at"] = created_at.isoformat()
        return item

    async def append_user(self, session_id: str, generation: int, message: dict) -> dict[str, Any]:
        """Durably append one pending user turn before any model work."""
        if message.get("role") != "user" or not isinstance(message.get("content"), str) or not message["content"]:
            raise ValueError("無效的 user message")
        turn_id = message.get("turn_id")
        if not isinstance(turn_id, str) or not 0 < len(turn_id) <= 128:
            raise ValueError("user message 缺少有效 turn_id")
        source_value = Jsonb(dict(message[MEMORY_SOURCE_FIELD])) if read_memory_source(message) is not None else None
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                row = await (await connection.execute(
                    """SELECT generation, next_message_sequence FROM chat_sessions
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    AND status = 'active' FOR UPDATE""", (session_id, *self.owner),
                )).fetchone()
                if row is None or row[0] != generation:
                    raise ChatSessionStaleError("chat session 已刪除或重置")
                existing = await (await connection.execute(
                    """SELECT sequence, content, turn_state FROM chat_messages
                    WHERE session_id = %s AND user_id = %s AND character_id = %s
                    AND generation = %s AND turn_id = %s AND role = 'user'""",
                    (session_id, *self.owner, generation, turn_id),
                )).fetchone()
                if existing is not None:
                    if existing[1] == message["content"]:
                        raise ChatSessionTurnReplay("turn_id 與內容已接收")
                    raise ChatSessionTurnConflict("同一 turn_id 不可對應不同內容")
                sequence = row[1]
                await connection.execute(
                    """INSERT INTO chat_messages
                    (session_id,user_id,character_id,generation,sequence,turn_id,role,content,status,turn_state,memory_source)
                    VALUES (%s,%s,%s,%s,%s,%s,'user',%s,'complete','pending',%s)""",
                    (session_id, *self.owner, generation, sequence, turn_id, message["content"], source_value),
                )
                revision = (await (await connection.execute(
                    """UPDATE chat_sessions SET next_message_sequence = next_message_sequence + 1,
                    revision = revision + 1, updated_at = now()
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    RETURNING revision""", (session_id, *self.owner),
                )).fetchone())[0]
                return {"sequence": sequence, "revision": revision, "replayed": False}

    async def update_user_source(self, session_id: str, generation: int, turn_id: str, message: dict) -> int:
        """Persist the long-term generation after MemoryRuntime accepts the input."""
        source = read_memory_source(message)
        if source is None:
            snapshot = await self.load_context(session_id)
            return snapshot["revision"] if snapshot else 0
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                row = await (await connection.execute(
                    """UPDATE chat_messages SET memory_source = %s
                    WHERE session_id = %s AND user_id = %s AND character_id = %s
                    AND generation = %s AND turn_id = %s AND role = 'user'
                    RETURNING sequence""",
                    (Jsonb(dict(message[MEMORY_SOURCE_FIELD])), session_id, *self.owner,
                     generation, turn_id),
                )).fetchone()
                if row is None:
                    raise ChatSessionStaleError("找不到目前 user turn")
                return (await (await connection.execute(
                    """UPDATE chat_sessions SET revision = revision + 1, updated_at = now()
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    AND generation = %s AND status = 'active' RETURNING revision""",
                    (session_id, *self.owner, generation),
                )).fetchone())[0]

    async def finish_turn(
        self, session_id: str, generation: int, turn_id: str,
        assistant_text: str | None = None, *, outcome: str = "completed",
    ) -> int:
        """Close a pending user turn idempotently without rebuilding history."""
        if outcome not in {"completed", "interrupted", "failed"}:
            raise ValueError("無效的 turn outcome")
        if assistant_text is not None and (not isinstance(assistant_text, str) or not assistant_text):
            assistant_text = None
        if outcome == "completed" and not assistant_text:
            raise ValueError("completed turn 必須有 assistant 內容")
        assistant_status = "complete" if outcome == "completed" else "interrupted"
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                session = await (await connection.execute(
                    """SELECT generation, next_message_sequence FROM chat_sessions
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    AND status = 'active' FOR UPDATE""", (session_id, *self.owner),
                )).fetchone()
                if session is None or session[0] != generation:
                    raise ChatSessionStaleError("chat session 已刪除或重置")
                user = await (await connection.execute(
                    """SELECT sequence, content, turn_state FROM chat_messages
                    WHERE session_id = %s AND user_id = %s AND character_id = %s
                    AND generation = %s AND turn_id = %s AND role = 'user' FOR UPDATE""",
                    (session_id, *self.owner, generation, turn_id),
                )).fetchone()
                if user is None:
                    raise ChatSessionStaleError("找不到目前 user turn")
                if user[2] != "pending":
                    return (await (await connection.execute(
                        "SELECT revision FROM chat_sessions WHERE id = %s AND user_id = %s AND character_id = %s",
                        (session_id, *self.owner),
                    )).fetchone())[0]
                if assistant_text:
                    existing = await (await connection.execute(
                        """SELECT content, status FROM chat_messages
                        WHERE session_id = %s AND user_id = %s AND character_id = %s
                        AND generation = %s AND turn_id = %s AND role = 'assistant' FOR UPDATE""",
                        (session_id, *self.owner, generation, turn_id),
                    )).fetchone()
                    if existing is not None:
                        if existing != (assistant_text, assistant_status):
                            raise ChatSessionTurnConflict("同一 turn_id 已有不同 assistant 結果")
                    else:
                        sequence = session[1]
                        await connection.execute(
                            """INSERT INTO chat_messages
                            (session_id,user_id,character_id,generation,sequence,turn_id,role,content,status)
                            VALUES (%s,%s,%s,%s,%s,%s,'assistant',%s,%s)""",
                            (session_id, *self.owner, generation, sequence, turn_id, assistant_text, assistant_status),
                        )
                        await connection.execute(
                            """UPDATE chat_sessions SET next_message_sequence = next_message_sequence + 1
                            WHERE id = %s AND user_id = %s AND character_id = %s""",
                            (session_id, *self.owner),
                        )
                await connection.execute(
                    """UPDATE chat_messages SET turn_state = %s
                    WHERE session_id = %s AND user_id = %s AND character_id = %s
                    AND generation = %s AND turn_id = %s AND role = 'user'""",
                    (outcome, session_id, *self.owner, generation, turn_id),
                )
                return (await (await connection.execute(
                    """UPDATE chat_sessions SET revision = revision + 1, updated_at = now()
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    RETURNING revision""", (session_id, *self.owner),
                )).fetchone())[0]

    async def load_compression_prefix(
        self, session_id: str, generation: int, *, trigger_messages: int,
        keep_recent_messages: int, force: bool = False, max_messages: int = 64,
    ) -> dict[str, Any] | None:
        """Return one bounded, contiguous batch of closed turns after the cursor."""
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                session = await (await connection.execute(
                    """SELECT generation, summary, summary_through_sequence
                    FROM chat_sessions WHERE id = %s AND user_id = %s AND character_id = %s
                    AND status = 'active'""", (session_id, *self.owner),
                )).fetchone()
                if session is None or session[0] != generation:
                    raise ChatSessionStaleError("chat session 已刪除或重置")
                cursor = session[2]
                rows = await (await connection.execute(
                    """SELECT sequence, turn_id, role, content, status, turn_state,
                    memory_source, created_at FROM chat_messages
                    WHERE session_id = %s AND user_id = %s AND character_id = %s
                    AND generation = %s AND sequence > %s ORDER BY sequence LIMIT %s""",
                    (session_id, *self.owner, generation, cursor, max_messages),
                )).fetchall()
        groups: list[list[dict[str, Any]]] = []
        current_key = object()
        for row in rows:
            item = self._message_item(row)
            key = item.get("turn_id") or ("sequence", item["sequence"])
            if key != current_key:
                groups.append([])
                current_key = key
            groups[-1].append(item)
        closed_groups: list[list[dict[str, Any]]] = []
        for group in groups:
            if any(item.get("role") == "user" and item.get("turn_state") == "pending" for item in group):
                break
            closed_groups.append(group)
        eligible_count = sum(len(group) for group in closed_groups)
        if eligible_count < trigger_messages and not force:
            return None
        target_count = eligible_count - keep_recent_messages
        if target_count <= 0:
            return None
        selected: list[dict[str, Any]] = []
        for group in closed_groups:
            if len(selected) + len(group) > target_count:
                break
            selected.extend(group)
        if not selected:
            return None
        return {
            "previous_summary": session[1] or "",
            "cursor": cursor,
            "through_sequence": selected[-1]["sequence"],
            "messages": selected,
            "eligible_message_count": eligible_count,
        }

    async def commit_summary(
        self, session_id: str, generation: int, expected_cursor: int,
        summary: str, through_sequence: int,
    ) -> int:
        """Advance only the summary cursor; original messages remain immutable."""
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
            raise ValueError("摘要內容無效")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                row = await (await connection.execute(
                    """SELECT generation, summary_through_sequence FROM chat_sessions
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    AND status = 'active' FOR UPDATE""", (session_id, *self.owner),
                )).fetchone()
                if row != (generation, expected_cursor) or through_sequence <= expected_cursor:
                    raise ChatSessionStaleError("摘要 cursor 已過期")
                range_row = await (await connection.execute(
                    """SELECT count(*), min(sequence), max(sequence),
                    bool_or(role = 'user' AND turn_state = 'pending')
                    FROM chat_messages WHERE session_id = %s AND user_id = %s
                    AND character_id = %s AND generation = %s
                    AND sequence > %s AND sequence <= %s""",
                    (session_id, *self.owner, generation, expected_cursor, through_sequence),
                )).fetchone()
                expected_count = through_sequence - expected_cursor
                if (range_row[0] != expected_count or range_row[1] != expected_cursor + 1
                        or range_row[2] != through_sequence or range_row[3]):
                    raise ChatSessionStaleError("摘要範圍不連續或包含未結束回合")
                return (await (await connection.execute(
                    """UPDATE chat_sessions SET summary = %s,
                    summary_through_sequence = %s, revision = revision + 1, updated_at = now()
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    RETURNING revision""",
                    (summary.strip(), through_sequence, session_id, *self.owner),
                )).fetchone())[0]

    async def update_emotion(self, session_id: str, generation: int, state: dict) -> int:
        validated = validate_emotion_state(state)
        if validated is None:
            raise ValueError("無效的 Emotion State")
        return await self._update_state(session_id, generation, "emotion_state", Jsonb(validated))

    async def _update_state(self, session_id: str, generation: int, column: str, value) -> int:
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(
                    sql.SQL("""UPDATE chat_sessions SET {} = %s, revision = revision + 1, updated_at = now()
                    WHERE id = %s AND user_id = %s AND character_id = %s
                    AND status = 'active' AND generation = %s RETURNING revision""").format(sql.Identifier(column)),
                    (value, session_id, *self.owner, generation),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ChatSessionStaleError("chat session 已刪除或重置")
                return row[0]

    async def reset(self, session_id: str, *, connection=None) -> dict[str, int]:
        async def execute(active_connection):
            await self._set_search_path(active_connection)
            row = await (await active_connection.execute(
                """UPDATE chat_sessions SET generation = generation + 1, revision = revision + 1,
                summary = '', summary_through_sequence = -1, next_message_sequence = 0,
                emotion_state = NULL, updated_at = now()
                WHERE id = %s AND user_id = %s AND character_id = %s AND status = 'active'
                RETURNING generation, revision""", (session_id, *self.owner),
            )).fetchone()
            if row is None:
                raise ChatSessionStaleError("chat session 已刪除")
            await active_connection.execute(
                "DELETE FROM chat_messages WHERE session_id = %s AND user_id = %s AND character_id = %s",
                (session_id, *self.owner),
            )
            return {"generation": row[0], "revision": row[1]}
        if connection is not None:
            return await execute(connection)
        async with self.pool.connection() as own_connection:
            async with own_connection.transaction():
                return await execute(own_connection)

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(
                    """SELECT s.id, s.status, s.updated_at, s.summary <> '', s.emotion_state IS NOT NULL,
                    count(m.sequence), COALESCE((array_agg(m.content ORDER BY m.sequence)
                        FILTER (WHERE m.role = 'user'))[1], ''), s.summary_through_sequence
                    FROM chat_sessions s LEFT JOIN chat_messages m
                    ON (m.session_id,m.user_id,m.character_id,m.generation) =
                       (s.id,s.user_id,s.character_id,s.generation)
                    WHERE s.user_id = %s AND s.character_id = %s
                    GROUP BY s.id,s.user_id,s.character_id,s.status,s.updated_at,s.summary,
                        s.emotion_state,s.summary_through_sequence
                    ORDER BY s.updated_at DESC""", self.owner,
                )
                return [{"session_id": row[0], "status": row[1], "updated_at": row[2].isoformat(),
                         "has_summary": row[3], "has_emotion_state": row[4],
                         "message_count": row[5], "preview": row[6][:160],
                         "summary_through_sequence": row[7]} for row in await cursor.fetchall()]

    async def load_session_messages(
        self, session_id: str, *, limit: int = 100, offset: int = 0,
    ) -> dict[str, Any] | None:
        normalized = normalize_session_id(session_id)
        if normalized != session_id:
            raise ValueError("無效的 session_id")
        limit = min(max(int(limit), 1), 200)
        offset = max(int(offset), 0)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                session = await (await connection.execute(
                    self._session_select() + """ WHERE id = %s AND user_id = %s
                    AND character_id = %s""", (session_id, *self.owner),
                )).fetchone()
                if session is None:
                    return None
                total = (await (await connection.execute(
                    """SELECT count(*) FROM chat_messages WHERE session_id = %s AND user_id = %s
                    AND character_id = %s AND generation = %s""", (session_id, *self.owner, session[1]),
                )).fetchone())[0]
                cursor = await connection.execute(
                    """SELECT sequence, turn_id, role, content, status, turn_state,
                    memory_source, created_at FROM chat_messages
                    WHERE session_id = %s AND user_id = %s AND character_id = %s
                    AND generation = %s ORDER BY sequence LIMIT %s OFFSET %s""",
                    (session_id, *self.owner, session[1], limit, offset),
                )
                items = [self._message_item(row) for row in await cursor.fetchall()]
                return {
                    "session_id": session[0], "generation": session[1], "revision": session[2],
                    "summary": session[3], "summary_through_sequence": session[5],
                    "emotion_state": validate_emotion_state(session[4]),
                    "messages": items, "message_count": total, "limit": limit, "offset": offset,
                    "created_at": session[7].isoformat(), "updated_at": session[8].isoformat(),
                }

    async def delete(self, session_id: str) -> dict[str, Any]:
        normalized = normalize_session_id(session_id)
        if normalized != session_id:
            raise ValueError("無效的 session_id")
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(
                    "DELETE FROM chat_sessions WHERE id = %s AND user_id = %s AND character_id = %s",
                    (session_id, *self.owner),
                )
                return {"session_id": session_id, "deleted_sessions": cursor.rowcount}

    async def delete_all(self) -> dict[str, int]:
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._set_search_path(connection)
                cursor = await connection.execute(
                    "DELETE FROM chat_sessions WHERE user_id = %s AND character_id = %s", self.owner,
                )
                return {"session_count": cursor.rowcount}

    @staticmethod
    def persistable_messages(messages: list[dict]) -> list[dict]:
        """Normalize legacy/test views without making it a persistence operation."""
        persisted = []
        for message in messages:
            role = get_msg_field(message, "role", "")
            content = get_msg_field(message, "content", "")
            if role not in {"user", "assistant"} or not isinstance(content, str) or not content:
                continue
            item = {"role": role, "content": content}
            turn_id = message.get("turn_id") if isinstance(message, dict) else None
            if isinstance(turn_id, str) and 0 < len(turn_id) <= 128:
                item["turn_id"] = turn_id
            if role == "assistant" and message.get("status") == "interrupted":
                item["status"] = "interrupted"
            if role == "user" and message.get("turn_state") in {"pending", "completed", "interrupted", "failed"}:
                item["turn_state"] = message["turn_state"]
            if role == "user" and read_memory_source(message) is not None:
                item[MEMORY_SOURCE_FIELD] = dict(message[MEMORY_SOURCE_FIELD])
            persisted.append(item)
        return persisted
