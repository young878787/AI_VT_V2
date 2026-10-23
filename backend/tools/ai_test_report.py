"""分組 AI 測試的離線摘要與閱讀視圖；不呼叫模型、工具或資料庫。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 2
EVIDENCE_FILES = ("cases.json", "turns.jsonl", "case_states.jsonl")
ATTENTION_LIMIT = 100
_AI_START = "<!-- AI_SYNTHESIS_START basis="
_AI_END = "<!-- AI_SYNTHESIS_END -->"


def fingerprint(value: Any) -> str:
    """JSON 內容指紋；檔案指紋另以實際 bytes 計算。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str | None:
    target = Path(path)
    return hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_jsonl(path: Path) -> tuple[list[dict], list[dict]]:
    rows, errors = [], []
    if not path.is_file():
        return rows, [{"stage": "artifacts", "reason_code": "missing_artifact", "evidence": path.name}]
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("record must be an object")
            rows.append(value)
        except (ValueError, TypeError) as exc:
            errors.append({"stage": "artifacts", "reason_code": "invalid_jsonl",
                           "evidence": f"{path.name}:line={number}", "detail": str(exc)})
    return rows, errors


def _cases(snapshot: Any) -> list[dict]:
    if isinstance(snapshot, list):
        return snapshot
    return snapshot.get("cases", []) if isinstance(snapshot, dict) else []


def _basis(run_dir: Path) -> dict:
    return {name: file_sha256(run_dir / name) for name in EVIDENCE_FILES}


def initialize_report(run_dir: str | Path, suite: str, cases_snapshot: Any, provenance: dict) -> None:
    """開新一輪時覆寫 latest 的輸入與人工判斷，不刪除任何歷史目錄。"""
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "cases.json", cases_snapshot)
    for name in EVIDENCE_FILES[1:]:
        _atomic_text(directory / name, "")
    review = {"schema_version": SCHEMA_VERSION, "suite": suite, "basis": _basis(directory),
              "cases": {case["case_id"]: {"status": "pending", "reason": None,
                         "turn_id": None, "evidence": []} for case in _cases(cases_snapshot)}}
    _write_json(directory / "review.json", review)
    # 清除舊綜整，避免在第一筆結果到來前讀到上輪結論。
    _atomic_text(directory / "report.md", "# AI 測試報告\n\n尚未執行；人工檢視與 AI 綜整已重設。\n")
    metadata = {"suite": suite, "provenance": provenance, "started_at": _now(),
                "planned_case_ids": [case["case_id"] for case in _cases(cases_snapshot)]}
    write_reports(directory, metadata, [], [], execution="running")


def _bucket(case: dict) -> str:
    if case.get("case_id") == "mem_014" or case.get("capacity") or case.get("phase") == "capacity":
        return "capacity"
    source = case.get("source", case.get("source_type", "fixed"))
    return "generated" if source == "generated" or case.get("generated") else "fixed_core"


def _combine(statuses: list[str], empty: str = "pending") -> str:
    if not statuses:
        return empty
    if "failed" in statuses:
        return "failed"
    if any(status != "passed" for status in statuses):
        return "pending"
    return "passed"


def _check_status(check: dict) -> str:
    status = check.get("status", "pending")
    if status in ("passed", "pass", "correct", "accepted"):
        return "passed"
    if status in ("failed", "fail", "incorrect", "needs_changes"):
        return "failed"
    return "pending"


def _hard_status(check: dict) -> str:
    status = _check_status(check)
    return "failed" if status == "passed" and not check.get("evidence") else status


def _turn_complete(record: dict) -> bool:
    return (record.get("request_sent") is not False and not record.get("interrupted")
            and not record.get("errors") and record.get("stream_complete", bool(record.get("reply"))) is True)


def _evidence(case_id: str, record: dict | None, field: str) -> str:
    turn = (record or {}).get("turn_id", "unexecuted")
    return f"turns.jsonl#case_id={case_id}&turn_id={turn}&field={field}"


def _image_references(value: Any) -> list[str]:
    references = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and key in ("asset_path", "report_asset", "asset", "image_path", "artifact", "path", "preview"):
                if re.search(r"\.(?:jpe?g|png|webp|gif)$", item, re.IGNORECASE):
                    references.append(item)
            elif isinstance(item, (dict, list)):
                references.extend(_image_references(item))
    elif isinstance(value, list):
        for item in value:
            references.extend(_image_references(item))
    return list(dict.fromkeys(references))


def _local_asset(directory: Path, reference: str) -> Path | None:
    if "://" in reference or Path(reference).is_absolute():
        return None
    candidate = (directory / reference).resolve()
    return candidate if candidate.is_relative_to(directory.resolve()) else None


def _attention(case_id: str | None, record: dict | None, stage: str,
               reason_code: str, evidence: Any = None, **extra: Any) -> dict:
    return {"case_id": case_id, "turn_id": (record or {}).get("turn_id"), "stage": stage,
            "reason_code": reason_code, "evidence": evidence or _evidence(case_id or "", record, stage), **extra}


def _cost(metadata: dict, records: list[dict]) -> dict:
    given = metadata.get("cost") or {}
    role_values = given.get("roles") or given
    result = {role: {"calls": None, "input_tokens": None, "output_tokens": None,
                     **(role_values.get(role, role_values.get("generator") if role == "generation" else None) or {})}
              for role in ("chat", "jev", "memory_agent", "generation", "embedding")}
    if given.get("embedding") is not None:
        result["embedding"].update(given["embedding"])
    if "summary" in role_values:
        result["summary"] = role_values["summary"]
    for key in ("job_wait_sec", "wall_clock_sec", "stage_cumulative_sec"):
        result[key] = given.get(key)
    # 缺少 usage 不推測成零；runner 已取得的額外成本指標原樣保留。
    result.update({key: value for key, value in given.items() if key not in result and key not in ("roles", "judge", "judge_sec")})
    return result


def _metric(checks: list[dict], stages: tuple[str, ...]) -> dict:
    selected = [check for check in checks if check.get("stage") in stages]
    return {"passed": sum(_hard_status(check) == "passed" for check in selected),
            "total": len(selected),
            "rate": (sum(_hard_status(check) == "passed" for check in selected) / len(selected)) if selected else None}


def _rate(numerator: int | None, denominator: int | None) -> float | None:
    return numerator / denominator if numerator is not None and denominator else None


def _sum_metric(rows: list[dict], key: str) -> int | None:
    values = [row.get(key) for row in rows]
    return sum(values) if all(isinstance(value, int) and value >= 0 for value in values) else None


def _metrics(records: list[dict], cases: dict, checks: list[dict], attention: list[dict]) -> dict:
    """分子分母來自實際證據；不把硬條件通過率誤稱為事實召回率。"""
    retrieval = [record["retrieval_metrics"] for record in records if isinstance(record.get("retrieval_metrics"), dict)]
    required = _sum_metric(retrieval, "required_count")
    recalled = _sum_metric(retrieval, "required_recalled")
    projected = _sum_metric(retrieval, "required_projected")
    injected = _sum_metric(retrieval, "required_injected")
    lexical = _sum_metric(retrieval, "lexical_coverage")
    vector = _sum_metric(retrieval, "vector_coverage")
    candidate_count = sum(len(row["candidate_ids"]) for row in retrieval) if all(isinstance(row.get("candidate_ids"), list) for row in retrieval) else None
    selection, delivery = [], []
    delivery_codes = {"receipt_pairs", "receipt_content", "receipt_contents", "image_delivery", "image_hash",
                      "image_fixture_hash", "expected_vision_rejection", "screenshot_image", "screenshot_fixture"}
    for record in records:
        oracle = cases.get(record.get("case_id"), {}).get("oracle", {})
        names = set(oracle.get("required_tools", [])) | {tool.get("name") for tool in record.get("tools", [])}
        if not names:
            continue
        selection.extend(check for check in record.get("checks", []) if check.get("reason_code") in {"required_" + name for name in names if name})
        delivery.extend(check for check in record.get("checks", []) if check.get("reason_code") in delivery_codes)

    def checked(values):
        passed = sum(_hard_status(check) == "passed" for check in values)
        return {"passed": passed, "total": len(values), "rate": _rate(passed, len(values))}

    return {"retrieval": {"required": required, "recalled": recalled, "rate": _rate(recalled, required)},
            "projection": {"required": required, "projected": projected, "rate": _rate(projected, required)},
            "injection": {"required": required, "injected": injected, "rate": _rate(injected, required)},
            "candidate_precision": {"relevant_candidates": recalled, "candidate_count": candidate_count,
                                    "rate": _rate(recalled, candidate_count)},
            "non_target_candidates": _sum_metric(retrieval, "non_target_candidates"),
            "lexical_coverage": {"covered": lexical, "required": required, "rate": _rate(lexical, required)},
            "vector_coverage": {"covered": vector, "required": required, "rate": _rate(vector, required)},
            "tool_selection": checked(selection), "tool_delivery": checked(delivery),
            "check_pass_rates": {name: _metric(checks, stages) for name, stages in (
                ("retrieval", ("retrieval", "recall")), ("projection", ("projection",)),
                ("injection", ("injection", "prompt")))},
            "errors": sum(len(record.get("errors") or []) for record in records),
            "missing_evidence": sum(item["reason_code"] in ("missing_evidence", "missing_image", "missing_artifact") for item in attention)}


def write_reports(run_dir: str | Path, metadata: dict, records: list[dict], states: list[dict],
                  execution: str = "running", error: Any = None, cleanup: Any = None) -> dict:
    """以已保存的逐輪證據衍生摘要。JSONL 由 runner 寫入，本函式不重送輸入。"""
    directory = Path(run_dir)
    artifact_issues = list(metadata.get("artifact_issues") or [])
    try:
        snapshot = _read_json(directory / "cases.json", {"cases": metadata.get("cases", [])})
    except (ValueError, TypeError) as exc:
        snapshot = {"cases": metadata.get("cases", [])}
        artifact_issues.append({"stage": "artifacts", "reason_code": "invalid_cases_json",
                                "evidence": "cases.json", "detail": str(exc)})
    cases = _cases(snapshot)
    by_case = {case["case_id"]: case for case in cases}
    planned = metadata.get("planned_case_ids") or list(by_case)
    selected = metadata.get("selected_case_ids") or planned
    for case_id in planned:
        by_case.setdefault(case_id, {"case_id": case_id})
    basis = _basis(directory)
    try:
        review = _read_json(directory / "review.json", {})
    except (ValueError, TypeError) as exc:
        review = {}
        artifact_issues.append({"stage": "artifacts", "reason_code": "invalid_review_json",
                                "evidence": "review.json", "detail": str(exc)})
    judgments = review.get("cases", {})
    if isinstance(judgments, list):
        judgments = {entry["case_id"]: entry for entry in judgments}
    has_judgment = any(value.get("status", "pending") != "pending" for value in judgments.values())
    if not has_judgment and not metadata.get("preserve_review_basis") and (review.get("basis") != basis or review.get("suite") != metadata["suite"]):
        review = {"schema_version": SCHEMA_VERSION, "suite": metadata["suite"], "basis": basis, "cases": judgments}
        _write_json(directory / "review.json", review)
    review_stale = review.get("basis") != basis or review.get("suite") != metadata["suite"]
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record.get("case_id", ""), []).append(record)
    attention, outcomes, all_checks = [], {}, []
    for name, expected_records in (("turns.jsonl", records), ("case_states.jsonl", states)):
        saved, issues = _read_jsonl(directory / name)
        artifact_issues.extend(issues)
        if fingerprint(saved) != fingerprint(expected_records):
            artifact_issues.append({"stage": "artifacts", "reason_code": "unsaved_evidence",
                                    "evidence": name})
    for issue in artifact_issues:
        attention.append(_attention(None, None, issue.get("stage", "artifacts"),
                                    issue.get("reason_code", "invalid_artifact"), issue.get("evidence"),
                                    detail=issue.get("detail")))
    if review_stale:
        attention.append(_attention(None, None, "human_review", "stale_review", "review.json#basis"))
    if execution != "completed":
        attention.append(_attention(None, None, "execution", "execution_" + execution, "run.json#execution.status"))
    if metadata.get("failure"):
        failure = metadata["failure"]
        attention.append(_attention(failure.get("case_id"), None, failure.get("stage", "runner"),
                                    "runner_failure", "run.json#execution.failure"))
    human_required = metadata["suite"] == "integration"
    state_ids = {state.get("case_id") for state in states}
    for case_id in planned:
        case, turns = by_case[case_id], grouped.get(case_id, [])
        submitted = [record for record in turns if record.get("request_sent") is not False]
        checks = [check for record in turns for check in (record.get("checks") or [])]
        all_checks.extend(checks)
        hard = _combine([_hard_status(check) for check in checks])
        judgment = judgments.get(case_id, {})
        human = judgment.get("status", "pending") if not review_stale else "pending"
        if human != "pending":
            turn_ids = {record.get("turn_id") for record in turns}
            valid_reference = judgment.get("turn_id") in turn_ids or any(
                turn_id and turn_id in _json(judgment.get("evidence", [])) for turn_id in turn_ids)
            if not judgment.get("reason") or not valid_reference or human not in ("accepted", "needs_changes", "uncertain"):
                human = "pending"
                attention.append(_attention(case_id, turns[-1] if turns else None, "human_review",
                                            "invalid_review_reference", f"review.json#cases.{case_id}"))
        status = "completed" if submitted else ("failed" if turns else "pending")
        if not submitted:
            attention.append(_attention(case_id, None, "execution", "unexecuted_case", f"cases.json#case_id={case_id}"))
        elif not checks:
            attention.append(_attention(case_id, turns[-1], "hard_checks", "missing_evidence"))
        if submitted and metadata["suite"] in ("memory", "integration") and case_id not in state_ids:
            hard = _combine([hard, "failed" if execution in ("completed", "failed") else "pending"])
            attention.append(_attention(case_id, turns[-1], "state", "missing_case_state",
                                        f"case_states.jsonl#case_id={case_id}"))
        for record in turns:
            if record.get("interrupted"):
                status = "interrupted"
            if record.get("errors"):
                hard = "failed"
                status = "failed"
                for failure in record["errors"]:
                    detail = failure if isinstance(failure, str) else failure.get("reason_code", str(failure))
                    attention.append(_attention(case_id, record, "execution", "execution_error",
                                                _evidence(case_id, record, "errors"), detail=detail))
            for check in record.get("checks") or []:
                if _hard_status(check) != "passed":
                    attention.append(_attention(case_id, record, check.get("stage", "hard_checks"),
                                                ("missing_evidence" if _check_status(check) == "passed" else check.get("reason_code")) or "check_pending", check.get("evidence")))
            expression = record.get("expression_debug") or {}
            if expression.get("fallback_reason") or expression.get("jevFallbackReason") or record.get("jev_fallback_reason") or "fallback" in str(expression.get("jevDecisionSource", "")):
                attention.append(_attention(case_id, record, "jev", "jev_fallback",
                                            _evidence(case_id, record, "expression_debug")))
            for reference in _image_references(record):
                asset = _local_asset(directory, reference)
                if asset is None or not asset.is_file():
                    hard = "failed"
                    attention.append(_attention(case_id, record, "image", "missing_image", reference))
            for asset in record.get("assets", []):
                target = _local_asset(directory, asset.get("path", ""))
                if target is not None and target.is_file() and asset.get("sha256") != file_sha256(target):
                    hard = "failed"
                    attention.append(_attention(case_id, record, "image", "image_hash_mismatch", asset["path"]))
        if human_required and human != "accepted":
            attention.append(_attention(case_id, turns[-1] if turns else None, "human_review",
                                        f"human_{human}", f"review.json#cases.{case_id}"))
        if submitted and hard == "pending" and execution in ("completed", "failed"):
            hard = "failed"
            attention.append(_attention(case_id, turns[-1], "hard_checks", "incomplete_objective_checks"))
        if (metadata.get("failure") or {}).get("case_id") == case_id:
            hard = "failed"
        if hard == "failed":
            status = "failed"
        overall = _combine([hard] + ([_check_status({"status": human})] if human_required else []))
        outcomes[case_id] = {"execution": status, "hard": hard,
                             "human": human, "acceptance": overall, "bucket": _bucket(case),
                             "duration_sec": sum(record["duration_sec"] for record in turns) if turns and all(isinstance(record.get("duration_sec"), (float, int)) for record in turns) else None}
    acceptance = {name: _combine([outcome["acceptance"] for outcome in outcomes.values() if outcome["bucket"] == name],
                                 "not_applicable") for name in ("fixed_core", "generated", "capacity")}
    acceptance["overall"] = _combine([value for value in acceptance.values() if value != "not_applicable"])
    if any(issue.get("reason_code") != "invalid_review_json" for issue in artifact_issues):
        acceptance["overall"] = "failed"
    if review_stale or any(issue.get("reason_code") == "invalid_review_json" for issue in artifact_issues):
        acceptance["overall"] = _combine([acceptance["overall"], "pending"])
    if execution == "failed":
        acceptance["overall"] = "failed"
    elif execution != "completed":
        acceptance["overall"] = _combine([acceptance["overall"], "pending"])
    submitted = sum(any(record.get("request_sent") is not False for record in grouped.get(case_id, [])) for case_id in planned)
    completed = sum(any(_turn_complete(record) for record in grouped.get(case_id, [])) for case_id in planned)
    counts = {"fixed": sum(outcome["bucket"] != "generated" for outcome in outcomes.values()),
              "generated": sum(outcome["bucket"] == "generated" for outcome in outcomes.values()),
              "fixed_core": sum(outcome["bucket"] == "fixed_core" for outcome in outcomes.values()),
              "capacity": sum(outcome["bucket"] == "capacity" for outcome in outcomes.values()),
              "executed": submitted, "unexecuted": len(planned) - submitted,
              "hard": dict(Counter(outcome["hard"] for outcome in outcomes.values())),
              "human_pending": sum(outcome["human"] != "accepted" for outcome in outcomes.values()) if human_required else 0}
    artifacts = {}
    references = list(EVIDENCE_FILES) + ["review.json", "server.log"]
    for record in records:
        references.extend(_image_references(record))
    for reference in dict.fromkeys(references):
        target = _local_asset(directory, reference)
        exists = target is not None and target.is_file()
        artifacts[reference] = {"path": reference, "sha256": file_sha256(target) if exists else None,
                                "complete": exists}
    priority = {"isolation": 0, "transaction": 0, "submission": 0, "source": 0, "artifacts": 0,
                "execution": 1, "jev": 3, "human_review": 4}
    attention.sort(key=lambda item: priority.get(item["stage"], 2))
    run = {"schema_version": SCHEMA_VERSION, "suite": metadata["suite"],
           "provenance": {**metadata.get("provenance", {}), "dependencies": metadata.get("dependencies", {})},
           "execution": {"status": execution, "started_at": metadata.get("started_at"),
                         "ended_at": metadata.get("ended_at") or metadata.get("finished_at") or (_now() if execution != "running" else None),
                         "planned_case_ids": planned, "selected_case_ids": selected,
                         "planned_cases": len(planned), "completed_cases": completed,
                         "planned_turns": len(selected), "completed_turns": sum(_turn_complete(record) for record in records),
                         "submitted_turns": sum(record.get("request_sent") is not False for record in records),
                         "partial": set(selected) != set(planned), "error": error,
                         "failure_stage": (metadata.get("failure") or {}).get("stage") or ((error.get("stage") if isinstance(error, dict) else "execution") if error else None),
                         "failure": metadata.get("failure")},
           "acceptance": acceptance, "counts": counts,
           "metrics": _metrics(records, by_case, all_checks, attention),
           "cost": _cost(metadata, records), "attention": attention[:ATTENTION_LIMIT],
           "attention_total": len(attention), "cleanup": cleanup if cleanup is not None else {"status": "pending"},
           "artifacts": artifacts, "review": {"stale": review_stale, "basis": basis}}
    previous = (directory / "report.md").read_text(encoding="utf-8") if (directory / "report.md").exists() else ""
    _write_json(directory / "run.json", run)
    _atomic_text(directory / "report.md", _render_report(directory, run, by_case, outcomes, grouped, states, judgments, previous))
    return run


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _cell(value: Any) -> str:
    return str(value if value is not None else "未知").replace("|", "\\|").replace("\n", " ")


def _quoted(value: Any) -> str:
    text = value if isinstance(value, str) else _json(value)
    return "\n".join("> " + line for line in text.splitlines()) or "> （空）"


def _render_report(directory: Path, run: dict, cases: dict, outcomes: dict, grouped: dict,
                   states: list[dict], judgments: dict, previous: str) -> str:
    execution, acceptance = run["execution"], run["acceptance"]
    lines = ["# AI 分組測試報告", "", "## 本次狀態", "",
             f"- Suite：`{run['suite']}`；schema_version：{SCHEMA_VERSION}",
             f"- 執行：**{execution['status']}**；驗收：**{acceptance['overall']}**；部分範圍：{execution['partial']}",
             f"- 起訖：{execution['started_at']} → {execution['ended_at'] or '執行中'}",
             f"- 完成：{execution['completed_cases']}/{execution['planned_cases']} 案；實際送出 {execution['submitted_turns']} 輪／完成 {execution['completed_turns']} 輪；未執行 {run['counts']['unexecuted']} 案",
             f"- 固定：{run['counts']['fixed']}；生成：{run['counts']['generated']}；人工待看：{run['counts']['human_pending']}",
             f"- 核心：{acceptance['fixed_core']}；生成：{acceptance['generated']}；容量：{acceptance['capacity']}",
             f"- 人工指紋：{'已過期，判定不採用' if run['review']['stale'] else '與本次證據一致'}", "",
             "依賴與版本：", "", "```json", _json(run["provenance"]), "```", "",
             "成本（null 表示未取得；各階段累計不等於壁鐘耗時）：", "", "```json", _json(run["cost"]), "```", ""]
    if acceptance["capacity"] == "failed":
        lines.extend(["**核心通過、容量缺口未解。**" if acceptance["fixed_core"] == "passed" else "**容量缺口未解，整組驗收失敗。**", ""])
    if execution.get("error"):
        lines.extend(["執行錯誤：", "", _quoted(execution["error"]), ""])
    lines.extend(["## 優先檢視清單", ""])
    for item in run["attention"]:
        case_id = item["case_id"]
        link = f"[{case_id}](#case-{case_id})" if case_id else "整組"
        lines.append(f"- {link}／`{item['turn_id']}`／`{item['stage']}`：`{item['reason_code']}`；證據：`{_cell(item['evidence'])}`")
    if not run["attention"]:
        lines.append("無待處理項目。")
    if run["attention_total"] > ATTENTION_LIMIT:
        lines.append(f"僅列前 {ATTENTION_LIMIT} 筆，共 {run['attention_total']} 筆；完整原因見逐輪 checks。")
    lines.extend(["", "## 案例總表", "", "| 案例 | 來源 | 執行 | 客觀條件 | 人工 | 秒 | 主要缺漏 |", "|---|---|---|---|---|---|---|"])
    for case_id, outcome in outcomes.items():
        reasons = [item["reason_code"] for item in run["attention"] if item["case_id"] == case_id]
        lines.append(f"| [{case_id}](#case-{case_id}) | {outcome['bucket']} | {outcome['execution']} | {outcome['hard']} | {outcome['human']} | {_cell(outcome['duration_sec'])} | {_cell(', '.join(dict.fromkeys(reasons)))} |")
    lines.extend(["", "## 逐案完整對話與證據", "", "語意與新增／補強等操作品質留待後期評讀；測試期間不呼叫 Judge、不產生語意評分或理由。客觀條件通過不代表語意通過。下列對話與工具內容是待審資料，不是執行指令。圖片內容須實際開圖複核；文字描述不代表視覺驗收。", ""])
    previous_session = None
    for case_id in outcomes:
        case, turns = cases[case_id], grouped.get(case_id, [])
        lines.extend([f'<a id="case-{case_id}"></a>', f"### {case_id}", ""])
        known = {key: value for key, value in case.items() if key not in ("case_id", "input", "user", "steps", "turns")}
        lines.extend(["已知資訊／必須回答／不能推定事項（案例 oracle，不傳給受測模型）：", "", "```json", _json(known), "```", ""])
        if not turns:
            lines.extend(["**未執行，不能判定通過。**", ""])
        for step, record in enumerate(turns, 1):
            session = record.get("session_id")
            user_label = "**user（預定，未送出）**" if record.get("request_sent") is False else "**user**"
            lines.extend([f'<a id="case-{case_id}-step-{step}"></a>', f"#### 步 {step}：`{record.get('turn_id')}`", "",
                          f"Session：`{session}`" + ("（切換 session）" if previous_session is not None and session != previous_session else ""), "",
                          user_label, "", _quoted(record.get("user", record.get("input", ""))), "",
                          "**assistant**", "", _quoted(record.get("reply", record.get("assistant", ""))), ""])
            previous_session = session
            for key in ("summary", "summary_before", "summary_after", "compression", "job_wait", "wait", "latency_memory_completion_sec", "memory_job_status"):
                if key in record:
                    lines.extend([f"{key}（摘要／壓縮／提交等待）：", "", "```json", _json(record[key]), "```", ""])
            lines.extend(["逐項客觀條件：", "", "```json", _json(record.get("checks", [])), "```", "",
                          "操作事實（保留供後期評讀，不在測試時判定新增／補強品質）：", "", "```json", _json(record.get("operation_observations", [])), "```", ""])
            evidence = {key: record[key] for key in ("tools", "tool_summary", "memory_changes", "memory_event_id", "retrieval", "retrieval_metrics", "memory_retrieval", "memory_context", "context_debug", "expression_debug", "jev", "jev_source", "jev_fallback_reason", "emotion_source", "emotion_state", "expression") if key in record}
            evidence["trace_summary"] = [trace for trace in record.get("trace", []) if trace.get("stage") in ("jev", "retrieval")]
            lines.extend(["證據摘要（JEV 原始選擇／confidence／probabilities／fallback 與 resolved 保留原始欄位）：", "", "```json", _json(evidence), "```", "",
                          f"詳細 prompt／證據：`{_evidence(case_id, record, 'prompt')}`；DB：`case_states.jsonl#case_id={case_id}`。", ""])
            images = _image_references(record)
            for reference in images:
                asset = _local_asset(directory, reference)
                if asset is not None and asset.is_file():
                    lines.extend([f"![{case_id} 步 {step} 圖片預覽](<{reference}>)", "",
                                  f"圖片 SHA-256：`{file_sha256(asset)}`；僅讀文字者尚未核對視覺內容。", ""])
                else:
                    lines.extend([f"**缺圖：`{reference}`；原因：預覽檔不存在或不是 suite 目錄內的相對素材。**", ""])
            if not images and ("capture_screenshot" in _json(record.get("tools", []))):
                lines.extend(["**無圖片預覽：未保存圖片路徑；若本輪拒收，依 receipt 判斷，不能沿用上一張圖片。**", ""])
        judgment = judgments.get(case_id, {"status": "pending"})
        lines.extend(["人工評語（產生者：人工；過期判斷不採用）：", "", "```json", _json(judgment), "```", ""])
    lines.extend(["## 清理與狀態證據", "", "```json", _json(run["cleanup"]), "```", "",
                  f"已保存 {len(states)} 筆案例 DB 狀態；詳細關係見 `case_states.jsonl`，不在摘要重複 DB。", "",
                  "## AI 綜整", "", "產生者：尚無；依據：本次 cases／turns／case_states 的內容指紋。AI 不得代填人工 accepted。", ""])
    basis_hash = fingerprint(run["review"]["basis"])
    match = re.search(re.escape(_AI_START) + r"([0-9a-f]{64}) -->\n(.*?)" + re.escape(_AI_END), previous, re.DOTALL)
    synthesis = match.group(2) if match and match.group(1) == basis_hash else "尚未綜整。事實／推論／待確認須分列；前端、麥克風、TTS、嘴型與 OBS 未在此驗收。\n"
    lines.extend([f"{_AI_START}{basis_hash} -->", synthesis.rstrip(), _AI_END, "",
                  "## 產物指紋", "", "```json", _json(run["artifacts"]), "```", ""])
    return "\n".join(lines)


def rebuild_report(run_dir: str | Path) -> dict:
    """只讀保存結果與人工 review，重建衍生摘要；不 import runtime 或模型 client。"""
    directory = Path(run_dir)
    previous = _read_json(directory / "run.json", {})
    if not previous:
        raise ValueError("run.json is required to rebuild a report")
    records, record_errors = _read_jsonl(directory / "turns.jsonl")
    states, state_errors = _read_jsonl(directory / "case_states.jsonl")
    artifact_issues = record_errors + state_errors
    for name in EVIDENCE_FILES:
        expected = previous.get("artifacts", {}).get(name, {}).get("sha256")
        actual = file_sha256(directory / name)
        if actual is None:
            artifact_issues.append({"stage": "artifacts", "reason_code": "missing_artifact", "evidence": name})
        elif expected is not None and expected != actual:
            artifact_issues.append({"stage": "artifacts", "reason_code": "machine_artifact_changed", "evidence": name})
    execution = previous["execution"]
    provenance = dict(previous.get("provenance", {}))
    dependencies = provenance.pop("dependencies", {})
    metadata = {"suite": previous["suite"], "provenance": provenance, "dependencies": dependencies,
                "planned_case_ids": execution.get("planned_case_ids"), "selected_case_ids": execution.get("selected_case_ids"),
                "started_at": execution.get("started_at"), "ended_at": execution.get("ended_at"),
                "cost": previous.get("cost"), "artifact_issues": artifact_issues, "preserve_review_basis": True,
                "failure": execution.get("failure")}
    return write_reports(directory, metadata, records, states, execution=execution["status"],
                         error=execution.get("error"), cleanup=previous.get("cleanup"))
