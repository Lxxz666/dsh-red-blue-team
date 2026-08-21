"""
wanter 量化基准（可复现：固定种子网格）。

实验设计（详见 manuals/16 §7）：

1. 双势阱 A/B 对照（20 种子）:
   - 基线 A: 纯梯度下降（无噪声、无侵蚀）
   - 基线 B: 梯度 + 噪声（Langevin，无侵蚀 = ACO 式随机探索）
   - wanter: 噪声 + 侵蚀（法则 ④）
   指标: 逃逸率 / 平均逃逸步数 / 平均侵蚀次数 / 最终落点均值±标准差

2. 路径复用学习曲线: 同一地形 15 次连续水滴，成功沿途沉积;
   对照无沉积。指标: steps-to-goal 随次数的线性回归斜率。

3. 性能与蒸发: density_at 在 0/1000/5000 事件下的查询耗时;
   容量上限生效; 蒸发半衰期 = ln2/λ 验证。

运行: python examples/wanter_benchmark.py
产出: examples/wanter_metrics.json + 控制台报告
"""
import json
import math
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dsh.wanter import Eroder, StagnationDetector, Terrain, TraceField
from dsh.wanter.flow import gradient_step, langevin_step

SEEDS = list(range(20))
MAX_STEPS = 12000


def double_well_terrain():
    terrain = Terrain(dim=1, goal=(3.0,), sigma=0.8)
    terrain.add_bump((1.0,), height=-2.0)   # 局部洼地
    terrain.add_bump((2.0,), height=+1.5)   # 势垒
    return terrain


def run_droplet(terrain, seed, *, with_noise, with_erosion):
    rng = random.Random(seed)
    detector = StagnationDetector(window=30, delta=0.2)
    eroder = Eroder(depth=0.4, anneal=0.995, radius=0.8)
    x = (0.5,)
    escaped = False
    steps_to_escape = None
    for step in range(MAX_STEPS):
        if with_noise:
            x = langevin_step(x, terrain, lr=0.05, D=0.005, rng=rng)
        else:
            x = gradient_step(x, terrain, lr=0.05)
        if with_erosion:
            detector.observe(x)
            if step % 20 == 0 and detector.stagnated():
                eroder.erode(terrain, x)
                detector.reset()
        if x[0] > 2.5 and steps_to_escape is None:
            escaped = True
            steps_to_escape = step
        if abs(x[0] - 3.0) < 0.4:
            break
    return {"escaped": escaped, "steps_to_escape": steps_to_escape,
            "final_x": x[0], "erosion_count": eroder.erosion_count}


def learning_curve(with_deposit: bool):
    """15 次连续水滴：每次成功沿途沉积（对照无沉积），返回 steps 列表。"""
    steps_list = []
    terrain = Terrain(dim=1, goal=(3.0,), sigma=0.8)
    trace = TraceField(decay_rate=0.0)
    terrain.trace_field = trace
    for run in range(15):
        rng = random.Random(1000 + run)
        x = (0.5,)
        path = []
        for step in range(MAX_STEPS):
            x = langevin_step(x, terrain, lr=0.05, D=0.05, rng=rng)
            path.append(x[0])
            if abs(x[0] - 3.0) < 0.4:
                break
        steps_list.append(step)
        if with_deposit and abs(x[0] - 3.0) < 0.4:
            for i, px in enumerate(path[::40]):
                trace.deposit((px,), weight=1.5, at=0.0)
    slope, _intercept = linear_regression(list(range(15)), steps_list)
    return steps_list, slope


def linear_regression(xs, ys):
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var = sum((x - mean_x) ** 2 for x in xs)
    slope = cov / var if var else 0.0
    return slope, mean_y - slope * mean_x


def perf_metrics():
    # density_at 查询耗时（聚合与未聚合、不同事件数）
    out = {}
    for n_events in (0, 1000, 5000):
        field = TraceField(decay_rate=0.0, aggregate=True, max_events=10000)
        rng = random.Random(7)
        for _ in range(n_events):
            field.deposit((rng.uniform(-5, 5), rng.uniform(-5, 5)),
                          weight=1.0, at=0.0)
        queries = [(rng.uniform(-5, 5), rng.uniform(-5, 5))
                   for _ in range(200)]
        start = time.perf_counter()
        for point in queries:
            field.density_at(point, sigma=1.0, now=0.0)
        elapsed = time.perf_counter() - start
        out[f"density_{n_events}"] = round(elapsed / len(queries) * 1e6, 1)  # µs
    # 容量上限
    field = TraceField(decay_rate=0.0, max_events=100)
    for i in range(500):
        field.deposit((float(i), 0.0), weight=1.0, at=0.0)
    out["cap_events"] = field.event_count()
    # 蒸发半衰期：ln2/λ
    lam = 0.2
    field2 = TraceField(decay_rate=lam)
    field2.deposit((0.0,), weight=1.0, at=0.0)
    half_life = math.log(2) / lam
    weight_at_half = field2.weights_at(now=half_life)[0]
    out["half_life_theory"] = round(half_life, 4)
    # 实测半衰期 = 权重衰减到 0.5 的时间：由 w=exp(−λt) 反解 t=−ln(w)/λ
    out["half_life_measured"] = round(-math.log(weight_at_half) / lam, 4)
    return out


def ab_benchmark(seeds=None):
    """双势阱 A/B 对照；返回 {A,B,wanter} 的指标 dict。"""
    seeds = seeds or SEEDS
    results = {"A": [], "B": [], "wanter": []}
    for seed in seeds:
        terrain = double_well_terrain()
        results["A"].append(run_droplet(terrain, seed, with_noise=False,
                                        with_erosion=False))
        terrain = double_well_terrain()
        results["B"].append(run_droplet(terrain, seed, with_noise=True,
                                        with_erosion=False))
        terrain = double_well_terrain()
        results["wanter"].append(run_droplet(terrain, seed, with_noise=True,
                                             with_erosion=True))
    report = {}
    for name in ("A", "B", "wanter"):
        runs = results[name]
        escaped = [r for r in runs if r["escaped"]]
        rate = len(escaped) / len(runs)
        steps = [r["steps_to_escape"] for r in escaped]
        report[name] = {
            "escape_rate": round(rate, 3),
            "mean_steps_to_escape": round(statistics.mean(steps), 1)
            if steps else None,
            "mean_erosion_count": round(
                statistics.mean(r["erosion_count"] for r in runs), 1),
            "final_x_mean": round(statistics.mean(r["final_x"] for r in runs), 3),
            "final_x_std": round(statistics.stdev(r["final_x"] for r in runs), 3),
        }
    return report


def main():
    # ---- 1. A/B 对照 ----
    report = {"seeds": len(SEEDS), "ab": {}, "learning": {}, "perf": {}}
    report["ab"] = ab_benchmark()
    for name in ("A", "B", "wanter"):
        row = report["ab"][name]
        print(f"[{name}] 逃逸率 {row['escape_rate']:.0%}  "
              f"平均逃逸步数 {row['mean_steps_to_escape']}  "
              f"平均侵蚀 {row['mean_erosion_count']}  "
              f"落点 {row['final_x_mean']:.2f}±{row['final_x_std']:.2f}")

    # ---- 2. 学习曲线 ----
    steps_no, slope_no = learning_curve(with_deposit=False)
    steps_yes, slope_yes = learning_curve(with_deposit=True)
    report["learning"] = {
        "no_deposit_first5_mean": round(statistics.mean(steps_no[:5]), 1),
        "no_deposit_last5_mean": round(statistics.mean(steps_no[-5:]), 1),
        "with_deposit_first5_mean": round(statistics.mean(steps_yes[:5]), 1),
        "with_deposit_last5_mean": round(statistics.mean(steps_yes[-5:]), 1),
        "slope_no_deposit": round(slope_no, 2),
        "slope_with_deposit": round(slope_yes, 2),
    }
    print(f"[学习曲线] 无沉积: 前5均值 {report['learning']['no_deposit_first5_mean']} "
          f"→ 后5均值 {report['learning']['no_deposit_last5_mean']} "
          f"(斜率 {slope_no:.2f})")
    print(f"[学习曲线] 有沉积: 前5均值 {report['learning']['with_deposit_first5_mean']} "
          f"→ 后5均值 {report['learning']['with_deposit_last5_mean']} "
          f"(斜率 {slope_yes:.2f})")

    # ---- 3. 性能与蒸发 ----
    report["perf"] = perf_metrics()
    print(f"[性能] density_at 查询耗时(us): {report['perf']['density_0']} / "
          f"{report['perf']['density_1000']} / {report['perf']['density_5000']}  "
          f"容量上限生效: {report['perf']['cap_events']} <= 100")
    print(f"[蒸发] 半衰期 理论 {report['perf']['half_life_theory']} "
          f"实测 {report['perf']['half_life_measured']}")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "wanter_metrics.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"指标已写入: {out_path}")


if __name__ == "__main__":
    main()
