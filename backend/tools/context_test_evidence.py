"""工具聊天的後端取得、receipt／圖片交付與來源隔離證據。"""
import json
from collections import Counter
from datetime import datetime

from domain.runtime_context import CONTEXT_TOOL_NAMES

SCREENSHOT_FIXTURE_SHA256 = "88c514e2debde4187639b5ab2a95f1a810192d712ed8a3efbc17d20f255db2c2"


def collect_tools(record: dict) -> list[dict]:
    traces = record.get("trace", [])
    requests = [item for item in traces if item.get("stage") == "chat_request"]
    final_messages = requests[-1].get("messages", []) if requests else []
    delivered = {message["tool_call_id"]: json.loads(message["content"])
                 for message in final_messages if message.get("role") == "tool"}
    delivery = next((item for item in reversed(traces) if item.get("stage") == "context_tool_delivery"), {})
    image_hashes = {part.get("image_sha256") for message in final_messages
                    if isinstance(message.get("content"), list) for part in message["content"]
                    if part.get("type") == "image_url"}
    tools = []
    for selection in (item for item in traces if item.get("stage") == "context_tool_calls"):
        for call in selection["calls"]:
            call_id = call["id"]
            execution = next((item for item in traces if item.get("stage") == "context_tool_execution"
                              and item.get("call_id") == call_id), {})
            rejected = next((item for item in traces if item.get("stage") == "context_tool_rejected"
                             and item.get("call_id") == call_id), {})
            receipt = rejected.get("result", execution.get("result", {}))
            data = receipt.get("data") or {}
            final_receipt = delivered.get(call_id, {})
            accepted_image = (call_id in delivery.get("image_call_ids", [])
                              and delivery.get("image_accepted") is True
                              and data.get("image_sha256") in image_hashes)
            tools.append({
                "call_id": call_id, "name": call["name"], "arguments": execution.get("arguments"),
                "executed": execution.get("executed", False), "duration_sec": execution.get("duration_sec"),
                "backend_status": receipt.get("status"), "backend_reason": receipt.get("reason"),
                "result": receipt, "receipt_delivered": call_id in delivery.get("receipt_ids", []) and call_id in delivered,
                "delivery_status": final_receipt.get("status"), "delivery_reason": final_receipt.get("reason"),
                "receipt_matches_backend": final_receipt == receipt,
                "image_bytes": execution.get("image_bytes", 0), "image_accepted": accepted_image,
                "title_in_answer": delivery.get("title_in_answer", {}).get(call_id),
            })
    return tools


def check_context_tools(step, record, context, mode, check):
    traces = record.get("trace", [])
    origins = context.get("message_origins", [])
    messages = context.get("messages", [])
    roles = {"system": {"system"}, "input": {"user"}, "runtime_context": {"user"},
             "history": {"user", "assistant"}, "tool_call": {"assistant"},
             "tool_result": {"tool"}, "tool_image": {"user"}}
    order = {name: index for index, name in enumerate(
        ("system", "history", "runtime_context", "input", "tool_call", "tool_result", "tool_image"))}
    provenance = (len(origins) == len(messages) and origins.count("input") == 1 and origins.count("system") == 1
                  and origins.count("runtime_context") == 1 and bool(origins) and origins[0] == "system"
                  and all(message.get("role") in roles.get(origin, set()) for message, origin in zip(messages, origins))
                  and [order.get(origin, -1) for origin in origins] == sorted(order.get(origin, -1) for origin in origins)
                  and all(isinstance(message.get("content"), str) for message, origin in zip(messages, origins)
                          if origin in {"system", "input", "runtime_context", "history", "tool_result"}))
    check("isolation_status", "context_origins", origins, "每則訊息的來源與當輪輸入可辨識", provenance,
          "缺少或不完整的 prompt 來源標記")
    request = next((item for item in traces if item.get("stage") == "chat_request"), {})
    check("tool_evidence_status", "tools_available", [context.get("tools_available", []), request.get("tools", [])],
          list(CONTEXT_TOOL_NAMES), set(context.get("tools_available", [])) == set(CONTEXT_TOOL_NAMES)
          and set(request.get("tools", [])) == set(CONTEXT_TOOL_NAMES), "本輪未向模型提供全部五個工具")
    selection = any(item.get("stage") == "context_tool_selection" for item in traces)
    check("tool_evidence_status", "provider_tools", selection, True, selection, "模型未接受或未完成工具選擇請求")
    snapshot = context.get("runtime_context", {})
    runtime_present = "runtime_context" in origins
    snapshot_matches = False
    try:
        content = messages[origins.index("runtime_context")]["content"]
        projected = json.loads(content.split("\n", 1)[1])
        expected_apps = snapshot["open_apps"]["apps"]
        actual_apps = projected["open_apps"]["apps"]
        snapshot_matches = (projected["foreground"] == snapshot["foreground"]
                            and projected["desktop_captured_at"] == snapshot["desktop_captured_at"]
                            and projected["open_apps"]["status"] == snapshot["open_apps"]["status"]
                            and actual_apps == expected_apps[:len(actual_apps)]
                            and (len(actual_apps) == len(expected_apps) or projected["open_apps"]["truncated"]))
        clock = datetime.fromisoformat(snapshot["message_time"]).strftime("%Y-%m-%d %H:%M:%S %z")
        snapshot_matches &= clock in messages[0]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    check("tool_evidence_status", "snapshot_in_prompt", [snapshot.get("message_time"), runtime_present],
          "訊息時間與當輪桌面資料已注入", snapshot_matches,
          "缺少本輪快照或實際 prompt 未注入")
    tools = record["tools"] = collect_tools(record)
    executed = [tool for tool in tools if tool["executed"]]
    check("tool_evidence_status", "tool_limits", [len(executed), sum(t["name"] == "capture_screenshot" for t in executed)],
          "最多兩次執行、一次截圖", len(executed) <= 2 and sum(t["name"] == "capture_screenshot" for t in executed) <= 1,
          "本輪工具執行超過上限")
    check("tool_evidence_status", "receipt_pairs", [t["call_id"] for t in tools if t["receipt_delivered"]],
          "所有 native call 有對應且成功交付的 receipt", all(t["receipt_delivered"] for t in tools), "缺少工具結果配對或交付")
    check("tool_evidence_status", "receipt_contents", [t["call_id"] for t in tools if t["delivery_status"] == "ok"],
          "成功交付的結果與後端取得資料完全一致",
          all(t["receipt_matches_backend"] for t in tools if t["delivery_status"] == "ok"), "成功 receipt 與後端結果不一致")
    for name in step.get("expected_tools", []):
        selected = [tool for tool in tools if tool["name"] == name]
        ok = any(tool["executed"] and tool["backend_status"] == "ok" and tool["delivery_status"] == "ok"
                 and tool["receipt_delivered"] for tool in selected)
        check("tool_evidence_status", "required_" + name, selected, "AI 自行選擇且後端取得／交付成功", ok,
              "必要工具未呼叫、取得失敗或結果未送入最終模型")
        if name == "capture_screenshot":
            images = [tool for tool in selected if tool["image_bytes"] > 0 and tool["image_accepted"]]
            check("tool_evidence_status", "screenshot_image", images, "非空圖片與後端雜湊一致，模型已接受", bool(images),
                  "模型未取得該次後端產出的圖片")
            sources = [(tool["result"].get("data") or {}).get("source", {}) for tool in images]
            check("tool_evidence_status", "screenshot_fixture", sources, "sample/擷取.JPG 的後端 fixture 來源",
                  any(source.get("kind") == "fixture_image" and source.get("ref") == "sample/擷取.JPG"
                      and source.get("sha256") == SCREENSHOT_FIXTURE_SHA256 for source in sources),
                  "未使用指定模擬截圖來源")
        if name == "get_activity_context":
            check("tool_evidence_status", "activity_title_used", [tool["title_in_answer"] for tool in selected], True,
                  any(tool["title_in_answer"] is True for tool in selected), "回答沒有使用後端讀取的前景標題")
    if not mode.short_term:
        mixed = [tool["name"] for tool in tools if tool["receipt_delivered"] and tool["delivery_status"] == "ok"
                 and tool["name"] in {"get_activity_context", "capture_screenshot"}]
        check("isolation_status", "independent_db_source", mixed, [], not mixed,
              "純 DB probe 使用桌面／圖片來源，不能宣稱獨立 DB 召回")


def summarize_tools(records):
    dialogue = [record for record in records if record.get("action") != "compress"]
    tools = [tool for record in dialogue for tool in record.get("tools", [])]
    return {"observed_turns": len(dialogue), "selected_calls": len(tools),
            "executed_calls": sum(tool["executed"] for tool in tools),
            "names": dict(Counter(tool["name"] for tool in tools)),
            "backend_statuses": dict(Counter(tool["backend_status"] or "not_executed" for tool in tools)),
            "receipts_delivered": sum(tool["receipt_delivered"] for tool in tools),
            "images_accepted": sum(tool["image_accepted"] for tool in tools),
            "hard_statuses": dict(Counter(record.get("hard_status", {}).get("tool_evidence_status", "not_checked") for record in dialogue))}
