"""分組驗收只判定客觀執行證據；操作選擇與對話保留供後期評讀。"""

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
import re
from uuid import UUID

from domain.chat_test_mode import ChatTestMode
from domain.jev_questions import JEV_ROLES
from domain.runtime_context import CONTEXT_TOOL_NAMES, project_snapshot
from infrastructure.memory_repository import CHAT_RETRIEVAL_LIMIT, CHAT_RETRIEVAL_MIN_SIMILARITY
from tools.context_test_evidence import collect_tools

JEV_EVIDENCE_VERSION = "raw_choice_confidence_gate_v2"


def case_mode(suite, case):
    if suite in {"chat", "tools"}:
        return ChatTestMode.SHORT_ONLY
    if suite == "memory":
        return ChatTestMode.MEMORY_SEED if case["phase"] == "operation" else ChatTestMode.MEMORY_PROBE
    return ChatTestMode.MIXED_READ if case.get("source") == "generated" else ChatTestMode.MIXED_UPDATE


def context_trace(record):
    return next((t for t in record.get("trace", []) if t.get("stage") == "chat_context"), {})


def linked_targets(record, state):
    source_ids = {_id(value) for value in record.get("memory_source_ids", [])}
    event = _id(record.get("memory_event_id"))
    # 本輪 evidence 只能使用後端建立的 user source，不能以 assistant 文字補證。
    source_ids &= {_id(s["id"]) for s in state.get("sources", [])
                   if s.get("speaker") == "user" and _id(s.get("id")) == event}
    audited = {a["target_id"] for a in record.get("memory_audit", []) if a.get("target_id")}
    return {e["memory_id"] for e in state.get("evidence", [])
            if _id(e["source_id"]) in source_ids and e["memory_id"] in audited}


def _id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return value


def _text(item):
    return item.get("text", item.get("canonical_text", ""))


def _normalized(value):
    return re.sub(r"[\s，,。；;：:！!？?]", "", value).casefold().replace("台", "臺")


def _version_family(targets, state):
    """只沿正式版本／合併關係及同一group擴展，不按相似文字猜版本。"""
    family = set(targets)
    while True:
        expanded = set(family)
        groups = {state.get("items", {}).get(target, {}).get("group_id") for target in family} - {None, ""}
        expanded.update(target for target, item in state.get("items", {}).items() if item.get("group_id") in groups)
        for relation in state.get("relations", []):
            if relation.get("kind") in {"supersedes", "merged_into", "contradicts"} and (
                    relation.get("from_id") in family or relation.get("to_id") in family):
                expanded.update((relation["from_id"], relation["to_id"]))
        if expanded == family:
            return family
        family = expanded


def _scoped_targets(scope, previous, state):
    source_cases = set(scope.get("source_case_ids", []))
    linked = {target for prior in previous if prior.get("case_id") in source_cases
              for target in linked_targets(prior, state)}
    terms = scope.get("identity_terms", [])
    if terms:
        linked = {target for target in linked if any(any(_normalized(term) in _normalized(_text(
            state.get("items", {}).get(target, {}))) for term in alternatives) for alternatives in terms)}
    return linked


def _projection_complete(item, projection):
    canonical = _text(item)
    return isinstance(canonical, str) and bool(canonical) and canonical in projection.get("text", "")


def _actual_system(context):
    return "\n".join(message.get("content", "") for message in context.get("messages", [])
                     if message.get("role") == "system" and isinstance(message.get("content"), str))


def _complete_injection(item_id, projection, context):
    return (item_id in context.get("injected_memory_ids", [])
            and projection.get("text", "") in _actual_system(context)
            and any(fragment.get("id") == item_id and fragment.get("complete") is True
                    and fragment.get("text") == projection.get("text")
                    and fragment.get("destination") == projection.get("destination")
                    for fragment in context.get("memory_fragments", [])))


def _receipt_map(record):
    requests = [trace for trace in record.get("trace", []) if trace.get("stage") == "chat_request"]
    messages = requests[-1].get("messages", []) if requests else []
    return {message.get("tool_call_id"): json.loads(message["content"])
            for message in messages if message.get("role") == "tool"}


def jev_evidence(record):
    """只核對已保存的原始判斷與編譯診斷，不把合法信心門檻當成呼叫失敗。"""
    traces = [trace for trace in record.get("trace", []) if trace.get("stage") == "jev"]
    by_role = {trace.get("role"): trace for trace in traces}
    jev = by_role.get("action", {})
    roles_complete = (len(traces) == len(JEV_ROLES) and set(by_role) == JEV_ROLES
                      and all(not trace.get("error") for trace in traces))
    debug = record.get("expression_debug") or {}
    answers = jev.get("answers") or {}
    valid_raw = all(isinstance(answers.get(key), dict) and isinstance(answers[key].get("choice"), str)
                    and answers[key]["choice"] and answers[key]["choice"] != "none"
                    and answers[key]["choice"] == debug.get(f"jev{prefix}Choice")
                    and isinstance(answers[key].get("confidence"), (float, int))
                    and not isinstance(answers[key].get("confidence"), bool)
                    and math.isfinite(answers[key]["confidence"]) and 0 <= answers[key]["confidence"] <= 1
                    and debug.get(f"jev{prefix}FallbackReason") in {"none", "low_confidence"}
                    for key, prefix in (("base_emotion", "BaseEmotion"), ("interaction_attitude", "InteractionAttitude")))
    valid = (roles_complete and valid_raw
             and debug.get("jevDecisionSource") in {"jev", "fallback", "partial_fallback"}
             and isinstance(debug.get("jevResolvedEmotion"), str) and bool(debug["jevResolvedEmotion"])
             and isinstance(debug.get("jevResolvedAttitude"), str) and bool(debug["jevResolvedAttitude"]))
    return {"stage": "jev", "status": "passed" if valid else "failed",
            "reason_code": "raw_call_and_fallback",
            "actual": {"roles": {role: trace.get("error") for role, trace in by_role.items()},
                       "source": debug.get("jevDecisionSource")},
            "expected": None, "evidence": {"file": "turns.jsonl", "case_id": record.get("case_id"),
                "turn_id": record.get("turn_id"), "field": "trace"}}


def check_turn(suite, case, record, previous, before, state, fixture_ids):
    checks = []
    record["operation_observations"] = []

    def check(stage, code, valid, actual=None, expected=None):
        checks.append({"stage": stage, "status": "passed" if valid else "failed",
                       "reason_code": code, "actual": actual, "expected": expected,
                       "evidence": {"file": "turns.jsonl", "case_id": case["case_id"],
                                    "turn_id": record.get("turn_id"), "field": "trace"}})

    mode = case_mode(suite, case)
    traces = record.get("trace", [])
    context = context_trace(record)
    retrieval = next((t for t in traces if t.get("stage") == "retrieval"), {})
    applied = next((t for t in traces if t.get("stage") == "test_mode"), {})
    oracle = case.get("oracle", {})
    record["operation"] = oracle.get("expected_operation")
    check("execution", "chat_complete", record.get("stream_complete") and bool(record.get("reply"))
          and bool(record.get("expression")) and not record.get("errors"))
    check("execution", "required_trace", all(any(t.get("stage") == s for t in traces)
          for s in ("jev", "chat", "chat_context")))
    checks.append(jev_evidence(record))
    expected = {"mode": mode.value, "short_term_enabled": mode.short_term,
                "memory_read_enabled": mode.memory_read, "memory_write_enabled": mode.memory_write}
    check("isolation", "applied_mode", all(applied.get(k) == v for k, v in expected.items()), applied, expected)
    check("isolation", "read_path", bool(retrieval) == mode.memory_read)
    check("isolation", "write_path", bool(record.get("memory_event_id")) == mode.memory_write)
    if not mode.memory_write:
        check("isolation", "long_term_unchanged", before == state and record.get("db_unchanged") is True)
    if not mode.short_term or (suite == "integration" and case["case_id"] in {"it_006", "it_008", "it_014"}):
        check("isolation", "empty_short_term", bool(context) and not context.get("history")
              and not context.get("summary") and not context.get("jev_recent_dialogue"))
    if context:
        origins = context.get("message_origins", [])
        messages = context.get("messages", [])
        origin_roles = {"system": {"system"}, "input": {"user"}, "history": {"user", "assistant"},
                        "runtime_context": {"user"}, "tool_call": {"assistant"},
                        "tool_result": {"tool"}, "tool_image": {"user"}}
        origin_order = {name: index for index, name in enumerate((
            "system", "history", "runtime_context", "input", "tool_call", "tool_result", "tool_image"))}
        check("prompt", "message_origins", len(origins) == len(messages) and origins.count("input") == 1
              and origins.count("system") == 1 and bool(origins) and origins[0] == "system"
              and all(message.get("role") in origin_roles.get(origin, set())
                      for message, origin in zip(messages, origins))
              and [origin_order.get(origin, -1) for origin in origins] == sorted(origin_order.get(origin, -1) for origin in origins)
              and all(isinstance(message.get("content"), list) if origin == "tool_image" else
                      (message.get("role") == "assistant" and origin == "tool_call" and bool(message.get("tool_calls")))
                      or isinstance(message.get("content"), str) for message, origin in zip(messages, origins)))
        check("prompt", "actual_input", any(m.get("content") == record.get("user")
              for m, origin in zip(messages, origins) if origin == "input"))
    else:
        check("prompt", "context_missing", False)
    if suite in {"tools", "integration"} or context.get("tools_available"):
        first_request = next((trace for trace in traces if trace.get("stage") == "chat_request"), {})
        check("tools", "tools_available", set(context.get("tools_available", [])) == set(CONTEXT_TOOL_NAMES)
              and set(first_request.get("tools", [])) == set(CONTEXT_TOOL_NAMES))
    if suite in {"chat", "tools", "integration"} or context.get("runtime_context"):
        snapshot = context.get("runtime_context", {})
        valid_snapshot = False
        try:
            origins = context.get("message_origins", [])
            messages = context.get("messages", [])
            projected = json.loads(messages[origins.index("runtime_context")]["content"].split("\n", 1)[1])
            expected_snapshot = json.loads(project_snapshot(snapshot).split("\n", 1)[1])
            expected_apps = expected_snapshot["open_apps"]["apps"]
            actual_apps = projected["open_apps"]["apps"]
            valid_snapshot = (origins.count("runtime_context") == 1
                and projected["foreground"] == expected_snapshot["foreground"]
                and projected["desktop_captured_at"] == expected_snapshot["desktop_captured_at"]
                and projected["open_apps"]["status"] == expected_snapshot["open_apps"]["status"]
                and actual_apps == expected_apps[:len(actual_apps)]
                and (len(actual_apps) == len(expected_apps) or projected["open_apps"].get("truncated") is True))
            clock = datetime.fromisoformat(snapshot["message_time"]).strftime("%Y-%m-%d %H:%M:%S %z")
            valid_snapshot = valid_snapshot and clock in _actual_system(context)
        except (ValueError, TypeError, KeyError, IndexError):
            pass
        check("prompt", "snapshot_in_prompt", valid_snapshot)

    if mode.memory_write:
        route, status = record.get("memory_route"), record.get("memory_job_status")
        check("transaction", "terminal_job", record.get("memory_route_finalized") and (route, status) in {
            ("none", "ignored"), ("needs_context", "buffered"), ("process", "done"), ("process", "ignored")},
            [route, status, record.get("memory_generation"), record.get("memory_attempts")])
        operation = oracle.get("expected_operation")
        audit = record.get("memory_audit", [])
        targets = linked_targets(record, state)
        mutation_targets = {entry.get("target_id") for entry in audit
                            if entry.get("action") in {"CREATE", "SUPERSEDE", "CONTRADICT", "REINFORCE", "MERGE"}}
        if mutation_targets:
            check("transaction", "committed_user_evidence", status == "done"
                  and mutation_targets <= targets and mutation_targets <= state.get("items", {}).keys(),
                  {"audit_target_ids": sorted(mutation_targets, key=str), "evidence_target_ids": sorted(targets)})
        record["committed_target_ids"] = sorted(targets)
        if operation in {"store", "reinforce", "correct"}:
            record["operation_observations"].append({"operation": operation, "observed": {
                "audit_actions": [entry.get("action") for entry in audit],
                "committed_target_ids": sorted(targets),
                "new_item_ids": sorted(state.get("items", {}).keys() - before.get("items", {}).keys())}})
        if operation == "reinforce":
            aliases = oracle.get("reinforced_memory_ids", [])
            old_targets = {fixture_ids[key] for key in aliases if key in fixture_ids}
            scope = oracle.get("reinforcement_scope", {})
            old_targets |= _scoped_targets(scope, previous, before)
            old_family = _version_family(old_targets, before)
            reinforced = {entry.get("target_id") for entry in audit if entry.get("action") in {"REINFORCE", "MERGE"}}
            old_texts = {_normalized(_text(before["items"][target])) for target in old_targets
                         if target in before.get("items", {})}
            duplicated = [target for target, item in state.get("items", {}).items()
                          if target not in before.get("items", {}) and item.get("status") == "active"
                          and _normalized(_text(item)) in old_texts]
            record["operation_observations"].append({"operation": "reinforce", "observed": {
                "referenced_target_ids": sorted(old_targets), "referenced_family_ids": sorted(old_family),
                "reinforced_target_ids": sorted(reinforced), "evidence_target_ids": sorted(targets),
                "new_same_text_ids": sorted(duplicated),
                "unresolved_fixture_ids": sorted(set(aliases) - fixture_ids.keys())}})
        if operation == "correct":
            scope = oracle.get("operation_scope") or {"source_case_ids": oracle.get("source_case_ids", [])}
            old_targets = _scoped_targets(scope, previous, before)
            superseded = [t for t in old_targets if state.get("items", {}).get(t, {}).get("status") in {"superseded", "merged", "archived"}]
            transitions = [relation for relation in state.get("relations", []) if relation.get("kind") == "supersedes"
                           and relation.get("from_id") in targets and relation.get("to_id") in superseded]
            family = _version_family(old_targets, before)
            record["operation_observations"].append({"operation": "correct", "observed": {
                "referenced_target_ids": sorted(old_targets), "supersedes_relations": transitions,
                "changed_other_item_ids": sorted(target for target, item in before.get("items", {}).items()
                    if target not in family and item != state.get("items", {}).get(target))}})
        # 是否應採用更正而非新增留待評讀；實際提交的版本操作仍須符合 DB 結構契約。
        new_transitions = [relation for relation in state.get("relations", [])
                           if relation.get("kind") == "supersedes" and relation not in before.get("relations", [])]
        supersede_targets = {entry.get("target_id") for entry in audit if entry.get("action") == "SUPERSEDE"}
        if new_transitions or supersede_targets:
            items = state.get("items", {})
            check("transaction", "version_relation_integrity",
                  supersede_targets <= {relation.get("from_id") for relation in new_transitions} and all(
                      relation.get("from_id") in items and relation.get("to_id") in items
                      and items[relation["to_id"]].get("status") in {"superseded", "merged", "archived"}
                      and items[relation["to_id"]].get("valid_to") is not None
                      and items[relation["from_id"]].get("status") == "active"
                      and items[relation["from_id"]].get("group_id") is not None
                      and items[relation["from_id"]].get("group_id") == items[relation["to_id"]].get("group_id")
                      for relation in new_transitions), new_transitions)
        if operation in {"no_store", "noise"}:
            check("transaction", "no_store_or_noise", not audit and before.get("items") == state.get("items")
                  and before.get("evidence") == state.get("evidence") and before.get("sources") == state.get("sources"))
        if operation == "forget":
            aliases = oracle.get("forgotten_memory_ids", [])
            forgotten = {fixture_ids[key] for key in aliases if key in fixture_ids}
            forgotten = _version_family(forgotten, before)
            observed_targets = _scoped_targets(oracle.get("operation_scope", {}), previous, before)
            deleted = before.get("items", {}).keys() - state.get("items", {}).keys()
            record["operation_observations"].append({"operation": "forget", "observed": {
                "referenced_target_ids": sorted(observed_targets), "fixture_target_ids": sorted(forgotten),
                "deleted_item_ids": sorted(deleted)}})
            if aliases:
                check("transaction", "forget_targets_resolved", bool(forgotten) and all(key in fixture_ids for key in aliases), sorted(forgotten))
                remaining = forgotten & state.get("items", {}).keys()
                remaining_evidence = [entry for entry in state.get("evidence", []) if entry.get("memory_id") in forgotten]
                check("transaction", "forget_all_versions", bool(forgotten) and not remaining and not remaining_evidence,
                      {"forgotten_ids": sorted(forgotten), "remaining_ids": sorted(remaining)})
            forget_audit = [entry for entry in audit if entry.get("action") == "FORGET"]
            expected_hashes = {hashlib.md5(_text(before["items"][target]).encode("utf-8")).hexdigest()
                               for target in deleted}
            barrier_hashes = {entry.get("fact_hash") for entry in state.get("barriers", [])}
            if deleted or forget_audit:
                check("transaction", "forget_barrier", status == "done" and bool(forget_audit)
                      and sum(entry.get("deleted_count") or 0 for entry in forget_audit) == len(deleted)
                      and expected_hashes <= barrier_hashes,
                      {"deleted_ids": sorted(deleted), "expected_fact_hashes": sorted(expected_hashes),
                       "barrier_fact_hashes": sorted(barrier_hashes - {None})})
                check("transaction", "forget_deleted_evidence", not any(
                    entry.get("memory_id") in deleted for entry in state.get("evidence", [])), sorted(deleted))
            # 固定 fixture 可客觀指定刪除範圍；動態文字選題只觀察，保留項仍禁止被連帶改寫。
            mutable = forgotten if aliases else deleted
            check("transaction", "forget_scope", all(item == state.get("items", {}).get(target)
                  for target, item in before.get("items", {}).items() if target not in mutable))

    candidates = retrieval.get("candidates", [])
    candidate_ids = {str(c.get("id")) for c in candidates}
    projections = {str(p.get("id")): p for p in retrieval.get("projections", [])}
    projected_ids = set(projections)
    injected_ids = set(context.get("injected_memory_ids", []))
    if mode.memory_read:
        check("retrieval", "retrieval_complete", bool(retrieval) and not retrieval.get("errors") and retrieval.get("embedding_available"))
        check("retrieval", "candidate_qualification", len(candidates) <= CHAT_RETRIEVAL_LIMIT and all(
            c.get("exact_match") or isinstance(c.get("similarity"), (float, int))
            and c["similarity"] >= CHAT_RETRIEVAL_MIN_SIMILARITY for c in candidates))
        check("isolation", "candidate_owner", candidate_ids <= state.get("items", {}).keys(), sorted(candidate_ids - state.get("items", {}).keys()))
        forbidden = {fixture_ids[k] for k in oracle.get("forbidden_memory_ids", []) if k in fixture_ids}
        missing_forbidden = set(oracle.get("forbidden_memory_ids", [])) - fixture_ids.keys()
        check("fixture", "forbidden_fixture_ids_resolved", not missing_forbidden, sorted(missing_forbidden))
        check("isolation", "forbidden_candidates_and_injection", not forbidden & (candidate_ids | projected_ids | injected_ids), sorted(forbidden))
        required = {fixture_ids[k] for k in oracle.get("required_memory_ids", []) if k in fixture_ids}
        missing_aliases = set(oracle.get("required_memory_ids", [])) - fixture_ids.keys()
        check("fixture", "required_fixture_ids_resolved", not missing_aliases, sorted(missing_aliases))
        required_source_cases = oracle.get("required_db_source_case_ids")
        if required_source_cases is None:
            required_source_cases = oracle.get("source_case_ids", []) if "db" in oracle.get("required_sources", []) else []
            # 遺忘／禁止保存的前序只是對話與操作來源，不應要求召回其新建 target。
            non_writing_sources = {prior["case_id"] for prior in previous
                                   if prior.get("operation") in {"forget", "no_store", "noise"}}
            required_source_cases = [source for source in required_source_cases if source not in non_writing_sources]
        source_ids = {source: [] for source in required_source_cases}
        for prior in previous:
            if prior["case_id"] in required_source_cases and prior.get("operation") in {"store", "reinforce", "correct"}:
                source_ids[prior["case_id"]] = sorted(linked_targets(prior, state))
        # 操作可能合併多事實；按實際來源提案的 target 追蹤，不預設拆筆數。
        for source, ids in source_ids.items():
            complete_ids = {target for target in ids if target in projections and _projection_complete(
                state.get("items", {}).get(target, {}), projections[target])
                and _complete_injection(target, projections[target], context)}
            check("injection", "source_targets_" + source, bool(complete_ids),
                  {"source_target_ids": ids, "complete_injected_ids": sorted(complete_ids)})
        check("retrieval", "required_candidates", required <= candidate_ids, sorted(required - candidate_ids))
        complete_projections = {target for target in projected_ids if _projection_complete(
            state.get("items", {}).get(target, {}), projections[target])}
        complete_injections = {target for target in complete_projections
                               if _complete_injection(target, projections[target], context)}
        check("projection", "required_complete_projection", required <= complete_projections, sorted(required - complete_projections))
        check("injection", "required_prompt_injection", required <= complete_injections, sorted(required - complete_injections))
        profile_ids = {target for target, projection in projections.items() if projection.get("destination") == "profile"}
        check("injection", "profile_source_mapping", profile_ids <= complete_injections,
              {"profile_ids": sorted(profile_ids), "unmapped_or_incomplete": sorted(profile_ids - complete_injections)})
        if oracle.get("capacity"):
            check("capacity", "six_independent_facts", len(required) == 6 and required <= complete_injections,
                  {"retrieval_limit": CHAT_RETRIEVAL_LIMIT, "required": len(required), "complete_injected": len(required & complete_injections)})
        all_required = required | {target for ids in source_ids.values() for target in ids}
        record["retrieval_metrics"] = {
            "required_ids": sorted(all_required), "candidate_ids": sorted(candidate_ids),
            "projection_ids": sorted(projected_ids), "injected_ids": sorted(injected_ids),
            "required_recalled": len(all_required & candidate_ids), "required_projected": len(all_required & complete_projections),
            "required_injected": len(all_required & complete_injections), "required_count": len(all_required),
            "candidate_precision": len(all_required & candidate_ids) / len(candidate_ids) if candidate_ids else None,
            "non_target_candidates": len(candidate_ids - all_required),
            "lexical_coverage": len({str(c["id"]) for c in candidates if c.get("exact_match")} & all_required),
            "vector_coverage": len({str(c["id"]) for c in candidates if isinstance(c.get("similarity"), (float, int))
                                    and c["similarity"] >= CHAT_RETRIEVAL_MIN_SIMILARITY} & all_required),
        }

    try:
        tools = record["tools"] = collect_tools(record)
        receipts = _receipt_map(record)
        check("tools", "parse_receipts", True)
    except (ValueError, TypeError, KeyError):
        tools, receipts = [], {}
        record["tools"] = []
        check("tools", "parse_receipts", False)
    check("tools", "receipt_pairs", all(t["receipt_delivered"] for t in tools))
    rejection = case.get("context", {}).get("vision_rejection", False)
    check("tool_delivery", "receipt_content", all(t["receipt_matches_backend"] or (
        rejection and t["name"] == "capture_screenshot" and t["backend_status"] == "ok"
        and receipts.get(t["call_id"], {}).get("status") == "unsupported"
        and receipts.get(t["call_id"], {}).get("reason") == "vision_not_supported"
        and receipts.get(t["call_id"], {}).get("data") is None) for t in tools))
    ids = [tool["call_id"] for tool in tools]
    check("tools", "unique_call_ids", len(ids) == len(set(ids)))
    check("tools", "execution_bounds", sum(t["executed"] for t in tools) <= 2
          and sum(t["executed"] and t["name"] == "capture_screenshot" for t in tools) <= 1)
    if "allowed_tools" in oracle:
        check("tool_selection", "allowed_tools", all(tool["name"] in oracle["allowed_tools"] for tool in tools))
    ambiguous = "allowed_tools" in oracle and not oracle.get("required_tools") and "get_weather" in oracle["allowed_tools"]
    if ambiguous:
        check("tool_delivery", "ambiguous_weather_only", all(tool["backend_reason"] in {"ambiguous_region", "location_required"}
              and tool["delivery_reason"] in {"ambiguous_region", "location_required"}
              and tool["receipt_matches_backend"] for tool in tools if tool["name"] == "get_weather"))
    for name in oracle.get("required_tools", []):
        selected = [t for t in tools if t["name"] == name]
        success = any(t["executed"] and t["backend_status"] == "ok" and t["receipt_delivered"]
                      and t["delivery_status"] == "ok" for t in selected)
        if name == "capture_screenshot" and rejection:
            success = any(t["executed"] and t["backend_status"] == "ok" and t["receipt_delivered"]
                          and t["delivery_status"] == "unsupported" and t["delivery_reason"] == "vision_not_supported"
                          and not t["image_accepted"] for t in selected)
        check("tool_selection", "required_" + name, success, selected)
        if name == "capture_screenshot" and not rejection:
            check("tool_delivery", "image_delivery", any(t["image_accepted"] and t["image_bytes"] > 0 for t in selected))
            for tool in selected:
                data = (tool.get("result") or {}).get("data") or {}
                source = data.get("source") or {}
                image_hash = data.get("image_sha256")
                hash_valid = isinstance(image_hash, str) and re.fullmatch(r"[0-9a-f]{64}", image_hash)
                check("tool_delivery", "image_hash", bool(hash_valid) and tool["image_accepted"],
                      {"image_sha256": image_hash, "source": source})
                fixture_reference = case.get("context", {}).get("tools", {}).get(name, {}).get("asset")
                if fixture_reference:
                    root = Path(__file__).resolve().parents[2]
                    path = (root / fixture_reference).resolve()
                    expected_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_relative_to(root) and path.is_file() else None
                    check("fixture", "image_fixture_hash", expected_hash is not None and source.get("sha256") == expected_hash
                          and source.get("ref") == fixture_reference and source.get("kind") == "fixture_image",
                          source, {"ref": fixture_reference, "sha256": expected_hash})
        if name == "capture_screenshot" and rejection:
            rejected = [entry for entry in traces if entry.get("stage") == "context_tool_image"
                        and entry.get("status") == "unsupported" and entry.get("reason") == "vision_not_supported"]
            check("tool_delivery", "expected_vision_rejection", bool(rejected) and any(
                tool["call_id"] in entry.get("call_ids", []) and tool["receipt_delivered"]
                and tool["delivery_status"] == "unsupported" and tool["delivery_reason"] == "vision_not_supported"
                and not tool["image_accepted"] and tool["image_bytes"] > 0 for tool in selected for entry in rejected))
    for name, expected_args in oracle.get("expected_tool_arguments", {}).items():
        check("tools", "arguments_" + name, any(t["name"] == name and all(
            _normalized(str((t.get("arguments") or {}).get(k, ""))) == _normalized(str(v))
            for k, v in expected_args.items()) for t in tools), expected_args)
    if not mode.short_term:
        check("isolation", "db_probe_without_desktop_answer", not any(t["name"] in {
            "get_activity_context", "capture_screenshot"} and t["delivery_status"] == "ok" for t in tools))
    for key in oracle.get("preserved_memory_ids", []):
        item_id = fixture_ids.get(key)
        check("transaction", "preserved_" + key, item_id is not None and before.get("items", {}).get(item_id)
              == state.get("items", {}).get(item_id), item_id)
    messages, origins = context.get("messages", []), context.get("message_origins", [])
    actual_history = [message for message, origin in zip(messages, origins) if origin == "history"]
    for source in oracle.get("required_sources", []):
        if source == "history":
            fixture_history = case.get("context", {}).get("history", [])
            complete = bool(actual_history) and all(any(message.get("role") == expected.get("role")
                and message.get("content") == expected.get("content") for message in actual_history) for expected in fixture_history)
            check("prompt", "required_history_source", complete,
                  {"fixture_messages": len(fixture_history), "actual_messages": len(actual_history)})
        elif source == "summary":
            summary = record.get("summary_after") if case.get("context", {}).get("compress_before") else case.get("context", {}).get("summary")
            summary = summary or context.get("summary")
            check("prompt", "required_summary_source", bool(summary) and context.get("summary") == summary
                  and context.get("retained_summary") == summary and summary in _actual_system(context))
        elif source == "db":
            check("prompt", "required_db_source", mode.memory_read and bool(injected_ids)
                  and bool(retrieval) and any(_complete_injection(target, projection, context)
                                             for target, projection in projections.items()))
        elif source == "tool":
            check("prompt", "required_tool_source", bool(tools) and all(tool["receipt_delivered"] for tool in tools))
        else:
            check("fixture", "unknown_required_source", False, source)
    if case.get("context", {}).get("compress_before"):
        summary = record.get("summary_after")
        compression = record.get("compression") or {}
        before_cursor = compression.get("before_cursor", (compression.get("before") or {}).get("summary_through_sequence"))
        after_cursor = compression.get("after_cursor", (compression.get("after") or {}).get("summary_through_sequence"))
        sequences = compression.get("source_sequences", compression.get("compressed_sequences", []))
        check("summary", "real_compression", record.get("control", {}).get("status") == "success"
              and bool(summary) and summary != record.get("summary_before")
              and isinstance(before_cursor, int) and isinstance(after_cursor, int) and after_cursor > before_cursor
              and bool(sequences) and all(before_cursor < sequence <= after_cursor for sequence in sequences), compression)
        check("summary", "summary_in_actual_prompt", bool(summary) and context.get("summary") == summary
              and context.get("retained_summary") == summary and summary in _actual_system(context))
        compressed_texts = compression.get("source_texts", [])
        check("summary", "compressed_original_removed", bool(compressed_texts)
              and all(not any(message.get("content") == text for message in actual_history) for text in compressed_texts),
              {"compressed_source_count": len(compressed_texts), "remaining_history_count": len(actual_history)})
    record["checks"] = checks
    return checks
