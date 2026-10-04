"""單一記憶 agent 的有界傳輸；只回傳工具與彙總診斷。"""
import asyncio
import json
import time

import tiktoken
from openai import AsyncOpenAI, BadRequestError
from core.ai_request_params import no_thinking_extra_body, provider_from_url

CALL_TIMEOUT_SEC = 35
CUMULATIVE_DIAGNOSTIC_FIELDS = (
    "calls", "input_tokens", "output_tokens", "latency_ms", "input_token_estimate",
    "embedding_calls", "logical_steps", "search_calls", "read_calls", "corrections",
    "replacements", "duplicate_proposals", "timeouts", "failures",
)


class MemoryAgentClient:
    def __init__(self, settings):
        self.client = AsyncOpenAI(base_url=settings.memory_base_url, api_key=settings.memory_api_key,
                                  timeout=CALL_TIMEOUT_SEC, max_retries=0)
        self.model = settings.memory_model
        self.provider = provider_from_url(settings.memory_base_url)

    async def call(self, messages, tools, diagnostic):
        input_tokens = len(tiktoken.get_encoding("cl100k_base").encode(
            json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, default=str)))
        if input_tokens > 8000:
            raise ValueError("Memory agent input budget exceeded")
        request = dict(model=self.model, messages=messages, tools=tools, tool_choice="required",
                       parallel_tool_calls=False, max_completion_tokens=1024)
        extra_body = no_thinking_extra_body(self.provider)
        if extra_body:
            request["extra_body"] = extra_body
        started = time.monotonic()
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SEC):
                diagnostic["calls"] += 1
                diagnostic["input_token_estimate"] += input_tokens
                try:
                    response = await self.client.chat.completions.create(**request)
                except BadRequestError as error:
                    if (getattr(error, "param", None) != "reasoning_effort"
                            or "set reasoning_effort to 'none'" not in str(error)):
                        raise
                    diagnostic["calls"] += 1
                    diagnostic["input_token_estimate"] += input_tokens
                    response = await self.client.chat.completions.create(**request, reasoning_effort="none")
        finally:
            diagnostic["latency_ms"] += round((time.monotonic() - started) * 1000)
        if response.usage:
            diagnostic["input_tokens"] += response.usage.prompt_tokens
            diagnostic["output_tokens"] += response.usage.completion_tokens
        if not response.choices or response.choices[0].finish_reason == "length":
            raise ValueError("Memory agent output incomplete")
        message = response.choices[0].message
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": call.id, "type": "function", "function": {
                "name": call.function.name, "arguments": call.function.arguments}}
            for call in (message.tool_calls or [])]}
