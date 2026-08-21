"""
dsh.wanter.viz —— wanter 地形可视化（纯 Python 生成 SVG，无第三方依赖）。

- ``render_terrain_svg(engine, size, grid)``：2D 热力图（势能 Φ 归一化着色：
  低=绿（目标洼地），高=蓝；目标势阱绿色圆环、侵蚀坑红点、迹事件紫点抽样）；
  1D 回退为剖面曲线（与 examples/wanter_demo.py 同风格）；dim>2 取前两轴
  并注明投影。
- 消费方：Web 地形面板（GET /api/wanter/terrain）与本地诊断。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Tuple

_GOAL_COLOR = "#4ade80"
_EROSION_COLOR = "#ff6b6b"
_TRACE_COLOR = "#7a5cff"
_BG = "#0f1115"


def _palette(t: float) -> str:
    """t ∈ [0,1]：蓝(高) → 暗(中) → 绿(低) 的发散配色。"""
    if t < 0.5:
        u = t * 2
        r, g, b = 79 - 50 * u, 140 - 100 * u, 255 - 160 * u
    else:
        u = (t - 0.5) * 2
        r, g, b = 29 + 45 * u, 40 + 134 * u, 95 - 60 * u
    return f"#{max(0, min(255, int(r))):02x}" \
           f"{max(0, min(255, int(g))):02x}{max(0, min(255, int(b))):02x}"


def render_terrain_svg(engine: Any, size: int = 520, grid: int = 64,
                       title: str = "wanter 势能地形") -> str:
    """渲染引擎当前地形的 SVG（2D 热力图 / 1D 剖面 / 高维投影）。"""
    terrain = engine.terrain
    snapshot = terrain.snapshot()
    dim = int(snapshot["dim"])
    if dim == 1:
        return _render_1d(engine, snapshot, size, title)
    return _render_2d(engine, snapshot, size, grid, title)


def _window(snapshot: dict) -> Tuple[float, float]:
    """坐标窗口：目标 ± 3σ，无目标取 [-4, 4]。"""
    sigma = float(snapshot.get("sigma", 1.0))
    lo, hi = -4.0, 4.0
    for goal, _strength in snapshot.get("goals") or []:
        c = goal[0]
        lo = min(lo, c - 3 * sigma)
        hi = max(hi, c + 3 * sigma)
    return lo, hi


def _render_2d(engine: Any, snapshot: dict, size: int, grid: int,
               title: str) -> str:
    lo, hi = _window(snapshot)
    cell = size / grid
    values: List[List[float]] = []
    phi_min, phi_max = float("inf"), float("-inf")
    for gy in range(grid):
        row: List[float] = []
        for gx in range(grid):
            x = lo + (gx + 0.5) / grid * (hi - lo)
            y = hi - (gy + 0.5) / grid * (hi - lo)
            phi = engine.phi((x, y))
            row.append(phi)
            phi_min, phi_max = min(phi_min, phi), max(phi_max, phi)
        values.append(row)
    span = (phi_max - phi_min) or 1.0
    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
        f'height="{size + 70}" viewBox="0 0 {size} {size + 70}">',
        f'<rect width="{size}" height="{size + 70}" fill="{_BG}"/>',
        f'<text x="14" y="30" fill="#e6e9f0" font-size="15" '
        f'font-family="monospace">{title}</text>',
        f'<text x="14" y="50" fill="#8b93a7" font-size="11" '
        f'font-family="monospace">dim={snapshot["dim"]} · '
        f'φ ∈ [{phi_min:.1f}, {phi_max:.1f}]（前两轴投影）</text>']
    for gy in range(grid):
        for gx in range(grid):
            t = (values[gy][gx] - phi_min) / span
            parts.append(
                f'<rect x="{gx * cell:.1f}" y="{gy * cell + 64:.1f}" '
                f'width="{cell + 0.6:.1f}" height="{cell + 0.6:.1f}" '
                f'fill="{_palette(t)}"/>')
    for goal, strength in snapshot.get("goals") or []:
        gx, gy = _to_px(goal[:2], lo, hi, size, 64)
        parts.append(f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="6" fill="none" '
                     f'stroke="{_GOAL_COLOR}" stroke-width="2"/>')
    for bump, _h in snapshot.get("erosion_bumps") or []:
        gx, gy = _to_px(bump[:2], lo, hi, size, 64)
        parts.append(f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="2.5" '
                     f'fill="{_EROSION_COLOR}"/>')
    trace_snapshot = engine.trace.snapshot()
    for event in trace_snapshot["events"][::7]:
        gx, gy = _to_px(event["point"][:2], lo, hi, size, 64)
        parts.append(f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="1.4" '
                     f'fill="{_TRACE_COLOR}" opacity="0.8"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def _to_px(point: Any, lo: float, hi: float, size: int,
           offset: int) -> Tuple[float, float]:
    span = (hi - lo) or 1.0
    return ((point[0] - lo) / span * size,
            offset + (hi - point[1]) / span * size)


def _render_1d(engine: Any, snapshot: dict, size: int,
               title: str) -> str:
    height = 300
    lo, hi = _window(snapshot)
    y_lo, y_hi = float("inf"), float("-inf")
    n = 240
    curve: List[Tuple[float, float]] = []
    for i in range(n + 1):
        x = lo + (hi - lo) * i / n
        phi = engine.phi((x,))
        y_lo, y_hi = min(y_lo, phi), max(y_hi, phi)
        curve.append((x, phi))
    span = (y_hi - y_lo) or 1.0

    def px(x: float) -> float:
        return 50 + (x - lo) / ((hi - lo) or 1.0) * (size - 100)

    def py(y: float) -> float:
        return 30 + (y_hi - y) / span * (height - 60)

    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
        f'height="{height}" viewBox="0 0 {size} {height}">',
        f'<rect width="{size}" height="{height}" fill="{_BG}"/>',
        f'<text x="14" y="18" fill="#e6e9f0" font-size="13" '
        f'font-family="monospace">{title}（1D 剖面）</text>']
    polyline = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in curve)
    parts.append(f'<polyline fill="none" stroke="#4f8cff" stroke-width="2" '
                 f'points="{polyline}"/>')
    for goal, _s in snapshot.get("goals") or []:
        x = goal[0]
        parts.append(f'<circle cx="{px(x):.1f}" cy="{py(engine.phi((x,))):.1f}" '
                     f'r="5" fill="{_GOAL_COLOR}"/>')
    for bump, _h in snapshot.get("erosion_bumps") or []:
        x = bump[0]
        parts.append(f'<circle cx="{px(x):.1f}" '
                     f'cy="{py(engine.phi((x,))):.1f}" r="2.5" '
                     f'fill="{_EROSION_COLOR}"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def render_terrain_state(engine: Any) -> dict:
    """面板 JSON 状态（地形/迹/侵蚀摘要，lossless JSON）。"""
    terrain = engine.terrain.snapshot()
    trace = engine.trace.snapshot()
    return {
        "dim": terrain["dim"],
        "goals": [[list(g), s] for g, s in terrain["goals"]],
        "sigma": terrain["sigma"],
        "erosion_bumps": len(terrain["erosion_bumps"]),
        "trace_events": len(trace["events"]),
        "trace_decay_rate": trace["decay_rate"],
        "completed": engine.completed(
            engine.nearest_goal((0.0,) * terrain["dim"])
            or (0.0,) * terrain["dim"])
        if terrain["goals"] else None,
    }


def render_calibration_chart(metrics: dict) -> str:
    """语义坐标校准实验图表（匹配率 + 平均步数双栏，纯 SVG）。"""
    hash_m = metrics["hash"]["matching"] * 100
    oracle_m = metrics["oracle"]["matching"] * 100
    hash_s = metrics["hash"]["mean_steps"]
    oracle_s = metrics["oracle"]["mean_steps"]
    width, height = 640, 300
    baseline = height - 46
    left_x = 120
    right_x = 400
    bar_w = 64

    def bar(x: int, value: float, vmax: float, color: str, label: str) -> str:
        h = value / vmax * (baseline - 40)
        return (f'<rect x="{x}" y="{baseline - h:.1f}" width="{bar_w}" '
                f'height="{h:.1f}" fill="{color}"/>\n'
                f'<text x="{x + bar_w / 2:.0f}" y="{baseline - h - 6:.0f}" '
                f'fill="#e6e9f0" font-size="13" font-family="monospace" '
                f'text-anchor="middle">{label}</text>')

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
             f'height="{height}" viewBox="0 0 {width} {height}">',
             f'<rect width="{width}" height="{height}" fill="{_BG}"/>',
             f'<text x="20" y="26" fill="#e6e9f0" font-size="15" '
             f'font-family="monospace">wanter 语义坐标校准：hash 伪嵌入 vs '
             f'oracle 语义嵌入</text>',
             f'<text x="20" y="46" fill="#8b93a7" font-size="11" '
             f'font-family="monospace">多目标匹配实验（4 任务 × 8 种子；'
             f'oracle = 与目标布局对齐的模拟嵌入）</text>',
             f'<text x="{left_x}" y="{height - 22}" fill="#8b93a7" '
             f'font-size="12" font-family="monospace">匹配率 %</text>',
             f'<text x="{right_x}" y="{height - 22}" fill="#8b93a7" '
             f'font-size="12" font-family="monospace">平均步数（越小越好）</text>',
             bar(left_x, hash_m, 100, "#ff6b6b", f"{hash_m:.0f}%"),
             bar(left_x + 96, oracle_m, 100, "#4ade80", f"{oracle_m:.0f}%"),
             bar(right_x, hash_s, max(hash_s, oracle_s, 1),
                 "#ff6b6b", f"{hash_s:.0f}"),
             bar(right_x + 96, oracle_s, max(hash_s, oracle_s, 1),
                 "#4ade80", f"{oracle_s:.0f}"),
             f'<text x="{left_x + 32}" y="{height - 8}" fill="#8b93a7" '
             f'font-size="11" font-family="monospace">hash</text>',
             f'<text x="{left_x + 128}" y="{height - 8}" fill="#8b93a7" '
             f'font-size="11" font-family="monospace">oracle</text>',
             f'<text x="{right_x + 32}" y="{height - 8}" fill="#8b93a7" '
             f'font-size="11" font-family="monospace">hash</text>',
             f'<text x="{right_x + 128}" y="{height - 8}" fill="#8b93a7" '
             f'font-size="11" font-family="monospace">oracle</text>',
             "</svg>"]
    return "\n".join(parts)
