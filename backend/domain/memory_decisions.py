"""DB Manager 的原子操作驗證契約。"""

import re
from datetime import datetime
from uuid import UUID

from domain.memory_routing import MEMORY_TYPES, _number


ACTIONS = frozenset({"CREATE", "REINFORCE", "SUPERSEDE", "MERGE", "CONTRADICT", "ARCHIVE", "FORGET", "IGNORE"})
RETENTION_CLASSES = frozenset({"temporary", "normal", "important", "core"})

DECISION_FIELDS = frozenset({
    "action", "source_ids", "candidate_index", "canonical_text", "memory_type", "subject_key",
    "target_memory_ids", "importance", "confidence", "retention_class", "valid_from", "valid_to",
    "expires_at", "reason", "forget_scope",
})


def validate_decisions(payload: object, allowed_target_ids: set[UUID], explicit_forget: bool = False) -> list[dict]:
    """在 DB mutation 前驗證基本輸出；target status 與 owner 仍由 DB Manager 重查。"""
    if not isinstance(payload, dict) or set(payload) != {"decisions"}:
        raise ValueError("Memory LLM 輸出必須只包含 decisions")
    decisions = payload["decisions"]
    if not isinstance(decisions, list) or len(decisions) > 12:
        raise ValueError("decisions 數量無效")
    result = []
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) - DECISION_FIELDS:
            raise ValueError("decision 欄位無效")
        action = decision.get("action")
        targets = decision.get("target_memory_ids")
        reason = decision.get("reason")
        if action not in ACTIONS or not isinstance(targets, list) or len(targets) > 8:
            raise ValueError("decision action 或 targets 無效")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("decision reason 無效")
        try:
            valid_targets = all(isinstance(target, str) and UUID(target) in allowed_target_ids for target in targets)
        except ValueError:
            valid_targets = False
        if not valid_targets:
            raise ValueError("decision 引用了未提供的 target")
        if len(set(targets)) != len(targets):
            raise ValueError("decision target 不可重複")
        if action in {"REINFORCE", "SUPERSEDE", "CONTRADICT"} and len(targets) != 1:
            raise ValueError("decision 必須有一個 target")
        if action == "MERGE" and len(targets) < 2:
            raise ValueError("MERGE 至少需要兩個 target")
        if action in {"ARCHIVE", "FORGET"} and not targets:
            raise ValueError("decision 缺少 target")
        if action == "CREATE" and targets:
            raise ValueError("CREATE 不可指定 target")
        if "forget_scope" in decision and decision["forget_scope"] not in {"fact", "version"}:
            raise ValueError("遺忘範圍無效")
        if action == "FORGET" and decision.get("forget_scope") == "version" and len(targets) != 1:
            raise ValueError("單一版本遺忘只允許一個 target")
        if action == "FORGET" and not explicit_forget:
            raise ValueError("FORGET 必須有明確 user request")
        if action in {"CREATE", "SUPERSEDE", "CONTRADICT"}:
            canonical = decision.get("canonical_text")
            if not isinstance(canonical, str) or not canonical.strip() or len(canonical) > 2000:
                raise ValueError("decision canonical_text 無效")
        memory_type = decision.get("memory_type")
        if memory_type is not None and memory_type not in MEMORY_TYPES - {"none"}:
            raise ValueError("decision memory_type 無效")
        retention = decision.get("retention_class")
        if retention is not None and retention not in RETENTION_CLASSES:
            raise ValueError("decision retention_class 無效")
        if action in {"CREATE", "SUPERSEDE", "CONTRADICT"} and (
            memory_type is None or retention is None or
            "importance" not in decision or "confidence" not in decision
        ):
            raise ValueError("新記憶缺少必要 metadata")
        if retention == "temporary" and not decision.get("expires_at"):
            raise ValueError("temporary 記憶需要 expires_at")
        subject_key = decision.get("subject_key")
        if subject_key is not None and (
            not isinstance(subject_key, str) or len(subject_key) > 160 or
            not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", subject_key)
        ):
            raise ValueError("subject_key 無效")
        for key in ("importance", "confidence"):
            if key in decision:
                _number(decision[key])
        for key in ("valid_from", "valid_to", "expires_at"):
            if key in decision:
                value = decision[key]
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except (AttributeError, ValueError):
                    raise ValueError(f"{key} 日期無效") from None
                if parsed.tzinfo is None:
                    raise ValueError(f"{key} 必須含時區")
        if decision.get("valid_from") and decision.get("valid_to"):
            if datetime.fromisoformat(decision["valid_from"].replace("Z", "+00:00")) >= datetime.fromisoformat(decision["valid_to"].replace("Z", "+00:00")):
                raise ValueError("記憶有效區間無效")
        result.append(decision)
    return result
