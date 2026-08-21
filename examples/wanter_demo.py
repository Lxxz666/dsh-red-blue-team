"""
wanter 双势阱实验可视化：纯 Python 生成 SVG（无第三方依赖）。

运行: python examples/wanter_demo.py
产出: examples/wanter_double_well.svg（地形剖面 + 水滴轨迹 + 侵蚀点）
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dsh.wanter import Eroder, StagnationDetector, Terrain
from dsh.wanter.flow import langevin_step

# ---- 场景 ----
GOAL = 3.0
TRAP = 1.0
SIGMA = 0.8
TERRAIN = Terrain(dim=1, goal=(GOAL,), sigma=SIGMA)
TERRAIN.add_bump((TRAP,), height=-2.0)   # 局部洼地
TERRAIN.add_bump((2.0,), height=+1.5)   # 势垒

# ---- 模拟（带侵蚀） ----
def simulate(steps=12000):
    rng = random.Random(42)
    detector = StagnationDetector(window=30, delta=0.2)
    eroder = Eroder(depth=0.4, anneal=0.995, radius=0.8)
    x = (0.5,)
    path = [x[0]]
    erosion_points = []
    for step in range(steps):
        x = langevin_step(x, TERRAIN, lr=0.05, D=0.005, rng=rng)
        path.append(x[0])
        detector.observe(x)
        if step % 20 == 0 and detector.stagnated():
            depth = eroder.erode(TERRAIN, x)
            erosion_points.append((x[0], depth))
            detector.reset()
        if abs(x[0] - GOAL) < 0.4:
            break
    return path, erosion_points, eroder.erosion_count


def main():
    path, erosion_points, erosion_count = simulate()
    print(f"水滴终点: x={path[-1]:.3f}（目标 {GOAL}）")
    print(f"侵蚀次数: {erosion_count}")

    # ---- SVG 生成 ----
    width, height = 900, 460
    x_min, x_max = -0.5, 4.2
    y_min, y_max = -3.0, 2.2

    def px(x):
        return 60 + (x - x_min) / (x_max - x_min) * (width - 120)

    def py(y):
        return height - 60 - (y - y_min) / (y_max - y_min) * (height - 120)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
             f'height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#0f1115"/>',
             '<text x="30" y="36" fill="#e6e9f0" font-size="18" '
             'font-family="monospace">wanter 双势阱实验：水流 + 侵蚀逃逸</text>',
             f'<text x="30" y="58" fill="#8b93a7" font-size="12" '
             f'font-family="monospace">局部阱@{TRAP} · 势垒@2.0 · '
             f'全局阱@{GOAL} · 侵蚀 {erosion_count} 次</text>']

    # 地形剖面（含侵蚀后的最终地形）
    curve = []
    n = 240
    for i in range(n + 1):
        x = x_min + (x_max - x_min) * i / n
        y = TERRAIN.phi((x,))
        curve.append(f"{px(x):.1f},{py(y):.1f}")
    parts.append(f'<polyline fill="none" stroke="#4f8cff" stroke-width="2" '
                 f'points="{" ".join(curve)}"/>')

    # 目标与洼地标记
    for marker_x, color, label in [(GOAL, "#4ade80", "目标势阱"),
                                   (TRAP, "#ff6b6b", "局部洼地")]:
        y = TERRAIN.phi((marker_x,))
        parts.append(f'<circle cx="{px(marker_x):.1f}" cy="{py(y):.1f}" r="5" '
                     f'fill="{color}"/>')
        parts.append(f'<text x="{px(marker_x) + 8:.1f}" y="{py(y) - 8:.1f}" '
                     f'fill="{color}" font-size="11" font-family="monospace">'
                     f'{label}</text>')

    # 水滴轨迹（每 10 步取一点）
    traj = []
    for i in range(0, len(path), 10):
        x = path[i]
        traj.append(f"{px(x):.1f},{py(TERRAIN.phi((x,))):.1f}")
    parts.append(f'<polyline fill="none" stroke="#7a5cff" stroke-width="1.5" '
                 f'opacity="0.9" points="{" ".join(traj)}"/>')
    parts.append(f'<circle cx="{px(path[-1]):.1f}" '
                 f'cy="{py(TERRAIN.phi((path[-1],))):.1f}" r="6" '
                 f'fill="#7a5cff"/>')

    # 侵蚀点（橙色 ✕）
    for ex, _depth in erosion_points[::2]:
        parts.append(f'<text x="{px(ex):.1f}" y="{py(TERRAIN.phi((ex,)) + 0.35):.1f}" '
                     f'fill="#ffa94d" font-size="13" font-family="monospace">✕</text>')

    # 图例
    legend = [
        ("#4f8cff", "势能地形 Φ(x)"),
        ("#7a5cff", "水滴轨迹（Langevin）"),
        ("#ffa94d", "侵蚀点（环形挖 rim）"),
    ]
    for i, (color, text) in enumerate(legend):
        y = 92 + i * 20
        parts.append(f'<rect x="{width - 220}" y="{y - 10}" width="14" '
                     f'height="14" fill="{color}"/>')
        parts.append(f'<text x="{width - 198}" y="{y}" fill="#e6e9f0" '
                     f'font-size="12" font-family="monospace">{text}</text>')

    parts.append("</svg>")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "wanter_double_well.svg")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    print(f"SVG 已生成: {out}")


if __name__ == "__main__":
    main()
