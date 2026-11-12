"""聊天卡的確定性規則：主題白名單、名稱正規化、概要檢查與心情彙總。"""

import hashlib
import json
import re
import unicodedata
from datetime import datetime

from domain.emotion_state import MOOD_NAMES, PRIMARY_EMOTION_FIELDS, rank_mood


DOMAINS = {
    "self": "使用者本人",
    "preference": "喜好與興趣",
    "daily": "日常與作息",
    "relation": "人際關係",
    "project": "專案與創作",
    "event": "事件與計畫",
    "bond": "與露西亞的互動",
    "other": "其他",
}
CALM_MOOD = "calm"
CARD_MOODS = (CALM_MOOD, *PRIMARY_EMOTION_FIELDS)
MOOD_LABELS = {CALM_MOOD: "平穩", **MOOD_NAMES}
TITLE_MIN_CHARS = 2
TITLE_MAX_CHARS = 16
SUMMARY_MAX_CHARS = 200
SPLIT_SUGGESTION_COUNT = 12

# 寫進 Memory agent prompt 的四條歸類規則（計劃書 4.3）。
CLASSIFICATION_RULES = (
    "Every CREATE belongs to a card; a card groups facts about one person, object, project or event. A CREATE either "
    "names a candidate card code (card) or gives domain plus a 2-16 character subtopic naming the person, object or "
    "event, not the fact itself.\n"
    "1. Subject comes before content: a friend's preference belongs to relation/<friend>, not to the user's "
    "preference card.\n"
    "2. Project comes before event: a demo or deadline of a named project belongs to project/<project>.\n"
    "3. A candidate card covering the same thing is reused through its code or its exact subtopic text; "
    "a different time or preference direction is not a new subtopic.\n"
    "4. A sentence spanning several topics becomes several atomic facts, each in its own card.\n"
    "Domains: " + ", ".join(f"{key}={name}" for key, name in DOMAINS.items()) + "."
)

_NAME_PATTERN = re.compile(r"[小阿][一-鿿]|[一-鿿]老師")
_QUOTED_PATTERN = re.compile(r"《([^》]{1,40})》|「([^」]{1,40})」|『([^』]{1,40})』")
_TOKEN_PATTERN = re.compile(r"\d+(?:\.\d+)?|[a-z][a-z0-9.+#_-]*", re.I)


def normalize_key(text: str) -> str:
    """全形轉半形、英文轉小寫，移除空白、標點與符號；用於名稱比對與卡內防重複。"""
    value = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in value
                   if not char.isspace() and unicodedata.category(char)[0] not in {"P", "S", "C", "Z"})


def validate_title(title: object) -> str:
    if not isinstance(title, str):
        raise ValueError("副主題名稱必須是文字")
    value = unicodedata.normalize("NFKC", title).strip()
    if not TITLE_MIN_CHARS <= len(value) <= TITLE_MAX_CHARS:
        raise ValueError(f"副主題名稱需為 {TITLE_MIN_CHARS}–{TITLE_MAX_CHARS} 字")
    if re.search(r"[\x00-\x1f\x7f\n]", value) or not normalize_key(value):
        raise ValueError("副主題名稱無效")
    return value


def validate_domain(domain: object) -> str:
    if domain not in DOMAINS:
        raise ValueError("主題不在白名單")
    return domain


def turn_mood(mood: dict | None) -> str:
    """露西亞當輪 mood_v1 的主要情緒；未達「微」為平穩。"""
    if not isinstance(mood, dict):
        return CALM_MOOD
    try:
        first, _ = rank_mood(mood)
    except (KeyError, TypeError):
        return CALM_MOOD
    return first or CALM_MOOD


def aggregate_mood(entries: list[tuple[str | None, datetime | None]]) -> str | None:
    """成員證據中最常出現的心情；同數取最近出現者。沒有任何紀錄時為 None。"""
    counts: dict[str, int] = {}
    latest: dict[str, datetime | None] = {}
    for mood, occurred_at in entries:
        if mood not in CARD_MOODS:
            continue
        counts[mood] = counts.get(mood, 0) + 1
        previous = latest.get(mood)
        if previous is None or (occurred_at is not None and occurred_at > previous):
            latest[mood] = occurred_at
    if not counts:
        return None

    def order(mood):
        moment = latest[mood]
        return counts[mood], (moment.timestamp() if moment else float("-inf"))
    return max(counts, key=order)


def member_fingerprint(members: list[dict]) -> str:
    """要點 ID、文字、狀態、有效期間；REINFORCE 只加證據，不改變指紋。"""
    def stamp(value):
        return value.isoformat() if hasattr(value, "isoformat") else value
    rows = sorted([str(item["id"]), item["canonical_text"], item["status"],
                   stamp(item.get("valid_from")), stamp(item.get("valid_to"))] for item in members)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def summary_tokens(summary: str) -> list[str]:
    """概要中必須能在成員要點找到的片段：數字、英數專有名詞、引號／書名號內名稱與人名。"""
    value = unicodedata.normalize("NFKC", summary)
    tokens = [match.group(0) for match in _TOKEN_PATTERN.finditer(value)]
    tokens += [next(group for group in match.groups() if group) for match in _QUOTED_PATTERN.finditer(value)]
    tokens += _NAME_PATTERN.findall(value)
    return list(dict.fromkeys(tokens))


def validate_summary(summary: object, points: list[str]) -> str:
    if not isinstance(summary, str):
        raise ValueError("概要必須是文字")
    value = summary.strip()
    if not value or len(value) > SUMMARY_MAX_CHARS:
        raise ValueError(f"概要需為 1–{SUMMARY_MAX_CHARS} 字")
    source = unicodedata.normalize("NFKC", "\n".join(points)).casefold()
    missing = [token for token in summary_tokens(value) if token.casefold() not in source]
    if missing:
        raise ValueError("概要含成員要點沒有的內容：" + "、".join(missing[:5]))
    return value


def card_document(domain: str, title: str, summary: str | None, points: list[str]) -> str:
    """卡片向量的 document 文字：主題、副主題與概要；尚無概要時改用要點。"""
    body = summary if summary else "；".join(points[:6])
    return f"{DOMAINS.get(domain, domain)}／{title}：{body}"[:2000]


def card_header(domain: str | None, title: str | None, mood: str | None) -> str:
    """Chat 卡片標題列；心情是露西亞當時的情緒，不是使用者的感受。"""
    label = f"【{DOMAINS.get(domain, '其他')}／{title or '未分類'}】"
    if mood and mood != CALM_MOOD and mood in MOOD_LABELS:
        label += f"（露西亞當時的心情：{MOOD_LABELS[mood]}）"
    return label
