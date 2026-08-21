"""redteam.adapters.http_adapter —— HTTP/SSE 型目标适配器。

适用于暴露 REST 对话接口或 API 的 agent 业务系统：
- 对话型样本 → POST chat_path（多轮 messages + 角色上下文）；
- API 型样本 → 任意 method/path/params/body（参数已渲染）；
- 副作用探测 → GET side_effect_path（带扫描器令牌）；
- 蓝队修复后 → POST /api/_admin/reload 重载目标防护配置。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx

from ..errors import TargetUnavailable
from ..models import ConcreteSample, TargetResponse
from .base import SideEffectSnapshot, TargetAdapter

log = logging.getLogger("redteam.adapters.http")


class HttpAdapter(TargetAdapter):
    kind = "http"

    def __init__(self, base_url: str, headers: Optional[Dict[str, str]] = None,
                 timeout_s: float = 15.0, chat_path: str = "/api/chat",
                 side_effect_path: str = "/api/state",
                 side_effect_token: str = "",
                 admin_token: str = "") -> None:
        super().__init__(base_url, headers, timeout_s)
        self.chat_path = chat_path
        self.side_effect_path = side_effect_path
        self.side_effect_token = side_effect_token
        self.admin_token = admin_token
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, headers=self.headers,
                timeout=self.timeout_s, follow_redirects=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- 攻击发送 ----

    async def send(self, sample: ConcreteSample) -> TargetResponse:
        client = await self._ensure_client()
        if sample.surface == "chat":
            return await self._send_chat(client, sample)
        return await self._send_request(client, sample)

    async def _send_chat(self, client: httpx.AsyncClient,
                         sample: ConcreteSample) -> TargetResponse:
        body: Dict[str, Any] = {
            "messages": [{"role": "user", "content": sample.payload}],
            "role": sample.role,
        }
        response = await client.post(self.chat_path, json=body)
        return self._wrap(response)

    async def _send_request(self, client: httpx.AsyncClient,
                            sample: ConcreteSample) -> TargetResponse:
        kwargs: Dict[str, Any] = {"params": sample.params or None}
        # 角色上下文约定：以 x-role 头传递（靶场按角色实施授权判定）
        request_headers = {"x-role": sample.role}
        request_headers.update(sample.sample.headers)
        kwargs["headers"] = request_headers
        if sample.surface == "api":
            body_payload: Dict[str, Any] = {}
            if sample.body:
                body_payload = {k: v for k, v in sample.body.items()}
                body_payload = _jsonable(body_payload)
            if body_payload:
                # httpx 约定：json 与 content 互斥（同时给出时 json 被静默丢弃）
                kwargs["json"] = body_payload
            elif sample.payload and sample.sample.method.upper() in (
                    "POST", "PUT", "PATCH"):
                kwargs["content"] = sample.payload.encode("utf-8")
        response = await client.request(sample.sample.method or "GET",
                                        sample.path, **kwargs)
        return self._wrap(response)

    def _wrap(self, response: httpx.Response) -> TargetResponse:
        text = response.text or ""
        try:
            data = response.json() if text.lstrip().startswith(("{", "[")) else None
        except (json.JSONDecodeError, ValueError):
            data = None
        return TargetResponse(
            status=response.status_code, text=text,
            headers={k.lower(): v for k, v in response.headers.items()},
            json=data, elapsed=response.elapsed.total_seconds(),
            meta={"url": str(response.url)})

    # ---- 自由文本 / 侦察 / 副作用 ----

    async def send_text(self, text: str, role: str = "customer",
                        session_id: Optional[str] = None) -> TargetResponse:
        client = await self._ensure_client()
        body: Dict[str, Any] = {
            "messages": [{"role": "user", "content": text}],
            "role": role,
        }
        if session_id:
            body["session_id"] = session_id
        response = await client.post(self.chat_path, json=body)
        return self._wrap(response)

    async def probe(self) -> "CapabilityProbe":
        from ..models import CapabilityProbe
        probe = CapabilityProbe()
        client = await self._ensure_client()
        try:
            response = await client.get("/api/health")
            probe.reachable = True
            if response.is_error:
                probe.notes.append(f"health 返回 {response.status_code}")
            probe.banner = response.headers.get("server", "")
        except httpx.HTTPError as exc:
            probe.notes.append(f"health 探测失败: {exc}")
            return probe
        # 对话能力
        try:
            chat = await self.send_text("你好，请介绍一下你自己。", role="customer")
            if chat.status < 500:
                probe.chat_ok = True
            else:
                probe.notes.append(f"chat 探测失败: HTTP {chat.status}")
        except httpx.HTTPError as exc:
            probe.notes.append(f"chat 探测失败: {exc}")
        # 副作用探测能力
        snapshot = await self.check_side_effect()
        probe.side_effect_check_ok = snapshot.available
        # 安全响应头（D7 配置检查基线）
        for name in ("content-security-policy", "strict-transport-security",
                     "x-content-type-options", "x-frame-options",
                     "referrer-policy", "permissions-policy"):
            probe.security_headers[name] = response.headers.get(name)
        # 业务场景指纹（业务元信息端点）
        try:
            meta = await client.get("/api/meta/business")
            if meta.status_code == 200 and meta.json():
                data = meta.json()
                probe.scenarios = [str(s) for s in
                                   data.get("scenarios") or []]
                if data.get("business"):
                    probe.notes.append(f"业务面: {data['business']}")
        except (httpx.HTTPError, ValueError):
            pass
        return probe

    async def check_side_effect(self) -> SideEffectSnapshot:
        client = await self._ensure_client()
        headers = {}
        if self.side_effect_token:
            headers["x-scanner-token"] = self.side_effect_token
        try:
            response = await client.get(self.side_effect_path, headers=headers)
        except httpx.HTTPError as exc:
            log.debug("side effect check unavailable: %s", exc)
            return SideEffectSnapshot(available=False)
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            return SideEffectSnapshot(available=False)
        if response.status_code != 200 or not isinstance(data, dict):
            return SideEffectSnapshot(available=False)
        if isinstance(data, dict) and data.get("available"):
            return SideEffectSnapshot(data=dict(data.get("data") or {}),
                                      available=True)
        return SideEffectSnapshot(available=False)

    async def reload_guards(self) -> bool:
        client = await self._ensure_client()
        headers = {"x-admin-token": self.admin_token} if self.admin_token else {}
        try:
            response = await client.post("/api/_admin/reload", headers=headers)
            return response.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("guard reload failed: %s", exc)
            return False

    async def reset(self) -> None:
        """重置目标会话/数据状态（靶场专用；外部目标默认不重置）。"""
        if not self.admin_token:
            return
        client = await self._ensure_client()
        try:
            await client.post("/api/_admin/reset",
                              headers={"x-admin-token": self.admin_token})
        except httpx.HTTPError as exc:
            log.debug("target reset failed (ignored): %s", exc)


def _jsonable(obj: Dict[str, Any]) -> Dict[str, Any]:
    """请求体中的字符串模板保持字符串（渲染已在上游完成）。"""
    return {k: (v if isinstance(v, (dict, list, int, float, bool, type(None)))
                else str(v)) for k, v in obj.items()}
