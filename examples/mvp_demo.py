"""MVP 验证：自定义插件挂载 + 完整 turn 流程一次跑通。"""
import asyncio
import sys

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

from dsh.boot import boot
from dsh.llm.mock import MockAdapter


async def main():
    # 1) 自定义：用 --patch 层把 examples/my_plugin.py 挂进树（不改框架代码）
    ctx, tree = await boot(profile="headless", workspace=".",
                           mock_llm=True,
                           extra_patches=[([{
                               "insert": [{
                                   "id": "my-plugin",
                                   "plugin": "examples.my_plugin:MyPlugin",
                                   "config": {"greeting": "MVP"}}]}],
                               "user-patch")])
    try:
        # 2) 自定义生效验证：插件注册的工具出现在 ctx.tools
        assert ctx.tools.get("greet") is not None, "greet 工具未注册"
        print("[MVP] 自定义插件已挂载: greet 工具在册 ✓")

        # 3) MVP 完整流程：脚本化模型 → 调用自定义工具 → 工具结果回填
        ctx.llm.register_adapter(MockAdapter(script=[
            {"tool": {"name": "greet", "arguments": {"name": "世界"}}},
            {"text": "完成。"},
        ]))
        agent = await ctx.agents.create(options={"provider": "mock",
                                                 "model": "mock"})
        agent.followup("打个招呼")
        await agent.when_idle()
        await asyncio.sleep(0.05)

        types = [e.type for e in agent.session.events]
        assert "tool/call" in types and "tool/result" in types
        assert types[-1] == "turn/end"
        result = [e for e in agent.session.events
                  if e.type == "tool/result"][0]
        print(f"[MVP] turn 闭环: {types[0]} → … → {types[-1]}（{len(types)} 事件）")
        print(f"[MVP] 工具结果: {result.data['content']}")

        # 4) 会话落盘（持久化缝）
        await ctx.sessions.flush(agent.session)
        print(f"[MVP] 会话已持久化: {agent.session.id}.jsonl ✓")
    finally:
        await tree.dispose()
    print("[MVP] 全部通过 ✓")


asyncio.run(main())
