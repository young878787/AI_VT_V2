"""四組 AI 驗收唯一入口；僅建立隔離測試 instance 與臨時 schema。"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import websockets
from dotenv import dotenv_values, load_dotenv
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
from domain.jev_questions import JEV_ROLES  # noqa: E402
RUNS_DIR = BACKEND_ROOT / "log" / "ai_tests"
JEV_TRACE_WAIT_SEC = 15.0
ENV_PATH = BACKEND_ROOT.parent / ".env"


def timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")


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
                f', embedding_contract, importance, confidence, retention_class, keywords, '
                f'md5(embedding::text) AS embedding_md5 '
                f'FROM "{self.schema}".memory_items '
                'WHERE user_id = :user_id AND character_id = :character_id ORDER BY id'
            ), self.owner).mappings()
            return {str(row["id"]): {"text": row["canonical_text"], "type": row["memory_type"],
                                      "status": row["status"], "has_embedding": row["has_embedding"],
                                      "embedding_model": row["embedding_model"],
                                      **{key: row[key] for key in ("embedding_contract", "importance", "confidence", "retention_class", "keywords", "embedding_md5")},
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
                "audit": ("memory_audit", "action, target_id, operation_key, reason_class, decision, source_event_id, deleted_count"),
                "jobs": ("memory_jobs", "id, conversation_id, route, status, route_finalized, context_job_ids, source_ids, generation, attempts, error"),
                "generation": ("memory_scope_state", "generation"),
                "barriers": ("memory_forget_barriers", "subject_hash, fact_hash, created_at"),
            }.items():
                name, fields = columns
                rows = connection.execute(text(f'SELECT {fields} FROM "{self.schema}".{name} '
                    'WHERE user_id = :user_id AND character_id = :character_id'), self.owner).mappings()
                state[table] = sorted(json.loads(json.dumps([dict(row) for row in rows], default=str)),
                                      key=lambda row: json.dumps(row, sort_keys=True))
        state["items"] = self.snapshot()
        # 全列指紋涵蓋未投影的metadata、向量與lease等；報告不複製1024維向量。
        with self.engine.connect() as connection:
            state["row_hashes"] = {}
            for table in ("memory_items", "memory_sources", "memory_evidence", "memory_relations", "memory_audit",
                          "memory_jobs", "memory_scope_state", "memory_forget_barriers"):
                state["row_hashes"][table] = sorted(connection.execute(text(
                    f'SELECT md5(to_jsonb(row)::text) FROM "{self.schema}".{table} AS row '
                    'WHERE user_id = :user_id AND character_id = :character_id'), self.owner).scalars())
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


def archive_run_dir(latest: Path, history: Path) -> Path:
    """把舊 latest 原樣搬到 history/<開始時間>/；搬移失敗直接拋出，不覆寫舊結果。"""
    try:
        run = json.loads((latest / "run.json").read_text(encoding="utf-8-sig"))
        moment = datetime.fromisoformat(run["execution"]["started_at"]).astimezone()
    except (OSError, ValueError, TypeError, KeyError):
        moment = datetime.fromtimestamp(latest.stat().st_mtime)
    name = moment.strftime("%Y%m%d-%H%M%S")
    history.mkdir(parents=True, exist_ok=True)
    target, suffix = history / name, 2
    while target.exists():
        target, suffix = history / f"{name}-{suffix}", suffix + 1
    latest.rename(target)
    return target


def create_run_dir(root: Path | None = None) -> Path:
    """固定最新產物；開新一輪前封存舊 latest，只解除舊 latest 連結，不搬移連結指向的歷史。"""
    root = root or RUNS_DIR
    root.mkdir(parents=True, exist_ok=True)
    latest = root / "latest"
    if latest.is_symlink():
        latest.unlink()
    elif latest.is_dir() and any(latest.iterdir()):
        archive_run_dir(latest, root / "history")
    latest.mkdir(exist_ok=True)
    return latest


def write_jev_questions(run_dir: Path) -> dict:
    """保存三角色 JEV 題目全文；逐輪 trace 只以 question_hash 引用。"""
    from domain.jev_questions import JEV_DECISION_CRITERIA_VERSION, build_role_questions, question_hash
    payload = {"schema_version": 1, "criteria_version": JEV_DECISION_CRITERIA_VERSION,
               "questions": {question_hash(questions): {"role": role, "questions": questions}
                             for role, questions in build_role_questions().items()}}
    atomic_write_text(run_dir / "jev_questions.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


async def wait_jev_trace(run_dir: Path, turn_id: str, timeout: float = JEV_TRACE_WAIT_SEC) -> None:
    """動作／記憶角色可能在 stream_end 後才寫 trace；送下一輪前等三角色紀錄齊全。"""
    path = run_dir / "memory" / "trace.jsonl"
    deadline = time.monotonic() + timeout
    while True:
        roles = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("turn_id") == turn_id and item.get("stage") == "jev":
                    roles.add(item.get("role"))
        if JEV_ROLES <= roles or time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.1)


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
    child_env["AI_VT_TEST_FIXTURE_PATH"] = str((run_dir / "fixture.json").resolve())
    child_env["ASR_ENABLED"] = "false"
    child_env["TTS_ENABLED"] = "false"
    # 用既有壓縮服務處理精簡fixture；保留最後一輪，不增加setup聊天。
    child_env["CHAT_COMPRESSION_KEEP_RECENT_MESSAGES"] = "2"
    child_env["CHAT_COMPRESSION_TRIGGER_MESSAGES"] = "16"
    command = [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)]
    if sys.platform == "win32":
        # psycopg async 不支援 Windows 的 ProactorEventLoop。
        command.extend(["--loop", "asyncio:SelectorEventLoop"])
    try:
        process = subprocess.Popen(
            command,
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
            front_port = os.environ.get("FRONTEND_PORT", dotenv_values(ENV_PATH).get("FRONTEND_PORT") or "5173")
            return await asyncio.wait_for(websockets.connect(url, max_size=10 * 1024 * 1024,
                origin=f"http://localhost:{front_port}"), timeout=2)
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
                "turn_id": turn_id,
                **({"test_mode": test_mode.value, "test_session_alias": session_id} if test_mode else {}),
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
    except Exception as exc:
        errors.append(f"單輪執行失敗：{type(exc).__name__}")
    interrupted = "使用者中斷測試" in errors
    job = None
    memory_wait_started = time.monotonic()
    try:
        if event_id and not interrupted:
            job = await wait_memory_job(store, event_id)
    except asyncio.CancelledError:
        interrupted = True
        errors.append("使用者中斷測試")
    except Exception as exc:
        errors.append(f"讀取記憶工作失敗：{type(exc).__name__}")
    memory_status = job["status"] if job else None
    memory_wait_sec = round(time.monotonic() - memory_wait_started, 3)
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
    try:
        state_after = await asyncio.to_thread(store.case_state) if test_mode else None
    except Exception as exc:
        state_after = None
        errors.append(f"讀取 DB 狀態失敗：{type(exc).__name__}")
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
        "memory_changes": {key: list(value) for key, value in memory_changes(before, after).items()},
        "memory_event_id": event_id,
        "memory_agent_diagnostics": job.get("agent_diagnostics", {}) if job else {},
        "memory_route": job.get("route") if job else None,
        "memory_route_confidence": job.get("confidence") if job else None,
        "memory_context_job_ids": job.get("context_job_ids", []) if job else [],
        "memory_audit": [{key: row[key] for key in ("action", "target_id", "operation_key", "deleted_count", "reason")} for row in audit],
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
        "job_wait_sec": memory_wait_sec,
        "duration_sec": round(time.monotonic() - started, 2),
    }


def atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


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
    requests = [item for item in records if item["stage"] == "chat_request"]
    if context is not None and requests:
        final = requests[-1]
        context.update({key: final[key] for key in ("messages", "message_origins", "system_retained_ranges", "token_estimate")})
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


async def control_step(ws, action: str, session_id: str, timeout: float, test_mode=None) -> dict:
    expected = {"reset": "reset_done", "compress": "compress_done"}[action]
    started = time.monotonic()
    await ws.send(json.dumps({"type": action,
                              **({"test_mode": test_mode.value, "test_session_alias": session_id}
                                 if test_mode else {})}))
    async with asyncio.timeout(timeout):
        while True:
            message = json.loads(await ws.recv())
            if message.get("type") == "error":
                raise RuntimeError(f"{action} 控制失敗")
            if message.get("type") == expected:
                return {"action": action, "acknowledged": True,
                        "status": message.get("status"), "reason": message.get("reason"),
                        "duration_sec": round(time.monotonic() - started, 3)}


def provenance(dataset):
    from tools.ai_testset import fingerprint
    from infrastructure.memory_repository import CHAT_RETRIEVAL_LIMIT, CHAT_RETRIEVAL_MIN_SIMILARITY
    from services.memory_retriever import MEMORY_CHAR_BUDGET
    root = BACKEND_ROOT.parent

    def git(*args):
        response = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
        return response.stdout

    files = [*BACKEND_ROOT.glob("tools/ai_test*.py"), *BACKEND_ROOT.glob("tools/ai_test_cases/**/*.json"),
             BACKEND_ROOT / "tools/chat_test_cli.py", BACKEND_ROOT / "domain/agent_a_prompts.py",
             BACKEND_ROOT / "services/chat_service.py", BACKEND_ROOT / "domain/jev_questions.py",
             BACKEND_ROOT / "services/memory_llm.py", *BACKEND_ROOT.glob("tools/ai_test_cases/assets/*")]
    return {"git_commit": git("rev-parse", "HEAD").decode().strip(),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
            "patch_sha256": hashlib.sha256(git("diff", "HEAD", "--", "backend")).hexdigest(),
            "file_sha256": {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in files if p.is_file()},
            "cases_sha256": fingerprint(dataset), "models": model_metadata(),
            "reference_time": dataset.get("reference_time"),
            "retrieval": {"limit": CHAT_RETRIEVAL_LIMIT, "min_similarity": CHAT_RETRIEVAL_MIN_SIMILARITY,
                          "projection_char_budget": MEMORY_CHAR_BUDGET},
            "compression": {"keep_recent_messages": 2, "trigger_messages": 16}}


def persist_jsonl(path, rows):
    atomic_write_text(path, "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows))


def update_cost(metadata, records, loader, started):
    roles = {}
    for role, stage in (("chat", "chat"), ("jev", "jev")):
        traces = []
        for record in records:
            if role == "chat":
                requests = []
                for item in record.get("trace", []):
                    if item.get("stage") == "chat_request":
                        requests.append({"usage": None, "duration_sec": None, "injected": False})
                    elif requests and item.get("stage") == "context_test_fault":
                        requests[-1]["injected"] = True
                    elif requests and item.get("stage") in {"context_tool_selection", "chat"}:
                        requests[-1].update({key: item.get(key) for key in ("usage", "duration_sec", "model")})
                if requests:
                    # 工具選擇與無工具時的chat完成是同一次request；按請求區間計一次。
                    traces.extend(request for request in requests if not request["injected"])
                    continue
            injected_failure = False
            for item in record.get("trace", []):
                if item.get("stage") == "context_test_fault":
                    injected_failure = True
                if item.get("stage") == stage:
                    if stage == "chat" and injected_failure and item.get("error") == "BadRequestError":
                        injected_failure = False
                        continue  # 拒收替身未送至provider，不列為實際模型呼叫。
                    traces.append(item)
        usages = [t.get("usage") for t in traces]
        roles[role] = {"calls": len(traces), "input_tokens": sum(u.get("prompt_tokens", 0) for u in usages)
                       if usages and all(u is not None for u in usages) else None,
                       "output_tokens": sum(u.get("completion_tokens", 0) for u in usages)
                       if usages and all(u is not None for u in usages) else None,
                       "duration_sec": sum(t["duration_sec"] for t in traces)
                       if all(isinstance(t.get("duration_sec"), (int, float)) for t in traces) else None}
    controls = [r["control"] for r in records if r.get("control", {}).get("action") == "compress"]
    roles["summary"] = {"calls": None if any(c.get("status") in {"failed", "cancelled"} for c in controls)
                        else sum(c.get("status") == "success" for c in controls), "input_tokens": None, "output_tokens": None,
                        "duration_sec": sum(c.get("duration_sec", 0) for c in controls)}
    diagnostics = [r["memory_agent_diagnostics"] for r in records if r.get("memory_agent_diagnostics")]
    roles["memory_agent"] = {"calls": sum(d.get("calls", 0) for d in diagnostics),
                             "input_tokens": sum(d["input_tokens"] for d in diagnostics)
                             if diagnostics and all("input_tokens" in d for d in diagnostics) else None,
                             "output_tokens": sum(d["output_tokens"] for d in diagnostics)
                             if diagnostics and all("output_tokens" in d for d in diagnostics) else None}
    for role in ("generator",):
        info = metadata.get(role, {})
        requests = info.get("requests", [])
        usages = [request.get("usage") for request in requests]
        complete_usage = len(usages) == info.get("calls", 0) and bool(usages) and all(u is not None for u in usages)
        roles[role] = {"calls": info.get("calls", 0),
                       "input_tokens": sum(u.get("prompt_tokens", 0) for u in usages) if complete_usage else None,
                       "output_tokens": sum(u.get("completion_tokens", 0) for u in usages) if complete_usage else None,
                       "models": sorted({request["model"] for request in requests if request.get("model")})}
    jev_traces = [t for r in records for t in r.get("trace", []) if t.get("stage") == "jev"]
    by_role = {}
    for role in sorted(JEV_ROLES):
        selected = [t for t in jev_traces if t.get("role") == role]
        by_role[role] = {"calls": len(selected),
                         "duration_sec": round(sum(t["duration_sec"] for t in selected), 4)
                         if all(isinstance(t.get("duration_sec"), (int, float)) for t in selected) else None}
    # 三角色並行；各角色耗時加總不等於壁鐘時間。
    roles["jev"]["by_role"] = by_role
    retrievals = [t for r in records for t in r.get("trace", []) if t.get("stage") == "retrieval"]
    event_retrievals = sum(any(t.get("stage") == "retrieval" for t in r.get("trace", []))
                           for r in records if r.get("memory_event_id"))
    event_embedding = [r.get("memory_embedding_diagnostics", {}) for r in records if r.get("memory_event_id")]
    background_calls = (sum(d.get("calls", 0) for d in event_embedding) - event_retrievals)
    embedding = {"initial_documents": dict(loader.embedding_cost) if loader else None,
                 "retrieval_queries": {"calls": len(retrievals), "input_tokens": None},
                 "jobs_queries_and_documents": {"calls": background_calls if background_calls >= 0 else None, "input_tokens": None},
                 "startup_dimension_probe": {"calls": 1 if metadata.get("backend_ready") else None, "input_tokens": None}}
    embedding_counts = [part.get("calls") if isinstance(part, dict) else None for part in embedding.values()]
    embedding["calls"] = sum(embedding_counts) if all(isinstance(value, int) for value in embedding_counts) else None
    metadata["cost"] = {"roles": roles, "embedding": embedding,
                        "job_wait_sec": sum(r.get("job_wait_sec") or 0 for r in records),
                        "wall_clock_sec": round(time.monotonic() - started, 3),
                        "note": "未提供usage的成本為null；階段累計不等於壁鐘時間，embedding含fixture載入，query與worker另見trace。"}


async def run(args):
    from domain.memory_settings import load_memory_settings
    from tools.ai_testset import load_suite, select_cases, generate_followup
    from tools.ai_test_evidence import case_mode, check_turn, context_trace
    from tools.ai_test_fixtures import FixtureLoader
    from tools.ai_test_report import initialize_report, write_reports

    # 重播latest時先完整讀取及驗證，再覆寫固定產物。沒有suite不啟動任何依賴。
    dataset = load_suite(args.suite, args.scenario)
    selected = select_cases(dataset, args.case)
    dataset["selection"] = [c["case_id"] for c in selected]
    load_dotenv(ENV_PATH, override=False)
    run_dir = create_run_dir(RUNS_DIR / args.suite)
    write_jev_questions(run_dir)
    manifest_path = run_dir / "fixture.json"
    memory_dir = run_dir / "memory"
    memory_dir.mkdir(exist_ok=True)
    for name in ("trace.jsonl", "prompt.log"):
        (memory_dir / name).write_text("", encoding="utf-8")
    (run_dir / "server.log").write_text("", encoding="utf-8")
    manifest_path.unlink(missing_ok=True)
    metadata = {"suite": args.suite, "cases": dataset["cases"], "started_at": timestamp(),
                "planned_case_ids": [c["case_id"] for c in dataset["cases"]],
                "selected_case_ids": dataset["selection"], "provenance": provenance(dataset),
                "dependencies": {"chat": "live", "jev": "live", "memory_agent": "live" if args.suite in {"memory", "integration"} else "disabled",
                                 "query_embedding": "live" if args.suite in {"memory", "integration"} else "disabled",
                                 "document_embedding": "fixed_cache_initial_corpus;live_operation_results",
                                 "tools": "live;it_012_vision_fault" if args.suite == "integration" else "fixture"},
                "generator": {"calls": 0}, "cleanup": {}}
    initialize_report(run_dir, args.suite, dataset, metadata["provenance"])
    records, states = [], []
    execution, error = "running", None
    process = log_file = ws = store = loader = None
    started = time.monotonic()
    active_scenario = active_session = None
    current_case_id = None
    failed_dependencies = set()

    def persist():
        persist_jsonl(run_dir / "turns.jsonl", records)
        persist_jsonl(run_dir / "case_states.jsonl", states)
        atomic_write_text(run_dir / "cases.json", json.dumps(dataset, ensure_ascii=False, indent=2))
        update_cost(metadata, records, loader, started)
        return write_reports(run_dir, metadata, records, states, execution, error, metadata["cleanup"])

    persist()
    print(f"測試組：{args.suite}；本次選取 {len(selected)} / {len(dataset['cases'])} 題（含必要前序）", flush=True)
    try:
        database_url = test_database_url()
        schema, user_id, character_id = "test_" + uuid.uuid4().hex, uuid.uuid4(), uuid.uuid4()
        metadata["provenance"]["test_scope"] = {"schema": schema, "user_id": str(user_id),
                                                 "character_id": str(character_id), "database_guard": "verified_distinct_database"}
        store = MemoryRunStore(database_url, schema, user_id, character_id)
        await asyncio.to_thread(store.open)
        settings = load_memory_settings({**os.environ, "AI_VT_TEST_MODE": "true", "MEMORY_TEST_DATABASE_URL": database_url,
                                         "MEMORY_DATABASE_SCHEMA": schema, "MEMORY_DEFAULT_USER_ID": str(user_id),
                                         "MEMORY_DEFAULT_CHARACTER_ID": str(character_id)})
        metadata["provenance"]["embedding_contract"] = {
            "model": settings.embedding_model, "serving_model": settings.embedding_serving_model,
            "dimension": settings.embedding_dimension, "query_prefix": settings.embedding_query_prefix,
            "document_prefix": settings.embedding_document_prefix, "normalization": "l2-v1", "sha256": settings.embedding_contract}
        loader = FixtureLoader(store, manifest_path=manifest_path, settings=settings)
        port = find_available_port()
        process, log_file = start_backend(run_dir, port, database_url, schema, user_id, character_id)
        url = f"ws://127.0.0.1:{port}/ws/chat"
        ws = await connect_backend(url, process, args.startup_timeout)
        metadata["backend_ready"] = True
        for index, case in enumerate(selected, 1):
            current_case_id = case["case_id"]
            dependencies = set(case.get("depends_on", [])) & failed_dependencies
            if dependencies:
                records.append({"case_id": case["case_id"], "source": case["source"], "phase": case["phase"],
                                "user": case["input"], "reply": "", "request_sent": False, "status": "failed",
                                "checks": [{"stage": "dependency", "status": "failed", "reason_code": "prerequisite_failed",
                                            "evidence": {"file": "turns.jsonl", "case_ids": sorted(dependencies)}}]})
                failed_dependencies.add(case["case_id"])
                persist()
                continue
            scenario = case["context"].get("scenario", case["case_id"])
            if scenario != active_scenario:
                await control_step(ws, "reset", "reset", args.turn_timeout)
                active_scenario, active_session = scenario, None
            fixture = await loader.prepare(case, dataset)
            session = fixture["session_id"]
            if session != active_session:
                await ws.close()
                ws = await connect_backend(url, process, args.startup_timeout)
                active_session = session
            mode = case_mode(args.suite, case)
            controls = {}
            summary_before = summary_after = None
            compression = None
            if case["context"].get("compress_before"):
                compression_before = await asyncio.to_thread(read_session_state, store, session)
                summary_before = compression_before["summary"]
                controls = await control_step(ws, "compress", session, args.turn_timeout, mode)
                compression_after = await asyncio.to_thread(read_session_state, store, session)
                summary_after = compression_after["summary"]
                compression = {"before": compression_before, "after": compression_after,
                               "compressed_sequences": list(range(compression_before["summary_through_sequence"] + 1,
                                                                  compression_after["summary_through_sequence"] + 1)),
                               "source_texts": await asyncio.to_thread(read_compressed_texts, store, session,
                                   compression_before["summary_through_sequence"], compression_after["summary_through_sequence"])}
            if case["source"] == "generated" and not case["input"]:
                if dataset.get("input_source") == "replayed_inputs":
                    raise ValueError("重播選取到未解析生成槽，禁止重新生成")
                case["input"] = await generate_followup(case, records, metadata["generator"])
                next(c for c in dataset["cases"] if c["case_id"] == case["case_id"])["input"] = case["input"]
                persist()
            before = await asyncio.to_thread(store.case_state)
            foreign_before = await asyncio.to_thread(loader.other_owner_state)
            record = await run_turn(ws, index, case["input"], "Rushia", session, store, args.turn_timeout, mode)
            if record["request_sent"]:
                await wait_jev_trace(run_dir, record["turn_id"])
            state = record.pop("_db_state", None) or {}
            record.update(case_id=case["case_id"], source=case["source"], phase=case["phase"], test_mode=mode.value,
                          fixture=fixture["metadata"], control=controls, summary_before=summary_before, summary_after=summary_after)
            if case["source"] == "generated":
                record["generation"] = metadata["generator"].get("cases", {}).get(case["case_id"], {"input_source": "replayed_inputs"})
            if compression:
                record["compression"] = compression
            records.append(record)
            states.append({"case_id": case["case_id"], "turn_id": record["turn_id"], "state": state,
                           "baseline_difference": state_difference(before, state), "fixture_ids": dict(loader.memory_ids)})
            persist()
            record["trace"] = read_turn_trace(run_dir, record.get("turn_id"))
            context = context_trace(record)
            context["retained_summary"] = retained_summary(context)
            foreign_after = await asyncio.to_thread(loader.other_owner_state)
            check_turn(args.suite, case, record, records[:-1], before, state, loader.memory_ids)
            record["checks"].append({"stage": "isolation", "status": "passed" if foreign_before == foreign_after else "failed",
                                     "reason_code": "other_owner_unchanged", "evidence": {"file": "case_states.jsonl", "case_id": case["case_id"], "field": "other_owner_unchanged"}})
            if fixture["metadata"].get("reference_day_matches_runtime") is False:
                record["checks"].append({"stage": "fixture", "status": "failed", "reason_code": "reference_day_mismatch",
                                         "evidence": {"file": "turns.jsonl", "case_id": case["case_id"], "field": "fixture.reference_day_matches_runtime"}})
            record["assets"] = collect_assets(record, run_dir)
            states[-1]["other_owner_unchanged"] = foreign_before == foreign_after
            # 保存對話與實際證據供後期評讀；不呼叫語意評分模型。
            persist()
            if record.get("interrupted"):
                execution, error = "cancelled", "使用者中斷測試"
                break
            if any(c["status"] == "failed" for c in record["checks"]):
                record["status"] = "failed"
                failed_dependencies.add(case["case_id"])
            else:
                record["status"] = "completed"
            print(f"[{index}/{len(selected)}] {case['case_id']} 客觀檢查={record['status']}", flush=True)
            persist()
        if execution == "running":
            execution = "completed"
    except asyncio.CancelledError:
        execution, error = "cancelled", "使用者中斷測試"
    except Exception as exc:
        execution, error = "failed", f"{type(exc).__name__}：{safe_error(exc)}"
        metadata["failure"] = {"case_id": current_case_id, "stage": "runner", "exception": type(exc).__name__,
                               "frames": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                                          for f in traceback.extract_tb(exc.__traceback__)[-8:]]}
        recorded = {record["case_id"]: record for record in records}
        for failed_case in selected:
            case_id = failed_case["case_id"]
            if case_id in recorded and case_id != current_case_id:
                continue
            record = recorded.get(case_id)
            if record is None:
                record = {"case_id": case_id, "source": failed_case["source"], "phase": failed_case["phase"],
                          "user": failed_case["input"], "reply": "", "request_sent": False, "checks": []}
                records.append(record)
            record["status"] = "failed"
            record.setdefault("checks", []).append({"stage": "execution", "status": "failed",
                "reason_code": "runner_failure" if case_id == current_case_id or current_case_id is None else "run_terminated",
                "evidence": {"file": "run.json", "field": "execution.failure", "case_id": current_case_id}})
        print(error, flush=True)
    finally:
        if ws is not None:
            try:
                await ws.close()
                metadata["cleanup"]["websocket"] = "closed"
            except Exception:
                metadata["cleanup"]["websocket"] = "failed"
        if process is not None:
            try:
                await asyncio.to_thread(stop_backend, process, log_file)
                metadata["cleanup"]["backend"] = "stopped"
                metadata["cleanup"]["worker"] = "stopped_with_backend"
            except Exception:
                metadata["cleanup"]["backend"] = "failed"
        if store is not None:
            try:
                await asyncio.to_thread(store.close)
                metadata["cleanup"]["schema"] = "removed"
                metadata["cleanup"]["connection"] = "disposed"
            except Exception:
                metadata["cleanup"]["schema"] = "failed"
        try:
            if not memory_dir.resolve().is_relative_to(run_dir.resolve()):
                raise ValueError("暫存路徑越界")
            shutil.rmtree(memory_dir)
            manifest_path.unlink(missing_ok=True)
            metadata["cleanup"]["temporary"] = "removed"
        except Exception:
            metadata["cleanup"]["temporary"] = "failed"
        from infrastructure.ai_client import _role_clients
        for client in list(_role_clients.values()):
            await client.close()
        _role_clients.clear()
        metadata["finished_at"] = timestamp()
        result = persist()
        print(f"報告：{run_dir / 'report.md'}；執行={execution}；驗收={result['acceptance']['overall']}", flush=True)
    return run_dir, result["acceptance"]["overall"]


def safe_error(exc):
    # DB/HTTP例外常含DSN或headers；只保留我們定義且無連線資訊的診斷。
    value = str(exc)
    if isinstance(exc, (ValueError, RuntimeError, TimeoutError)) and not any(
            token in value.lower() for token in ("://", "api_key", "authorization", "password", "sk-")):
        return value[:600]
    return "詳細原因請核對隔離 server.log；敏感連線內容未輸出"


def read_session_summary(store, session):
    return read_session_state(store, session)["summary"]


def read_session_state(store, session):
    with store.engine.connect() as connection:
        row = connection.execute(text(f'SELECT summary,summary_through_sequence,next_message_sequence FROM "{store.schema}".chat_sessions '
            'WHERE id=:session AND user_id=:user_id AND character_id=:character_id'),
            {**store.owner, "session": session}).mappings().first()
        return dict(row) if row else {"summary": "", "summary_through_sequence": -1, "next_message_sequence": 0}


def retained_summary(context):
    summary = context.get("summary", "")
    start = context.get("summary_section_start")
    if start is None:
        return ""
    return "\n".join(summary[max(left, start)-start:min(right, start+len(summary))-start]
                     for left, right in context.get("system_retained_ranges", [])
                     if max(left, start) < min(right, start+len(summary)))


def read_compressed_texts(store, session, before_cursor, after_cursor):
    with store.engine.connect() as connection:
        return list(connection.execute(text(f'SELECT content FROM "{store.schema}".chat_messages '
            'WHERE session_id=:session AND user_id=:user_id AND character_id=:character_id '
            'AND sequence>:before AND sequence<=:after ORDER BY sequence'),
            {**store.owner, "session": session, "before": before_cursor, "after": after_cursor}).scalars())


def state_difference(before, after):
    from tools.ai_testset import fingerprint
    return {"baseline_sha256": fingerprint(before), "final_sha256": fingerprint(after),
            "changed_tables": [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]}


def collect_assets(record, run_dir):
    from hashlib import sha256
    assets = []
    for tool in record.get("tools", []):
        if tool.get("name") != "capture_screenshot":
            continue
        source = ((tool.get("result") or {}).get("data") or {}).get("source", {})
        if not isinstance(source, dict):
            continue
        ref = source.get("report_asset") or source.get("asset_path") or source.get("asset")
        if ref and tool.get("image_accepted"):
            path = (run_dir / ref).resolve()
            if path.is_relative_to(run_dir.resolve()) and path.is_file():
                assets.append({"path": ref, "sha256": sha256(path.read_bytes()).hexdigest()})
    return assets


def main(argv=None):
    parser = argparse.ArgumentParser(description="四組隔離AI測試；必須明確選suite，不執行舊28-case或TXT。")
    parser.add_argument("--suite", choices=("chat", "memory", "tools", "integration"))
    parser.add_argument("--scenario", help="重播該suite的完整cases.json，不重新生成續問")
    parser.add_argument("--case", help="定位單題並重播必要前序；保留全組分母")
    parser.add_argument("--startup-timeout", type=float, default=90)
    parser.add_argument("--turn-timeout", type=float, default=120)
    args = parser.parse_args(argv)
    if not args.suite:
        parser.print_help()
        return 0
    if args.startup_timeout <= 0 or args.turn_timeout <= 0:
        parser.error("timeout 必須大於0")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    _, status = asyncio.run(run(args))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
