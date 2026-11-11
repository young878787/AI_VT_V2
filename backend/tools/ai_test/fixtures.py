"""四組測試的合成前置資料；不替操作題預植預期結果。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import time
from uuid import UUID, uuid5

from sqlalchemy import text

from domain.memory.cards import aggregate_mood, card_document, normalize_key
from domain.memory.embedding import normalize_embedding
from domain.memory.scope import conversation_id, message_id
from domain.memory.settings import load_memory_settings
from services.memory.db_manager import _memory_keywords

ROOT = Path(__file__).resolve().parents[3]
VECTOR_PATH = ROOT / "backend/tools/ai_test/cases/memory_vectors.json"


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def vector_contract(settings) -> dict:
    return {"model": settings.embedding_model, "dimension": settings.embedding_dimension,
            "query_prefix": settings.embedding_query_prefix,
            "document_prefix": settings.embedding_document_prefix, "normalization": "l2-v1",
            "sha256": settings.embedding_contract}


class DocumentVectors:
    """cache 初始 corpus document；同 ID 文字或契約漂移一律停止載入。

    含日期模板的記憶（vector_source=live）每天代換後文字不同，每次即時 embed 且不寫入 cache；
    新增的記憶只補算缺少的那幾筆，既有向量不重算。卡片與另有原話的 document 也存在同一份 cache，
    key 分別為 ``card:<情境>:<owner>:<主題>:<副主題>`` 與 ``source:<原話 key>``。
    """

    def __init__(self, path: Path, settings, client_factory=None):
        self.path, self.settings = Path(path), settings
        self.client_factory = client_factory
        self.cost = {"calls": 0, "cache_hits": 0, "live_documents": 0, "duration_sec": 0.0,
                     "input_tokens": None, "service_version": "unverified"}

    async def load(self, memories: list[dict], *, record_corpus: bool = True) -> dict[str, list[float]]:
        cached = [item for item in memories if item.get("vector_source") != "live"]
        corpus_hash = fingerprint(sorted(({"id": item["id"], "text": item["text"]} for item in cached),
                                         key=lambda item: item["id"]))
        contract = vector_contract(self.settings)
        data = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists()
                else {"version": 1, "contract": None, "corpus_sha256": None, "documents": {}})
        documents = data.get("documents")
        if data.get("version") != 1 or not isinstance(documents, dict):
            raise ValueError("fixture_invalid: vector cache schema")
        if documents and data.get("contract") != contract:
            raise ValueError("fixture_invalid: vector contract changed")
        result, missing = {}, []
        for memory in memories:
            key, value = memory["id"], documents.get(memory["id"])
            digest = hashlib.sha256(memory["text"].encode()).hexdigest()
            if memory.get("vector_source") == "live":
                missing.append((memory, None))
                continue
            if value is None:
                missing.append((memory, digest))
                continue
            if value.get("text_sha256") != digest:
                raise ValueError("fixture_invalid: document text changed")
            raw = value.get("vector")
            try:
                normalized = normalize_embedding(raw, self.settings.embedding_dimension)
            except ValueError as exc:
                raise ValueError("fixture_invalid: document vector") from exc
            if not math.isclose(math.hypot(*raw), 1.0, rel_tol=1e-5, abs_tol=1e-5):
                raise ValueError("fixture_invalid: document normalization")
            result[key] = normalized
            self.cost["cache_hits"] += 1
        if missing:
            from infrastructure.memory.embedding_client import MemoryEmbeddingClient
            client = (self.client_factory or MemoryEmbeddingClient)(self.settings)
            started = time.monotonic()
            try:
                for memory, digest in missing:
                    self.cost["calls"] += 1
                    vector = await client.embed(memory["text"], query=False)
                    vector = normalize_embedding(vector, self.settings.embedding_dimension)
                    result[memory["id"]] = vector
                    if digest is None:
                        self.cost["live_documents"] += 1
                    else:
                        documents[memory["id"]] = {"text_sha256": digest, "vector": vector}
            finally:
                self.cost["duration_sec"] += round(time.monotonic() - started, 4)
                await client.client.close()
            if any(digest is not None for _, digest in missing):
                # corpus_sha256 只記錄本次 cache 涵蓋的 corpus，不作為拒絕條件；漂移由逐筆 text_sha256 把關。
                # 卡片／原話等衍生 document 補算時不改寫 corpus 指紋。
                data.update(contract=contract, documents=documents,
                            serving_model=self.settings.embedding_serving_model, service_version="unverified")
                if record_corpus or data.get("corpus_sha256") is None:
                    data["corpus_sha256"] = corpus_hash
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(self.path)
        return result


class FixtureLoader:
    """只由 runner 操作專用 store；WebSocket 不接收任意 history／owner／工具內容。"""

    def __init__(self, store, *, vector_path: Path = VECTOR_PATH, manifest_path: Path | None = None,
                 settings=None, client_factory=None):
        if not re.fullmatch(r"test_[0-9a-f]{32}", store.schema) or not store.created:
            raise ValueError("fixture_invalid: fixture loader requires an owned test schema")
        self.store, self.vector_path = store, Path(vector_path)
        self.manifest_path = Path(manifest_path) if manifest_path else None
        self.settings, self.client_factory = settings, client_factory
        self.memory_ids: dict[str, str] = {}
        self.scenario: str | None = None
        self._seen_scenarios: set[str] = set()
        self._sessions: set[str] = set()
        self._vectors: dict[str, list[float]] | None = None
        self.embedding_cost = {"calls": 0, "cache_hits": 0, "duration_sec": 0.0,
                               "input_tokens": None, "service_version": "unverified"}

    async def prepare(self, case: dict, dataset: dict) -> dict:
        context = case.get("context", {})
        scenario = context["scenario"]
        reference = datetime.fromisoformat(dataset["reference_time"])
        if reference.tzinfo is None:
            raise ValueError("fixture_invalid: reference_time requires timezone")
        suite = dataset["suite"]
        memories = dataset.get("memories", [])
        by_id = {item["id"]: item for item in memories}
        if len(by_id) != len(memories):
            raise ValueError("fixture_invalid: duplicate memory IDs")
        if scenario != self.scenario:
            if scenario in self._seen_scenarios:
                raise ValueError("fixture_invalid: cannot silently rebuild an earlier scenario")
            if memories and self._vectors is None:
                settings = self.settings or load_memory_settings()
                cache = DocumentVectors(self.vector_path, settings, self.client_factory)
                self._vectors = await cache.load(memories)
                self.settings, self.embedding_cost = settings, cache.cost
            ids = context.get("memory_ids", [])
            if any(key not in by_id for key in ids):
                raise ValueError("fixture_invalid: missing initial memory")
            selected = [by_id[key] for key in ids]
            self.memory_ids = {key: str(uuid5(self.store.owner["user_id"], f"fixture:{scenario}:{key}"))
                               for key in by_id}
            cards = self._cards(selected, scenario)
            await self._derived_vectors(selected, cards)
            await asyncio.to_thread(self._seed_memories, selected, reference, scenario, cards)
            self.scenario = scenario
            self._seen_scenarios.add(scenario)
        session_id = "fixture_" + uuid5(self.store.owner["user_id"], f"{scenario}:{case['session']}").hex
        if session_id not in self._sessions:
            await asyncio.to_thread(self._seed_session, session_id, context, reference)
            self._sessions.add(session_id)
        if suite == "integration" and (context.get("tools") or context.get("snapshot")):
            raise ValueError("fixture_invalid: integration sources must stay live")
        if context.get("vision_rejection") and case["case_id"] not in {"to_007", "it_012"}:
            raise ValueError("fixture_invalid: unexpected vision fault")
        manifest = {"version": 1, "schema": self.store.schema, "suite": suite,
                    "case_id": case["case_id"], "scenario": scenario, "session_id": session_id,
                    "reference_time": reference.isoformat(), "tools": context.get("tools", {}),
                    "snapshot": context.get("snapshot"), "vision_rejection": bool(context.get("vision_rejection")),
                    "metadata": {"initial_memory_ids": context.get("memory_ids", []),
                        "memory_ids": dict(self.memory_ids), "fixture_sha256": fingerprint(context),
                        "history_source": "synthetic_fixture" if context.get("history") else "empty_or_live_session",
                        "summary_source": "synthetic_fixture" if context.get("summary") else "empty_or_live_session",
                        "tools_source": "live" if suite == "integration" else "fixture",
                        "clock_source": "live" if suite == "integration" else "fixture",
                        "source_and_transaction_clock": "live",
                        "reference_day_matches_runtime": reference.date() == datetime.now(reference.tzinfo).date(),
                        "document_vectors": "fixed_document_cache;live_templated_documents", "query_vectors": "live",
                        "embedding_cost": dict(self.embedding_cost),
                        "visual_evidence_unavailable": suite == "integration"}}
        if self.manifest_path:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.manifest_path)
        return manifest

    def _owner(self, memory: dict) -> dict:
        owner = dict(self.store.owner)
        if memory.get("owner", "self") == "other":
            owner["user_id"] = uuid5(owner["user_id"], "fixture_other_owner")
        return owner

    def _cards(self, memories: list[dict], scenario: str) -> dict:
        """依 fixture 的 card 欄位分組；有效要點、心情與卡片 document 的算法對齊 runtime 的卡片同步。"""
        cards = {}
        for memory in memories:
            card = memory.get("card")
            if card is None:
                continue
            owner_name = memory.get("owner", "self")
            entry = cards.setdefault((owner_name, card["domain"], card["title"]), {
                "key": f"card:{scenario}:{owner_name}:{card['domain']}:{card['title']}",
                "domain": card["domain"], "title": card["title"], "members": [],
                # 卡片 ID 不含情境：外來 owner 的資料不隨 reset 清除，跨情境同名卡沿用同一列。
                "id": uuid5(self._owner(memory)["user_id"], f"fixture:card:{card['domain']}:{card['title']}")})
            entry["members"].append(memory)
        for entry in cards.values():
            # 同日要點以 fixture ID 排序；DB ID 由每次隨機 owner 推導，拿來排序會讓 document 文字逐次漂移。
            entry["members"].sort(key=lambda item: (item.get("observed_days", -30), item["id"]))
            current = [item for item in entry["members"] if item["status"] in {"active", "conflict"}
                       and (item.get("expires_in_days") is None or item["expires_in_days"] > 0)]
            # 錯誤 contract 的要點不進卡片 document，避免經卡片向量繞過 contract 過濾。
            points = [item["text"] for item in current if not item.get("wrong_contract")]
            entry["document"] = card_document(entry["domain"], entry["title"], None, points) if points else None
            entry["live"] = any(item.get("vector_source") == "live" for item in current)
        return cards

    @staticmethod
    def _source_key(memory: dict) -> str | None:
        if "source_text" not in memory:
            return None
        return "source:" + (memory.get("source_key") or memory["id"])

    async def _derived_vectors(self, memories: list[dict], cards: dict) -> None:
        """原話與卡片向量也走固定 document cache；文字或 contract 漂移一律 fixture_invalid。"""
        entries = {}
        for memory in memories:
            key = self._source_key(memory)
            if key is not None:
                entries[key] = {"id": key, "text": memory["source_text"]}
        for card in cards.values():
            if card["document"] is not None:
                entries[card["key"]] = {"id": card["key"], "text": card["document"],
                                        **({"vector_source": "live"} if card["live"] else {})}
        if self._vectors is None:
            self._vectors = {}
        missing = [entry for key, entry in entries.items()
                   if key not in self._vectors or entry.get("vector_source") == "live"]
        if not missing:
            return
        settings = self.settings or load_memory_settings()
        cache = DocumentVectors(self.vector_path, settings, self.client_factory)
        self._vectors.update(await cache.load(missing, record_corpus=False))
        self.settings = settings
        for name in ("calls", "cache_hits", "duration_sec"):
            self.embedding_cost[name] = self.embedding_cost.get(name, 0) + cache.cost[name]

    def _seed_memories(self, memories: list[dict], reference: datetime, scenario: str, cards: dict) -> None:
        if not memories:
            return
        schema = self.store.schema
        with self.store.engine.begin() as connection:
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", public')
            occurred_at = {memory["id"]: min(reference + timedelta(days=memory.get("observed_days", -30)),
                                             datetime.now(reference.tzinfo)) for memory in memories}
            for card in cards.values():
                owner = self._owner(card["members"][0])
                connection.execute(text("""INSERT INTO memory_scope_state (user_id,character_id)
                    VALUES (:user_id,:character_id) ON CONFLICT DO NOTHING"""), owner)
                vector = self._vectors.get(card["key"]) if card["document"] is not None else None
                connection.execute(text("""INSERT INTO memory_cards
                    (id,user_id,character_id,domain,title,title_key,summary_stale,embedding,embedding_contract)
                    VALUES (:id,:user_id,:character_id,:domain,:title,:title_key,true,
                     CAST(:embedding AS vector),:contract)
                    ON CONFLICT (user_id,character_id,domain,title_key) DO UPDATE SET
                     embedding = EXCLUDED.embedding, embedding_contract = EXCLUDED.embedding_contract,
                     summary = NULL, summary_fingerprint = NULL, summary_stale = true, updated_at = now()"""),
                    {**owner, "id": card["id"], "domain": card["domain"], "title": card["title"],
                     "title_key": normalize_key(card["title"]),
                     "embedding": "[" + ",".join(str(value) for value in vector) + "]" if vector else None,
                     "contract": self.settings.embedding_contract if vector else None})
            card_ids = {member["id"]: card["id"] for card in cards.values() for member in card["members"]}
            for memory in memories:
                owner = self._owner(memory)
                identifier = UUID(self.memory_ids[memory["id"]])
                previous = memory.get("supersedes_id")
                group_id = UUID(self.memory_ids[previous]) if previous else identifier
                occurred = occurred_at[memory["id"]]
                def date_offset(key, default=None):
                    offset = memory.get(key, default)
                    return reference + timedelta(days=offset) if offset is not None else None
                params = {**owner, "id": identifier, "group_id": group_id,
                    "memory_type": memory["memory_type"], "canonical": memory["text"],
                    "subject": memory["subject_key"], "status": memory["status"],
                    "keywords": _memory_keywords(memory["text"], memory["subject_key"], memory.get("search_terms")),
                    "retention": memory.get("retention_class", "normal"),
                    "embedding": "[" + ",".join(str(value) for value in self._vectors[memory["id"]]) + "]",
                    "model": self.settings.embedding_model,
                    "contract": "fixture_wrong_contract" if memory.get("wrong_contract") else self.settings.embedding_contract,
                    "occurred": occurred, "valid_from": date_offset("valid_from_days", -60),
                    "valid_to": date_offset("valid_to_days"), "expires": date_offset("expires_in_days"),
                    "card_id": card_ids.get(memory["id"])}
                connection.execute(text("""INSERT INTO memory_scope_state (user_id,character_id)
                    VALUES (:user_id,:character_id) ON CONFLICT DO NOTHING"""), owner)
                connection.execute(text("""INSERT INTO memory_items
                    (id,user_id,character_id,group_id,memory_type,canonical_text,subject_key,keywords,status,
                     importance,confidence,retention_class,embedding,embedding_model,embedding_contract,
                     observed_at,valid_from,valid_to,expires_at,created_at,updated_at,card_id)
                    VALUES (:id,:user_id,:character_id,:group_id,:memory_type,:canonical,:subject,:keywords,:status,
                     .5,1,:retention,CAST(:embedding AS vector),:model,:contract,:occurred,:valid_from,:valid_to,
                     :expires,:occurred,:occurred,:card_id)"""), params)
                # source_key 表示多筆要點共用同一句原話；同情境內只建立一列 source。
                shared = memory.get("source_key")
                source_id = (uuid5(owner["user_id"], f"fixture:{scenario}:source:{shared}") if shared
                             else uuid5(identifier, "fixture_user_source"))
                # 原話向量只來自使用者原話；與要點同文字時沿用要點向量。
                source_vector = self._vectors[self._source_key(memory) or memory["id"]]
                source = {**owner, "source_id": source_id, "conversation": conversation_id(f"fixture:{scenario}"),
                          "message_id": message_id(f"fixture:{scenario}", shared or memory["id"]),
                          "canonical": memory.get("source_text", memory["text"]), "occurred": occurred,
                          "embedding": "[" + ",".join(str(value) for value in source_vector) + "]",
                          "contract": params["contract"], "mood": memory.get("mood", "calm")}
                connection.execute(text("""INSERT INTO memory_sources
                    (id,user_id,character_id,conversation_id,message_id,speaker,raw_text,occurred_at,
                     embedding,embedding_contract,mood)
                    VALUES (:source_id,:user_id,:character_id,:conversation,:message_id,'user',:canonical,:occurred,
                     CAST(:embedding AS vector),:contract,:mood)
                    ON CONFLICT DO NOTHING"""), source)
                connection.execute(text("""INSERT INTO memory_evidence
                    (user_id,character_id,memory_id,source_id,kind)
                    VALUES (:user_id,:character_id,:memory_id,:source_id,'origin')"""),
                    {**owner, "memory_id": identifier, "source_id": source_id})
                connection.execute(text("""INSERT INTO memory_audit
                    (id,user_id,character_id,operation_key,action,target_id,source_event_id,reason_class,decision,created_at)
                    VALUES (:audit_id,:user_id,:character_id,:operation_key,'FIXTURE_CREATE',:target,:source,
                     'synthetic_fixture',CAST(:decision AS jsonb),:occurred)"""),
                    {**owner, "audit_id": uuid5(identifier, "fixture_audit"), "operation_key": f"fixture:{scenario}:{memory['id']}",
                     "target": identifier, "source": source_id, "occurred": occurred,
                     "decision": json.dumps({"source": "synthetic_fixture", "logical_id": memory["id"]})})
            self._sync_cards(connection, cards)
            for memory in memories:
                if memory.get("supersedes_id"):
                    connection.execute(text("""INSERT INTO memory_relations (user_id,character_id,from_id,to_id,kind)
                        VALUES (:user_id,:character_id,:current,:previous,'supersedes')"""),
                        {**self.store.owner, "current": UUID(self.memory_ids[memory["id"]]),
                         "previous": UUID(self.memory_ids[memory["supersedes_id"]])})

    def _sync_cards(self, connection, cards: dict) -> None:
        """依 DB 實際要點重算有效數與心情（最常見、同數取最近），與 runtime 的卡片同步相同。"""
        for card in cards.values():
            owner = {**self._owner(card["members"][0]), "id": card["id"]}
            active = connection.execute(text("""SELECT count(*) FROM memory_items
                WHERE card_id = :id AND user_id = :user_id AND character_id = :character_id
                AND status IN ('active', 'conflict') AND (expires_at IS NULL OR expires_at > now())"""),
                owner).scalar_one()
            moods = connection.execute(text("""SELECT DISTINCT source.id, source.mood, source.occurred_at
                FROM memory_items AS item JOIN memory_evidence AS evidence
                ON (evidence.memory_id, evidence.user_id, evidence.character_id) = (item.id, item.user_id, item.character_id)
                JOIN memory_sources AS source
                ON (source.id, source.user_id, source.character_id) = (evidence.source_id, evidence.user_id, evidence.character_id)
                WHERE item.card_id = :id AND item.user_id = :user_id AND item.character_id = :character_id
                AND source.mood IS NOT NULL"""), owner).all()
            connection.execute(text("""UPDATE memory_cards SET active_count = :active, mood = :mood
                WHERE id = :id AND user_id = :user_id AND character_id = :character_id"""),
                {**owner, "active": active, "mood": aggregate_mood([(mood, moment) for _, mood, moment in moods])})

    def _seed_session(self, session_id: str, context: dict, reference: datetime) -> None:
        history, summary = context.get("history", []), context.get("summary", "")
        if not history and not summary:
            return
        if len(summary) > 4000 or any(item.get("role") not in {"user", "assistant"}
                                     or not item.get("content") for item in history):
            raise ValueError("fixture_invalid: session content")
        owner = self.store.owner
        with self.store.engine.begin() as connection:
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{self.store.schema}", public')
            connection.execute(text("""INSERT INTO chat_sessions
                (id,user_id,character_id,status,summary,summary_through_sequence,next_message_sequence,created_at)
                VALUES (:id,:user_id,:character_id,'closed',:summary,-1,:next_sequence,:occurred)"""),
                {**owner, "id": session_id, "summary": summary, "next_sequence": len(history), "occurred": reference})
            for index, item in enumerate(history):
                connection.execute(text("""INSERT INTO chat_messages
                    (session_id,user_id,character_id,generation,sequence,turn_id,role,content,turn_state,created_at)
                    VALUES (:session,:user_id,:character_id,0,:sequence,:turn,:role,:content,:state,:occurred)"""),
                    {**owner, "session": session_id, "sequence": index, "turn": f"fixture_{index // 2}",
                     "role": item["role"], "content": item["content"],
                     "state": "completed" if item["role"] == "user" else None,
                     "occurred": reference + timedelta(seconds=index - len(history))})

    def other_owner_state(self) -> dict:
        """讓 runner 對照外來 owner 的完整長期狀態，不只比較筆數。"""
        state = {}
        with self.store.engine.connect() as connection:
            for table in ("memory_cards", "memory_items", "memory_sources", "memory_evidence", "memory_relations",
                          "memory_jobs", "memory_audit", "memory_scope_state", "memory_forget_barriers"):
                rows = connection.execute(text(f'SELECT * FROM "{self.store.schema}".{table} '
                    'WHERE user_id <> :user_id OR character_id <> :character_id'), self.store.owner).mappings()
                values = json.loads(json.dumps([dict(row) for row in rows], default=str))
                state[table] = sorted(values, key=lambda row: json.dumps(row, sort_keys=True))
        return state
