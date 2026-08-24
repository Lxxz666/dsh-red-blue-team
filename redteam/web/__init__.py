"""redteam.web —— Web 面板（FastAPI + 原生 JS）。"""
from .panel import ScanTaskManager, create_app

__all__ = ["create_app", "ScanTaskManager"]
