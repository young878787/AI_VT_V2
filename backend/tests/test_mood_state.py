"""後端情緒量化算法驗證（mood_v1）：JEV score 為固定輸入，不呼叫模型。

涵蓋參數、紀錄驗證、單輪套用、回落指標與 Chat 提詞投影；JEV 判斷準確度另由真實模型驗收。
"""

import math
import pathlib
import random
import re
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.agent_a_prompts import build_agent_a_prompt
from domain.emotion_state import (
    EMOTION_FIELDS,
    MOOD_GAIN,
    MOOD_HALF_LIFE_TURNS,
    MOOD_IDLE_HALF_LIFE_SEC,
    MOOD_OPPOSITION,
    MOOD_RETENTION,
    NEUTRAL_EMOTION_STATE,
    PRIMARY_EMOTION_FIELDS,
    apply_mood_turn,
    build_mood_lines,
    idle_decayed_mood,
    validate_mood_record,
)

R = MOOD_RETENTION
CALM = ["主要情緒：平穩", "表達方式：照平常的樣子表達", "互動意願：正常回應，不特別延伸也不刻意收尾"]


def answers(confidence=0.9, **scores):
    return {field: {"type": "score", "score": scores.get(field, 2), "confidence": confidence}
            for field in EMOTION_FIELDS}


def record(mood=None, streak=None, history=None, updated_at=0.0, last_turn_id="previous"):
    return {
        "schema": "mood_v1",
        "mood": {**NEUTRAL_EMOTION_STATE, **(mood or {})},
        "streak": {**dict.fromkeys(PRIMARY_EMOTION_FIELDS, 0), **(streak or {})},
        "updated_at": updated_at,
        "last_turn_id": last_turn_id,
        "history": list(history or []),
    }


def step(previous, turn_id="turn", timestamp=0.0, **scores):
    current = idle_decayed_mood(previous, timestamp)
    return apply_mood_turn(previous, current, answers(**scores), turn_id, timestamp)


def run(sequence):
    """連續套用（不含閒置間隔）；回傳每輪 (紀錄, changes)。"""
    previous, results = None, []
    for index, scores in enumerate(sequence, 1):
        previous, changes, _ = step(previous, f"t{index}", **scores)
        results.append((previous, changes))
    return results


def first_place(mood):
    return max(PRIMARY_EMOTION_FIELDS, key=lambda field: mood[field])


class MoodParameterTests(unittest.TestCase):
    """A-01"""

    def test_parameter_table_and_opposition_matrix(self):
        self.assertEqual(set(MOOD_GAIN), set(EMOTION_FIELDS))
        self.assertEqual(set(MOOD_HALF_LIFE_TURNS), set(EMOTION_FIELDS))
        self.assertEqual(NEUTRAL_EMOTION_STATE, {
            "shy": 0.0, "pleased": 0.0, "genuinely_angry": 0.0, "sad_or_hurt": 0.0,
            "masking_positive_feeling": 0.5, "wants_continue_interaction": 0.5,
        })
        self.assertEqual(len(PRIMARY_EMOTION_FIELDS), 4)
        self.assertTrue(set(PRIMARY_EMOTION_FIELDS) < set(EMOTION_FIELDS))
        for field in EMOTION_FIELDS:
            self.assertAlmostEqual(R[field], 0.5 ** (1 / MOOD_HALF_LIFE_TURNS[field]), places=12)
        self.assertEqual(set(MOOD_OPPOSITION), set(PRIMARY_EMOTION_FIELDS))
        for raised, targets in MOOD_OPPOSITION.items():
            self.assertNotIn(raised, targets)
            self.assertTrue(set(targets) <= set(PRIMARY_EMOTION_FIELDS))
            self.assertTrue(all(0.0 <= value <= 1.0 for value in targets.values()))
        for first, second in (("pleased", "shy"), ("sad_or_hurt", "genuinely_angry")):
            self.assertEqual(MOOD_OPPOSITION[first].get(second, 0.0), 0.0)
            self.assertEqual(MOOD_OPPOSITION[second].get(first, 0.0), 0.0)


class MoodRecordValidationTests(unittest.TestCase):
    """A-02"""

    def test_valid_record_round_trips(self):
        valid = record({"pleased": 0.35, "shy": 0.17}, {"pleased": 1, "shy": 1},
                       [{"turn_id": "t1", "changes": {"pleased": 0.35, "shy": 0.17}}], 1761600000.0, "t1")
        self.assertEqual(validate_mood_record(valid), valid)

    def test_invalid_records_are_rejected_atomically(self):
        valid = record(history=[{"turn_id": "t1", "changes": {"pleased": 0.1}}])
        cases = {
            "legacy_six_fields": dict(NEUTRAL_EMOTION_STATE),
            "missing_key": {key: value for key, value in valid.items() if key != "streak"},
            "extra_key": {**valid, "extra": 1},
            "schema": {**valid, "schema": "mood_v2"},
            "mood_range": {**valid, "mood": {**valid["mood"], "shy": 1.2}},
            "mood_nan": {**valid, "mood": {**valid["mood"], "shy": math.nan}},
            "mood_bool": {**valid, "mood": {**valid["mood"], "shy": True}},
            "streak_float": {**valid, "streak": {**valid["streak"], "shy": 1.0}},
            "streak_bool": {**valid, "streak": {**valid["streak"], "shy": True}},
            "streak_range": {**valid, "streak": {**valid["streak"], "shy": 100}},
            "streak_negative": {**valid, "streak": {**valid["streak"], "shy": -1}},
            "streak_axis_key": {**valid, "streak": {**valid["streak"], "masking_positive_feeling": 0}},
            "history_too_long": {**valid, "history": [{"turn_id": f"t{i}", "changes": {}} for i in range(4)]},
            "history_bad_field": {**valid, "history": [{"turn_id": "t1", "changes": {"other": 0.1}}]},
            "history_nan": {**valid, "history": [{"turn_id": "t1", "changes": {"shy": math.nan}}]},
            "updated_at_inf": {**valid, "updated_at": math.inf},
            "updated_at_nan": {**valid, "updated_at": math.nan},
            "updated_at_bool": {**valid, "updated_at": True},
            "turn_id_empty": {**valid, "last_turn_id": ""},
            "not_dict": [valid],
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(validate_mood_record(value))


class MoodApplyTests(unittest.TestCase):
    """A-03"""

    def assertMood(self, actual, expected):
        for field in EMOTION_FIELDS:
            self.assertAlmostEqual(actual[field], expected[field], delta=1e-9, msg=field)

    def test_scenario_a_turn_1_from_baseline(self):
        next_record, changes, source = step(None, "t1", pleased=4, shy=3, masking_positive_feeling=3)
        self.assertEqual(source, "jev")
        self.assertMood(next_record["mood"], {**NEUTRAL_EMOTION_STATE, "shy": 0.175, "pleased": 0.35,
                                              "masking_positive_feeling": 0.575})
        self.assertEqual(changes, {"shy": 0.17, "pleased": 0.35, "masking_positive_feeling": 0.07})
        self.assertEqual(next_record["streak"], {"shy": 1, "pleased": 1, "genuinely_angry": 0, "sad_or_hurt": 0})
        self.assertEqual(next_record["history"], [{"turn_id": "t1", "changes": changes}])
        self.assertEqual(next_record["last_turn_id"], "t1")

    def test_scenario_a_turn_5_anger_resets_pleased_and_shy(self):
        previous = record({"shy": 0.07, "pleased": 0.45, "masking_positive_feeling": 0.53})
        next_record, _, _ = step(previous, "t5", genuinely_angry=4, pleased=0, wants_continue_interaction=1)
        self.assertMood(next_record["mood"], {
            "shy": max(0.0, 0.07 * R["shy"] - 0.3 * 0.40),
            "pleased": max(0.0, max(0.0, 0.45 * R["pleased"] - 0.35) - 0.5 * 0.40),
            "genuinely_angry": 0.40, "sad_or_hurt": 0.0,
            "masking_positive_feeling": 0.5 + 0.03 * R["masking_positive_feeling"],
            "wants_continue_interaction": 0.5 - 0.125 * 0.5,
        })
        self.assertEqual(next_record["mood"]["shy"], 0.0)
        self.assertEqual(next_record["mood"]["pleased"], 0.0)

    def test_scenario_a_turn_9_apology(self):
        previous = record({"genuinely_angry": 0.24})
        next_record, changes, _ = step(previous, "t9", genuinely_angry=1, pleased=3)
        angry = 0.24 * R["genuinely_angry"] - 0.2
        self.assertMood(next_record["mood"], {**NEUTRAL_EMOTION_STATE, "pleased": 0.175,
                                              "genuinely_angry": max(0.0, angry - 0.5 * 0.175)})
        self.assertEqual(changes, {"pleased": 0.17, "genuinely_angry": -0.2})

    def test_scenario_c_turn_3_bad_news_offsets_pleased(self):
        previous = record({"pleased": 0.53})
        next_record, _, _ = step(previous, "t3", sad_or_hurt=4)
        self.assertMood(next_record["mood"], {**NEUTRAL_EMOTION_STATE, "sad_or_hurt": 0.35,
                                              "pleased": 0.53 * R["pleased"] - 0.5 * 0.35})
        self.assertEqual(first_place(next_record["mood"]), "sad_or_hurt")

    def test_dead_zone_is_no_change(self):
        previous = record({"pleased": 0.4})
        decayed = 0.4 * R["pleased"]
        for score in (2.25, 1.75):
            with self.subTest(score=score):
                result, changes, source = apply_mood_turn(
                    previous, previous["mood"], answers(pleased=score), "t", 0.0)
                self.assertEqual(source, "jev")
                self.assertAlmostEqual(result["mood"]["pleased"], decayed, delta=1e-12)
                self.assertEqual(changes, {})
                self.assertEqual(result["streak"]["pleased"], 0)
        result, _, _ = step(previous, pleased=2.5)
        self.assertGreater(result["mood"]["pleased"], decayed)

    def test_confidence_does_not_gate_or_weight_changes(self):
        expected, _, _ = apply_mood_turn(None, dict(NEUTRAL_EMOTION_STATE), answers(0.9, genuinely_angry=4),
                                         "t", 0.0)
        for confidence in (0.0, 0.22, 0.49, 1.0):
            with self.subTest(confidence=confidence):
                result, _, source = apply_mood_turn(
                    None, dict(NEUTRAL_EMOTION_STATE), answers(confidence, genuinely_angry=4), "t", 0.0)
                self.assertEqual(source, "jev")
                self.assertEqual(result, expected)
        missing = {field: {"type": "score", "score": 2} for field in EMOTION_FIELDS}
        missing["genuinely_angry"]["score"] = 4
        self.assertEqual(apply_mood_turn(None, dict(NEUTRAL_EMOTION_STATE), missing, "t", 0.0)[0], expected)

    def test_primary_decrease_is_linear_and_axes_saturate_both_ways(self):
        previous = record({"pleased": 0.6, "masking_positive_feeling": 0.8, "wants_continue_interaction": 0.2})
        result, _, _ = step(previous, pleased=1, masking_positive_feeling=0, wants_continue_interaction=4)
        self.assertAlmostEqual(result["mood"]["pleased"], 0.6 * R["pleased"] - 0.175, delta=1e-12)
        masking = 0.5 + 0.3 * R["masking_positive_feeling"]
        self.assertAlmostEqual(result["mood"]["masking_positive_feeling"], masking - 0.3 * masking, delta=1e-12)
        wants = 0.5 - 0.3 * R["wants_continue_interaction"]
        self.assertAlmostEqual(result["mood"]["wants_continue_interaction"], wants + 0.25 * (1 - wants),
                               delta=1e-12)
        result, _, _ = step(record({"pleased": 0.2}), pleased=0)
        self.assertEqual(result["mood"]["pleased"], 0.0)

    def test_opposition_uses_actual_primary_rise_only(self):
        previous = record({"pleased": 0.5, "genuinely_angry": 0.5, "shy": 0.5})
        result, _, _ = step(previous, pleased=4)
        pleased = 0.5 * R["pleased"]
        rise = 0.35 * (1 - pleased)
        self.assertAlmostEqual(result["mood"]["genuinely_angry"],
                               0.5 * R["genuinely_angry"] - 0.5 * rise, delta=1e-12)
        self.assertAlmostEqual(result["mood"]["shy"], 0.5 * R["shy"], delta=1e-12)
        # 主情緒下降與兩個軸的變化都不觸發對沖。
        result, _, _ = step(previous, shy=0, masking_positive_feeling=4, wants_continue_interaction=4)
        self.assertAlmostEqual(result["mood"]["genuinely_angry"], 0.5 * R["genuinely_angry"], delta=1e-12)
        self.assertAlmostEqual(result["mood"]["pleased"], 0.5 * R["pleased"], delta=1e-12)

    def test_invalid_answers_only_decay_and_reset_streak(self):
        history = [{"turn_id": "t1", "changes": {"pleased": 0.35}}]
        previous = record({"pleased": 0.35}, {"pleased": 3}, history)
        invalid_cases = {
            "none": None,
            "missing": {key: value for key, value in answers().items() if key != "shy"},
            "noul": {**answers(), "shy": {"type": "noul", "noul": 0.9}},
            "nan": {**answers(), "shy": {"type": "score", "score": math.nan, "confidence": 0.9}},
            "out_of_range": {**answers(), "shy": {"type": "score", "score": 4.5, "confidence": 0.9}},
            "bool": {**answers(), "shy": {"type": "score", "score": True, "confidence": 0.9}},
        }
        for name, value in invalid_cases.items():
            with self.subTest(case=name):
                result, changes, source = apply_mood_turn(previous, previous["mood"], value, "t2", 10.0)
                self.assertEqual(source, "previous_fallback")
                self.assertEqual(changes, {})
                self.assertAlmostEqual(result["mood"]["pleased"], 0.35 * R["pleased"], delta=1e-12)
                self.assertEqual(result["streak"], dict.fromkeys(PRIMARY_EMOTION_FIELDS, 0))
                self.assertEqual(result["history"], history)
                self.assertEqual((result["last_turn_id"], result["updated_at"]), ("t2", 10.0))

    def test_failure_without_record_is_neutral_and_same_turn_is_unchanged(self):
        result, changes, source = apply_mood_turn(None, dict(NEUTRAL_EMOTION_STATE), None, "t1", 5.0)
        self.assertEqual((result["mood"], changes, source), (NEUTRAL_EMOTION_STATE, {}, "neutral_fallback"))
        self.assertEqual(validate_mood_record(result), result)
        previous = record({"pleased": 0.4}, last_turn_id="same")
        unchanged, changes, _ = apply_mood_turn(previous, previous["mood"], answers(pleased=4), "same", 9.0)
        self.assertEqual(unchanged, previous)
        self.assertEqual(changes, {})

    def test_history_keeps_last_three_jev_changes(self):
        results = run([{"pleased": 4}, {}, {"shy": 4}, {"sad_or_hurt": 4}, {"genuinely_angry": 4}])
        final = results[-1][0]
        self.assertEqual([entry["turn_id"] for entry in final["history"]], ["t3", "t4", "t5"])
        self.assertEqual(final["history"][-1]["changes"], results[-1][1])
        self.assertEqual(validate_mood_record(final), final)

    def test_random_inputs_keep_mood_in_range(self):
        rng = random.Random(20261029)
        previous = None
        for index in range(500):
            scores = {field: rng.choice([0, 0.5, 1, 1.7, 2, 2.4, 3, 3.6, 4]) for field in EMOTION_FIELDS}
            current = idle_decayed_mood(previous, index * 600.0)
            previous, _, _ = apply_mood_turn(
                previous, current, answers(**scores), f"t{index}", index * 600.0)
            self.assertIsNotNone(validate_mood_record(previous))
            self.assertTrue(all(0.0 <= value <= 1.0 for value in previous["mood"].values()))


class MoodIdleDecayTests(unittest.TestCase):
    """A-04"""

    def test_idle_half_life(self):
        previous = record({"pleased": 0.8, "genuinely_angry": 0.4, "masking_positive_feeling": 0.9,
                           "wants_continue_interaction": 0.1}, updated_at=1000.0)
        half = idle_decayed_mood(previous, 1000.0 + MOOD_IDLE_HALF_LIFE_SEC)
        for field in EMOTION_FIELDS:
            deviation = previous["mood"][field] - NEUTRAL_EMOTION_STATE[field]
            self.assertAlmostEqual(half[field] - NEUTRAL_EMOTION_STATE[field], deviation / 2, delta=1e-9)
        self.assertEqual(idle_decayed_mood(previous, 1000.0), previous["mood"])
        self.assertEqual(idle_decayed_mood(previous, 0.0), previous["mood"])
        later = idle_decayed_mood(previous, 1000.0 + 8 * MOOD_IDLE_HALF_LIFE_SEC)
        self.assertTrue(all(abs(later[field] - NEUTRAL_EMOTION_STATE[field]) < 0.004 for field in EMOTION_FIELDS))
        self.assertEqual(idle_decayed_mood(None, 1000.0), NEUTRAL_EMOTION_STATE)


class MoodScenarioTests(unittest.TestCase):
    """A-05：第 3.4 節量化指標與第 4 節情境。"""

    def test_single_strong_stimulus_reaches_gain(self):
        for field in PRIMARY_EMOTION_FIELDS:
            result, _, _ = step(None, **{field: 4})
            self.assertAlmostEqual(result["mood"][field], MOOD_GAIN[field], delta=1e-12)

    def test_turns_to_return_from_obvious_to_calm(self):
        expected = {"shy": 2, "pleased": 4, "genuinely_angry": 5, "sad_or_hurt": 4}
        for field, turns in expected.items():
            with self.subTest(field=field):
                previous = record({field: 0.35})
                count = 0
                while previous["mood"][field] >= 0.15:
                    previous, _, _ = step(previous, f"t{count}")
                    count += 1
                self.assertEqual(count, turns)

    def test_repeated_strong_stimulus_converges_to_limit(self):
        for field in PRIMARY_EMOTION_FIELDS:
            gain, retention = MOOD_GAIN[field], R[field]
            limit = gain / (1 - retention + gain * retention)
            final = run([{field: 4}] * 60)[-1][0]
            self.assertAlmostEqual(final["mood"][field], limit, delta=0.01)
        self.assertAlmostEqual(0.35 / (1 - R["shy"] + 0.35 * R["shy"]), 0.59, delta=0.01)
        self.assertAlmostEqual(0.40 / (1 - R["genuinely_angry"] + 0.40 * R["genuinely_angry"]), 0.81, delta=0.01)

    def test_scenario_b_repeat_decay(self):
        expected = {1: 0.17, 2: 0.29, 3: 0.36, 4: 0.38, 5: 0.36, 6: 0.33, 8: 0.25, 10: 0.18, 12: 0.13}
        results = run([{"pleased": 3}] * 12)
        for turn, value in expected.items():
            self.assertAlmostEqual(results[turn - 1][0]["mood"]["pleased"], value, delta=0.01, msg=turn)
        self.assertEqual(build_mood_lines(results[11][0]["mood"], results[11][1]), CALM)
        self.assertEqual(build_mood_lines(results[5][0]["mood"])[0], "主要情緒：心情不錯")

    def test_scenario_c_bad_news_takes_first_place(self):
        results = run([{"pleased": 4}, {"pleased": 4}, {"sad_or_hurt": 4}])
        mood = results[2][0]["mood"]
        self.assertEqual(first_place(mood), "sad_or_hurt")
        self.assertEqual(build_mood_lines(mood)[:2], ["主要情緒：難過、覺得受傷", "輔助情緒：心情不錯"])

    def test_s4_each_strong_stimulus_takes_first_place(self):
        sequence = [{field: 4} for field in PRIMARY_EMOTION_FIELDS]
        for (result, _), scores in zip(run(sequence), sequence):
            self.assertEqual(first_place(result["mood"]), next(iter(scores)))

    def test_s5_no_stimulus_stays_calm(self):
        for result, changes in run([{}] * 5):
            self.assertEqual(result["mood"], NEUTRAL_EMOTION_STATE)
            self.assertEqual(build_mood_lines(result["mood"], changes), CALM)


class MoodProjectionTests(unittest.TestCase):
    """A-07"""

    def test_baseline_lists_calm_and_fixed_axes(self):
        self.assertEqual(build_mood_lines(NEUTRAL_EMOTION_STATE), CALM)

    def test_first_and_second_place(self):
        mood = {**NEUTRAL_EMOTION_STATE, "pleased": 0.53, "shy": 0.30}
        self.assertEqual(build_mood_lines(mood), ["主要情緒：很開心", "輔助情緒：有點不好意思", *CALM[1:]])
        mood = {**NEUTRAL_EMOTION_STATE, "pleased": 0.53, "shy": 0.20}
        self.assertEqual(build_mood_lines(mood), ["主要情緒：很開心", *CALM[1:]])
        mood = {**NEUTRAL_EMOTION_STATE, "genuinely_angry": 0.7}
        self.assertEqual(build_mood_lines(mood), ["主要情緒：非常生氣，話變少、態度冷淡", *CALM[1:]])

    def test_impact_line_needs_threshold_and_keeps_largest(self):
        mood = dict(NEUTRAL_EMOTION_STATE)
        self.assertEqual(build_mood_lines(mood, {"pleased": 0.11}), CALM)
        self.assertEqual(build_mood_lines(mood, {"pleased": 0.13, "shy": 0.2, "masking_positive_feeling": 0.3}),
                         [CALM[0], "本輪影響：這句話讓你更害羞", *CALM[1:]])
        self.assertEqual(build_mood_lines(mood, {"genuinely_angry": -0.2, "pleased": 0.17}),
                         [CALM[0], "本輪影響：你的生氣因這句話緩和下來", *CALM[1:]])

    def test_expression_and_interaction_axes_always_listed(self):
        for value, line in ((0.70, "表達方式：嘴硬，不直接承認正面感受"),
                            (0.30, "表達方式：坦率撒嬌，想討對方回應"), (0.50, CALM[1])):
            lines = build_mood_lines({**NEUTRAL_EMOTION_STATE, "masking_positive_feeling": value})
            self.assertEqual(lines, [CALM[0], line, CALM[2]])
        for value, line in ((0.70, "互動意願：想繼續聊，可以主動延伸話題"),
                            (0.30, "互動意願：興致較低，回覆可以簡短"), (0.50, CALM[2])):
            lines = build_mood_lines({**NEUTRAL_EMOTION_STATE, "wants_continue_interaction": value})
            self.assertEqual(lines, [CALM[0], CALM[1], line])

    def test_chat_prompt_uses_projection_without_raw_scores(self):
        mood = {"shy": 0.31, "pleased": 0.53, "genuinely_angry": 0.04, "sad_or_hurt": 0.02,
                "masking_positive_feeling": 0.71, "wants_continue_interaction": 0.68}
        prompt = build_agent_a_prompt({}, "", mood, message_timestamp=0.0, mood_changes={"shy": 0.17})
        self.assertIn(
            "目前心情（會隨對話累積與回落；依性格自然流露，不必每句都表現，不說出標籤或分數）：\n"
            "- 主要情緒：很開心\n- 輔助情緒：有點不好意思\n- 本輪影響：這句話讓你更害羞\n"
            "- 表達方式：嘴硬，不直接承認正面感受\n- 互動意願：想繼續聊，可以主動延伸話題",
            prompt,
        )
        for field in EMOTION_FIELDS:
            self.assertNotIn(field, prompt)
        self.assertIsNone(re.search(r"0\.\d\d", prompt))
        self.assertIn("- 主要情緒：平穩", build_agent_a_prompt({}, "", NEUTRAL_EMOTION_STATE, message_timestamp=0.0))


if __name__ == "__main__":
    unittest.main()
