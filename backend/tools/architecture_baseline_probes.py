"""V1–V10 實際函式隔離重現；只記觀察，保留缺口與修正的區別。"""

import asyncio
import json
import queue
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4


def record(identifier, reproduced, observed):
    return {"id": identifier, "status": "reproduced" if reproduced else "not_reproduced",
            "observed": observed}


def policy_probe():
    from domain.memory_routing import instruction_policy
    texts = ("請幫我寫一句「忘記我的地址」的台詞", "忘記我生日的是我朋友",
             "不要忘記我喜歡茶", "請忘記我的地址")
    observed = [instruction_policy(text) for text in texts]
    return record("V1", observed[:2] == ["forget", "forget"],
                  {"cases": [{"input": text, "policy": policy} for text, policy in zip(texts, observed)]})


async def source_probe():
    from domain.memory_routing import instruction_policy, MemoryRouting
    from domain.memory_scope import MemoryScope
    from infrastructure.memory_repository import MemoryRepository

    class Connection:
        def __init__(self):
            self.insertions = []

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, statement, params=None):
            statement = str(statement)
            row = None
            if "SELECT conversation_id" in statement:
                row = (uuid4(), datetime(2026, 10, 4, tzinfo=timezone.utc), 0)
            if "INSERT INTO memory_sources" in statement:
                self.insertions.append(params[5])
            return SimpleNamespace(
                rowcount=1,
                fetchone=AsyncMock(return_value=row),
                fetchall=AsyncMock(return_value=[]),
            )

    connection = Connection()

    @asynccontextmanager
    async def connect():
        yield connection

    text = "我在整理測試用的工作紀錄。" * 60 + "不要記住這件事"
    repository = MemoryRepository(SimpleNamespace(connection=connect), MemoryScope(uuid4(), uuid4(), "baseline"))
    await repository.route(uuid4(), MemoryRouting(None, confidence=.9), "我今天完成另一個工作項目。",
                           [{"role": "user", "content": text}])
    observed = {"full_policy": instruction_policy(text), "trimmed_policy": instruction_policy(text[:500]),
                "source_insert_count": len(connection.insertions),
                "history_inserted": text[:500] in connection.insertions}
    return record("V2", observed["full_policy"] == "no_store" and observed["history_inserted"], observed)


def response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


async def summary_probe():
    from services.chat_service import compress_context
    captured = []

    async def summarize(**kwargs):
        captured.append(kwargs["messages"])
        return response("第一份摘要保留最早事實" if len(captured) == 1 else "第二份摘要")

    messages = [{"role": "user", "content": "最早事實"}] + [
        {"role": "assistant", "content": f"回覆 {i}"} for i in range(21)]
    with patch("services.chat_service.chat_create_with_fallback", side_effect=summarize), \
            patch("services.chat_service.save_session_summary"):
        first = await compress_context(messages, SimpleNamespace(send_json=AsyncMock()), "baseline")
        first.extend({"role": "user", "content": f"新一輪 {i}"} for i in range(21))
        second = await compress_context(first, SimpleNamespace(send_json=AsyncMock()), "baseline")
    supplied = json.dumps(captured[-1], ensure_ascii=False)
    observed = {"previous_summary_supplied": "第一份摘要" in supplied,
                "earliest_fact_supplied": "最早事實" in supplied,
                "system_message_count": sum(m["role"] == "system" for m in second)}
    return record("V3", not observed["previous_summary_supplied"] and not observed["earliest_fact_supplied"], observed)


async def compression_race_probe():
    from services.chat_service import compress_context
    started, release = asyncio.Event(), asyncio.Event()

    async def summarize(**kwargs):
        started.set()
        await release.wait()
        return response("摘要")

    messages = [{"role": "user", "content": str(i)} for i in range(22)]
    tail = [{"role": "user", "content": "併發新問題"}, {"role": "assistant", "content": "併發新回答"}]
    with patch("services.chat_service.chat_create_with_fallback", side_effect=summarize), \
            patch("services.chat_service.save_session_summary"):
        task = asyncio.create_task(compress_context(messages, SimpleNamespace(send_json=AsyncMock()), "baseline"))
        try:
            await started.wait()
            messages.extend(tail)
            release.set()
            result = await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    observed = {"original_keeps_tail": all(m in messages for m in tail),
                "returned_keeps_tail": all(m in result for m in tail)}
    return record("V4", observed["original_keeps_tail"] and not observed["returned_keeps_tail"], observed)


async def projection_probe(future=False):
    from domain.memory_decisions import normalize_proposal
    from services.memory_retriever import MemoryRetriever
    source_id, memory_id = uuid4(), uuid4()
    text = "請詳細回答" if future else "測" * 801
    proposal = normalize_proposal({
        "action": "CREATE", "source_ids": [str(source_id)], "reason": "明確使用者來源",
        "canonical_text": text, "memory_type": "profile" if future else "project",
        "importance": .7, "confidence": .9, "search_terms": ["測試"],
    }, set(), {source_id}, "請記住測試內容", current_source_id=source_id)
    row = {"id": memory_id, "group_id": memory_id, "canonical_text": proposal["canonical_text"],
           "memory_type": proposal["memory_type"], "status": "active", "similarity": .9,
           "exact_match": True, "subject_key": "profile.communication_style" if future else "project",
           "valid_from": "2099-01-01T00:00:00+00:00", "valid_to": "2099-02-01T00:00:00+00:00"}
    repository = SimpleNamespace(related_items=AsyncMock(return_value=[row]))
    embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0] + [0.0] * 1023))
    with patch("services.memory_retriever.trace") as trace:
        profile, memory = await MemoryRetriever(repository, embedding).retrieve("未來的回覆風格" if future else "測試配置")
    if future:
        observed = {"profile": profile, "memory_empty": not memory,
                    "validity_retained": "2099" in json.dumps(profile) + memory}
        return record("V8", bool(profile) and not observed["validity_retained"], observed)
    observed = {"proposal_accepted": True, "fact_characters": len(text), "memory_characters": len(memory),
                "projection": trace.call_args.args[1]["candidates"][0]["projection"]}
    return record("V5", not memory and observed["projection"] == "memory_budget_exhausted", observed)


async def disconnect_probe():
    from fastapi import WebSocketDisconnect
    from api.routes.chat_ws import websocket_endpoint
    from backend.tests.test_emotion_chat_ws import jev_answers
    tts_started = asyncio.Event()
    pending = []

    class Socket:
        disconnected = False
        received = False

        async def accept(self):
            pass

        async def receive_text(self):
            if not self.received:
                self.received = True
                return json.dumps({"content": "測試斷線", "turn_id": "baseline"})
            await tts_started.wait()
            self.disconnected = True
            raise WebSocketDisconnect()

        async def send_json(self, payload):
            if self.disconnected:
                raise RuntimeError("socket closed")

    async def chat(messages, send_chunk):
        await send_chunk("可見回答")
        return "可見回答"

    async def action(*args, **kwargs):
        await asyncio.Event().wait()

    async def tts(*args, **kwargs):
        pending.append(asyncio.current_task())
        tts_started.set()
        await asyncio.Event().wait()

    runtime = SimpleNamespace(accept=AsyncMock(return_value=uuid4()), retrieve=AsyncMock(return_value=({}, "")),
                              route_background=Mock(), reset=AsyncMock())
    socket = Socket()
    socket.app = SimpleNamespace(state=SimpleNamespace(memory_runtime=runtime))
    endpoint_error = None
    with patch("api.routes.chat_ws.call_jev", return_value=jev_answers()), \
            patch("api.routes.chat_ws.stream_agent_a", side_effect=chat), \
            patch("api.routes.chat_ws._produce_and_send_action_plan", side_effect=action), \
            patch("api.routes.chat_ws.synthesize_and_send_voice", side_effect=tts), \
            patch("api.routes.chat_ws.log_turn"), patch("api.routes.chat_ws.CHAT_PERSISTENCE_ENABLED", False):
        try:
            await websocket_endpoint(socket)
        except RuntimeError as exc:
            endpoint_error = type(exc).__name__
        finally:
            alive = sum(not task.done() and not task.cancelling() for task in pending)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    observed = {"endpoint_error": endpoint_error, "uncancelled_tts_tasks": alive,
                "harness_cleaned_tasks": True}
    return record("V6", endpoint_error == "RuntimeError" and alive > 0, observed)


def voice_probe():
    from api.routes.voice_ws import _VoiceSession, _STOP
    session = object.__new__(_VoiceSession)
    session._frames = queue.Queue()
    session._frames.put(b"\x01")
    session._frames.put(_STOP)
    session._mic_active = True
    session._segmenter = Mock()
    session._send = Mock()
    error = None
    try:
        session._worker_loop()
    except ValueError as exc:
        error = type(exc).__name__
    observed = {"worker_error": error, "error_event_sent": session._send.called,
                "feed_called": session._segmenter.feed.called}
    return record("V7", error == "ValueError" and not observed["error_event_sent"], observed)


def prompt_probe():
    from domain.agent_a_prompts import build_agent_a_prompt
    from domain.emotion_state import NEUTRAL_EMOTION_STATE
    from services.chat_service import build_chat_context, estimate_token_count
    prompt = build_agent_a_prompt({}, "", NEUTRAL_EMOTION_STATE, "Rushia")
    small = build_chat_context(prompt, [], "嗨", budget=512)[0]["content"]
    normal = build_chat_context(prompt, [], "嗨", budget=8192)[0]["content"]
    observed = {"fixed_prompt_tokens": estimate_token_count([{"role": "system", "content": prompt}]),
                "default_preserves_prompt": prompt == normal, "small_preserves_prompt": prompt == small,
                "commit_rule_retained": "本輪沒有提交成功通知時" in small}
    return record("V9", observed["default_preserves_prompt"] and not observed["commit_rule_retained"], observed)


def confidence_probe():
    from domain.jev_questions import map_answers_to_intent
    from api.routes.chat_ws import _choice_fallback_reason
    from backend.tests.test_emotion_chat_ws import action_answers
    observed = []
    for label, value in (("bool", True), ("infinity", float("inf")), ("over_one", 1.5)):
        answers = action_answers()
        answers["base_emotion"]["confidence"] = value
        intent = map_answers_to_intent(answers)
        observed.append({"input_class": label, "emotion": intent.get("emotion"),
                         "diagnostic": _choice_fallback_reason(answers["base_emotion"], {"shy"})})
    return record("V10", all(row["emotion"] == "shy" for row in observed), {"cases": observed})


def run_probes() -> list[dict]:
    probes = (("V1", policy_probe), ("V2", source_probe), ("V3", summary_probe),
              ("V4", compression_race_probe), ("V5", projection_probe), ("V6", disconnect_probe),
              ("V7", voice_probe), ("V8", lambda: projection_probe(True)),
              ("V9", prompt_probe), ("V10", confidence_probe))

    async def run():
        results = []
        for identifier, probe in probes:
            try:
                value = probe()
                if asyncio.iscoroutine(value):
                    value = await asyncio.wait_for(value, timeout=5)
                results.append(value)
            except Exception as exc:
                results.append({"id": identifier, "status": "error", "observed": {"error_type": type(exc).__name__}})
        return results

    return asyncio.run(run())
