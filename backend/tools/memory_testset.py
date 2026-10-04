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
    required = {"case_id", "case_group", "case_type", "source", "expected_focus", "expected_result", "conversation"}
    for case in cases:
        if not isinstance(case, dict) or not required <= case.keys():
            raise ValueError("案例缺少必要欄位")
        if case.keys() - required - {"base_case", "event_date"}:
            raise ValueError("案例包含未授權欄位")
        if case["source"] != source or not isinstance(case["case_id"], str) or case["case_id"] in ids:
            raise ValueError("案例來源或 ID 錯誤")
        ids.add(case["case_id"])
        if case["case_group"] not in {"short_term", "long_term", "mixed"}:
            raise ValueError("case_group 錯誤")
        if not isinstance(case["case_type"], str) or not 1 <= len(case["case_type"]) <= 80:
            raise ValueError("案例類型錯誤")
        focus = case["expected_focus"]
        if not isinstance(focus, list) or not 1 <= len(focus) <= 8 or any(
                not isinstance(item, str) or not 1 <= len(item) <= 300 for item in focus):
            raise ValueError("expected_focus 必須是有界觀察方向")
        if not isinstance(case["expected_result"], str) or not 1 <= len(case["expected_result"]) <= 2000:
            raise ValueError("expected_result 必須是有界參考答案")
        steps = case["conversation"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise ValueError("conversation 為空或超出限制")
        seen_sessions = set()
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict) or not {"phase", "session"} <= step.keys() or step.keys() - {
                    "phase", "session", "input", "action", "expected_result", "evidence", "memory_expectation", "versions"}:
                raise ValueError("步驟只接受 user input 或 compress 與有界驗收條件")
            session = step["session"]
            if not isinstance(session, str) or not 1 <= len(session) <= 40:
                raise ValueError("session 別名錯誤")
            step_mode(case, step)
            if "action" in step:
                if "input" in step or step["action"] != "compress" or session not in seen_sessions or step["phase"] != "context":
                    raise ValueError("compress 必須在已有對話的 context session")
                continue
            value = step.get("input")
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_INPUT:
                raise ValueError("input 為空或超出限制")
            if "expected_result" in step and (not isinstance(step["expected_result"], str) or not 1 <= len(step["expected_result"]) <= 2000):
                raise ValueError("probe expected_result 錯誤")
            if step_mode(case, step).value == "memory_probe" and session in seen_sessions:
                raise ValueError("獨立長期 probe 必須使用全新 session")
            if step.get("memory_expectation", "optional") not in {"stored", "optional", "ignored", "buffered"}:
                raise ValueError("memory_expectation 錯誤")
            evidence = step.get("evidence", [])
            if not isinstance(evidence, list) or len(evidence) > 8:
                raise ValueError("evidence 超出限制")
            if source == "generated" and step["phase"] == "probe" and not evidence:
                raise ValueError("生成參考答案必須引用 user setup step")
            for fact in evidence:
                if not isinstance(fact, dict) or set(fact) != {"source_step", "source", "fragments"}:
                    raise ValueError("來源引用格式錯誤")
                ref = fact["source_step"]
                if type(ref) is not int or not 1 <= ref < index or "input" not in steps[ref - 1] or steps[ref - 1]["phase"] not in {"memory_setup", "context", "update"}:
                    raise ValueError("來源引用必須指向先前 user setup step")
                if fact["source"] not in {"db", "history", "summary", "context"}:
                    raise ValueError("來源類型錯誤")
                fragments = fact["fragments"]
                if not isinstance(fragments, list) or not 1 <= len(fragments) <= 8 or any(
                        not isinstance(f, str) or not f or f not in steps[ref - 1]["input"] for f in fragments):
                    raise ValueError("來源片段必須存在於引用的 user input")
                if fact["source"] == "db" and (not step_mode(case, steps[ref - 1]).memory_write or not step_mode(case, step).memory_read):
                    raise ValueError("DB 來源必須在可寫入的 setup step")
                if fact["source"] != "db" and (steps[ref - 1]["session"] != session or not step_mode(case, step).short_term):
                    raise ValueError("短期來源必須在相同 session")
            versions = step.get("versions", [])
            if not isinstance(versions, list) or len(versions) > 4:
                raise ValueError("versions 超出限制")
            for version in versions:
                if not isinstance(version, dict) or set(version) != {"old_step", "new_step", "old", "new", "allowed_operations"}:
                    raise ValueError("版本條件格式錯誤")
                for key in ("old", "new"):
                    ref = version[key + "_step"]
                    if type(ref) is not int or not 1 <= ref < index or not isinstance(version[key], str) or not version[key] or version[key] not in steps[ref - 1].get("input", ""):
                        raise ValueError("版本片段必須引用先前 user input")
                operations = version["allowed_operations"]
                if not isinstance(operations, list) or not operations or set(operations) - {"CREATE", "REINFORCE", "MERGE", "SUPERSEDE", "ARCHIVE", "CONTRADICT"}:
                    raise ValueError("允許操作集合錯誤")
            seen_sessions.add(session)
        probes = [s for s in steps if s["phase"] in {"probe", "recall_probe"}]
        if not probes and (source == "generated" or case["case_id"] not in {f"case_{i:03}" for i in range(6, 11)}):
            raise ValueError("召回案例必須有 probe")
        if case["case_type"] in {"cross_source_synthesis", "temporary_constraint"} and any(
                not any(f["source"] == "db" for f in s.get("evidence", [])) or
                not any(f["source"] != "db" for f in s.get("evidence", [])) for s in probes):
            raise ValueError("綜合 probe 必須引用 DB 與短期兩項來源")
        if case["case_group"] == "short_term" and len(seen_sessions) != 1:
            raise ValueError("短期案例必須使用同一 session")
    if source == "Gold" and [case["case_id"] for case in cases] != [f"case_{i:03}" for i in range(1, 24)]:
        raise ValueError("固定核心 ID 必須為 case_001–case_023")
    return cases


def step_mode(case: dict, step: dict):
    from domain.chat_test_mode import ChatTestMode
    group, phase = case["case_group"], step["phase"]
    matrix = {
        "short_term": {"context": "short_only", "probe": "short_only"},
        "long_term": {"memory_setup": "memory_seed", "probe": "memory_probe"},
        "mixed": {"memory_setup": "memory_seed", "context": "short_only", "probe": "mixed_read", "update": "mixed_update", "recall_probe": "memory_probe"},
    }
    if group not in matrix or phase not in matrix[group]:
        raise ValueError("case_group / phase 組合錯誤")
    return ChatTestMode(matrix[group][phase])


def validate_generated(cases, specifications):
    validate_cases(cases, source="generated", count=5)
    by_id = {case["case_id"]: case for case in cases}
    if set(by_id) != {spec["case_id"] for spec in specifications}:
        raise ValueError("生成 ID 錯誤")
    ordered = []
    for spec in specifications:
        case = by_id[spec["case_id"]]
        if any(case.get(key) != spec[key] for key in ("case_type", "base_case", "case_group")):
            raise ValueError("生成類型、base case 或召回意圖漂移")
        steps = case["conversation"]
        setups = [step for step in steps if step["phase"] in {"memory_setup", "context"} and "input" in step]
        minimum = {"distractor_retrieval": 4, "memory_update": 2, "multi_memory_synthesis": 4}.get(case["case_type"], 1)
        if len(setups) < minimum:
            raise ValueError("生成前置事實不足")
        ordered.append(case)
    return ordered


def load_snapshot(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) != 28:
        raise ValueError("重播快照必須恰好 28 cases")
    validate_cases(data[:23], source="Gold", count=23)
    specs = []
    for index, (kind, bases, route) in enumerate(GENERATED_TYPES, 21):
        case = data[23 + index - 21]
        if case.get("base_case") not in {f"case_{base:03}" for base in bases}:
            raise ValueError("快照 base case 錯誤")
        specs.append(dict(case_id=f"random_{index:03}", case_type=kind,
                          base_case=case["base_case"], case_group=route))
    validate_generated(data[23:], specs)
    return data


async def generate_cases(core: list[dict], diagnostics: dict) -> list[dict]:
    # 延遲載入，結構驗證與重播不需要初始化 AI client。
    from core.config import CHAT_MODEL_NAME
    from infrastructure.ai_client import chat_create_with_fallback

    specifications = []
    for index, (kind, bases, route) in enumerate(GENERATED_TYPES, 21):
        base = core[random.choice(bases) - 1]
        specifications.append({"case_id": f"random_{index:03}", "case_type": kind,
            "base_case": base["case_id"], "case_group": route,
            "memory_goal": base["expected_focus"], "difficulty": "moderate",
            "required_behavior": {"short_term": "context 與 probe 同 session；正常對話，不加禁止記憶前綴",
                "long_term": "memory_setup 建立新事實；probe 每次使用不同的新 session"}[route],
            "base_conversation": base["conversation"]})
    prompt = (
        "你是測試資料作者。只回傳 JSON array，恰好五筆繁體中文案例。改寫主體、事實與干擾，"
        "保留指定 case_id/case_type/base_case/case_group。source 固定 generated。"
        "每筆只有 case_id,case_group,case_type,base_case,source,expected_focus,expected_result,conversation。"
        "expected_focus 為 1–8 筆方向；expected_result 為自然語言參考答案，包含應回答事實與不可推定內容。"
        "conversation 為 2–24 步，每步有 phase,session,input。短期 phase=context/probe，長期=memory_setup/probe。"
        "input 是 user 文字，1–1000 字。不要提供 assistant reply、SQL、工具或執行指令。"
        "每個 probe 必須提供 expected_result 與 evidence array；每筆 evidence 只有 source_step(從1起算的先前user setup步號),"
        "source(短期為history或context，長期為db),fragments(1–8個原樣存在於該user setup輸入的事實片段)。"
        "參考答案只能引用這些 setup 事實，不得憑空寫標準答案。短期全部同 session，不加禁止保存前綴。"
        "長期每個 probe 必須使用全新 session。memory_setup 的持久事實設 memory_expectation=stored。"
        "干擾至少四筆 setup（目標與三個不同領域）；更新至少兩筆 setup（同主體舊值、新值），"
        "並在probe加入versions:[{old_step,new_step,old,new,allowed_operations:[SUPERSEDE,MERGE,ARCHIVE,CREATE]}]，"
        "old/new為各setup原樣片段。整合至少四筆不同原子事實 setup，probe evidence 必須引用所有必要事實。"
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
