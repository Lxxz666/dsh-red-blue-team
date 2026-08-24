"""redteam.web.panel —— Web 面板（FastAPI，复用 dsh 依赖栈）。

提供：
- POST /api/scans            启动一次红队扫描（后台任务，串行队列）
- GET  /api/scans            扫描任务列表（状态/命中/漏洞数）
- GET  /api/scans/{id}       单任务详情（含漏洞清单）
- GET  /api/scans/{id}/report 攻击报告（Markdown）
- POST /api/scans/{id}/fix   蓝队修复+回归（lab 目标自动应用）
- GET  /api/scans/{id}/remediation 完整修复报告（Markdown）
- GET  /                       面板页面（原生 JS，无构建）

扫描任务在后台 asyncio 串行执行（同一靶场一次一测，避免状态污染）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ..config import ScanConfig

log = logging.getLogger("redteam.web")

_INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "static", "index.html")


class ScanTaskManager:
    """后台扫描任务注册表（串行队列）。"""

    def __init__(self, cfg: ScanConfig, lab=None) -> None:
        self.cfg = cfg
        self.lab = lab
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._seq = 0
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

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

    # ---- 队列 ----

    def submit(self) -> str:
        self._seq += 1
        task_id = f"task-{self._seq:03d}"
        self.tasks[task_id] = {"task_id": task_id, "status": "queued",
                               "scan_id": None, "started_at": None,
                               "finished_at": None, "error": None,
                               "result": None, "remediation": None}
        self._queue.put_nowait(task_id)
        return task_id

    async def _run_queue(self) -> None:
        while True:
            task_id = await self._queue.get()
            await self._run_scan(task_id)

    async def _run_scan(self, task_id: str) -> None:
        task = self.tasks[task_id]
        task["status"] = "running"
        task["started_at"] = time.time()
        runtime = None
        adapter = None
        try:
            from ..runtime import RedTeamRuntime
            from ..engine import ScanRunner, build_adapter
            runtime = RedTeamRuntime(self.cfg)
            await runtime.start()
            adapter = build_adapter(self.cfg)
            result = await ScanRunner(runtime, self.cfg, adapter,
                                      scan_mode="web").run()
            task["result"] = {
                "scan_id": result.scan_id,
                "target": result.target,
                "total": result.total,
                "success": result.success_count,
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
            task["scan_id"] = result.scan_id
            task["status"] = "finished"
        except Exception as exc:
            log.exception("面板扫描任务 %s 失败", task_id)
            task["status"] = "failed"
            task["error"] = str(exc)
        finally:
            if adapter is not None:
                try:
                    await adapter.close()
                except Exception:
                    pass
            if runtime is not None:
                try:
                    await runtime.close()
                except Exception:
                    pass
            task["finished_at"] = time.time()

    # ---- 查询 ----

    def list_tasks(self) -> List[Dict[str, Any]]:
        out = []
        for task in self.tasks.values():
            row = {k: task[k] for k in ("task_id", "status", "scan_id",
                                        "started_at", "finished_at", "error")}
            result = task.get("result") or {}
            row.update({"target": result.get("target", self.cfg.target.name),
                        "total": result.get("total"),
                        "success": result.get("success"),
                        "findings": len(result.get("findings") or [])})
            out.append(row)
        return out

    def get_task(self, task_id: str) -> Dict[str, Any]:
        if task_id not in self.tasks:
            raise KeyError(task_id)
        return self.tasks[task_id]

    async def run_fix(self, task_id: str) -> str:
        """蓝队修复+回归，返回修复报告路径。"""
        task = self.get_task(task_id)
        result = task.get("result")
        if not result:
            raise RuntimeError("扫描尚未完成")
        if task.get("remediation"):
            return task["remediation"]
        from ..runtime import RedTeamRuntime
        from ..engine import build_adapter
        from ..blueteam import BlueEngine
        runtime = RedTeamRuntime(self.cfg)
        await runtime.start()
        adapter = None
        try:
            adapter = build_adapter(self.cfg)
            from ..cli import _load_scan_result
            scan_result = await _load_scan_result(runtime, runtime.storage,
                                                  result["scan_id"])
            outcome = await BlueEngine(runtime, self.cfg, adapter,
                                       scan_result).run(apply_fixes=True)
            task["remediation"] = outcome.remediation_path
            task["fix_summary"] = outcome.summary()
            return outcome.remediation_path
        finally:
            if adapter is not None:
                await adapter.close()
            await runtime.close()

    def report_text(self, task_id: str) -> str:
        task = self.get_task(task_id)
        result = task.get("result")
        if not result or not result.get("report_path"):
            raise RuntimeError("报告尚未生成")
        path = result["report_path"]
        if not os.path.exists(path):
            raise RuntimeError(f"报告文件不存在: {path}")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def remediation_text(self, task_id: str) -> str:
        task = self.get_task(task_id)
        path = task.get("remediation")
        if not path or not os.path.exists(path):
            raise RuntimeError("修复报告尚未生成（先调用 POST /fix）")
        with open(path, encoding="utf-8") as fh:
            return fh.read()


def create_app(cfg: ScanConfig, lab=None) -> FastAPI:
    manager = ScanTaskManager(cfg, lab)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager.start()
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(title="dsh-red-blue-team 面板", lifespan=lifespan)
    app.state.manager = manager  # 供测试/嵌入式使用直接驱动任务管理器

    @app.get("/", response_class=HTMLResponse)
    async def index():
        if os.path.exists(_INDEX_HTML):
            with open(_INDEX_HTML, encoding="utf-8") as fh:
                return fh.read()
        return "<h1>dsh-red-blue-team</h1><p>static/index.html 缺失</p>"

    @app.get("/api/config")
    async def config():
        target = cfg.target
        return {"target": target.name, "type": target.type,
                "base_url": (target.folder_path if target.type == "folder"
                             else target.base_url),
                "scenario": target.scenario, "roles": target.roles}

    @app.post("/api/scans")
    async def start_scan():
        task_id = manager.submit()
        return {"task_id": task_id, "status": "queued"}

    @app.get("/api/scans")
    async def list_scans():
        return {"tasks": manager.list_tasks()}

    @app.get("/api/scans/{task_id}")
    async def task_detail(task_id: str):
        try:
            task = manager.get_task(task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"task": {k: v for k, v in task.items()
                         if k != "result"},
                "result": task.get("result")}

    @app.get("/api/scans/{task_id}/report")
    async def task_report(task_id: str):
        try:
            text = manager.report_text(task_id)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return PlainTextResponse(text, media_type="text/markdown")

    @app.post("/api/scans/{task_id}/fix")
    async def task_fix(task_id: str):
        try:
            path = await manager.run_fix(task_id)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"remediation_path": path,
                "fix_summary": manager.get_task(task_id).get("fix_summary")}

    @app.get("/api/scans/{task_id}/remediation")
    async def task_remediation(task_id: str):
        try:
            text = manager.remediation_text(task_id)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return PlainTextResponse(text, media_type="text/markdown")

    return app
