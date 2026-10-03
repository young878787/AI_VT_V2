"""持久化的接收／圖書工作；外部呼叫不持有 transaction。"""
import asyncio
import time

from services.memory_events import publish_memory_event
from core.prompt_logger import trace, trace_event


class MemoryWorker:
    def __init__(self, repository, embedding, llm, manager, intake):
        self.repository = repository
        self.embedding = embedding
        self.llm = llm
        self.manager = manager
        self.intake = intake
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
        job = await self.repository.claim()
        if job is None:
            return False
        started = time.monotonic()
        trace_token = trace_event.set(job["id"])
        outcome = "completed"
        error_detail = None
        try:
            async with asyncio.timeout(100):
                query_text = job["source_text"] if job["stage"] == "intake" else "\n".join(
                    candidate["canonical_text"] for candidate in job["reviewed_candidates"])
                query = await self.embedding.embed(query_text, purpose="memory_match_query",
                                                   event_id=job["id"], stage=job["stage"], job_attempt=job["attempts"])
                if job["stage"] == "intake":
                    held = await self.repository.related_context(job, query)
                    sources = await self.repository.intake_sources(job, held)
                    result, diagnostic = await self.intake.review(job, sources, held)
                    context_vector = None
                    if result["route"] == "needs_context":
                        try:
                            context_vector = await self.embedding.embed(job["source_text"], purpose="context_document",
                                                                        event_id=job["id"], stage="intake")
                        except Exception:
                            pass  # 已持久化來源；確定性 maintenance 補算。
                    await self.repository.complete_intake(job, result, sources, held, diagnostic, context_vector)
                    self.wake()
                else:
                    related = await self.repository.related_items(
                        query_text, query, mode="management",
                        subject_keys=tuple(candidate["subject_key"] for candidate in job["reviewed_candidates"]
                                           if candidate.get("subject_key")),
                    )
                    evidence = {"existing": await self.repository.memory_evidence(related),
                                "reviewed_sources": await self.repository.intake_sources(job, [])}
                    decisions, diagnostic = await self.llm.decide(job, related, evidence)
                    if isinstance(decisions, dict):
                        await self.repository.return_for_review(job, decisions["missing_context"], diagnostic)
                        return True
                    await self.repository.mark_pending_targets(job, decisions, {row["id"] for row in related})
                    embeddings = {}
                    for index, decision in enumerate(decisions):
                        if decision["action"] in {"CREATE", "SUPERSEDE", "CONTRADICT"}:
                            embeddings[index] = await self.embedding.embed(
                                decision["canonical_text"], purpose="memory_document", event_id=job["id"],
                                stage="librarian", job_attempt=job["attempts"], decision_index=index)
                    committed = await self.manager.apply(job, decisions, {row["id"] for row in related}, embeddings,
                                                         tuple(job["context_job_ids"]), diagnostic)
                    if committed:
                        publish_memory_event("memory_committed", str(job["id"]), str(job["message_id"]),
                                             stage="librarian", status="done")
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome = type(exc).__name__
            error_detail = str(exc) if isinstance(exc, ValueError) else None
            attempts = job[f"{job['stage']}_attempts"]
            await self.repository.finish(job, "failed" if attempts >= 3 else "retry", error=type(exc).__name__)
        finally:
            trace("memory_stage", {"role": job["stage"], "attempt": job["attempts"],
                "duration_sec": round(time.monotonic() - started, 4), "outcome": outcome,
                "validation_error": error_detail}, job["id"])
            trace_event.reset(trace_token)
        return True

    async def run(self):
        maintenance_at = 0
        while True:
            try:
                self._wake.clear()
                worked = await self.process_one()
                if time.monotonic() >= maintenance_at:
                    await self.repository.expire_context()
                    await self.repository.expire_temporary()
                    for context in await self.repository.unembedded_context():
                        try:
                            vector = await self.embedding.embed(context["source_text"], purpose="context_document",
                                                                event_id=context["id"], stage="maintenance")
                            await self.repository.save_context_embedding(context, vector)
                        except Exception:
                            break
                    maintenance_at = time.monotonic() + 300
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[Memory] worker error: {type(exc).__name__}")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                except TimeoutError:
                    pass
