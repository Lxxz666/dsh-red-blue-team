"""dsh.goal —— 目标域。"""
from .goal import (Goal, GoalRoundDriverPlugin, GoalService,
                   ToolGoalPlugin, build_goal_tools)

__all__ = ["Goal", "GoalService", "GoalRoundDriverPlugin",
           "ToolGoalPlugin", "build_goal_tools"]
