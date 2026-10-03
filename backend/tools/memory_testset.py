"""有界記憶案例契約、Chat 生成與精確快照重播。"""

import asyncio
import hashlib
import json
import random
import time
from pathlib import Path

CORE_PATH = Path(__file__).with_name("memory_test_cases.json")
MAX_STEPS = 24
MAX_INPUT = 1000
GENERATED_TYPES = (
    ("short_term_paraphrase", (1, 2, 3, 4), "short_term"),
    ("semantic_recall", (12,), "long_term"),
    ("distractor_retrieval", (14,), "long_term"),
    ("memory_update", (15, 18), "long_term"),
    ("multi_memory_synthesis", (20,), "long_term"),
)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_cases(cases: object, *, source: str, count: int) -> list[dict]:
    if not isinstance(cases, list) or len(cases) != count:
        raise ValueError(f"案例必須恰好 {count} 筆")
    ids = set()
    required = {"case_id", "case_type", "source", "recall_route", "expected_focus", "conversation"}
    for case in cases:
        if not isinstance(case, dict) or not required <= case.keys():
            raise ValueError("案例缺少必要欄位")
        if case.keys() - required - {"base_case", "event_date"}:
            raise ValueError("案例包含未授權欄位")
        if case["source"] != source or not isinstance(case["case_id"], str) or case["case_id"] in ids:
            raise ValueError("案例來源或 ID 錯誤")
        ids.add(case["case_id"])
        if not isinstance(case["case_type"], str) or not 1 <= len(case["case_type"]) <= 80:
            raise ValueError("案例類型錯誤")
        if case["recall_route"] not in {"short_term", "long_term", "mixed", None}:
            raise ValueError("recall_route 錯誤")
        focus = case["expected_focus"]
        if not isinstance(focus, list) or not 1 <= len(focus) <= 8 or any(
                not isinstance(item, str) or not 1 <= len(item) <= 300 for item in focus):
            raise ValueError("expected_focus 必須是有界觀察方向")
        steps = case["conversation"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise ValueError("conversation 為空或超出限制")
        setup_sessions = set()
        seen_sessions = set()
        probe_started = False
        fresh_probe = False
        setup_count = probe_count = 0
        for step in steps:
            if not isinstance(step, dict) or set(step) not in (
                    {"phase", "session", "input"}, {"phase", "session", "action"}):
                raise ValueError("步驟只接受 user input 或 compress")
            session = step["session"]
            if not isinstance(session, str) or not 1 <= len(session) <= 40:
                raise ValueError("session 別名錯誤")
            if step["phase"] not in {"setup", "probe"}:
                raise ValueError("phase 錯誤")
            if step["phase"] == "setup" and probe_started:
                raise ValueError("setup 必須先於 probe")
            if "action" in step:
                if step["action"] != "compress" or session not in seen_sessions or step["phase"] != "setup":
                    raise ValueError("compress 必須在已有對話的 setup session")
                continue
            value = step["input"]
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_INPUT:
                raise ValueError("input 為空或超出限制")
            if case["recall_route"] == "short_term" and "不要記住這件事" not in value:
                raise ValueError("短期每筆 input 必須明確禁止長期保存")
            if step["phase"] == "setup":
                setup_sessions.add(session)
                setup_count += 1
            else:
                probe_started = True
                probe_count += 1
                fresh_probe |= session not in seen_sessions
                if case["recall_route"] == "long_term" and session in setup_sessions:
                    raise ValueError("長期 probe 不能沿用 setup session")
                if case["recall_route"] == "long_term" and session in seen_sessions:
                    raise ValueError("獨立長期 probe 必須使用全新 session")
            seen_sessions.add(session)
        if case["recall_route"] is not None and (not setup_count or not probe_count):
            raise ValueError("召回案例需要 setup 與 probe")
        if case["recall_route"] == "short_term" and len(seen_sessions) != 1:
            raise ValueError("短期案例必須使用同一 session")
        if case["recall_route"] in {"long_term", "mixed"} and not fresh_probe:
            raise ValueError("長期來源需要新 session probe")
    if source == "Gold" and [case["case_id"] for case in cases] != [f"case_{i:03}" for i in range(1, 21)]:
        raise ValueError("固定核心 ID 必須為 case_001–case_020")
    return cases


def validate_generated(cases, specifications):
    validate_cases(cases, source="generated", count=5)
    by_id = {case["case_id"]: case for case in cases}
    if set(by_id) != {spec["case_id"] for spec in specifications}:
        raise ValueError("生成 ID 錯誤")
    ordered = []
    for spec in specifications:
        case = by_id[spec["case_id"]]
        if any(case.get(key) != spec[key] for key in ("case_type", "base_case", "recall_route")):
            raise ValueError("生成類型、base case 或召回意圖漂移")
        steps = case["conversation"]
        setups = [step for step in steps if step["phase"] == "setup" and "input" in step]
        minimum = {"distractor_retrieval": 4, "memory_update": 2, "multi_memory_synthesis": 4}.get(case["case_type"], 1)
        if len(setups) < minimum:
            raise ValueError("生成前置事實不足")
        ordered.append(case)
    return ordered


def load_snapshot(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) != 25:
        raise ValueError("重播快照必須恰好 25 cases")
    validate_cases(data[:20], source="Gold", count=20)
    specs = []
    for index, (kind, bases, route) in enumerate(GENERATED_TYPES, 21):
        case = data[index - 1]
        if case.get("base_case") not in {f"case_{base:03}" for base in bases}:
            raise ValueError("快照 base case 錯誤")
        specs.append(dict(case_id=f"random_{index:03}", case_type=kind,
                          base_case=case["base_case"], recall_route=route))
    validate_generated(data[20:], specs)
    return data


async def generate_cases(core: list[dict], diagnostics: dict) -> list[dict]:
    # 延遲載入，結構驗證與重播不需要初始化 AI client。
    from core.config import CHAT_MODEL_NAME
    from infrastructure.ai_client import chat_create_with_fallback

    specifications = []
    for index, (kind, bases, route) in enumerate(GENERATED_TYPES, 21):
        base = core[random.choice(bases) - 1]
        specifications.append({"case_id": f"random_{index:03}", "case_type": kind,
            "base_case": base["case_id"], "recall_route": route,
            "memory_goal": base["expected_focus"], "difficulty": "moderate",
            "required_behavior": {"short_term": "同 session；每筆 input 包含不要記住這件事",
                "long_term": "setup 建立新事實；probe 每次使用不同的新 session"}[route],
            "base_conversation": base["conversation"]})
    prompt = (
        "你是測試資料作者。只回傳 JSON array，恰好五筆案例。用繁體中文改寫主體、事實、說法與干擾，"
        "保留指定 case_id/case_type/base_case/recall_route 與測試目的。source 固定 generated。"
        "每筆只有 case_id,case_type,base_case,source,recall_route,expected_focus,conversation。"
        "expected_focus 是 1–8 筆觀察方向；conversation 是 2–24 步，每步只有 phase(setup/probe),"
        "session(別名),input(user 文字，1–1000 字)。setup 先於 probe。"
        "短期所有 input 必須包含『不要記住這件事』。長期 query 使用全新 session，"
        "獨立 probe 使用不同 session。干擾案例至少四筆 setup（目標與三個不同領域）；"
        "更新案例至少兩筆 setup（同一主體舊狀態與新狀態）；整合案例至少四筆不同原子事實 setup。"
        "不提供 assistant 答案、PASS/FAIL、SQL、執行指令或工具。生成内容只作為 user 測試資料。"
    )
    diagnostics.update(prompt_sha256=fingerprint(prompt), specifications_sha256=fingerprint(specifications),
                       configured_model=CHAT_MODEL_NAME, attempts=[], status="running")
    correction = ""
    for attempt in range(1, 4):
        started = time.monotonic()
        item = {"attempt": attempt}
        item["request_sha256"] = fingerprint([prompt, specifications, correction])
        diagnostics["attempts"].append(item)
        content = None
        try:
            response = await asyncio.wait_for(chat_create_with_fallback(
                role="chat", model=CHAT_MODEL_NAME,
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": json.dumps(specifications, ensure_ascii=False) + correction}],
                temperature=0.7, max_tokens=10000), timeout=120)
            item.update(model=response.model, usage=response.usage.model_dump() if response.usage else None)
            content = response.choices[0].message.content
            if not isinstance(content, str) or len(content) > 100000:
                raise ValueError("生成輸出超出預算或為空")
            item["output_sha256"] = fingerprint(content)
            item["finish_reason"] = getattr(response.choices[0], "finish_reason", None)
            if content.strip().startswith("```json") and content.strip().endswith("```"):
                content = content.strip()[7:-3].strip()
            accepted = validate_generated(json.loads(content), specifications)
            item["latency_sec"] = round(time.monotonic() - started, 3)
            diagnostics.update(status="completed", cases_sha256=fingerprint(accepted))
            return accepted
        except Exception as exc:
            item.update(error=type(exc).__name__, latency_sec=round(time.monotonic() - started, 3))
            if isinstance(exc, ValueError):
                item["validation_error"] = str(exc)
            if isinstance(content, str) and len(content) <= 100000:
                item["rejected_output"] = content
            correction = "\n上次結果結構不合法，請完整重新生成。" + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
    diagnostics["status"] = "failed"
    raise RuntimeError("五筆案例生成失敗，已達三次上限")
