"""
examples.my_plugin —— 示例插件：一个最小工具 + 一个 hook + 一个提示分节。

挂载方式（任选其一）:

1. 在你的 profile 的 cordis.patch.yml 里 insert 本行::

       - insert:
         - id: my-plugin
           plugin: examples.my_plugin:MyPlugin
           config: { greeting: "你好" }

2. 或直接写进自己的 bundle yml。
"""
from typing import Any, Dict, List, Optional

from dsh.kernel import Service
from dsh.prompt import PromptSection
from dsh.tools import define_tool


def build_greeting_tool(greeting: str, tool_name: str = "greet"):
    """构造一个问候工具（闭包携带配置）。"""

    @define_tool(name=tool_name, description="向某个名字问好",
                 parameters={"name": {"type": "string", "required": True}},
                 output={"type": "string"})
    async def greet(args, run_ctx):
        return f"{greeting}，{args['name']}！"

    return greet


class MyPlugin(Service):
    """
    示例插件：注册 1 个工具 + 1 个提示分节 + 1 个 hook。

    ``provides`` 为 None 表示不提供新服务；``inject`` 声明依赖的服务
    （Loader 会等这些服务就绪后再挂载本插件）。
    """

    inject = ("tools", "systemPrompt")

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        config = self.config or {}
        greeting = config.get("greeting", "你好")
        tool_name = config.get("tool", "greet")
        # 1) 工具
        self._disposers.append(
            ctx.tools.register(build_greeting_tool(greeting, tool_name)))
        # 2) 提示分节（order 200 位于 persona 与工具指引之后）
        self._disposers.append(ctx.systemPrompt.section(
            PromptSection(name="my-plugin:note", order=200,
                          text=f"本会话安装了示例插件（问候语: {greeting}）。")))
        # 3) hook：工具执行前打印（观察，不拦截）
        async def pre_execute_hook(execution, next):
            print(f"[my-plugin] 工具 {execution.name} 即将执行")
            return await next()
        self._disposers.append(ctx.on("tools/pre-execute", pre_execute_hook))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
