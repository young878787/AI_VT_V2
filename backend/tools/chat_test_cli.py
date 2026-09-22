"""
Headless Chat 測試 CLI：不開前端，直接連 /ws/chat 進行真實 LLM 多輪對話測試。

用法：
  # 互動模式（手動輸入）
  python tools/chat_test_cli.py

  # 腳本模式（預先寫好的 user 對話表，逐輪發送）
  python tools/chat_test_cli.py --scenario scenarios.txt

  # 限制輪數 / 重試次數 / 自訂連線
  python tools/chat_test_cli.py --scenario scenarios.txt --max-turns 20
  python tools/chat_test_cli.py --scenario scenarios.txt --retries 3
  python tools/chat_test_cli.py --url ws://localhost:9000/ws/chat --model Hiyori

每次執行一律先重置記憶（user_profile / memory.md / jpaf_state / 對話歷史），
保證全新測試起點。

Scenario 檔格式（# 開頭為註解，空白行跳過）：
  今天有點傷心
  好多了 謝謝
  還記得我昨天說什麼嗎

每輪擷取並記錄：
  - 回覆文字（text_stream）
  - 情緒 emotion（behavior payload / expression_plan）
  - JPAF 狀態（persona / weights / turn）
  - 記憶操作（記憶檔案逐輪 diff）

輸出：
  - backend/log/chat_test_report.jsonl（append，原始資料）
  - backend/log/chat_test_reports/chat_test_YYYYMMDD_HHMMSS.md（Markdown 報告，以時間命名方便查閱舊結果）
"""
import argparse
import asyncio
import json
import sys
import time
import websockets
from pathlib import Path
BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = BACKEND_ROOT / "log" / "chat_test_report.jsonl"
REPORT_MD_DIR = BACKEND_ROOT / "log" / "chat_test_reports"
MEMORY_DIR = BACKEND_ROOT / "memory"
PROFILE_PATH = MEMORY_DIR / "user_profile.json"
MEMORY_MD_PATH = MEMORY_DIR / "memory.md"
JPAF_PATH = MEMORY_DIR / "jpaf_state.json"


def load_scenario(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def snapshot_memory() -> dict:
    """讀取三個記憶檔案的當前狀態（讀不到就用 None）。"""
    def _read(p: Path):
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return p.read_text(encoding="utf-8")

    return {
        "user_profile": _read(PROFILE_PATH),
        "memory_md": _read(MEMORY_MD_PATH),
        "jpaf_state": _read(JPAF_PATH),
    }


class TurnCollector:
    """收集單輪 WebSocket 訊息，整理成測試報告用的 dict。"""

    def __init__(self, turn: int):
        self.turn = turn
        self.reply_parts: list[str] = []
        self.jpaf: dict | None = None
        self.expression_plan: dict | None = None
        self.behavior: dict | None = None
        self.errors: list[str] = []
        self.t0 = time.time()
        self.latency_first_text: float | None = None

    def feed(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "text_stream":
            if self.latency_first_text is None:
                self.latency_first_text = round(time.time() - self.t0, 2)
            self.reply_parts.append(msg.get("content", ""))
        elif mtype == "jpaf_update":
            self.jpaf = msg
        elif mtype == "expression_plan":
            self.expression_plan = msg
        elif mtype == "behavior":
            self.behavior = msg
        elif mtype == "error":
            self.errors.append(msg.get("content", ""))

    @property
    def reply(self) -> str:
        return "".join(self.reply_parts).strip()

    def to_record(self, memory_before: dict, memory_after: dict) -> dict:
        # 記憶差異：比較前後 snapshot
        memory_changes = {}
        for key in ("user_profile", "memory_md", "jpaf_state"):
            if memory_before.get(key) != memory_after.get(key):
                memory_changes[key] = memory_after.get(key)

        emotion = None
        if self.behavior:
            emotion = self.behavior.get("expression", {}).get("emotion") or self.behavior.get("emotion")
        if not emotion and self.expression_plan:
            emotion = self.expression_plan.get("debug", {}).get("intentEmotion")

        return {
            "turn": self.turn,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "reply": self.reply,
            "emotion": emotion,
            "jpaf": self.jpaf,
            "memory_changes": memory_changes,
            "errors": self.errors,
            "latency_first_text_sec": self.latency_first_text,
        }


def print_turn(rec: dict) -> None:
    print(f"\n{'=' * 60}")
    print(f"[Turn {rec['turn']}] {rec['ts']}  (首字延遲 {rec['latency_first_text_sec']}s)")
    print(f"{'=' * 60}")
    print(f"AI: {rec['reply']}")

    if rec["emotion"]:
        print(f"情緒: {json.dumps(rec['emotion'], ensure_ascii=False)}")

    if rec["jpaf"]:
        j = rec["jpaf"]
        print(
            f"JPAF: turn={j.get('turnCount')} persona={j.get('persona')} "
            f"dom={j.get('dominant')} aux={j.get('auxiliary')}"
        )
        print(f"  weights: {j.get('baseWeights')}")

    if rec["memory_changes"]:
        print("記憶變更:")
        for key, val in rec["memory_changes"].items():
            preview = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
            if len(preview) > 300:
                preview = preview[:300] + "...(截斷)"
            print(f"  - {key}: {preview}")

    if rec["errors"]:
        print(f"錯誤: {rec['errors']}")


# ============================================================
# Markdown 報告輸出
# ============================================================
def _trunc(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").replace("|", "\\|")
    return s if len(s) <= n else s[:n] + "…"


def _fmt_emotion(emotion) -> str:
    if emotion is None:
        return "-"
    if isinstance(emotion, str):
        return emotion
    if isinstance(emotion, dict):
        primary = emotion.get("primary") or emotion.get("primary_emotion") or emotion.get("emotion")
        intensity = emotion.get("intensity")
        if primary and intensity is not None:
            return f"{primary} ({intensity})"
        return primary or _trunc(json.dumps(emotion, ensure_ascii=False), 40)
    return str(emotion)


def _weights_diff(before: dict | None, after: dict | None) -> str:
    if not before or not after:
        return "-"
    changes = [
        f"{fn} {before[fn]:.2f}→{after[fn]:.2f}"
        for fn in after
        if fn in before and abs(before[fn] - after[fn]) > 0.0001
    ]
    return ", ".join(changes) if changes else "-"


def write_markdown_report(records: list[dict], md_path: Path, meta: dict) -> None:
    lines: list[str] = []
    lines.append("# Headless Chat 測試報告")
    lines.append("")
    lines.append(f"- 時間: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- Scenario: {meta.get('scenario') or '（互動模式）'}")
    lines.append("- Reset 記憶: 是（每次執行皆全新測試）")
    lines.append(f"- 總輪數: {len(records)}")
    lines.append("")

    # ---- 逐輪摘要表 ----
    lines.append("## 逐輪摘要")
    lines.append("")
    lines.append("| # | 使用者 | AI 回覆 | 情緒 | Persona | Turn | JPAF 權重變化 | 記憶變更 | 錯誤 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    prev_weights: dict | None = None
    prev_persona: str | None = None
    for rec in records:
        j = rec.get("jpaf") or {}
        persona = j.get("persona")
        persona_cell = persona or "-"
        if persona and prev_persona and persona != prev_persona:
            persona_cell = f"**{persona}**（切換自 {prev_persona}）"
        weights_cell = _weights_diff(prev_weights, j.get("baseWeights"))
        mem_keys = list((rec.get("memory_changes") or {}).keys())
        mem_cell = "、".join(
            {"user_profile": "user_profile", "memory_md": "memory.md", "jpaf_state": "jpaf_state"}.get(k, k)
            for k in mem_keys
        ) or "-"
        err_cell = "⚠️ " + _trunc(rec["errors"][0], 50) if rec.get("errors") else "-"
        lines.append(
            f"| {rec['turn']} | {_trunc(rec.get('user', ''), 24)} "
            f"| {_trunc(rec.get('reply', ''), 48)} "
            f"| {_fmt_emotion(rec.get('emotion'))} "
            f"| {persona_cell} | {j.get('turnCount', '-')} "
            f"| {weights_cell} | {mem_cell} | {err_cell} |"
        )
        if j.get("baseWeights"):
            prev_weights = j["baseWeights"]
        if persona:
            prev_persona = persona
    lines.append("")

    # ---- JPAF 演化 ----
    lines.append("## JPAF 演化")
    lines.append("")
    persona_switches = []
    prev = None
    for rec in records:
        p = (rec.get("jpaf") or {}).get("persona")
        if p and prev and p != prev:
            persona_switches.append(f"- Turn {rec['turn']}: {prev} → **{p}**")
        if p:
            prev = p
    if persona_switches:
        lines.extend(persona_switches)
    else:
        lines.append(f"- Persona 全程維持: **{prev}**（無切換）")
    final = snapshot_memory().get("jpaf_state") or {}
    if final:
        hist = final.get("active_history") or []
        lines.append(
            f"- 最終狀態: persona={final.get('current_persona')}, "
            f"dom={final.get('dominant')}, aux={final.get('auxiliary')}, "
            f"turn={final.get('turn_count')}"
        )
        if hist:
            lines.append(f"- active_function 歷史: {' → '.join(hist)}")
        lines.append(f"- 最終權重: `{final.get('base_weights')}`")
    lines.append("")

    # ---- 記憶時間線 ----
    lines.append("## 記憶時間線")
    lines.append("")
    mem_records = [r for r in records if r.get("memory_changes")]
    if not mem_records:
        lines.append("（全程無記憶檔變更）")
    for rec in mem_records:
        lines.append(f"### Turn {rec['turn']}：{_trunc(rec.get('user', ''), 30)}")
        changes = rec.get("memory_changes") or {}
        if "memory_md" in changes:
            md = changes["memory_md"]
            note_lines = [ln for ln in str(md).splitlines() if ln.strip().startswith("- [")]
            for ln in note_lines:
                lines.append(f"- memory.md: {ln.strip()}")
        if "user_profile" in changes:
            prof = changes["user_profile"]
            lines.append(f"- user_profile: `{_trunc(json.dumps(prof, ensure_ascii=False), 300)}`")
        if "jpaf_state" in changes:
            lines.append("- jpaf_state: 更新（詳見 JPAF 演化）")
        lines.append("")

    # ---- 錯誤 ----
    err_records = [r for r in records if r.get("errors")]
    if err_records:
        lines.append("## 錯誤記錄")
        lines.append("")
        for rec in err_records:
            for e in rec["errors"]:
                lines.append(f"- Turn {rec['turn']}: {e}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown 報告已寫入: {md_path}")


async def run_turn(ws, turn: int, user_message: str) -> dict:
    collector = TurnCollector(turn)
    memory_before = snapshot_memory()

    await ws.send(json.dumps({"type": "chat", "content": user_message, "model_name": "Hiyori"}))

    # 等到 stream_end / error 才算本輪結束
    received_any = False
    while True:
        try:
            raw = await ws.recv()
        except websockets.ConnectionClosed:
            # 後端 API error 時會 raise 並關閉連線（chat_ws.py）；
            # 已收到過訊息（含 error）→ 本輪結束；完全沒收到 → 讓上層重連重試。
            if received_any:
                if not collector.errors:
                    collector.errors.append("連線被後端關閉（未收到 error 訊息）")
                break
            raise
        received_any = True
        msg = json.loads(raw)
        mtype = msg.get("type")
        collector.feed(msg)
        if mtype in ("stream_end", "error"):
            break
        if mtype == "compressing":
            print("  ...（context 壓縮中）")

    memory_after = snapshot_memory()
    rec = collector.to_record(memory_before, memory_after)
    rec["user"] = user_message
    return rec


async def reset_memory(ws, http_base: str) -> None:
    """還原記憶：REST /api/reset-memory 清檔 + WS reset 重新載入。"""
    import httpx

    try:
        resp = await httpx.AsyncClient().post(f"{http_base}/api/reset-memory")
        if resp.status_code != 200:
            print(f"[警告] reset-memory 回應 {resp.status_code}: {resp.text[:100]}")
    except Exception as exc:
        print(f"[警告] REST reset-memory 失敗（僅做 WS reset）: {exc}")

    await ws.send(json.dumps({"type": "reset"}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("type") == "reset_done":
            break
    print("（記憶已重置：user_profile / memory.md / jpaf_state / 對話歷史）")


async def connect_ws(url: str):
    ws = await websockets.connect(url, max_size=10 * 1024 * 1024)
    init = json.loads(await ws.recv())
    if init.get("type") == "jpaf_update":
        print(
            f"  重連完成（JPAF: persona={init.get('persona')} "
            f"turn={init.get('turnCount')}）— 注意：in-memory 對話歷史已重置"
        )
    return ws


def ws_is_open(ws) -> bool:
    try:
        return ws.state is websockets.State.OPEN
    except AttributeError:
        return not ws.closed


async def ensure_ws(ws_holder) -> None:
    """確保連線可用；後端在 API error 時會關閉 WS，此時主動重連。"""
    if not ws_is_open(ws_holder["ws"]):
        print("  連線已關閉，自動重連...")
        ws_holder["ws"] = await connect_ws(ws_holder["url"])


def turn_failed(rec: dict) -> bool:
    """判定該輪是否需要重試：完全沒有回覆文字且記錄了錯誤。"""
    return bool(rec.get("errors")) and not (rec.get("reply") or "").strip()


async def run_turn_with_retry(ws_holder, turn: int, user_message: str, retries: int) -> dict:
    """執行單輪，失敗（API 錯誤 / 斷線）自動重試 retries 次，避免一次失敗卡死整場測試。

    失敗情境：
    1. run_turn 拋出 ConnectionClosed → 重連後重試
    2. 收到後端 error 訊息且無回覆文字 → 後端已關閉連線，重連後重試
    注意：重連後 in-memory 對話歷史會遺失（除非 CHAT_PERSISTENCE_ENABLED）。
    """
    attempts = 1 + max(0, retries)
    rec = None
    for attempt in range(1, attempts + 1):
        try:
            await ensure_ws(ws_holder)
            rec = await run_turn(ws_holder["ws"], turn, user_message)
            if not turn_failed(rec):
                if attempt > 1:
                    rec["retried"] = attempt
                return rec
            reason = rec["errors"][0]
        except websockets.ConnectionClosed as exc:
            reason = f"連線中斷: {exc}"

        if attempt < attempts:
            print(f"  第 {attempt}/{attempts} 次失敗（{reason[:80]}），重試...")
            await asyncio.sleep(1.5)
        else:
            print(f"  {attempts} 次嘗試皆失敗，放棄此輪。")
            if rec is None:
                rec = {
                    "turn": turn,
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "user": user_message,
                    "reply": "",
                    "emotion": None,
                    "jpaf": None,
                    "memory_changes": {},
                    "errors": [f"{attempts} 次嘗試皆失敗: {reason}"],
                    "latency_first_text_sec": None,
                    "reconnected": True,
                }
    if rec is not None:
        rec["retried"] = attempt
    return rec


async def interactive_loop(ws_holder, max_turns: int, report_f, records: list, retries: int):
    turn = 0
    while max_turns <= 0 or turn < max_turns:
        try:
            user_message = input(f"\n[Turn {turn + 1}] You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n結束。")
            break
        if not user_message:
            continue
        if user_message in ("/quit", "/exit"):
            break

        turn += 1
        rec = await run_turn_with_retry(ws_holder, turn, user_message, retries)
        rec["user"] = user_message
        print_turn(rec)
        records.append(rec)
        report_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        report_f.flush()


async def scenario_loop(ws_holder, scenario: list[str], max_turns: int, report_f, records: list, retries: int):
    """逐輪執行 scenario。失敗自動重連 + 重試，避免單輪失敗卡死整場測試。"""
    msgs = scenario[:max_turns] if max_turns > 0 else scenario
    for i, user_message in enumerate(msgs, 1):
        print(f"\n>>> 發送: {user_message}")
        rec = await run_turn_with_retry(ws_holder, i, user_message, retries)
        rec["user"] = user_message
        print_turn(rec)
        records.append(rec)
        report_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        report_f.flush()
    print(f"\nScenario 完成，共 {len(msgs)} 輪。")


async def main():
    parser = argparse.ArgumentParser(description="Headless chat 測試 CLI")
    parser.add_argument("--url", default="ws://localhost:9000/ws/chat")
    parser.add_argument("--model", default="Hiyori")
    parser.add_argument("--scenario", help="對話腳本檔路徑（一行一輪）")
    parser.add_argument("--max-turns", type=int, default=0, help="最多幾輪（0 = 不限）")
    parser.add_argument("--retries", type=int, default=2, help="每輪失敗時的重試次數（預設 2，總嘗試 = 1 + retries）")
    args = parser.parse_args()

    # ws://localhost:9000/ws/chat → http://localhost:9000
    http_base = args.url.split("/ws/")[0].replace("ws://", "http://").replace("wss://", "https://")

    print(f"連線: {args.url}")
    async with websockets.connect(args.url, max_size=10 * 1024 * 1024) as ws:
        # 收初始 jpaf_update
        raw = await ws.recv()
        init = json.loads(raw)
        if init.get("type") == "jpaf_update":
            print(
                f"初始 JPAF: persona={init.get('persona')} "
                f"dom={init.get('dominant')} turn={init.get('turnCount')}"
            )

        # 每次執行都是全新測試：一律重置記憶
        await reset_memory(ws, http_base)

        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_MD_DIR.mkdir(parents=True, exist_ok=True)
        records: list[dict] = []
        with open(REPORT_PATH, "a", encoding="utf-8") as report_f:
            if args.scenario:
                scenario = load_scenario(args.scenario)
                print(f"Scenario: {len(scenario)} 輪（max-turns={args.max_turns or '不限'}, retries={args.retries}）")
                await scenario_loop({"ws": ws, "url": args.url}, scenario, args.max_turns, report_f, records, args.retries)
            else:
                await interactive_loop(
                    {"ws": ws, "url": args.url, "http_base": http_base}, args.max_turns, report_f, records, args.retries
                )

        md_path = REPORT_MD_DIR / f"chat_test_{time.strftime('%Y%m%d_%H%M%S')}.md"
        write_markdown_report(records, md_path, {"scenario": args.scenario})


if __name__ == "__main__":
    asyncio.run(main())
