"""
dsh.cordis.registry —— 动态 Plugin 注册表（无 ctx 的纯状态容器）。

对应 TS 版 registry.ts：

- 每 Plugin 一个稳定身份（session 拥有）+ 定义序的不可变 Package 版本；
- 生命周期指针：currentPackageId（最后成功激活）/ nextPackageId（失败或
  在途转换目标）/ run（当前物理运行）/ latestRun（最新激活尝试，含审批
  挂起与诊断）；
- 挂起请求（arm/peek/claim/pending_for）：模型驱动的激活在审批期间占用
  Plugin 的转换槽（transition-in-flight 拒绝后到者）。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from .types import CordisRunAttempt


class DynamicCordisRegistry:
    """进程内动态 Plugin 状态（不可变包 + 单活动运行 + 挂起请求）。"""

    def __init__(self) -> None:
        self._plugins: Dict[str, Dict[str, Any]] = {}
        self._requests: Dict[str, Dict[str, Any]] = {}
        self._plugin_counters: Dict[str, int] = {}
        self._package_counter = 0
        self._run_counter = 0
        self._request_counter = 0

    # ---- 身份铸造 ----

    def mint_plugin_id(self, prefix: str) -> str:
        self._plugin_counters[prefix] = self._plugin_counters.get(prefix, 0) + 1
        return f"{prefix}-{self._plugin_counters[prefix]}"

    def mint_package_id(self) -> str:
        self._package_counter += 1
        return f"dyn-{self._package_counter}"

    def mint_run_id(self) -> str:
        self._run_counter += 1
        return f"run-{self._run_counter}"

    def mint_request_id(self) -> str:
        self._request_counter += 1
        return f"req-{self._request_counter}"

    # ---- Plugin ----

    def add(self, plugin: Dict[str, Any]) -> None:
        self._plugins[plugin["plugin_id"]] = plugin

    def get(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        return self._plugins.get(plugin_id)

    def delete(self, plugin_id: str) -> None:
        self._plugins.pop(plugin_id, None)

    def all(self) -> List[Dict[str, Any]]:
        return list(self._plugins.values())

    def of_session(self, session_id: str) -> List[Dict[str, Any]]:
        return [p for p in self._plugins.values()
                if p.get("session_id") == session_id]

    # ---- 挂起请求 ----

    def arm_request(self, request_id: str, record: Dict[str, Any]) -> None:
        self._requests[request_id] = record

    def peek_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self._requests.get(request_id)

    def claim_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self._requests.pop(request_id, None)

    def pending_for(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        for record in self._requests.values():
            if record.get("plugin_id") == plugin_id:
                return record
        return None

    def clear_requests(self, plugin_id: str) -> None:
        for request_id in [rid for rid, record in self._requests.items()
                           if record.get("plugin_id") == plugin_id]:
            self._requests.pop(request_id, None)

    # ---- 快照 ----

    @staticmethod
    def clone_attempt(attempt: Optional[CordisRunAttempt]
                      ) -> Optional[Dict[str, Any]]:
        return attempt.clone().to_json() if attempt is not None else None

    def close(self) -> None:
        self._plugins.clear()
        self._requests.clear()
