"""
wanter 展示图表生成（纯 Python SVG，无第三方依赖）。

运行: python examples/wanter_charts.py
产出: examples/wanter_showcase.svg（四面板：逃逸率 / 学习曲线 / 语义校准 /
性能与半衰期）——README 与 WANTER.md 的展示图。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
BG = "#0f1115"
FG = "#e6e9f0"
MUTED = "#8b93a7"


def _panel(x: int, y: int, w: int, h: int, title: str) -> str:
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" '
            f'fill="#12151c" stroke="#232836"/>\n'
            f'<text x="{x + 14}" y="{y + 26}" fill="{FG}" font-size="15" '
            f'font-family="monospace">{title}</text>')


def _bar(x: int, base: int, value: float, vmax: float, color: str,
         label: str, w: int = 56) -> str:
    h = value / vmax * 120
    return (f'<rect x="{x}" y="{base - h:.1f}" width="{w}" height="{h:.1f}" '
            f'fill="{color}"/>\n'
            f'<text x="{x + w / 2:.0f}" y="{base - h - 6:.0f}" fill="{FG}" '
            f'font-size="12" font-family="monospace" text-anchor="middle">'
            f'{label}</text>')


def main():
    metrics = json.load(open(os.path.join(HERE, "wanter_metrics.json"),
                             encoding="utf-8"))
    try:
        calib = json.load(open(os.path.join(
            HERE, "wanter_embedding_metrics.json"), encoding="utf-8"))
    except OSError:
        calib = None
    ab = metrics["ab"]
    learn = metrics["learning"]
    perf = metrics["perf"]
    width, height = 980, 620
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
             f'height="{height}" viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="{BG}"/>',
             f'<text x="24" y="30" fill="{FG}" font-size="17" '
             f'font-family="monospace">wanter（water+ant）量化指标总览</text>',
             f'<text x="24" y="50" fill="{MUTED}" font-size="12" '
             f'font-family="monospace">脚本复现：wanter_benchmark.py / '
             f'wanter_embedding_calibration.py（确定性种子，离线可复现）</text>']

    # 面板 1：双势阱逃逸率
    parts.append(_panel(24, 70, 296, 240, "双势阱逃逸率（20 种子）"))
    base = 70 + 240 - 34
    labels = [("A 纯梯度", ab["A"]["escape_rate"]),
              ("B 梯度+噪声", ab["B"]["escape_rate"]),
              ("wanter", ab["wanter"]["escape_rate"])]
    for i, (name, rate) in enumerate(labels):
        x = 24 + 22 + i * 92
        color = "#4ade80" if name == "wanter" else "#ff6b6b"
        parts.append(_bar(x, base, rate * 100, 100, color,
                          f"{rate * 100:.0f}%"))
        parts.append(f'<text x="{x + 28}" y="{base + 18}" fill="{MUTED}" '
                     f'font-size="11" font-family="monospace">{name}</text>')
    parts.append(f'<text x="40" y="{70 + 240 - 12}" fill="{MUTED}" '
                 f'font-size="11" font-family="monospace">'
                 f'wanter 平均 {ab["wanter"]["mean_steps_to_escape"]:.0f} 步 · '
                 f'{ab["wanter"]["mean_erosion_count"]:.1f} 次侵蚀 '
                 f'· 落点 {ab["wanter"]["final_x_mean"]:.2f}±'
                 f'{ab["wanter"]["final_x_std"]:.2f}</text>')

    # 面板 2：学习曲线（斜率对比）
    parts.append(_panel(344, 70, 296, 240, "路径复用学习斜率"))
    base = 70 + 240 - 34
    s_max = 30
    parts.append(_bar(344 + 60, base, -learn["slope_no_deposit"], s_max,
                      "#ff6b6b", f"{learn['slope_no_deposit']:.1f}"))
    parts.append(_bar(344 + 170, base, -learn["slope_with_deposit"], s_max,
                      "#4ade80", f"{learn['slope_with_deposit']:.1f}"))
    parts.append(f'<text x="432" y="{base + 18}" fill="{MUTED}" '
                 f'font-size="11" font-family="monospace">无沉积</text>')
    parts.append(f'<text x="542" y="{base + 18}" fill="{MUTED}" '
                 f'font-size="11" font-family="monospace">有沉积（≈30×）</text>')
    parts.append(f'<text x="360" y="{70 + 240 - 12}" fill="{MUTED}" '
                 f'font-size="11" font-family="monospace">'
                 f'后 5 次均值：无沉积 {learn["no_deposit_last5_mean"]:.0f} 步 '
                 f'vs 有沉积 {learn["with_deposit_last5_mean"]:.1f} 步</text>')

    # 面板 3：语义坐标校准
    parts.append(_panel(664, 70, 296, 240, "语义坐标校准（多目标匹配）"))
    if calib:
        base = 70 + 240 - 34
        parts.append(_bar(664 + 60, base,
                          calib["hash"]["matching"] * 100, 100, "#ff6b6b",
                          f'{calib["hash"]["matching"] * 100:.0f}%'))
        parts.append(_bar(664 + 170, base,
                          calib["oracle"]["matching"] * 100, 100, "#4ade80",
                          f'{calib["oracle"]["matching"] * 100:.0f}%'))
        parts.append(f'<text x="752" y="{base + 18}" fill="{MUTED}" '
                     f'font-size="11" font-family="monospace">hash 伪嵌入</text>')
        parts.append(f'<text x="862" y="{base + 18}" fill="{MUTED}" '
                     f'font-size="11" font-family="monospace">oracle 语义</text>')
        parts.append(f'<text x="680" y="{70 + 240 - 12}" fill="{MUTED}" '
                     f'font-size="11" font-family="monospace">'
                     f'平均步数：hash {calib["hash"]["mean_steps"]:.0f} '
                     f'vs oracle {calib["oracle"]["mean_steps"]:.0f}</text>')

    # 面板 4：性能与半衰期
    parts.append(_panel(24, 330, 936, 250, "性能与蒸发（半衰期 ln2/λ）"))
    rows = [
        ("density_at 查询耗时（0/1k/5k 事件）",
         f'{perf["density_0"]} / {perf["density_1000"]} / '
         f'{perf["density_5000"]} µs'),
        ("事件容量上限（max_events=100）",
         f'500 次沉积 → 钳制 {perf["cap_events"]} ✓'),
        ("迹蒸发半衰期（λ=0.2）",
         f'理论 {perf["half_life_theory"]}s · 实测 '
         f'{perf["half_life_measured"]}s（误差 <1e-4）✓'),
    ]
    for i, (k, v) in enumerate(rows):
        y = 330 + 52 + i * 40
        parts.append(f'<text x="40" y="{y}" fill="{MUTED}" font-size="12" '
                     f'font-family="monospace">{k}</text>')
        parts.append(f'<text x="420" y="{y}" fill="{FG}" font-size="12" '
                     f'font-family="monospace">{v}</text>')
    # 面板 5 位置让给「四法则」示意（文本）
    laws = [
        ("① 势能地形", "Φ = Φ_goal + Φ_base + Φ_trace + Φ_erosion"),
        ("② 水迹沉积/蒸发", "∂T/∂t = βρ − λT（旧路径更低 → 新水流优先沿旧径）"),
        ("③ Langevin 流动", "dx = −∇Φ dt + √(2D) dW（软最小 Boltzmann 选向）"),
        ("④ 洼地侵蚀", "停滞检测 → 目标方向偏置开挖 + 退火（逃出局部最优）"),
    ]
    for i, (k, v) in enumerate(laws):
        y = 330 + 52 + 120 + i * 26
        parts.append(f'<text x="40" y="{y}" fill="#7a5cff" font-size="12" '
                     f'font-family="monospace">{k}</text>')
        parts.append(f'<text x="230" y="{y}" fill="{FG}" font-size="12" '
                     f'font-family="monospace">{v}</text>')
    parts.append("</svg>")
    out = os.path.join(HERE, "wanter_showcase.svg")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    print(f"展示图已写入: {out}")


if __name__ == "__main__":
    main()
