"""在隔離後端執行多輪 Chat 測試，逐輪保存可追溯報告。"""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
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
from tools.memory_testset import CORE_PATH, fingerprint, generate_cases, load_snapshot, validate_cases


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
                f'SELECT route, route_confidence, stage, agent_diagnostics, status, route_finalized, context_job_ids, decisions, error, '
                f'embedding_diagnostics, embedding IS NOT NULL AS route_embedding_present, reviewed_candidates, '
                f'missing_context, source_ids, attempts, intake_attempts, librarian_attempts, generation, '
                f'created_at, updated_at, lease_until '
                f'FROM "{self.schema}".memory_jobs WHERE id = :event_id '
                'AND user_id = :user_id AND character_id = :character_id'
            ), {**self.owner, "event_id": uuid.UUID(event_id)}).mappings().first()
            if row is None:
                return None
            return {"route": row["route"], "confidence": row["route_confidence"],
                    "status": row["status"], "route_finalized": row["route_finalized"],
                    "stage": row["stage"], "agent_diagnostics": row["agent_diagnostics"],
                    "context_job_ids": [str(item) for item in row["context_job_ids"]],
                    "decisions": row["decisions"] or [], "error": row["error"],
                    "embedding_diagnostics": row["embedding_diagnostics"] or [],
                    "route_embedding_present": row["route_embedding_present"],
                    **{key: row[key] for key in ("reviewed_candidates", "missing_context", "attempts",
                        "intake_attempts", "librarian_attempts", "generation")},
                    "source_ids": [str(value) for value in row["source_ids"]],
                    **{key: str(row[key]) if row[key] else None for key in ("created_at", "updated_at", "lease_until")}}

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
                "jobs": ("memory_jobs", "id, route, stage, status, route_finalized, context_job_ids, source_ids, generation, attempts, error"),
            }.items():
                name, fields = columns
                rows = connection.execute(text(f'SELECT {fields} FROM "{self.schema}".{name} '
                    'WHERE user_id = :user_id AND character_id = :character_id'), self.owner).mappings()
                state[table] = json.loads(json.dumps([dict(row) for row in rows], default=str))
        state["items"] = self.snapshot()
        return state


def memory_changes(before: dict, after: dict) -> dict:
    changes = {
        "created": {key: value for key, value in after.items() if key not in before},
        "updated": {key: value for key, value in after.items() if key in before and value != before[key]},
        "removed": [key for key in before if key not in after],
    }
    return {key: value for key, value in changes.items() if value}


async def wait_memory_job(store: MemoryRunStore, event_id: str | None, timeout: float = 30) -> dict:
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
    """建立帶 Asia/Taipei 時間戳的 run 目錄；不同測試類型可使用不同 root。"""
    root = root or RUNS_DIR
    root.mkdir(parents=True, exist_ok=True)
    name = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%d_%H%M%S")
    suffix = 1
    while True:
        run_dir = root / (name if suffix == 1 else f"{name}_{suffix}")
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            suffix += 1


def update_latest(run_dir: Path) -> str | None:
    """最新 Chat 測試入口只保存相對連結，歷史資料仍由時間資料夾擁有。"""
    latest = run_dir.parent / "latest"
    if latest.exists() and not latest.is_symlink():
        raise RuntimeError("latest 已有非連結資料，停止以避免覆蓋")
    previous = latest.resolve() if latest.is_symlink() else None
    previous_run = (previous.name if previous is not None and previous.is_dir()
                    and previous.parent == run_dir.parent.resolve() and previous != run_dir.resolve() else None)
    temporary = run_dir.parent / f".latest-{uuid.uuid4().hex}.tmp"
    try:
        temporary.symlink_to(run_dir.name, target_is_directory=True)
        temporary.replace(latest)
    finally:
        temporary.unlink(missing_ok=True)
    return previous_run


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
    store: MemoryRunStore, timeout: float,
) -> dict:
    before = await asyncio.to_thread(store.snapshot)
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
            job = await wait_memory_job(store, event_id, timeout=timeout)
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
    return {
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
        "memory_stage": job.get("stage") if job else None,
        "memory_agent_diagnostics": job.get("agent_diagnostics", []) if job else [],
        "memory_route": job.get("route") if job else None,
        "memory_route_confidence": job.get("confidence") if job else None,
        "memory_context_job_ids": job.get("context_job_ids", []) if job else [],
        "memory_decisions": job.get("decisions", []) if job else [],
        "memory_audit": audit,
        "memory_embedding_diagnostics": job.get("embedding_diagnostics", []) if job else [],
        "memory_route_embedding_present": job.get("route_embedding_present") if job else None,
        "memory_job_status": memory_status,
        "memory_reviewed_candidates": job.get("reviewed_candidates") if job else None,
        "memory_missing_context": job.get("missing_context") if job else None,
        "memory_source_ids": job.get("source_ids", []) if job else [],
        "memory_attempts": {key: job.get(key) for key in ("attempts", "intake_attempts", "librarian_attempts")} if job else None,
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


_EMBEDDING_PURPOSE_LABELS = {
    "retrieval_query": "對話檢索 query",
    "memory_match_query": "記憶比對 query",
    "context_document": "候選文件 document",
    "memory_document": "記憶文件 document",
}


def summarize_embedding_diagnostics(diagnostics: list[dict]) -> str:
    summaries = []
    for item in diagnostics:
        purpose = _EMBEDDING_PURPOSE_LABELS.get(item.get("purpose"), item.get("purpose", "未知類別"))
        status = "成功" if item.get("status") == "succeeded" else "失敗"
        dimension = f"{item['dimension']} 維" if item.get("dimension") is not None else "維度未知"
        duration = f"{item['duration_ms']} ms" if item.get("duration_ms") is not None else "耗時未知"
        error = f"，錯誤 {item['error_class']}" if item.get("error_class") else ""
        stage = f"（{item['stage']}）" if item.get("stage") else ""
        summaries.append(f"{purpose}{stage} {status}／{dimension}／{duration}{error}")
    return "；".join(summaries) if summaries else "無紀錄"


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


def read_turn_trace(run_dir: Path, event_id: str | None) -> list[dict]:
    path = run_dir / "memory" / "trace.jsonl"
    if not event_id or not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
            if uuid.UUID(item["event_id"]) == uuid.UUID(event_id):
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
    """以原始 prompt 區間與實際裁切區間交集歸因，包含只保留部分的記憶。"""
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
        for left, right in context["system_retained_ranges"]:
            lower, upper = max(start, left), min(start + len(text_value), right)
            if lower < upper:
                fragment = text_value[lower - start:upper - start]
                context["memory_fragments"].append({"id": projection["id"],
                    "destination": projection["destination"], "text": fragment,
                    "original_system_range": [lower, upper]})
                if projection["id"] not in context["injected_memory_ids"]:
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


def write_memory_report(records: list[dict], path: Path, metadata: dict, status: str, error: str | None) -> None:
    """寫入記憶／聊天摘要；逐輪完整 trace 不嵌入 Markdown。"""
    try:
        cases = {case["case_id"]: case for case in json.loads((path.parent / "cases.json").read_text(encoding="utf-8"))}
    except (OSError, ValueError, TypeError, KeyError):
        cases = {}
    lines = ["# 記憶與聊天案例觀察", "", f"- 執行狀態：**{status}**",
        f"- 案例：已完成 {len(metadata.get('completed_case_ids', []))} / {metadata.get('planned_cases', 0)}；"
        f"實際對話 {sum(record.get('action') != 'compress' for record in records)} / {metadata.get('planned_turns')} 輪",
        f"- 開始：{metadata['started_at']}；案例 SHA-256：`{metadata.get('cases_sha256', '-')}`",
        "- 語意品質供人工查閱；工作結案與注入均不代表回答正確或因果使用。",
        "- [表情／態度／JEV 報告](expression_report.md)；逐輪詳細來源：[turns.jsonl](turns.jsonl)",
        "- 案例快照：[cases.json](cases.json)；結案狀態：[case_states.jsonl](case_states.jsonl)；執行摘要：[run.json](run.json)", "",
        "| Case | 類型／來源／召回意圖 | 執行狀態 | 對話 | 提出操作／提交操作 | probe 注入筆數 |",
        "|---|---|---|---|---|---|"]
    for case in metadata.get("case_statuses", []):
        actual = [r for r in records if r.get("case_id") == case["case_id"] and r.get("action") != "compress"]
        material = cases.get(case["case_id"], {})
        proposed = Counter(d.get("action", "?") for r in actual for d in r.get("memory_decisions", []))
        committed = Counter(d.get("action", "?") for r in actual for d in r.get("memory_audit", []))
        injected = [injected_count(r) for r in actual if r.get("phase") == "probe"]
        lines.append(f"| [{case['case_id']}](#{case['case_id']}) | {material.get('case_type')} / {material.get('source')} / "
                     f"{material.get('recall_route')} | {case['status']} | {len(actual)} | {dict(proposed)} / {dict(committed)} | {injected} |")
    if error:
        lines.extend(["", f"停止原因：{error}"])
    if metadata.get("previous_run"):
        previous = metadata["previous_run"]
        lines.extend(["", f"歷史比較：[前一次記憶報告](../{previous}/memory_report.md)／"
                      f"[前一次表情報告](../{previous}/expression_report.md)／[前一次案例](../{previous}/cases.json)。",
                      "固定核心案例可逐案比較；新生成案例的內容可能不同，請先核對 cases.json 與模型設定。"])
    lines.extend(["", "## 逐輪索引", "",
                  "完整記憶 agent diagnostics、embedding、retrieval、audit、實際 messages 與回覆原文，請依 `case_id`＋`turn` 從 `turns.jsonl` 擷取。", ""])
    ordered_case_ids = [case["case_id"] for case in metadata.get("case_statuses", [])]
    ordered_case_ids.extend(case_id for case_id in dict.fromkeys(record.get("case_id", "legacy") for record in records)
                            if case_id not in ordered_case_ids)
    for case_id in ordered_case_ids:
        material = cases.get(case_id, {})
        case_records = [record for record in records if record.get("case_id") == case_id]
        focus = "、".join(material.get("expected_focus", [])) or "-"
        lines.extend([f'<a id="{case_id}"></a>', f"## {case_id}", "",
                      "觀察方向：" + focus, "",
                      "| Turn | Phase | 使用者 | 回覆 | Route／Job | 提出／提交 | 注入 | Event | 狀態 |",
                      "|---:|---|---|---|---|---|---:|---|---|"])
        for record in case_records:
            route_status = f"{record.get('memory_stage') or 'control'}:{record.get('memory_route') or '-'} / {record.get('memory_job_status') or '-'}"
            operations = f"{operation_counts([record], 'memory_decisions')} / {operation_counts([record], 'memory_audit')}"
            state = "錯誤" if record.get("errors") else "OK"
            if record.get("action") == "compress":
                state = "compress"
            elif record.get("memory_job_status"):
                state = f"{state}; {record['memory_job_status']}"
            lines.append("| " + " | ".join([
                markdown_cell(record.get("turn")), markdown_cell(record.get("phase")),
                markdown_cell(record.get("user")), markdown_cell(record.get("reply")),
                markdown_cell(route_status), markdown_cell(operations),
                markdown_cell(injected_count(record)),
                markdown_cell(record.get("memory_event_id")), markdown_cell(state),
            ]) + " |")
            if record.get("errors"):
                lines.append(f"|  |  | 錯誤：{markdown_cell(', '.join(record['errors']))} |  |  |  |  |  |  |")
        if not case_records:
            lines.append("| - | - | 尚未執行 | - | - | - | 0 | - | pending |")
        lines.append("")
    lines.extend([
        "## 詳細紀錄", "",
        "- `turns.jsonl`：每行一筆完整逐輪 evidence；使用 `jq` 以 `case_id`＋`turn` 精確擷取。",
        "- `case_states.jsonl`：每案 reset 前保存的 sources、evidence、relations、jobs 與 memory items。",
        "- `server.log`：只在排查後端 transport／runtime stdout 時查看，不作為記憶交易的唯一證據。",
        "",
    ])
    atomic_write_text(path, "\n".join(lines) + "\n")


def write_reports(records, run_dir, metadata, status, error):
    dialogue = [record for record in records if record.get("action") != "compress"]
    write_markdown_report(dialogue, run_dir / "expression_report.md", metadata, status, error)
    write_memory_report(records, run_dir / "memory_report.md", metadata, status, error)


def save_record(records: list[dict], record: dict, run_dir: Path, metadata: dict, status: str, error: str | None) -> None:
    records.append(record)
    with (run_dir / "turns.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


async def control_step(ws, action: str, session_id: str, timeout: float) -> dict:
    expected = {"reset": "reset_done", "compress": "compress_done"}[action]
    started = time.monotonic()
    await ws.send(json.dumps({"type": action, "session_id": session_id}))
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
    # 先驗證重播來源，執行時建立獨立資料夾並保留來源產物。
    legacy = bool(args.scenario and Path(args.scenario).suffix.lower() == ".txt")
    cases = None
    generator_metadata = {"status": "not_started"}
    if args.scenario:
        if legacy:
            inputs = load_scenario(args.scenario)
            if args.max_turns:
                inputs = inputs[:args.max_turns]
            cases = [{"case_id": "legacy", "recall_route": None, "conversation": [
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
        raise ValueError("--max-turns 僅供既有 TXT 表情回歸，不裁切 25-case 集")
    if not legacy and args.max_turns:
        raise ValueError("25-case 快照不得以 --max-turns 裁切")
    run_dir = create_run_dir()
    (run_dir / "memory").mkdir()
    for name in ("cases.json", "turns.jsonl", "case_states.jsonl", "memory_report.md", "expression_report.md", "server.log"):
        (run_dir / name).write_text("", encoding="utf-8")
    schema = "test_" + uuid.uuid4().hex
    user_id, character_id = uuid.uuid4(), uuid.uuid4()
    store = MemoryRunStore(database_url, schema, user_id, character_id)
    metadata = {"started_at": timestamp(), "scenario": str(Path(args.scenario).resolve()) if args.scenario else str(CORE_PATH),
        "scenario_sha256": fingerprint(cases) if cases is not None else None,
        "planned_cases": len(cases) if cases is not None else 25,
        "planned_turns": sum("input" in step for case in cases for step in case["conversation"]) if cases else None,
        "memory_schema": schema, "legacy": legacy, "generator": generator_metadata,
        "completed_case_ids": [], "case_statuses": [], "cleanup": {}, **model_metadata()}
    records = []
    status, error = "running", None
    process = log_file = ws = None
    current_case = None

    def persist(*, render_reports: bool = False):
        metadata.update(status=status, error=error, executed_cases=len({r["case_id"] for r in records}),
                        executed_turns=sum(r.get("action") != "compress" for r in records), updated_at=timestamp())
        (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        if render_reports:
            write_reports(records, run_dir, metadata, status, error)

    persist(render_reports=True)
    print(f"測試資料夾：{run_dir}")
    try:
        metadata["previous_run"] = update_latest(run_dir)
        persist(render_reports=True)
        if cases is None:
            core = validate_cases(json.loads(CORE_PATH.read_text(encoding="utf-8")), source="Gold", count=20)
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
                        record["control"] = await control_step(ws, "compress", session_id, args.turn_timeout)
                    except (TimeoutError, RuntimeError) as exc:
                        record["errors"].append(type(exc).__name__)
                else:
                    attempt_errors = []
                    for attempt in range(1, args.retries + 2):
                        record = await run_turn(ws, turn, step["input"], "Rushia", session_id, store, args.turn_timeout)
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
                    record["trace"] = read_turn_trace(run_dir, record.get("memory_event_id"))
                    # 成功角色輸出已由 repository 保存；只保留未提交嘗試的補充 trace。
                    record["trace"] = [item for item in record["trace"] if not (
                        item["stage"] == "memory_agent" and any(
                            all(diagnostic.get(key) == value for key, value in item.items()
                                if key not in {"event_id", "stage", "timestamp"})
                            for diagnostic in record.get("memory_agent_diagnostics", [])))]
                    if not legacy and not record["errors"] and not any(item["stage"] == "chat_context" for item in record["trace"]):
                        record["errors"].append("缺少裁切後 Chat context trace")
                    context = next((item for item in record["trace"] if item["stage"] == "chat_context"), None)
                    if case["recall_route"] == "long_term" and step["phase"] == "probe" and context and (
                            context.get("history_count", 0) != 0 or context.get("summary", "")):
                        record["errors"].append("長期 probe 的 history／summary 非空")
                record.update(case_id=case["case_id"], phase=step["phase"], recall_route=case["recall_route"])
                if record["errors"]:
                    case_has_errors = True
                    status = "interrupted" if record.get("interrupted") else "failed"
                    error = f"第 {turn} 輪失敗：{record['errors'][0]}"
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
        if not args.scenario:
            from infrastructure.ai_client import _role_clients
            for client in _role_clients.values():
                await client.close()
        persist(render_reports=True)
        print(f"報告已寫入：{run_dir / 'memory_report.md'}（{status}）")
    return run_dir, status


def main() -> None:
    parser = argparse.ArgumentParser(description="隔離式 Headless JEV Chat 測試")
    parser.add_argument("--scenario", help="25-case JSON 快照重播；TXT 保留既有表情回歸，省略時生成完整新集")
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
