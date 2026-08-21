"""
wanter 语义坐标校准实验：hash 伪嵌入 vs oracle 语义嵌入。

运行: python examples/wanter_embedding_calibration.py
产出: examples/wanter_embedding_metrics.json + examples/wanter_embedding_chart.svg
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dsh.wanter.calibration import run_calibration  # noqa: E402
from dsh.wanter.viz import render_calibration_chart  # noqa: E402


def main():
    metrics = run_calibration()
    out_dir = os.path.dirname(os.path.abspath(__file__))
    metrics_path = os.path.join(out_dir, "wanter_embedding_metrics.json")
    chart_path = os.path.join(out_dir, "wanter_embedding_chart.svg")
    with open(metrics_path, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=2)
    with open(chart_path, "w", encoding="utf-8") as fh:
        fh.write(render_calibration_chart(metrics))
    hash_m, oracle_m = (metrics["hash"]["matching"] * 100,
                        metrics["oracle"]["matching"] * 100)
    print(f"[校准] hash 匹配率 {hash_m:.0f}% / oracle 匹配率 {oracle_m:.0f}%")
    print(f"[校准] hash 平均步数 {metrics['hash']['mean_steps']:.0f} / "
          f"oracle 平均步数 {metrics['oracle']['mean_steps']:.0f}")
    print(f"指标已写入: {metrics_path}")
    print(f"图表已写入: {chart_path}")


if __name__ == "__main__":
    main()
