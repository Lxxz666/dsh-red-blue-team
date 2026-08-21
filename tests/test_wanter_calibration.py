"""wanter 语义坐标校准回归守护：oracle 语义嵌入必须显著优于 hash 伪嵌入。"""
from dsh.wanter.calibration import (OracleEmbeddingProvider,
                                    build_calibration_terrain,
                                    run_calibration,
                                    run_matching_experiment)
from dsh.wanter.coordinator import HashCoordinator


def test_calibration_terrain_and_oracle_alignment():
    from dsh.wanter.calibration import DEFAULT_GOALS
    build_calibration_terrain()  # 构造不抛错（高斯洼地）
    oracle = OracleEmbeddingProvider()
    for i, label in enumerate(["写后端接口", "修前端样式", "数据库调优",
                               "部署流水线"]):
        point = oracle.embed(label)
        # oracle 起点必须离「自己的目标」最近（与目标布局对齐）
        distances = [sum((point[k] - goal[k]) ** 2
                         for k in range(2)) ** 0.5
                     for goal in DEFAULT_GOALS]
        assert distances.index(min(distances)) == i


def test_calibration_oracle_beats_hash():
    """快速子集（2 种子）：oracle 匹配率 > hash 匹配率 + 安全余量。"""
    metrics = run_calibration(seeds=(0, 1))
    assert metrics["hash"]["matching"] < 0.8
    assert metrics["oracle"]["matching"] >= 0.9
    assert metrics["oracle"]["matching"] > \
        metrics["hash"]["matching"] + 0.3
    assert metrics["oracle"]["mean_steps"] < \
        metrics["hash"]["mean_steps"]
