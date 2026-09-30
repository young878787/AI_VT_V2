"""以單一 transaction 套用已驗證的 Memory LLM decisions。"""

import re
from uuid import UUID, uuid4, uuid5

from pgvector import Vector
from psycopg import sql
from psycopg.types.json import Jsonb

from domain.memory_decisions import validate_decisions
from domain.memory_scope import MemoryScope
from services.memory_llm import _FORGET_REQUEST
from services.memory_import import LegacyEntry


class MemoryDBManager:
    def __init__(
        self, pool, scope: MemoryScope, model: str, embedding_model: str, embedding_contract: str,
    ) -> None:
        self.pool = pool
        self.scope = scope
        self.model = model
        self.embedding_model = embedding_model
        self.embedding_contract = embedding_contract

    async def import_legacy(self, entries: list[LegacyEntry], embeddings: dict[UUID, list[float]]) -> int:
        """舊檔匯入也由 DB Manager 寫入，固定 UUID 與 audit 使重跑安全。"""
        owner = (self.scope.user_id, self.scope.character_id)
        imported = 0
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    "INSERT INTO memory_scope_state (user_id, character_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", owner,
                )
                await connection.execute(
                    "SELECT generation FROM memory_scope_state WHERE user_id = %s AND character_id = %s FOR SHARE", owner,
                )
                for entry in entries:
                    if entry.id not in embeddings:
                        raise ValueError("Legacy import 缺少 embedding")
                    keywords = sorted(set(re.findall(
                        r"[a-z0-9]{2,}|[\u3400-\u9fff]{2,4}", entry.canonical_text.lower(),
                    )))[:12]
                    cursor = await connection.execute(
                        """INSERT INTO memory_items
                        (id, user_id, character_id, group_id, memory_type, canonical_text, subject_key,
                         keywords, status, importance, confidence, retention_class, embedding,
                         embedding_model, embedding_contract, observed_at, valid_from)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0.6, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id, user_id, character_id) DO NOTHING RETURNING id""",
                        (entry.id, *owner, entry.id, entry.memory_type, entry.canonical_text,
                         entry.subject_key, keywords, entry.status, entry.importance,
                         "important" if entry.memory_type == "special" else "normal",
                         Vector(embeddings[entry.id]), self.embedding_model, self.embedding_contract,
                         entry.observed_at, entry.observed_at),
                    )
                    if await cursor.fetchone() is None:
                        continue
                    imported += 1
                    source_id = uuid5(entry.id, "legacy_source")
                    await connection.execute(
                        """INSERT INTO memory_sources
                        (id, user_id, character_id, speaker, raw_text, occurred_at)
                        VALUES (%s, %s, %s, 'legacy_import', %s, %s)""",
                        (source_id, *owner, entry.canonical_text, entry.observed_at),
                    )
                    await connection.execute(
                        """INSERT INTO memory_evidence (user_id, character_id, memory_id, source_id, kind)
                        VALUES (%s, %s, %s, %s, 'origin')""", (*owner, entry.id, source_id),
                    )
                    await connection.execute(
                        """INSERT INTO memory_audit
                        (id, user_id, character_id, operation_key, action, target_id, reason_class)
                        VALUES (%s, %s, %s, %s, 'LEGACY_IMPORT', %s, 'legacy_import')""",
                        (uuid4(), *owner, f"legacy:{entry.id}", entry.id),
                    )
        return imported

    async def apply(
        self, job: dict, decisions: list[dict], allowed_targets: set[UUID],
        embeddings: dict[int, list[float]], buffered_ids: tuple[UUID, ...] = (),
    ) -> bool:
        forget = bool(_FORGET_REQUEST.search(job["source_text"]))
        validate_decisions({"decisions": decisions}, allowed_targets, explicit_forget=forget)
        if any(item["action"] == "FORGET" for item in decisions) and any(
            item["action"] not in {"FORGET", "IGNORE"} for item in decisions
        ):
            raise ValueError("FORGET 不可與其他記憶 mutation 混用")
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                generation = await (await connection.execute(
                    "SELECT generation FROM memory_scope_state WHERE user_id = %s AND character_id = %s FOR SHARE", owner,
                )).fetchone()
                current = await (await connection.execute(
                    """SELECT status, attempts, generation FROM memory_jobs
                    WHERE id = %s AND user_id = %s AND character_id = %s FOR UPDATE""",
                    (job["id"], *owner),
                )).fetchone()
                if (not generation or not current or generation[0] != job["generation"]
                        or current != ("running", job["attempts"], job["generation"])):
                    return False
                if len(buffered_ids) > 3 or len(set(buffered_ids)) != len(buffered_ids):
                    raise ValueError("buffer promotion 數量無效")
                if buffered_ids:
                    selected_buffers = await (await connection.execute(
                        """SELECT id FROM memory_jobs WHERE user_id = %s AND character_id = %s
                        AND id = ANY(%s) AND status = 'buffered'
                        AND memory_type_hint = %s FOR UPDATE""",
                        (*owner, list(buffered_ids), job.get("memory_type_hint")),
                    )).fetchall()
                    if {row[0] for row in selected_buffers} != set(buffered_ids):
                        raise ValueError("buffer promotion owner、類型或狀態無效")

                source_id = uuid5(job["id"], "source")
                has_forget = any(item["action"] == "FORGET" for item in decisions)
                if decisions and not has_forget and any(item["action"] not in {"IGNORE", "ARCHIVE", "MERGE"} for item in decisions):
                    await connection.execute(
                        """INSERT INTO memory_sources
                        (id, user_id, character_id, conversation_id, message_id, speaker, raw_text, occurred_at)
                        VALUES (%s, %s, %s, %s, %s, 'user', %s, now())
                        ON CONFLICT (id, user_id, character_id) DO NOTHING""",
                        (source_id, *owner, job["conversation_id"], job["message_id"], job["source_text"]),
                    )
                for index, decision in enumerate(decisions):
                    action = decision["action"]
                    targets = [UUID(value) for value in decision["target_memory_ids"]]
                    operation_key = f"{job['id']}:{index}"
                    already_applied = await (await connection.execute(
                        "SELECT 1 FROM memory_audit WHERE user_id = %s AND character_id = %s AND operation_key = %s",
                        (*owner, operation_key),
                    )).fetchone()
                    if already_applied:
                        continue
                    rows = []
                    if targets:
                        async with await connection.execute(
                            """SELECT id, group_id, status FROM memory_items
                            WHERE user_id = %s AND character_id = %s AND id = ANY(%s) FOR UPDATE""",
                            (*owner, targets),
                        ) as cursor:
                            rows = await cursor.fetchall()
                    if len(rows) != len(set(targets)) or any(row[2] != "active" for row in rows):
                        raise ValueError("Memory target owner 或狀態無效")
                    target_map = {row[0]: row for row in rows}
                    new_id = uuid5(job["id"], f"decision:{index}")
                    audit_target = (new_id if action in {"CREATE", "SUPERSEDE", "CONTRADICT"}
                                    else targets[0] if targets and action != "FORGET" else None)
                    deleted_count = None
                    if action in {"CREATE", "SUPERSEDE", "CONTRADICT"}:
                        if index not in embeddings:
                            raise ValueError("Memory mutation 缺少必要 embedding")
                        if action in {"SUPERSEDE", "CONTRADICT"} and len(targets) != 1:
                            raise ValueError("版本與衝突操作只可指定一個 target")
                        group_id = target_map[targets[0]][1] if targets else new_id
                        status = "conflict" if action == "CONTRADICT" else "active"
                        canonical = decision["canonical_text"].strip()
                        keywords = sorted(set(re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]{2,4}", canonical.lower())))[:12]
                        await connection.execute(
                            """INSERT INTO memory_items
                            (id, user_id, character_id, group_id, memory_type, canonical_text, subject_key,
                             keywords, status, importance, confidence, retention_class, embedding, embedding_model,
                             embedding_contract, observed_at, valid_from, valid_to, expires_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, now(), COALESCE(%s, now()), %s, %s)""",
                            (new_id, *owner, group_id, decision.get("memory_type", "event"), canonical,
                             decision.get("subject_key"), keywords, status, decision.get("importance", 0.5),
                             decision.get("confidence", 0.5), decision.get("retention_class", "normal"),
                             Vector(embeddings[index]), self.embedding_model, self.embedding_contract,
                             decision.get("valid_from"),
                             decision.get("valid_to"), decision.get("expires_at")),
                        )
                        await connection.execute(
                            """INSERT INTO memory_evidence (user_id, character_id, memory_id, source_id, kind)
                            VALUES (%s, %s, %s, %s, %s)""",
                            (*owner, new_id, source_id, "contradicts" if action == "CONTRADICT" else "origin"),
                        )
                        if action == "SUPERSEDE":
                            await connection.execute(
                                """UPDATE memory_items SET status = 'superseded', valid_to = now(), updated_at = now()
                                WHERE id = %s AND user_id = %s AND character_id = %s""", (targets[0], *owner),
                            )
                            await connection.execute(
                                """INSERT INTO memory_relations (user_id, character_id, from_id, to_id, kind)
                                VALUES (%s, %s, %s, %s, 'supersedes')""", (*owner, new_id, targets[0]),
                            )
                        elif action == "CONTRADICT":
                            await connection.execute(
                                """INSERT INTO memory_relations (user_id, character_id, from_id, to_id, kind)
                                VALUES (%s, %s, %s, %s, 'contradicts')""", (*owner, new_id, targets[0]),
                            )
                    elif action == "REINFORCE":
                        if len(targets) != 1:
                            raise ValueError("REINFORCE 只可指定一個 target")
                        await connection.execute(
                            """INSERT INTO memory_evidence (user_id, character_id, memory_id, source_id, kind)
                            VALUES (%s, %s, %s, %s, 'supports') ON CONFLICT DO NOTHING""",
                            (*owner, targets[0], source_id),
                        )
                        await connection.execute(
                            """UPDATE memory_items SET observed_at = now(), updated_at = now()
                            WHERE id = %s AND user_id = %s AND character_id = %s""", (targets[0], *owner),
                        )
                    elif action == "MERGE":
                        if len(targets) < 2:
                            raise ValueError("MERGE 至少需要兩個 target，首個為保留版本")
                        winner = targets[0]
                        for loser in targets[1:]:
                            await connection.execute(
                                """INSERT INTO memory_evidence (user_id, character_id, memory_id, source_id, kind)
                                SELECT user_id, character_id, %s, source_id, kind FROM memory_evidence
                                WHERE memory_id = %s AND user_id = %s AND character_id = %s
                                ON CONFLICT DO NOTHING""", (winner, loser, *owner),
                            )
                            await connection.execute(
                                """UPDATE memory_items SET status = 'merged', updated_at = now()
                                WHERE id = %s AND user_id = %s AND character_id = %s""", (loser, *owner),
                            )
                            await connection.execute(
                                """INSERT INTO memory_relations (user_id, character_id, from_id, to_id, kind)
                                VALUES (%s, %s, %s, %s, 'merged_into')""", (*owner, loser, winner),
                            )
                    elif action == "ARCHIVE":
                        await connection.execute(
                            """UPDATE memory_items SET status = 'archived', valid_to = now(), updated_at = now()
                            WHERE id = ANY(%s) AND user_id = %s AND character_id = %s""",
                            (targets, *owner),
                        )
                    elif action == "FORGET":
                        if not forget:
                            raise ValueError("FORGET 缺少明確 user request")
                        source_messages = await (await connection.execute(
                            """SELECT DISTINCT source.message_id FROM memory_sources AS source
                            JOIN memory_evidence AS evidence ON evidence.source_id = source.id
                            AND evidence.user_id = source.user_id AND evidence.character_id = source.character_id
                            WHERE source.user_id = %s AND source.character_id = %s
                            AND evidence.memory_id = ANY(%s) AND source.message_id IS NOT NULL""",
                            (*owner, targets),
                        )).fetchall()
                        message_ids = [row[0] for row in source_messages]
                        await connection.execute(
                            """UPDATE memory_audit SET decision = NULL, target_id = NULL,
                            reason_class = 'forgotten' WHERE user_id = %s AND character_id = %s
                            AND (target_id = ANY(%s) OR source_event_id = ANY(%s))""",
                            (*owner, targets, message_ids),
                        )
                        await connection.execute(
                            """UPDATE memory_jobs SET source_text = NULL, recent_dialogue = NULL,
                            decisions = NULL, embedding = NULL WHERE user_id = %s AND character_id = %s
                            AND message_id = ANY(%s)""", (*owner, message_ids),
                        )
                        deleted_count = (await connection.execute(
                            "DELETE FROM memory_items WHERE user_id = %s AND character_id = %s AND id = ANY(%s)",
                            (*owner, targets),
                        )).rowcount
                        await connection.execute(
                            """DELETE FROM memory_sources AS source
                            WHERE source.user_id = %s AND source.character_id = %s
                            AND NOT EXISTS (SELECT 1 FROM memory_evidence AS evidence
                            WHERE evidence.source_id = source.id AND evidence.user_id = source.user_id
                            AND evidence.character_id = source.character_id)""", owner,
                        )
                    elif action != "IGNORE":
                        raise ValueError("不支援的 Memory action")
                    await connection.execute(
                        """INSERT INTO memory_audit
                        (id, user_id, character_id, operation_key, action, target_id, source_event_id,
                         reason_class, model, decision, deleted_count)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (uuid4(), *owner, operation_key, action, audit_target,
                         None if action == "FORGET" else job["id"],
                         "user_request" if action == "FORGET" else decision["reason"][:100],
                         self.model, None if action == "FORGET" else Jsonb(decision), deleted_count),
                    )
                terminal = "ignored" if not decisions or all(item["action"] == "IGNORE" for item in decisions) else "done"
                if buffered_ids:
                    await connection.execute(
                        """UPDATE memory_jobs SET status = 'discarded', source_text = NULL,
                        recent_dialogue = NULL, embedding = NULL, updated_at = now()
                        WHERE user_id = %s AND character_id = %s AND id = ANY(%s)""",
                        (*owner, list(buffered_ids)),
                    )
                await connection.execute(
                    """UPDATE memory_jobs SET status = %s, lease_until = NULL, updated_at = now(),
                    source_text = CASE WHEN %s THEN NULL ELSE source_text END,
                    recent_dialogue = CASE WHEN %s THEN NULL ELSE recent_dialogue END,
                    embedding = CASE WHEN %s THEN NULL ELSE embedding END,
                    decisions = %s, buffered_job_ids = %s
                    WHERE id = %s AND user_id = %s AND character_id = %s""",
                    (terminal, has_forget, has_forget, has_forget,
                     None if has_forget else Jsonb(decisions), list(buffered_ids), job["id"], *owner),
                )
                return True
