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

from domain.memory_embedding import normalize_embedding
from domain.memory_scope import conversation_id, message_id
from domain.memory_settings import load_memory_settings
from services.memory_db_manager import _memory_keywords

ROOT = Path(__file__).resolve().parents[2]
VECTOR_PATH = ROOT / "backend/tools/ai_test_cases/memory_vectors.json"


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def vector_contract(settings) -> dict:
    return {"model": settings.embedding_model, "dimension": settings.embedding_dimension,
            "query_prefix": settings.embedding_query_prefix,
            "document_prefix": settings.embedding_document_prefix, "normalization": "l2-v1",
            "sha256": settings.embedding_contract}


class DocumentVectors:
    """僅 cache 初始 corpus document；文字或契約漂移一律停止載入。"""

    def __init__(self, path: Path, settings, client_factory=None):
        self.path, self.settings = Path(path), settings
        self.client_factory = client_factory
        self.cost = {"calls": 0, "cache_hits": 0, "duration_sec": 0.0,
                     "input_tokens": None, "service_version": "unverified"}

    async def load(self, memories: list[dict]) -> dict[str, list[float]]:
        corpus_hash = fingerprint(sorted(({"id": item["id"], "text": item["text"]} for item in memories),
                                         key=lambda item: item["id"]))
        contract = vector_contract(self.settings)
        data = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists()
                else {"version": 1, "contract": None, "corpus_sha256": None, "documents": {}})
        documents = data.get("documents")
        if data.get("version") != 1 or not isinstance(documents, dict):
            raise ValueError("fixture_invalid: vector cache schema")
        if documents and (data.get("contract") != contract or data.get("corpus_sha256") != corpus_hash):
            raise ValueError("fixture_invalid: vector corpus or contract changed")
        result, missing = {}, []
        for memory in memories:
            key, value = memory["id"], documents.get(memory["id"])
            digest = hashlib.sha256(memory["text"].encode()).hexdigest()
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
            from infrastructure.memory_embedding_client import MemoryEmbeddingClient
            client = (self.client_factory or MemoryEmbeddingClient)(self.settings)
            started = time.monotonic()
            try:
                for memory, digest in missing:
                    self.cost["calls"] += 1
                    vector = await client.embed(memory["text"], query=False)
                    vector = normalize_embedding(vector, self.settings.embedding_dimension)
                    result[memory["id"]] = vector
                    documents[memory["id"]] = {"text_sha256": digest, "vector": vector}
            finally:
                self.cost["duration_sec"] += round(time.monotonic() - started, 4)
                await client.client.close()
            data.update(contract=contract, corpus_sha256=corpus_hash, documents=documents,
                        serving_model=self.settings.embedding_serving_model, service_version="unverified")
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
            await asyncio.to_thread(self._seed_memories, selected, reference, scenario)
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
                        "document_vectors": "fixed_document_cache", "query_vectors": "live",
                        "embedding_cost": dict(self.embedding_cost),
                        "visual_evidence_unavailable": suite == "integration"}}
        if self.manifest_path:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.manifest_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.manifest_path)
        return manifest

    def _seed_memories(self, memories: list[dict], reference: datetime, scenario: str) -> None:
        if not memories:
            return
        schema, base = self.store.schema, self.store.owner
        with self.store.engine.begin() as connection:
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", public')
            for memory in memories:
                owner = dict(base)
                if memory.get("owner", "self") == "other":
                    owner["user_id"] = uuid5(base["user_id"], "fixture_other_owner")
                identifier = UUID(self.memory_ids[memory["id"]])
                previous = memory.get("supersedes_id")
                group_id = UUID(self.memory_ids[previous]) if previous else identifier
                occurred = min(reference + timedelta(days=memory.get("observed_days", -30)),
                               datetime.now(reference.tzinfo))
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
                    "valid_to": date_offset("valid_to_days"), "expires": date_offset("expires_in_days")}
                connection.execute(text("""INSERT INTO memory_scope_state (user_id,character_id)
                    VALUES (:user_id,:character_id) ON CONFLICT DO NOTHING"""), owner)
                connection.execute(text("""INSERT INTO memory_items
                    (id,user_id,character_id,group_id,memory_type,canonical_text,subject_key,keywords,status,
                     importance,confidence,retention_class,embedding,embedding_model,embedding_contract,
                     observed_at,valid_from,valid_to,expires_at,created_at,updated_at)
                    VALUES (:id,:user_id,:character_id,:group_id,:memory_type,:canonical,:subject,:keywords,:status,
                     .5,1,:retention,CAST(:embedding AS vector),:model,:contract,:occurred,:valid_from,:valid_to,
                     :expires,:occurred,:occurred)"""), params)
                source_id = uuid5(identifier, "fixture_user_source")
                source = {**owner, "source_id": source_id, "conversation": conversation_id(f"fixture:{scenario}"),
                          "message_id": message_id(f"fixture:{scenario}", memory["id"]),
                          "canonical": memory.get("source_text", memory["text"]), "occurred": occurred}
                connection.execute(text("""INSERT INTO memory_sources
                    (id,user_id,character_id,conversation_id,message_id,speaker,raw_text,occurred_at)
                    VALUES (:source_id,:user_id,:character_id,:conversation,:message_id,'user',:canonical,:occurred)"""), source)
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
            for memory in memories:
                if memory.get("supersedes_id"):
                    connection.execute(text("""INSERT INTO memory_relations (user_id,character_id,from_id,to_id,kind)
                        VALUES (:user_id,:character_id,:current,:previous,'supersedes')"""),
                        {**base, "current": UUID(self.memory_ids[memory["id"]]),
                         "previous": UUID(self.memory_ids[memory["supersedes_id"]])})

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
            for table in ("memory_items", "memory_sources", "memory_evidence", "memory_relations", "memory_jobs",
                          "memory_audit", "memory_scope_state", "memory_forget_barriers"):
                rows = connection.execute(text(f'SELECT * FROM "{self.store.schema}".{table} '
                    'WHERE user_id <> :user_id OR character_id <> :character_id'), self.store.owner).mappings()
                values = json.loads(json.dumps([dict(row) for row in rows], default=str))
                state[table] = sorted(values, key=lambda row: json.dumps(row, sort_keys=True))
        return state
