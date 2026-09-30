"""
AI 客戶端：Chat 的 OpenAI 相容客戶端初始化、extra_body 組裝與後備模型呼叫包裝。
"""
from openai import AsyncOpenAI, BadRequestError

from core.config import FALLBACK_MODEL, CHAT_PROVIDER, CHAT_API_KEY, CHAT_BASE_URL

# Chat 使用指定的 OpenAI 相容端點；長期記憶由 MemoryLLM 經 MemorySettings 自行管理。
_role_clients = {
    "chat": AsyncOpenAI(base_url=CHAT_BASE_URL, api_key=CHAT_API_KEY),
}


def no_thinking_extra_body(provider: str) -> dict:
    if provider == "nvidia":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    if provider == "qwen":
        return {"enable_thinking": False}
    return {}


async def chat_create_with_fallback(**kwargs) -> object:
    """
    包裝 client.chat.completions.create()。
    若 Chat 主模型呼叫失敗且 provider 有後備模型（FALLBACK_MODEL），
    自動切換 model= 重試一次。
    """
    role = kwargs.pop("role")
    target = _role_clients[role]
    role_provider = CHAT_PROVIDER
    request_kwargs = dict(kwargs)
    if role_provider == "openai" and "max_tokens" in request_kwargs:
        request_kwargs.setdefault("max_completion_tokens", request_kwargs.pop("max_tokens"))
    try:
        for _ in range(3):
            try:
                return await target.chat.completions.create(**request_kwargs)
            except BadRequestError as error:
                if role_provider != "openai":
                    raise
                param = getattr(error, "param", None)
                if param == "temperature" and "temperature" in request_kwargs:
                    # Some OpenAI models only support the default temperature.
                    request_kwargs.pop("temperature")
                    continue
                if (
                    param == "reasoning_effort"
                    and "tools" in request_kwargs
                    and "set reasoning_effort to 'none'" in str(error)
                ):
                    request_kwargs["reasoning_effort"] = "none"
                    continue
                raise
        raise RuntimeError("OpenAI 相容參數調整後仍無法送出請求")
    except Exception as e:
        if role_provider == "qwen" and FALLBACK_MODEL and kwargs.get("model") != FALLBACK_MODEL:
            print(f"[Fallback] 主模型失敗 ({e})，切換至後備模型: {FALLBACK_MODEL}")
            fallback_kwargs = {**request_kwargs, "model": FALLBACK_MODEL}
            return await target.chat.completions.create(**fallback_kwargs)
        raise
