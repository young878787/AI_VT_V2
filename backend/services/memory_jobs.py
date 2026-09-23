"""單程序持久化 Memory 工作；決策與每個寫入都可重跑。"""
import asyncio
import hashlib
import json
import os
import time
from uuid import uuid4

from core.config import MEMORY_DIR
from domain.agent_b_prompts import build_memory_prompt
from infrastructure.memory_store import _atomic_write, _write_lock, load_user_profile
from infrastructure.memory_records import append_record_once, archive_expired_records
from services.agent_tool_pipeline import (
    MEMORY_AGENT_ALLOWED_TOOL_NAMES,
    extract_agent_tool_calls,
    filter_tool_calls_for_pool,
    get_meaningful_memory_tool_arguments,
)
from services.chat_service import call_memory_agent
from services.memory_service import execute_profile_update
from services.memory_events import publish_memory_event
from services.memory_consolidation import consolidate_memory

JOB_DIR = os.path.join(MEMORY_DIR, "memory_jobs")
EPOCH_PATH = os.path.join(JOB_DIR, "epoch.json")
MAX_PENDING = 1000
MAX_ATTEMPTS = 3
_wake = asyncio.Event()
_worker: asyncio.Task | None = None


def _read(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return default


def _save(path: str, data: dict) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def current_epoch() -> int:
    return int(_read(EPOCH_PATH, {"epoch": 0})["epoch"])


def reset_epoch() -> int:
    with _write_lock:
        epoch = current_epoch() + 1
        _save(EPOCH_PATH, {"epoch": epoch})
        if os.path.isdir(JOB_DIR):
            for name in os.listdir(JOB_DIR):
                if not name.endswith(".json") or name == "epoch.json":
                    continue
                path = os.path.join(JOB_DIR, name)
                job = _read(path, None)
                if job and job.get("status") == "pending":
                    job["status"] = "cancelled"
                    _save(path, job)
        return epoch


def enqueue_input(session_id: str, turn_id: str, text: str, model_name: str, recent_dialogue: list[dict], source: str = "text", timestamp: float | None = None) -> str:
    with _write_lock:
        os.makedirs(JOB_DIR, exist_ok=True)
        pending = [name for name in os.listdir(JOB_DIR) if name.endswith(".json") and name != "epoch.json"
                   and (_read(os.path.join(JOB_DIR, name), {}) or {}).get("status") == "pending"]
        if len(pending) >= MAX_PENDING:
            raise RuntimeError("Memory 待辦容量已滿")
        event_id = uuid4().hex
        _save(os.path.join(JOB_DIR, event_id + ".json"), {
            "event_id": event_id, "epoch": current_epoch(), "session_id": session_id,
            "turn_id": turn_id, "text": text, "model_name": model_name,
            "recent_dialogue": recent_dialogue[-16:], "created_at": timestamp or time.time(),
            "source": source, "user_id": "default_user",
            "attempts": 0, "status": "pending", "operations": None,
        })
    _wake.set()
    return event_id


def _operations(calls: list[dict], event_id: str, model_name: str) -> list[dict]:
    filtered = filter_tool_calls_for_pool(calls, allowed_tool_names=MEMORY_AGENT_ALLOWED_TOOL_NAMES, label="Memory Agent")
    operations = []
    for index, call in enumerate(filtered):
        args = get_meaningful_memory_tool_arguments(call["name"], call["arguments"], model_name=model_name)
        if args is None:
            continue
        operation_id = hashlib.sha256(f"{event_id}:{index}".encode()).hexdigest()[:32]
        operations.append({"id": operation_id, "name": call["name"], "args": args, "done": False})
    return operations


async def process_job(path: str) -> None:
    job = _read(path, None)
    if not job or job["status"] != "pending" or job["epoch"] != current_epoch():
        return
    try:
        if job["operations"] is None:
            prompt = build_memory_prompt(job["text"], "", job["model_name"])
            recent = "\n".join(
                f"{item.get('role', '')}: {str(item.get('content', ''))[:200]}"
                for item in job.get("recent_dialogue", [])[-8:]
                if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
            )
            if recent:
                prompt += "\n\n近期對話（僅供消歧，不作長期事實）：\n" + recent[:1200]
            response = await call_memory_agent([
                {"role": "system", "content": prompt},
                {"role": "user", "content": "請分析用戶訊息，判斷是否需要記憶操作。"},
            ], job["model_name"])
            calls = extract_agent_tool_calls(response, model_name=job["model_name"], label="Memory Agent")
            with _write_lock:
                if job["epoch"] != current_epoch():
                    return
                job["operations"] = _operations(calls, job["event_id"], job["model_name"])
                _save(path, job)
        for operation in job["operations"]:
            if operation["done"]:
                continue
            with _write_lock:
                if job["epoch"] != current_epoch():
                    return
                args = operation["args"]
                if operation["name"] == "update_user_profile":
                    already_applied = operation["id"] in load_user_profile().get("_applied_operations", [])
                    execute_profile_update(args["action"], args["field"], args["value"], job["model_name"], operation["id"])
                    if not already_applied:
                        publish_memory_event("user.update", job["event_id"], job["turn_id"], changed_fields=[args["field"]])
                elif operation["name"] == "save_memory_note":
                    if append_record_once(args["content"], operation["id"], job["turn_id"], args):
                        publish_memory_event("memory.update", job["event_id"], job["turn_id"], memory_ids=[operation["id"]])
                operation["done"] = True
                _save(path, job)
        job["status"] = "done"
        _save(path, job)
        try:
            await consolidate_memory(job["epoch"], current_epoch)
        except Exception as exc:
            print(f"[Memory] 長期摘要失敗，保留原紀錄: {exc}")
    except Exception as exc:
        if job["epoch"] != current_epoch():
            return
        job["attempts"] += 1
        job["status"] = "failed" if job["attempts"] >= MAX_ATTEMPTS else "pending"
        job["error"] = str(exc)[:300]
        _save(path, job)
        print(f"[Memory] 工作 {job['event_id']} 失敗: {exc}")


async def _run_worker() -> None:
    last_archive = 0.0
    last_consolidation = 0.0
    while True:
        if time.monotonic() - last_archive >= 86400:
            try:
                archive_expired_records()
            except Exception as exc:
                print(f"[Memory] 封存檢查失敗: {exc}")
            last_archive = time.monotonic()
        os.makedirs(JOB_DIR, exist_ok=True)
        paths = [os.path.join(JOB_DIR, name) for name in sorted(os.listdir(JOB_DIR)) if name.endswith(".json") and name != "epoch.json"]
        for path in paths:
            job = _read(path, None)
            if job and job["status"] == "pending":
                await process_job(path)
        if time.monotonic() - last_consolidation >= 600:
            try:
                await consolidate_memory(current_epoch(), current_epoch)
            except Exception as exc:
                print(f"[Memory] 長期摘要重試失敗: {exc}")
            last_consolidation = time.monotonic()
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


def start_worker() -> None:
    global _worker
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_run_worker())
