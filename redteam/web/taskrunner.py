"""redteam.web.taskrunner —— 任务管线（步骤追踪 + 实时日志）。

每个扫描任务是一条可追踪的管线：
    URL 目标:   侦察 → 红队攻击(确定性引擎+可选LLM自主) → 蓝队修复 → 回归 → 完成
    上传项目:   解析文件 → 静态扫描 → 修复方案 → 完成

追踪能力：
- steps：每个阶段的状态（pending/running/finished/failed/skipped）+ 起止时间 + 摘要；
- logs：实时日志行（订阅 dsh 事件总线：攻击执行/判定/漏洞/子代理派发…）；
- 持久化：每任务 JSONL 日志文件（runtime_dir/tasks/<id>.jsonl），可回放。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
import zipfile
from typing import Any, Dict, List, Optional

from ..config import ScanConfig

log = logging.getLogger("redteam.web.taskrunner")

_TASK_EVENTS = {
    "scan/started": "扫描启动",
    "recon/finished": "侦察完成",
    "agent/dispatched": "子代理派发",
    "agent/report": "子代理报告",
    "attack/executed": "攻击执行",
    "attack/verdict": "攻击判定",
    "finding/detected": "漏洞确认",
    "fix/planned": "修复规划",
    "fix/applied": "修复应用",
    "regression/verified": "回归验证",
    "scan/finished": "扫描完成",
    "scan/failed": "扫描失败",
}


class TaskRunner:
    """任务管线执行器（串行队列 + 步骤/日志追踪）。"""

    def __init__(self, cfg: ScanConfig, runtime_dir: str, lab=None,
                 with_lab: bool = False) -> None:
        self.cfg = cfg
        self.runtime_dir = runtime_dir
        self.lab = lab
        self.with_lab = with_lab
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

    # ---- 生命周期 ----

    def start(self) -> None:
        self._worker = asyncio.get_running_loop().create_task(self._run_queue())

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        if self.lab is not None:
            self.lab.stop()

    # ---- 任务创建 ----

    def submit(self, name: str, target_url: str = "",
               project_dir: str = "", llm_agent: bool = False,
               llm_explorer: bool = False, llm_fix_plan: bool = False,
               blue_fix: bool = False) -> str:
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        task = {
            "task_id": task_id, "name": name, "status": "queued",
            "target": target_url or project_dir, "target_url": target_url,
            "project_dir": project_dir,
            "llm_agent": llm_agent, "llm_explorer": llm_explorer,
            "llm_fix_plan": llm_fix_plan, "blue_fix": blue_fix,
            "created_at": time.time(), "started_at": None,
            "finished_at": None, "error": None,
            "scan_id": None, "result": None, "remediation": None,
            "steps": [], "logs": [], "log_file": None,
        }
        self.tasks[task_id] = task
        self._queue.put_nowait(task_id)
        return task_id

    def extract_project(self, zip_bytes: bytes, name: str) -> str:
        """解压上传的 ZIP 到 runtime_dir/projects/<id>，返回目录路径。"""
        project_dir = os.path.join(self.runtime_dir, "projects",
                                   uuid.uuid4().hex[:8])
        os.makedirs(project_dir, exist_ok=True)
        with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as zf:
            # 防 zip-slip：所有成员路径规范化后必须在 project_dir 内
            base = os.path.abspath(project_dir)
            for member in zf.namelist():
                target = os.path.abspath(os.path.join(base, member))
                if not target.startswith(base + os.sep) and target != base:
                    raise ValueError(f"非法压缩包路径: {member}")
            zf.extractall(project_dir)
        return project_dir

    # ---- 队列与管线 ----

    async def _run_queue(self) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                await self._run_task(task_id)
            except Exception as exc:  # 单任务失败不影响后续任务
                log.exception("任务 %s 执行失败", task_id)
                task = self.tasks[task_id]
                task["status"] = "failed"
                task["error"] = str(exc)
                task["finished_at"] = time.time()

    async def _run_task(self, task_id: str) -> None:
        task = self.tasks[task_id]
        task["status"] = "running"
        task["started_at"] = time.time()
        self._open_log(task)
        self._step(task, "prepare", "任务准备")
        if task["target_url"]:
            await self._run_url_task(task)
        else:
            await self._run_project_task(task)
        self._finish_step(task, "prepare", "任务准备完成")
        if task["status"] == "running":
            task["status"] = "finished"
        task["finished_at"] = time.time()
        self._log(task, "info", f"任务结束（{task['status']}）")

    async def _run_url_task(self, task: Dict[str, Any]) -> None:
        cfg = self._build_url_config(task)
        from ..runtime import RedTeamRuntime
        from ..engine import ScanRunner, build_adapter
        runtime = RedTeamRuntime(cfg)
        await runtime.start()
        adapter = None
        try:
            adapter = build_adapter(cfg)
            self._attach_events(runtime, task)
            # ① 侦察
            self._step(task, "recon", "目标侦察")
            probe = await adapter.probe()
            task["probe"] = probe.to_json()
            self._log(task, "info", f"目标可达={probe.reachable} "
                                    f"对话={probe.chat_ok} "
                                    f"场景={probe.scenarios or '未识别'}")
            self._finish_step(task, "recon")
            # ② 红队攻击（确定性引擎 + 可选 LLM 自主/主动侦察）
            self._step(task, "attack", "红队攻击")
            result = await ScanRunner(runtime, cfg, adapter,
                                      scan_mode="web").run()
            task["scan_id"] = result.scan_id
            task["result"] = self._result_summary(result)
            self._finish_step(task, "attack", f"命中 {result.success_count}，"
                                             f"发现 {len(result.findings)} 条漏洞")
            # ③ 蓝队修复（可选）
            if task["blue_fix"] and adapter is not None and \
                    cfg.target.type != "folder":
                self._step(task, "fix", "蓝队修复+回归")
                from ..blueteam import BlueEngine
                from ..cli import _load_scan_result
                scan_result = await _load_scan_result(
                    runtime, runtime.storage, result.scan_id)
                outcome = await BlueEngine(runtime, cfg, adapter,
                                           scan_result).run(apply_fixes=True)
                task["remediation"] = outcome.remediation_path
                self._finish_step(task, "fix",
                                  f"修复 {outcome.summary()['applied']} 条，"
                                  f"回归通过 {outcome.summary()['verified']} 条")
            else:
                self._skip_step(task, "fix", "蓝队修复+回归")
        finally:
            if adapter is not None:
                await adapter.close()
            await runtime.close()

    async def _run_project_task(self, task: Dict[str, Any]) -> None:
        cfg = self._build_project_config(task)
        from ..runtime import RedTeamRuntime
        from ..engine import ScanRunner
        runtime = RedTeamRuntime(cfg)
        await runtime.start()
        try:
            self._attach_events(runtime, task)
            self._step(task, "static", "静态代码扫描")
            result = await ScanRunner(runtime, cfg, None,
                                      scan_mode="web-upload").run()
            task["scan_id"] = result.scan_id
            task["result"] = self._result_summary(result)
            self._finish_step(task, "static",
                              f"发现 {len(result.findings)} 处代码问题")
            self._step(task, "fix", "修复方案生成")
            from ..blueteam import BlueEngine
            from ..cli import _load_scan_result
            scan_result = await _load_scan_result(
                runtime, runtime.storage, result.scan_id)
            outcome = await BlueEngine(runtime, cfg, None,
                                       scan_result).run(apply_fixes=False)
            task["remediation"] = outcome.remediation_path
            self._finish_step(task, "fix",
                              f"生成 {len(outcome.plans)} 条修复方案")
        finally:
            await runtime.close()

    # ---- 配置构造 ----

    def _build_url_config(self, task: Dict[str, Any]) -> ScanConfig:
        cfg = self.cfg
        target = cfg.target
        # 面板自带靶场（type=lab 且网址相同）：保留 lab 语义（蓝队可自动修复+回归）；
        # 其余网址一律视为外部目标 http（只出方案，绝不自动修改）
        if target.type == "lab" and \
                (target.base_url or "").rstrip("/") == \
                task["target_url"].rstrip("/"):
            target.type = "lab"
        else:
            target.type = "http"
        target.base_url = task["target_url"]
        cfg.engine.llm_agent = bool(task["llm_agent"])
        cfg.engine.llm_explorer_tools = bool(task["llm_explorer"])
        cfg.engine.llm_fix_plan = bool(task["llm_fix_plan"])
        return cfg

    def _build_project_config(self, task: Dict[str, Any]) -> ScanConfig:
        cfg = self.cfg
        cfg.target.type = "folder"
        cfg.target.folder_path = task["project_dir"]
        cfg.engine.llm_fix_plan = bool(task["llm_fix_plan"])
        return cfg

    # ---- 事件订阅（实时日志） ----

    def _attach_events(self, runtime: Any, task: Dict[str, Any]) -> None:
        ctx = runtime.ctx
        for name in _TASK_EVENTS:
            ctx.on(name, lambda payload=None, _n=name, _t=task:
                   self._on_event(_t, _n, payload))

    def _on_event(self, task: Dict[str, Any], name: str,
                  payload: Any = None) -> None:
        detail = ""
        if isinstance(payload, dict):
            detail = (payload.get("sample") or payload.get("scan_id")
                      or payload.get("agent") or "")
        elif hasattr(payload, "sample_uid") and hasattr(payload, "verdict"):
            detail = f"{payload.sample_uid} → {payload.verdict}"
        elif hasattr(payload, "sample_uid"):
            detail = f"{payload.sample_uid}"
        message = _TASK_EVENTS.get(name, name)
        self._log(task, "info", f"{message} {detail}".strip())

    # ---- 步骤与日志 ----

    def _step(self, task: Dict[str, Any], step_id: str, name: str) -> None:
        task["steps"].append({"id": step_id, "name": name, "status": "running",
                              "started_at": time.time(), "finished_at": None,
                              "message": ""})
        self._log(task, "info", f"▶ 阶段开始：{name}")

    def _finish_step(self, task: Dict[str, Any], step_id: str,
                     message: str = "") -> None:
        for step in reversed(task["steps"]):
            if step["id"] == step_id and step["status"] == "running":
                step["status"] = "finished"
                step["finished_at"] = time.time()
                step["message"] = message
                break
        self._log(task, "info", f"✓ 阶段完成：{message or step_id}")

    def _skip_step(self, task: Dict[str, Any], step_id: str,
                   name: str = "") -> None:
        task["steps"].append({"id": step_id, "name": name or step_id,
                              "status": "skipped", "started_at": time.time(),
                              "finished_at": time.time(), "message": "跳过"})

    def _open_log(self, task: Dict[str, Any]) -> None:
        os.makedirs(os.path.join(self.runtime_dir, "tasks"), exist_ok=True)
        task["log_file"] = os.path.join(
            self.runtime_dir, "tasks", f"{task['task_id']}.jsonl")

    def _log(self, task: Dict[str, Any], level: str, message: str) -> None:
        row = {"ts": time.strftime("%H:%M:%S"), "level": level,
               "msg": message}
        task["logs"].append(row)
        if len(task["logs"]) > 2000:      # 内存环形上限
            task["logs"] = task["logs"][-2000:]
        if task.get("log_file"):
            try:
                with open(task["log_file"], "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass

    @staticmethod
    def _result_summary(result: Any) -> Dict[str, Any]:
        return {
            "scan_id": result.scan_id, "target": result.target,
            "total": result.total, "success": result.success_count,
            "suspicious": result.suspicious_count,
            "findings": [
                {"finding_id": f.finding_id, "category": f.category,
                 "severity": f.severity, "role": f.role,
                 "sample_id": f.sample_id, "evidence": f.evidence[:200],
                 "fix_plan": f.fix.get("plan", "")}
                for f in result.findings],
            "severity_counts": result.severity_counts(),
            "report_path": result.report_path,
            "report_json_path": result.report_json_path,
        }

    # ---- 按需蓝队修复（任务已完成扫描后） ----

    async def _run_fix_now(self, task: Dict[str, Any]) -> None:
        """对已完成扫描的任务执行蓝队修复并生成修复报告。"""
        scan_id = task.get("scan_id")
        if not scan_id:
            raise RuntimeError("扫描尚未完成")
        cfg = (self._build_url_config(task) if task["target_url"]
               else self._build_project_config(task))
        from ..runtime import RedTeamRuntime
        from ..engine import build_adapter
        from ..cli import _load_scan_result
        from ..blueteam import BlueEngine
        runtime = RedTeamRuntime(cfg)
        await runtime.start()
        adapter = None
        try:
            adapter = build_adapter(cfg)
            self._attach_events(runtime, task)
            scan_result = await _load_scan_result(
                runtime, runtime.storage, scan_id)
            self._step(task, "fix", "蓝队修复+回归")
            outcome = await BlueEngine(
                runtime, cfg, adapter, scan_result,
            ).run(apply_fixes=(adapter is not None))
            task["remediation"] = outcome.remediation_path
            task["fix_summary"] = outcome.summary()
            self._finish_step(
                task, "fix",
                f"修复 {outcome.summary()['applied']} 条，"
                f"回归通过 {outcome.summary()['verified']} 条")
        finally:
            if adapter is not None:
                await adapter.close()
            await runtime.close()
