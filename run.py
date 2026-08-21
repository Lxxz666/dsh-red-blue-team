"""
dsh-python 快速启动入口（PyCharm 直接运行本文件即可）。

用法:
    python run.py web                    # 启动 Web UI（http://127.0.0.1:3080）
    python run.py web --port 8080        # 指定端口
    python run.py headless "总结一下"      # 一次性无界面运行（需要 DEEPSEEK_API_KEY 或 --mock）
    python run.py --dump-config          # 打印组合后的配置树
    python run.py plugin list            # 管理 profile 插件
"""
import sys

from dsh.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
