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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .._env import load_dotenv
from ..config import ScanConfig
from .taskrunner import TaskRunner

# 面板启动即加载项目 .env（DEEPSEEK_* 凭据）——
# 否则 /api/config 的 llm_available 在 runtime 尚未导入时为 False，
# 前端会把三个 LLM 开关全部禁用。
load_dotenv()

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

    # ---- 企业级鉴权：设置 REDTEAM_API_TOKEN 后强制 Bearer Token ----
    # 未设置时（开发/内网）放行；生产部署必须设置，避免面板裸奔。
    _api_token = os.environ.get("REDTEAM_API_TOKEN", "").strip()

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if _api_token and request.url.path.startswith("/api/"):
            auth = request.headers.get("Authorization", "")
            if (auth != f"Bearer {_api_token}"
                    and request.headers.get("X-API-Token") != _api_token):
                return JSONResponse(
                    {"detail": "unauthorized: 缺少或错误的访问令牌（REDTEAM_API_TOKEN）"},
                    status_code=401)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        if os.path.exists(_INDEX_HTML):
            with open(_INDEX_HTML, encoding="utf-8") as fh:
                return fh.read()
        return "<h1>dsh-red-blue-team</h1><p>static/index.html 缺失</p>"

    @app.get("/api/stats")
    async def stats():
        """仪表盘总览：聚合所有任务的漏洞/攻击/任务统计。"""
        from collections import Counter
        tasks = runner.tasks
        sev: Counter = Counter()
        cats: Counter = Counter()
        status = Counter(t.get("status", "?") for t in tasks.values())
        findings_n = attack_total = attack_success = attack_suspicious = 0
        for t in tasks.values():
            r = t.get("result")
            if not r:
                continue
            sc = r.get("severity_counts") or {}
            for k, v in sc.items():
                sev[k] += v
            for f in r.get("findings", []):
                cats[f.get("category", "?")] += 1
            findings_n += len(r.get("findings", []))
            attack_total += int(r.get("total", 0))
            attack_success += int(r.get("success", 0))
            attack_suspicious += int(r.get("suspicious", 0))
        order = ["critical", "high", "medium", "low", "info"]
        return {
            "total_tasks": len(tasks),
            "status": {"running": status.get("running", 0),
                       "finished": status.get("finished", 0),
                       "failed": status.get("failed", 0)},
            "findings_total": findings_n,
            "severity": {k: sev.get(k, 0) for k in order},
            "categories": dict(cats.most_common(10)),
            "attack": {"total": attack_total, "success": attack_success,
                       "suspicious": attack_suspicious},
        }

    @app.get("/api/config")
    async def config():
        target = cfg.target
        return {"target": target.name, "type": target.type,
                "base_url": (target.folder_path if target.type == "folder"
                             else target.base_url),
                "scenario": target.scenario, "roles": target.roles,
                "llm_available": _llm_available(),
                "llm_model": os.environ.get("DEEPSEEK_MODEL", "")}

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

    @app.get("/api/tasks/{task_id}/report.html")
    async def task_report_html(task_id: str):
        """HTML 版报告：打印友好单页，浏览器「打印→另存 PDF」可得专业 PDF。"""
        task = runner.tasks.get(task_id)
        if task is None or not task.get("result"):
            raise HTTPException(status_code=404, detail="报告尚未生成")
        path = task["result"].get("report_path")
        if not path or not os.path.exists(path):
            raise HTTPException(status_code=404, detail="报告文件不存在")
        from ..reporter.html import render_report_html
        with open(path, encoding="utf-8") as fh:
            md = fh.read()
        title = f"安全检测报告 · {task.get('name') or task_id}"
        return HTMLResponse(render_report_html(md, title=title))

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
