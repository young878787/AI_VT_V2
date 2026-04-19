# Dual-Agent JPAF Architecture Design

> **Status:** Approved  
> **Date:** 2026-04-19  
> **Goal:** Split the single-agent backend into two specialized agents:  
> Agent A (JPAF Chat) handles personality-driven character expression,  
> Agent B (Tools) handles Live2D behavior, user profile, and memory management.

## Background

Current architecture uses a single LLM agent that simultaneously handles:
1. Character role-play dialog
2. Live2D expression tool calls (`set_ai_behavior`)
3. Memory updates (`update_user_profile`, `save_memory_note`)

This creates a bloated system prompt and conflicting responsibilities.
The JPAF framework (arXiv:2601.10025) exists in `sample/JPAF_prompts.py` but is not integrated into production.

## Architecture

### Sequential A -> B Flow

```
User Message
│
├─ Step 1: Agent A (JPAF Chat, streaming, NO tools)
│  ├─ System: VTuber character + JPAF personality framework
│  ├─ Output: streaming text + <jpaf_state> JSON
│  └─ Side-effect: update jpaf_state.json if reflection triggered
│
├─ Step 2: Agent B (Tools, non-streaming, WITH tools)
│  ├─ System: tool decision prompt + Agent A's reply + jpaf_state
│  ├─ Output: tool calls (set_ai_behavior required, others optional)
│  └─ Side-effect: update user_profile.json, memory.md
│
└─ Step 3: Post-processing
   ├─ Send behavior payload to frontend + display WS
   ├─ Token count + auto-compression
   ├─ Send stream_end
   └─ Background TTS synthesis
```

### Future: Parallel Mode (planned, not implemented)

Both agents run concurrently. Agent B uses only user message + chat history
(without Agent A's reply) for tool decisions. Lower latency but less accurate
expression mapping. To be evaluated after sequential mode is stable.

## Agent A: JPAF Chat Agent

### Responsibilities
- Generate character dialog with JPAF-driven personality
- Output `<jpaf_state>` JSON for emotion/persona tracking
- Manage JPAF BaseWeights evolution (Reflection mechanism)

### System Prompt Structure
1. VTuber character base (from existing `prompts.py`)
2. User profile section (dynamic, from `user_profile.json`)
3. Memory section (dynamic, from `memory.md` last 50 lines)
4. JPAF personality framework:
   - 8 Jungian functions with character behavior mappings
   - BaseWeights table
   - Three mechanisms (Coordination / Reinforcement-Compensation / Reflection)
   - `<thinking>` format for hidden reasoning
   - `<jpaf_state>` output format
5. Voice script techniques (TTS tips from existing prompt)
6. Behavioral guidelines (1-4 sentences, conversational)

### JPAF Integration (from arXiv:2601.10025)
- Constants: B=0.06, A=0.30, Dw=0.06
- 4 persona profiles: tsundere (default), happy, angry, seductive
- Each persona has: dominant/auxiliary types, BaseWeights, meta_override, character description
- First turn: full init prompt with complete JPAF explanation
- Subsequent turns: compact prompt with weights + rules reminder
- Persona auto-selection: LLM evaluates in `<thinking>`, outputs `suggested_persona` in jpaf_state

### LLM Call Config
- streaming=True
- tools=None (no function calling)
- temperature=0.85
- extra_body=EXTRA_BODY (same as current)

## Agent B: Tools Agent

### Responsibilities
- Decide and execute tool calls based on Agent A's output
- Control Live2D expressions via `set_ai_behavior`
- Update user profile and memory notes

### System Prompt Structure
1. Role: Live2D expression controller + memory manager
2. Context injection: Agent A's reply text + jpaf_state (active_function, suggested_persona)
3. User's original message
4. Tool usage rules + expression parameter quick-reference (from existing prompt)

### Tools (unchanged from current `domain/tools.py`)
1. `set_ai_behavior` - REQUIRED every response
2. `update_user_profile` - optional
3. `save_memory_note` - optional

### LLM Call Config
- streaming=False
- tools=tools, tool_choice="auto"
- temperature=0.85
- max_tokens=256
- extra_body=EXTRA_BODY

## Memory Architecture

```
backend/memory/
├── user_profile.json     ← Agent B writes (user's long-term profile)
├── memory.md             ← Agent B writes (conversation event log)
├── jpaf_state.json       ← Agent A writes (AI character personality evolution) [NEW]
└── sessions/             ← chat_ws writes (chat history, unchanged)
```

### jpaf_state.json Format
```json
{
  "dominant": "Ti",
  "auxiliary": "Ne",
  "base_weights": {"Ti": 0.40, "Ne": 0.24, "Fi": 0.06, "Si": 0.06, "Fe": 0.06, "Te": 0.06, "Se": 0.06, "Ni": 0.06},
  "current_persona": "tsundere",
  "turn_count": 0
}
```

### Shared Chat History
- Single `messages` list shared between both agents
- Agent A's cleaned reply (stripped of `<thinking>` and `<jpaf_state>`) is appended as `assistant` role
- Agent B does NOT add its own messages to history (it's a silent tool executor)
- Both agents see the same user/assistant history on next turn

## File Changes

| Action | File | Description |
|--------|------|-------------|
| CREATE | `backend/domain/jpaf.py` | JPAF core: session class, constants, persona profiles, parsers |
| CREATE | `backend/domain/agent_a_prompts.py` | Agent A system prompt builder |
| CREATE | `backend/domain/agent_b_prompts.py` | Agent B system prompt builder |
| MODIFY | `backend/core/config.py` | Add JPAF_STATE_PATH constant |
| MODIFY | `backend/infrastructure/memory_store.py` | Add load/save_jpaf_state() |
| MODIFY | `backend/services/chat_service.py` | Add stream_agent_a() + call_agent_b() |
| MODIFY | `backend/api/routes/chat_ws.py` | Rewrite main loop for A->B flow |
| KEEP | `backend/domain/tools.py` | Unchanged, used only by Agent B |
| KEEP | `backend/domain/prompts.py` | Preserved as reference, not deleted |
| KEEP | `backend/services/memory_service.py` | Profile update logic unchanged |
| KEEP | `backend/infrastructure/ai_client.py` | Same client for both agents |
| KEEP | All frontend files | WebSocket protocol unchanged, zero frontend changes |

## Frontend Impact

**None.** The WebSocket message protocol is identical:
- `behavior` - from Agent B's set_ai_behavior
- `text_stream` - from Agent A's streaming
- `stream_end` - end signal
- `voice` - TTS audio
- `compressing` / `compress_done` - compression notifications

## Risks & Mitigations

1. **Latency increase** - Sequential adds Agent B call time (~200-500ms). Acceptable for now; Parallel mode planned for future optimization.
2. **JPAF prompt length** - Full init prompt ~2000 tokens. Mitigated by compact prompt on subsequent turns.
3. **Consistency** - Both agents must see consistent state. Solved by shared messages list + Agent A completing before Agent B starts.
4. **Token cost** - Two LLM calls per turn. Accepted per user decision; use same model from .env.

## Decisions Made

- [x] JPAF wraps existing VTuber character (overlay, not replace)
- [x] Shared single chat history (not separate per agent)
- [x] TTS not affected by persona for now
- [x] Sequential execution (A first, then B)
- [x] Same model from .env for both agents
- [x] 4 persona profiles only (tsundere, happy, angry, seductive)
- [x] Token cost ignored for now
- [x] Parallel mode documented as future work
