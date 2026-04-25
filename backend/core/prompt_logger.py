"""
Prompt 日誌工具：每輪對話後記錄輸入/輸出提示詞、工具調用、Token 數。
日誌路徑：backend/log/prompt.log
"""
import datetime
from pathlib import Path

# backend/log/prompt.log
_LOG_DIR = Path(__file__).resolve().parent.parent / "log"
_LOG_FILE = _LOG_DIR / "prompt.log"
_SEP = "=" * 72


def _ensure_dir() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


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
        turn_count:      本輪的 JPAF turn 編號。
        system_prompt:   送給 Dialogue Agent 的完整系統提示詞。
        user_message:    使用者輸入。
        dialogue_agent_output: Dialogue Agent 清理後的輸出。
        tool_names:      Expression Agent 與 Memory Agent 本輪呼叫的工具名稱清單。
        output_tokens:   Dialogue Agent + Expression Agent + Memory Agent 輸出 token 數估算。
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
        f"\n[EXPRESSION AGENT / MEMORY AGENT TOOL CALLS]  {tool_str}\n"
        f"[OUTPUT TOKENS (est.)]  {output_tokens}\n"
        f"{_SEP}\n"
    )

    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(block)
    except Exception as e:
        print(f"[PromptLogger] 寫入失敗: {e}")


def reset_log() -> None:
    """清空 prompt.log，寫入重置時間戳（還原記憶時呼叫）。"""
    _ensure_dir()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"# prompt.log — 重置於 {ts}\n")
        print("[PromptLogger] Log 已重置")
    except Exception as e:
        print(f"[PromptLogger] 重置失敗: {e}")
