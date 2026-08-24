"""redteam.web.panel —— Web 面板（FastAPI，复用 dsh 依赖栈）。

面向验收的核心面板：
- POST /api/tasks            {name, url, llm_agent, llm_explorer, blue_fix}
                             创建网址扫描任务（任意 http(s) 网址，本地测试直接输入）
- POST /api/tasks/upload     原始 ZIP 体（?name=项目名）→ 解压解析 → 静态扫描任务
- GET  /api/tasks            任务列表（状态/步骤进度/漏洞数）
- GET  /api/tasks/{id}       任务详情：步骤时间线 + 日志（?after=N 增量拉取）
- POST /api/tasks/{id}/fix   蓝队修复（已扫描任务）
- GET  /api/tasks/{id}/report|remediation  攻击/修复报告（Markdown）
- GET  /                       精致前端（static/index.html）
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..config import ScanConfig
from .taskrunner import TaskRunner

_INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "static", "index.html")


def create_app(cfg: ScanConfig, runtime_dir: str,
               lab=None) -> FastAPI:
    runner = TaskRunner(cfg, runtime_dir, lab=lab)
    os.makedirs(runtime_dir, exist_ok=True)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runner.start()
        try:
            yield
        finally:
            await runner.close()

    app = FastAPI(title="dsh-red-blue-team 面板", lifespan=lifespan)
    app.state.runner = runner

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
                "scenario": target.scenario, "roles": target.roles,
                "llm_available": _llm_available()}

    # ---- 任务创建 ----

    @app.post("/api/tasks")
    async def create_task(payload: Dict[str, Any]):
        name = str(payload.get("name") or "未命名目标").strip()[:60]
        url = str(payload.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="请输入目标网址")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400,
                                detail="网址需以 http:// 或 https:// 开头")
        task_id = runner.submit(
            name=name, target_url=url,
            llm_agent=bool(payload.get("llm_agent")),
            llm_explorer=bool(payload.get("llm_explorer")),
            llm_fix_plan=bool(payload.get("llm_fix_plan")),
            blue_fix=bool(payload.get("blue_fix")))
        return {"task_id": task_id, "status": "queued"}

    @app.post("/api/tasks/upload")
    async def upload_project(request: Request, name: str = Query("上传项目"),
                             llm_fix_plan: bool = Query(False)):
        body = await request.body()
        if not body or len(body) > 50 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="上传体为空或超过 50MB")
        try:
            project_dir = runner.extract_project(body, name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"压缩包解析失败: {exc}")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"压缩包解析失败: {exc}")
        task_id = runner.submit(
            name=str(name)[:60], project_dir=project_dir,
            llm_fix_plan=bool(llm_fix_plan), blue_fix=False)
        return {"task_id": task_id, "status": "queued",
                "project_dir": project_dir}

    # ---- 任务查询 ----

    @app.get("/api/tasks")
    async def list_tasks():
        rows = []
        for task in runner.tasks.values():
            result = task.get("result") or {}
            steps = task.get("steps") or []
            current = next((s["name"] for s in reversed(steps)
                            if s["status"] == "running"), None)
            rows.append({
                "task_id": task["task_id"], "name": task["name"],
                "status": task["status"], "target": task["target"],
                "current_step": current,
                "step_total": len(steps),
                "step_done": sum(1 for s in steps
                                 if s["status"] in ("finished", "skipped")),
                "total": result.get("total"),
                "success": result.get("success"),
                "findings": len(result.get("findings") or []),
                "severity_counts": result.get("severity_counts"),
                "error": task.get("error"),
                "created_at": task.get("created_at"),
                "llm_agent": task.get("llm_agent"),
            })
        rows.sort(key=lambda r: -(r.get("created_at") or 0))
        return {"tasks": rows}

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str, after: int = Query(0, ge=0)):
        task = runner.tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        logs = task.get("logs") or []
        return {
            "task": {
                "task_id": task["task_id"], "name": task["name"],
                "status": task["status"], "target": task["target"],
                "error": task.get("error"),
                "scan_id": task.get("scan_id"),
                "created_at": task.get("created_at"),
                "started_at": task.get("started_at"),
                "finished_at": task.get("finished_at"),
                "remediation": task.get("remediation"),
                "probe": task.get("probe"),
                "llm_agent": task.get("llm_agent"),
            },
            "steps": task.get("steps") or [],
            "logs": logs[after:],
            "log_count": len(logs),
            "result": task.get("result"),
        }

    # ---- 报告与修复 ----

    @app.get("/api/tasks/{task_id}/report")
    async def task_report(task_id: str):
        task = runner.tasks.get(task_id)
        if task is None or not task.get("result"):
            raise HTTPException(status_code=404, detail="报告尚未生成")
        path = task["result"].get("report_path")
        if not path or not os.path.exists(path):
            raise HTTPException(status_code=404, detail="报告文件不存在")
        with open(path, encoding="utf-8") as fh:
            return PlainTextResponse(fh.read(), media_type="text/markdown")

    @app.post("/api/tasks/{task_id}/fix")
    async def task_fix(task_id: str):
        task = runner.tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if not task.get("result"):
            raise HTTPException(status_code=409, detail="扫描尚未完成")
        await runner._run_fix_now(task)
        return {"remediation": task.get("remediation"),
                "fix_summary": task.get("fix_summary")}

    @app.get("/api/tasks/{task_id}/remediation")
    async def task_remediation(task_id: str):
        task = runner.tasks.get(task_id)
        path = task.get("remediation") if task else None
        if not path or not os.path.exists(path):
            raise HTTPException(status_code=404,
                                detail="修复报告尚未生成（先调用 POST /fix）")
        with open(path, encoding="utf-8") as fh:
            return PlainTextResponse(fh.read(), media_type="text/markdown")

    return app


def _llm_available() -> bool:
    """DeepSeek 密钥是否可用（.env / 环境变量）。"""
    try:
        from dsh.llm.deepseek import DeepSeekAdapter
        return bool(DeepSeekAdapter().api_key)
    except Exception:
        return False
