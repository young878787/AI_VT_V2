"""OpenAI 相容端點的純請求參數規則。"""
from urllib.parse import urlparse


def provider_from_url(base_url: str) -> str:
    """辨識已知端點需要的特殊請求參數；自訂端點採用 vLLM 相容格式。"""
    host = (urlparse(base_url).hostname or "").lower()
    if host == "integrate.api.nvidia.com":
        return "nvidia"
    if host in {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    } or host.endswith(".dashscope.aliyuncs.com"):
        return "qwen"
    if host == "openrouter.ai":
        return "openrouter"
    if host == "generativelanguage.googleapis.com":
        return "google"
    if host == "api.openai.com":
        return "openai"
    return "custom"


def no_thinking_extra_body(provider: str) -> dict:
    if provider in {"custom", "nvidia"}:
        return {"chat_template_kwargs": {"enable_thinking": False}}
    if provider == "qwen":
        return {"enable_thinking": False}
    return {}
