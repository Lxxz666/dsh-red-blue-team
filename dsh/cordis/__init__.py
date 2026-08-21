"""
dsh.cordis —— 自指 cordis：动态 Cordis Plugin 运行器（对应 TS 版
cordis-host-runner + tool-cordis 的 host-only 子集）。

- ``dsh/cordis/types.py``：客户端安全的线协议词汇（运行尝试/半状态/诊断/收据）；
- ``dsh/cordis/registry.py``：不可变包版本注册表 + 身份铸造 + 挂起请求；
- ``dsh/cordis/sandbox.py``：host 半的沙箱（async 函数体 + ctx/harness/console
  注入 + AST 级危险导入拒绝——非容器，文档化信任立场）；
- ``dsh/cordis/inspect.py``：只读 inspect 提供者目录（host 内建 + 可注册）；
- ``dsh/cordis/runner.py``：DynamicCordisRunnerService（ctx.dynamicCordisRunner）：
  define/undefine/run/stop/list/inspect/snapshot/invoke + 审批门控 + 事件；
- ``dsh/cordis/tools.py``：cordis_define/run/stop/undefine/inspect_list/
  inspect_query/inspect_self 七工具。

边界（文档化）：dsh_python 无浏览器 client 运行时——client 半仅存储/检查，
带 client 代码的包定义成功但运行被拒（client-half-failed，指引宿主端替代）；
审批门控由 ``requires_approval`` 配置打开，经 ctx.approval 完成
（awaiting-approval 状态 + cordis/request-run(-resolved) 事件）。
"""
from .inspect import CordisInspectRegistryService  # noqa: F401
from .registry import DynamicCordisRegistry  # noqa: F401
from .runner import DynamicCordisRunnerService  # noqa: F401
from .sandbox import HOST_BUILTIN_INSPECTION, precheck_code  # noqa: F401
