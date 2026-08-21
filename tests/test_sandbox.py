"""真 sandbox 测试：Windows Job Object（kill-on-close）+ 降级语义。"""
import asyncio
import subprocess
import sys
import time

import pytest

from dsh.boot import boot


async def test_sandbox_jobobject_active_on_windows(tmp_path):
    if sys.platform != "win32":
        pytest.skip("jobobject backend is Windows-only")
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        describe = ctx.sandbox.describe()
        assert describe["active"] is True, describe
        assert describe["confinement"] == "jobobject (kill-on-close)"
    finally:
        await tree.dispose()


async def test_jobobject_assign_returns_true(tmp_path):
    if sys.platform != "win32":
        pytest.skip("jobobject backend is Windows-only")
    from dsh.sandbox.jobobject import WindowsJobObject
    job = WindowsJobObject()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        time.sleep(0.8)
        assert job.assign(process.pid) is True  # 子进程确实挂入 Job
        job.close()
        deadline = time.time() + 5
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        # Windows 对 Job 终止的进程报告退出码 0；以「远早于自然结束」判定
        assert process.poll() is not None
    finally:
        job.close()
        if process.poll() is None:
            process.kill()


async def test_sandbox_jobobject_normal_run_unaffected(tmp_path):
    if sys.platform != "win32":
        pytest.skip("jobobject backend is Windows-only")
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        result = await ctx.subprocess.run(
            [sys.executable, "-c", "print('job-ok')"], cwd=str(tmp_path))
        assert result.ok and "job-ok" in result.stdout
    finally:
        await tree.dispose()


async def test_sandbox_jobobject_kills_children_on_close(tmp_path):
    """kill-on-close 硬保证：关闭 Job 句柄 → 挂入的 30s 睡眠子进程数秒内终止。"""
    if sys.platform != "win32":
        pytest.skip("jobobject backend is Windows-only")
    ctx, tree = await boot(profile="headless", workspace=str(tmp_path),
                           mock_llm=True)
    try:
        task = asyncio.ensure_future(ctx.subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(tmp_path)))
        await asyncio.sleep(1.2)  # 子进程起来并被挂入 job
        started = time.monotonic()
        ctx.sandbox.close()       # 关闭句柄 → Job 内进程被终止
        result = await asyncio.wait_for(task, timeout=10)
        elapsed = time.monotonic() - started
        assert elapsed < 8, f"30s 睡眠进程 {elapsed:.1f}s 才结束（未被杀）"
        assert not result.timed_out
        ctx.sandbox.close()       # 幂等
    finally:
        await tree.dispose()


async def test_sandbox_local_mode_on_windows(tmp_path):
    from dsh.errors import ToolError
    from dsh.kernel import Context
    from dsh.sandbox.sandbox import SandboxService
    ctx = Context("sandbox-local")
    sandbox = SandboxService(ctx, {"mode": "local"})
    sandbox.apply(ctx)
    assert sandbox.describe()["active"] is False
    with pytest.raises(ToolError):
        SandboxService(Context("bad"), {"mode": "weird"})


# ---- landlock 后端 ----

def test_landlock_wrapper_argv_construction():
    from dsh.sandbox.landlock import wrapper_argv
    argv = wrapper_argv("/workspace", ["bash", "-lc", "echo hi"])
    assert argv[1] == "-m" and argv[2] == "dsh.sandbox.landlock_exec"
    assert argv[3] == "/workspace" and argv[4:] == ["bash", "-lc", "echo hi"]


def test_landlock_mode_degrades_honestly_off_linux():
    from dsh.kernel import Context
    from dsh.sandbox.landlock import available
    from dsh.sandbox.sandbox import SandboxService
    if sys.platform == "linux":
        pytest.skip("landlock probe runs on Linux")
    assert available() is False
    sandbox = SandboxService(Context("ll"), {"mode": "landlock"})
    describe = sandbox.describe()
    assert describe["active"] is False
    assert "landlock" in describe["degraded"]  # 如实标注降级原因
    # 降级状态下 confine 原样返回（不伪装成沙箱）
    assert sandbox.confine(["bash", "-c", "x"], "/ws") == ["bash", "-c", "x"]


@pytest.mark.skipif(sys.platform != "linux", reason="landlock is Linux-only")
def test_landlock_active_on_linux():
    from dsh.kernel import Context
    from dsh.sandbox.landlock import available
    from dsh.sandbox.sandbox import SandboxService
    if not available():
        pytest.skip("kernel lacks landlock ABI v1+")
    sandbox = SandboxService(Context("ll"), {"mode": "landlock"})
    describe = sandbox.describe()
    assert describe["active"] is True
    assert "landlock" in describe["confinement"]
    # 活动状态下 confine 返回包装器 argv
    argv = sandbox.confine(["true"], "/workspace")
    assert argv[2] == "dsh.sandbox.landlock_exec"
    assert argv[3] == "/workspace"
