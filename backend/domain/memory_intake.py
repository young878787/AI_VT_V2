"""接收端業務工具與來源驗證；沒有正式記憶 mutation 權限。"""
from uuid import UUID

from domain.memory_decisions import validate_decisions
from domain.memory_routing import instruction_policy, forget_scope


def tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": properties, "required": required}}}


TEXT = {"type": "string", "minLength": 1, "maxLength": 1000}
SOURCE_IDS = {"type": "array", "minItems": 1, "maxItems": 16,
              "items": {"type": "string", "format": "uuid"}}
FACT = {"type": "object", "additionalProperties": False, "properties": {
    "canonical_text": {"type": "string", "minLength": 1, "maxLength": 2000},
    "source_ids": SOURCE_IDS,
    "memory_type": {"type": "string", "enum": ["profile", "preference", "project", "event"]},
    "subject_key": {"type": "string", "maxLength": 160},
    "intent": {"type": "string", "enum": ["fact", "correction", "forget"]},
    "importance": {"type": "number", "minimum": 0, "maximum": 1},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "retention_class": {"type": "string", "enum": ["temporary", "normal", "important", "core"]},
    "valid_from": {"type": "string", "format": "date-time"},
    "valid_to": {"type": "string", "format": "date-time"},
    "expires_at": {"type": "string", "format": "date-time"},
    "reason": TEXT,
}, "required": ["canonical_text", "source_ids", "memory_type", "intent", "importance",
                "confidence", "retention_class", "reason"]}
INTAKE_TOOLS = [
    tool("accept_candidates", "Accept reviewed atomic facts or a clarified management request.",
         {"candidates": {"type": "array", "minItems": 1, "maxItems": 12, "items": FACT}}, ["candidates"]),
    tool("hold_for_context", "Wait for related new user evidence; do not create facts.",
         {"missing_context": TEXT}, ["missing_context"]),
    tool("dismiss_input", "Close this input without changing existing memories.", {"reason": TEXT}, ["reason"]),
]


def validate_intake(name: str, payload: object, sources: list[dict], text: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("接收工具參數必須是 object")
    if name in {"hold_for_context", "dismiss_input"}:
        key = "missing_context" if name == "hold_for_context" else "reason"
        if set(payload) != {key} or not isinstance(payload[key], str) or not 0 < len(payload[key].strip()) <= 1000:
            raise ValueError("接收結案理由無效")
        return {"route": "needs_context" if name == "hold_for_context" else "none", **payload}
    if name != "accept_candidates" or set(payload) != {"candidates"}:
        raise ValueError("接收端無此工具權限")
    candidates = payload["candidates"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 12:
        raise ValueError("接收候選數量無效")
    allowed = {str(source["id"]) for source in sources if source["speaker"] == "user"}
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) - (set(FACT["properties"]) | {"forget_scope"}) or set(FACT["required"]) - set(candidate):
            raise ValueError("候選欄位無效")
        ids = candidate["source_ids"]
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 16 or
                any(not isinstance(value, str) or value not in allowed for value in ids) or len(set(ids)) != len(ids)):
            raise ValueError("候選必須引用已授權的 user 來源")
        for value in ids:
            UUID(value)
        intent = candidate["intent"]
        if intent not in {"fact", "correction", "forget"}:
            raise ValueError("候選意圖無效")
        if intent == "forget" and instruction_policy(text) != "forget":
            raise ValueError("接收端不可擴張遺忘授權")
        if intent == "forget":
            scope = forget_scope(text)
            if candidate.get("forget_scope", scope) != scope:
                raise ValueError("遺忘範圍不可超出 user request")
            candidate["forget_scope"] = scope
        elif "forget_scope" in candidate:
            raise ValueError("非遺忘候選不可含遺忘範圍")
        if instruction_policy(text) == "no_store":
            raise ValueError("禁止保存")
        decision = {key: value for key, value in candidate.items() if key not in {"source_ids", "intent", "forget_scope"}}
        validate_decisions({"decisions": [{**decision, "action": "CREATE", "target_memory_ids": []}]}, set())
    return {"route": "candidate", "candidates": candidates}
