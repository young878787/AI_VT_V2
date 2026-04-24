# Live2D Prompt Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify the Live2D Agent prompt so expression decisions are driven primarily by direct user intent, current emotion, and previous expression state instead of persona-safe parameter templates.

**Architecture:** Remove persona-specific parameter examples from the Live2D prompt path and replace them with a lighter context model: `user_message`, `agent_a_reply`, `current emotion_state`, and a concise `previous_expression_state` summary. Keep the current tool set unchanged.

**Tech Stack:** Python 3.11+, FastAPI backend, unittest, JSON schema-backed prompt config

---

## File Structure

### Modified Files
- `backend/domain/agent_b_prompts.py` — simplify prompt composition and add previous-expression context
- `backend/api/routes/chat_ws.py` — pass previous behavior summary into the Live2D prompt builder
- `backend/tests/test_live2d_prompt_config.py` — verify simplified prompt behavior
- `backend/domain/tools/Hiyori.json` — remove persona-safe parameter template dependence from prompt config

### Unchanged Files
- `backend/services/tool_arg_parser.py` — parser salvage remains as-is
- `vtuber-web-app/**` — no frontend changes in this phase

---

## Notes

- This phase intentionally keeps `set_ai_behavior` and `blink_control` only.
- This phase is meant to reduce conflicting prompt signals, not add new animation layers.
- If the prompt is still too conservative after simplification, the next step should be `queue_expression_pulse` rather than more persona tuning.
