"""
dsh.cordis.types —— 客户端安全的线协议词汇（对应 TS 版 cordis-host-runner/types）。

Python 版为 dataclass + to_json；除 run/attempt 外多为收据/响应的 dict 形状
（与 TS 版字段名对齐，snake_case 转 camelCase 在 to_json 层完成以保持工具
结果与事件载荷的线形状一致）。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CordisHalfState:
    """一次激活尝试中一个平台半的状态。"""

    status: str = "absent"  # absent|pending|stopped|running|waiting|failed
    waiting_for: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.status,
                               "waitingFor": list(self.waiting_for)}
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass
class CordisRunDiagnostic:
    """绑定精确激活尝试的结构化失败。"""

    phase: str  # approval|host-load|host-apply|client-load|client-apply|client-render
    message: str
    plugin_id: str
    package_id: str
    plugin_run_id: str

    def to_json(self) -> Dict[str, Any]:
        return {"phase": self.phase, "message": self.message,
                "pluginId": self.plugin_id, "packageId": self.package_id,
                "pluginRunId": self.plugin_run_id}


@dataclass
class CordisRunAttempt:
    """独立于物理运行保留的最新激活尝试。"""

    plugin_run_id: str
    package_id: str
    mode: str  # run|update
    status: str = "awaiting-approval"
    approval_request_id: Optional[str] = None
    requires_approval: bool = False
    host: CordisHalfState = field(default_factory=CordisHalfState)
    client: CordisHalfState = field(default_factory=CordisHalfState)
    error: Optional[CordisRunDiagnostic] = None

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "pluginRunId": self.plugin_run_id,
            "packageId": self.package_id,
            "mode": self.mode,
            "status": self.status,
            "host": self.host.to_json(),
            "client": self.client.to_json(),
        }
        if self.approval_request_id is not None:
            out["approvalRequestId"] = self.approval_request_id
        if self.requires_approval:
            out["requiresApproval"] = True
        if self.error is not None:
            out["error"] = self.error.to_json()
        return out

    def clone(self) -> "CordisRunAttempt":
        return copy.deepcopy(self)


def define_receipt(plugin_id: str, package_id: str, name: str, purpose: str,
                   has_host: bool, has_client: bool) -> Dict[str, Any]:
    """cordis_define 的收据。"""
    return {"pluginId": plugin_id, "packageId": package_id, "name": name,
            "purpose": purpose, "hasHostHalf": has_host,
            "hasClientHalf": has_client}


def run_response_ok(status: str, plugin_id: str, package_id: str,
                    plugin_run_id: str, mode: str,
                    waiting_for: Optional[List[str]] = None,
                    current_package_id: Optional[str] = None,
                    next_package_id: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": True, "status": status, "pluginId": plugin_id,
                           "packageId": package_id,
                           "pluginRunId": plugin_run_id, "mode": mode,
                           "waitingFor": list(waiting_for or [])}
    if current_package_id is not None:
        out["currentPackageId"] = current_package_id
    if next_package_id is not None:
        out["nextPackageId"] = next_package_id
    return out


def run_response_fail(reason: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "message": message}


def stop_response_ok() -> Dict[str, Any]:
    return {"ok": True}


def stop_response_fail(reason: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "message": message}


def undefine_receipt(ok: bool, was_running: Optional[bool] = None,
                     reason: Optional[str] = None,
                     message: Optional[str] = None) -> Dict[str, Any]:
    if ok:
        return {"ok": True, "wasRunning": bool(was_running)}
    return {"ok": False, "reason": reason, "message": message}


def invoke_result_ok(value: Any) -> Dict[str, Any]:
    return {"ok": True, "value": value}


def invoke_result_fail(code: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "code": code, "message": message}


def inspect_resolution_ok(data: Any) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def inspect_resolution_fail(reason: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "message": message}
