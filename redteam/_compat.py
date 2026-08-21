"""dsh 框架依赖引导。

本项目自带 dsh-python 框架完整副本（项目根目录 ``dsh/``），
因此 ``import dsh`` 直接可用（从项目根运行或安装本包后均可）。

如使用外部安装的 dsh（例如工作区中独立克隆的 dsh_python），
可设置环境变量 ``DSH_PYTHON_PATH`` 指向该框架根目录，本模块会优先加载。
"""
from __future__ import annotations

import os
import sys


def ensure_dsh() -> None:
    try:
        import dsh  # noqa: F401
        return
    except ImportError:
        pass
    env_path = os.environ.get("DSH_PYTHON_PATH")
    if env_path:
        path = os.path.abspath(env_path)
        if os.path.isdir(os.path.join(path, "dsh")):
            if path not in sys.path:
                sys.path.insert(0, path)
            try:
                import dsh  # noqa: F401
                return
            except ImportError:
                pass
    raise ImportError(
        "无法导入 dsh 框架：本项目应自带 dsh 包副本（项目根目录 dsh/）。"
        "若使用外部框架，请设置环境变量 DSH_PYTHON_PATH 指向其根目录。")
