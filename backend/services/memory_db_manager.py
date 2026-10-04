"""以單一 transaction 套用已驗證的 Memory LLM decisions。"""

import re
from uuid import UUID, uuid4, uuid5

from pgvector import Vector
from psycopg import sql
from psycopg.types.json import Jsonb

from domain.memory_decisions import validate_decisions, validate_sources, validate_batch, validate_current_source
from domain.memory_scope import MemoryScope
from domain.memory_routing import instruction_policy, forget_scope
from services.memory_import import LegacyEntry


def _memory_keywords(canonical: str, subject_key: str | None = None,
                     search_terms: list[str] | None = None) -> list[str]:
    """檢索詞優先採用已驗證的語意索引，再補 canonical／subject 的穩定詞面。

    查詢端會把中文連續字串切成相鄰雙字詞；索引端也保留相同粒度，避免
    ``甜點偏好`` 與 ``哪種甜點`` 因陣列元素必須完全相等而錯失詞面命中。
    """
    values = [*(search_terms or [])]
    if subject_key:
        values.extend([subject_key, *re.findall(r"[a-z0-9]{2,}", subject_key.lower())])
    values.extend(re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]{2,4}", canonical.lower()))
    result = []
    for value in values:
        normalized = value.strip().lower()
        if not normalized:
            continue
        parts = [normalized, *re.findall(r"[a-z0-9_.-]{2,}", normalized)]
        for chinese in re.findall(r"[\u3400-\u9fff]{2,}", normalized):
            parts.extend(chinese[index:index + 2] for index in range(len(chinese) - 1))
        for part in parts:
            if part not in result:
                result.append(part)
    return result[:24]


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
                    "SELECT generation FROM memory_scope_state WHERE user_id = %s AND character_id = %s FOR UPDATE", owner,
                )
                for entry in entries:
                    if entry.id not in embeddings:
                        raise ValueError("Legacy import 缺少 embedding")
                    keywords = _memory_keywords(entry.canonical_text, entry.subject_key)
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
        embeddings: dict[int, list[float]], context_ids: tuple[UUID, ...] = (), diagnostic: dict | None = None,
        target_snapshots: dict | None = None,
    ) -> bool:
        forget = instruction_policy(job["source_text"]) == "forget"
        validate_decisions({"decisions": decisions}, allowed_targets, explicit_forget=forget)
        validate_batch(decisions)
        if any(item["action"] == "FORGET" for item in decisions) and any(
            item["action"] not in {"FORGET", "IGNORE"} for item in decisions
        ):
            raise ValueError("FORGET 不可與其他記憶 mutation 混用")
        owner = (self.scope.user_id, self.scope.character_id)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                generation = await (await connection.execute(
                    "SELECT generation FROM memory_scope_state WHERE user_id = %s AND character_id = %s FOR UPDATE", owner,
                )).fetchone()
                current = await (await connection.execute(
                    """SELECT status, attempts, generation, source_ids,
                    lease_until > now() AND expires_at > now() AND route_finalized, instruction, conversation_id FROM memory_jobs
                    WHERE id = %s AND user_id = %s AND character_id = %s FOR UPDATE""",
                    (job["id"], *owner),
                )).fetchone()
                if (not generation or not current or generation[0] != job["generation"]
                        or current[:3] != ("running", job["attempts"], job["generation"]) or not current[4]):
                    return False
                if len(context_ids) > 3 or len(set(context_ids)) != len(context_ids):
                    raise ValueError("buffer promotion 數量無效")
                if context_ids:
                    selected_buffers = await (await connection.execute(
                        """SELECT id, source_ids FROM memory_jobs WHERE user_id = %s AND character_id = %s
                        AND id = ANY(%s) AND status = 'buffered' AND conversation_id = %s
                        AND generation = %s AND expires_at > now() FOR UPDATE""",
                        (*owner, list(context_ids), current[6], job["generation"]),
                    )).fetchall()
                    if {row[0] for row in selected_buffers} != set(context_ids):
                        raise ValueError("buffer promotion owner、類型或狀態無效")

                authorized = set(current[3])
                if context_ids:
                    authorized.update(value for row in selected_buffers for value in row[1])
                source_ids = validate_sources(decisions, authorized)
                for decision in decisions:
                    validate_current_source({UUID(value) for value in decision["source_ids"]}, job["id"], job["source_text"])
                if current[5] == "no_store":
                    raise ValueError("禁止保存")
                # 授權以持久化當輪原文為準，不接受呼叫者改寫 job 的遺忘權限。
                current_source = await (await connection.execute(
                    "SELECT raw_text FROM memory_sources WHERE id = %s AND user_id = %s AND character_id = %s AND speaker = 'user' FOR SHARE",
                    (job["id"], *owner),
                )).fetchone()
                if not current_source or current_source[0] != job["source_text"]:
                    raise ValueError("當輪來源已失效或不符")
                if instruction_policy(current_source[0]) != current[5]:
                    raise ValueError("持久化授權契約不符")
                for row in selected_buffers if context_ids else []:
                    if not source_ids.intersection(row[1]):
                        raise ValueError("不可採用未引用的待補來源")
                sources = await (await connection.execute(
                    """SELECT id FROM memory_sources WHERE user_id = %s AND character_id = %s
                    AND id = ANY(%s) AND speaker = 'user' AND raw_text IS NOT NULL FOR SHARE""",
                    (*owner, list(source_ids)),
                )).fetchall()
                if {row[0] for row in sources} != source_ids:
                    raise ValueError("候選來源已失效")
                has_forget = any(item["action"] == "FORGET" for item in decisions)
                checked_targets = set()
                for index, decision in enumerate(decisions):
                    action = decision["action"]
                    source_id = UUID(decision["source_ids"][0])
                    barrier = await (await connection.execute(
                        """SELECT 1 FROM memory_forget_barriers WHERE user_id = %s AND character_id = %s
                        AND created_at >= %s AND (fact_hash = md5(%s) OR (subject_hash IS NOT NULL AND subject_hash = md5(%s))) LIMIT 1""",
                        (*owner, job["created_at"], decision.get("canonical_text"), decision.get("subject_key")),
                    )).fetchone()
                    if barrier:
                        raise ValueError("舊候選受遺忘屏障阻擋")
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
                            """SELECT id, group_id, status, observed_at, updated_at, canonical_text FROM memory_items
                            WHERE user_id = %s AND character_id = %s AND id = ANY(%s) FOR UPDATE""",
                            (*owner, targets),
                        ) as cursor:
                            rows = await cursor.fetchall()
                    if len(rows) != len(set(targets)) or any(row[2] not in ({"active", "conflict", "superseded", "archived", "expired"} if action == "FORGET" else {"active", "conflict"}) for row in rows):
                        raise ValueError("Memory target owner 或狀態無效")
                    if action not in {"FORGET", "REINFORCE"} and any(row[3] > job["created_at"] for row in rows):
                        raise ValueError("舊工作不可覆寫較新的事實")
                    if rows:
                        if target_snapshots is None or any(row[0] not in target_snapshots for row in rows):
                            raise ValueError("缺少已交付 target 快照")
                        if any((row[2], row[4], row[5]) != (target_snapshots[row[0]]["status"],
                               target_snapshots[row[0]]["updated_at"], target_snapshots[row[0]]["canonical_text"]) for row in rows if row[0] not in checked_targets):
                            raise ValueError("target 已在檢索後變更，需重新處理")
                    checked_targets.update(row[0] for row in rows)
                    target_map = {row[0]: row for row in rows}
                    if action == "CREATE" and decision.get("subject_key"):
                        newer = await (await connection.execute(
                            """SELECT 1 FROM memory_items WHERE user_id = %s AND character_id = %s
                            AND subject_key = %s AND observed_at > %s AND canonical_text <> %s AND status IN ('active', 'conflict') LIMIT 1""",
                            (*owner, decision["subject_key"], job["created_at"], decision["canonical_text"].strip()),
                        )).fetchone()
                        if newer:
                            raise ValueError("較新的事實已存在，舊候選需重新審查")
                    if action == "CREATE":
                        duplicate = await (await connection.execute(
                            """SELECT id FROM memory_items WHERE user_id = %s AND character_id = %s
                            AND status = 'active' AND memory_type = %s
                            AND subject_key IS NOT DISTINCT FROM %s AND canonical_text = %s
                            AND valid_to IS NOT DISTINCT FROM %s::timestamptz
                            AND (%s::timestamptz IS NULL OR valid_from = %s::timestamptz)
                            AND (expires_at IS NULL OR expires_at > now()) LIMIT 1 FOR UPDATE""",
                            (*owner, decision["memory_type"], decision.get("subject_key"), decision["canonical_text"].strip(),
                             decision.get("valid_to"), decision.get("valid_from"), decision.get("valid_from")),
                        )).fetchone()
                        if duplicate:
                            action, targets = "REINFORCE", [duplicate[0]]
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
                        keywords = _memory_keywords(canonical, decision.get("subject_key"),
                                                   decision.get("search_terms"))
                        await connection.execute(
                            """INSERT INTO memory_items
                            (id, user_id, character_id, group_id, memory_type, canonical_text, subject_key,
                             keywords, status, importance, confidence, retention_class, embedding, embedding_model,
                             embedding_contract, observed_at, valid_from, valid_to, expires_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    %s, %s, COALESCE(%s, %s), %s, %s)""",
                            (new_id, *owner, group_id, decision.get("memory_type", "event"), canonical,
                             decision.get("subject_key"), keywords, status, decision.get("importance", 0.5),
                             decision.get("confidence", 0.5), decision.get("retention_class", "normal"),
                             Vector(embeddings[index]), self.embedding_model, self.embedding_contract, job["created_at"],
                             decision.get("valid_from"), job["created_at"],
                             decision.get("valid_to"), decision.get("expires_at")),
                        )
                        await connection.execute(
                            """INSERT INTO memory_evidence (user_id, character_id, memory_id, source_id, kind)
                            VALUES (%s, %s, %s, %s, %s)""",
                            (*owner, new_id, source_id, "contradicts" if action == "CONTRADICT" else "origin"),
                        )
                        if action == "SUPERSEDE":
                            await connection.execute(
                                """UPDATE memory_items SET status = 'superseded', valid_to = COALESCE(%s::timestamptz, %s), updated_at = now()
                                WHERE group_id = %s AND id <> %s AND user_id = %s AND character_id = %s AND status IN ('active', 'conflict')""", (decision.get("valid_from"), job["created_at"], group_id, new_id, *owner),
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
                            """UPDATE memory_items SET updated_at = now()
                            WHERE id = %s AND user_id = %s AND character_id = %s""", (targets[0], *owner),
                        )
                    elif action == "MERGE":
                        if len(targets) < 2:
                            raise ValueError("MERGE 至少需要兩個 target，首個為保留版本")
                        winner = targets[0]
                        # MERGE 沒有建立新 memory item，仍須把促成本輪合併的
                        # 第一個使用者來源連到 winner，避免只留下操作 audit、
                        # 卻無法從正式記憶追溯本輪 evidence。
                        await connection.execute(
                            """INSERT INTO memory_evidence (user_id, character_id, memory_id, source_id, kind)
                            VALUES (%s, %s, %s, %s, 'supports') ON CONFLICT DO NOTHING""",
                            (*owner, winner, source_id),
                        )
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
                        scope = decision.get("forget_scope", "fact")
                        if scope != forget_scope(job["source_text"]):
                            raise ValueError("遺忘範圍超出工作授權")
                        # 同一事實的版本、衝突及合併關係在同一交易內清除。
                        expanded = await (await connection.execute(
                            """WITH RECURSIVE family(id) AS (
                                SELECT id FROM memory_items WHERE user_id = %s AND character_id = %s
                                AND group_id = ANY(%s)
                                UNION
                                SELECT CASE WHEN r.from_id = f.id THEN r.to_id ELSE r.from_id END
                                FROM memory_relations r JOIN family f ON r.from_id = f.id OR r.to_id = f.id
                                WHERE r.user_id = %s AND r.character_id = %s AND r.kind IN ('supersedes', 'merged_into', 'contradicts')
                            ) SELECT DISTINCT id FROM family""",
                            (*owner, [row[1] for row in rows], *owner),
                        )).fetchall()
                        if scope == "fact":
                            targets = [row[0] for row in expanded]
                        await connection.execute(
                            """INSERT INTO memory_forget_barriers (user_id, character_id, subject_hash, fact_hash)
                            SELECT user_id, character_id, CASE WHEN %s THEN md5(subject_key) ELSE NULL END, md5(canonical_text) FROM memory_items
                            WHERE user_id = %s AND character_id = %s AND id = ANY(%s)""", (scope == "fact", *owner, targets),
                        )
                        if not forget:
                            raise ValueError("FORGET 缺少明確 user request")
                        source_messages = await (await connection.execute(
                            """SELECT DISTINCT source.message_id, source.id FROM memory_sources AS source
                            JOIN memory_evidence AS evidence ON evidence.source_id = source.id
                            AND evidence.user_id = source.user_id AND evidence.character_id = source.character_id
                            WHERE source.user_id = %s AND source.character_id = %s
                            AND evidence.memory_id = ANY(%s)""",
                            (*owner, targets),
                        )).fetchall()
                        erased_sources = [row[1] for row in source_messages] + [job["id"]]
                        affected_jobs = await (await connection.execute(
                            """SELECT id FROM memory_jobs WHERE user_id = %s AND character_id = %s
                            AND (source_ids && %s::uuid[] OR id = %s)""",
                            (*owner, erased_sources, job["id"]),
                        )).fetchall()
                        message_ids = list({row[0] for row in source_messages if row[0]} | {row[0] for row in affected_jobs})
                        duplicates = await (await connection.execute(
                            """SELECT id FROM memory_sources WHERE user_id = %s AND character_id = %s
                            AND (message_id = ANY(%s) OR raw_text IN (SELECT raw_text FROM memory_sources
                            WHERE user_id = %s AND character_id = %s AND id = ANY(%s)))""",
                            (*owner, message_ids, *owner, erased_sources),
                        )).fetchall()
                        erased_sources = list(set(erased_sources) | {row[0] for row in duplicates})
                        affected = await (await connection.execute(
                            """SELECT message_id FROM memory_jobs WHERE user_id = %s AND character_id = %s
                            AND source_ids && %s::uuid[]""", (*owner, erased_sources),
                        )).fetchall()
                        message_ids = list(set(message_ids) | {row[0] for row in affected})
                        await connection.execute(
                            "UPDATE memory_sources SET raw_text = NULL WHERE user_id = %s AND character_id = %s AND id = ANY(%s)",
                            (*owner, erased_sources),
                        )
                        await connection.execute(
                            """UPDATE memory_audit SET decision = NULL, target_id = NULL,
                            reason_class = 'forgotten' WHERE user_id = %s AND character_id = %s
                            AND (target_id = ANY(%s) OR source_event_id = ANY(%s))""",
                            (*owner, targets, message_ids),
                        )
                        await connection.execute(
                            """UPDATE memory_jobs SET recent_dialogue = NULL, missing_context = NULL, source_ids = '{}',
                            agent_diagnostics = '{}', embedding_diagnostics = '{}', pending_target_ids = '{}',
                            lease_until = NULL, status = 'cancelled' WHERE user_id = %s AND character_id = %s
                            AND message_id = ANY(%s)""", (*owner, message_ids),
                        )
                        deleted_count = (await connection.execute(
                            "DELETE FROM memory_items WHERE user_id = %s AND character_id = %s AND id = ANY(%s)",
                            (*owner, targets),
                        )).rowcount
                        await connection.execute(
                            """DELETE FROM memory_sources AS source
                            WHERE source.user_id = %s AND source.character_id = %s
                            AND source.id = ANY(%s)
                            AND NOT EXISTS (SELECT 1 FROM memory_evidence AS evidence
                            WHERE evidence.source_id = source.id AND evidence.user_id = source.user_id
                            AND evidence.character_id = source.character_id)""", (*owner, erased_sources),
                        )
                    elif action != "IGNORE":
                        raise ValueError("不支援的 Memory action")
                    if action in {"CREATE", "SUPERSEDE", "CONTRADICT", "REINFORCE", "MERGE"}:
                        evidence_target = new_id if action in {"CREATE", "SUPERSEDE", "CONTRADICT"} else targets[0]
                        # CREATE/SUPERSEDE/CONTRADICT 已先寫入首個來源的 origin/contradicts；
                        # 只把其餘來源記成 supports，避免同一來源重複留下兩筆證據。
                        for additional_source in decision["source_ids"]:
                            if UUID(additional_source) == source_id:
                                continue
                            await connection.execute(
                                """INSERT INTO memory_evidence (user_id, character_id, memory_id, source_id, kind)
                                VALUES (%s,%s,%s,%s,'supports') ON CONFLICT DO NOTHING""",
                                (*owner, evidence_target, UUID(additional_source)),
                            )
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
                if context_ids:
                    await connection.execute(
                        """UPDATE memory_jobs SET status = 'discarded',
                        recent_dialogue = NULL, missing_context = NULL, pending_target_ids = '{}', updated_at = now()
                        WHERE user_id = %s AND character_id = %s AND id = ANY(%s)""",
                        (*owner, list(context_ids)),
                    )
                await connection.execute(
                    """UPDATE memory_jobs SET status = %s, lease_until = NULL, pending_target_ids = '{}', updated_at = now(),
                    recent_dialogue = NULL, missing_context = NULL, context_job_ids = %s, agent_diagnostics = %s::jsonb
                    WHERE id = %s AND user_id = %s AND character_id = %s""",
                    (terminal, list(context_ids), Jsonb({**(diagnostic or {}), "attempt": job["attempts"],
                        "result": terminal, "committed": True}), job["id"], *owner),
                )
                return True
