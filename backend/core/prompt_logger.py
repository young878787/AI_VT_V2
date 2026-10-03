"""
Prompt 日誌工具：每輪對話後記錄輸入/輸出提示詞、工具調用、Token 數。
一般聊天日誌：backend/log/runtime/prompt.log；測試使用隔離短期目錄。
"""
import datetime
import json
import os
from contextvars import ContextVar
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "log" / "runtime"
_LOG_FILE = _LOG_DIR / "prompt.log"
_SEP = "=" * 72
trace_event = ContextVar("memory_test_event", default=None)


def test_log_dir() -> Path | None:
    if os.getenv("AI_VT_TEST_MODE", "").lower() == "true" and os.getenv("AI_VT_MEMORY_DIR"):
        return Path(os.environ["AI_VT_MEMORY_DIR"])
    return None


def trace(stage: str, data: dict, event_id=None) -> None:
    """只在隔離測試記錄結構化證據；不改 WS 或 DB 契約。"""
    directory = test_log_dir()
    event_id = event_id or trace_event.get()
    if directory is None or event_id is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "trace.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"event_id": str(event_id), "stage": stage,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                **data}, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        print(f"[Test trace] 寫入失敗: {type(exc).__name__}")


def _ensure_dir() -> None:
    (test_log_dir() or _LOG_DIR).mkdir(parents=True, exist_ok=True)


def log_turn(
    turn_count: int,
    system_prompt: str,
    user_message: str,
    dialogue_agent_output: str,
    tool_names: list[str],
    output_tokens: int,
) -> None:
    """記錄單輪對話到 prompt.log（append 模式）。

    Args:
        turn_count:      本輪的對話編號。
        system_prompt:   送給 Dialogue Agent 的完整系統提示詞。
        user_message:    使用者輸入。
        dialogue_agent_output: Dialogue Agent 清理後的輸出。
        tool_names:      本輪記憶流程診斷標籤。
        output_tokens:   Chat 輸出 token 數估算。
    """
    _ensure_dir()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tool_str = "、".join(tool_names) if tool_names else "（無）"

    block = (
        f"\n{_SEP}\n"
        f"Turn {turn_count}  |  {ts}\n"
        f"{_SEP}\n"
        f"[SYSTEM PROMPT]\n"
        f"{system_prompt}\n"
        f"\n[USER]\n"
        f"{user_message}\n"
        f"\n[DIALOGUE AGENT OUTPUT]\n"
        f"{dialogue_agent_output}\n"
        f"\n[MEMORY ROUTE]  {tool_str}\n"
        f"[OUTPUT TOKENS (est.)]  {output_tokens}\n"
        f"{_SEP}\n"
    )

    try:
        with open((test_log_dir() / "prompt.log") if test_log_dir() else _LOG_FILE, "a", encoding="utf-8") as f:
            f.write(block)
    except Exception as e:
        print(f"[PromptLogger] 寫入失敗: {e}")


def reset_log() -> None:
    """清空 prompt.log，寫入重置時間戳（還原記憶時呼叫）。"""
    _ensure_dir()
    if test_log_dir() is not None:
        return  # 每案 reset 不抹除本輪隔離診斷。
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"# prompt.log — 重置於 {ts}\n")
        print("[PromptLogger] Log 已重置")
    except Exception as e:
        print(f"[PromptLogger] 重置失敗: {e}")
