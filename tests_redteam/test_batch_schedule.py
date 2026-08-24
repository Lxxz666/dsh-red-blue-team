"""test_batch_schedule —— batch 多目标批扫与 schedule 定时扫描。"""
import os

from redteam.cli import _parse_interval, cmd_batch, cmd_schedule


def test_parse_interval():
    assert _parse_interval("30m") == 1800
    assert _parse_interval("1h") == 3600
    assert _parse_interval("24h") == 86400
    assert _parse_interval("5s") == 5
    try:
        _parse_interval("偶尔")
        assert False, "非法周期应报错"
    except Exception:
        pass


async def test_batch_multi_target_risk_ranking(vuln_lab, tmp_path):
    """两个目标（靶场+文件夹）串行批扫，输出风险排序汇总报告。"""
    import argparse
    import yaml

    lab, guards_file = vuln_lab
    # 目标 2：合成有漏洞的文件夹
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "app.py").write_text(
        'API_KEY = "sk-live-9f8a7b6c9f8a7b6c9f8a7b6c"\nDEBUG = True\n',
        encoding="utf-8")
    targets_file = tmp_path / "targets.yml"
    with open(targets_file, "w", encoding="utf-8") as fh:
        yaml.safe_dump([
            {"name": "电商靶场", "profile": "quick",
             "target": {"name": "ecom", "type": "lab",
                        "base_url": lab.base_url,
                        "guards_file": guards_file},
             "vectors": {"variants_per_sample": 1},
             "storage": {"db_path": str(tmp_path / "b1.db"),
                         "audit_dir": str(tmp_path / "audit1")}},
            {"name": "内部项目(静态)", "profile": "quick",
             "target": {"name": "proj", "type": "folder",
                        "folder_path": str(proj)},
             "storage": {"db_path": str(tmp_path / "b2.db"),
                         "audit_dir": str(tmp_path / "audit2")}},
        ], fh, allow_unicode=True)
    out = tmp_path / "batch_out"
    await cmd_batch(argparse.Namespace(targets=str(targets_file),
                                       out=str(out)))
    summaries = [f for f in os.listdir(out) if f.startswith("batch_summary")]
    assert summaries, "应生成汇总报告"
    with open(os.path.join(out, summaries[0]), encoding="utf-8") as fh:
        content = fh.read()
    assert "风险排序" in content
    assert "电商靶场" in content and "内部项目(静态)" in content
    # 靶场（37 漏洞）风险评分应高于合成文件夹
    assert content.index("电商靶场") < content.index("内部项目(静态)")


async def test_schedule_once(vuln_lab, tmp_path):
    """schedule --once：执行一次扫描并把报告留存到时间戳目录。"""
    import argparse
    import yaml

    lab, guards_file = vuln_lab
    cfg_file = tmp_path / "scan.yml"
    with open(cfg_file, "w", encoding="utf-8") as fh:
        yaml.safe_dump({
            "profile": "quick",
            "target": {"name": "sched", "type": "lab",
                       "base_url": lab.base_url,
                       "guards_file": guards_file},
            "vectors": {"variants_per_sample": 1},
        }, fh, allow_unicode=True)
    out = tmp_path / "scheduled"
    await cmd_schedule(argparse.Namespace(
        config=str(cfg_file), every="5s", out=str(out), webhook="", once=True))
    runs = [d for d in os.listdir(out) if d.startswith("scan_")]
    assert len(runs) == 1
    reports = os.listdir(os.path.join(out, runs[0], "reports"))
    assert any(f.startswith("report_") for f in reports)


async def test_push_webhook_failure_is_graceful():
    """webhook 不可达：打印失败而非抛异常（推送是尽力而为）。"""
    from redteam.cli import _push_webhook
    await _push_webhook("http://127.0.0.1:1/never", {"event": "test"})
