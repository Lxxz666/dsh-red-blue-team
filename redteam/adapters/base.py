"""redteam.adapters.base —— TargetAdapter 抽象契约。

设计要点：
- 每次攻击独立会话（``reset()`` 隔离），防状态污染；
- 超时/重试内置，防把目标打挂；
- 副作用探测：攻击前/后抓取目标状态快照，供 Detector 判定数据是否被篡改。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import AdapterError
from ..models import CapabilityProbe, ConcreteSample, TargetResponse


@dataclass
class SideEffectSnapshot:
    """目标状态快照（副作用探测前后对比）。"""
    data: Dict[str, Any] = field(default_factory=dict)
    available: bool = False

    def delta(self, other: "SideEffectSnapshot") -> Dict[str, Any]:
        """比较两个快照，返回有变化的键（新值优先）。"""
        changes: Dict[str, Any] = {}
        keys = set(self.data) | set(other.data)
        for key in keys:
            before = self.data.get(key)
            after = other.data.get(key)
            if before != after:
                changes[key] = {"before": before, "after": after}
        return changes


class TargetAdapter(abc.ABC):
    """目标适配器抽象：统一对话/请求/探测/副作用探测/重置。"""

    kind: str = "abstract"

    def __init__(self, base_url: str, headers: Optional[Dict[str, str]] = None,
                 timeout_s: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout_s = timeout_s

    # ---- 核心能力 ----

    @abc.abstractmethod
    async def send(self, sample: ConcreteSample) -> TargetResponse:
        """发送一次攻击（对话或 HTTP 请求），返回原始响应。"""

    async def send_text(self, text: str, role: str = "customer",
                        session_id: Optional[str] = None) -> TargetResponse:
        """发送一条自由文本（基线消息 / 侦察用）。默认实现抛错，对话型适配器重写。"""
        raise AdapterError(f"{self.kind} 适配器不支持自由文本对话")

    @abc.abstractmethod
    async def probe(self) -> "CapabilityProbe":
        """侦察：探测目标可达性/能力/安全头。"""

    async def check_side_effect(self) -> SideEffectSnapshot:
        """副作用探测：抓取目标状态快照（默认不可用）。"""
        return SideEffectSnapshot(available=False)

    async def reset(self) -> None:
        """重置目标会话状态（隔离测试）。默认空操作。"""

    async def reload_guards(self) -> bool:
        """请求目标重载防护配置（蓝队修复后调用）。默认不支持。"""
        return False

    # ---- 生命周期 ----

    async def close(self) -> None:
        """释放连接等资源。"""

    async def __aenter__(self) -> "TargetAdapter":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
