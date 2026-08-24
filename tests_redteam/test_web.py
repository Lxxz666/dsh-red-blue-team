"""test_web —— Web 面板新 API：网址任务全链路 / 上传任务 / 授权闸门。

覆盖：POST /api/tasks（网址）、POST /api/tasks/upload（ZIP）、
GET /api/tasks（列表）、GET /api/tasks/{id}?after=N（步骤+增量日志）、
GET report / POST fix / GET remediation、404/400/403 语义。
"""
import asyncio
import io
import zipfile

import httpx
import pytest

from redteam.web.panel import create_app

from .conftest import make_config


@pytest.fixture
async def web_app(vuln_lab, tmp_path):
    """面板应用（lab 由 fixture 管理，不交给 TaskRunner 关闭）。"""
    lab, guards_file = vuln_lab
    cfg = make_config(lab, guards_file, tmp_path,
                      vectors={"variants_per_sample": 1})
    app = create_app(cfg, str(tmp_path / "web_runtime"))
    app.state.runner.start()
    yield app, lab
    await app.state.runner.close()


@pytest.fixture
async def client(web_app):
    transport = httpx.ASGITransport(app=web_app[0])
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=180.0) as c:
        yield c


async def _poll(client: httpx.AsyncClient, task_id: str, timeout_s: float = 180.0):
    for _ in range(int(timeout_s * 2)):
        await asyncio.sleep(0.5)
        detail = (await client.get(f"/api/tasks/{task_id}")).json()
        if detail["task"]["status"] in ("finished", "failed"):
            return detail
    raise AssertionError("任务超时未完成")


async def test_index_and_config(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "dsh-red-blue-team" in resp.text
    assert "开始红队检测" in resp.text
    data = (await client.get("/api/config")).json()
    assert data["type"] == "lab"
    assert data["target"] == "test-lab"
    assert isinstance(data["llm_available"], bool)


async def test_url_task_full_flow(client, web_app):
    """网址任务：侦察→攻击→（可选修复）→ 步骤/日志/报告/修复报告全链路。"""
    target_url = web_app[0].state.runner.cfg.target.base_url
    resp = await client.post("/api/tasks", json={
        "name": "e2e 靶场目标", "url": target_url,
        "llm_agent": False, "llm_explorer": False,
        "llm_fix_plan": False, "blue_fix": False})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    detail = await _poll(client, task_id)
    assert detail["task"]["status"] == "finished", \
        f"任务失败: {detail['task'].get('error')}"

    # 步骤时间线：recon 与 attack 必须存在且 finished
    step_ids = {s["id"]: s for s in detail["steps"]}
    assert "recon" in step_ids and step_ids["recon"]["status"] == "finished"
    assert "attack" in step_ids and step_ids["attack"]["status"] == "finished"
    assert step_ids["attack"]["finished_at"] > step_ids["attack"]["started_at"]

    # 结果：确定性引擎应命中靶场漏洞
    result = detail["result"]
    assert result["total"] > 0
    assert result["success"] > 0
    assert len(result["findings"]) > 0
    assert result["severity_counts"]["critical"] > 0
    assert result["report_path"]

    # 日志：事件订阅产生实时日志；?after=N 增量拉取语义正确
    assert detail["log_count"] > 0
    assert any("红队攻击" in l["msg"] or "阶段" in l["msg"]
               for l in detail["logs"])
    first_count = detail["log_count"]
    after_resp = (await client.get(
        f"/api/tasks/{task_id}?after={first_count}")).json()
    assert after_resp["logs"] == []          # 无新日志 → 空增量
    assert after_resp["log_count"] == first_count

    # 任务列表包含该任务
    tasks = (await client.get("/api/tasks")).json()["tasks"]
    row = next(t for t in tasks if t["task_id"] == task_id)
    assert row["status"] == "finished"
    assert row["step_done"] == row["step_total"]
    assert row["findings"] == len(result["findings"])

    # 攻击报告
    report = await client.get(f"/api/tasks/{task_id}/report")
    assert report.status_code == 200
    assert "漏洞总览" in report.text

    # 蓝队修复（任务完成后按需触发）+ 修复报告
    fix = await client.post(f"/api/tasks/{task_id}/fix")
    assert fix.status_code == 200
    remediation = await client.get(f"/api/tasks/{task_id}/remediation")
    assert remediation.status_code == 200
    assert "① 漏洞问题说明" in remediation.text
    assert "回归验证汇总" in remediation.text


async def test_upload_project_task(client, tmp_path):
    """上传 ZIP → 解压解析 → 静态扫描 → 修复方案（无需 lab 攻击）。"""
    zf_bytes = io.BytesIO()
    with zipfile.ZipFile(zf_bytes, "w") as zf:
        zf.writestr("app.py", (
            "import pickle\n"
            "import subprocess\n"
            "DEBUG = True\n"
            'API_KEY = "sk-abcdef1234567890"\n'
            'query = "SELECT * FROM users WHERE id = %s"\n'
            "def run(cmd):\n"
            "    subprocess.call(cmd, shell=True)\n"
            "def load(data):\n"
            "    return pickle.loads(data)\n"))
    resp = await client.post(
        "/api/tasks/upload?name=订单服务",
        content=zf_bytes.getvalue(),
        headers={"Content-Type": "application/zip"})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    detail = await _poll(client, task_id)
    assert detail["task"]["status"] == "finished", \
        f"任务失败: {detail['task'].get('error')}"
    step_ids = {s["id"]: s for s in detail["steps"]}
    assert step_ids["static"]["status"] == "finished"
    assert step_ids["fix"]["status"] == "finished"

    findings = detail["result"]["findings"]
    categories = {f["category"] for f in findings}
    assert "unsafe_eval" not in categories          # 无 eval 不应命中
    assert "hardcoded_secret" in categories         # API_KEY
    assert "command_injection" in categories        # shell=True
    assert "unsafe_deserialization" in categories   # pickle.loads
    assert "debug_mode" in categories               # DEBUG=True
    # 修复方案已生成（静态目标 → 方案不自动应用）
    remediation = await client.get(f"/api/tasks/{task_id}/remediation")
    assert remediation.status_code == 200
    assert "修复方案" in remediation.text or "漏洞问题说明" in remediation.text


async def test_upload_rejects_bad_zip(client):
    resp = await client.post("/api/tasks/upload?name=坏包",
                             content=b"not-a-zip")
    assert resp.status_code == 400
    assert "压缩包解析失败" in resp.json()["detail"]


async def test_upload_rejects_zip_slip(client):
    """zip-slip 路径（../ 逃逸）必须被拒绝。"""
    zf_bytes = io.BytesIO()
    with zipfile.ZipFile(zf_bytes, "w") as zf:
        zf.writestr("../evil.py", "x = 1")
    resp = await client.post("/api/tasks/upload?name=穿越",
                             content=zf_bytes.getvalue())
    assert resp.status_code == 400
    assert "非法压缩包路径" in resp.json()["detail"]


async def test_non_local_url_accepted(client):
    """本地测试便利：任意网址直接创建任务（不再要求 authorization 块）。"""
    # 域名解析必然失败的地址 → 后台任务快速失败，不影响创建语义
    resp = await client.post("/api/tasks", json={
        "name": "外部目标", "url": "http://nonexistent.invalid"})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    tasks = (await client.get("/api/tasks")).json()["tasks"]
    row = next(t for t in tasks if t["task_id"] == task_id)
    assert row["target"] == "http://nonexistent.invalid"
    assert row["status"] in ("queued", "running", "finished", "failed")


async def test_bad_url_rejected(client):
    resp = await client.post("/api/tasks", json={
        "name": "坏地址", "url": "ftp://example.com"})
    assert resp.status_code == 400
    resp = await client.post("/api/tasks", json={"name": "空", "url": ""})
    assert resp.status_code == 400


async def test_unknown_task_404(client):
    assert (await client.get("/api/tasks/not-exist")).status_code == 404
    assert (await client.get("/api/tasks/not-exist/report")).status_code == 404
    assert (await client.get(
        "/api/tasks/not-exist/remediation")).status_code == 404
    assert (await client.post(
        "/api/tasks/not-exist/fix")).status_code == 404
