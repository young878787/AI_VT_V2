"""DB Manager 的原子操作驗證契約。"""

import re
from datetime import datetime
from uuid import UUID

from domain.memory.cards import DOMAINS, validate_title
from domain.memory.routing import MEMORY_TYPES, _number


ACTIONS = frozenset({"CREATE", "REINFORCE", "SUPERSEDE", "MERGE", "CONTRADICT", "ARCHIVE", "FORGET", "IGNORE",
                     "MERGE_CARDS"})
RETENTION_CLASSES = frozenset({"temporary", "normal", "important", "core"})
CARD_FIELDS = frozenset({"card_id", "domain", "subtopic"})

DECISION_FIELDS = frozenset({
    "action", "source_ids", "canonical_text", "memory_type", "subject_key",
    "target_memory_ids", "importance", "confidence", "retention_class", "valid_from", "valid_to",
    "expires_at", "reason", "forget_scope", "search_terms", *CARD_FIELDS, "card_ids",
})
_CODE = re.compile(r"[SMC][1-9][0-9]{0,2}")


def _uuid_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("卡片 ID 格式錯誤")
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError("卡片 ID 格式錯誤") from None


def _validate_card_fields(decision: dict, action: str) -> None:
    """卡片欄位只屬於 CREATE（擇一）與 MERGE_CARDS；DB 層允許 CREATE 不帶卡片（未歸卡）。"""
    fields = CARD_FIELDS & set(decision)
    if action != "CREATE" and fields:
        raise ValueError("只有 CREATE 可指定卡片")
    if action != "MERGE_CARDS" and "card_ids" in decision:
        raise ValueError("只有 MERGE_CARDS 可指定 cards")
    if action == "CREATE" and fields:
        if "card_id" in fields and fields & {"domain", "subtopic"}:
            raise ValueError("CREATE 只能選候選卡或提供新卡主題與名稱其中一種")
        if "card_id" in fields:
            _uuid_text(decision["card_id"])
        elif fields != {"domain", "subtopic"}:
            raise ValueError("新卡需要同時提供 domain 與 subtopic")
        else:
            if decision["domain"] not in DOMAINS:
                raise ValueError("主題不在白名單")
            validate_title(decision["subtopic"])
    if action == "MERGE_CARDS":
        cards = decision.get("card_ids")
        if not isinstance(cards, list) or len(cards) != 2:
            raise ValueError("MERGE_CARDS 需要兩張卡")
        if len({_uuid_text(value) for value in cards}) != 2:
            raise ValueError("MERGE_CARDS 的兩張卡不可相同")


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
        if action in {"CREATE", "MERGE_CARDS"} and targets:
            raise ValueError(f"{action} 不可指定 target")
        _validate_card_fields(decision, action)
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
            search_terms = decision.get("search_terms")
            if search_terms is not None and (
                not isinstance(search_terms, list) or not 1 <= len(search_terms) <= 12 or
                any(not isinstance(term, str) or not term.strip() or len(term) > 80 or
                    re.search(r"[\x00-\x1f\x7f]", term) for term in search_terms) or
                len({term.strip().lower() for term in search_terms}) != len(search_terms)
            ):
                raise ValueError("decision search_terms 無效")
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


def validate_sources(decisions: list[dict], authorized: set[UUID]) -> set[UUID]:
    used = set()
    for decision in decisions:
        ids = decision.get("source_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 16:
            raise ValueError("操作必須引用授權 user source_ids")
        try:
            values = [UUID(value) for value in ids if isinstance(value, str)]
        except ValueError:
            raise ValueError("source_ids 格式錯誤") from None
        if len(values) != len(ids) or len(set(values)) != len(values) or not set(values) <= authorized:
            raise ValueError("操作引用未授權或重複的 user source_ids")
        used.update(values)
    return used


def validate_current_source(used: set[UUID], current_source_id: UUID, text: str) -> None:
    from domain.memory.routing import instruction_policy
    earlier_request = instruction_policy(text) == "remember" and re.search(
        r"這件事|這個|那個|剛剛|剛才|之前|上面|那些|上次|以上|聊過|談過|\b(that|earlier|previous|said)\b", text, re.I)
    if current_source_id not in used and not earlier_request:
        raise ValueError("只處理當輪新事實，必須引用 current_source_id；較早來源僅供消解上下文")


def validate_batch(decisions: list[dict]) -> None:
    """整批不允許對同一目標進行不相容操作。"""
    if any(d["action"] == "FORGET" for d in decisions) and any(d["action"] != "FORGET" for d in decisions):
        raise ValueError("FORGET 不可與其他記憶 mutation 混用")
    targets = {}
    facts = set()
    merged_cards, used_cards = set(), set()
    for decision in decisions:
        if decision["action"] == "MERGE_CARDS":
            if set(decision["card_ids"]) & (merged_cards | used_cards):
                raise ValueError("同一張卡不可在同一批同時合併與使用")
            merged_cards.update(decision["card_ids"])
        elif decision.get("card_id"):
            if decision["card_id"] in merged_cards:
                raise ValueError("同一張卡不可在同一批同時合併與使用")
            used_cards.add(decision["card_id"])
        for target in decision["target_memory_ids"]:
            previous = targets.get(target)
            if previous and (previous != "REINFORCE" or decision["action"] != "REINFORCE"):
                raise ValueError("同一 target 有不相容操作，請替換原提案")
            targets[target] = decision["action"]
        if decision["action"] in {"CREATE", "SUPERSEDE", "CONTRADICT"}:
            fact = decision["canonical_text"].strip()
            if fact in facts:
                raise ValueError("同一批不可重複建立相同事實")
            facts.add(fact)


def normalize_proposal(payload: object, allowed_targets: set[UUID], authorized_sources: set[UUID], text: str,
                       current_source_id: UUID | None = None, allowed_cards: set[UUID] | None = None) -> dict:
    """agent 提案層；傳入 allowed_cards 時 CREATE 必須歸卡，且卡片只能是本次候選。"""
    from domain.memory.routing import instruction_policy, forget_scope
    if not isinstance(payload, dict) or "candidate_index" in payload or payload.get("action") == "IGNORE":
        raise ValueError("請提出一個有效操作；無需操作請用 finish")
    decision = dict(payload)
    decision.setdefault("target_memory_ids", [])
    if decision.get("action") in {"CREATE", "SUPERSEDE", "CONTRADICT"}:
        decision.setdefault("retention_class", "normal")
        if not decision.get("search_terms"):
            raise ValueError("新記憶缺少 search_terms")
    else:
        if set(decision) & {"canonical_text", "memory_type", "subject_key", "importance", "confidence",
                            "retention_class", "valid_from", "valid_to", "expires_at", "search_terms"}:
            raise ValueError("此操作不可改寫事實 metadata")
    policy = instruction_policy(text)
    if policy == "no_store":
        raise ValueError("禁止保存")
    if decision.get("action") == "FORGET":
        decision["forget_scope"] = forget_scope(text)
    elif "forget_scope" in decision:
        raise ValueError("非遺忘操作不可指定 forget_scope")
    validate_decisions({"decisions": [decision]}, allowed_targets, policy == "forget")
    if allowed_cards is not None:
        if decision["action"] == "CREATE" and not CARD_FIELDS & set(decision):
            raise ValueError("CREATE 必須指定 card（候選卡代號）或 domain 加 subtopic")
        chosen = ([decision["card_id"]] if decision.get("card_id") else []) + list(decision.get("card_ids", []))
        if any(UUID(value) not in allowed_cards for value in chosen):
            raise ValueError("卡片代號不在本次候選卡內")
    used = validate_sources([decision], authorized_sources)
    if current_source_id is not None:
        validate_current_source(used, current_source_id, text)
    decision["source_ids"] = [str(UUID(value)) for value in decision["source_ids"]]
    decision["target_memory_ids"] = [str(UUID(value)) for value in decision["target_memory_ids"]]
    if decision.get("card_id"):
        decision["card_id"] = _uuid_text(decision["card_id"])
    if "card_ids" in decision:
        decision["card_ids"] = [_uuid_text(value) for value in decision["card_ids"]]
    if "subtopic" in decision:
        decision["subtopic"] = validate_title(decision["subtopic"])
    return decision


def translate_codes(operation: object, codes: dict[str, UUID]) -> dict:
    """agent 只填短代號（S／M／C）；換回 UUID，UUID 或不在本工作的代號一律拒絕。"""
    if not isinstance(operation, dict):
        raise ValueError("operation 必須是 object")
    result = dict(operation)

    def resolve(value, prefix, field):
        if not isinstance(value, str) or not _CODE.fullmatch(value) or value[0] != prefix:
            raise ValueError(f"{field} 只接受本工作的 {prefix} 代號（例如 {prefix}1），不接受 UUID")
        if value not in codes:
            raise ValueError(f"{field} 的代號 {value} 不存在或未授權")
        return str(codes[value])

    for field, prefix in (("source_ids", "S"), ("target_memory_ids", "M")):
        if field in result:
            if not isinstance(result[field], list):
                raise ValueError(f"{field} 必須是陣列")
            result[field] = [resolve(value, prefix, field) for value in result[field]]
    if "card" in result:
        result["card_id"] = resolve(result.pop("card"), "C", "card")
    if "cards" in result:
        if not isinstance(result["cards"], list):
            raise ValueError("cards 必須是陣列")
        result["card_ids"] = [resolve(value, "C", "cards") for value in result.pop("cards")]
    return result


def translate_read_codes(args: dict, codes: dict[str, UUID]) -> tuple[set[UUID], set[UUID]]:
    """read_context 的 memory_ids／source_ids 也只接受代號。"""
    resolved = translate_codes({"target_memory_ids": args.get("memory_ids", []),
                                "source_ids": args.get("source_ids", [])}, codes)
    return ({UUID(value) for value in resolved["target_memory_ids"]},
            {UUID(value) for value in resolved["source_ids"]})


def agent_tools(forget: bool) -> list[dict]:
    def tool(name, description, properties, required):
        return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": properties, "required": required}}}
    text = {"type": "string", "minLength": 1, "maxLength": 1000}

    def codes(prefix, maximum):
        return {"type": "array", "maxItems": maximum,
                "items": {"type": "string", "pattern": f"^{prefix}[1-9][0-9]{{0,2}}$"}}
    ids = codes("M", 8)
    operation = {
        "action": {"type": "string", "enum": sorted(ACTIONS - {"IGNORE", *(set() if forget else {"FORGET"})})},
        "source_ids": {**codes("S", 16), "minItems": 1}, "target_memory_ids": ids, "reason": text,
        "card": {"type": "string", "pattern": "^C[1-9][0-9]{0,2}$"},
        "domain": {"type": "string", "enum": sorted(DOMAINS)},
        "subtopic": {"type": "string", "minLength": 2, "maxLength": 16},
        "cards": {**codes("C", 2), "minItems": 2},
        "canonical_text": {"type": "string", "minLength": 1, "maxLength": 2000},
        "memory_type": {"type": "string", "enum": sorted(MEMORY_TYPES - {"none"})},
        "subject_key": {"type": "string", "maxLength": 160, "pattern": "^[a-z0-9][a-z0-9_.-]*$"},
        "search_terms": {"type": "array", "minItems": 1, "maxItems": 12, "uniqueItems": True,
                         "items": {"type": "string", "minLength": 1, "maxLength": 80}},
        "importance": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "retention_class": {"type": "string", "enum": sorted(RETENTION_CLASSES)},
        **{key: {"type": "string", "format": "date-time"} for key in ("valid_from", "valid_to", "expires_at")},
    }
    return [
        tool("search_memories", "Find a small batch of related memories using a focused query.", {"query": text}, ["query"]),
        tool("read_context", "Read authorized sources or supplied memories and their evidence/versions.",
             {"memory_ids": codes("M", 3), "source_ids": codes("S", 3)}, []),
        tool("propose_operation", "Propose ONE operation; no DB write. Use short codes only (S1, M1, C1). CREATE/SUPERSEDE/CONTRADICT need canonical_text, memory_type, importance, confidence and search_terms; CREATE also needs card (candidate card code) or domain plus subtopic. MERGE_CARDS needs cards [kept, merged] of the same domain. Other actions need targets only. Cite user sources. Replace an accepted proposal using its index if needed.",
             {"operation": {"type": "object", "additionalProperties": False, "properties": operation,
                            "required": ["action", "source_ids", "reason"]},
              "replace_index": {"type": "integer", "minimum": 0, "maximum": 11}}, ["operation"]),
        tool("finish", "End the job without repeating proposals. needs_context discards pending proposals; explain missing evidence.",
             {"outcome": {"type": "string", "enum": ["complete", "ignore", "needs_context"]}, "reason": text}, ["outcome", "reason"]),
    ]
