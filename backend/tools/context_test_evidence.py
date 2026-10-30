"""工具聊天的後端取得、receipt／圖片交付與來源隔離證據。"""
import json
from collections import Counter


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


def summarize_tools(records):
    tools = [tool for record in records for tool in record.get("tools", [])]
    return {"observed_turns": len(records), "selected_calls": len(tools),
            "executed_calls": sum(tool["executed"] for tool in tools),
            "names": dict(Counter(tool["name"] for tool in tools)),
            "backend_statuses": dict(Counter(tool["backend_status"] or "not_executed" for tool in tools)),
            "receipts_delivered": sum(tool["receipt_delivered"] for tool in tools),
            "images_accepted": sum(tool["image_accepted"] for tool in tools)}
