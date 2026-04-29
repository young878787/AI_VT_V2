# Expression Signature + Hiyori Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `compile_expression_plan()` generate visibly distinct Hiyori expressions for angry, happy, goofy, sad, and special event modes by adding a signature layer and a Hiyori-specific adapter.

**Architecture:** Keep AI intent high-level. Add a compiler-side signature resolver after topic guard, use that signature to influence preset selection and modifier amplification, then run a Hiyori adapter to push eyebrow, eye, mouth, and blush values into stronger visible ranges. Keep the frontend playback path unchanged unless tests prove the backend output is still too weak.

**Tech Stack:** Python 3, `unittest`, existing backend compiler pipeline in `backend/services/expression_compiler.py`

---

### Task 1: Lock the desired visual behaviors with failing compiler tests

**Files:**
- Modify: `backend/tests/test_expression_compiler.py`
- Test: `backend/tests/test_expression_compiler.py`

- [ ] **Step 1: Write failing tests for the new signature outcomes**

```python
    def test_angry_signature_clears_blush_and_strengthens_frown(self):
        happy_plan = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "smile",
                "intensity": 0.75,
                "energy": 0.65,
                "warmth": 0.75,
            },
            model_name="Hiyori",
            previous_state=None,
        )
        angry_plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.85,
                "energy": 0.8,
                "dominance": 0.8,
                "warmth": 0.1,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        angry = angry_plan["basePose"]["params"]
        happy = happy_plan["basePose"]["params"]

        self.assertLessEqual(angry["blushLevel"], 0.02)
        self.assertLess(angry["blushLevel"], happy["blushLevel"])
        self.assertLess(angry["eyeLOpen"], happy["eyeLOpen"])
        self.assertLess(angry["browLForm"], -0.3)
        self.assertGreater(angry["browLX"], happy["browLX"])

    def test_goofy_signature_keeps_blush_and_forces_brow_asymmetry(self):
        plan = compile_expression_plan(
            {
                "emotion": "playful",
                "performance_mode": "goofy_face",
                "intensity": 0.9,
                "energy": 0.85,
                "playfulness": 0.95,
                "warmth": 0.6,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        params = plan["basePose"]["params"]
        self.assertGreater(params["blushLevel"], 0.05)
        self.assertFalse(params["eyeSync"])
        self.assertGreater(abs(params["browLY"] - params["browRY"]), 0.2)
        self.assertGreater(abs(params["eyeLOpen"] - params["eyeROpen"]), 0.12)

    def test_sad_signature_drops_blush_and_softly_squints(self):
        sad_plan = compile_expression_plan(
            {
                "emotion": "sad",
                "performance_mode": "tense_hold",
                "intensity": 0.85,
                "energy": 0.35,
                "warmth": 0.2,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        sad = sad_plan["basePose"]["params"]
        self.assertLessEqual(sad["blushLevel"], 0.0)
        self.assertLess(sad["eyeLOpen"], 0.78)
        self.assertLess(sad["mouthForm"], -0.3)

    def test_wink_like_modes_emit_more_visible_events(self):
        plan = compile_expression_plan(
            {
                "emotion": "teasing",
                "performance_mode": "cheeky_wink",
                "intensity": 0.8,
                "energy": 0.7,
                "playfulness": 0.85,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["microEvents"][0]["kind"], "wink_left")
        self.assertGreaterEqual(plan["microEvents"][0]["durationMs"], 300)
        self.assertGreater(plan["microEvents"][0]["patch"]["blushLevel"], 0.12)
```

- [ ] **Step 2: Run the targeted test file and verify it fails for the new assertions**

Run: `C:\Users\陳洋\Desktop\AI_VT_V2\.venv\Scripts\pytest.exe backend\tests\test_expression_compiler.py -q`
Expected: FAIL on the new blush, brow asymmetry, eye squint, or event duration assertions.

- [ ] **Step 3: Keep the new tests focused on compiler output only**

```python
# Do not add frontend assertions here.
# Assert only on expression_plan fields:
# - plan["basePose"]["params"]
# - plan["microEvents"]
# - plan["debug"]
```

- [ ] **Step 4: Re-run the targeted test file to confirm the failure is still the intended one**

Run: `C:\Users\陳洋\Desktop\AI_VT_V2\.venv\Scripts\pytest.exe backend\tests\test_expression_compiler.py -q`
Expected: FAIL only because compiler output is weaker than the required signature.

- [ ] **Step 5: Commit the test-only red state once implementation is ready to follow immediately**

```bash
git add backend/tests/test_expression_compiler.py
git commit -m "test: define stronger expression signature expectations"
```

### Task 2: Add the compiler signature resolver and Hiyori adapter

**Files:**
- Modify: `backend/services/expression_compiler.py`
- Test: `backend/tests/test_expression_compiler.py`

- [ ] **Step 1: Add helpers for signature resolution and model adaptation**

```python
def resolve_visual_signature(intent: dict, performance_mode: str) -> dict:
    emotion = intent.get("emotion", "neutral")
    signature = {
        "blush_policy": "neutralize",
        "eye_shape": "open",
        "brow_pattern": "calm",
        "mouth_pattern": "smile",
        "asymmetry_strength": 0.0,
        "event_bias": [],
    }

    if emotion == "happy" and performance_mode == "smile":
        signature.update({
            "blush_policy": "keep",
            "eye_shape": "soft_squint",
            "brow_pattern": "calm",
            "mouth_pattern": "smile",
        })
    elif performance_mode == "goofy_face":
        signature.update({
            "blush_policy": "boost",
            "eye_shape": "soft_squint",
            "brow_pattern": "one_up_one_down",
            "mouth_pattern": "smile",
            "asymmetry_strength": 0.85,
            "event_bias": ["goofy_eye_cross_bias", "awkward_freeze"],
        })
    elif emotion == "angry":
        signature.update({
            "blush_policy": "drop",
            "eye_shape": "hard_squint",
            "brow_pattern": "frown",
            "mouth_pattern": "downturned",
            "asymmetry_strength": 0.25 if performance_mode != "meltdown" else 0.65,
            "event_bias": ["meltdown_warp"] if performance_mode == "meltdown" else [],
        })
    elif emotion in {"sad", "gloomy"}:
        signature.update({
            "blush_policy": "drop",
            "eye_shape": "soft_squint",
            "brow_pattern": "inner_raised" if emotion == "sad" else "asymmetric_tense",
            "mouth_pattern": "downturned",
        })

    return signature


def apply_hiyori_signature_adapter(params: dict, signature: dict, intent: dict) -> dict:
    adjusted = deepcopy(params)
    intensity = _coerce_float(intent.get("intensity", 0.35), 0.35)
    energy = _coerce_float(intent.get("energy", 0.35), 0.35)

    # eyebrow, blush, eye shape, and mouth amplification live here
    return adjusted
```

- [ ] **Step 2: Thread the signature through `compile_expression_plan()`**

```python
    performance_mode = resolve_effective_performance_mode(emotion, performance_mode, topic_guard)
    visual_signature = resolve_visual_signature(intent, performance_mode)

    preset_name = select_base_pose(
        emotion,
        performance_mode,
        model_name=model_name,
        signature=visual_signature,
    )

    base_pose = apply_base_pose_modifiers(intent, base_pose, signature=visual_signature)
    if model_name == "Hiyori":
        base_pose["params"] = apply_hiyori_signature_adapter(
            base_pose["params"],
            visual_signature,
            intent,
        )
```

- [ ] **Step 3: Extend `apply_base_pose_modifiers()` to honor the signature**

```python
def apply_base_pose_modifiers(intent: dict, base_pose: dict, signature: dict | None = None) -> dict:
    signature = signature or {}
    params = deepcopy(base_pose["params"])

    blush_policy = signature.get("blush_policy", "neutralize")
    if blush_policy == "keep":
        params["blushLevel"] = max(params["blushLevel"], 0.05)
    elif blush_policy == "boost":
        params["blushLevel"] = min(1.0, params["blushLevel"] + 0.08)
    elif blush_policy == "drop":
        params["blushLevel"] = min(params["blushLevel"], 0.0)

    if signature.get("eye_shape") == "hard_squint":
        params["eyeLOpen"] = max(0.38, params["eyeLOpen"] - 0.18)
        params["eyeROpen"] = max(0.38, params["eyeROpen"] - 0.18)
```

- [ ] **Step 4: Populate debug output with the resolved signature**

```python
        "debug": {
            "intentEmotion": emotion,
            "intentPerformanceMode": performance_mode,
            "signatureBlushPolicy": visual_signature["blush_policy"],
            "signatureEyeShape": visual_signature["eye_shape"],
            "signatureBrowPattern": visual_signature["brow_pattern"],
            "selectedBasePreset": preset_name,
        },
```

- [ ] **Step 5: Run the targeted compiler test file and confirm the new tests turn green**

Run: `C:\Users\陳洋\Desktop\AI_VT_V2\.venv\Scripts\pytest.exe backend\tests\test_expression_compiler.py -q`
Expected: PASS for the newly added signature tests, with existing compiler tests still green.

- [ ] **Step 6: Commit the compiler signature layer**

```bash
git add backend/services/expression_compiler.py backend/tests/test_expression_compiler.py
git commit -m "feat: add Hiyori expression signature adapter"
```

### Task 3: Strengthen micro events for wink, goofy, tense, and meltdown modes

**Files:**
- Modify: `backend/domain/expression_sequence_library.py`
- Modify: `backend/services/expression_compiler.py`
- Test: `backend/tests/test_expression_compiler.py`

- [ ] **Step 1: Write or extend failing tests for event visibility**

```python
    def test_meltdown_event_is_longer_and_more_distinct_than_angry_deadpan(self):
        meltdown = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.9,
                "energy": 0.9,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(meltdown["microEvents"][0]["kind"], "meltdown_warp")
        self.assertGreaterEqual(meltdown["microEvents"][0]["durationMs"], 500)
        self.assertLessEqual(meltdown["microEvents"][0]["patch"]["eyeLOpen"], 0.35)
```

- [ ] **Step 2: Run the targeted tests and verify the event-duration assertions fail**

Run: `C:\Users\陳洋\Desktop\AI_VT_V2\.venv\Scripts\pytest.exe backend\tests\test_expression_compiler.py -q`
Expected: FAIL on wink or meltdown event visibility thresholds.

- [ ] **Step 3: Increase event distinction in the library and compiler event scaling**

```python
MICRO_EVENT_LIBRARY["wink_left"]["durationMs"] = 320
MICRO_EVENT_LIBRARY["wink_left"]["patch"]["blushLevel"] = 0.18
MICRO_EVENT_LIBRARY["goofy_eye_cross_bias"]["patch"]["browLY"] = 0.36
MICRO_EVENT_LIBRARY["tense_squeeze"]["patch"]["eyeLOpen"] = 0.5
MICRO_EVENT_LIBRARY["meltdown_warp"]["durationMs"] = 520
MICRO_EVENT_LIBRARY["meltdown_warp"]["patch"]["eyeLOpen"] = 0.32
```

```python
    if event_name and event_name in MICRO_EVENT_LIBRARY:
        event = deepcopy(MICRO_EVENT_LIBRARY[event_name])
        if signature.get("event_bias") and event_name in signature["event_bias"]:
            event["durationMs"] = int(event["durationMs"] * 1.15)
```

- [ ] **Step 4: Re-run the targeted test file and confirm event assertions pass**

Run: `C:\Users\陳洋\Desktop\AI_VT_V2\.venv\Scripts\pytest.exe backend\tests\test_expression_compiler.py -q`
Expected: PASS for wink, goofy, tense, and meltdown event assertions.

- [ ] **Step 5: Commit the event-library strengthening**

```bash
git add backend/domain/expression_sequence_library.py backend/services/expression_compiler.py backend/tests/test_expression_compiler.py
git commit -m "feat: strengthen expression micro events"
```

### Task 4: Run the focused regression suite and verify no compiler regressions

**Files:**
- Test: `backend/tests/test_expression_compiler.py`
- Test: `backend/tests/test_expression_intent_parser.py`
- Test: `backend/tests/test_live2d_prompt_config.py`

- [ ] **Step 1: Run the compiler test suite**

Run: `C:\Users\陳洋\Desktop\AI_VT_V2\.venv\Scripts\pytest.exe backend\tests\test_expression_compiler.py -q`
Expected: PASS

- [ ] **Step 2: Run parser and prompt tests that depend on expression terminology**

Run: `C:\Users\陳洋\Desktop\AI_VT_V2\.venv\Scripts\pytest.exe backend\tests\test_expression_intent_parser.py backend\tests\test_live2d_prompt_config.py -q`
Expected: PASS

- [ ] **Step 3: If any regression appears, fix it with another red-green cycle before closing**

```python
# Add the smallest missing test first.
# Then patch only the compiler path that caused the regression.
```

- [ ] **Step 4: Commit the verified implementation**

```bash
git add backend/domain/expression_sequence_library.py backend/services/expression_compiler.py backend/tests/test_expression_compiler.py
git commit -m "feat: ship stronger Hiyori expression signatures"
```
