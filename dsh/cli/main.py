"""
dsh.cli.main —— dsh-python 命令行入口。

子命令:
    web [--port 3080] [--workspace DIR] [--mock]         启动 Web UI
    headless "<prompt>" [--workspace DIR] [--mock]       一次性运行并打印最终答案
    plugin init <name> | plugin list                     管理 profile
    --dump-config [--profile NAME]                       打印组合后的配置树
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any, List, Optional

from .. import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh-python",
        description="DeepSeek Harness 的 Python 实现：一切皆插件的智能体框架")
    parser.add_argument("--profile", default=None,
                        help="profile 名（web/headless 自动初始化）")
    parser.add_argument("--workspace", default=None, help="工作区根目录")
    parser.add_argument("--mock", action="store_true",
                        help="禁用 DeepSeek，只保留 mock 适配器")
    parser.add_argument("--provider", default=None, help="默认 provider 路由")
    parser.add_argument("--model", default=None, help="默认模型 id")
    parser.add_argument("--dump-config", action="store_true",
                        help="打印组合后的配置树并退出")
    sub = parser.add_subparsers(dest="command")

    def add_common(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("--workspace", default=None,
                                help="工作区根目录")
        sub_parser.add_argument("--mock", action="store_true",
                                help="禁用 DeepSeek，只保留 mock 适配器")
        sub_parser.add_argument("--provider", default=None,
                                help="默认 provider 路由")
        sub_parser.add_argument("--model", default=None, help="默认模型 id")

    web = sub.add_parser("web", help="启动 Web UI")
    add_common(web)
    web.add_argument("--port", type=int, default=3080)
    web.add_argument("--host", default="127.0.0.1")

    headless = sub.add_parser("headless", help="一次性无界面运行")
    add_common(headless)
    headless.add_argument("prompt", help="任务描述")
    headless.add_argument("--max-tokens", type=int, default=None)

    plugin = sub.add_parser("plugin", help="管理 profile 插件")
    plugin.add_argument("action", choices=["init", "list", "path"])
    plugin.add_argument("name", nargs="?", default=None)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    setup_logging()
    # Windows 控制台默认 GBK，重配 UTF-8 避免中文输出乱码
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    profile = args.profile or ("web" if args.command != "headless" else "headless")

    if args.dump_config:
        from ..config.profile import compose, dump_config
        tree, _ = compose(profile)
        print(dump_config(tree))
        return 0

    if args.command == "plugin":
        return _plugin_cmd(args)

    if args.command == "web":
        from ..server.app import run_server
        asyncio.run(run_server(profile=profile, workspace=args.workspace,
                               mock=args.mock, provider=args.provider,
                               model=args.model, host=args.host,
                               port=args.port))
        return 0

    if args.command == "headless":
        asyncio.run(run_headless(prompt=args.prompt, profile=profile,
                                 workspace=args.workspace, mock=args.mock,
                                 provider=args.provider, model=args.model,
                                 max_tokens=args.max_tokens))
        return 0

    parser.print_help()
    return 1


def _plugin_cmd(args: argparse.Namespace) -> int:
    from ..config.profile import (init_profile, profile_dir, profiles_dir)
    if args.action == "init":
        name = args.name or "web"
        init_profile(name)
        print(f"profile {name!r} initialized at {profile_dir(name)}")
        return 0
    if args.action == "path":
        print(profile_dir(args.name or "web"))
        return 0
    if args.action == "list":
        directory = profiles_dir()
        if not directory.exists():
            print("(no profiles)")
            return 0
        for child in sorted(directory.iterdir()):
            if child.is_dir():
                print(child.name)
        return 0
    return 1


async def run_headless(prompt: str, profile: str, workspace: Optional[str],
                       mock: bool, provider: Optional[str],
                       model: Optional[str], max_tokens: Optional[int]) -> None:
    """headless：创建 agent → followup → 等静默 → 打印最终助手文本。"""
    from ..boot import boot

    ctx, tree = await boot(profile="headless", workspace=workspace,
                           mock_llm=mock, provider=provider, model=model)
    try:
        agent = await ctx.agents.create()
        agent.followup(prompt)
        await agent.when_idle()
        await asyncio.sleep(0.05)
        await ctx.sessions.flush(agent.session)
        messages = agent.session.derive_messages()
        assistant = [m for m in messages if m.role == "assistant"]
        if assistant:
            print(assistant[-1].plain_text())
        else:
            print("(no assistant reply)")
    finally:
        for agent in ctx.agents.list():
            from ..agent import AgentHandle
            await AgentHandle(agent).dispose()
        await tree.dispose()
