import json
import re


_LEADING_ZERO_DECIMAL_PATTERN = re.compile(
    r'(?P<prefix>(?:[:\[,]\s*))(?P<sign>-?)0+(?P<body>\d+\.\d+)'
)


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
    return normalized, was_normalized


def parse_tool_call_arguments(raw_args: str) -> tuple[dict, bool]:
    try:
        return json.loads(raw_args), False
    except json.JSONDecodeError:
        normalized, was_normalized = normalize_tool_call_arguments(raw_args)
        if not was_normalized:
            raise
        return json.loads(normalized), True
