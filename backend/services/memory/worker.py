"""持久化的單一記憶工作；外部呼叫不持有 transaction。"""
import asyncio
import re
import time
from datetime import datetime, timezone
from uuid import UUID
from openai import APITimeoutError

from domain.memory.cards import card_document
from services.memory.events import publish_memory_event
from core.prompt_logger import trace, trace_event
from services.memory.agent_client import CUMULATIVE_DIAGNOSTIC_FIELDS

ATTEMPT_TIMEOUT_SEC = 100
DB_TIMEOUT_SEC = 10


class CommitRejectedError(RuntimeError):
    pass


class MemoryWorker:
    def __init__(self, repository, embedding, llm, manager):
        self.repository = repository
        self.embedding = embedding
        self.llm = llm
        self.manager = manager
        self._task = None
        self._wake = asyncio.Event()

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    def wake(self):
        self._wake.set()

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def process_one(self):
        async with asyncio.timeout(DB_TIMEOUT_SEC):
            job = await self.repository.claim()
        if job is None:
            return False
        started = time.monotonic()
        token = trace_event.set(job["id"])
        previous = job.get("agent_diagnostics") or {}
        diagnostic = {key: previous.get(key, 0) for key in CUMULATIVE_DIAGNOSTIC_FIELDS}
        diagnostic.update(phase="sources", candidate_count=0, proposal_count=0,
                          recovered_lease_age_sec=job.get("recovered_lease_age_sec"))
        if previous.get("last_failure"):
            diagnostic["last_failure"] = previous["last_failure"]
        if job.get("created_at"):
            diagnostic["queue_age_sec"] = max(0, (datetime.now(timezone.utc) - job["created_at"]).total_seconds())
        cache = {}
        degraded = False
        processing_budget = ATTEMPT_TIMEOUT_SEC
        if job.get("lease_until"):
            remaining = (job["lease_until"] - datetime.now(timezone.utc)).total_seconds()
            processing_budget = max(0, min(processing_budget, remaining - DB_TIMEOUT_SEC))
        diagnostic["attempt_budget_sec"] = processing_budget
        def elapsed():
            diagnostic["attempt_processing_sec"] = round(time.monotonic() - started, 4)
            diagnostic["attempt_metrics"] = {key: diagnostic[key] - previous.get(key, 0)
                                             for key in CUMULATIVE_DIAGNOSTIC_FIELDS}

        async def finish(status, **kwargs):
            elapsed()
            async with asyncio.timeout(DB_TIMEOUT_SEC):
                changed = await self.repository.finish(job, status, diagnostic=diagnostic, **kwargs)
                if changed and status in {"ignored", "buffered", "failed"}:
                    publish_memory_event(
                        "memory_job_finished", str(job["id"]), str(job["message_id"]), status=status,
                    )
                return changed

        async def embed(text, purpose):
            key = (getattr(getattr(self.embedding, "settings", None), "embedding_contract", None),
                   purpose.endswith("query"), text)
            if key not in cache:
                diagnostic["embedding_calls"] += 1
                cache[key] = await self.embedding.embed(text, purpose=purpose, event_id=job["id"],
                                                       stage="memory", job_attempt=job["attempts"])
            return cache[key]
        try:
            if processing_budget <= 0:
                raise TimeoutError("Memory job lease has no processing budget")
            async with asyncio.timeout(processing_budget):
                diagnostic["queue"] = await self.repository.queue_health()
                if not job["source_text"]:
                    raise ValueError("缺少當輪 user 來源")
                if len(job["source_text"]) > 4000:
                    await finish("buffered", missing_context="輸入超過保存窗口，請分段補充")
                    return True
                held = await self.repository.context_jobs(job)
                sources = await self.repository.job_sources(job, held)
                job["held_inputs"] = [{"id": str(row["id"]), "missing_context": row["missing_context"]} for row in held]
                query_text = job["source_text"]
                if re.search(r"這件事|那個|這個|剛才|(?:整體|整体)(?:設計|设计|架構|架构|規劃|规划)|overall (?:design|plan|architecture)|that|\bit\b", query_text, re.I):
                    prior = [source["raw_text"] for source in sources if str(source["id"]) != str(job["id"])]
                    query_text = "\n".join([*prior[-2:], query_text])[:4000]
                async def candidates(text, limit, exclude=()):
                    nonlocal degraded
                    try:
                        vector = await embed(text, "memory_match_query")
                    except Exception:
                        degraded = True
                        vector = None
                    return await self.repository.agent_candidates(text, vector, limit, exclude)
                diagnostic["phase"] = "retrieval"
                organize_started = time.monotonic()
                related = await candidates(query_text, 6)
                # 候選卡與候選要點共用同一個已快取的查詢向量；向量失敗時候選卡為空並降級。
                card_vector = None if degraded else await embed(query_text, "memory_match_query")
                job["candidate_cards"] = await self.repository.agent_cards(card_vector)
                job["retrieval_degraded"] = degraded
                async def search(text, exclude):
                    return await candidates(text, 4, exclude)
                async def read(memory_ids, source_ids):
                    fresh_sources = await self.repository.job_sources(job, held)
                    chosen = [source for source in fresh_sources if UUID(str(source["id"])) in source_ids]
                    if len(chosen) != len(source_ids):
                        raise ValueError("來源已失效")
                    memories = await self.repository.read_memories(memory_ids, versions=True)
                    if not memory_ids <= {UUID(str(row["id"])) for row in memories}:
                        raise ValueError("target 已失效")
                    return {"memories": memories, "sources": chosen,
                            "evidence": await self.repository.memory_evidence(memories)}
                diagnostic["phase"] = "agent"
                result = await self.llm.decide(job, sources, related, search, read, diagnostic)
                diagnostic["timing"] = {"organize_ms": round((time.monotonic() - organize_started) * 1000)}
                if result["outcome"] != "complete":
                    await finish("buffered" if result["outcome"] == "needs_context" else "ignored",
                        missing_context=result["reason"] if result["outcome"] == "needs_context" else None)
                    return True
                if degraded:
                    raise RuntimeError("Embedding retrieval degraded; mutation deferred")
                decisions, targets = result["decisions"], result["targets"]
                used = {UUID(value) for decision in decisions for value in decision["source_ids"]}
                context_ids = tuple(row["id"] for row in held if used.intersection(row["source_ids"]))
                diagnostic["phase"] = "document_embedding"
                await self.repository.mark_pending_targets(job, decisions, set(targets))
                embeddings = {}
                for index, decision in enumerate(decisions):
                    if decision["action"] in {"CREATE", "SUPERSEDE", "CONTRADICT"}:
                        embeddings[index] = await embed(decision["canonical_text"], "memory_document")
                # 只向量化使用者原話（證據）；露西亞的回覆從不進來源向量。
                source_embeddings = {}
                if not any(decision["action"] == "FORGET" for decision in decisions):
                    texts = {UUID(str(source["id"])): source["raw_text"] for source in sources
                             if source["speaker"] == "user"}
                    for source_id in sorted(used & texts.keys(), key=str):
                        source_embeddings[source_id] = await embed(texts[source_id], "memory_document")
                diagnostic["phase"] = "commit"
                elapsed()
                committed = await self.manager.apply(job, decisions, set(targets), embeddings,
                    context_ids, diagnostic, target_snapshots=targets, source_embeddings=source_embeddings)
                if committed:
                    publish_memory_event("memory_committed", str(job["id"]), str(job["message_id"]), status="done")
                else:
                    raise CommitRejectedError("Memory commit rejected")
            if diagnostic.get("cards"):
                # 已提交後的整理不得讓工作改判重試；逾時或錯誤只留下 stale 卡片。
                diagnostic["phase"] = "card_refresh"
                try:
                    async with asyncio.timeout(ATTEMPT_TIMEOUT_SEC):
                        await self.refresh_cards(job, diagnostic)
                except Exception as error:
                    diagnostic["card_refresh_error"] = type(error).__name__
        except asyncio.CancelledError:
            raise
        except Exception as error:
            diagnostic["last_failure"] = {"attempt": job["attempts"], "phase": diagnostic["phase"],
                                           "error": type(error).__name__}
            diagnostic["failures"] += 1
            diagnostic["timeouts"] += int(isinstance(error, (TimeoutError, APITimeoutError)))
            diagnostic["retry_exhausted"] = job["attempts"] >= 3
            await finish("failed" if job["attempts"] >= 3 else "retry",
                error=type(error).__name__, validation_error=str(error) if isinstance(error, ValueError) else None,
            )
        finally:
            elapsed()
            trace("memory_job", {"attempt": job["attempts"], "duration_sec": round(time.monotonic() - started, 4),
                                 **diagnostic}, job["id"])
            trace_event.reset(token)
        return True

    async def refresh_cards(self, job, diagnostic):
        """commit 後重建變動卡片的概要與卡片向量；失敗只留下 stale，不影響已提交的記憶。"""
        cards = diagnostic.get("cards") or {}
        timing = diagnostic.setdefault("timing", {})
        generate_ms, edit_ms, refreshed = 0, 0, []
        for card_id in sorted(set(cards.get("updated", []))):
            started = time.monotonic()
            try:
                state = await self.repository.card_refresh_state(UUID(card_id))
                if state is None or not state["points"]:
                    continue
                summary, summary_diagnostic = None, {key: 0 for key in CUMULATIVE_DIAGNOSTIC_FIELDS}
                fresh = not state["summary_stale"] and state["summary_fingerprint"] == state["fingerprint"]
                if not fresh:
                    summary = await self.llm.summarize_card(state, summary_diagnostic)
                    diagnostic["calls"] += summary_diagnostic["calls"]
                    diagnostic["latency_ms"] += summary_diagnostic["latency_ms"]
                    generate_ms += round((time.monotonic() - started) * 1000)
                edit_started = time.monotonic()
                vector = None
                if summary is not None or not state["embedding_current"]:
                    vector = await self.embedding.embed(
                        card_document(state["domain"], state["title"], summary or (state["summary"] if fresh else None),
                                      state["points"]),
                        purpose="memory_document", event_id=job["id"], stage="memory", job_attempt=job["attempts"])
                    diagnostic["embedding_calls"] += 1
                saved = await self.repository.save_card_refresh(
                    UUID(card_id), state["generation"], state["fingerprint"], summary, vector)
                edit_ms += round((time.monotonic() - edit_started) * 1000)
                refreshed.append({"id": card_id, "summary": summary is not None, "saved": saved})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                refreshed.append({"id": card_id, "error": type(error).__name__})
        timing.update(generate_ms=generate_ms, edit_ms=edit_ms)
        diagnostic["card_refresh"] = refreshed
        trace("memory_card", {"cards": cards, "refresh": refreshed, "timing": timing}, job["id"])
        print(f"[Memory] cards: created={len(cards.get('created', []))} updated={len(cards.get('updated', []))} "
              f"deleted={len(cards.get('deleted', []))} organize_ms={timing.get('organize_ms')} "
              f"generate_ms={generate_ms} edit_ms={edit_ms}")

    async def run(self):
        maintenance_at = 0
        while True:
            try:
                self._wake.clear()
                worked = await self.process_one()
                if time.monotonic() >= maintenance_at:
                    async with asyncio.timeout(DB_TIMEOUT_SEC):
                        await self.repository.expire_context()
                        await self.repository.expire_temporary()
                        health = await self.repository.queue_health()
                        print(f"[Memory] queue: {health}")
                    maintenance_at = time.monotonic() + 300
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[Memory] worker error: {type(error).__name__}")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                except TimeoutError:
                    pass
