# Live2D Prompt vs Architecture Redesign Proposal

> **Status:** Proposed  
> **Date:** 2026-04-24  
> **Goal:** Improve tool-call robustness and make Live2D expressions feel more human by moving from a single static expression payload to a layered `base pose + micro-expression events` architecture.

## Background

Current Live2D control is already separated into Agent B-1 (Live2D) and B-2 (Memory), but the expression system still behaves like a single-frame decision pipeline:

1. Agent B-1 emits one required `set_ai_behavior` tool call.
2. Backend parses tool arguments and stores one final behavior payload.
3. Frontend interpolates toward one target expression for a fixed duration.
4. Only `blink_control` acts as an additional dynamic layer.

This works for "the final face of this line," but it is weak at:

- daily small facial variation
- playful asymmetry
- making faces (`鬼臉`)
- short emotional arcs inside one line
- recovering gracefully when Qwen emits malformed tool JSON

## Current Constraints Observed in Code

### 1. Tool argument parsing is too strict

`backend/services/tool_arg_parser.py` only repairs one malformed pattern: leading-zero decimals such as `00.15`.

It does **not** recover from more common model failures such as:

- broken booleans
- truncated JSON
- missing commas
- malformed key/value boundaries
- partially valid payloads where some fields are still recoverable

Impact: one malformed `set_ai_behavior` call can drop the whole expression back to safe defaults.

### 2. The prompt is already fairly strong

`backend/domain/agent_b_prompts.py` and `backend/domain/tools/Hiyori.json` already instruct the model to:

- avoid safe middle values
- use asymmetry when appropriate
- use `blink_control` actively
- widen ranges for high-energy emotions

Impact: prompt quality is **not** the primary bottleneck anymore.

### 3. Backend collapses expression into one final state

`backend/api/routes/chat_ws.py` currently:

- collects all tool calls
- keeps one final `set_ai_behavior` state
- immediately sends one `behavior` payload

Impact: even if the model conceptually reasons about an arc, the runtime only preserves the last target state.

### 4. Frontend runtime is target-based, not event-based

`vtuber-web-app/src/live2d/LAppModel.ts`:

- stores one AI target state
- lerps toward that state over time
- layers blink on top

Impact: the model can drift toward one expression, but it cannot naturally perform a short sequence such as:

1. widen eyes
2. hold for 300ms
3. smirk on one side
4. settle into a teasing face

## Prompt vs Architecture: What Each Can Solve

### Prompt can improve

- willingness to use asymmetry
- willingness to use larger ranges
- better mapping from tone to expression parameters
- more frequent `blink_control` usage

### Prompt cannot fully solve

- malformed tool JSON
- single-state collapse in backend
- lack of a micro-expression event layer
- lack of short staged expression playback
- over-verbose 17-field tool payloads for every expressive beat

## Decision Summary

### Primary decision

The first priority should be **architecture and parsing**, not more prompt wording.

### Recommended order

1. Add tolerant parsing and partial salvage for Live2D tool arguments.
2. Stop treating `set_ai_behavior` as the only expressive mechanism.
3. Add an event layer for micro-expression playback.
4. Simplify what the LLM must output per expressive decision.
5. Only then revise prompts to exploit the new architecture.

## Target Architecture

### Layer 1: Base Pose

Keep `set_ai_behavior`, but redefine its responsibility:

- It controls the **dominant resting expression** for the current line.
- It should represent where the face settles, not every micro-beat.
- It should allow partial updates instead of requiring a full 17-field snapshot every time.

This makes it suitable for:

- calm chat
- normal emotional tone
- the final face after a short expressive moment

### Layer 2: Micro-Expression Events

Add a second expressive layer that plays short-lived events on top of the base pose.

Recommended tools:

#### `queue_expression_pulse`

Purpose: brief exaggeration of a small set of parameters, then decay back.

Use cases:

- quick eyebrow pop
- quick pout
- small teasing mouth twitch
- sudden embarrassment squeeze

Recommended shape:

```json
{
  "patch": {
    "mouth_form": 0.35,
    "brow_l_y": 0.25,
    "brow_r_y": 0.1
  },
  "duration_ms": 450,
  "ease": "out",
  "return_to_base": true
}
```

#### `queue_micro_expression`

Purpose: semantic short event with a much smaller schema than full parameter control.

Use cases:

- `wink_left`
- `smirk_left`
- `smirk_right`
- `brow_raise_left`
- `brow_raise_right`
- `embarrassed_shrink`
- `surprised_pop`
- `pout`

Recommended shape:

```json
{
  "kind": "smirk_left",
  "intensity": 0.7,
  "duration_ms": 600
}
```

This is more reliable than asking the model to output another full 17-field expression object.

#### `queue_expression_sequence`

Purpose: explicit staged performance for special moments.

Use cases:

- making faces
- dramatic reactions
- playful "ehhh?" beats
- two- or three-stage expression arcs in one reply

Recommended shape:

```json
{
  "steps": [
    {
      "patch": {
        "eye_l_open": 1.15,
        "eye_r_open": 1.15,
        "mouth_form": 0.15
      },
      "duration_ms": 250
    },
    {
      "patch": {
        "eye_sync": false,
        "eye_l_smile": 0.7,
        "eye_r_smile": 0.15,
        "mouth_form": 0.45
      },
      "duration_ms": 650
    },
    {
      "patch": {
        "mouth_form": 0.2,
        "eye_sync": true
      },
      "duration_ms": 500
    }
  ]
}
```

This tool should be capped at short sequences only, for example 2-4 steps.

### Layer 3: Deterministic Runtime Variation

Not all human-like variation should come from the LLM.

The frontend runtime should own small deterministic fluctuations such as:

- slight blink interval drift
- tiny asymmetry jitter during playful states
- brief gaze hold during pauses
- subtle settle-back after a strong micro-expression

This gives "alive" behavior without forcing the LLM to micromanage every facial movement.

## Should the Model Output 3 Tool Calls?

### Short answer

Yes, **one Live2D agent can handle multiple tool calls in one response**, but it should not be forced to output three separate large full-parameter payloads by default.

### Recommended budgeting

#### Normal chat

- `set_ai_behavior` x1
- `blink_control` x0-1
- `queue_micro_expression` x0-1

#### Playful / teasing / shy / mild face-making

- `set_ai_behavior` x1
- `blink_control` x0-1
- `queue_expression_pulse` or `queue_micro_expression` x1

#### Explicit performance / strong reaction / making faces

- `set_ai_behavior` x1
- `blink_control` x0-1
- `queue_expression_sequence` x1

### What should be avoided

Avoid asking the model to emit:

- three separate `set_ai_behavior` calls
- each with 17 explicit fields
- all in one reply

That is high token cost, high schema complexity, and high failure risk.

## Single Live2D Agent vs Multiple AIs

### Recommended now: one Live2D agent

Use one Live2D agent that can emit:

- one base pose
- zero or more compact event tools

Reasons:

- lower latency
- easier state consistency
- simpler debugging
- less prompt duplication
- current bottleneck is schema shape and runtime architecture, not lack of reasoning stages

### Do not split into multiple AIs yet

Splitting into multiple AIs is not the first fix because the main problems are currently:

- fragile parsing
- overly large tool payloads
- missing event queue in runtime
- single-state collapse after tool execution

If those stay unchanged, adding more agents only increases coordination cost.

### When multi-AI might become worth it

Multi-AI only becomes justified if, after simplifying tools and adding the event layer, one agent still cannot reliably plan expressive beats.

The likely future split would be:

1. **Expression Planner Agent**
   - outputs abstract intent timeline
   - examples: `teasing`, `surprised_pop`, `hold_gaze`, `settle_smirk`

2. **Expression Renderer Agent or deterministic mapper**
   - converts abstract beats into tool payloads

This is a later-stage optimization, not phase 1.

## Tool Schema Redesign Principles

### 1. Reduce required fields

`set_ai_behavior` currently behaves like a full snapshot contract.

That should be relaxed so the model can send only meaningful fields, while backend merges with:

- safe defaults
- current active base pose
- model-specific clamps

### 2. Prefer semantic tools for short events

For micro-expression, prefer compact semantic enums over raw full-face parameter dumps.

Reason: LLMs are better at choosing `smirk_left` than consistently setting 8 related sliders in a stable way.

### 3. Keep sequence payloads short

`queue_expression_sequence` should remain a short performance primitive, not a general animation language.

Recommended limits:

- max 4 steps
- partial patch only
- short duration only

### 4. Backend should salvage partial payloads

If one field is malformed, backend should still recover the valid fields instead of dropping the whole tool call.

## Prompt Redesign After Architecture Upgrade

Once the architecture supports events, the prompt should change from "pick one strong face" to "pick a settle pose plus optional expressive beats."

Recommended prompt rules:

- Always choose one final settle expression using `set_ai_behavior`.
- For playful, shy, teasing, surprised, or face-making moments, add one compact event tool.
- Use `queue_expression_sequence` only when the line contains an obvious staged reaction.
- Do not emit multiple full-face snapshots when one base pose plus one event is enough.
- If emotion is stable but not neutral, allow small variation instead of returning to flat default every turn.

## Proposed Rollout Phases

### Phase 1: Robustness first

- tolerant parser for malformed tool arguments
- field-level salvage for `set_ai_behavior`
- backend merge of partial payloads

### Phase 2: Minimal expressive event layer

- add `queue_expression_pulse`
- add backend event forwarding
- add frontend event queue playback

### Phase 3: Semantic micro-expression tool

- add `queue_micro_expression`
- map semantic events to deterministic parameter patches

### Phase 4: Short sequence support

- add `queue_expression_sequence`
- add step scheduler in frontend runtime

### Phase 5: Prompt revision

- rewrite Live2D agent prompt for `base pose + event` output strategy
- add event-budget rules so the model does not over-call tools

## Recommended Initial Scope

For the next implementation cycle, the best first cut is:

1. tolerant parser + partial salvage
2. partial `set_ai_behavior` contract
3. `queue_expression_pulse`
4. prompt revision for one base pose plus optional one event

This gives the highest impact with the lowest architectural risk.

`queue_micro_expression` and `queue_expression_sequence` should follow after the event queue has proven stable.

## Success Criteria

The redesign should be considered successful when:

- malformed but partially readable tool payloads no longer collapse to neutral behavior
- daily chat shows subtle but visible variation without needing extreme prompts
- playful and shy lines can show short asymmetrical beats
- making faces can produce a clear two- or three-beat reaction
- one Live2D agent remains sufficient for most turns
- tool-call debugging becomes easier because base pose and micro-events are clearly separated

## Final Recommendation

The immediate path should be:

1. **Do not start with more prompt wording alone.**
2. **Do not split into more AIs yet.**
3. **First make tool parsing tolerant and expression control layered.**
4. **Use one Live2D agent that emits one base pose plus compact event tools.**

That is the lowest-risk path to getting more human, more expressive, and more robust facial behavior.
