"""redteam.cli —— dsh-redteam 命令行入口。

子命令:
    scan     --config scan.yml                对目标发起红队扫描并出报告
    fix      --config scan.yml --scan ID      蓝队：按报告生成并应用修复+回归
    lab      [--port 8765] [--guards FILE]    启动内置靶场
    report   [--scan ID] [--list]             查看/重新生成报告
    bench    --config scan.yml                自适应优先级基准（二次扫描命中率提升）
    demo     [--out DIR]                      一键演示：靶场+扫描+修复+回归闭环
    samples  [list|show CATEGORY]             查看攻击样本库
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

from . import __version__
from .config import ScanConfig
from .errors import AuthorizationError, RedTeamError
from .models import Severity, Verdict


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh-redteam",
        description="红队/蓝队智能安全检测系统（基于 dsh-python 框架）。"
                    "合规红线：仅限授权测试。")
    parser.add_argument("--version", action="version",
                        version=f"dsh-redteam {__version__}")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="红队扫描（网址动态 / 文件夹静态）")
    scan.add_argument("--config", required=True, help="扫描配置 scan.yml")
    scan.add_argument("--out", default=None, help="报告输出目录")
    scan.add_argument("--fix", action="store_true",
                      help="扫描后自动执行蓝队修复+回归（仅 lab 目标自动应用）")
    scan.add_argument("--mode", default=None, help="扫描模式标签（默认取 profile）")
    scan.add_argument("--webhook", default="", help="扫描完成后 POST JSON 摘要（V12 推送）")

    static = sub.add_parser("static", help="本地文件夹快速静态扫描（免配置）")
    static.add_argument("folder", help="项目文件夹路径")
    static.add_argument("--out", default="./reports", help="报告输出目录")
    static.add_argument("--scenario", default="auto",
                        help="业务场景（auto/显式指定，如 ecommerce）")

    fix = sub.add_parser("fix", help="蓝队修复（生成方案/沙箱应用/回归/修复报告）")
    fix.add_argument("--config", required=True, help="扫描配置 scan.yml")
    fix.add_argument("--scan", required=True, help="扫描编号（dsh-redteam report --list）")
    fix.add_argument("--dry-run", action="store_true", help="只生成修复方案，不应用")
    fix.add_argument("--webhook", default="", help="修复完成后 POST JSON 摘要（V12 推送）")

    lab = sub.add_parser("lab", help="启动内置靶场")
    lab.add_argument("--port", type=int, default=8765)
    lab.add_argument("--guards", default=None, help="防护配置文件（默认自动生成）")

    report = sub.add_parser("report", help="查看报告")
    report.add_argument("--config", default=None, help="扫描配置（重建报告需要）")
    report.add_argument("--scan", default=None, help="扫描编号")
    report.add_argument("--list", action="store_true", help="列出历史扫描")
    report.add_argument("--json", action="store_true", help="输出 JSON 报告")
    report.add_argument("--remediation", action="store_true",
                        help="查看该扫描的完整修复报告（fix 后生成）")

    bench = sub.add_parser("bench", help="自适应优先级基准（二次扫描命中率提升）")
    bench.add_argument("--config", required=True, help="扫描配置 scan.yml")

    demo = sub.add_parser("demo", help="一键演示完整闭环（含多业务场景）")
    demo.add_argument("--out", default="./demo_out", help="产物目录")
    demo.add_argument("--port", type=int, default=0, help="靶场端口（0=随机）")

    samples = sub.add_parser("samples", help="查看攻击样本库")
    samples.add_argument("action", nargs="?", default="list",
                         choices=["list", "show"])
    samples.add_argument("category", nargs="?", default=None)

    scenarios = sub.add_parser("scenarios", help="查看业务场景库（12 大场景指纹与样本）")
    scenarios.add_argument("action", nargs="?", default="list",
                           choices=["list", "show"])
    scenarios.add_argument("scenario", nargs="?", default=None)

    web = sub.add_parser("web", help="Web 面板（扫描任务/漏洞/报告/修复）")
    web.add_argument("--config", default=None, help="扫描配置（省略时自动挂内置靶场）")
    web.add_argument("--port", type=int, default=8766)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--with-lab", action="store_true",
                     help="自动启动内置靶场并以其为目标")

    batch = sub.add_parser("batch", help="多目标批扫（targets.yml 串行扫描+风险排序汇总）")
    batch.add_argument("--targets", required=True, help="多目标清单 YAML")
    batch.add_argument("--out", default="./batch_reports", help="报告输出目录")

    schedule = sub.add_parser("schedule", help="定时扫描（V10：周期扫描+报告留存+可选推送）")
    schedule.add_argument("--config", required=True, help="扫描配置 scan.yml")
    schedule.add_argument("--every", default="24h", help="周期：30m/1h/24h（测试可用 5s）")
    schedule.add_argument("--out", default="./scheduled", help="报告留存目录")
    schedule.add_argument("--webhook", default="", help="可选：每次扫描后 POST JSON 摘要")
    schedule.add_argument("--once", action="store_true", help="只跑一次（测试用）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            asyncio.run(cmd_scan(args))
        elif args.command == "static":
            asyncio.run(cmd_static(args))
        elif args.command == "fix":
            asyncio.run(cmd_fix(args))
        elif args.command == "lab":
            cmd_lab(args)
        elif args.command == "report":
            asyncio.run(cmd_report(args))
        elif args.command == "bench":
            asyncio.run(cmd_bench(args))
        elif args.command == "demo":
            asyncio.run(cmd_demo(args))
        elif args.command == "samples":
            cmd_samples(args)
        elif args.command == "scenarios":
            cmd_scenarios(args)
        elif args.command == "web":
            cmd_web(args)   # uvicorn 自行管理事件循环（同步入口）
        elif args.command == "batch":
            asyncio.run(cmd_batch(args))
        elif args.command == "schedule":
            asyncio.run(cmd_schedule(args))
        else:
            build_parser().print_help()
            return 1
        return 0
    except AuthorizationError as exc:
        print(f"\n[合规拦截] {exc}\n", file=sys.stderr)
        return 2
    except RedTeamError as exc:
        print(f"\n[错误] {exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[已中断]", file=sys.stderr)
        return 130


# ---------- 实现 ----------

async def _load_scan_result(runtime, storage, scan_id: str):
    """从存储重建扫描结果（报告/修复命令用）。"""
    from .models import ScanResult
    from .detector.verdict import VerdictResult  # noqa: F401 (类型提示)
    row = storage.get_scan(scan_id)
    if row is None:
        raise RedTeamError(f"扫描不存在: {scan_id}")
    result = ScanResult(scan_id=scan_id, target=row["target"],
                        started_at=row["started_at"],
                        finished_at=row["finished_at"],
                        status=row["status"])
    for attack in storage.attacks_for_scan(scan_id):
        result.verdicts.append(_verdict_from_row(attack))
    for finding in storage.findings_for_scan(scan_id):
        from .models import Finding
        result.findings.append(Finding(
            finding_id=finding["finding_id"], scan_id=finding["scan_id"],
            category=finding["category"], owasp=finding["owasp"],
            severity=finding["severity"], sample_id=finding["sample_id"],
            sample_uid=finding["sample_uid"], payload=finding["payload"],
            role=finding["role"], chain=json.loads(finding["chain"] or "[]"),
            evidence=finding["evidence"],
            signals=json.loads(finding["signals"] or "{}"),
            confidence=finding["confidence"],
            fix={"auto_fixable": bool(finding["fix_template"]),
                 "template": finding["fix_template"],
                 "plan": finding["fix_plan"],
                 "status": finding["fix_status"]}))
    result.report_path = row["report_path"] or ""
    result.audit_path = row["audit_path"] or ""
    result.probe = json.loads(row["probe"] or "{}")
    return result


def _verdict_from_row(row: Dict[str, Any]):
    from .models import VerdictResult
    return VerdictResult(
        sample_uid=row["sample_uid"], category=row["category"],
        role=row["role"], verdict=row["verdict"],
        confidence=row["confidence"], evidence=row["evidence"] or "",
        chain=json.loads(row["chain"] or "[]"), created_at=row["created_at"])


def _print_scan_summary(result) -> None:
    counts = result.severity_counts()
    print(f"\n扫描完成：{result.scan_id}（{result.target}）")
    print(f"  样本 {result.total} ｜ 攻击成功 {result.success_count} ｜ "
          f"存疑 {result.suspicious_count}")
    print("  漏洞分布：", "  ".join(
        f"{s.value}={counts[s.value]}" for s in Severity if counts[s.value]))
    if result.report_path:
        print(f"  报告：{result.report_path}")
        print(f"  JSON：{result.report_json_path}")
    if result.audit_path:
        print(f"  审计：{result.audit_path}")


async def cmd_scan(args: argparse.Namespace) -> None:
    cfg = ScanConfig.from_yaml(args.config)
    if args.out:
        cfg.out_dir = args.out
    from .runtime import RedTeamRuntime
    from .engine import ScanRunner, build_adapter
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)   # folder 目标返回 None（静态扫描）
        runner = ScanRunner(runtime, cfg, adapter,
                            scan_mode=args.mode or cfg.profile)
        result = await runner.run()
        _print_scan_summary(result)
        if args.webhook:
            await _push_webhook(args.webhook, {
                "event": "scan_finished",
                "scan_id": result.scan_id,
                "target": result.target,
                "success": result.success_count,
                "findings": len(result.findings),
                "severity_counts": result.severity_counts(),
                "report": result.report_path})
        if (args.fix or cfg.blueteam.enabled) and adapter is not None \
                and cfg.target.type != "folder":
            print("\n--- 蓝队修复 ---")
            await _run_blue(runtime, cfg, adapter, result, apply=args.fix
                            or cfg.blueteam.enabled)
        elif args.fix and cfg.target.type == "folder":
            print("\n（文件夹静态扫描的修复为人工实施指引，见报告修复工单）")
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


async def _push_webhook(url: str, payload: Dict[str, Any]) -> None:
    """POST JSON 摘要到 webhook（V12 推送渠道：可接钉钉/企微/邮件网关）。"""
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            print(f"webhook 推送: HTTP {response.status_code}")
    except Exception as exc:
        print(f"webhook 推送失败: {exc}")


async def cmd_static(args: argparse.Namespace) -> None:
    """免配置快速静态扫描：dsh-redteam static <文件夹>"""
    folder = os.path.abspath(args.folder)
    cfg = ScanConfig.from_dict({
        "profile": "quick",
        "target": {"name": os.path.basename(folder), "type": "folder",
                   "folder_path": folder, "scenario": args.scenario},
        "storage": {"db_path": os.path.join(args.out, "static.db"),
                    "audit_dir": os.path.join(args.out, "audit")},
        "out_dir": args.out,
    })
    from .runtime import RedTeamRuntime
    from .engine import ScanRunner
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    try:
        result = await ScanRunner(runtime, cfg, None, scan_mode="static").run()
        _print_scan_summary(result)
    finally:
        await runtime.close()


async def _run_blue(runtime, cfg, adapter, result, apply: bool = True) -> None:
    from .blueteam import BlueEngine
    blue = BlueEngine(runtime, cfg, adapter, result)
    outcome = await blue.run(apply_fixes=apply)
    summary = outcome.summary()
    print(f"  修复方案 {summary['plans']} 条 ｜ 应用 {summary['applied']} 条 ｜ "
          f"回归通过 {summary['verified']} 条 ｜ 回滚 {len(outcome.rolled_back)} 条")
    if outcome.regressions:
        for regression in outcome.regressions:
            mark = "✓" if regression.passed else "✗"
            print(f"  [{mark}] {regression.finding_id}: {regression.detail}")
    if outcome.remediation_path:
        print(f"  修复报告: {outcome.remediation_path}")
    if apply and summary["passed_all"] and outcome.regressions:
        print("\n  🎉 回归清零：全部漏洞修复有效，同一攻击重跑 0 命中。")
    elif apply and outcome.regressions:
        print("\n  ⚠️ 存在回归未清零的漏洞（已按配置回滚），请人工复核。")


async def cmd_fix(args: argparse.Namespace) -> None:
    cfg = ScanConfig.from_yaml(args.config)
    from .runtime import RedTeamRuntime
    from .engine import build_adapter
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        result = await _load_scan_result(runtime, runtime.storage, args.scan)
        print(f"蓝队修复：扫描 {args.scan}（{len(result.findings)} 条漏洞）")
        if adapter is None:
            print("（静态/文件夹扫描：输出人工实施修复报告）")
            from .blueteam import BlueEngine
            outcome = await BlueEngine(runtime, cfg, adapter, result).run(
                apply_fixes=False)
            print(f"  修复方案 {len(outcome.plans)} 条 ｜ "
                  f"修复报告: {outcome.remediation_path}")
            if args.webhook:
                await _push_webhook(args.webhook, {
                    "event": "fix_finished", "scan_id": args.scan,
                    "plans": len(outcome.plans),
                    "remediation": outcome.remediation_path})
            return
        from .blueteam import BlueEngine
        blue = BlueEngine(runtime, cfg, adapter, result)
        outcome = await blue.run(apply_fixes=not args.dry_run)
        summary = outcome.summary()
        print(f"  修复方案 {summary['plans']} 条 ｜ 应用 {summary['applied']} 条"
              f" ｜ 回归通过 {summary['verified']} 条 ｜ "
              f"修复报告: {outcome.remediation_path}")
        if args.webhook:
            await _push_webhook(args.webhook, {
                "event": "fix_finished", "scan_id": args.scan,
                "summary": summary,
                "remediation": outcome.remediation_path})
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()


def cmd_lab(args: argparse.Namespace) -> None:
    from target_lab import build_default_guards_file, start_lab
    guards = args.guards or os.path.join(os.getcwd(), "lab_guards.yml")
    if not os.path.exists(guards):
        build_default_guards_file(guards)
        print(f"已生成默认弱防护配置（全部漏洞开启）: {guards}")
    lab = start_lab(guards_file=guards, port=args.port)
    print(f"靶场已启动: {lab.base_url}")
    print(f"  防护配置: {guards}（蓝队修复会修改此文件）")
    print(f"  管理令牌: lab-admin-token（/_admin/reload /_admin/reset）")
    print("  Ctrl+C 停止")
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        lab.stop()
        print("\n靶场已停止。")


async def cmd_report(args: argparse.Namespace) -> None:
    if args.config is None:
        args.config = os.path.join(os.getcwd(), "scan.yml")
    if not os.path.exists(args.config):
        raise RedTeamError(f"缺少配置文件: {args.config}（--config 指定）")
    cfg = ScanConfig.from_yaml(args.config)
    from .runtime import RedTeamRuntime
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    try:
        if args.list or not args.scan:
            rows = runtime.storage.list_scans()
            if not rows:
                print("(暂无历史扫描)")
                return
            print(f"{'扫描编号':<40} {'目标':<24} {'状态':<14} {'样本':>5} {'命中':>4}")
            for row in rows:
                print(f"{row['scan_id']:<40} {row['target']:<24} "
                      f"{row['status']:<14} {row['total_samples']:>5} "
                      f"{row['success_count']:>4}")
            return
        result = await _load_scan_result(runtime, runtime.storage, args.scan)
        # 完整修复报告查看（fix 后生成于 out_dir）
        if args.remediation:
            safe_target = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                                  for ch in result.target)
            path = os.path.join(cfg.out_dir,
                                f"remediation_{safe_target}_{args.scan}.md")
            if not os.path.exists(path):
                raise RedTeamError(
                    f"修复报告不存在: {path}（先执行 dsh-redteam fix --scan "
                    f"{args.scan}）")
            print(open(path, encoding="utf-8").read())
            return
        from .reporter import write_report
        # 目标显示：文件夹目标显示路径；场景从侦察快照恢复
        if cfg.target.type == "folder":
            base_url = cfg.target.folder_path
        else:
            base_url = cfg.target.base_url
        scenarios = (result.probe or {}).get("scenarios") or []
        md_path, json_path = write_report(
            result, cfg.out_dir, probe=result.probe,
            base_url=base_url, audit_path=result.audit_path,
            scenarios=scenarios)
        print(f"报告已生成: {md_path}\nJSON: {json_path}")
        if args.json:
            print(json.dumps(json.load(open(json_path, encoding="utf-8")),
                             ensure_ascii=False, indent=2))
        else:
            print("\n" + open(md_path, encoding="utf-8").read())
    finally:
        await runtime.close()


async def cmd_bench(args: argparse.Namespace) -> None:
    """自适应优先级基准：随机基线序 vs 自适应序（wanter 地形学过一轮后）。

    指标（确定性可复现）：
    - 命中率@预算：不同样本预算下两种顺序的漏洞命中率；
    - 覆盖效率：发现全部漏洞类别所需样本数（越少越好）。
    """
    from target_lab import build_default_guards_file, planted_categories, start_lab
    cfg = ScanConfig.from_yaml(args.config)
    cfg.adaptive.enabled = True
    with tempfile.TemporaryDirectory(prefix="dsh-redteam-bench-") as tmp:
        guards_file = os.path.join(tmp, "guards.yml")
        build_default_guards_file(guards_file)
        cfg.target.guards_file = guards_file
        # 产物目录隔离：bench 的数据库/审计/报告全部落临时目录，不污染工作区
        cfg.storage.db_path = os.path.join(tmp, "bench.db")
        cfg.storage.audit_dir = os.path.join(tmp, "audit")
        cfg.out_dir = os.path.join(tmp, "reports")
        lab = start_lab(guards_file=guards_file, port=0)
        cfg.target.base_url = lab.base_url
        try:
            from .runtime import RedTeamRuntime
            from .engine import ScanRunner, build_adapter
            runtime = RedTeamRuntime(cfg)
            await runtime.start()
            adapter = None
            try:
                adapter = build_adapter(cfg)
                # 第一轮：随机基线（"新手扫描"）
                runner1 = ScanRunner(runtime, cfg, adapter, scan_mode="bench-1",
                                     order="random")
                result1 = await runner1.run()
                # 第二轮：自适应顺序（wanter 地形已学第一轮结果）
                runner2 = ScanRunner(runtime, cfg, adapter, scan_mode="bench-2",
                                     order="adaptive")
                result2 = await runner2.run()
                _print_bench(result1, result2, planted_categories(),
                             runtime.terrain.stats())
            finally:
                if adapter is not None:
                    await adapter.close()
                await runtime.close()
        finally:
            lab.stop()


def _bench_rows(result) -> List[tuple]:
    """按执行顺序返回 (类别, 是否命中)。"""
    from .models import Verdict
    pairs = []
    for verdict in result.verdicts:
        if verdict.verdict == Verdict.ERROR.value:
            continue
        pairs.append((verdict.category, verdict.success))
    return pairs


def _first_hit_positions(pairs: List[tuple]) -> Dict[str, int]:
    positions: Dict[str, int] = {}
    for index, (category, hit) in enumerate(pairs):
        if hit and category not in positions:
            positions[category] = index + 1
    return positions


def _print_bench(result1, result2, planted: set,
                 terrain_stats: Dict[str, Any]) -> None:
    pairs1 = _bench_rows(result1)
    pairs2 = _bench_rows(result2)
    n = len(pairs1)
    print(f"\n自适应攻击优先级基准（{n} 条样本，wanter 势能地形）")
    print(f"{'预算':<8} {'随机基线命中率':>14} {'自适应命中率':>14} {'提升':>8}")
    for budget in (0.25, 0.5, 0.75, 1.0):
        k = max(1, int(n * budget))
        rate1 = sum(h for _, h in pairs1[:k]) / k
        rate2 = sum(h for _, h in pairs2[:k]) / k
        gain = (rate2 - rate1) / rate1 * 100 if rate1 > 0 else 0.0
        print(f"{int(budget * 100):<6}% {rate1:>13.1%} {rate2:>13.1%} "
              f"{gain:>+7.1f}%")
    pos1 = _first_hit_positions(pairs1)
    pos2 = _first_hit_positions(pairs2)
    found1 = planted & set(pos1)
    found2 = planted & set(pos2)
    cover1 = max((pos1[c] for c in found1), default=n)
    cover2 = max((pos2[c] for c in found2), default=n)
    speedup = (cover1 - cover2) / cover1 * 100 if cover1 else 0.0
    print(f"\n覆盖效率（发现全部漏洞类别所需样本数）:")
    print(f"  随机基线: {cover1} 条 ｜ 自适应: {cover2} 条 ｜ 提速 {speedup:+.1f}%")
    print(f"地形统计: {terrain_stats['events']} 条沉积 "
          f"(成功 {terrain_stats['success_traces']} / "
          f"失败 {terrain_stats['failed_traces']})，"
          f"净刻蚀深度 {terrain_stats['net_depth']}")


async def cmd_demo(args: argparse.Namespace) -> None:
    """一键演示：起靶场 → 扫描 → 报告 → 修复 → 回归 → 验收对比。"""
    from target_lab import (build_default_guards_file, planted_categories,
                            start_lab)
    os.makedirs(args.out, exist_ok=True)
    guards_file = os.path.join(args.out, "lab_guards.yml")
    build_default_guards_file(guards_file)
    lab = start_lab(guards_file=guards_file, port=args.port)
    planted_count = len(planted_categories())
    print(f"① 靶场已启动（{planted_count} 个埋入漏洞全部开启，"
          f"含电商/教育/金融/SaaS 业务场景）: {lab.base_url}")

    cfg = ScanConfig.from_dict({
        "profile": "quick",
        "target": {"name": "demo-ecommerce-lab", "type": "lab",
                   "base_url": lab.base_url, "guards_file": guards_file},
        "vectors": {"variants_per_sample": 2, "seed": 42},
        "detector": {"baseline": True},
        "blueteam": {"enabled": True, "sandbox_dir": os.path.join(args.out, "sandbox")},
        "adaptive": {"enabled": True},
        "storage": {"db_path": os.path.join(args.out, "redteam.db"),
                    "audit_dir": os.path.join(args.out, "audit")},
        "out_dir": os.path.join(args.out, "reports"),
    })
    from .runtime import RedTeamRuntime
    from .engine import ScanRunner, build_adapter
    from .blueteam import BlueEngine
    runtime = RedTeamRuntime(cfg)
    await runtime.start()
    adapter = None
    try:
        adapter = build_adapter(cfg)
        # ② 红队扫描
        runner = ScanRunner(runtime, cfg, adapter, scan_mode="demo")
        result = await runner.run()
        found = {f.category for f in result.findings}
        planted = planted_categories()
        rate = len(found & planted) / len(planted) * 100
        print(f"② 红队扫描：{result.success_count} 次攻击成功，"
              f"发现 {len(result.findings)} 条漏洞（{len(found & planted)}/"
              f"{len(planted)} 类埋入漏洞，发现率 {rate:.0f}%）")
        _print_scan_summary(result)
        # ③ 蓝队修复 + 回归
        print("\n③ 蓝队修复……")
        blue = BlueEngine(runtime, cfg, adapter, result)
        outcome = await blue.run(apply_fixes=True)
        summary = outcome.summary()
        print(f"   修复方案 {summary['plans']} 条 → 应用 {summary['applied']} 条"
              f" → 回归通过 {summary['verified']} 条")
        # ④ 复扫验收（同一攻击重跑，应 0 命中）
        print("\n④ 修复后复扫验收……")
        adapter2 = build_adapter(cfg)
        runner2 = ScanRunner(runtime, cfg, adapter2, scan_mode="demo-verify")
        result2 = await runner2.run()
        await adapter2.close()
        print(f"   复扫命中：{result2.success_count} 条"
              f"（修复前 {result.success_count} 条）")
        if result2.success_count == 0:
            print("\n  🎉 验收通过：修复后同一攻击重跑 0 命中，闭环完整。")
        else:
            print("\n  ⚠️ 复扫仍有命中，请检查修复与回归结果。")
    finally:
        if adapter is not None:
            await adapter.close()
        await runtime.close()
        lab.stop()


def cmd_samples(args: argparse.Namespace) -> None:
    from ._compat import ensure_dsh
    ensure_dsh()
    from .vectors.registry import VectorRegistry
    from dsh.kernel import Context
    ctx = Context("samples")
    registry = VectorRegistry(ctx, {})
    registry.apply(ctx)
    if args.action == "list":
        print(f"攻击样本库：{len(registry.samples)} 条基础样本，"
              f"{len(registry.categories())} 个分类\n")
        for category in registry.categories():
            samples = [s for s in registry.samples if s.category == category]
            surfaces = {s.surface for s in samples}
            print(f"  {category:<22} {len(samples):>3} 条  "
                  f"[{'/'.join(sorted(surfaces))}]")
        print("\n用法: dsh-redteam samples show <category>")
        return
    category = args.category
    if category not in registry.categories():
        raise RedTeamError(f"分类不存在: {category}（见 samples list）")
    for sample in registry.samples:
        if sample.category != category:
            continue
        print(f"  {sample.id}  [{sample.severity}] {sample.name}")
        print(f"      OWASP: {sample.owasp} ｜ 面: {sample.surface} "
              f"｜ 角色: {','.join(sample.role_context) or '全部'}")
        print(f"      载荷: {sample.payload.strip()[:100]}")
        if sample.evidence_patterns:
            print(f"      证据: {sample.evidence_patterns}")


def cmd_scenarios(args: argparse.Namespace) -> None:
    from .scenarios import SCENARIOS, sample_categories_for
    if args.action == "list":
        print(f"业务场景库：{len(SCENARIOS)} 大业务场景\n")
        for scenario in SCENARIOS:
            print(f"  {scenario.id:<14} {scenario.name}")
            print(f"      指纹: {', '.join(scenario.path_keywords[:6])}…")
            print(f"      专属样本类别: {', '.join(scenario.sample_categories)}")
        print("\n用法: dsh-redteam scenarios show <scenario_id>")
        return
    from .scenarios import get_scenario
    scenario = get_scenario(args.scenario or "")
    if scenario is None:
        raise RedTeamError(f"场景不存在: {args.scenario}（见 scenarios list）")
    print(f"{scenario.name}（{scenario.id}）\n  {scenario.description}")
    print(f"  路径指纹: {scenario.path_keywords}")
    print(f"  端点指纹: {scenario.endpoint_keywords}")
    print(f"  内容指纹: {scenario.content_keywords}")
    print(f"  专属样本类别: {scenario.sample_categories}")


async def cmd_batch(args: argparse.Namespace) -> None:
    """多目标批扫：targets.yml 串行扫描 + 风险排序汇总报告。

    targets.yml 格式: 每个条目 = 完整扫描配置 dict（可用 name 覆盖目标名）:
        - name: 电商系统
          target: { type: lab, base_url: "http://127.0.0.1:8765", ... }
          vectors: { categories: [all], variants_per_sample: 1 }
        - name: 内部服务(静态)
          target: { type: folder, folder_path: "./src" }
    """
    import yaml as _yaml
    if not os.path.exists(args.targets):
        raise RedTeamError(f"目标清单不存在: {args.targets}")
    with open(args.targets, "r", encoding="utf-8") as fh:
        entries = _yaml.safe_load(fh) or []
    if not isinstance(entries, list) or not entries:
        raise RedTeamError("targets.yml 必须是目标配置列表")
    from .runtime import RedTeamRuntime
    from .engine import ScanRunner, build_adapter
    results = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise RedTeamError(f"第 {index} 个目标配置必须是映射")
        name = str(entry.pop("name", f"target-{index}"))
        cfg = ScanConfig.from_dict(entry)
        cfg.out_dir = os.path.join(args.out, "reports")
        print(f"\n[{index}/{len(entries)}] 扫描目标: {name}（{cfg.target.type}）")
        runtime = RedTeamRuntime(cfg)
        await runtime.start()
        adapter = None
        try:
            adapter = build_adapter(cfg)
            result = await ScanRunner(runtime, cfg, adapter,
                                      scan_mode=f"batch-{index}").run()
            severity_counts = result.severity_counts()
            # 风险评分：critical×10 + high×5 + medium×2 + low×1
            score = (severity_counts.get("critical", 0) * 10
                     + severity_counts.get("high", 0) * 5
                     + severity_counts.get("medium", 0) * 2
                     + severity_counts.get("low", 0))
            results.append({"name": name, "type": cfg.target.type,
                            "scan_id": result.scan_id, "score": score,
                            "total": result.total,
                            "success": result.success_count,
                            "findings": len(result.findings),
                            "severity_counts": severity_counts,
                            "report": result.report_path})
            print(f"  命中 {result.success_count} / 发现 "
                  f"{len(result.findings)} 条漏洞 / 风险评分 {score}")
        finally:
            if adapter is not None:
                await adapter.close()
            await runtime.close()
    _write_batch_summary(results, args.out)


def _write_batch_summary(results: List[Dict[str, Any]], out_dir: str) -> None:
    """风险排序汇总报告（按风险评分降序）。"""
    import time as _time
    os.makedirs(out_dir, exist_ok=True)
    stamp = _time.strftime("%Y%m%d%H%M%S")
    path = os.path.join(out_dir, f"batch_summary_{stamp}.md")
    ranked = sorted(results, key=lambda r: -r["score"])
    lines = [f"# 多目标批扫汇总报告",
             f"\n> 生成时间: {_time.strftime('%Y-%m-%dT%H:%M:%S%z')} ｜ "
             f"目标数: {len(results)}\n",
             "## 风险排序\n",
             "| 排名 | 目标 | 类型 | 风险评分 | 命中 | 漏洞数 | 严重/高/中/低 |",
             "|:---:|:---|:---:|:---:|:---:|:---:|:---:|"]
    for index, row in enumerate(ranked, start=1):
        counts = row["severity_counts"]
        lines.append(
            f"| {index} | {row['name']} | {row['type']} | **{row['score']}** "
            f"| {row['success']} | {row['findings']} | "
            f"{counts.get('critical', 0)}/{counts.get('high', 0)}/"
            f"{counts.get('medium', 0)}/{counts.get('low', 0)} |")
    lines.append("\n## 明细\n")
    for row in ranked:
        lines.append(f"- **{row['name']}**（{row['scan_id']}）: "
                     f"报告 {row['report']}")
    content = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"\n批扫完成：{len(results)} 个目标，汇总报告 {path}")
    if ranked:
        print(f"风险最高目标: {ranked[0]['name']}（评分 {ranked[0]['score']}）")


async def cmd_schedule(args: argparse.Namespace) -> None:
    """定时扫描：周期执行扫描 + 报告按时间留存 + 可选 webhook 推送。"""
    import time as _time
    interval_s = _parse_interval(args.every)
    from .runtime import RedTeamRuntime
    from .engine import ScanRunner, build_adapter
    import httpx as _httpx
    os.makedirs(args.out, exist_ok=True)
    cfg = ScanConfig.from_yaml(args.config)
    print(f"定时扫描已启动：每 {args.every} 一次（Ctrl+C 停止），"
          f"报告留存 {args.out}")
    while True:
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(args.out, f"scan_{stamp}")
        os.makedirs(run_dir, exist_ok=True)
        cfg.out_dir = os.path.join(run_dir, "reports")
        cfg.storage.db_path = os.path.join(run_dir, "scan.db")
        cfg.storage.audit_dir = os.path.join(run_dir, "audit")
        runtime = RedTeamRuntime(cfg)
        await runtime.start()
        adapter = None
        try:
            adapter = build_adapter(cfg)
            result = await ScanRunner(runtime, cfg, adapter,
                                      scan_mode=f"schedule-{stamp}").run()
            print(f"[{stamp}] 扫描完成：命中 {result.success_count}，"
                  f"发现 {len(result.findings)} 条漏洞 → {result.report_path}")
            if args.webhook:
                payload = {"scan_id": result.scan_id,
                           "target": result.target,
                           "timestamp": result.finished_at,
                           "success": result.success_count,
                           "findings": len(result.findings),
                           "severity_counts": result.severity_counts(),
                           "report": result.report_path}
                try:
                    async with _httpx.AsyncClient(timeout=10.0) as client:
                        await client.post(args.webhook, json=payload)
                except Exception as exc:
                    print(f"  webhook 推送失败: {exc}")
        finally:
            if adapter is not None:
                await adapter.close()
            await runtime.close()
        if args.once:
            break
        await asyncio.sleep(interval_s)


def _parse_interval(text: str) -> float:
    """解析周期：30m/1h/24h/5s → 秒。"""
    import re as _re
    match = _re.fullmatch(r"(\d+)\s*(s|m|h|d)", str(text).strip().lower())
    if not match:
        raise RedTeamError(f"无法解析周期: {text!r}（如 30m/1h/24h/5s）")
    value, unit = int(match.group(1)), match.group(2)
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def cmd_web(args: argparse.Namespace) -> None:
    """Web 面板：dsh-redteam web [--config scan.yml] [--with-lab]（阻塞运行）"""
    lab = None
    guards_file = ""
    runtime_dir = os.path.join(os.getcwd(), "web_runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    if args.config and not args.with_lab:
        cfg = ScanConfig.from_yaml(args.config)
    else:
        # 默认模式：自动起内置靶场作为目标
        from target_lab import build_default_guards_file, start_lab
        out_dir = runtime_dir
        guards_file = os.path.join(out_dir, "lab_guards.yml")
        build_default_guards_file(guards_file)
        lab = start_lab(guards_file=guards_file, port=0)
        base = {"name": "web-demo-lab", "type": "lab",
                "base_url": lab.base_url, "guards_file": guards_file}
        if args.config:   # --config + --with-lab：沿用配置但指向内置靶场
            cfg = ScanConfig.from_yaml(args.config)
            cfg.target.type = "lab"
            cfg.target.base_url = lab.base_url
            cfg.target.guards_file = guards_file
        else:
            cfg = ScanConfig.from_dict({
                "profile": "quick",
                "target": base,
                "vectors": {"variants_per_sample": 1},
                "blueteam": {"enabled": True,
                             "sandbox_dir": os.path.join(out_dir, "sandbox")},
                "storage": {"db_path": os.path.join(out_dir, "web.db"),
                            "audit_dir": os.path.join(out_dir, "audit")},
                "out_dir": os.path.join(out_dir, "reports"),
                "engine": {"concurrency": 8, "min_interval_ms": 5},
            })
    from .web.panel import _llm_available, create_app
    app = create_app(cfg, runtime_dir, lab=lab)
    import uvicorn
    print(f"🔴🔵 dsh-red-blue-team Web 面板: http://{args.host}:{args.port}")
    print(f"   默认目标: {cfg.target.name}（{cfg.target.base_url or cfg.target.folder_path}）")
    print(f"   面板支持: 输入任意网址扫描 / 上传项目 ZIP 解析 / 任务步骤与日志追踪")
    if _llm_available():
        print(f"   LLM: 已就绪（{os.environ.get('DEEPSEEK_MODEL', 'DeepSeek')}）"
              f"——自主攻击/主动侦察/修复建议开关可用")
    else:
        print("   LLM: 未配置（仅确定性引擎，LLM 开关自动禁用）")
    # info 级日志：控制台实时显示每次 API 请求与任务日志（排查面板问题时可见）
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())
