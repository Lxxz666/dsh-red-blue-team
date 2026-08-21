"""redteam.adapters —— 目标适配层。

统一抽象"如何与目标系统对话"，攻击引擎不关心目标是 HTTP API、
内置靶场还是 SDK 直连。
"""
from .base import CapabilityProbe, TargetAdapter, TargetResponse
from .http_adapter import HttpAdapter
from .sdk_adapter import SdkAdapter

__all__ = ["TargetAdapter", "TargetResponse", "CapabilityProbe",
           "HttpAdapter", "SdkAdapter"]
