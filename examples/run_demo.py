"""examples/run_demo.py —— 编程式接入示例：红队扫描 → 蓝队修复 → 回归验收。

运行: python examples/run_demo.py
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from redteam._compat import ensure_dsh

ensure_dsh()

from target_lab import build_default_guards_file, planted_categories, start_lab
from redteam.config import ScanConfig
from redteam.engine import ScanRunner, build_adapter
from redteam.runtime import RedTeamRuntime
from redteam.blueteam import BlueEngine


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="dsh-redteam-demo-") as tmp:
        guards_file = os.path.join(tmp, "lab_guards.yml")
        build_default_guards_file(guards_file)
        lab = start_lab(guards_file=guards_file, port=0)
        print(f"① 靶场: {lab.base_url}")

        cfg = ScanConfig.from_dict({
            "profile": "quick",
            "target": {"name": "demo-ecommerce-lab", "type": "lab",
                       "base_url": lab.base_url, "guards_file": guards_file},
            "vectors": {"variants_per_sample": 2},
            "storage": {"db_path": os.path.join(tmp, "redteam.db"),
                        "audit_dir": os.path.join(tmp, "audit")},
            "out_dir": os.path.join(tmp, "reports"),
            "blueteam": {"enabled": True,
                         "sandbox_dir": os.path.join(tmp, "sandbox")},
        })
        runtime = RedTeamRuntime(cfg)
        await runtime.start()
        adapter = None
        try:
            adapter = build_adapter(cfg)
            # ② 红队扫描
            result = await ScanRunner(runtime, cfg, adapter).run()
            found = {f.category for f in result.findings}
            planted = planted_categories()
            print(f"② 扫描: 命中 {result.success_count}，发现 "
                  f"{len(found & planted)}/{len(planted)} 类埋入漏洞")
            print(f"   报告: {result.report_path}")
            # ③ 蓝队修复 + 回归
            blue = BlueEngine(runtime, cfg, adapter, result)
            outcome = await blue.run(apply_fixes=True)
            print(f"③ 修复: {outcome.summary()}")
            # ④ 复扫验收
            result2 = await ScanRunner(runtime, cfg, adapter,
                                       scan_mode="verify").run()
            print(f"④ 复扫: 命中 {result2.success_count} "
                  f"（修复前 {result.success_count}）")
        finally:
            if adapter is not None:
                await adapter.close()
            await runtime.close()
            lab.stop()


if __name__ == "__main__":
    asyncio.run(main())
