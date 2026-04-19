"""
記憶服務：user_profile 更新邏輯（Use Case 層）。
"""
from infrastructure.memory_store import load_user_profile, save_user_profile


def execute_profile_update(action: str, field: str, value: str) -> dict:
    """執行 user_profile 的更新操作"""
    profile = load_user_profile()

    # 陣列型欄位
    list_fields = ["core_traits", "dislikes", "recent_interests", "custom_notes"]
    # 字串型欄位
    str_fields = ["communication_style"]

    if field in list_fields:
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
