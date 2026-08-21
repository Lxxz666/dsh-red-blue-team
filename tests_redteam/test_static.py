"""test_static —— 静态扫描引擎（文件夹模式输入）。"""
import os

from redteam.static import StaticScanner, findings_to_model


def _make_vulnerable_project(path) -> None:
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "app.py"), "w", encoding="utf-8") as fh:
        fh.write(
            'API_KEY = "sk-live-9f8a7b6c9f8a7b6c9f8a7b6c"\n'
            "import hashlib, subprocess, pickle, yaml\n"
            'pwd = hashlib.md5(password.encode())\n'
            'subprocess.run(f"ping {host}", shell=True)\n'
            "obj = pickle.loads(user_input)\n"
            "cfg = yaml.load(user_input)\n"
            "DEBUG = True\n")
    with open(os.path.join(path, "requirements.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("django==3.2.20\nflask==1.0.2\nrequests>=2.0\nfastapi==0.100.0\n")
    with open(os.path.join(path, "Dockerfile"), "w", encoding="utf-8") as fh:
        fh.write("FROM python:3.11\nCOPY . /app\nCMD python app.py\n")
    with open(os.path.join(path, ".env"), "w", encoding="utf-8") as fh:
        fh.write("SECRET_KEY=prod-secret-123456\n")
    os.makedirs(os.path.join(path, "sub"), exist_ok=True)
    with open(os.path.join(path, "sub", "server.js"), "w",
              encoding="utf-8") as fh:
        fh.write("element.innerHTML = userInput;\n")


def test_scanner_finds_expected_issues(tmp_path):
    proj = tmp_path / "proj"
    _make_vulnerable_project(str(proj))
    findings = StaticScanner().scan(str(proj))
    categories = {f.category for f in findings}
    assert "hardcoded_secret" in categories
    assert "weak_crypto" in categories
    assert "command_injection" in categories
    assert "unsafe_deserialization" in categories
    assert "debug_mode" in categories
    assert "sensitive_file" in categories
    assert "dependency_vuln" in categories
    assert "xss_sink" in categories
    # file:line 证据
    secret = [f for f in findings if f.category == "hardcoded_secret"][0]
    assert secret.file == "app.py" and secret.line == 1
    assert "app.py:1" in secret.evidence
    # CVE-lite 命中具体包与版本
    deps = {f.snippet for f in findings if f.category == "dependency_vuln"}
    assert any("django" in s for s in deps)
    assert any("flask" in s for s in deps)
    # 非漏洞依赖不误报
    assert not any("fastapi" in s for s in deps)


def test_scanner_clean_project_no_findings(tmp_path):
    proj = tmp_path / "clean"
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "app.py"), "w", encoding="utf-8") as fh:
        fh.write("def add(a, b):\n    return a + b\n")
    with open(os.path.join(proj, "requirements.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("fastapi==0.115.0\nuvicorn==0.30.0\n")
    findings = StaticScanner().scan(str(proj))
    assert findings == []


def test_findings_to_model(tmp_path):
    proj = tmp_path / "proj"
    _make_vulnerable_project(str(proj))
    findings = StaticScanner().scan(str(proj))
    models = findings_to_model(findings, "scan-static-test")
    assert models
    for model in models:
        assert model.scan_id == "scan-static-test"
        assert model.role == "static"
        assert model.evidence
        assert model.fix.get("plan"), "静态发现必须带修复指引"
    secret = next(m for m in models if m.category == "hardcoded_secret")
    assert secret.fix["template"] == "secret-rotation"


def test_scanner_ignores_vcs_and_caches(tmp_path):
    proj = tmp_path / "proj"
    _make_vulnerable_project(str(proj))
    cache = proj / "node_modules" / "lib"
    os.makedirs(cache, exist_ok=True)
    with open(cache / "evil.js", "w", encoding="utf-8") as fh:
        fh.write("element.innerHTML = x;\nAPI_KEY='sk-x';")
    findings = StaticScanner().scan(str(proj))
    assert not any("node_modules" in f.file for f in findings)
