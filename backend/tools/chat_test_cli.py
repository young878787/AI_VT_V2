"""在隔離後端執行多輪 Chat 測試，逐輪保存可追溯報告。"""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
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
                f'SELECT id, canonical_text, memory_type, status FROM "{self.schema}".memory_items '
                'WHERE user_id = :user_id AND character_id = :character_id ORDER BY id'
            ), self.owner).mappings()
            return {str(row["id"]): {"text": row["canonical_text"], "type": row["memory_type"],
                                      "status": row["status"]} for row in rows}

    def job(self, event_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(text(
                f'SELECT route, route_confidence, status, route_finalized, buffered_job_ids, decisions, error '
                f'FROM "{self.schema}".memory_jobs WHERE id = :event_id '
                'AND user_id = :user_id AND character_id = :character_id'
            ), {**self.owner, "event_id": uuid.UUID(event_id)}).mappings().first()
            if row is None:
                return None
            return {"route": row["route"], "confidence": row["route_confidence"],
                    "status": row["status"], "route_finalized": row["route_finalized"],
                    "buffered_job_ids": [str(item) for item in row["buffered_job_ids"]],
                    "decisions": row["decisions"] or [], "error": row["error"]}

    def audit(self, event_id: str) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(text(
                f'SELECT action, target_id, reason_class, deleted_count FROM "{self.schema}".memory_audit '
                'WHERE user_id = :user_id AND character_id = :character_id '
                "AND operation_key LIKE :operation_key ORDER BY split_part(operation_key, ':', 2)::integer"
            ), {**self.owner, "operation_key": f"{event_id}:%"}).mappings()
            return [{"action": row["action"], "target_id": str(row["target_id"]) if row["target_id"] else None,
                     "reason": row["reason_class"], "deleted_count": row["deleted_count"]} for row in rows]


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


def create_run_dir() -> Path:
    run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    run_dir = RUNS_DIR / run_id
    (run_dir / "memory").mkdir(parents=True)
    return run_dir


def model_metadata() -> dict:
    values = dotenv_values(ENV_PATH)

    def configured(name: str) -> str | None:
        return os.environ.get(name, values.get(name))

    return {
        "ai_provider": urlparse(configured("CHAT_AI_BASE_URL") or "").hostname or "(unset)",
        "chat_model": configured("CHAT_AI_MODEL") or "(unset)",
        "jev_model": configured("JEV_AI_MODEL") or "jev-latest",
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
    child_env["MEMORY_STORAGE_BACKEND"] = "postgres"
    child_env["MEMORY_DATABASE_URL"] = database_url
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
    try:
        async with asyncio.timeout(timeout):
            await ws.send(json.dumps({
                "type": "chat", "content": user_message, "model_name": model_name,
                "session_id": session_id,
                "turn_id": turn_id,
            }, ensure_ascii=False))
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
    except TimeoutError:
        errors.append(f"單輪逾時（>{timeout:g}s）")
    except (websockets.ConnectionClosed, OSError, ValueError) as exc:
        errors.append(f"連線或回應錯誤：{exc}")
    job = await wait_memory_job(store, event_id, timeout=timeout) if not errors else None
    memory_status = job["status"] if job else None
    if job and memory_status in {"failed", "cancelled", "timeout", "missing"}:
        errors.append(job.get("error") or f"記憶工作狀態：{memory_status}")
    memory_completion_sec = round(time.monotonic() - started, 2) if job else None
    after = await asyncio.to_thread(store.snapshot)
    return {
        "turn": turn,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user": user_message,
        "reply": "".join(reply_parts).strip(),
        "emotion_state": emotion,
        "emotion_source": source,
        "expression": expression,
        "expression_debug": expression_debug,
        "memory_changes": memory_changes(before, after),
        "memory_event_id": event_id,
        "memory_route": job.get("route") if job else None,
        "memory_route_confidence": job.get("confidence") if job else None,
        "memory_buffered_job_ids": job.get("buffered_job_ids", []) if job else [],
        "memory_decisions": job.get("decisions", []) if job else [],
        "memory_audit": await asyncio.to_thread(store.audit, event_id) if event_id else [],
        "memory_job_status": memory_status,
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
    if record["errors"]:
        print(f"錯誤: {record['errors']}")


def write_markdown_report(records: list[dict], path: Path, metadata: dict, status: str, error: str | None) -> None:
    completed = sum(not record["errors"] for record in records)
    planned = metadata["planned_turns"]
    lines = [
        "# Headless Chat 測試報告", "",
        f"- Run ID：`{metadata['run_id']}`",
        f"- 狀態：**{status}**；已完成 {completed} / {planned if planned is not None else '不限'} 輪",
        f"- 開始：{metadata['started_at']}",
        f"- 更新：{datetime.now().isoformat(timespec='seconds')}",
        f"- Scenario：`{metadata['scenario']}`（SHA-256：`{metadata['scenario_sha256']}`）",
        f"- AI：{metadata['ai_provider']} / `{metadata['chat_model']}`；JEV：`{metadata['jev_model']}`",
        f"- 測試 DB schema：`{metadata['memory_schema']}`（報告產出後清理）",
        f"- 測試短期記憶：`{path.parent / 'memory'}`",
        f"- 原始紀錄：`{path.parent / 'turns.jsonl'}`；後端日誌：`{path.parent / 'server.log'}`",
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
        f"- 互動態度：{counts_text(attitude_counts)}",
        f"- 最終表情：{counts_text(expression_counts)}",
        f"- 需檢查輪次：{len(fallback_rows)} / {len(records)}",
        "", "## 20 輪決策總覽", "",
        "| # | 使用者 | AI 回覆 | 基礎情緒 | 互動態度 | 最終表情 | 最終態度 | 信心 | 狀態 |",
        "|---|---|---|---|---|---|---|---|---|",
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
        cells = [
            record["turn"], record["user"], record.get("reply") or "-",
            diagnostic_value("jevBaseEmotionChoice"),
            diagnostic_value("jevInteractionAttitudeChoice"),
            record.get("expression") or "-",
            diagnostic_value("jevResolvedAttitude"),
            f"B {diagnostic_value('jevBaseEmotionConfidence')} / A {diagnostic_value('jevInteractionAttitudeConfidence')}",
            status_text,
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in cells) + " |")
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
            lines.append(
                f"- 第 {record['turn']} 輪：{record['user']}（" + "; ".join(reasons) + "）"
            )
    else:
        lines.append("- 無")

    lines.extend(["", "## 詳細資料", ""])
    for record in records:
        debug = record.get("expression_debug") or {}
        summary = (
            f"第 {record['turn']} 輪｜{record['user']}｜"
            f"{debug.get('jevBaseEmotionChoice', '-')} + "
            f"{debug.get('jevInteractionAttitudeChoice', '-')} → "
            f"{record.get('expression') or '-'}"
        )
        detail = {
            "emotion_state": record.get("emotion_state") or {},
            "emotion_source": record.get("emotion_source") or "-",
            "reply": record.get("reply") or "",
            "expression_debug": debug,
            "memory_changes": record.get("memory_changes", {}),
            "memory_route": record.get("memory_route"),
            "memory_route_confidence": record.get("memory_route_confidence"),
            "memory_buffered_job_ids": record.get("memory_buffered_job_ids", []),
            "memory_decisions": record.get("memory_decisions", []),
            "memory_audit": record.get("memory_audit", []),
            "memory_job_status": record.get("memory_job_status"),
            "latency_memory_completion_sec": record.get("latency_memory_completion_sec"),
            "errors": record.get("errors", []),
        }
        lines.extend([
            "<details>", f"<summary>{summary}</summary>", "",
            "```json", json.dumps(detail, ensure_ascii=False, indent=2), "```", "",
            "</details>", "",
        ])
    lines.append("")
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)


def save_record(records: list[dict], record: dict, run_dir: Path, metadata: dict, status: str, error: str | None) -> None:
    records.append(record)
    with (run_dir / "turns.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
    write_markdown_report(records, run_dir / "report.md", metadata, status, error)


async def run(args: argparse.Namespace) -> tuple[Path, str]:
    database_url = test_database_url()
    scenario = load_scenario(args.scenario) if args.scenario else None
    run_dir = create_run_dir()
    schema = "test_" + uuid.uuid4().hex
    user_id, character_id = uuid.uuid4(), uuid.uuid4()
    store = MemoryRunStore(database_url, schema, user_id, character_id)
    scenario_bytes = Path(args.scenario).read_bytes() if args.scenario else b"interactive"
    planned = min(len(scenario), args.max_turns) if scenario is not None and args.max_turns > 0 else (
        len(scenario) if scenario is not None else args.max_turns or None
    )
    metadata = {
        "run_id": run_dir.name,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "scenario": str(Path(args.scenario).resolve()) if args.scenario else "互動模式",
        "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
        "planned_turns": planned,
        "memory_schema": schema,
        **model_metadata(),
    }
    (run_dir / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "turns.jsonl").touch()
    records: list[dict] = []
    status = "running"
    error = None
    process = log_file = ws = None
    session_id = f"test_{uuid.uuid4().hex}"
    write_markdown_report(records, run_dir / "report.md", metadata, status, error)
    print(f"測試資料夾：{run_dir}")
    try:
        store.open()
        port = find_available_port()
        url = f"ws://127.0.0.1:{port}/ws/chat"
        process, log_file = start_backend(run_dir, port, database_url, schema, user_id, character_id)
        ws = await connect_backend(url, process, args.startup_timeout)
        turn = 0
        while args.max_turns <= 0 or turn < args.max_turns:
            if scenario is not None:
                if turn >= len(scenario):
                    break
                user_message = scenario[turn]
            else:
                try:
                    user_message = input(f"\n[Turn {turn + 1}] You > ").strip()
                except EOFError:
                    break
                if user_message in ("/quit", "/exit"):
                    break
                if not user_message:
                    continue
            turn += 1
            attempt_errors = []
            for attempt in range(1, args.retries + 2):
                record = await run_turn(
                    ws, turn, user_message, args.model, session_id, store, args.turn_timeout,
                )
                record["attempts"] = attempt
                if not record["errors"]:
                    break
                attempt_errors.extend(record["errors"])
                # 已回覆或逾時時不能安全重送，避免重複記憶寫入或與未完成請求競爭。
                if record["reply"] or record.get("memory_event_id") or "逾時" in record["errors"][0] or attempt > args.retries:
                    break
                await ws.close()
                try:
                    ws = await connect_backend(url, process, args.startup_timeout)
                except Exception as exc:
                    record["errors"].append(f"重連失敗：{exc}")
                    break
                print(f"第 {attempt} 次失敗，沿用 session 重試本輪。")
            record["attempt_errors"] = attempt_errors
            if record["errors"]:
                error = f"第 {turn} 輪失敗：{record['errors'][0]}"
                status = "failed"
            save_record(records, record, run_dir, metadata, status, error)
            print_turn(record)
            if record["errors"]:
                break
        if status == "running":
            status = "completed"
    except asyncio.CancelledError:
        status = "interrupted"
        error = "使用者中斷測試"
        raise
    except KeyboardInterrupt:
        status = "interrupted"
        error = "使用者中斷測試"
    except Exception as exc:
        status = "failed"
        error = str(exc) if isinstance(exc, (RuntimeError, TimeoutError)) else type(exc).__name__
        print(f"測試失敗：{error}")
    finally:
        try:
            if ws is not None:
                await ws.close()
        except Exception as exc:
            print(f"關閉測試連線時發生錯誤：{exc}")
        finally:
            if process is not None:
                try:
                    stop_backend(process, log_file)
                except Exception as exc:
                    status = "failed"
                    error = f"關閉測試後端失敗：{exc}"
        write_markdown_report(records, run_dir / "report.md", metadata, status, error)
        try:
            store.close()
        except Exception as exc:
            status = "failed"
            error = f"清理測試 schema 失敗：{type(exc).__name__}"
            write_markdown_report(records, run_dir / "report.md", metadata, status, error)
        print(f"報告已寫入：{run_dir / 'report.md'}（{status}）")
    return run_dir, status


def main() -> None:
    parser = argparse.ArgumentParser(description="隔離式 Headless JEV Chat 測試")
    parser.add_argument("--model", default="Hiyori", help="Live2D 模型名稱")
    parser.add_argument("--scenario", help="一行一輪的對話腳本；省略時互動輸入")
    parser.add_argument("--max-turns", type=int, default=0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--startup-timeout", type=float, default=15)
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
