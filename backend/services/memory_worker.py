"""持久化的單一記憶工作；外部呼叫不持有 transaction。"""
import asyncio
import re
import time
from datetime import datetime, timezone
from uuid import UUID
from openai import APITimeoutError

from services.memory_events import publish_memory_event
from core.prompt_logger import trace, trace_event
from services.memory_agent_client import CUMULATIVE_DIAGNOSTIC_FIELDS

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
                return await self.repository.finish(job, status, diagnostic=diagnostic, **kwargs)

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
                related = await candidates(query_text, 6)
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
                diagnostic["phase"] = "commit"
                elapsed()
                committed = await self.manager.apply(job, decisions, set(targets), embeddings,
                    context_ids, diagnostic, target_snapshots=targets)
                if committed:
                    publish_memory_event("memory_committed", str(job["id"]), str(job["message_id"]), status="done")
                else:
                    raise CommitRejectedError("Memory commit rejected")
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
