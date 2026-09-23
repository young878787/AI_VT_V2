"""Memory Agent 的系統 Prompt 組裝。"""

from domain.tools.schema_loader import load_schema, DEFAULT_MODEL


def build_memory_prompt(
    user_message: str,
    ai_role_reply: str,
    model_name: str = DEFAULT_MODEL,
) -> str:
    memory_cfg = load_schema(model_name)["prompt_config"]["memory"]
    up = memory_cfg["update_user_profile"]
    sm = memory_cfg["save_memory_note"]

    field_lines = "\n".join(
        f"- {item['field']}：{item['description']}"
        for item in up["field_guide"]
    )
    example_lines = "\n".join(
        f'- 「{ex["input"]}」→ {ex["action"]}, {ex["field"]}, "{ex["value"]}"'
        for ex in up["examples"]
    )
    principle_lines = "\n".join(f"- {p}" for p in memory_cfg["principles"])

    return f"""你是{memory_cfg['system_role']}。
{memory_cfg['task_description']}

# 當前對話

【用戶的訊息】
{user_message}

【AI 角色的回覆】（僅供參考上下文，記憶判斷以用戶訊息為主）
{ai_role_reply}

# 工具使用規則

## update_user_profile — 積極使用
{up['description']}

欄位選擇指南：
{field_lines}

示例：
{example_lines}

## save_memory_note — 積極使用
{sm['description']}

記錄格式：{sm['format']}。

---
**重要原則**：
{principle_lines}"""
