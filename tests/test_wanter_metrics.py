"""wanter 指标回归测试：量化指标的 CI 守护（防调参回退）。

用固定种子的子集复跑 examples/wanter_benchmark 的实验函数，
对关键指标加安全余量断言。若这里失败，说明 wanter 的核心性质退化。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from examples.wanter_benchmark import (ab_benchmark, learning_curve,
                                       perf_metrics)

AB_SEEDS = list(range(6))  # 完整基准用 20 种子，测试用 6 加速


def test_ab_escape_rates():
    report = ab_benchmark(AB_SEEDS)
    # wanter 的核心卖点：侵蚀使逃逸率显著高于「纯随机探索」
    assert report["wanter"]["escape_rate"] >= 0.85
    assert report["B"]["escape_rate"] <= 0.1     # 噪声本身几乎逃不掉
    assert report["A"]["escape_rate"] == 0.0     # 纯梯度必然被困
    # 逃逸后落入目标盆地（最终落点接近目标 3.0 而非陷阱 1.2）
    assert abs(report["wanter"]["final_x_mean"] - 3.0) < 0.8
    assert report["wanter"]["mean_erosion_count"] >= 1


def test_learning_curve_slope():
    _steps_no, slope_no = learning_curve(with_deposit=False)
    _steps_yes, slope_yes = learning_curve(with_deposit=True)
    # 水迹沉积带来显著加速（斜率更负，且差距远超噪声）
    assert slope_yes < slope_no - 5


def test_perf_and_evaporation_metrics():
    perf = perf_metrics()
    # 容量上限生效
    assert perf["cap_events"] == 100
    # 查询耗时随事件数增长（聚合后仍可控：5000 事件 < 20ms）
    assert perf["density_0"] < perf["density_1000"] < perf["density_5000"]
    assert perf["density_5000"] < 20_000  # µs
    # 蒸发半衰期与理论值一致（1% 内）
    assert abs(perf["half_life_measured"] - perf["half_life_theory"]) \
        < perf["half_life_theory"] * 0.01
