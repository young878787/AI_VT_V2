"""兩種記憶角色共用有界傳輸，不共用語意決策或工具權限。"""
import json
import time

import tiktoken

from openai import AsyncOpenAI, BadRequestError

from core.ai_request_params import no_thinking_extra_body, provider_from_url
from core.prompt_logger import trace


class MemoryAgentClient:
    def __init__(self, settings, role):
        self.client = AsyncOpenAI(base_url=settings.memory_base_url, api_key=settings.memory_api_key,
                                  timeout=35, max_retries=0)
        self.model = settings.memory_model
        self.provider = provider_from_url(settings.memory_base_url)
        self.role = role

    async def call(self, prompt, payload, tools):
        content = json.dumps(payload, ensure_ascii=False, default=str)
        input_tokens = len(tiktoken.get_encoding("cl100k_base").encode(prompt + content + json.dumps(tools)))
        if len(content) > 28000 or input_tokens > 8000:
            raise ValueError("Memory agent input budget exceeded")
        started = time.monotonic()
        request = dict(model=self.model, messages=[{"role": "system", "content": prompt},
                       {"role": "user", "content": content}], tools=tools,
                       tool_choice="required", max_completion_tokens=4000)
        extra_body = no_thinking_extra_body(self.provider)
        if extra_body:
            request["extra_body"] = extra_body
        try:
            response = await self.client.chat.completions.create(**request)
        except BadRequestError as error:
            if (getattr(error, "param", None) != "reasoning_effort"
                    or "set reasoning_effort to 'none'" not in str(error)):
                raise
            response = await self.client.chat.completions.create(**request, reasoning_effort="none")
        calls = response.choices[0].message.tool_calls if response.choices else None
        if not calls or len(calls) > 12:
            raise ValueError("Memory agent 缺少有界工具結果")
        allowed = {item["function"]["name"] for item in tools}
        results = []
        for call in calls:
            if call.function.name not in allowed or len(call.function.arguments) > 16000:
                raise ValueError("Memory agent 越權或超過輸出預算")
            results.append((call.function.name, json.loads(call.function.arguments)))
        diagnostic = {"role": self.role, "model": self.model,
                      "latency_ms": round((time.monotonic() - started) * 1000),
                      "input_token_estimate": input_tokens,
                      "tokens": response.usage.total_tokens if response.usage else None,
                      "actual_model": getattr(response, "model", None),
                      "usage": response.usage.model_dump() if response.usage else None,
                      "finish_reason": getattr(response.choices[0], "finish_reason", None) if response.choices else None,
                      "tools": [name for name, _ in results],
                      "tool_results": [{"name": name, "arguments": args} for name, args in results]}
        trace("memory_agent", diagnostic)
        return results, diagnostic
