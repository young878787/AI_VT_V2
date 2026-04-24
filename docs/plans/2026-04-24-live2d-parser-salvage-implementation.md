# Live2D Parser Salvage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make current Live2D tool calls resilient to malformed or partial JSON so expression behavior no longer collapses to safe defaults when Qwen emits slightly broken arguments.

**Architecture:** Keep the current tool set unchanged. Add tolerant parsing and schema-aware partial field salvage in `tool_arg_parser`, then wire existing `chat_ws.py` parsing to pass the tool name and current model so salvaged partial fields can still flow into the existing default-merge logic.

**Tech Stack:** Python 3.11+, unittest, FastAPI backend, OpenAI-compatible tool calling

**Design Doc:** `docs/plans/2026-04-24-live2d-prompt-vs-architecture-redesign.md`

---

## File Structure

### Modified Files
- `backend/services/tool_arg_parser.py` — tolerant normalization, schema-aware field salvage, parse API extension
- `backend/api/routes/chat_ws.py` — pass tool name and model name into parser without changing tool semantics
- `backend/tests/test_tool_arg_parser.py` — regression coverage for normalization and salvage paths

### Unchanged Files
- `backend/domain/tools/Hiyori.json` — keep current tool schema intact for Phase 1
- `backend/services/chat_service.py` — no call configuration changes in this phase
- `vtuber-web-app/**` — frontend runtime unchanged in this phase

---

## Task 1: Add failing parser regression tests

**Files:**
- Modify: `backend/tests/test_tool_arg_parser.py`

- [ ] **Step 1: Add a failing test for Python booleans and trailing commas**

Append this test to `backend/tests/test_tool_arg_parser.py`:

```python
    def test_parse_tool_call_arguments_recovers_python_bool_and_trailing_comma(self):
        raw_args = '{"eye_sync": False, "duration_sec": 2.5,}'

        parsed, was_normalized = parse_tool_call_arguments(raw_args)

        self.assertTrue(was_normalized)
        self.assertFalse(parsed["eye_sync"])
        self.assertEqual(parsed["duration_sec"], 2.5)
```

- [ ] **Step 2: Add a failing test for malformed Live2D partial salvage**

Append this test to `backend/tests/test_tool_arg_parser.py`:

```python
    def test_parse_tool_call_arguments_salvages_partial_live2d_fields(self):
        raw_args = (
            '{"head_intensity": 0.65, "blush_level": 0.45, "eye_sync": fal: 4.5, '
            '"mouth_form": 0.25, "brow_l_y": 00.15, "eye_l_smile": 0.35}'
        )

        parsed, was_normalized = parse_tool_call_arguments(
            raw_args,
            tool_name="set_ai_behavior",
            model_name="Hiyori",
        )

        self.assertTrue(was_normalized)
        self.assertEqual(parsed["head_intensity"], 0.65)
        self.assertEqual(parsed["blush_level"], 0.45)
        self.assertEqual(parsed["mouth_form"], 0.25)
        self.assertEqual(parsed["brow_l_y"], 0.15)
        self.assertEqual(parsed["eye_l_smile"], 0.35)
        self.assertNotIn("eye_sync", parsed)
    ```

- [ ] **Step 3: Run the parser test module and verify RED**

Run from `backend/`:

```bash
python -m unittest discover -s tests -p "test_tool_arg_parser.py" -v
```

Expected: FAIL because current parser does not accept the new signature and cannot recover these malformed argument cases.

---

## Task 2: Implement tolerant normalization and partial salvage

**Files:**
- Modify: `backend/services/tool_arg_parser.py`
- Modify: `backend/api/routes/chat_ws.py`

- [ ] **Step 1: Extend parser API to accept `tool_name` and `model_name`**

Update `parse_tool_call_arguments()` so the signature becomes:

```python
def parse_tool_call_arguments(
    raw_args: str,
    tool_name: str | None = None,
    model_name: str = "Hiyori",
) -> tuple[dict, bool]:
```

- [ ] **Step 2: Add minimal tolerant normalization before salvage**

Implement normalization for:

- leading-zero decimals like `00.15`
- Python booleans `True` / `False`
- Python `None`
- trailing commas before `}` or `]`

Keep the existing `json.loads()` happy-path first.

- [ ] **Step 3: Add schema-aware field salvage for malformed payloads**

When normalized JSON still fails and `tool_name` is present:

- load the current model schema
- locate the matching tool definition
- extract recoverable primitive fields by property name
- coerce numbers / booleans / strings based on schema type
- return only successfully recovered fields

If no fields can be recovered, re-raise the original `JSONDecodeError`.

- [ ] **Step 4: Pass tool metadata from `chat_ws.py` into the parser**

Update both Live2D and Memory tool parsing call sites to pass:

```python
parse_tool_call_arguments(
    tc.function.arguments or "",
    tool_name=tc.function.name,
    model_name=model_name,
)
```

For Memory tools, continue using the same `model_name` since memory tool definitions fall back to the default schema.

- [ ] **Step 5: Run the parser test module and verify GREEN**

Run from `backend/`:

```bash
python -m unittest discover -s tests -p "test_tool_arg_parser.py" -v
```

Expected: PASS for all parser regression tests.

---

## Task 3: Verify actual Phase 1 effect

**Files:**
- No code changes required

- [ ] **Step 1: Re-run parser tests with verbose output**

Run from `backend/`:

```bash
python -m unittest discover -s tests -p "test_tool_arg_parser.py" -v
```

Expected: PASS with coverage for normalized and salvaged malformed inputs.

- [ ] **Step 2: Manually inspect the expected runtime effect**

Confirm the following behavior remains true from current `chat_ws.py` logic:

- salvaged partial `set_ai_behavior` args still flow into `args.get(...)`
- missing fields continue to use safe defaults
- valid recovered fields now survive malformed tool payloads instead of being dropped wholesale

- [ ] **Step 3: Commit**

```bash
git add docs/plans/2026-04-24-live2d-parser-salvage-implementation.md backend/tests/test_tool_arg_parser.py backend/services/tool_arg_parser.py backend/api/routes/chat_ws.py
git commit -m "fix: salvage malformed live2d tool arguments"
```

---

## Notes

- This phase intentionally does **not** change tool schemas.
- This phase intentionally does **not** add new Live2D tools.
- This phase relies on the existing backend behavior merge pattern already present in `chat_ws.py`.
- If this phase proves stable, the next plan should cover `queue_expression_pulse` and prompt updates.
