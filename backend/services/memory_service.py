"""
記憶服務：user_profile 更新邏輯（Use Case 層）。
"""
from domain.tools.schema_loader import load_schema
from infrastructure.memory_store import load_user_profile, save_user_profile


def _get_allowed_profile_field_types(model_name: str) -> tuple[set[str], set[str]]:
    list_fields = {"core_traits", "dislikes", "recent_interests", "custom_notes"}
    str_fields = {"communication_style"}

    schema = load_schema(model_name)
    for tool in schema.get("openai_tools", {}).get("memory", []):
        function_def = tool.get("function", {})
        if function_def.get("name") != "update_user_profile":
            continue

        parameters = function_def.get("parameters", {})
        enum_values = parameters.get("properties", {}).get("field", {}).get("enum")
        if not isinstance(enum_values, list):
            return list_fields, str_fields

        field_shapes: dict[str, str] = {}
        for item in (
            schema.get("prompt_config", {})
            .get("memory", {})
            .get("update_user_profile", {})
            .get("field_guide", [])
        ):
            if not isinstance(item, dict):
                continue
            field_name = item.get("field")
            value_shape = item.get("value_shape")
            if isinstance(field_name, str) and isinstance(value_shape, str):
                field_shapes[field_name] = value_shape

        for field_name in enum_values:
            if not isinstance(field_name, str):
                continue
            if field_name in str_fields:
                continue
            if field_shapes.get(field_name) == "string":
                str_fields.add(field_name)
            else:
                list_fields.add(field_name)
        return list_fields, str_fields

    return list_fields, str_fields


def execute_profile_update(action: str, field: str, value: str, model_name: str = "Hiyori") -> dict:
    """執行 user_profile 的更新操作"""
    profile = load_user_profile()

    list_fields, str_fields = _get_allowed_profile_field_types(model_name)

    if field in list_fields:
        if not isinstance(profile.get(field), list):
            profile[field] = []
        if action == "add":
            if value not in profile[field]:
                profile[field].append(value)
        elif action == "remove":
            profile[field] = [item for item in profile[field] if item != value]
        elif action == "update":
            # update 對陣列型欄位視為 add
            if value not in profile[field]:
                profile[field].append(value)
    elif field in str_fields:
        profile[field] = value

    save_user_profile(profile)
    return profile
