"""將 PostgreSQL 長期記憶投影成既有 Chat prompt 可使用的有界資料。"""

import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from dataclasses import dataclass, field
from uuid import UUID

from domain.jev_questions import CONFIDENCE_THRESHOLD, build_recall_context, build_recall_questions, question_hash
from domain.memory.cards import DOMAINS, MOOD_LABELS, card_header
from infrastructure.memory.embedding_client import MemoryEmbeddingClient
from infrastructure.memory.repository import MemoryRepository
from infrastructure.memory.repository import (CHAT_CARD_CANDIDATE_LIMIT, CHAT_CARD_INJECT_LIMIT, CHAT_CARD_MIN_SCORE,
                                              CHAT_RETRIEVAL_MIN_SIMILARITY)
from core.prompt_logger import trace


_PROFILE_LIST_FIELDS = {"core_traits", "dislikes", "recent_interests", "custom_notes"}
MEMORY_CHAR_BUDGET = 1500
RECALL_CHOICES = ("key", "assist", "none")
UNCARDED_KEY = "uncarded"


@dataclass
class RecallJudge:
    """聊天端交給召回的 JEV 判斷；previous／selected 是本 session 上一輪與本輪採用的卡。"""

    ask: Callable[[dict, dict], Awaitable[dict | None]]
    previous_card_ids: tuple = ()
    selected_card_ids: list = field(default_factory=list)


_CONTEXT_REFERENCE = re.compile(r"那個|那部分|那件事|這個|這部分|這件事|它|剛才|剛剛|我的口味|我的喜好|今晚的安排|\b(it|that|my taste)\b", re.I)
_RECALL_WRAPPER = re.compile(
    r"^(?:我想)?(?:確認|問)(?:一下)?[，,：:\s]*|"
    r"^(?:我有點)?忘了[，,：:\s]*|"
    r"^(?:關於)[，,：:\s]*|"
    r"你(?:還)?記得|"
    r"(?:可以|能)?告訴我|"
    r"(?:嗎|呢)[？?]?$",
    re.I,
)


@dataclass(frozen=True)
class RetrievalQuery:
    """同一輪召回的兩種查詢表示；詞面保留原文，向量使用正規化語意。"""

    lexical_text: str
    semantic_text: str


def _format_memory_projection(row: dict, mode: str) -> str:
    """以完整 fact 為優先，metadata 只保留 Chat 判斷現況所需欄位。"""
    status = str(row.get("status") or "unknown")
    metadata = [f"status={status}"]
    if row.get("has_conflict"):
        metadata.append("conflict=true")
    if row.get("pending_change"):
        metadata.append("pending_change=true")
    if mode in {"future", "chat"} or status != "active":
        for key in ("valid_from", "valid_to"):
            value = row.get(key)
            if value is not None:
                # 時區與日內先後仍保留；版本狀態不依微秒或列出順序推定。
                if isinstance(value, datetime):
                    value = value.isoformat(timespec="seconds")
                metadata.append(f"{key}={value}")
    fact = str(row.get("canonical_text") or "")
    return f"- {fact}（" + "; ".join(metadata) + "）"


def _normalize_semantic_query(text: str) -> str:
    """移除不承載檢索語意的聊天框架，不擴張偏好、頻率或時間語意。"""
    value = text.strip()[:4000]
    value = value.replace("我的", "使用者的")
    value = re.sub(r"(?<![\w\u3400-\u9fff])我(?=比較|平常|通常|一直|目前|現在|以前|曾經)", "使用者", value)
    previous = None
    while previous != value:
        previous = value
        value = _RECALL_WRAPPER.sub("", value).strip(" ，,：:？?。")
    value = re.sub(r"\s+", " ", value)
    return value or text.strip()[:4000]


def build_retrieval_query(user_text: str, recent_dialogue: list[dict] | None = None, summary: str = "") -> RetrievalQuery:
    """建立分離的詞面與向量查詢；只有指代型訊息才加入有界 user 上下文。"""
    lexical = user_text[:4000]
    semantic = _normalize_semantic_query(user_text)
    if not _CONTEXT_REFERENCE.search(user_text):
        return RetrievalQuery(lexical, semantic)
    user_context = [m["content"][:400] for m in (recent_dialogue or [])[-16:]
                    if m.get("role") == "user" and isinstance(m.get("content"), str)][-2:]
    context = "\n".join(user_context) if user_context else summary[:800]
    if context:
        lexical = user_text[:3000] + "\n" + context
        semantic_context = "\n".join(_normalize_semantic_query(item) for item in user_context)
        if not semantic_context and summary:
            semantic_context = _normalize_semantic_query(summary[:800])
        if semantic_context:
            semantic = semantic[:3000] + "\n相關使用者情境：\n" + semantic_context
    return RetrievalQuery(lexical, semantic)


def _group_cards(rows: list[dict]) -> list[dict]:
    """要點列依卡片分組；未歸卡要點合成一張「其他」卡，排在有排名的卡之後。"""
    cards: dict[str, dict] = {}
    for row in rows:
        key = str(row["card_id"]) if row.get("card_id") else UNCARDED_KEY
        card = cards.get(key)
        if card is None:
            card = cards[key] = {
                "key": key, "card_id": row.get("card_id"), "domain": row.get("card_domain") or "other",
                "title": row.get("card_title"), "summary": row.get("card_summary"),
                "summary_stale": bool(row.get("card_summary_stale", True)), "mood": row.get("card_mood"),
                "score": row.get("card_score"), "routes": list(row.get("card_routes") or []),
                "rank": row.get("card_rank"), "items": [],
            }
        card["items"].append(row)
        if key == UNCARDED_KEY:
            similarity = row.get("similarity")
            if similarity is not None and (card["score"] is None or similarity > card["score"]):
                card["score"] = similarity
            if "item" not in card["routes"]:
                card["routes"].append("item")
    return sorted(cards.values(), key=lambda card: (card["rank"] is None, card["rank"] or 0))


def _card_view(card: dict, label: str) -> dict:
    """JEV 看到的卡片：主題、副主題、概要與心情；概要過期或未建立時改放前 2 筆有效要點。"""
    view = {"card": label, "domain": DOMAINS.get(card["domain"], "其他"), "title": card["title"] or "未分類"}
    if card["summary"] and not card["summary_stale"]:
        view["summary"] = card["summary"]
    else:
        current = [row["canonical_text"][:300] for row in card["items"] if row["status"] in {"active", "conflict"}]
        view["points"] = (current or [row["canonical_text"][:300] for row in card["items"]])[:2]
    if card["mood"]:
        view["mood"] = MOOD_LABELS.get(card["mood"], card["mood"])
    return view


def _judge_answer(answer: object) -> tuple[str, str | None]:
    """回傳 (選擇, 不採用原因)；只有 key／assist 且信心達門檻才注入。"""
    if not isinstance(answer, dict) or answer.get("choice") not in RECALL_CHOICES:
        return "none", "invalid_choice"
    confidence = answer.get("confidence")
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not math.isfinite(confidence)):
        return "none", "invalid_confidence"
    if answer["choice"] != "none" and confidence < CONFIDENCE_THRESHOLD:
        return "none", "low_confidence"
    return answer["choice"], None


def _project_profile(cards: list[dict]) -> tuple[dict, list[dict], list[str], set[str]]:
    """profile 投影路徑不變，來源改為被選中卡片的要點；communication_style 只保留最後一筆。

    被覆寫的 profile 要點也算已消耗，不再落到記憶行。
    """
    profile: dict = {}
    projections: list[dict] = []
    keys: list[str] = []
    consumed: set[str] = set()
    now = datetime.now(timezone.utc)
    for card in cards[:CHAT_CARD_INJECT_LIMIT]:
        for row in card["items"]:
            if (row["memory_type"] != "profile" or row["status"] != "active" or row.get("has_conflict")
                    or row.get("pending_change")):
                continue
            if ((row.get("valid_from") is not None and row["valid_from"] > now)
                    or (row.get("valid_to") is not None and row["valid_to"] <= now)):
                continue  # 歷史或未來 profile 只能帶時間投影，不能成為目前個人資料。
            key, text = row.get("subject_key"), row.get("canonical_text")
            if not isinstance(key, str) or not key.startswith("profile.") or not isinstance(text, str):
                continue
            name = key.removeprefix("profile.")
            if name in _PROFILE_LIST_FIELDS:
                profile.setdefault(name, []).append(text[:300])
            elif name == "communication_style":
                profile[name] = text[:300]
                projections = [item for item in projections if item.get("field") != name]
            else:
                continue
            consumed.add(str(row["id"]))
            projections.append({"id": str(row["id"]), "destination": "profile", "field": name,
                                "text": text[:300], "card_id": card["key"]})
            if card["key"] not in keys:
                keys.append(card["key"])
    return profile, projections, keys, consumed


class MemoryRetriever:
    def __init__(self, repository: MemoryRepository, embedding: MemoryEmbeddingClient) -> None:
        self.repository = repository
        self.embedding = embedding

    async def retrieve(self, user_text: str, event_id: UUID | None = None,
                       recent_dialogue: list[dict] | None = None, summary: str = "",
                       judge: RecallJudge | None = None) -> tuple[dict, str]:
        """粗篩候選卡 → JEV 判斷 key／assist／none → 依卡片分組投影；失敗一律回傳空投影。"""
        started = time.monotonic()
        mode = "chat"
        query = build_retrieval_query(user_text, recent_dialogue, summary)
        errors: list[dict] = []
        timing: dict = {"embedding_ms": None, "locate_ms": None, "jev_ms": None, "project_ms": None}
        trace_base = {"query": query.lexical_text, "semantic_query": query.semantic_text, "user_query": user_text,
                      "mode": mode, "limit": CHAT_CARD_CANDIDATE_LIMIT, "inject_limit": CHAT_CARD_INJECT_LIMIT,
                      "min_score": CHAT_CARD_MIN_SCORE, "fallback_min_similarity": CHAT_RETRIEVAL_MIN_SIMILARITY,
                      "confidence_threshold": CONFIDENCE_THRESHOLD, "memory_char_budget": MEMORY_CHAR_BUDGET,
                      "profile_char_limit": 300}
        if judge is not None:
            judge.selected_card_ids = []

        def ms(since: float) -> float:
            return round((time.monotonic() - since) * 1000, 1)

        def finish_empty(stage: str | None = None, error: Exception | None = None,
                         embedding_available: bool = False) -> tuple[dict, str]:
            if error is not None:
                print(f"[Memory] retrieval fallback: {type(error).__name__}")
                errors.append({"stage": stage, "error": type(error).__name__})
            timing.update(total_ms=ms(started), candidate_card_count=0, injected_card_count=0)
            trace("retrieval", {**trace_base, "embedding_available": embedding_available, "fallback": False,
                "candidates": [], "candidate_cards": [], "recall_jev": None, "projections": [],
                "timing": timing, "errors": errors, "duration_sec": round(time.monotonic() - started, 4)}, event_id)
            return {}, ""

        step = time.monotonic()
        try:
            query_embedding = await self.embedding.embed(
                query.semantic_text, query=True,
                purpose="retrieval_query" if event_id is not None else None,
                event_id=event_id, stage="chat_retrieval",
            )
        except Exception as exc:
            timing["embedding_ms"] = ms(step)
            return finish_empty("embedding", exc)
        timing["embedding_ms"] = ms(step)
        step = time.monotonic()
        try:
            rows = await self.repository.locate_cards(
                query.lexical_text, query_embedding, limit=CHAT_CARD_CANDIDATE_LIMIT, min_score=CHAT_CARD_MIN_SCORE,
                previous_card_ids=tuple(judge.previous_card_ids) if judge is not None else ())
        except Exception as exc:
            timing["locate_ms"] = ms(step)
            return finish_empty("repository", exc, embedding_available=True)
        timing["locate_ms"] = ms(step)
        cards = _group_cards(rows)
        if not cards:
            # 沒有候選卡時不呼叫召回判斷，行為與沒有記憶相同。
            return finish_empty(embedding_available=True)

        labels = {card["key"]: f"card_{index + 1}" for index, card in enumerate(cards)}
        recall_jev = None
        choices: dict[str, tuple[str, str | None]] = {}
        if judge is not None:
            questions = build_recall_questions(len(cards))
            context = build_recall_context(user_text, recent_dialogue or [],
                                           [_card_view(card, labels[card["key"]]) for card in cards])
            step = time.monotonic()
            try:
                answers = await judge.ask(context, questions)
            except Exception as exc:
                print(f"[JEV recall] 呼叫失敗，使用 fallback: {type(exc).__name__}")
                answers = None
            timing["jev_ms"] = ms(step)
            recall_jev = {"question_hash": question_hash(questions), "question_keys": list(questions),
                          "state": context, "answers": answers, "duration_sec": round(timing["jev_ms"] / 1000, 4),
                          "error": None if isinstance(answers, dict) else "jev_call_failed"}
            if isinstance(answers, dict):
                choices = {card["key"]: _judge_answer(answers.get(labels[card["key"]])) for card in cards}
        step = time.monotonic()
        fallback = not choices
        if fallback:
            # JEV 失敗或沒有判斷函式：只採粗篩分數達門檻或名稱命中的卡，依分數排序。
            ordered = sorted(
                (card for card in cards if (card["score"] or 0) >= CHAT_RETRIEVAL_MIN_SIMILARITY
                 or "name" in card["routes"]),
                key=lambda card: -(card["score"] or 0))
            selection = {card["key"]: "fallback_selected" for card in ordered}
        else:
            ordered = ([card for card in cards if choices[card["key"]][0] == "key"]
                       + [card for card in cards if choices[card["key"]][0] == "assist"])
            selection = {card["key"]: choices[card["key"]][0] for card in cards}
        profile, projections, injected_keys, profiled = _project_profile(ordered)
        lines: list[str] = []
        used = 0
        status: dict[str, str] = {key: "profile" for key in injected_keys}
        for card in ordered:
            items = [row for row in card["items"] if str(row["id"]) not in profiled]
            if not items:
                status.setdefault(card["key"], "profile_only")
                continue
            if len(injected_keys) >= CHAT_CARD_INJECT_LIMIT and card["key"] not in injected_keys:
                status[card["key"]] = "inject_limit"
                continue
            header = card_header(card["domain"], card["title"], card["mood"])
            separator = 1 if lines else 0
            cost = separator + len(header)
            block_items = []
            for row in items:
                text = _format_memory_projection(row, mode)
                if used + cost + 1 + len(text) > MEMORY_CHAR_BUDGET:
                    break
                block_items.append((row, text))
                cost += 1 + len(text)
            if not block_items:
                status[card["key"]] = "memory_budget_exhausted"
                continue
            block = [header, *(text for _, text in block_items)]
            hidden = len(items) - len(block_items)
            if hidden:
                # 卡片超出預算時不改寫要點；有新鮮概要就補上，並標示未列出的筆數。
                note = f"（此卡另有 {hidden} 筆未列出）"
                if card["summary"] and not card["summary_stale"]:
                    note = f"概要：{card['summary']}" + note
                if used + cost + 1 + len(note) > MEMORY_CHAR_BUDGET:
                    note = f"（此卡另有 {hidden} 筆未列出）"
                if used + cost + 1 + len(note) <= MEMORY_CHAR_BUDGET:
                    block.append(note)
                    cost += 1 + len(note)
            cursor = used + separator + len(header) + 1
            for row, text in block_items:
                projections.append({"id": str(row["id"]), "destination": "memory", "text": text,
                                    "offset": cursor, "card_id": card["key"]})
                cursor += len(text) + 1
            lines.extend(block)
            used += cost
            status[card["key"]] = "selected" if not hidden else "partial"
            if card["key"] not in injected_keys:
                injected_keys.append(card["key"])
        timing["project_ms"] = ms(step)
        memory = "\n".join(lines)
        if judge is not None:
            judge.selected_card_ids = [card["card_id"] for card in cards
                                       if card["key"] in injected_keys and card["card_id"] is not None]
        timing.update(total_ms=ms(started), candidate_card_count=len(cards), injected_card_count=len(injected_keys))
        projected = {item["id"] for item in projections}
        trace("retrieval", {**trace_base, "embedding_available": query_embedding is not None, "fallback": fallback,
            "candidates": [{"rank": index + 1,
                **{key: row.get(key) for key in ("id", "group_id", "canonical_text", "memory_type", "subject_key",
                    "similarity", "status", "valid_from", "valid_to", "expires_at", "has_conflict",
                    "pending_change", "card_score", "card_rank")},
                "card_id": str(row["card_id"]) if row.get("card_id") else UNCARDED_KEY,
                "projection": "selected" if str(row["id"]) in projected else "not_projected"}
                for index, row in enumerate(rows)],
            "candidate_cards": [{"rank": index + 1, "label": labels[card["key"]], "card_id": card["key"],
                "domain": card["domain"], "title": card["title"], "score": card["score"], "routes": card["routes"],
                "summary_stale": card["summary_stale"], "mood": card["mood"], "item_count": len(card["items"]),
                "judge": None if fallback else choices[card["key"]][0],
                "judge_reason": None if fallback else choices[card["key"]][1],
                "selection": selection.get(card["key"], "fallback_dropped"),
                "injection": status.get(card["key"], "not_selected")}
                for index, card in enumerate(cards)],
            "recall_jev": recall_jev, "projections": projections, "timing": timing,
            "errors": errors, "duration_sec": round(time.monotonic() - started, 4)}, event_id)
        if event_id is not None:
            print("[Memory] recall: " + json.dumps({
                "event_id": str(event_id), "cards": len(cards), "injected": len(injected_keys),
                "fallback": fallback, "timing": timing}, separators=(",", ":")))
        return profile, memory
