"""test_web —— Web 面板：任务创建/轮询/报告/修复端到端。"""
import asyncio

import httpx
import pytest

from redteam.config import ScanConfig
from redteam.web.panel import create_app

from .conftest import make_config


@pytest.fixture
def web_app(vuln_lab, tmp_path):
    """内置靶场 + 面板应用（含 lifespan 管理）。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path,
                      vectors={"variants_per_sample": 1})
    app = create_app(cfg, lab=lab)
    return app


async def test_panel_full_flow(web_app):
    transport = httpx.ASGITransport(app=web_app)
    manager = web_app.state.manager
    manager.start()
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     timeout=120.0) as client:
            # ① 面板首页
            resp = await client.get("/")
            assert resp.status_code == 200
            assert "控制面板" in resp.text
            # ② 配置信息
            resp = await client.get("/api/config")
            assert resp.status_code == 200
            data = resp.json()
            assert data["type"] == "lab"
            # ③ 发起扫描 → 轮询到完成
            resp = await client.post("/api/scans")
            task_id = resp.json()["task_id"]
            for _ in range(120):
                await asyncio.sleep(0.5)
                detail = (await client.get(f"/api/scans/{task_id}")).json()
                if detail["task"]["status"] in ("finished", "failed"):
                    break
            assert detail["task"]["status"] == "finished", \
                f"扫描任务失败: {detail['task'].get('error')}"
            result = detail["result"]
            assert result["success"] > 0
            assert result["findings"]
            assert result["severity_counts"]["critical"] > 0
            # ④ 任务列表
            tasks = (await client.get("/api/scans")).json()["tasks"]
            assert any(t["task_id"] == task_id for t in tasks)
            # ⑤ 攻击报告
            report = await client.get(f"/api/scans/{task_id}/report")
            assert report.status_code == 200
            assert "漏洞总览" in report.text
            # ⑥ 蓝队修复 + 修复报告
            fix = await client.post(f"/api/scans/{task_id}/fix")
            assert fix.status_code == 200
            remediation = await client.get(
                f"/api/scans/{task_id}/remediation")
            assert remediation.status_code == 200
            assert "① 漏洞问题说明" in remediation.text
            assert "回归验证汇总" in remediation.text
            # ⑦ 404 语义
            resp = await client.get("/api/scans/unknown/report")
            assert resp.status_code == 404
    finally:
        await manager.close()


async def test_panel_scan_queue_serialized(web_app):
    """连续提交两个任务：第二个排队（queued），串行执行不交叉。"""
    transport = httpx.ASGITransport(app=web_app)
    manager = web_app.state.manager
    manager.start()
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     timeout=120.0) as client:
            ids = []
            for _ in range(2):
                resp = await client.post("/api/scans")
                ids.append(resp.json()["task_id"])
            await asyncio.sleep(0.2)
            statuses = [((await client.get(f"/api/scans/{i}")).json()
                         )["task"]["status"] for i in ids]
            assert statuses[0] in ("running", "finished")
            assert statuses[1] == "queued", "第二个任务应排队"
            # 等全部完成
            for _ in range(120):
                await asyncio.sleep(0.5)
                done = True
                for i in ids:
                    detail = (await client.get(f"/api/scans/{i}")).json()
                    if detail["task"]["status"] != "finished":
                        done = False
                        break
                if done:
                    break
            assert done
    finally:
        await manager.close()
