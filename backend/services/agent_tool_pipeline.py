import json

from core.utils import strip_thinking
from domain.tools.schema_loader import load_schema
from services.chat_service import parse_xml_tool_calls
from services.tool_arg_parser import parse_tool_call_arguments


EXPRESSION_AGENT_ALLOWED_TOOL_NAMES = {"set_ai_behavior", "blink_control"}
MEMORY_AGENT_ALLOWED_TOOL_NAMES = {"update_user_profile", "save_memory_note"}
BLINK_CONTROL_ALLOWED_ACTIONS = {"force_blink", "pause", "resume", "set_interval"}
UPDATE_USER_PROFILE_ALLOWED_ACTIONS = {"add", "remove", "update"}
UPDATE_USER_PROFILE_ALLOWED_FIELDS = {
    "core_traits",
    "dislikes",
    "recent_interests",
    "communication_style",
    "custom_notes",
}


def _clean_non_empty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _get_tool_parameter_enum_values(tool_name: str, parameter_name: str, model_name: str) -> set[str] | None:
    schema = load_schema(model_name)
    for tool in schema.get("openai_tools", {}).get("memory", []):
        function_def = tool.get("function", {})
        if function_def.get("name") != tool_name:
            continue
        properties = function_def.get("parameters", {}).get("properties", {})
        enum_values = properties.get(parameter_name, {}).get("enum")
        if isinstance(enum_values, list) and enum_values:
            return {value for value in enum_values if isinstance(value, str)}
        return None
    return None


def get_meaningful_memory_tool_arguments(tool_name: str, args: dict, model_name: str = "Hiyori") -> dict | None:
    if tool_name == "update_user_profile":
        action = _clean_non_empty_string(args.get("action"))
        field = _clean_non_empty_string(args.get("field"))
        value = _clean_non_empty_string(args.get("value"))
        allowed_actions = (
            _get_tool_parameter_enum_values(tool_name, "action", model_name)
            or UPDATE_USER_PROFILE_ALLOWED_ACTIONS
        )
        allowed_fields = (
            _get_tool_parameter_enum_values(tool_name, "field", model_name)
            or UPDATE_USER_PROFILE_ALLOWED_FIELDS
        )
        if (
            not action
            or not field
            or not value
            or action not in allowed_actions
            or field not in allowed_fields
        ):
            return None
        return {
            "action": action,
            "field": field,
            "value": value,
        }

    if tool_name == "save_memory_note":
        content = _clean_non_empty_string(args.get("content"))
        if not content:
            return None
        return {"content": content}

    return args


def get_meaningful_expression_tool_arguments(tool_name: str, args: dict) -> dict | None:
    if tool_name == "blink_control":
        action = _clean_non_empty_string(args.get("action"))
        if not action or action not in BLINK_CONTROL_ALLOWED_ACTIONS:
            return None
        if action == "set_interval":
            interval_min = args.get("interval_min")
            interval_max = args.get("interval_max")
            if (
                isinstance(interval_min, bool)
                or not isinstance(interval_min, (int, float))
                or isinstance(interval_max, bool)
                or not isinstance(interval_max, (int, float))
                or interval_min > interval_max
            ):
                return None
        return args

    return args


def sanitize_agent_tool_call(tool_name: str, args: dict, label: str, model_name: str = "Hiyori") -> dict | None:
    if tool_name in EXPRESSION_AGENT_ALLOWED_TOOL_NAMES:
        meaningful_args = get_meaningful_expression_tool_arguments(tool_name, args)
        if meaningful_args is None:
            print(f"[{label}][SKIP_INCOMPLETE_TOOL_ARGS] name={tool_name}")
            return None
        args = meaningful_args

    meaningful_args = get_meaningful_memory_tool_arguments(tool_name, args, model_name=model_name)
    if tool_name in MEMORY_AGENT_ALLOWED_TOOL_NAMES and meaningful_args is None:
        print(f"[{label}][SKIP_INCOMPLETE_TOOL_ARGS] name={tool_name}")
        return None
    if meaningful_args is not None:
        args = meaningful_args

    return {"name": tool_name, "arguments": args}


def extract_agent_tool_calls(response: object, model_name: str, label: str) -> list[dict]:
    """Collect tool calls, using XML only as a fallback transport path."""
    calls: list[dict] = []
    if not getattr(response, "choices", None):
        return calls

    message = response.choices[0].message
    content = strip_thinking(message.content or "")
    native_tool_names: set[str] = set()

    if getattr(message, "tool_calls", None):
        for tc in message.tool_calls:
            try:
                args, was_normalized = parse_tool_call_arguments(
                    tc.function.arguments or "",
                    tool_name=tc.function.name,
                    model_name=model_name,
                )
                if not isinstance(args, dict):
                    print(
                        f"[{label}][SKIP_NON_OBJECT_TOOL_ARGS] "
                        f"name={tc.function.name}, type={type(args).__name__}"
                    )
                    continue
                sanitized_call = sanitize_agent_tool_call(tc.function.name, args, label, model_name=model_name)
                if sanitized_call is None:
                    continue
                calls.append(sanitized_call)
                native_tool_names.add(tc.function.name)
                if was_normalized:
                    print(f"[{label}][NORMALIZED_TOOL_ARGS] name={tc.function.name}")
            except json.JSONDecodeError as e:
                raw_args = tc.function.arguments or ""
                around_start = max(0, e.pos - 80)
                around_end = min(len(raw_args), e.pos + 80)
                print(
                    f"[{label}][MALFORMED_TOOL_ARGS] "
                    f"name={tc.function.name}, pos={e.pos}, len={len(raw_args)}, error={e}"
                )
                print(f"[{label}][MALFORMED_TOOL_ARGS][RAW] {raw_args}")
                print(
                    f"[{label}][MALFORMED_TOOL_ARGS][AROUND] "
                    f"{raw_args[around_start:around_end]}"
                )

    xml_calls, _ = parse_xml_tool_calls(content)
    if xml_calls:
        fallback_calls: list[dict] = []
        for call in xml_calls:
            if call["name"] in native_tool_names:
                continue
            sanitized_call = sanitize_agent_tool_call(call["name"], call["arguments"], label, model_name=model_name)
            if sanitized_call is None:
                continue
            fallback_calls.append(sanitized_call)
        print(f"[{label}] XML fallback tool calls: {len(fallback_calls)}")
        calls.extend(fallback_calls)

    return calls


def summarize_tool_names(*tool_groups: list[dict]) -> list[str]:
    names: list[str] = []
    for group in tool_groups:
        names.extend(call["name"] for call in group)
    return names


def filter_tool_calls_for_pool(
    calls: list[dict],
    allowed_tool_names: set[str],
    label: str,
) -> list[dict]:
    filtered_calls: list[dict] = []
    for call in calls:
        fn_name = call["name"]
        if fn_name not in allowed_tool_names:
            print(f"[{label}][WARN] unexpected tool: {fn_name}")
            continue
        filtered_calls.append(call)
    return filtered_calls
