"""
dsh.cordis.runner —— DynamicCordisRunnerService（ctx.dynamicCordisRunner）。

对应 TS 版 cordis-host-runner 的 host-only 子集：

- define：不可变 Package 定义（host/client 两半都可存储；client 半仅存与检查）；
- run：解析计划 → 审批门控（requires_approval 配置打开，经 ctx.approval +
  cordis/request-run(-resolved) 事件）→ host 半激活（作用域 fiber +
  PluginTree 挂载）→ cordis/dynamic-package；
- stop / undefine：回收运行（cordis/dynamic-retract）+ 保留/删除版本；
- list/inspect/snapshot/reference：无源码元数据视图（模型自省）；
- invoke：对活动 Host 方法做 Client RPC 等价调用（stale-run 拒绝）；
- 身份铸造：pluginId = <idPrefix>-<n>（每前缀计数）、packageId = dyn-<n>、
  runId = run-<n>、requestId = req-<n>。

文档化边界：无浏览器 client 运行时——带 client 代码的包 run 被拒
（client-half-failed）；host 半在进程内执行（与 run_code 同信任立场，
非容器；AST 级危险导入拒绝见 sandbox.py）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..errors import ToolError
from ..kernel import PluginTree, Service
from ..session.events import is_json_value
from .inspect import CordisInspectRegistryService
from .registry import DynamicCordisRegistry
from .sandbox import _TaggedConsole, evaluate_host_code, precheck_code
from .types import (CordisHalfState, CordisRunAttempt, CordisRunDiagnostic,
                    define_receipt, invoke_result_fail, invoke_result_ok,
                    run_response_fail, run_response_ok, stop_response_fail,
                    stop_response_ok, undefine_receipt)

log = logging.getLogger("dsh.cordis")


def _missing_plugin_message(plugin_id: str) -> str:
    return (f'dynamic plugin "{plugin_id}" does not exist or is not owned '
            "by this session")


class _Harness:
    """每运行的注册助手（host 代码里的 ``harness`` 全局）。"""

    def __init__(self, run_record: Dict[str, Any]) -> None:
        self._run = run_record

    def handle(self, method: str, handler: Any):
        """注册包私有 Host 方法（invoke 的调用面）。"""
        if not isinstance(method, str) or not method.strip():
            raise ToolError("harness.handle needs a non-empty method name")
        if method in self._run["handlers"]:
            raise ToolError(f"duplicate host method: {method}")
        self._run["handlers"][method] = handler

        def unregister() -> None:
            self._run["handlers"].pop(method, None)
        return unregister

    def defineTool(self, definition: Any) -> Any:
        """校验一个工具定义并原样返回（与 registerTool 配对）。"""
        from ..tools.definition import ToolDefinition
        if not isinstance(definition, ToolDefinition):
            raise ToolError("harness.defineTool expects a ToolDefinition "
                            "(build one with dsh.tools.define_tool)")
        return definition

    def registerTool(self, ctx: Any, tool: Any):
        """把动态工具注册进调用者的作用域 ctx（落在包自己的层）。"""
        return ctx.tools.register(self.defineTool(tool))


class DynamicCordisRunnerService(Service):
    """动态 Plugin 注册表与 host 半生命周期（ctx.dynamicCordisRunner）。"""

    provides = "dynamicCordisRunner"

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self.root_ctx = ctx
        self.vm_timeout_ms = int((config or {}).get("vm_timeout_ms", 5000))
        if self.vm_timeout_ms < 1:
            raise ToolError("vm_timeout_ms must be a positive integer")
        self.requires_approval = bool(
            (config or {}).get("requires_approval", False))
        self._registry = DynamicCordisRegistry()
        self.inspect_registry = CordisInspectRegistryService(ctx)
        self._starting: Dict[str, asyncio.Future] = {}
        self._retract_task: Optional[asyncio.Future] = None
        self._closed = False

    def apply(self, ctx) -> None:
        ctx.set("dynamicCordisRunner", self)
        if not ctx.has("cordisInspect"):
            self.inspect_registry.apply(ctx)

    # ---- 定义 / 删除 ----

    def define(self, request: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        """
        定义新 Plugin 的首个 Package 或给已有 Plugin 追加不可变 Package。

        :param request: {name, purpose, plugin: {kind, idPrefix?|pluginId?},
            code: {host?, client?}}。
        :raises ToolError: 校验失败（含源码预检失败）。
        """
        if self._closed:
            raise ToolError("dynamic cordis runner is closed")
        name = (request.get("name") or "").strip()
        purpose = (request.get("purpose") or "").strip()
        if not name:
            raise ToolError("cordis_define needs a non-empty `name`")
        if not purpose:
            raise ToolError("cordis_define needs a non-empty `purpose`")
        code = request.get("code") or {}
        host_code = code.get("host")
        client_code = code.get("client")
        if host_code is None and client_code is None:
            raise ToolError("cordis_define needs `code.host`, `code.client`, "
                            "or both")
        if host_code is not None:
            precheck_code(host_code, "code.host")
        if client_code is not None:
            precheck_code(client_code, "code.client")

        plugin_spec = request.get("plugin") or {}
        if plugin_spec.get("kind") == "new":
            import re
            prefix = (plugin_spec.get("idPrefix") or "").strip()
            if not re.fullmatch(r"[a-z]{3,6}", prefix):
                raise ToolError("cordis_define `plugin.idPrefix` must contain "
                                "3-6 lowercase English letters")
            plugin_id = self._registry.mint_plugin_id(prefix)
            plugin = {"plugin_id": plugin_id, "session_id": session_id,
                      "packages": {}, "approved_client_packages": set(),
                      "client_version_updates_approved": False,
                      "current_package_id": None, "next_package_id": None,
                      "run": None, "latest_run": None}
            self._registry.add(plugin)
        else:
            plugin_id = plugin_spec.get("pluginId")
            plugin = self._registry.get(plugin_id) if plugin_id else None
            if plugin is None or plugin.get("session_id") != session_id:
                raise ToolError(_missing_plugin_message(str(plugin_id)))
            plugin = self._registry.get(plugin_id)

        package_id = self._registry.mint_package_id()
        definition: Dict[str, Any] = {"package_id": package_id, "name": name,
                                      "purpose": purpose}
        if host_code is not None:
            definition["host_code"] = host_code
        if client_code is not None:
            definition["client_code"] = client_code
        plugin["packages"][package_id] = definition
        return define_receipt(plugin_id, package_id, name, purpose,
                              host_code is not None, client_code is not None)

    async def undefine(self, agent: Any, plugin_id: str) -> Dict[str, Any]:
        """删除 Plugin、其活动运行与全部 Package 版本。"""
        plugin = self._owned(agent, plugin_id)
        if plugin is None:
            return undefine_receipt(False, reason="plugin-missing",
                                    message=_missing_plugin_message(plugin_id))
        was_running = plugin["run"] is not None
        self._cancel_pending(plugin_id,
                             f'dynamic plugin "{plugin_id}" was removed '
                             "before approval")
        if plugin["run"] is not None:
            await self._retract(plugin)
        self._registry.delete(plugin_id)
        return undefine_receipt(True, was_running=was_running)

    # ---- 运行 ----

    async def run(self, agent: Any, plugin_id: str, package_id: str,
                  mode: str, signal: Any = None) -> Dict[str, Any]:
        """启动或切换一个 Package（审批门控 + host 半激活）。"""
        if self._closed:
            return run_response_fail("plugin-missing",
                                     "dynamic cordis runner is closed")
        plan = self._resolve_plan(agent, plugin_id, package_id, mode)
        if not plan["ok"]:
            return plan["response"]
        if signal is not None and signal.aborted:
            return run_response_fail(
                "cancelled",
                f'the run request for dynamic plugin "{plugin_id}" was '
                "cancelled before activation")
        if self._registry.pending_for(plugin_id) is not None:
            return run_response_fail(
                "transition-in-flight",
                f'dynamic plugin "{plugin_id}" already has a pending run '
                "request")
        plugin, definition, mode = (plan["plugin"], plan["definition"],
                                    plan["mode"])
        if definition.get("client_code") is not None:
            attempt = self._create_attempt(plugin, package_id, mode)
            plugin["next_package_id"] = package_id
            plugin["latest_run"] = attempt
            message = ("dsh_python has no browser client runtime: dynamic "
                       "packages with a client half are stored for inspection "
                       "but cannot run — move the capability into code.host "
                       "instead")
            self._fail_attempt(plugin, attempt, "client-load", message)
            return run_response_fail("client-half-failed", message)

        attempt = self._create_attempt(plugin, package_id, mode)
        plugin["next_package_id"] = package_id
        plugin["latest_run"] = attempt

        if self.requires_approval:
            request_id = self._registry.mint_request_id()
            attempt.approval_request_id = request_id
            attempt.requires_approval = True
            attempt.status = "awaiting-approval"
            self._registry.arm_request(request_id, {
                "request_id": request_id, "agent_id": agent.id,
                "plugin_id": plugin_id, "package_id": package_id,
                "plugin_run_id": attempt.plugin_run_id, "mode": mode,
                "requires_approval": True})
            self.ctx.events.emit("cordis/request-run", {
                "requestId": request_id, "agentId": agent.id,
                "pluginId": plugin_id, "packageId": package_id, "mode": mode,
                "name": definition["name"], "purpose": definition["purpose"],
                "requiresApproval": True})
            approved = False
            if self.ctx.has("approval"):
                approved = await self.ctx.approval.request(
                    f"允许运行动态插件 {plugin_id}（{definition['name']}）?",
                    detail=definition["purpose"])
            if attempt.status != "awaiting-approval":
                # stop/undefine/close 在审批挂起期间撤销了请求（已发 resolved）
                return run_response_fail(
                    "cancelled",
                    f'the run request for dynamic plugin "{plugin_id}" was '
                    "cancelled before approval")
            outcome = "approved" if approved else "rejected"
            self._registry.claim_request(request_id)
            self.ctx.events.emit("cordis/request-run-resolved",
                                 {"requestId": request_id,
                                  "outcome": outcome})
            if signal is not None and signal.aborted:
                self._fail_attempt(plugin, attempt, "approval",
                                   "cancelled before activation")
                return run_response_fail(
                    "cancelled",
                    f'the run request for dynamic plugin "{plugin_id}" was '
                    "cancelled before activation")
            if not approved:
                self._fail_attempt(plugin, attempt, "approval",
                                   "activation rejected by the user")
                return run_response_fail("rejected",
                                         "activation rejected by the user")
            attempt.status = "starting-host"

        started = await self._activate(plugin, definition, attempt)
        if not started["ok"]:
            self._fail_attempt(plugin, attempt, "host-load", started["message"])
            return run_response_fail("host-half-failed", started["message"])
        plugin["current_package_id"] = package_id
        plugin["next_package_id"] = None
        attempt.status = "running"
        attempt.host = CordisHalfState(status="running", waiting_for=[])
        self.ctx.events.emit("cordis/dynamic-package", {
            "pluginId": plugin_id, "packageId": package_id,
            "pluginRunId": attempt.plugin_run_id, "name": definition["name"]})
        return run_response_ok(
            "running", plugin_id, package_id, attempt.plugin_run_id, mode,
            current_package_id=package_id)

    async def stop(self, agent: Any, plugin_id: str) -> Dict[str, Any]:
        """停止活动运行（保留全部 Package 版本）。"""
        plugin = self._owned(agent, plugin_id)
        if plugin is None:
            return stop_response_fail("plugin-missing",
                                      _missing_plugin_message(plugin_id))
        if plugin["run"] is None and \
                self._registry.pending_for(plugin_id) is None:
            return stop_response_fail(
                "not-running",
                f'dynamic plugin "{plugin_id}" is not running')
        self._cancel_pending(plugin_id,
                             f'dynamic plugin "{plugin_id}" was stopped '
                             "before approval")
        if plugin["run"] is not None:
            await self._retract(plugin)
        latest = plugin["latest_run"]
        if latest is not None:
            latest.status = "stopped"
            if latest.host.status != "absent":
                latest.host = CordisHalfState(status="stopped", waiting_for=[])
            if latest.client.status != "absent":
                latest.client = CordisHalfState(status="stopped",
                                                waiting_for=[])
        return stop_response_ok()

    # ---- 自省 ----

    def list_plugins(self, agent: Any) -> List[Dict[str, Any]]:
        rows = []
        for plugin in self._registry.of_session(agent.id):
            reference = self.reference(agent, plugin["plugin_id"])
            if reference is not None:
                rows.append(reference)
        return rows

    def inspect_plugin(self, agent: Any, plugin_id: str) -> Dict[str, Any]:
        plugin = self._owned(agent, plugin_id)
        if plugin is None:
            raise ToolError(_missing_plugin_message(plugin_id))
        reference = self.reference(agent, plugin_id)
        if reference is None:
            raise ToolError(f'dynamic plugin "{plugin_id}" has no package')
        reference["packages"] = self._package_summaries(plugin)
        return reference

    def inspect_package(self, agent: Any, plugin_id: str,
                        package_id: str) -> Dict[str, Any]:
        plugin = self._owned(agent, plugin_id)
        if plugin is None:
            raise ToolError(_missing_plugin_message(plugin_id))
        definition = plugin["packages"].get(package_id)
        if definition is None:
            raise ToolError(f'dynamic package "{package_id}" does not exist '
                            f'on plugin "{plugin_id}"')
        out: Dict[str, Any] = {"pluginId": plugin_id, "packageId": package_id,
                               "name": definition["name"],
                               "purpose": definition["purpose"],
                               "code": {}}
        if definition.get("host_code") is not None:
            out["code"]["host"] = definition["host_code"]
        if definition.get("client_code") is not None:
            out["code"]["client"] = definition["client_code"]
        self._attach_pointers(out, plugin)
        return out

    def snapshot(self, agent: Any) -> List[Dict[str, Any]]:
        rows = []
        for plugin in self._registry.of_session(agent.id):
            row: Dict[str, Any] = {"pluginId": plugin["plugin_id"],
                                   "packages": self._package_summaries(plugin)}
            self._attach_pointers(row, plugin)
            rows.append(row)
        return rows

    def reference(self, agent: Any, plugin_id: str) -> Optional[Dict[str, Any]]:
        plugin = self._owned(agent, plugin_id)
        if plugin is None:
            return None
        package_id = (plugin["next_package_id"] or plugin["current_package_id"]
                      or (list(plugin["packages"].keys())[-1]
                          if plugin["packages"] else None))
        if package_id is None:
            return None
        definition = plugin["packages"][package_id]
        out: Dict[str, Any] = {"pluginId": plugin_id, "packageId": package_id,
                               "name": definition["name"],
                               "purpose": definition["purpose"]}
        self._attach_pointers(out, plugin)
        return out

    # ---- 调用 ----

    async def invoke(self, plugin_id: str, plugin_run_id: str, method: str,
                     args: Any) -> Dict[str, Any]:
        """调用活动 Host 方法（stale-run / 未知方法 / handler 失败结构化拒绝）。"""
        plugin = self._registry.get(plugin_id)
        if plugin is None or plugin["run"] is None:
            return invoke_result_fail(
                "plugin-not-running",
                f'dynamic plugin "{plugin_id}" is not running')
        run = plugin["run"]
        if run["plugin_run_id"] != plugin_run_id:
            return invoke_result_fail(
                "stale-run",
                f'activation "{plugin_run_id}" is no longer active')
        handler = run["handlers"].get(method)
        if handler is None:
            return invoke_result_fail(
                "method-not-found",
                f'dynamic plugin "{plugin_id}" registered no Host method '
                f'"{method}"')
        try:
            value = handler(args)
            if asyncio.iscoroutine(value):
                value = await value
        except Exception as exc:
            return invoke_result_fail(
                "handler-error", f"{type(exc).__name__}: {exc}")
        if not is_json_value(value):
            return invoke_result_fail(
                "handler-error",
                "handler result is not lossless JSON "
                f"({type(value).__name__})")
        return invoke_result_ok(value)

    # ---- 内部 ----

    def _owned(self, agent: Any, plugin_id: str) -> Optional[Dict[str, Any]]:
        plugin = self._registry.get(plugin_id)
        if plugin is None or plugin.get("session_id") != agent.id:
            return None
        return plugin

    def _resolve_plan(self, agent: Any, plugin_id: str, package_id: str,
                      mode: str) -> Dict[str, Any]:
        plugin = self._owned(agent, plugin_id)
        if plugin is None:
            return {"ok": False, "response": run_response_fail(
                "plugin-missing", _missing_plugin_message(plugin_id))}
        definition = plugin["packages"].get(package_id)
        if definition is None:
            return {"ok": False, "response": run_response_fail(
                "package-missing",
                f'plugin "{plugin_id}" has no package "{package_id}"')}
        if mode not in ("run", "update"):
            return {"ok": False, "response": run_response_fail(
                "invalid-mode", "mode must be 'run' or 'update'")}
        current = plugin["current_package_id"]
        if mode == "update" and (current is None or current == package_id):
            return {"ok": False, "response": run_response_fail(
                "invalid-mode",
                (f'plugin "{plugin_id}" has no successful version yet; '
                 f'start "{package_id}" with mode "run"'
                 if current is None else
                 f'package "{package_id}" is already current; '
                 'use mode "run"'))}
        if mode == "run" and current is not None and current != package_id:
            return {"ok": False, "response": run_response_fail(
                "invalid-mode",
                f'package "{package_id}" differs from current "{current}"; '
                'use mode "update"')}
        return {"ok": True, "plugin": plugin, "definition": definition,
                "mode": mode}

    def _create_attempt(self, plugin: Dict[str, Any], package_id: str,
                        mode: str) -> CordisRunAttempt:
        definition = plugin["packages"][package_id]
        attempt = CordisRunAttempt(
            plugin_run_id=self._registry.mint_run_id(),
            package_id=package_id, mode=mode, status="starting-host",
            host=CordisHalfState(status="pending", waiting_for=[])
            if definition.get("host_code") is not None
            else CordisHalfState())
        return attempt

    async def _activate(self, plugin: Dict[str, Any],
                        definition: Dict[str, Any],
                        attempt: CordisRunAttempt) -> Dict[str, Any]:
        plugin_id = plugin["plugin_id"]
        in_flight = self._starting.get(plugin_id)
        if in_flight is not None:
            return await in_flight
        future = asyncio.ensure_future(
            self._start_fresh(plugin, definition, attempt))
        self._starting[plugin_id] = future
        try:
            return await future
        finally:
            self._starting.pop(plugin_id, None)

    async def _start_fresh(self, plugin: Dict[str, Any],
                           definition: Dict[str, Any],
                           attempt: CordisRunAttempt) -> Dict[str, Any]:
        plugin_id = plugin["plugin_id"]
        # 替换语义：激活新版本前回收旧运行（旧工具的注册层随之清空，
        # 否则同名工具在新层上重复注册）
        if plugin["run"] is not None:
            await self._retract(plugin)
        scope = self.root_ctx.scoped(f"cordis:{plugin_id}")
        # 包私有工具层：注册落根注册表的 cordis:<id> 作用域层（对外不可见）
        if scope.has("tools") and "tools" not in scope._instances:
            from ..tools import ToolRuntime
            local = ToolRuntime(scope, {}, parent=scope.get("tools"))
            local.apply(scope)
        run_record: Dict[str, Any] = {
            "plugin_run_id": attempt.plugin_run_id,
            "package_id": definition["package_id"],
            "handlers": {}, "fiber": None, "scope": scope}
        harness = _Harness(run_record)
        console = _TaggedConsole(plugin_id)
        tree: Optional[PluginTree] = None
        try:
            plugin_obj = await evaluate_host_code(
                definition.get("host_code", ""), plugin_id,
                self.vm_timeout_ms, scope, harness, console)
            from ..kernel.service import is_service_class
            if not (callable(plugin_obj) or is_service_class(plugin_obj)
                    or isinstance(plugin_obj, Service)):
                raise ToolError(
                    "host half must return a plugin: a function "
                    "(ctx) -> disposer or a Service class")
            tree = PluginTree(scope)
            tree.add_bundle_rows(
                [{"id": f"cordis:{plugin_id}", "plugin": plugin_obj}],
                layer_name="cordis-dynamic")
            await tree.mount()
            run_record["fiber"] = tree
            plugin["run"] = run_record
            return {"ok": True, "plugin_run_id": attempt.plugin_run_id,
                    "waiting_for": []}
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if tree is not None:
                try:
                    await tree.dispose(dispose_ctx=False)
                except Exception:
                    pass
            try:
                await scope.dispose()
            except Exception:
                pass
            if plugin["run"] is run_record:
                plugin["run"] = None
            return {"ok": False, "message": message}

    async def _retract(self, plugin: Dict[str, Any]) -> None:
        run = plugin["run"]
        if run is None:
            return
        plugin["run"] = None
        tree = run.get("fiber")
        if tree is not None:
            try:
                await tree.dispose()
            except Exception:
                log.exception("dynamic plugin %s dispose failed",
                              plugin["plugin_id"])
        run["handlers"].clear()
        self.ctx.events.emit("cordis/dynamic-retract", {
            "pluginId": plugin["plugin_id"],
            "packageId": run["package_id"],
            "pluginRunId": run["plugin_run_id"]})

    def _cancel_pending(self, plugin_id: str, message: str) -> None:
        pending = self._registry.pending_for(plugin_id)
        if pending is None:
            return
        request_id = pending["request_id"]
        self._registry.claim_request(request_id)
        self.ctx.events.emit("cordis/request-run-resolved",
                             {"requestId": request_id, "outcome": "cancelled"})
        plugin = self._registry.get(plugin_id)
        if plugin is not None:
            latest = plugin["latest_run"]
            if latest is not None and latest.status == "awaiting-approval":
                latest.status = "cancelled"

    def _fail_attempt(self, plugin: Dict[str, Any], attempt: CordisRunAttempt,
                      phase: str, message: str) -> None:
        attempt.error = CordisRunDiagnostic(
            phase=phase, message=message, plugin_id=plugin["plugin_id"],
            package_id=attempt.package_id,
            plugin_run_id=attempt.plugin_run_id)
        attempt.status = "failed"
        if attempt.host.status not in ("absent",):
            attempt.host = CordisHalfState(status="failed", waiting_for=[],
                                           error=message)

    @staticmethod
    def _package_summaries(plugin: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [{"packageId": d["package_id"], "name": d["name"],
                 "purpose": d["purpose"],
                 "hasHostHalf": d.get("host_code") is not None,
                 "hasClientHalf": d.get("client_code") is not None}
                for d in plugin["packages"].values()]

    @staticmethod
    def _attach_pointers(out: Dict[str, Any],
                         plugin: Dict[str, Any]) -> None:
        if plugin["current_package_id"] is not None:
            out["currentPackageId"] = plugin["current_package_id"]
        if plugin["next_package_id"] is not None:
            out["nextPackageId"] = plugin["next_package_id"]
        run = plugin["run"]
        if run is not None:
            out["activeRun"] = {"pluginRunId": run["plugin_run_id"],
                                "packageId": run["package_id"],
                                "handlers": sorted(run["handlers"].keys())}
        if plugin["latest_run"] is not None:
            out["latestRun"] = DynamicCordisRegistry.clone_attempt(
                plugin["latest_run"])

    def close(self) -> None:
        """幂等关闭：取消挂起审批 + 回收全部活动运行 + 清空注册表。"""
        if self._closed:
            return
        self._closed = True
        for plugin in self._registry.all():
            self._cancel_pending(plugin["plugin_id"], "runner closed")
        pending_retracts = [self._retract(plugin)
                            for plugin in self._registry.all()
                            if plugin["run"] is not None]
        if pending_retracts:
            try:
                self._retract_task = asyncio.ensure_future(
                    self._drain_retracts(pending_retracts))
            except RuntimeError:
                # 无运行事件循环（如解释器收尾路径）：如实记录，不崩溃
                log.warning("dynamic cordis close without a running loop: "
                            "%d retracts deferred", len(pending_retracts))
                self._retract_task = None
        else:
            self._retract_task = None
        self._registry.close()

    async def _drain_retracts(self, pending: List[Any]) -> None:
        await asyncio.gather(*pending, return_exceptions=True)
