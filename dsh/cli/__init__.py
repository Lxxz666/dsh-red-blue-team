"""dsh.cli —— 命令行入口。"""
from .main import build_parser, main, run_headless

__all__ = ["main", "build_parser", "run_headless"]
