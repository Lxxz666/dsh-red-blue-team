"""
dsh.llm.deepseek —— DeepSeek 适配器（OpenAI 兼容 chat/completions，httpx 实现）。

凭据来源（与 TS 版一致）: 环境变量 ``DEEPSEEK_API_KEY``（必需）、
``DEEPSEEK_BASE_URL``（可选，默认 https://api.deepseek.com）。

流式: SSE（text/event-stream），逐行解析 ``data: {...}``，``data: [DONE]`` 收尾；
非 2xx → LlmFailure（code 取自响应），超时 → LlmTimeoutError。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from ..errors import LlmFailure, LlmTimeoutError
from .adapters import LlmAdapter, LlmRequest
from .messages import messages_to_openai
from .stream import StreamChunk

log = logging.getLogger("dsh.llm")

DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekAdapter(LlmAdapter):
    """DeepSeek API 的 OpenAI 兼容适配器。"""

    name = "deepseek"
    supports_reasoning = True

    def __init__(self, api_key: Optional[str] = None,
                 base_url: Optional[str] = None,
                 model: Optional[str] = None,
                 timeout: float = 300.0,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        """
        :param api_key: 密钥（默认读 DEEPSEEK_API_KEY）。
        :param base_url: API 基址（默认 DEEPSEEK_BASE_URL 或官方地址）。
        :param model: 默认模型（请求配置可覆盖）。
        :param timeout: 单次请求超时秒数。
        :param client: 外部注入的 httpx 客户端（测试用）。
        """
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.default_model = model
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout))
        return self._client

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def _payload(self, request: LlmRequest) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": request.config.model or self.default_model or "deepseek-chat",
            "messages": [],
            "stream": True,
        }
        if request.system:
            payload["messages"].append({"role": "system", "content": request.system})
        payload["messages"].extend(messages_to_openai(request.messages))
        if request.tools:
            payload["tools"] = request.tool_schemas()
        if request.config.max_tokens:
            payload["max_tokens"] = request.config.max_tokens
        if request.config.temperature is not None:
            payload["temperature"] = request.config.temperature
        return payload

    def _parse_line(self, line: str) -> Optional[StreamChunk]:
        """解析一行 SSE：data: {...} → StreamChunk；其余返回 None。"""
        line = line.strip()
        if not line.startswith("data:"):
            return None
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            return StreamChunk.finish("stop")
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None
        choices = obj.get("choices") or []
        if not choices:
            return None
        choice = choices[0]
        delta = choice.get("delta") or {}
        finish = choice.get("finish_reason")
        if finish:
            return StreamChunk.finish(finish)
        if delta.get("content"):
            return StreamChunk.text_delta(delta["content"])
        if delta.get("reasoning_content"):
            return StreamChunk.reasoning_delta(delta["reasoning_content"])
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            tc = tool_calls[0]
            fn = tc.get("function") or {}
            return StreamChunk.tool_call_chunk(
                index=int(tc.get("index", 0)), call_id=tc.get("id", ""),
                name=fn.get("name", ""), arguments=fn.get("arguments", ""))
        if obj.get("usage"):
            return StreamChunk.usage_chunk(obj["usage"])
        return None

    async def stream(self, request: LlmRequest) -> AsyncIterator[StreamChunk]:
        """
        流式请求。检查取消信号（abort 时中止消费）。

        :raises LlmFailure: 非 2xx 响应或协议错误。
        :raises LlmTimeoutError: 请求超时。
        """
        if not self.api_key:
            raise LlmFailure("DEEPSEEK_API_KEY is not set", code="NO_API_KEY",
                             provider=self.name)
        url = self._endpoint()
        payload = self._payload(request)
        saw_finish = False
        try:
            async with self.client.stream("POST", url, json=payload,
                                          headers=self._headers()) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise LlmFailure(
                        f"DeepSeek API {response.status_code}: {body[:500]}",
                        code=f"HTTP_{response.status_code}", provider=self.name)
                async for raw_line in response.aiter_lines():
                    if request.signal is not None and request.signal.aborted:
                        break
                    if not raw_line.strip():
                        continue
                    chunk = self._parse_line(raw_line)
                    if chunk is not None:
                        if chunk.type == "finish":
                            saw_finish = True
                        yield chunk
        except LlmFailure:
            raise
        except httpx.TimeoutException as exc:
            raise LlmTimeoutError(provider=self.name, timeout=self.timeout) from exc
        except httpx.HTTPError as exc:
            raise LlmFailure(f"DeepSeek request failed: {exc}", code="HTTP_ERROR",
                             provider=self.name) from exc
        if not saw_finish:
            yield StreamChunk.finish("stop")

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
