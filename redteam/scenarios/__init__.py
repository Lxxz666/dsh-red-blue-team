"""redteam.scenarios —— 业务场景适配层。"""
from .registry import (SCENARIOS, BusinessScenario, detect_scenario_endpoints,
                       detect_scenario_folder, detect_scenario_text,
                       get_scenario, sample_categories_for, scenario_ids,
                       scenario_names)

__all__ = ["SCENARIOS", "BusinessScenario", "detect_scenario_folder",
           "detect_scenario_endpoints", "detect_scenario_text", "get_scenario",
           "sample_categories_for", "scenario_ids", "scenario_names"]
