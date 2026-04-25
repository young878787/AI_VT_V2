# Agent 分層與職責重整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以低風險方式重整現有 Agent 邊界，完成命名對齊、移除 Expression Agent 對 `jpaf_state` 的直接依賴，並把 Expression / Memory 工具解析與執行從混池改為分池。

**Architecture:** 保留現有三個 Agent，不先增加更多 Agent。第一階段先讓 `build_live2d_prompt()` 只依賴 `user_message`、`agent_a_reply`、`previous_expression_state`、`emotion_state`，再把 `chat_ws.py` 內的 `all_calls` 混池流程切成 Expression / Memory 兩條獨立管線，最後再抽出較薄的 orchestrator 輔助函式，縮小 route handler 的責任。

**Tech Stack:** Python 3.10+, FastAPI, WebSocket, unittest, OpenAI-compatible tool calling

**Design Doc:** `docs/superpowers/specs/2026-04-24-agent-layering-responsibility-redesign-design.md`

---

## File Structure

### Modified Files
- `backend/domain/agent_b_prompts.py` — 將 Live2D prompt builder 重命名為 Expression prompt builder 語意，並移除 `jpaf_state` 依賴
- `backend/api/routes/chat_ws.py` — 改寫 Agent 命名註解、調整 Expression Agent 呼叫參數、拆分 Expression / Memory tool parsing 與 execution
- `backend/tests/test_live2d_prompt_config.py` — 更新 prompt 測試，確認不再依賴 `jpaf_state`
- `backend/core/prompt_logger.py` — 視需要調整記錄欄位命名，讓 log 中的 Agent 名稱與新責任對齊

### New Files
- `backend/services/agent_tool_pipeline.py` — 放置 Expression / Memory tool parsing 與結果整理的共用輔助函式

### Unchanged Files
- `backend/domain/agent_a_prompts.py` — Dialogue Agent 不變
- `backend/services/tool_arg_parser.py` — 沿用既有 tolerant parsing 能力
- `backend/services/chat_service.py` — 保留 LLM 呼叫設定，不在本階段增加新 Agent 呼叫介面
- `vtuber-web-app/**` — 本階段不做前端重構

---

## Task 1: 先讓 Expression Agent 與 `jpaf_state` 解耦

**Files:**
- Modify: `backend/domain/agent_b_prompts.py`
- Modify: `backend/api/routes/chat_ws.py`
- Modify: `backend/tests/test_live2d_prompt_config.py`

- [ ] **Step 1: 在測試中加入「不再接受 `jpaf_state`」的失敗案例**

修改 `backend/tests/test_live2d_prompt_config.py`，把所有 `build_live2d_prompt()` 呼叫中的 `jpaf_state=...` 參數移除，並把其中一個案例改成明確驗證 prompt 不再包含 `active_function` 與 `persona` 區塊：

```python
    def test_live2d_prompt_uses_previous_expression_and_drops_dialogue_internal_state(self):
        prompt = build_live2d_prompt(
            user_message="正常聊天就好",
            agent_a_reply="嗯，今天就慢慢聊吧。",
            previous_expression_state={
                "summary": "上一輪是淡淡微笑、雙眼自然、眉毛幾乎不動",
                "mouth_form": 0.12,
                "eye_sync": True,
            },
            emotion_state={
                "primary_emotion": "calm",
                "intensity": "low",
            },
            model_name="Hiyori",
        )

        self.assertIn("【上一個表情摘要】", prompt)
        self.assertIn("淡淡微笑", prompt)
        self.assertIn("優先參考上一個表情摘要來維持連續性", prompt)
        self.assertNotIn("active_function", prompt)
        self.assertNotIn("persona", prompt)
```

- [ ] **Step 2: 執行 Live2D prompt 測試並確認目前會失敗**

在 `backend/` 下執行：

```bash
python -m unittest discover -s tests -p "test_live2d_prompt_config.py" -v
```

Expected: FAIL，因為目前 `build_live2d_prompt()` 仍要求 `jpaf_state` 參數，且 prompt 仍含有 `active_function` / `persona` 內容。

- [ ] **Step 3: 修改 `build_live2d_prompt()` 介面，移除 `jpaf_state` 參數**

將 `backend/domain/agent_b_prompts.py` 中的函式簽名從：

```python
def build_live2d_prompt(
    user_message: str,
    agent_a_reply: str,
    previous_expression_state: dict | None,
    jpaf_state: dict | None,
    emotion_state: dict | None,
    model_name: str = DEFAULT_MODEL,
) -> str:
```

改成：

```python
def build_live2d_prompt(
    user_message: str,
    agent_a_reply: str,
    previous_expression_state: dict | None,
    emotion_state: dict | None,
    model_name: str = DEFAULT_MODEL,
) -> str:
```

並刪除 `active_fn` / `persona` 推導區塊與 prompt 中這段：

```python
【情緒狀態】
- active_function: {active_fn}
- persona: {persona}
```

同時把這句：

```python
請優先根據用戶的直接表情要求、emotion_state 與上一個表情摘要決定本輪表情；persona 只作為語氣風格背景，不作為安全參數模板。
```

改為：

```python
請優先根據用戶的直接表情要求、agent_a_reply 的語氣、emotion_state 與上一個表情摘要決定本輪表情，不要依賴任何上游內部人格狀態欄位。
```

- [ ] **Step 4: 修改 `chat_ws.py` 的 Expression Agent 呼叫點**

把 `backend/api/routes/chat_ws.py` 中的：

```python
                live2d_system = build_live2d_prompt(
                    user_message,
                    agent_a_text,
                    previous_expression_state,
                    jpaf_state,
                    emotion_state,
                    model_name,
                )
```

改成：

```python
                live2d_system = build_live2d_prompt(
                    user_message,
                    agent_a_text,
                    previous_expression_state,
                    emotion_state,
                    model_name,
                )
```

- [ ] **Step 5: 重新命名測試案例，讓名稱對齊新責任**

把以下舊測試名稱：

```python
    def test_live2d_prompt_uses_previous_expression_and_drops_persona_parameter_templates(self):
```

改成：

```python
    def test_live2d_prompt_uses_previous_expression_and_drops_dialogue_internal_state(self):
```

- [ ] **Step 6: 執行 Live2D prompt 測試並確認轉綠**

在 `backend/` 下執行：

```bash
python -m unittest discover -s tests -p "test_live2d_prompt_config.py" -v
```

Expected: PASS，且測試確認 prompt 只依賴 `user_message`、`agent_a_reply`、`previous_expression_state`、`emotion_state`。

---

## Task 2: 調整命名，讓責任模型與註解一致

**Files:**
- Modify: `backend/api/routes/chat_ws.py`
- Modify: `backend/domain/agent_b_prompts.py`
- Modify: `backend/core/prompt_logger.py`

- [ ] **Step 1: 在 `chat_ws.py` 中把註解與 log wording 對齊新命名**

將類似以下註解：

```python
# 步驟 3：Agent B-1 (Live2D) + B-2 (Memory) 並行呼叫
print(f"[{AI_PROVIDER.upper()}] Agent B: parallel live2d + memory...")
```

改為：

```python
# 步驟 3：Expression Agent + Memory Agent 並行呼叫
print(f"[{AI_PROVIDER.upper()}] Agent orchestration: parallel expression + memory...")
```

並把解析區塊註解從：

```python
# --- 解析 Live2D response ---
# --- 解析 Memory response ---
```

改為：

```python
# --- 解析 Expression Agent response ---
# --- 解析 Memory Agent response ---
```

- [ ] **Step 2: 在 `agent_b_prompts.py` 頂部註解補上新舊命名對照**

將檔頭註解調整為：

```python
"""
Agent B 系統 Prompt 組裝：拆分為兩個獨立 Agent。
  - Expression Agent（原 Agent B-1 / Live2D Agent）：表情控制
  - Memory Agent（原 Agent B-2 / Memory Agent）：記憶管理
資料來源：tools/{model_name}.json（每個模型獨立設定）。
"""
```

- [ ] **Step 3: 視需要調整 prompt logger 的欄位命名註解**

若 `backend/core/prompt_logger.py` 註解仍寫成：

```python
tool_names: Agent B 本輪呼叫的工具名稱清單。
```

改成：

```python
tool_names: Expression Agent 與 Memory Agent 本輪呼叫的工具名稱清單。
```

- [ ] **Step 4: 重新執行既有兩組測試確認命名調整未破壞功能**

在 `backend/` 下執行：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: PASS，確認命名調整僅影響註解、訊息與文字，不影響邏輯。

---

## Task 3: 拆出 Expression / Memory 分池解析

**Files:**
- Create: `backend/services/agent_tool_pipeline.py`
- Modify: `backend/api/routes/chat_ws.py`
- Modify: `backend/tests/test_tool_arg_parser.py`

- [ ] **Step 1: 新增分池解析輔助函式檔案**

建立 `backend/services/agent_tool_pipeline.py`，先加入最小可用的兩個函式：

```python
import json

from core.utils import strip_thinking
from services.chat_service import parse_xml_tool_calls
from services.tool_arg_parser import parse_tool_call_arguments


def extract_agent_tool_calls(response: object, model_name: str, label: str) -> list[dict]:
    calls: list[dict] = []
    if not getattr(response, "choices", None):
        return calls

    message = response.choices[0].message
    content = strip_thinking(message.content or "")
    xml_calls, _ = parse_xml_tool_calls(content)

    if message.tool_calls:
        for tc in message.tool_calls:
            args, _ = parse_tool_call_arguments(
                tc.function.arguments or "",
                tool_name=tc.function.name,
                model_name=model_name,
            )
            calls.append({"name": tc.function.name, "arguments": args})

    calls.extend(xml_calls)
    return calls


def summarize_tool_names(*tool_groups: list[dict]) -> list[str]:
    names: list[str] = []
    for group in tool_groups:
        names.extend(call["name"] for call in group)
    return names
```

這一版先不做 logging 或 print 代理，先把混池解析邏輯搬出來。

- [ ] **Step 2: 在 `chat_ws.py` 改成兩條獨立 call list**

把：

```python
all_calls: list[dict] = []
```

改成：

```python
expression_calls: list[dict] = []
memory_calls: list[dict] = []
```

並用新 helper 取代兩段重複解析邏輯：

```python
                expression_calls = extract_agent_tool_calls(
                    live2d_response,
                    model_name=model_name,
                    label="Expression Agent",
                )
                memory_calls = extract_agent_tool_calls(
                    memory_response,
                    model_name=model_name,
                    label="Memory Agent",
                )
```

- [ ] **Step 3: 調整缺少 `set_ai_behavior` 的檢查，只看 Expression Agent**

把：

```python
if not any(call["name"] == "set_ai_behavior" for call in all_calls):
```

改成：

```python
if not any(call["name"] == "set_ai_behavior" for call in expression_calls):
```

並把 log 從：

```python
print(f"[Agent B] all_calls 總數: {len(all_calls)}, names: {[c['name'] for c in all_calls]}")
```

改成：

```python
print(
    "[Agent orchestration] "
    f"expression_calls={len(expression_calls)}, memory_calls={len(memory_calls)}, "
    f"names={summarize_tool_names(expression_calls, memory_calls)}"
)
```

- [ ] **Step 4: 為新 helper 加一個最小回歸測試**

在 `backend/tests/test_tool_arg_parser.py` 末尾新增一個最小 smoke test，確認 `parse_tool_call_arguments()` 仍可支撐分池解析使用：

```python
    def test_parse_tool_call_arguments_keeps_tool_name_signature_stable(self):
        raw_args = '{"duration_sec": 2.0}'

        parsed, was_normalized = parse_tool_call_arguments(
            raw_args,
            tool_name="blink_control",
            model_name="Hiyori",
        )

        self.assertFalse(was_normalized)
        self.assertEqual(parsed["duration_sec"], 2.0)
```

- [ ] **Step 5: 執行 parser 與 prompt 測試，確認分池解析不破壞既有能力**

在 `backend/` 下執行：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: PASS，確認分池解析輔助檔案導入後，既有 parsing 與 prompt 測試仍正常。

---

## Task 4: 拆出 Expression / Memory 分池執行

**Files:**
- Modify: `backend/api/routes/chat_ws.py`
- Modify: `backend/services/agent_tool_pipeline.py`

- [ ] **Step 1: 先保留原邏輯，但把執行順序改成先 Expression 後 Memory**

將目前：

```python
for call in all_calls:
```

改為兩段：

```python
for call in expression_calls:
    ...

for call in memory_calls:
    ...
```

其中：

1. `set_ai_behavior`、`blink_control` 只允許出現在 `expression_calls`
2. `update_user_profile`、`save_memory_note` 只允許出現在 `memory_calls`

- [ ] **Step 2: 對錯池工具加入防呆 warning，不立即 raise**

在 `chat_ws.py` 中加入最小防呆：

```python
    if fn_name not in {"set_ai_behavior", "blink_control"}:
        print(f"[Expression Agent][WARN] unexpected tool: {fn_name}")
        continue
```

與：

```python
    if fn_name not in {"update_user_profile", "save_memory_note"}:
        print(f"[Memory Agent][WARN] unexpected tool: {fn_name}")
        continue
```

這一步先做隔離，不做更激進的例外拋出。

- [ ] **Step 3: 更新 log_turn 的 tool_names 來源**

把：

```python
tool_names=[c["name"] for c in all_calls],
```

改成：

```python
tool_names=summarize_tool_names(expression_calls, memory_calls),
```

- [ ] **Step 4: 重新執行全部後端單元測試**

在 `backend/` 下執行：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: PASS。

- [ ] **Step 5: 手動檢查 route handler 中是否已完全移除 `all_calls`**

確認 `backend/api/routes/chat_ws.py` 不再出現：

```python
all_calls
```

並改由 `expression_calls` / `memory_calls` 控制各自的解析、檢查、執行與記錄。

---

## Task 5: 先做最小版 orchestrator 抽離

**Files:**
- Modify: `backend/services/agent_tool_pipeline.py`
- Modify: `backend/api/routes/chat_ws.py`

- [ ] **Step 1: 在 helper 檔中補一個工具名稱彙整與缺失檢查函式**

在 `backend/services/agent_tool_pipeline.py` 補上：

```python
def has_required_expression_behavior(expression_calls: list[dict]) -> bool:
    return any(call["name"] == "set_ai_behavior" for call in expression_calls)
```

- [ ] **Step 2: 用 helper 取代 route handler 內的重複檢查邏輯**

把：

```python
if not any(call["name"] == "set_ai_behavior" for call in expression_calls):
```

改成：

```python
if not has_required_expression_behavior(expression_calls):
```

這一步的目標不是大重構，而是先把 orchestration 中可抽離的邏輯開始收斂到 service。

- [ ] **Step 3: 重新執行完整測試**

在 `backend/` 下執行：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: PASS。

- [ ] **Step 4: 手動驗證最小驗收條件**

檢查以下條件是否成立：

1. `build_live2d_prompt()` 已無 `jpaf_state` 參數
2. `chat_ws.py` 已無 `all_calls`
3. `chat_ws.py` 中已出現 `expression_calls` / `memory_calls`
4. 缺少 `set_ai_behavior` 的檢查只看 Expression Agent
5. logger 的 tool name 來源已改為分池彙整

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-04-24-agent-layering-responsibility-redesign-design.md docs/superpowers/plans/2026-04-24-agent-layering-responsibility-redesign.md backend/domain/agent_b_prompts.py backend/api/routes/chat_ws.py backend/tests/test_live2d_prompt_config.py backend/tests/test_tool_arg_parser.py backend/core/prompt_logger.py backend/services/agent_tool_pipeline.py
git commit -m "refactor: separate expression and memory agent boundaries"
```

---

## Self-Review Checklist

本計畫已覆蓋以下 spec 要求：

1. 問題彙總對應 Task 1-5 的分階段修改
2. 命名重整對應 Task 2
3. Expression Agent 去除 `jpaf_state` 依賴對應 Task 1
4. Expression / Memory 分池解析與執行對應 Task 3 與 Task 4
5. `chat_ws.py` 責任縮小的第一步對應 Task 5

本計畫未使用占位語，所有步驟都已對應到具體檔案與具體動作。

本計畫刻意不在這一輪做：

1. 前端改名或視覺調整
2. 新增更多 Agent
3. 大幅改寫 `chat_service.py` 的 provider 呼叫介面
