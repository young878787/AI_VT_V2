import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from uuid import UUID


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.emotion_state import EMOTION_FIELDS, apply_mood_turn, build_mood_lines, idle_decayed_mood
from tools.ai_test.evidence import case_mode, check_turn, emotion_checks, emotion_curve, jev_evidence, linked_targets
from tools.ai_test.testset import load_suite


EVENT = "12345678-1234-1234-1234-123456789abc"


def item(text, status="active", group="group", **extra):
    return {"text": text, "status": status, "group_id": group, "type": "semantic", **extra}


def fixture(phase="recall", oracle=None):
    case = {"case_id": "mem_002", "phase": phase, "source": "fixed", "input": "本輪問題",
            "context": {}, "oracle": oracle or {}}
    mode = case_mode("memory", case)
    context = {"stage": "chat_context", "messages": [{"role": "system", "content": "角色規則"},
        {"role": "user", "content": case["input"]}], "message_origins": ["system", "input"],
        "history": [], "summary": "", "jev_recent_dialogue": [], "injected_memory_ids": [], "memory_fragments": []}
    trace = [{"stage": "jev", "role": "emotion", "answers": {}},
             {"stage": "jev", "role": "action", "answers": {"base_emotion": {"choice": "neutral", "confidence": 0.9},
              "interaction_attitude": {"choice": "smile", "confidence": 0.9}}},
             {"stage": "jev", "role": "memory", "answers": {}}, {"stage": "chat"}, context,
             {"stage": "test_mode", "mode": mode.value, "short_term_enabled": mode.short_term,
              "memory_read_enabled": mode.memory_read, "memory_write_enabled": mode.memory_write}]
    if mode.memory_read:
        trace.append({"stage": "retrieval", "embedding_available": True, "candidates": [], "projections": [], "errors": []})
    record = {"case_id": case["case_id"], "turn_id": "turn", "user": case["input"], "reply": "回答",
              "trace": trace, "stream_complete": True, "expression": "neutral", "errors": [],
              "expression_debug": {"jevDecisionSource": "jev", "jevBaseEmotionChoice": "neutral",
                  "jevInteractionAttitudeChoice": "smile", "jevBaseEmotionFallbackReason": "none",
                  "jevInteractionAttitudeFallbackReason": "none", "jevResolvedEmotion": "neutral", "jevResolvedAttitude": "smile"}, "db_unchanged": True,
              "memory_event_id": UUID(EVENT).hex if mode.memory_write else None,
              "memory_source_ids": [EVENT] if mode.memory_write else [], "memory_audit": [],
              "memory_job_status": "done", "memory_route": "process", "memory_route_finalized": True}
    state = {"items": {}, "sources": [], "evidence": [], "relations": [], "barriers": []}
    return case, record, state, context


def run_checks(case, record, before, state=None, previous=None, aliases=None, suite="memory"):
    return {check["reason_code"]: check for check in check_turn(
        suite, case, record, previous or [], before, state if state is not None else before, aliases or {})}


def inject(record, context, state, target="target", canonical="完整事實", projection=None):
    state["items"][target] = item(canonical)
    text = projection if projection is not None else "- " + canonical + "（status=active）"
    retrieval = next(trace for trace in record["trace"] if trace["stage"] == "retrieval")
    retrieval["candidates"].append({"id": target, "exact_match": True, "canonical_text": canonical})
    retrieval["projections"].append({"id": target, "text": text, "destination": "memory"})
    context["messages"][0]["content"] += "\n" + text
    context["injected_memory_ids"].append(target)
    context["memory_fragments"].append({"id": target, "text": text, "destination": "memory", "complete": True})


class AiTestEvidenceTests(unittest.TestCase):
    def test_unknown_answer_does_not_require_zero_candidates(self):
        case, record, state, context = fixture(oracle={"answerability": "unknown"})
        inject(record, context, state)
        checks = run_checks(case, record, state)
        self.assertTrue(all(check["status"] == "passed" for check in checks.values()), checks)

    def test_low_confidence_fallback_keeps_raw_call_success_visible_but_errors_fail(self):
        case, record, state, _ = fixture()
        record["expression_debug"].update(jevDecisionSource="partial_fallback", jevInteractionAttitudeFallbackReason="low_confidence")
        record["trace"][1]["answers"]["interaction_attitude"]["confidence"] = 0.3
        self.assertEqual(run_checks(case, record, state)["raw_call_and_fallback"]["status"], "passed")
        saved = copy.deepcopy(record)
        self.assertEqual(jev_evidence(record), run_checks(case, record, state)["raw_call_and_fallback"])
        self.assertEqual(record, saved)
        for index in range(3):
            record["trace"][index]["error"] = "TimeoutError"
            self.assertEqual(run_checks(case, record, state)["raw_call_and_fallback"]["status"], "failed")
            record["trace"][index].pop("error")
        memory = record["trace"].pop(2)
        self.assertEqual(jev_evidence(record)["status"], "failed")
        record["trace"].insert(2, memory)
        record["trace"].append(copy.deepcopy(memory))
        self.assertEqual(jev_evidence(record)["status"], "failed")
        record["trace"].pop()
        self.assertEqual(jev_evidence(record)["status"], "passed")
        record["expression_debug"] = {}
        self.assertEqual(run_checks(case, record, state)["raw_call_and_fallback"]["status"], "failed")

    def test_missing_alias_never_silently_passes_required_or_forbidden_ids(self):
        case, record, state, _ = fixture(oracle={"required_memory_ids": ["missing"], "forbidden_memory_ids": ["foreign"]})
        checks = run_checks(case, record, state)
        self.assertEqual(checks["required_fixture_ids_resolved"]["status"], "failed")
        self.assertEqual(checks["forbidden_fixture_ids_resolved"]["status"], "failed")

    def test_complete_projection_requires_full_canonical_and_actual_prompt_fragment(self):
        case, record, state, context = fixture(oracle={"required_memory_ids": ["gold"]})
        inject(record, context, state, canonical="完整事實，還有另一段", projection="完整事實")
        checks = run_checks(case, record, state, aliases={"gold": "target"})
        self.assertEqual(checks["required_complete_projection"]["status"], "failed")
        self.assertEqual(checks["required_prompt_injection"]["status"], "failed")
        case, record, state, context = fixture(oracle={"required_memory_ids": ["gold"]})
        inject(record, context, state)
        context["messages"][0]["content"] = "實際prompt已裁掉所有記憶"
        checks = run_checks(case, record, state, aliases={"gold": "target"})
        self.assertEqual(checks["required_complete_projection"]["status"], "passed")
        self.assertEqual(checks["required_prompt_injection"]["status"], "failed")

    def test_source_link_normalizes_uuid_hex_and_never_uses_assistant_source(self):
        case, record, state, _ = fixture("operation")
        record["memory_audit"] = [{"action": "CREATE", "target_id": "target"}]
        state["sources"] = [{"id": EVENT, "speaker": "user"}]
        state["evidence"] = [{"memory_id": "target", "source_id": EVENT}]
        self.assertEqual(linked_targets(record, state), {"target"})
        state["sources"][0]["speaker"] = "assistant"
        self.assertEqual(linked_targets(record, state), set())

    def test_no_store_forget_and_screenshot_source_references_are_not_required_db_targets(self):
        case, record, state, _ = fixture(oracle={"source_case_ids": ["old"]})
        for operation in ("no_store", "forget", None):
            checks = run_checks(case, record, state, previous=[{"case_id": "old", "operation": operation}])
            self.assertNotIn("source_targets_old", checks)
        # it_014 的 store 來源只是已遺忘事實的 lineage，不是本題必須注入的資料。
        checks = run_checks(case, record, state, previous=[{"case_id": "old", "operation": "store"}])
        self.assertNotIn("source_targets_old", checks)

    def test_required_db_lineage_cannot_pass_with_missing_or_unwritten_predecessor(self):
        case, record, state, context = fixture(oracle={"required_sources": ["db"], "source_case_ids": ["old"]})
        inject(record, context, state, target="unrelated", canonical="其他可用記憶")
        for previous in ([], [{"case_id": "old", "operation": "store", "memory_audit": []}]):
            checks = run_checks(case, record, state, previous=previous)
            self.assertEqual(checks["required_db_source"]["status"], "passed")
            self.assertEqual(checks["source_targets_old"]["status"], "failed")

    def test_forget_probe_requires_preserved_memory_instead_of_a_new_forget_target(self):
        case, record, state, context = fixture(oracle={"required_sources": ["db"],
            "source_case_ids": ["old"], "required_memory_ids": ["project"]})
        inject(record, context, state, target="project", canonical="AI VT 動作由 JEV 負責")
        for operation in ("forget", "no_store", "noise"):
            previous = [{"case_id": "old", "operation": operation}]
            checks = run_checks(case, record, state, previous=previous, aliases={"project": "project"})
            self.assertNotIn("source_targets_old", checks)
            self.assertTrue(all(check["status"] == "passed" for check in checks.values()), checks)
        # 明確要求 DB 來源時仍嚴格核對；不把不存在的來源當成成功。
        case["oracle"]["required_db_source_case_ids"] = ["old"]
        checks = run_checks(case, record, state, previous=previous, aliases={"project": "project"})
        self.assertEqual(checks["source_targets_old"]["status"], "failed")

    def test_forget_requires_all_versions_and_preserves_unrelated_memory(self):
        oracle = {"expected_operation": "forget", "forgotten_memory_ids": ["drink"], "preserved_memory_ids": ["project"]}
        case, record, state, _ = fixture("operation", oracle)
        before = copy.deepcopy(state)
        before["items"] = {"old": item("以前全糖", "superseded"), "new": item("現在無糖"),
                           "project": item("專案仍在", group="project")}
        before["relations"] = [{"from_id": "new", "to_id": "old", "kind": "supersedes"}]
        state["items"] = {"project": before["items"]["project"]}
        state["barriers"] = [{"fact_hash": hashlib.md5(text.encode()).hexdigest()} for text in ("以前全糖", "現在無糖")]
        record["memory_audit"] = [{"action": "FORGET", "target_id": None, "deleted_count": 2}]
        checks = run_checks(case, record, before, state, aliases={"drink": "new", "project": "project"})
        self.assertEqual(checks["forget_all_versions"]["status"], "passed")
        self.assertEqual(checks["forget_barrier"]["status"], "passed")
        state["items"]["old"] = before["items"]["old"]
        self.assertEqual(run_checks(case, record, before, state, aliases={"drink": "new", "project": "project"})["forget_all_versions"]["status"], "failed")
        state["items"].pop("project")
        self.assertEqual(run_checks(case, record, before, state, aliases={"drink": "new", "project": "project"})["forget_scope"]["status"], "failed")

    def test_reinforcement_choice_is_observed_without_scoring(self):
        case, record, state, _ = fixture("operation", {"expected_operation": "reinforce", "reinforced_memory_ids": ["game"]})
        before = copy.deepcopy(state)
        before["items"] = {"game": item("一直玩Minecraft")}
        state["items"] = copy.deepcopy(before["items"])
        state["sources"] = [{"id": EVENT, "speaker": "user"}]
        state["evidence"] = [{"source_id": EVENT, "memory_id": "game"}]
        record["memory_audit"] = [{"action": "REINFORCE", "target_id": "game"}]
        checks = run_checks(case, record, before, state, aliases={"game": "game"})
        self.assertNotIn("reinforcement_not_duplicate", checks)
        self.assertEqual(record["operation_observations"][-1]["observed"]["reinforced_target_ids"], ["game"])
        state["items"]["duplicate"] = item("一直玩 Minecraft。", group="new-group")
        record["memory_audit"] = [{"action": "CREATE", "target_id": "duplicate"}]
        state["evidence"] = [{"source_id": EVENT, "memory_id": "duplicate"}]
        checks = run_checks(case, record, before, state, aliases={"game": "game"})
        self.assertTrue(all(check["status"] == "passed" for check in checks.values()), checks)
        observation = record["operation_observations"][-1]
        self.assertEqual(observation["observed"]["new_same_text_ids"], ["duplicate"])
        self.assertEqual(observation["observed"]["reinforced_target_ids"], [])
        self.assertNotIn("status", observation)
        self.assertNotIn("reason", observation)

    def test_dynamic_forget_wording_is_observed_but_actual_deletion_requires_audit_and_barriers(self):
        case, record, state, _ = fixture("operation", {"expected_operation": "forget", "operation_scope": {
            "source_case_ids": ["old-case"], "identity_terms": [["低甜"]]}})
        before = copy.deepcopy(state)
        before["items"] = {"drink": item("飲品喜歡微糖", group="drink"), "project": item("專案設定", group="project")}
        before["sources"] = [{"id": "old-source", "speaker": "user"}]
        before["evidence"] = [{"memory_id": "drink", "source_id": "old-source"}]
        prior = {"case_id": "old-case", "memory_event_id": "old-source", "memory_source_ids": ["old-source"],
                 "memory_audit": [{"action": "CREATE", "target_id": "drink"}]}
        state["items"] = {"project": before["items"]["project"]}
        checks = run_checks(case, record, before, state, previous=[prior])
        self.assertNotIn("forget_targets_resolved", checks)
        self.assertEqual(record["operation_observations"][0]["observed"]["referenced_target_ids"], [])
        self.assertEqual(checks["forget_barrier"]["status"], "failed")
        record["memory_audit"] = [{"action": "FORGET", "target_id": None, "deleted_count": 1}]
        self.assertEqual(run_checks(case, record, before, state, previous=[prior])["forget_barrier"]["status"], "failed")
        state["barriers"] = [{"fact_hash": hashlib.md5("飲品喜歡微糖".encode()).hexdigest()}]
        checks = run_checks(case, record, before, state, previous=[prior])
        self.assertTrue(all(check["status"] == "passed" for check in checks.values()), checks)
        state["evidence"] = before["evidence"]
        self.assertEqual(run_checks(case, record, before, state, previous=[prior])["forget_deleted_evidence"]["status"], "failed")
        state["items"]["project"] = item("被修改", group="project")
        self.assertEqual(run_checks(case, record, before, state, previous=[prior])["forget_scope"]["status"], "failed")

    def test_no_mutation_is_not_scored_but_actual_mutations_require_user_source_evidence(self):
        case, record, state, _ = fixture("operation", {"expected_operation": "reinforce"})
        record.update(memory_route="none", memory_job_status="ignored")
        checks = run_checks(case, record, state)
        self.assertTrue(all(check["status"] == "passed" for check in checks.values()), checks)
        self.assertNotIn("committed_user_evidence", checks)
        self.assertEqual(record["operation_observations"][0]["observed"]["audit_actions"], [])
        before = copy.deepcopy(state)
        state["items"]["new"] = item("新事實")
        record.update(memory_route="process", memory_job_status="done",
                      memory_audit=[{"action": "CREATE", "target_id": "new"}])
        self.assertEqual(run_checks(case, record, before, state)["committed_user_evidence"]["status"], "failed")
        state["sources"] = [{"id": EVENT, "speaker": "assistant"}]
        state["evidence"] = [{"source_id": EVENT, "memory_id": "new"}]
        self.assertEqual(run_checks(case, record, before, state)["committed_user_evidence"]["status"], "failed")
        state["sources"][0]["speaker"] = "user"
        self.assertEqual(run_checks(case, record, before, state)["committed_user_evidence"]["status"], "passed")
        # 每個實際 mutation 都要有來源，不能以另一筆合法 evidence 掩蓋缺漏。
        state["items"]["unlinked"] = item("沒有證據")
        record["memory_audit"].append({"action": "CREATE", "target_id": "unlinked"})
        self.assertEqual(run_checks(case, record, before, state)["committed_user_evidence"]["status"], "failed")

    def test_correction_choice_is_observed_but_written_version_relations_are_validated(self):
        case, record, state, _ = fixture("operation", {"expected_operation": "correct", "source_case_ids": ["old-case"]})
        before = copy.deepcopy(state)
        before["items"] = {"old": item("以前主模型"), "game": item("Minecraft", group="game")}
        before["sources"] = [{"id": "old-source", "speaker": "user"}]
        before["evidence"] = [{"memory_id": "old", "source_id": "old-source"}]
        prior = {"case_id": "old-case", "memory_event_id": "old-source", "memory_source_ids": ["old-source"],
                 "memory_audit": [{"action": "CREATE", "target_id": "old"}]}
        state["items"] = {"old": item("以前主模型", "superseded", valid_to="now"), "new": item("現在JEV"), **{"game": before["items"]["game"]}}
        state["sources"] = [{"id": EVENT, "speaker": "user"}]
        state["evidence"] = [{"memory_id": "new", "source_id": EVENT}]
        record["memory_audit"] = [{"action": "CREATE", "target_id": "new"}]
        checks = run_checks(case, record, before, state, previous=[prior])
        self.assertNotIn("version_transition", checks)
        self.assertNotIn("version_relation_integrity", checks)
        record["memory_audit"][0]["action"] = "SUPERSEDE"
        self.assertEqual(run_checks(case, record, before, state, previous=[prior])["version_relation_integrity"]["status"], "failed")
        state["relations"] = [{"from_id": "new", "to_id": "old", "kind": "supersedes"}]
        self.assertEqual(run_checks(case, record, before, state, previous=[prior])["version_relation_integrity"]["status"], "passed")
        state["items"].pop("game")
        checks = run_checks(case, record, before, state, previous=[prior])
        self.assertNotIn("correction_scope", checks)
        self.assertEqual(record["operation_observations"][-1]["observed"]["changed_other_item_ids"], ["game"])
        state["items"]["old"]["valid_to"] = None
        self.assertEqual(run_checks(case, record, before, state, previous=[prior])["version_relation_integrity"]["status"], "failed")

    def test_required_tool_failures_are_failed_but_title_wording_is_not_scored(self):
        case, record, state, _ = fixture(oracle={"required_tools": ["get_activity_context"]})
        checks = run_checks(case, record, state, suite="integration")
        self.assertEqual(checks["required_get_activity_context"]["status"], "failed")
        tool = {"call_id": "activity", "name": "get_activity_context", "executed": True,
                "backend_status": "unavailable", "backend_reason": "unsupported_platform", "receipt_delivered": True,
                "delivery_status": "unavailable", "delivery_reason": "unsupported_platform", "receipt_matches_backend": True,
                "title_in_answer": False, "result": {"status": "unavailable", "data": None}}
        with mock.patch("tools.ai_test.evidence.collect_tools", return_value=[tool]):
            checks = run_checks(case, record, state, suite="integration")
        self.assertEqual(checks["required_get_activity_context"]["status"], "failed")
        tool.update(backend_status="ok", delivery_status="ok")
        with mock.patch("tools.ai_test.evidence.collect_tools", return_value=[tool]):
            checks = run_checks(case, record, state, suite="integration")
        self.assertEqual(checks["required_get_activity_context"]["status"], "passed")
        self.assertNotIn("activity_title_used", checks)

    def test_history_and_summary_must_reach_actual_prompt(self):
        case, record, state, context = fixture()
        case["context"] = {"history": [{"role": "user", "content": "唯一history事實"}], "summary": "唯一摘要事實"}
        case["oracle"]["required_sources"] = ["history", "summary"]
        context.update(summary="唯一摘要事實", retained_summary="唯一摘要事實")
        checks = run_checks(case, record, state)
        self.assertEqual(checks["required_history_source"]["status"], "failed")
        self.assertEqual(checks["required_summary_source"]["status"], "failed")
        context["messages"][0]["content"] += "唯一摘要事實"
        context["messages"].insert(1, {"role": "user", "content": "唯一history事實"})
        context["message_origins"].insert(1, "history")
        checks = run_checks(case, record, state)
        self.assertEqual(checks["required_history_source"]["status"], "passed")
        self.assertEqual(checks["required_summary_source"]["status"], "passed")

    def test_skipped_compression_and_old_summary_are_not_success(self):
        case, record, state, context = fixture()
        case["context"]["compress_before"] = True
        record.update(control={"acknowledged": True, "status": "skipped"}, summary_before="舊", summary_after="舊")
        self.assertEqual(run_checks(case, record, state)["real_compression"]["status"], "failed")
        record.update(control={"status": "success"}, summary_after="新", compression={
            "before_cursor": -1, "after_cursor": 1, "source_sequences": [0, 1], "source_texts": ["壓縮原文"]})
        context.update(summary="新", retained_summary="新")
        context["messages"][0]["content"] += "新"
        checks = run_checks(case, record, state)
        self.assertEqual(checks["real_compression"]["status"], "passed")
        self.assertEqual(checks["compressed_original_removed"]["status"], "passed")

    def test_six_fact_capacity_is_failed_at_five_injections(self):
        aliases = {f"fact{index}": f"target{index}" for index in range(6)}
        case, record, state, context = fixture(oracle={"capacity": True, "required_memory_ids": list(aliases)})
        for index in range(5):
            inject(record, context, state, target=f"target{index}", canonical=f"架構事實{index}")
        checks = run_checks(case, record, state, aliases=aliases)
        self.assertEqual(checks["six_independent_facts"]["status"], "failed")
        self.assertEqual(checks["required_prompt_injection"]["status"], "failed")

    def test_screenshot_encoding_hash_is_separate_from_raw_fixture_hash(self):
        case, record, state, _ = fixture(oracle={"required_tools": ["capture_screenshot"]})
        tool = {"call_id": "screen", "name": "capture_screenshot", "executed": True,
                "backend_status": "ok", "backend_reason": None, "receipt_delivered": True,
                "delivery_status": "ok", "delivery_reason": None, "receipt_matches_backend": True,
                "image_accepted": True, "image_bytes": 100, "result": {"status": "ok", "data": {
                    "image_sha256": "a" * 64, "source": {"sha256": "b" * 64}}}}
        record["trace"].append({"stage": "chat_request", "messages": [{"role": "tool",
            "tool_call_id": "screen", "content": json.dumps(tool["result"])}]})
        with mock.patch("tools.ai_test.evidence.collect_tools", return_value=[tool]):
            checks = run_checks(case, record, state)
        self.assertEqual(checks["image_hash"]["status"], "passed")
        tool["image_accepted"] = False
        with mock.patch("tools.ai_test.evidence.collect_tools", return_value=[tool]):
            self.assertEqual(run_checks(case, record, state)["image_hash"]["status"], "failed")

    def test_to005_uses_real_fixture_source_hash_without_comparing_to_jpeg_hash(self):
        data = json.loads((BACKEND_ROOT / "tools/ai_test/cases/tools.json").read_text(encoding="utf-8"))
        case = next(case for case in data["cases"] if case["case_id"] == "to_005")
        _, record, state, _ = fixture()
        reference = case["context"]["tools"]["capture_screenshot"]["asset"]
        source_hash = hashlib.sha256((BACKEND_ROOT.parent / reference).read_bytes()).hexdigest()
        tool = {"call_id": "screen", "name": "capture_screenshot", "executed": True,
                "backend_status": "ok", "backend_reason": None, "receipt_delivered": True,
                "delivery_status": "ok", "delivery_reason": None, "receipt_matches_backend": True,
                "image_accepted": True, "image_bytes": 100, "result": {"status": "ok", "data": {
                    "image_sha256": "a" * 64, "source": {"kind": "fixture_image", "ref": reference, "sha256": source_hash}}}}
        record["trace"].append({"stage": "chat_request", "messages": [{"role": "tool",
            "tool_call_id": "screen", "content": json.dumps(tool["result"])}]})
        with mock.patch("tools.ai_test.evidence.collect_tools", return_value=[tool]):
            checks = run_checks(case, record, state, suite="tools")
        self.assertEqual(checks["image_fixture_hash"]["status"], "passed")
        tool["result"]["data"]["source"]["sha256"] = "bad"
        with mock.patch("tools.ai_test.evidence.collect_tools", return_value=[tool]):
            self.assertEqual(run_checks(case, record, state, suite="tools")["image_fixture_hash"]["status"], "failed")

    def test_vision_rejection_requires_actual_replacement_receipt_and_rejection_trace(self):
        case, record, state, _ = fixture(oracle={"required_tools": ["capture_screenshot"]})
        case["context"]["vision_rejection"] = True
        tool = {"call_id": "screen", "name": "capture_screenshot", "executed": True,
                "backend_status": "ok", "backend_reason": None, "receipt_delivered": True,
                "delivery_status": "unsupported", "delivery_reason": "vision_not_supported", "receipt_matches_backend": False,
                "image_accepted": False, "image_bytes": 100, "result": {"status": "ok", "data": {"image_sha256": "a" * 64}}}
        final = {"status": "unsupported", "reason": "vision_not_supported", "data": None}
        record["trace"].extend([{"stage": "context_tool_image", "status": "unsupported",
            "reason": "vision_not_supported", "call_ids": ["screen"]}, {"stage": "chat_request", "messages": [
                {"role": "tool", "tool_call_id": "screen", "content": json.dumps(final)}]}])
        with mock.patch("tools.ai_test.evidence.collect_tools", return_value=[tool]):
            checks = run_checks(case, record, state)
        self.assertEqual(checks["expected_vision_rejection"]["status"], "passed")
        self.assertEqual(checks["receipt_content"]["status"], "passed")
        record["trace"][-1]["messages"][0]["content"] = json.dumps({**final, "data": {"old_image": "leaked"}})
        with mock.patch("tools.ai_test.evidence.collect_tools", return_value=[tool]):
            self.assertEqual(run_checks(case, record, state)["receipt_content"]["status"], "failed")


def emotion_turn(scores, before=None, turn_id="t2", emotion=None):
    """以真實 mood 純函式組出一輪 runtime 證據；呼叫端再竄改單一層驗證歸因。"""
    answers = {field: {"type": "score", "score": scores.get(field, 2), "confidence": 0.8} for field in EMOTION_FIELDS}
    current = idle_decayed_mood(before, 100.0)
    after, changes, source = apply_mood_turn(before, current, answers, turn_id, 100.0)
    system = "角色規則\n目前心情：\n" + "\n".join(f"- {line}" for line in build_mood_lines(after["mood"], changes))
    case = {"case_id": "je_008", "oracle": {"emotion": emotion or {
        "first": ["pleased"], "second": ["shy", "none"], "axes": {"wants_continue_interaction": "not_down"},
        "ideal_scores": dict.fromkeys(EMOTION_FIELDS, 2)}}}
    record = {"case_id": "je_008", "turn_id": turn_id, "emotion_source": source, "emotion_state": dict(after["mood"]),
              "emotion_record_before": copy.deepcopy(before), "emotion_record_after": copy.deepcopy(after),
              "trace": [{"stage": "jev", "role": "emotion", "answers": answers, "error": None, "question_hash": "h",
                         "state": {"current_mood": {f: round(current[f], 2) for f in EMOTION_FIELDS}, "mood_history": []}},
                        {"stage": "chat_context", "messages": [{"role": "system", "content": system}]}]}
    return case, record


def emotion_results(case, record):
    return {check["reason_code"]: check["status"] for check in emotion_checks(case, record)}


def curve_records(overrides=None):
    """以 suite 理想 score 跑完整 15 輪 runtime 證據；overrides 換掉指定輪的 score。"""
    dataset = load_suite("jev-emotion")
    records, before = [], None
    for case in dataset["cases"]:
        scores = (overrides or {}).get(case["case_id"], case["oracle"]["emotion"]["ideal_scores"])
        _, record = emotion_turn(scores, before=before, turn_id="turn-" + case["case_id"])
        record["case_id"] = case["case_id"]
        record["checks"] = emotion_checks(case, record)
        before = record["emotion_record_after"]
        records.append(record)
    return dataset, records


class EmotionEvidenceTests(unittest.TestCase):
    def test_praise_passes_and_keeps_layered_evidence(self):
        case, record = emotion_turn({"pleased": 4, "shy": 3})
        results = emotion_results(case, record)
        self.assertTrue(all(status == "passed" for status in results.values()), results)
        evidence = record["emotion_evidence"]
        self.assertEqual((evidence["judgment"]["first"], evidence["judgment"]["second"]), ("pleased", "shy"))
        self.assertEqual(evidence["failure_layers"], [])
        self.assertEqual(evidence["jev"]["scores"]["pleased"], 4)
        self.assertEqual(evidence["breakdown"]["pleased"]["jev_change"], 0.35)

    def test_continuous_turn_recomputes_from_db_record_with_streak(self):
        _, first = emotion_turn({"pleased": 4, "shy": 3}, turn_id="t1")
        case, record = emotion_turn({"pleased": 4}, before=first["emotion_record_after"])
        self.assertEqual(emotion_results(case, record)["emotion_recompute"], "passed")
        self.assertEqual(record["emotion_evidence"]["input"]["record_before"]["streak"]["pleased"], 1)

    def test_wrong_judgment_is_attributed_to_jev_not_computation(self):
        case, record = emotion_turn({"genuinely_angry": 4, "pleased": 0})
        results = emotion_results(case, record)
        for code in ("emotion_jev_scores", "emotion_recompute", "emotion_prompt_projection"):
            self.assertEqual(results[code], "passed")
        self.assertEqual(results["emotion_first"], "failed")
        self.assertEqual(results["emotion_axis_wants_continue_interaction"], "passed")
        self.assertEqual(record["emotion_evidence"]["judgment"]["first"], "genuinely_angry")
        self.assertEqual(record["emotion_evidence"]["failure_layers"], ["jev_judgment"])

    def test_persisted_mismatch_is_computation_failure(self):
        case, record = emotion_turn({"pleased": 4})
        record["emotion_record_after"]["streak"]["pleased"] = 3
        self.assertEqual(emotion_results(case, record)["emotion_recompute"], "failed")
        self.assertEqual(record["emotion_evidence"]["failure_layers"], ["computation"])
        case, record = emotion_turn({"pleased": 4})
        record["emotion_state"]["pleased"] += 0.01
        self.assertEqual(emotion_results(case, record)["emotion_recompute"], "failed")

    def test_prompt_projection_requires_block_and_no_field_names(self):
        case, record = emotion_turn({"pleased": 4})
        record["trace"][1]["messages"][0]["content"] = "角色規則\n- 主要情緒：平穩"
        self.assertEqual(emotion_results(case, record)["emotion_prompt_projection"], "failed")
        self.assertEqual(record["emotion_evidence"]["failure_layers"], ["projection"])
        case, record = emotion_turn({"pleased": 4})
        record["trace"][1]["messages"][0]["content"] += "\npleased=0.35"
        self.assertEqual(emotion_results(case, record)["emotion_prompt_projection"], "failed")

    def test_invalid_jev_fails_scores_and_every_direction_check(self):
        case, record = emotion_turn({"pleased": 4})
        record["trace"][0]["answers"]["shy"] = {"type": "score", "score": 5, "confidence": 0.8}
        record["emotion_source"] = "neutral_fallback"
        results = emotion_results(case, record)
        self.assertEqual(results["emotion_jev_scores"], "failed")
        self.assertEqual(results["emotion_axis_wants_continue_interaction"], "failed")
        self.assertEqual(record["emotion_evidence"]["failure_layers"][0], "jev_call")

    def test_calm_and_axis_directions(self):
        emotion = {"first": ["calm"], "second": ["none"], "axes": {"masking_positive_feeling": "up"},
                   "ideal_scores": dict.fromkeys(EMOTION_FIELDS, 2)}
        case, record = emotion_turn({"masking_positive_feeling": 3}, emotion=emotion)
        self.assertTrue(all(status == "passed" for status in emotion_results(case, record).values()))
        case, record = emotion_turn({"pleased": 3}, emotion=emotion)
        results = emotion_results(case, record)
        self.assertEqual(results["emotion_first"], "failed")
        self.assertEqual(results["emotion_axis_masking_positive_feeling"], "failed")
        # 逐輪硬條件只有前提三項、第一／第二名與兩軸方向；跨輪條件移到曲線觀察。
        self.assertEqual(set(results), {"emotion_jev_scores", "emotion_recompute", "emotion_prompt_projection",
                                        "emotion_first", "emotion_second", "emotion_axis_masking_positive_feeling"})

    def test_check_turn_adds_emotion_only_when_oracle_has_it(self):
        case, record, state, _ = fixture()
        self.assertFalse(any(code.startswith("emotion_") for code in run_checks(case, record, state)))
        emotion_case, emotion_record = emotion_turn({"pleased": 4})
        case["oracle"]["emotion"] = emotion_case["oracle"]["emotion"]
        record.update({key: value for key, value in emotion_record.items() if key.startswith("emotion_")})
        self.assertIn("emotion_first", run_checks(case, record, state))
        self.assertEqual(case_mode("jev-emotion", case).value, case_mode("chat", case).value)


if __name__ == "__main__":
    unittest.main()


class EmotionCurveTests(unittest.TestCase):
    def test_ideal_run_passes_every_curve_check(self):
        dataset, records = curve_records()
        curve = emotion_curve(dataset, records)
        self.assertTrue(curve["recompute_consistent"])
        self.assertEqual({check["id"]: check["status"] for check in curve["checks"]},
                         {f"C{index}": "passed" for index in range(1, 13)})
        self.assertEqual((curve["turns"]["je_010"]["first"], curve["turns"]["je_010"]["second"]),
                         ("genuinely_angry", "sad_or_hurt"))

    def test_weak_scold_is_attributed_to_its_turn(self):
        weak = {**load_suite("jev-emotion")["cases"][8]["oracle"]["emotion"]["ideal_scores"], "genuinely_angry": 2}
        dataset, records = curve_records({"je_009": weak})
        checks = {check["id"]: check for check in emotion_curve(dataset, records)["checks"]}
        self.assertEqual(checks["C6"]["status"], "failed")
        self.assertEqual(checks["C6"]["root_cause"]["kind"], "single_turn")
        self.assertEqual(checks["C6"]["root_cause"]["case_ids"], ["je_009"])
        self.assertEqual(checks["C6"]["root_cause"]["details"][0]["actual_scores"]["genuinely_angry"], 2)
        self.assertEqual(checks["C1"]["status"], "passed")

    def test_wrong_standard_and_missing_turns(self):
        dataset, records = curve_records()
        dataset["curve_checks"].append({"id": "X", "title": "錯誤標準", "conditions": [
            {"kind": "at_least", "field": "genuinely_angry", "case": "je_013", "value": 0.15}]})
        checks = {check["id"]: check for check in emotion_curve(dataset, records)["checks"]}
        self.assertEqual(checks["X"]["root_cause"]["kind"], "standard_or_parameter")
        # 只跑到 je_010：引用後段輪次的條件維持 pending，不當成失敗。
        partial = {check["id"]: check["status"] for check in emotion_curve(dataset, records[:10])["checks"]}
        self.assertEqual(partial["C6"], "passed")
        self.assertEqual(partial["C9"], "pending")
        self.assertEqual(partial["C12"], "pending")
