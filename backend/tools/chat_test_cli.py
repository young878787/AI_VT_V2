"""在隔離後端執行多輪 Chat 測試，逐輪保存可追溯報告。"""

import argparse
import asyncio
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


BACKEND_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = BACKEND_ROOT / "log" / "chat_test_runs"
ENV_PATH = BACKEND_ROOT.parent / ".env"


def load_scenario(path: str) -> list[str]:
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def snapshot_memory(memory_dir: Path) -> dict:
    def read(path: Path):
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content

    return {
        "user_profile": read(memory_dir / "user_profile.json"),
        "memory_md": read(memory_dir / "memory.md"),
        "memory_records": read(memory_dir / "memory_records.json"),
    }


async def wait_memory_job(memory_dir: Path, event_id: str | None, timeout: float = 30) -> str | None:
    if not event_id:
        return None
    path = memory_dir / "memory_jobs" / f"{event_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("status") in {"done", "failed"}:
                return job["status"]
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        await asyncio.sleep(0.1)
    return "timeout"


def create_run_dir() -> Path:
    run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    run_dir = RUNS_DIR / run_id
    (run_dir / "memory").mkdir(parents=True)
    return run_dir


def model_metadata() -> dict:
    values = dotenv_values(ENV_PATH)
    return {
        "ai_provider": urlparse(values.get("CHAT_AI_BASE_URL") or "").hostname or "(unset)",
        "chat_model": values.get("CHAT_AI_MODEL") or "(unset)",
        "jev_model": values.get("JEV_AI_MODEL") or "jev-latest",
    }


def find_available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_backend(run_dir: Path, port: int) -> tuple[subprocess.Popen, object]:
    log_file = (run_dir / "server.log").open("w", encoding="utf-8")
    child_env = os.environ.copy()
    child_env["AI_VT_MEMORY_DIR"] = str((run_dir / "memory").resolve())
    child_env["AI_VT_TEST_MODE"] = "true"
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
    memory_dir: Path, timeout: float,
) -> dict:
    before = snapshot_memory(memory_dir)
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
                if message_type == "stream_end":
                    stream_complete = True
                    stream_end_sec = round(time.monotonic() - started, 2)
                if message_type == "error" or (stream_complete and expression is not None):
                    break
    except TimeoutError:
        errors.append(f"單輪逾時（>{timeout:g}s）")
    except (websockets.ConnectionClosed, OSError, ValueError) as exc:
        errors.append(f"連線或回應錯誤：{exc}")
    memory_status = await wait_memory_job(memory_dir, event_id) if not errors else None
    memory_completion_sec = round(time.monotonic() - started, 2) if memory_status else None
    after = snapshot_memory(memory_dir)
    return {
        "turn": turn,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "user": user_message,
        "reply": "".join(reply_parts).strip(),
        "emotion_state": emotion,
        "emotion_source": source,
        "expression": expression,
        "expression_debug": expression_debug,
        "memory_changes": {key: after[key] for key in after if after[key] != before[key]},
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
    if record["memory_changes"]:
        print(f"記憶變更: {', '.join(record['memory_changes'])}")
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
        f"- 測試記憶：`{path.parent / 'memory'}`",
        f"- 原始紀錄：`{path.parent / 'turns.jsonl'}`；後端日誌：`{path.parent / 'server.log'}`",
    ]
    if error:
        lines.append(f"- 停止原因：{error}")
    lines.extend(["", "| # | 使用者 | AI 回覆 | JEV 情緒六欄位 | 來源 | 表情 | 重試 | 記憶變更 | 錯誤 |",
                  "|---|---|---|---|---|---|---|---|---|"])
    for record in records:
        state = record.get("emotion_state") or {}
        scores = ", ".join(f"{key}={value:.2f}" for key, value in state.items()) or "-"
        cells = [
            record["turn"], record["user"], record["reply"], scores,
            record.get("emotion_source") or "-", record.get("expression") or "-",
            record.get("attempts", 1) - 1,
            ", ".join(record.get("memory_changes", {})) or "-",
            ", ".join(record.get("errors", [])) or "-",
        ]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in cells) + " |")
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
    scenario = load_scenario(args.scenario) if args.scenario else None
    run_dir = create_run_dir()
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
        port = find_available_port()
        url = f"ws://127.0.0.1:{port}/ws/chat"
        process, log_file = start_backend(run_dir, port)
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
                    ws, turn, user_message, args.model, session_id, run_dir / "memory", args.turn_timeout,
                )
                record["attempts"] = attempt
                if not record["errors"]:
                    break
                attempt_errors.extend(record["errors"])
                # 已回覆或逾時時不能安全重送，避免重複記憶寫入或與未完成請求競爭。
                if record["reply"] or "逾時" in record["errors"][0] or attempt > args.retries:
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
        error = str(exc)
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
