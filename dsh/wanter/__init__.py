"""dsh.wanter —— wanter（water+ant）动力学引擎。"""
from .coordinator import (HashCoordinator, HttpEmbeddingCoordinator,
                          MockEmbeddingCoordinator, build_coordinator)
from .engine import WanterEngine
from .erosion import Eroder, StagnationDetector
from .flow import descend, gradient_step, langevin_step, softmin_weights
from .plugin import ToolWanterGoalsPlugin, WanterPlugin
from .terrain import Terrain
from .trace import TraceField

__all__ = [
    "WanterEngine", "WanterPlugin", "ToolWanterGoalsPlugin",
    "Terrain", "TraceField", "StagnationDetector", "Eroder",
    "gradient_step", "langevin_step", "softmin_weights", "descend",
    "HashCoordinator", "MockEmbeddingCoordinator",
    "HttpEmbeddingCoordinator", "build_coordinator",
]
