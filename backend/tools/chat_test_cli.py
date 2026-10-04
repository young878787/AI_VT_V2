"""在隔離後端執行多輪 Chat 測試，逐輪保存可追溯報告。"""

import argparse
import asyncio
from collections import Counter
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import websockets
from dotenv import dotenv_values
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from zoneinfo import ZoneInfo

# 支援從 repository 根目錄直接執行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.memory_test_evidence import check_turn, answer_result
from tools.memory_semantic_review import evaluate_semantics
from tools.memory_testset import CORE_PATH, fingerprint, generate_cases, load_snapshot, validate_cases, step_mode
from services.memory_agent_client import CUMULATIVE_DIAGNOSTIC_FIELDS


def timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")


BACKEND_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = BACKEND_ROOT / "log" / "chat_test_runs"
ENV_PATH = BACKEND_ROOT.parent / ".env"


def load_scenario(path: str) -> list[str]:
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def test_database_url() -> str:
    values = dotenv_values(ENV_PATH)
    test_url = (os.environ.get("MEMORY_TEST_DATABASE_URL", values.get("MEMORY_TEST_DATABASE_URL")) or "").strip()
    production_url = (os.environ.get("MEMORY_DATABASE_URL", values.get("MEMORY_DATABASE_URL")) or "").strip()
    if not test_url or not production_url:
        raise RuntimeError("MEMORY_TEST_DATABASE_URL 與 MEMORY_DATABASE_URL 都必須設定")
    try:
        test_database = make_url(test_url).database
        production_database = make_url(production_url).database
        if not test_database or test_database == production_database:
            raise ValueError
        if make_url(test_url).get_backend_name() != "postgresql":
            raise ValueError
    except (ArgumentError, ValueError, TypeError):
        raise RuntimeError("測試 DB 必須是與正式 DB 不同的 PostgreSQL 資料庫") from None
    return test_url


class MemoryRunStore:
    """只連線至專用測試 DB，擁有並清理本次建立的 schema。"""

    def __init__(self, database_url: str, schema: str, user_id: uuid.UUID, character_id: uuid.UUID):
        if not schema.startswith("test_") or len(schema) != 37 or any(c not in "0123456789abcdef" for c in schema[5:]):
            raise ValueError("測試 schema 必須符合 test_<32 lowercase hex>")
        self.schema = schema
        self.owner = {"user_id": user_id, "character_id": character_id}
        self.engine = create_engine(make_url(database_url).set(drivername="postgresql+psycopg"))
        self.created = False

    def open(self) -> None:
        with self.engine.begin() as connection:
            exists = connection.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"),
                                        {"schema": self.schema}).scalar()
            if exists:
                raise RuntimeError("測試 schema 已存在，停止以避免覆蓋資料")
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.created = True
        with self.engine.begin() as connection:
            config = Config(str(BACKEND_ROOT / "alembic.ini"))
            config.attributes.update(connection=connection, schema=self.schema)
            command.upgrade(config, "head")

    def close(self) -> None:
        try:
            if self.created:
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')
                self.created = False
        finally:
            self.engine.dispose()

    def snapshot(self) -> dict:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                f'SELECT id, canonical_text, memory_type, status, embedding IS NOT NULL AS has_embedding, '
                f'embedding_model, group_id, subject_key, valid_from, valid_to, expires_at, observed_at '
                f'FROM "{self.schema}".memory_items '
                'WHERE user_id = :user_id AND character_id = :character_id ORDER BY id'
            ), self.owner).mappings()
            return {str(row["id"]): {"text": row["canonical_text"], "type": row["memory_type"],
                                      "status": row["status"], "has_embedding": row["has_embedding"],
                                      "embedding_model": row["embedding_model"],
                                      **{key: str(row[key]) if row[key] is not None else None for key in
                                         ("group_id", "subject_key", "valid_from", "valid_to", "expires_at", "observed_at")}}
                    for row in rows}

    def job(self, event_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(text(
                f'SELECT route, route_confidence, agent_diagnostics, status, route_finalized, context_job_ids, error, '
                f'embedding_diagnostics, missing_context, source_ids, attempts, generation, created_at, updated_at, lease_until '
                f'FROM "{self.schema}".memory_jobs WHERE id = :event_id '
                'AND user_id = :user_id AND character_id = :character_id'
            ), {**self.owner, "event_id": uuid.UUID(event_id)}).mappings().first()
            if row is None:
                return None
            result = dict(row)
            result["confidence"] = result.pop("route_confidence")
            for key in ("context_job_ids", "source_ids"):
                result[key] = [str(value) for value in result[key]]
            for key in ("created_at", "updated_at", "lease_until"):
                result[key] = str(result[key]) if result[key] else None
            return result

    def audit(self, event_id: str) -> list[dict]:
        operation_event_id = str(uuid.UUID(event_id))
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                f'SELECT action, target_id, reason_class, deleted_count, decision, operation_key, created_at '
                f'FROM "{self.schema}".memory_audit '
                'WHERE user_id = :user_id AND character_id = :character_id '
                "AND operation_key LIKE :operation_key ORDER BY split_part(operation_key, ':', 2)::integer"
            ), {**self.owner, "operation_key": f"{operation_event_id}:%"}).mappings()
            return [{"action": row["action"], "target_id": str(row["target_id"]) if row["target_id"] else None,
                     "reason": row["reason_class"], "deleted_count": row["deleted_count"],
                     "decision": row["decision"], "operation_key": row["operation_key"],
                     "created_at": str(row["created_at"])} for row in rows]

    def case_state(self) -> dict:
        """reset 前保存 owner 的來源、evidence、版本與工作最終關聯。"""
        state = {}
        with self.engine.connect() as connection:
            for table, columns in {
                "sources": ("memory_sources", "id, conversation_id, message_id, speaker, raw_text, occurred_at"),
                "evidence": ("memory_evidence", "memory_id, source_id, kind"),
                "relations": ("memory_relations", "from_id, to_id, kind"),
                "audit": ("memory_audit", "action, target_id, operation_key, reason_class"),
                "jobs": ("memory_jobs", "id, conversation_id, route, status, route_finalized, context_job_ids, source_ids, generation, attempts, error"),
            }.items():
                name, fields = columns
                rows = connection.execute(text(f'SELECT {fields} FROM "{self.schema}".{name} '
                    'WHERE user_id = :user_id AND character_id = :character_id'), self.owner).mappings()
                state[table] = sorted(json.loads(json.dumps([dict(row) for row in rows], default=str)),
                                      key=lambda row: json.dumps(row, sort_keys=True))
        state["items"] = self.snapshot()
        return state


def memory_changes(before: dict, after: dict) -> dict:
    changes = {
        "created": {key: value for key, value in after.items() if key not in before},
        "updated": {key: value for key, value in after.items() if key in before and value != before[key]},
        "removed": [key for key in before if key not in after],
    }
    return {key: value for key, value in changes.items() if value}


async def wait_memory_job(store: MemoryRunStore, event_id: str | None, timeout: float = 330) -> dict:
    if not event_id:
        return {"status": "missing", "error": "未收到 memory event_id"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await asyncio.to_thread(store.job, event_id)
        if job and job["route_finalized"] and job["status"] in {
            "done", "ignored", "buffered", "failed", "cancelled", "discarded",
        }:
            return job
        await asyncio.sleep(0.2)
    return {"status": "timeout", "error": "記憶工作等待逾時"}


def create_run_dir(root: Path | None = None) -> Path:
    """固定最新產物；只解除舊 latest 連結，不覆寫連結指向的歷史。"""
    root = root or RUNS_DIR
    root.mkdir(parents=True, exist_ok=True)
    latest = root / "latest"
    if latest.is_symlink():
        latest.unlink()
    latest.mkdir(exist_ok=True)
    return latest


def model_metadata() -> dict:
    values = dotenv_values(ENV_PATH)

    def configured(name: str) -> str | None:
        return os.environ.get(name, values.get(name))

    return {
        "ai_provider": urlparse(configured("CHAT_AI_BASE_URL") or "").hostname or "(unset)",
        "chat_model": configured("CHAT_AI_MODEL") or "(unset)",
        "jev_model": configured("JEV_AI_MODEL") or "jev-latest",
        "memory_model": configured("MEMORY_AI_MODEL") or "(unset)",
        "embedding_model": configured("EMBEDDING_AI_MODEL") or "(unset)",
        "embedding_dimension": configured("EMBEDDING_AI_DIMENSION") or "(unset)",
    }


def find_available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_backend(run_dir: Path, port: int, database_url: str, schema: str,
                  user_id: uuid.UUID, character_id: uuid.UUID) -> tuple[subprocess.Popen, object]:
    log_file = (run_dir / "server.log").open("w", encoding="utf-8")
    child_env = os.environ.copy()
    child_env["AI_VT_MEMORY_DIR"] = str((run_dir / "memory").resolve())
    child_env["AI_VT_TEST_MODE"] = "true"
    child_env["MEMORY_TEST_DATABASE_URL"] = database_url
    child_env["MEMORY_DATABASE_SCHEMA"] = schema
    child_env["MEMORY_DEFAULT_USER_ID"] = str(user_id)
    child_env["MEMORY_DEFAULT_CHARACTER_ID"] = str(character_id)
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=BACKEND_ROOT, env=child_env, stdout=log_file, stderr=subprocess.STDOUT,
        )
    except Exception:
        log_file.close()
        raise
    return process, log_file


def stop_backend(process: subprocess.Popen, log_file: object) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    finally:
        log_file.close()


async def connect_backend(url: str, process: subprocess.Popen, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"測試後端啟動失敗（exit {process.returncode}），請查看 server.log")
        try:
            return await asyncio.wait_for(websockets.connect(url, max_size=10 * 1024 * 1024), timeout=2)
        except (OSError, TimeoutError, websockets.InvalidHandshake):
            await asyncio.sleep(0.2)
    raise TimeoutError(f"測試後端在 {timeout:g} 秒內未就緒，請查看 server.log")


async def run_turn(
    ws, turn: int, user_message: str, model_name: str, session_id: str,
    store: MemoryRunStore, timeout: float, test_mode=None,
) -> dict:
    before = await asyncio.to_thread(store.snapshot)
    state_before = await asyncio.to_thread(store.case_state) if test_mode else None
    started = time.monotonic()
    reply_parts = []
    emotion = source = expression = expression_debug = None
    errors = []
    first_text_sec = None
    emotion_sec = expression_sec = stream_end_sec = None
    event_id = None
    turn_id = uuid.uuid4().hex
    stream_complete = False
    request_sent = False
    try:
        async with asyncio.timeout(timeout):
            await ws.send(json.dumps({
                "type": "chat", "content": user_message, "model_name": model_name,
                "session_id": session_id,
                "turn_id": turn_id,
                **({"test_mode": test_mode.value} if test_mode else {}),
            }, ensure_ascii=False))
            request_sent = True
            while True:
                message = json.loads(await ws.recv())
                message_type = message.get("type")
                if message.get("turn_id") not in (None, turn_id):
                    continue
                if message_type == "input_accepted":
                    event_id = message.get("event_id")
                if message_type == "emotion_update":
                    emotion = message.get("state")
                    source = message.get("source")
                    emotion_sec = round(time.monotonic() - started, 2)
                elif message_type == "expression_plan":
                    expression_debug = message.get("debug")
                    expression = (expression_debug or {}).get("intentEmotion")
                    expression_sec = round(time.monotonic() - started, 2)
                elif message_type == "text_stream":
                    if first_text_sec is None:
                        first_text_sec = round(time.monotonic() - started, 2)
                    reply_parts.append(message.get("content", ""))
                elif message_type == "error":
                    errors.append(message.get("content", "未知錯誤"))
                elif message_type == "memory_enqueue_error":
                    errors.append("測試 DB 無法建立記憶事件")
                if message_type == "stream_end":
                    stream_complete = True
                    stream_end_sec = round(time.monotonic() - started, 2)
                if message_type in {"error", "memory_enqueue_error"} or (stream_complete and expression is not None):
                    break
    except asyncio.CancelledError:
        errors.append("使用者中斷測試")
    except TimeoutError:
        errors.append(f"單輪逾時（>{timeout:g}s）")
    except (websockets.ConnectionClosed, OSError, ValueError) as exc:
        errors.append(f"連線或回應錯誤：{exc}")
    interrupted = "使用者中斷測試" in errors
    job = None
    try:
        if event_id and not interrupted:
            job = await wait_memory_job(store, event_id)
    except asyncio.CancelledError:
        interrupted = True
        errors.append("使用者中斷測試")
    except Exception as exc:
        errors.append(f"讀取記憶工作失敗：{type(exc).__name__}")
    memory_status = job["status"] if job else None
    if job and memory_status in {"failed", "cancelled", "timeout", "missing"}:
        errors.append(job.get("error") or f"記憶工作狀態：{memory_status}")
    memory_completion_sec = round(time.monotonic() - started, 2) if job else None
    audit = []
    try:
        after = await asyncio.to_thread(store.snapshot)
        if event_id:
            audit = await asyncio.to_thread(store.audit, event_id)
    except Exception as exc:
        after = before
        errors.append(f"讀取 DB 證據失敗：{type(exc).__name__}")
    state_after = await asyncio.to_thread(store.case_state) if test_mode else None
    return {
        "_db_state": state_after,
        "db_unchanged": state_before == state_after if test_mode else None,
        "turn": turn,
        "ts": timestamp(),
        "turn_id": turn_id,
        "session_id": session_id,
        "request_sent": request_sent,
        "stream_complete": stream_complete,
        "interrupted": interrupted,
        "user": user_message,
        "reply": "".join(reply_parts).strip(),
        "emotion_state": emotion,
        "emotion_source": source,
        "expression": expression,
        "expression_debug": expression_debug,
        "memory_changes": memory_changes(before, after),
        "memory_event_id": event_id,
        "memory_agent_diagnostics": job.get("agent_diagnostics", {}) if job else {},
        "memory_route": job.get("route") if job else None,
        "memory_route_confidence": job.get("confidence") if job else None,
        "memory_context_job_ids": job.get("context_job_ids", []) if job else [],
        "memory_audit": audit,
        "memory_embedding_diagnostics": job.get("embedding_diagnostics", {}) if job else {},
        "memory_job_status": memory_status,
        "memory_missing_context": job.get("missing_context") if job else None,
        "memory_source_ids": job.get("source_ids", []) if job else [],
        "memory_attempts": job.get("attempts") if job else None,
        "memory_generation": job.get("generation") if job else None,
        "memory_route_finalized": job.get("route_finalized") if job else None,
        "memory_job_created_at": job.get("created_at") if job else None,
        "memory_job_updated_at": job.get("updated_at") if job else None,
        "errors": errors,
        "latency_first_text_sec": first_text_sec,
        "latency_emotion_sec": emotion_sec,
        "latency_expression_sec": expression_sec,
        "latency_stream_end_sec": stream_end_sec,
        "latency_memory_completion_sec": memory_completion_sec,
        "duration_sec": round(time.monotonic() - started, 2),
    }


def print_turn(record: dict) -> None:
    print(f"\n[Turn {record['turn']}] {record['user']}")
    print(f"露西亞: {record['reply']}")
    print(f"情緒 ({record['emotion_source']}): {json.dumps(record['emotion_state'], ensure_ascii=False)}")
    print(f"表情: {record['expression']}")
    debug = record.get("expression_debug") or {}
    if debug.get("jevBaseEmotionChoice"):
        print(
            f"JEV 表演: {debug['jevBaseEmotionChoice']} + "
            f"{debug.get('jevInteractionAttitudeChoice', '-')} → "
            f"{debug.get('jevResolvedEmotion', '-')} / "
            f"{debug.get('jevResolvedAttitude', '-')} "
            f"({debug.get('jevDecisionSource', '-')})"
        )
    if record["memory_changes"]:
        print(f"記憶變更: {', '.join(record['memory_changes'])}")
    if record.get("memory_job_status"):
        print(f"記憶: {record.get('memory_route') or '-'} / {record['memory_job_status']}")
    if record.get("memory_embedding_diagnostics"):
        print("Embedding: " + summarize_embedding_diagnostics(record["memory_embedding_diagnostics"]))
    if record["errors"]:
        print(f"錯誤: {record['errors']}")
    elif any(not check["passed"] for check in record.get("hard_checks", [])):
        print(answer_result(record))


def summarize_embedding_diagnostics(diagnostics: dict) -> str:
    if not diagnostics:
        return "無紀錄"
    configured = diagnostics.get("model") or "-"
    serving = diagnostics.get("serving_model") or "-"
    served = diagnostics.get("served_model") or "-"
    return (f"呼叫 {diagnostics.get('calls', 0)} 次／失敗 {diagnostics.get('failures', 0)} 次／"
            f"{diagnostics.get('dimension')} 維／{configured}→{serving}→{served}／"
            f"{diagnostics.get('duration_ms', 0):.1f} ms")


def markdown_cell(value: object, limit: int = 240) -> str:
    """將摘要欄位壓成單行；完整值仍保留在 JSONL 詳細紀錄。"""
    text = "-" if value is None or value == "" else str(value)
    text = " ".join(text.split()).replace("|", "\\|")
    if len(text) > limit:
        return text[:limit - 1] + "…"
    return text


def atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def operation_counts(records: list[dict], key: str) -> str:
    counts = Counter(item.get("action", "?") for record in records for item in record.get(key, []))
    return ", ".join(f"{name}:{count}" for name, count in counts.items()) or "-"


def injected_count(record: dict) -> int:
    return sum(
        len(trace.get("injected_memory_ids", []))
        for trace in record.get("trace", [])
        if trace.get("stage") == "chat_context"
    )


def read_turn_trace(run_dir: Path, turn_id: str | None) -> list[dict]:
    path = run_dir / "memory" / "trace.jsonl"
    if not turn_id or not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if item.get("turn_id") == turn_id:
                records.append(item)
        except (ValueError, KeyError):
            continue  # 程序中斷留下的未完成一行不當成證據。
    context = next((item for item in records if item["stage"] == "chat_context"), None)
    retrieval = next((item for item in records if item["stage"] == "retrieval"), None)
    if context is not None:
        context["injected_memory_ids"] = []
        context["memory_fragments"] = []
        if retrieval:
            locate_injected_fragments(context, retrieval["projections"])
    return records


def locate_injected_fragments(context: dict, projections: list[dict]) -> None:
    """記錄裁切後片段；只有完整投影仍在 prompt 時才算成功注入。"""
    from domain.agent_a_prompts import _build_profile_section
    profile = context.get("profile", {})
    profile_section = _build_profile_section(profile)
    memory_cursor = context.get("memory_section_start")
    for projection in projections:
        text_value = projection["text"]
        if projection["destination"] == "memory":
            start = memory_cursor
            if start is not None:
                memory_cursor += len(text_value) + 1
        else:
            value = profile.get(projection["field"])
            if text_value != value and (not isinstance(value, list) or text_value not in value):
                continue
            field_section = _build_profile_section({projection["field"]: value})
            field_start = profile_section.find(field_section)
            offset = field_start + field_section.find(text_value) if field_start >= 0 else -1
            start = context.get("profile_section_start", 0) + offset if offset >= 0 else None
        if start is None or start < 0:
            continue
        fragments = []
        projection_end = start + len(text_value)
        for left, right in context["system_retained_ranges"]:
            lower, upper = max(start, left), min(start + len(text_value), right)
            if lower < upper:
                fragment = text_value[lower - start:upper - start]
                fragments.append((lower, upper, fragment))
        complete = any(lower <= start and upper >= projection_end for lower, upper, _ in fragments)
        for lower, upper, fragment in fragments:
            context["memory_fragments"].append({"id": projection["id"],
                "destination": projection["destination"], "text": fragment,
                "complete": complete, "original_system_range": [lower, upper]})
        if complete and projection["id"] not in context["injected_memory_ids"]:
            context["injected_memory_ids"].append(projection["id"])


def write_markdown_report(records: list[dict], path: Path, metadata: dict, status: str, error: str | None) -> None:
    """寫入表情／JEV 摘要；完整證據只保留在 turns.jsonl。"""
    completed = sum(not record["errors"] for record in records)
    planned = metadata["planned_turns"]
    lines = [
        "# Headless Chat 測試報告", "",
        f"- 狀態：**{status}**；已完成 {completed} / {planned if planned is not None else '不限'} 輪",
        f"- 開始：{metadata['started_at']}",
        f"- 更新：{timestamp()}",
        f"- Scenario：`{metadata['scenario']}`（SHA-256：`{metadata['scenario_sha256']}`）",
        f"- AI：{metadata['ai_provider']} / `{metadata['chat_model']}`；JEV：`{metadata['jev_model']}`",
        f"- Embedding：`{metadata.get('embedding_model', '(unset)')}`／"
        f"{metadata.get('embedding_dimension', '(unset)')} 維／L2 normalization",
        f"- 測試 DB schema：`{metadata['memory_schema']}`（報告產出後清理）",
        "- 測試短期記憶：執行期間使用隔離目錄，結束時清除",
        f"- 詳細逐輪紀錄：`{path.parent / 'turns.jsonl'}`；後端日誌：`{path.parent / 'server.log'}`",
        f"- 案例結案快照：`{path.parent / 'case_states.jsonl'}`",
        "- [記憶／聊天報告](memory_report.md)",
    ]
    if error:
        lines.append(f"- 停止原因：{error}")
    if records:
        first_debug = records[0].get("expression_debug") or {}
        criteria_version = first_debug.get("jevDecisionCriteriaVersion")
        if criteria_version:
            question_hash = first_debug.get("jevDecisionQuestionHash")
            lines.append(f"- JEV 聯合判準版本：`{criteria_version}`；問題指紋：`{question_hash or '-'}`")

    debug_rows = [(record, record.get("expression_debug") or {}) for record in records]
    base_counts = Counter(debug.get("jevBaseEmotionChoice", "-") for _, debug in debug_rows)
    attitude_counts = Counter(debug.get("jevInteractionAttitudeChoice", "-") for _, debug in debug_rows)
    expression_counts = Counter(record.get("expression") or "-" for record in records)
    fallback_rows = [
        (record, debug) for record, debug in debug_rows
        if debug.get("jevDecisionSource") != "jev" or record.get("errors")
    ]

    def counts_text(counts: Counter) -> str:
        return "、".join(f"{key} {value}" for key, value in counts.most_common()) or "-"

    lines.extend([
        "", "## 統計摘要", "",
        f"- 基礎情緒：{counts_text(base_counts)}",
        f"- 互動態度（JEV 原始選擇）：{counts_text(attitude_counts)}",
        f"- 最終表情：{counts_text(expression_counts)}",
        f"- 需檢查輪次：{len(fallback_rows)} / {len(records)}",
        "", "## 全部輪次決策總覽", "",
        "| # | 使用者 | AI 回覆 | 六欄情緒 | 基礎情緒 | 原始態度 | 最終表情 | 最終態度 | 信心 | 狀態 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ])
    for record in records:
        debug = record.get("expression_debug") or {}

        def diagnostic_value(key: str) -> str:
            value = debug.get(key, "-")
            return f"{value:.2f}" if isinstance(value, (int, float)) else str(value)

        source = debug.get("jevDecisionSource")
        status_text = "錯誤" if record.get("errors") else (
            "OK" if source == "jev" else "部分回退" if source == "partial_fallback" else "回退"
        )
        if source == "partial_fallback" and not record.get("errors"):
            reasons = []
            if debug.get("jevBaseEmotionFallbackReason") not in (None, "none"):
                reasons.append("情緒 " + str(debug["jevBaseEmotionFallbackReason"]))
            if debug.get("jevInteractionAttitudeFallbackReason") not in (None, "none"):
                reasons.append("態度 " + str(debug["jevInteractionAttitudeFallbackReason"]))
            if reasons:
                status_text += "（" + "、".join(reasons) + "）"
        cells = [
            f"{record.get('case_id', '')}/{record['turn']}" if record.get("case_id") else record["turn"],
            markdown_cell(record["user"]), markdown_cell(record.get("reply")),
            "、".join(f"{field} {value:.2f}" if isinstance(value, (int, float)) else f"{field} 未提供"
                for field in ("shy", "pleased", "genuinely_angry", "sad_or_hurt", "masking_positive_feeling", "wants_continue_interaction")
                for value in [(record.get("emotion_state") or {}).get(field)]),
            diagnostic_value("jevBaseEmotionChoice"),
            diagnostic_value("jevInteractionAttitudeChoice"),
            record.get("expression") or "-",
            diagnostic_value("jevResolvedAttitude"),
            f"B {diagnostic_value('jevBaseEmotionConfidence')} / A {diagnostic_value('jevInteractionAttitudeConfidence')}",
            status_text,
        ]
        lines.append("| " + " | ".join(markdown_cell(cell) for cell in cells) + " |")
    lines.extend(["", "## 需查看的輪次", ""])
    if fallback_rows:
        for record, debug in fallback_rows:
            reasons = []
            if debug.get("jevBaseEmotionFallbackReason") not in (None, "none"):
                reasons.append(f"基礎情緒 {debug.get('jevBaseEmotionFallbackReason')}")
            if debug.get("jevInteractionAttitudeFallbackReason") not in (None, "none"):
                reasons.append(f"互動態度 {debug.get('jevInteractionAttitudeFallbackReason')}")
            if record.get("errors"):
                reasons.append("錯誤：" + ", ".join(record["errors"]))
            key = f"{record.get('case_id', 'legacy')}/{record['turn']}"
            lines.append(f"- `{key}`：{markdown_cell(record['user'])}（" + "; ".join(reasons) + "）")
    else:
        lines.append("- 無")
    lines.extend([
        "", "## 詳細紀錄", "",
        "- 完整 JEV、probabilities、fallback、記憶交易、召回與裁切後 messages：`turns.jsonl`。",
        "- 每行一筆逐輪 JSON；使用 `case_id`＋`turn`，或 `turn_id`／`memory_event_id` 精確擷取。",
        "- 每案 reset 前的 DB sources、evidence、relations、jobs 與 memory items：`case_states.jsonl`。",
        "- 摘要中的使用者輸入與回覆可能為單行截短；完整文字以 JSONL 為準。",
        "",
    ])
    atomic_write_text(path, "\n".join(lines))


def summarize_memory_stability(records: list[dict]) -> dict:
    """按 event 去重，累計成本不混成最後 attempt；語意評分仍獨立。"""
    jobs = {r["memory_event_id"]: r for r in records if r.get("memory_event_id")}
    diagnostics = [r.get("memory_agent_diagnostics") or {} for r in jobs.values()]
    waits = sorted(r["latency_memory_completion_sec"] for r in jobs.values()
                   if isinstance(r.get("latency_memory_completion_sec"), (int, float)))
    queues = [d["queue"] for d in diagnostics if d.get("queue")]
    return {
        "observed_jobs": len(jobs),
        "agent_jobs": sum(d.get("logical_steps", 0) > 0 for d in diagnostics),
        "statuses": dict(Counter(r.get("memory_job_status", "unknown") for r in jobs.values())),
        "attempts": sum(r.get("memory_attempts") or 0 for r in jobs.values()),
        "totals": {key: sum(d.get(key, 0) for d in diagnostics) for key in CUMULATIVE_DIAGNOSTIC_FIELDS},
        "last_errors": dict(Counter((d.get("last_failure") or {}).get("error") or d.get("error")
                                    for d in diagnostics if d.get("last_failure") or d.get("error"))),
        "retry_exhausted_jobs": sum(bool(d.get("retry_exhausted")) for d in diagnostics),
        "recovered_leases": sum(d.get("recovered_lease_age_sec") is not None for d in diagnostics),
        "completion_wait_sec": {"samples": len(waits), "p50": waits[math.ceil(len(waits) * .5) - 1] if waits else None,
                                "p95": waits[math.ceil(len(waits) * .95) - 1] if waits else None,
                                "max": waits[-1] if waits else None},
        "queue_samples": len(queues),
        "max_observed_queue_jobs": max((q["active_jobs"] for q in queues), default=None),
        "max_observed_queue_oldest_sec": max((q["oldest_age_sec"] for q in queues), default=None),
    }


def write_memory_report(records: list[dict], path: Path, metadata: dict, status: str, error: str | None) -> None:
    """主要表格提供回答比對；硬條件及來源證據保留在詳細段落。"""
    try:
        materials = json.loads((path.parent / "cases.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        materials = []
    states = {}
    state_path = path.parent / "case_states.jsonl"
    if state_path.exists():
        for line in state_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                states[item["case_id"]] = item["state"]
            except (ValueError, KeyError):
                continue
    if not materials:
        materials = [{"case_id": c["case_id"], "conversation": []} for c in metadata.get("case_statuses", [])]
    lines = ["# 記憶與聊天案例比對", "", f"- 執行狀態：**{status}**",
        f"- 案例：已執行 {metadata.get('executed_cases', 0)} / {metadata.get('planned_cases', 0)}；實際對話 {sum(r.get('action') != 'compress' for r in records)} 輪",
        f"- 開始：{metadata['started_at']}；案例 SHA-256：`{metadata.get('cases_sha256', '-')}`",
        "- 回答結果以執行、隔離與來源硬條件為前提；語意比對獨立呈現，不能覆蓋來源失敗。",
        "- [表情／態度／JEV 報告](expression_report.md)；[案例](cases.json)；[逐輪證據](turns.jsonl)；[DB 結案證據](case_states.jsonl)；[執行摘要](run.json)", ""]
    if error:
        lines.extend([f"最近錯誤：{error}", ""])
    stability = metadata.get("memory_stability")
    if stability:
        totals = stability["totals"]
        lines.extend(["## Memory 多輪穩定性", "",
            f"- 工作 {stability['observed_jobs']}；狀態 {stability['statuses']}；總 attempts {stability['attempts']}。",
            f"- 累計邏輯步驟 {totals['logical_steps']}／HTTP calls {totals['calls']}；search/read {totals['search_calls']}/{totals['read_calls']}；修正 {totals['corrections']}／合法替換 {totals['replacements']}。",
            f"- Token input/output {totals['input_tokens']}/{totals['output_tokens']}；模型累計耗時 {totals['latency_ms']} ms；timeout {totals['timeouts']}；attempt failures {totals['failures']}；重試耗盡工作 {stability['retry_exhausted_jobs']}。",
            f"- 結案等待（秒）{stability['completion_wait_sec']}；queue 取樣 {stability['queue_samples']}，觀測最大工作數 {stability['max_observed_queue_jobs']}／最舊 age {stability['max_observed_queue_oldest_sec']} 秒。",
            "- 每個 event 僅取最後紀錄；累計成本跨 attempts，最後 attempt 的增量見 turns.jsonl 的 attempt_metrics。queue 只在 claim 後取樣；等待包含 Chat 與背景工作重疊，不等同純 Agent 延遲，也不代表持續負載 SLO。", ""])

    def cell(value):
        return markdown_cell(value, limit=100000)

    for group in ("short_term", "long_term", "mixed", None):
        selected = [c for c in materials if c.get("case_group") == group]
        if not selected:
            continue
        lines.extend([f"## {group or 'legacy'}", "",
            "| Case／Probe | 組別／類型 | 回答結果 | 最終應該答案／對話 |", "|---|---|---|---|"])
        for case in selected:
            steps = [(i, s) for i, s in enumerate(case.get("conversation", []), 1) if s["phase"] in {"probe", "recall_probe"}]
            if not steps and case.get("conversation"):
                steps = [(len(case["conversation"]), case["conversation"][-1])]
            for probe, (index, step) in enumerate(steps, 1):
                record = next((r for r in records if r.get("case_id") == case["case_id"] and r.get("turn") == index), None)
                result = answer_result(record) if record else "錯誤：此步驟尚未執行"
                expected = step.get("expected_result", case.get("expected_result", "未定義；TXT 表情回歸"))
                label = f"probe_{probe}" if step["phase"] in {"probe", "recall_probe"} else "setup"
                lines.append(f"| [{case['case_id']} / {label}](#{case['case_id']}) | {group or 'legacy'} / {case.get('case_type', '-')} | {cell(result)} | {cell(expected)} |")
        lines.append("")
    reviews = [r for r in records if r.get("semantic_review")]
    if reviews:
        evaluation = metadata.get("semantic_evaluation", {})
        lines.extend(["## 回答語意比對", "",
            f"- 評分 prompt：`{evaluation.get('prompt_version')}`；模型：{evaluation.get('models', [])}；統計：{evaluation.get('counts', {})}",
            "- 使用既有 Chat 模型，以獨立審查 prompt 比對；這是模型評估，仍需保留人工核對空間。", "",
            "| Case／Step | 語意判定 | 理由 | 缺少事實／無來源斷言 | 硬條件符合 |", "|---|---|---|---|---|"])
        for record in reviews:
            review = record["semantic_review"]
            lines.append(f"| {record['case_id']} / {record['turn']} | {review.get('verdict', review['status'])} | {cell(review.get('reason', review.get('error')))} | {cell(review.get('missing_facts', []) + review.get('unsupported_claims', []))} | {review['hard_conditions_met']} |")
        lines.append("")
    for case in materials:
        case_id = case["case_id"]
        actual = [r for r in records if r.get("case_id") == case_id]
        lines.extend([f'<a id="{case_id}"></a>', f"## {case_id}", "",
                      "測試目的：" + "、".join(case.get("expected_focus", [])), "",
                      "| Step | Phase／實際模式 | Session | User input | Assistant reply |",
                      "|---:|---|---|---|---|"])
        for index, step in enumerate(case.get("conversation", []), 1):
            record = next((r for r in actual if r.get("turn") == index), {})
            lines.append(f"| {index} | {step['phase']} / {record.get('test_mode', '未執行')} | {step['session']} / {record.get('session_id', '-')} | {cell(step.get('input', '[compress]'))} | {cell(record.get('reply', '未執行'))} |")
        for record in actual:
            if record.get("action") == "compress":
                continue
            lines.extend(["", f"### Step {record['turn']}", ""])
            if record.get("phase") in {"probe", "recall_probe"} or not any(s["phase"] in {"probe", "recall_probe"} for s in case.get("conversation", [])):
                lines.extend(["回答結果：" + cell(answer_result(record)), "",
                    "最終應該答案／對話：" + cell(record.get("expected_result", case.get("expected_result"))), ""])
            context = next((t for t in record.get("trace", []) if t.get("stage") == "chat_context"), {})
            retrieval = next((t for t in record.get("trace", []) if t.get("stage") == "retrieval"), {})
            lines.extend(["短期來源（裁切後）：", "",
                          "- Summary：" + cell(context.get("retained_summary")),
                          "- History：" + cell(" / ".join(f"{m['role']}: {m['content']}" for m in context.get("messages", [])[1:-1])), ""])
            for fact in record.get("source_evidence", []):
                source = case["conversation"][fact["source_step"] - 1]["input"]
                lines.append(f"- 來源 Step {fact['source_step']} / {fact['source']}：{cell(source)}；實際位置：{cell(fact['actual'])}")
            lines.extend(["", "| Memory ID／狀態 | Canonical text | Cosine／exact_match | 投影目的地／裁切後注入 |",
                          "|---|---|---|---|"])
            for candidate in retrieval.get("candidates", []):
                destinations = [p.get("destination") for p in retrieval.get("projections", []) if p['id'] == candidate['id']]
                injected = candidate['id'] in context.get("injected_memory_ids", [])
                lines.append(f"| {candidate['id']} / {candidate.get('status')} | {cell(candidate.get('canonical_text'))} | {candidate.get('similarity')} / {candidate.get('exact_match')} | {destinations} / {injected} |")
            if not retrieval.get("candidates"):
                lines.append("| - | 無合格候選或此模式停用召回 | - | 無 |")
            for trace_item in record.get("trace", []):
                if trace_item.get("stage") == "retrieval_filter" and trace_item.get("mode") == retrieval.get("mode"):
                    for candidate in trace_item.get("excluded_candidates", []):
                        lines.append(f"| {candidate['id']} / 未通過 | {cell(candidate.get('canonical_text'))} | {candidate.get('similarity')} / {candidate.get('exact_match')} | 否：{candidate.get('rejection_reason')} |")
            management_ids = list(dict.fromkeys(c['id'] for t in record.get("trace", [])
                if t.get("stage") == "memory_candidates" for c in t.get("candidates", [])))
            lines.extend(["", f"寫入：{record.get('memory_route')} / {record.get('memory_job_status')}；source IDs：{record.get('memory_source_ids', [])}；audit：{cell(record.get('memory_audit', []))}",
                          "", "Agent 管理候選（不是 Chat 召回）：" + cell(management_ids), "",
                          "| 硬條件 | 實際值 | 預期值 | 失敗理由 |", "|---|---|---|---|"])
            for check in record.get("hard_checks", []):
                lines.append(f"| {check['layer']} / {check['name']} | {cell(check['actual'])} | {cell(check['expected'])} | {cell(check['reason'])} |")
        state = states.get(case_id, {})
        if state.get("items"):
            lines.extend(["", "結案 current/history：", "",
                          "| Memory ID／狀態 | Canonical text | 有效期間 | User evidence source IDs |", "|---|---|---|---|"])
            for memory_id, item in state["items"].items():
                sources = [e["source_id"] for e in state.get("evidence", []) if e["memory_id"] == memory_id]
                lines.append(f"| {memory_id} / {item['status']} | {cell(item['text'])} | {item.get('valid_from')} ~ {item.get('valid_to')} | {cell(sources)} |")
            if state.get("relations"):
                lines.extend(["", "版本關聯：" + cell(state["relations"])])
        lines.extend(["", f"完整 sources／evidence／relations／audit 與 current/history 狀態請在 [case_states.jsonl](case_states.jsonl) 依 `{case_id}` 查閱；逐輪原始 trace 與硬條件在 [turns.jsonl](turns.jsonl)。", ""])
    atomic_write_text(path, "\n".join(lines) + "\n")

def write_reports(records, run_dir, metadata, status, error):
    dialogue = [record for record in records if record.get("action") != "compress"]
    write_markdown_report(dialogue, run_dir / "expression_report.md", metadata, status, error)
    write_memory_report(records, run_dir / "memory_report.md", metadata, status, error)


def save_record(records: list[dict], record: dict, run_dir: Path, metadata: dict, status: str, error: str | None) -> None:
    record.pop("_db_state", None)
    records.append(record)
    with (run_dir / "turns.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


async def control_step(ws, action: str, session_id: str, timeout: float, test_mode=None) -> dict:
    expected = {"reset": "reset_done", "compress": "compress_done"}[action]
    started = time.monotonic()
    await ws.send(json.dumps({"type": action, "session_id": session_id,
                              **({"test_mode": test_mode.value} if test_mode else {})}))
    async with asyncio.timeout(timeout):
        while True:
            message = json.loads(await ws.recv())
            if message.get("type") == "error":
                raise RuntimeError(f"{action} 控制失敗")
            if message.get("type") == expected:
                return {"action": action, "acknowledged": True,
                        "duration_sec": round(time.monotonic() - started, 3)}


async def run(args: argparse.Namespace) -> tuple[Path, str]:
    database_url = test_database_url()
    # 先完整載入重播來源，再覆寫固定最新產物，避免重播 latest 時先清空案例。
    legacy = bool(args.scenario and Path(args.scenario).suffix.lower() == ".txt")
    cases = None
    generator_metadata = {"status": "not_started"}
    if args.scenario:
        if legacy:
            inputs = load_scenario(args.scenario)
            if args.max_turns:
                inputs = inputs[:args.max_turns]
            cases = [{"case_id": "legacy", "case_group": None, "conversation": [
                {"phase": "probe", "session": "context", "input": value} for value in inputs]}]
        else:
            cases = load_snapshot(args.scenario)
            generator_metadata = {"status": "replayed"}
            provenance = Path(args.scenario).with_name("run.json")
            if provenance.exists():
                try:
                    previous = json.loads(provenance.read_text(encoding="utf-8"))
                    if previous.get("cases_sha256") == fingerprint(cases):
                        generator_metadata = {**previous.get("generator", {}), "replayed": True}
                except (ValueError, OSError):
                    pass
    elif args.max_turns:
        raise ValueError("--max-turns 僅供既有 TXT 表情回歸，不裁切 28-case 集")
    if not legacy and args.max_turns:
        raise ValueError("28-case 快照不得以 --max-turns 裁切")
    run_dir = create_run_dir()
    (run_dir / "memory").mkdir(exist_ok=True)
    for name in ("cases.json", "turns.jsonl", "case_states.jsonl", "memory_report.md", "expression_report.md", "server.log"):
        (run_dir / name).write_text("", encoding="utf-8")
    schema = "test_" + uuid.uuid4().hex
    user_id, character_id = uuid.uuid4(), uuid.uuid4()
    store = MemoryRunStore(database_url, schema, user_id, character_id)
    metadata = {"started_at": timestamp(), "scenario": str(Path(args.scenario).resolve()) if args.scenario else str(CORE_PATH),
        "scenario_sha256": fingerprint(cases) if cases is not None else None,
        "planned_cases": len(cases) if cases is not None else 28,
        "planned_turns": sum("input" in step for case in cases for step in case["conversation"]) if cases else None,
        "memory_schema": schema, "legacy": legacy, "generator": generator_metadata,
        "completed_case_ids": [], "case_statuses": [], "cleanup": {}, **model_metadata()}
    records = []
    status, error = "running", None
    process = log_file = ws = None
    current_case = None

    def persist(*, render_reports: bool = False):
        metadata["memory_stability"] = summarize_memory_stability(records)
        metadata["hard_checks"] = {layer: dict(Counter(r.get("hard_status", {}).get(layer, "not_checked")
            for r in records if r.get("action") != "compress")) for layer in
            ("execution_status", "isolation_status", "memory_evidence_status", "context_evidence_status")}
        metadata.update(status=status, error=error, executed_cases=len({r["case_id"] for r in records}),
                        executed_turns=sum(r.get("action") != "compress" for r in records), updated_at=timestamp())
        (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        if render_reports:
            write_reports(records, run_dir, metadata, status, error)

    persist(render_reports=True)
    print(f"測試資料夾：{run_dir}")
    try:
        persist(render_reports=True)
        if cases is None:
            core = validate_cases(json.loads(CORE_PATH.read_text(encoding="utf-8")), source="Gold", count=23)
            # 生成失敗也保存已知固定素材，不冒充完整快照。
            (run_dir / "cases.json").write_text(json.dumps(core, ensure_ascii=False, indent=2), encoding="utf-8")
            cases = core + await generate_cases(core, metadata["generator"])
        metadata.update(cases_sha256=fingerprint(cases), scenario_sha256=fingerprint(cases),
            planned_turns=sum("input" in step for case in cases for step in case["conversation"]),
            case_statuses=[{"case_id": case["case_id"], "status": "pending"} for case in cases])
        (run_dir / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
        persist(render_reports=True)
        store.open()
        port = find_available_port()
        url = f"ws://127.0.0.1:{port}/ws/chat"
        process, log_file = start_backend(run_dir, port, database_url, schema, user_id, character_id)
        ws = await connect_backend(url, process, args.startup_timeout)
        for case, current_case in zip(cases, metadata["case_statuses"]):
            current_case["status"] = "running"
            case_has_errors = False
            sessions = {step["session"]: f"test_{uuid.uuid4().hex}" for step in case["conversation"]}
            active_alias = case["conversation"][0]["session"] if case["conversation"] else None
            if not legacy:
                # 新 case owner reset 與每個 probe 的新連線；已保存前案結案證據。
                await control_step(ws, "reset", sessions[active_alias], args.turn_timeout)
            for turn, step in enumerate(case["conversation"], 1):
                if step["session"] != active_alias:
                    await ws.close()
                    ws = await connect_backend(url, process, args.startup_timeout)
                    active_alias = step["session"]
                session_id = sessions[active_alias]
                if step.get("action") == "compress":
                    record = {"turn": turn, "ts": timestamp(), "action": "compress", "user": "[compress]",
                              "reply": "", "session_id": session_id, "errors": []}
                    try:
                        record["control"] = await control_step(ws, "compress", session_id, args.turn_timeout,
                                                            test_mode=step_mode(case, step))
                    except (TimeoutError, RuntimeError) as exc:
                        record["errors"].append(type(exc).__name__)
                else:
                    attempt_errors = []
                    for attempt in range(1, args.retries + 2):
                        record = await run_turn(ws, turn, step["input"], "Rushia", session_id, store, args.turn_timeout,
                                                **({"test_mode": step_mode(case, step)} if not legacy else {}))
                        record["attempts"] = attempt
                        if not record["errors"]:
                            break
                        attempt_errors.extend(record["errors"])
                        # send 成功也不能盲目重送：ACK 遺失不代表後端沒接受。
                        if (record.get("request_sent") or record["reply"] or record.get("memory_event_id")
                                or "逾時" in record["errors"][0] or attempt > args.retries):
                            break
                        await ws.close()
                        try:
                            ws = await connect_backend(url, process, args.startup_timeout)
                        except Exception as exc:
                            record["errors"].append(f"重連失敗：{type(exc).__name__}")
                            break
                    record["attempt_errors"] = attempt_errors
                    record["trace"] = read_turn_trace(run_dir, record.get("turn_id"))
                    if not legacy and not record["errors"] and not any(item["stage"] == "chat_context" for item in record["trace"]):
                        record["errors"].append("缺少裁切後 Chat context trace")
                    db_state = record.pop("_db_state", None) or {}
                    if not legacy:
                        record["expected_result"] = step.get("expected_result", case["expected_result"])
                        check_turn(case, step, record, [r for r in records if r.get("case_id") == case["case_id"]], db_state)
                record.update(case_id=case["case_id"], phase=step["phase"], case_group=case["case_group"],
                              test_mode=step_mode(case, step).value if not legacy else "normal")
                if record["errors"] or any(v == "failed" for v in record.get("hard_status", {}).values()):
                    case_has_errors = True
                    status = "interrupted" if record.get("interrupted") else "failed"
                    error = f"第 {turn} 輪失敗：{record['errors'][0] if record['errors'] else answer_result(record)}"
                    current_case["status"] = status
                    current_case["error"] = error
                save_record(records, record, run_dir, metadata, status, error)
                persist()
                if "input" in step:
                    print_turn(record)
                if record["errors"]:
                    if not legacy and record.get("stream_complete") and record.get("memory_job_status") == "failed":
                        # 背景模型已達重試上限並結案；保存失敗，繼續實際 probe 觀察。
                        status = "running"
                    else:
                        break
            if not legacy and records and records[-1]["case_id"] == case["case_id"]:
                case_state = await asyncio.to_thread(store.case_state)
                with (run_dir / "case_states.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps({"case_id": case["case_id"], "captured_at": timestamp(),
                                           "state": case_state}, ensure_ascii=False) + "\n")
            if status == "interrupted" or (legacy and status != "running"):
                break
            if case_has_errors:
                current_case["status"] = "failed"
                status = "running"  # 後續獨立 case 仍可 reset 後執行。
            else:
                current_case["status"] = "completed"
                metadata["completed_case_ids"].append(case["case_id"])
            persist(render_reports=True)
        if status == "running":
            status = "failed" if any(case["status"] == "failed" for case in metadata["case_statuses"]) else "completed"
        if not legacy:
            metadata["semantic_evaluation"] = {}
            semantic_failed = await evaluate_semantics(cases, records, metadata["semantic_evaluation"])
            for case_status in metadata["case_statuses"]:
                if case_status["case_id"] in semantic_failed:
                    case_status["status"] = "failed"
                    case_status["error"] = "回答語意比對未通過或未完成"
            metadata["completed_case_ids"] = [c["case_id"] for c in metadata["case_statuses"] if c["status"] == "completed"]
            if semantic_failed:
                status = "failed"
                error = "回答語意比對未通過或未完成：" + ", ".join(sorted(semantic_failed))

    except asyncio.CancelledError:
        status, error = "interrupted", "使用者中斷測試"
        if current_case:
            current_case["status"] = status
        raise
    except KeyboardInterrupt:
        status, error = "interrupted", "使用者中斷測試"
    except Exception as exc:
        status = "failed"
        error = str(exc) if isinstance(exc, (RuntimeError, TimeoutError, ValueError)) else type(exc).__name__
        if current_case:
            current_case["status"] = status
        print(f"測試失敗：{error}")
    finally:
        if metadata.get("semantic_evaluation"):
            try:
                atomic_write_text(run_dir / "turns.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
            except OSError as exc:
                status, error = "failed", f"保存語意比對失敗：{type(exc).__name__}"
        if ws is not None:
            try:
                await ws.close()
                metadata["cleanup"]["websocket"] = "closed"
            except Exception as exc:
                metadata["cleanup"]["websocket"] = type(exc).__name__
                status = "failed"
        if process is not None:
            try:
                stop_backend(process, log_file)
                metadata["cleanup"]["backend"] = "stopped"
            except Exception as exc:
                status, error = "failed", f"關閉測試後端失敗：{type(exc).__name__}"
                metadata["cleanup"]["backend"] = "failed"
        try:
            store.close()
            metadata["cleanup"]["schema"] = "removed"
        except Exception as exc:
            status, error = "failed", f"清理測試 schema 失敗：{type(exc).__name__}"
            metadata["cleanup"]["schema"] = "failed"
        try:
            shutil.rmtree(run_dir / "memory")
            metadata["cleanup"]["short_term"] = "removed"
        except OSError as exc:
            status, error = "failed", f"清理短期暫存失敗：{type(exc).__name__}"
            metadata["cleanup"]["short_term"] = "failed"
        if not args.scenario or metadata.get("semantic_evaluation"):
            from infrastructure.ai_client import _role_clients
            for client in _role_clients.values():
                await client.close()
        persist(render_reports=True)
        print(f"報告已寫入：{run_dir / 'memory_report.md'}（{status}）")
    return run_dir, status


def main() -> None:
    parser = argparse.ArgumentParser(description="隔離式 Headless JEV Chat 測試")
    parser.add_argument("--scenario", help="28-case JSON 快照重播；TXT 保留既有表情回歸，省略時生成完整新集")
    parser.add_argument("--max-turns", type=int, default=0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--startup-timeout", type=float, default=60)
    parser.add_argument("--turn-timeout", type=float, default=120)
    args = parser.parse_args()
    if args.max_turns < 0 or args.retries < 0 or args.startup_timeout <= 0 or args.turn_timeout <= 0:
        parser.error("輪數與重試次數不得為負；逾時必須大於 0")
    try:
        _, status = asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130)
    if status == "interrupted":
        raise SystemExit(130)
    if status == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
