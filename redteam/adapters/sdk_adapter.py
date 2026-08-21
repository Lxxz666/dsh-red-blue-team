"""redteam.adapters.sdk_adapter —— SDK 直连适配器（内置靶场用）。

绕过 HTTP 直接调用目标的可调用对象（如靶场 agent 的 respond 函数），
用于零网络开销的回归测试与基准评测。协议与 HttpAdapter 一致。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..models import CapabilityProbe, ConcreteSample, TargetResponse
from .base import SideEffectSnapshot, TargetAdapter


class SdkAdapter(TargetAdapter):
    """直连可调用目标的适配器。

    :param handle: 可调用对象 ``handle(kind, **payload) -> dict``，其中
        kind ∈ {"chat", "api", "state", "probe", "reload"}；返回 dict 含
        status/text/headers/json 等字段。由 target_lab 提供工厂函数构造。
    """

    kind = "sdk"

    def __init__(self, base_url: str, handle: Callable[..., Dict[str, Any]],
                 headers: Optional[Dict[str, str]] = None,
                 timeout_s: float = 15.0,
                 side_effect_token: str = "") -> None:
        super().__init__(base_url, headers, timeout_s)
        self.handle = handle
        self.side_effect_token = side_effect_token

    def _call(self, kind: str, **payload: Any) -> TargetResponse:
        raw = self.handle(kind, **payload) or {}
        return TargetResponse(
            status=int(raw.get("status", 0)),
            text=str(raw.get("text", "")),
            headers={str(k).lower(): v for k, v in (raw.get("headers") or {}).items()},
            json=raw.get("json"),
            elapsed=float(raw.get("elapsed", 0.0)),
            meta=dict(raw.get("meta") or {}))

    async def send(self, sample: ConcreteSample) -> TargetResponse:
        if sample.surface == "chat":
            return self._call("chat", messages=[{"role": "user",
                                                 "content": sample.payload}],
                              role=sample.role)
        return self._call("api", method=sample.sample.method or "GET",
                          path=sample.path, params=sample.params,
                          body=sample.body, payload=sample.payload)

    async def send_text(self, text: str, role: str = "customer",
                        session_id: Optional[str] = None) -> TargetResponse:
        return self._call("chat", messages=[{"role": "user", "content": text}],
                          role=role, session_id=session_id)

    async def probe(self) -> CapabilityProbe:
        raw = self.handle("probe") or {}
        if "probe" in raw:
            raw = raw["probe"]
        probe = CapabilityProbe()
        probe.reachable = bool(raw.get("reachable", True))
        probe.chat_ok = bool(raw.get("chat_ok", True))
        probe.side_effect_check_ok = bool(raw.get("side_effect_check_ok", False))
        probe.security_headers = dict(raw.get("security_headers") or {})
        probe.banner = str(raw.get("banner", "target-lab-sdk"))
        probe.scenarios = [str(s) for s in raw.get("scenarios") or []]
        probe.notes = [str(n) for n in raw.get("notes") or []]
        return probe

    async def check_side_effect(self) -> SideEffectSnapshot:
        raw = self.handle("state", token=self.side_effect_token) or {}
        if raw.get("available"):
            return SideEffectSnapshot(data=dict(raw.get("data") or {}),
                                      available=True)
        return SideEffectSnapshot(available=False)

    async def reload_guards(self) -> bool:
        raw = self.handle("reload") or {}
        return bool(raw.get("ok", False))

    async def reset(self) -> None:
        self.handle("reset")
