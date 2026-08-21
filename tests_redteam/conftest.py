"""tests_redteam 共享 fixture：靶场（全漏洞/全加固）、配置、运行时。"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from target_lab import (build_default_guards_file, build_hardened_guards_file,
                        start_lab)
from redteam.config import ScanConfig


@pytest.fixture
def vuln_lab(tmp_path):
    """弱防护靶场（23 个埋入漏洞全部开启）。"""
    guards_file = str(tmp_path / "guards.yml")
    build_default_guards_file(guards_file)
    lab = start_lab(guards_file=guards_file, port=0)
    yield lab, guards_file
    lab.stop()


@pytest.fixture
def hardened_lab(tmp_path):
    """全加固靶场（防护全部开启，回归验收对照）。"""
    guards_file = str(tmp_path / "guards_hardened.yml")
    build_hardened_guards_file(guards_file)
    lab = start_lab(guards_file=guards_file, port=0)
    yield lab, guards_file
    lab.stop()


def make_config(lab, guards_file, tmp_path, **overrides) -> ScanConfig:
    raw = {
        "profile": "quick",
        "target": {"name": "test-lab", "type": "lab",
                   "base_url": lab.base_url, "guards_file": guards_file},
        "vectors": {"variants_per_sample": 1, "seed": 42},
        "detector": {"baseline": True, "llm_judge": False},
        "blueteam": {"enabled": True,
                     "sandbox_dir": str(tmp_path / "sandbox")},
        "adaptive": {"enabled": True},
        "storage": {"db_path": str(tmp_path / "rt.db"),
                    "audit_dir": str(tmp_path / "audit")},
        "out_dir": str(tmp_path / "reports"),
        "engine": {"concurrency": 6, "min_interval_ms": 0},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and key in raw:
            raw[key].update(value)
        else:
            raw[key] = value
    return ScanConfig.from_dict(raw)
