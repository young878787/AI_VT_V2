import json
import re

from domain.tools.schema_loader import load_schema


_LEADING_ZERO_DECIMAL_PATTERN = re.compile(
    r'(?P<prefix>(?:[:\[,]\s*))(?P<sign>-?)0+(?P<body>\d+\.\d+)'
)
_PYTHON_LITERAL_PATTERN = re.compile(
    r'(?P<prefix>(?:[:\[,]\s*))(?P<value>True|False|None)(?=\s*(?:[,}\]]))'
)
_TRAILING_COMMA_PATTERN = re.compile(r',\s*(?P<closer>[}\]])')


def normalize_tool_call_arguments(raw_args: str) -> tuple[str, bool]:
    if not raw_args:
        return raw_args, False

    was_normalized = False

    def _replace(match: re.Match) -> str:
        nonlocal was_normalized
        was_normalized = True
        prefix = match.group("prefix")
        sign = match.group("sign")
        body = match.group("body")
        if body.startswith("0."):
            normalized_number = f"{sign}{body}"
        else:
            normalized_number = f"{sign}{body.lstrip('0') or '0'}"
        return f"{prefix}{normalized_number}"

    normalized = _LEADING_ZERO_DECIMAL_PATTERN.sub(_replace, raw_args)

    def _replace_python_literal(match: re.Match) -> str:
        nonlocal was_normalized
        was_normalized = True
        prefix = match.group("prefix")
        value = match.group("value")
        replacements = {
            "True": "true",
            "False": "false",
            "None": "null",
        }
        return f"{prefix}{replacements[value]}"

    normalized = _PYTHON_LITERAL_PATTERN.sub(_replace_python_literal, normalized)

    def _replace_trailing_comma(match: re.Match) -> str:
        nonlocal was_normalized
        was_normalized = True
        return match.group("closer")

    normalized = _TRAILING_COMMA_PATTERN.sub(_replace_trailing_comma, normalized)
    return normalized, was_normalized


def _get_tool_properties(tool_name: str, model_name: str) -> dict[str, dict]:
    schema = load_schema(model_name)
    for tool_group in schema.get("openai_tools", {}).values():
        for tool in tool_group:
            function_def = tool.get("function", {})
            if function_def.get("name") == tool_name:
                return function_def.get("parameters", {}).get("properties", {})
    return {}


def _extract_string_value(source: str, key: str) -> str | None:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"')
    match = pattern.search(source)
    if not match:
        return None
    return json.loads(f'"{match.group(1)}"')


def _extract_number_value(source: str, key: str) -> float | None:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*(-?(?:\d+(?:\.\d+)?|\.\d+))')
    match = pattern.search(source)
    if not match:
        return None
    return float(match.group(1))


def _extract_boolean_value(source: str, key: str) -> bool | None:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*(true|false|True|False)')
    match = pattern.search(source)
    if not match:
        return None
    return match.group(1).lower() == "true"


def _salvage_tool_call_arguments(
    raw_args: str,
    tool_name: str,
    model_name: str,
) -> dict:
    properties = _get_tool_properties(tool_name, model_name)
    if not properties:
        return {}

    salvaged: dict = {}
    for key, property_def in properties.items():
        prop_type = property_def.get("type")
        value = None

        if prop_type == "number":
            value = _extract_number_value(raw_args, key)
        elif prop_type == "boolean":
            value = _extract_boolean_value(raw_args, key)
        elif prop_type == "string":
            value = _extract_string_value(raw_args, key)

        if value is not None:
            salvaged[key] = value

    return salvaged


def parse_tool_call_arguments(
    raw_args: str,
    tool_name: str | None = None,
    model_name: str = "Hiyori",
) -> tuple[dict, bool]:
    try:
        return json.loads(raw_args), False
    except json.JSONDecodeError as original_error:
        normalized, was_normalized = normalize_tool_call_arguments(raw_args)
        try:
            return json.loads(normalized), was_normalized
        except json.JSONDecodeError:
            if not tool_name:
                raise original_error

            salvaged = _salvage_tool_call_arguments(normalized, tool_name, model_name)
            if salvaged:
                return salvaged, True

            raise original_error
