"""
dsh.code —— Code Mode（代码模式）：run_code 传输 + 代码执行 seam + SDK 渲染。

对应 TS 版 dsh-tools Code Mode + dsh-code-runtime：

- ``dsh/code/runtime.py``：CodeRuntime（ctx.codeRuntime）——对模型编写的
  Python 程序执行，捕获 print 输出与返回值（in-process 后端，language='python'）；
- ``dsh/code/sdk.py``：SDK 提示分节渲染（Python 风格：TypedDict + Tools
  Protocol + tools 单例）；
- ``dsh/code/mode.py``：保留的 ``run_code`` 传输 + 子调用派发桥
  （tool/code-dispatch-start、tool/code-dispatch 事件与
  tools/code-dispatch-log waterfall）。

设计细节见 manuals/18-CodeMode。
"""
from .mode import CodeRunFailedError, RUN_CODE_NAME  # noqa: F401
from .runtime import (CodeRunFailure, CodeRunResult, CodeRuntime,  # noqa: F401
                      CodeToolCallError)
