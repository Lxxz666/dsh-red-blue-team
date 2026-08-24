"""test_web_frontend —— 前端 JS 冒烟验收。

用 node + 最小 DOM/fetch 桩执行 static/index.html 的 init()，
捕获 ReferenceError/TypeError 这类只在浏览器运行时暴露的错误
（语法检查 node --check 无法发现跨作用域引用等 bug）。
"""
import os
import shutil
import subprocess

import pytest

_HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "frontend_harness.js")
_INDEX = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "redteam", "web", "static", "index.html")


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 环境")
def test_frontend_init_with_and_without_llm():
    """init() 在 LLM 可用/不可用两种场景都必须完整跑完（徽标正确、无异常）。"""
    result = subprocess.run(
        ["node", _HARNESS, _INDEX], capture_output=True, text=True,
        timeout=120, encoding="utf-8", errors="replace")
    out = result.stdout or ""
    assert result.returncode == 0, (
        f"前端 init 失败:\n{out}\n{result.stderr}")
    # LLM 可用：徽标显示模型名（此前 cfg 跨 try 作用域引用 → ReferenceError，
    # 导致徽标卡在「检测中」且按钮事件全部未绑定）
    assert "llm_available=true → INIT OK" in out
    assert "LLM 已就绪（deepseek-v4-flash）" in out
    # LLM 不可用：确定性引擎提示 + 开关禁用逻辑不抛错
    assert "llm_available=false → INIT OK" in out
    assert "LLM 未配置" in out
    # 右上角徽标随选中任务名称变化
    assert "任务: <b>我的业务系统</b>" in out
    assert "http://192.168.1.50:8080" in out
