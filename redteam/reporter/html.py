"""redteam.reporter.html —— Markdown → 自包含 HTML 报告渲染（零第三方依赖）。

把扫描生成的 Markdown 报告渲染成打印友好的单文件 HTML：
- 内置样式（深色/浅色可打印、A4 打印 CSS）
- 浏览器「打印 → 另存为 PDF」即可得到专业 PDF 报告
- 支持报告用到的语法：标题 / 表格 / 列表 / 代码块 / 引用 / 粗体 / 分隔线 / 链接
"""
from __future__ import annotations

import html as _html
import re
from typing import List

_ESCAPE = _html.escape

# ---------- 行内格式 ----------

_INLINE_RULES = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r'<a href="\2" target="_blank" rel="noopener">\1</a>'),
]


def _inline(text: str) -> str:
    text = _ESCAPE(text)
    for pat, repl in _INLINE_RULES:
        text = pat.sub(repl, text)
    return text


# ---------- 块级渲染 ----------

def _render_table(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    thead = "<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead>"
    tbody = "<tbody>"
    for row in body:
        tbody += "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
    tbody += "</tbody>"
    return f"<table>{thead}{tbody}</table>"


def render_markdown(md: str) -> str:
    """Markdown → HTML 主体（不含 <html> 包裹）。"""
    lines = md.split("\n")
    out: List[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip()

        # 代码块
        if line.startswith("```"):
            lang = line[3:].strip()
            buf: List[str] = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过结束 ```
            code = _html.escape("\n".join(buf))
            out.append(f'<pre class="code"><code>{code}</code></pre>')
            continue

        # 表格（连续 | 行，第 2 行是分隔行）
        if line.startswith("|") and i + 1 < n and re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", lines[i + 1]):
            rows: List[List[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
                    rows.append(cells)
                i += 1
            out.append(_render_table(rows))
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line):
            out.append("<hr>")
            i += 1
            continue

        # 列表（- 或 - [ ]）
        if re.match(r"^\s*[-*]\s", line):
            buf: List[str] = []
            while i < n and re.match(r"^\s*[-*]\s", lines[i]):
                item = lines[i].strip()
                item = re.sub(r"^\s*[-*]\s+", "", item)
                if item.startswith("[ ]"):
                    item = "☐ " + item[3:].strip()
                elif item.startswith("[x]"):
                    item = "☑ " + item[3:].strip()
                buf.append(f"<li>{_inline(item)}</li>")
                i += 1
            out.append("<ul>" + "".join(buf) + "</ul>")
            continue

        # 引用
        if line.startswith(">"):
            buf: List[str] = []
            while i < n and lines[i].startswith(">"):
                buf.append(_inline(lines[i][1:].strip()))
                i += 1
            out.append(f"<blockquote>{'<br>'.join(buf)}</blockquote>")
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 普通段落（合并连续非空行）
        buf = [_inline(line)]
        i += 1
        while i < n and lines[i].strip() and not re.match(
                r"^(#{1,4}\s|```|\s*[-*]\s|>\s|\|)", lines[i]):
            buf.append(_inline(lines[i].strip()))
            i += 1
        out.append(f"<p>{' '.join(buf)}</p>")

    return "\n".join(out)


# ---------- 完整 HTML 文档 ----------

_PAGE_CSS = """
:root { --fg:#1b2433; --muted:#5a6b85; --accent:#0e9ec7; --border:#e3eaf5;
        --code-bg:#f4f7fb; --h-bg:#eef4fa; --critical:#ff4d6d; --high:#ff9f43; }
* { box-sizing:border-box; }
body { font-family:"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       color:var(--fg); font-size:14px; line-height:1.7; margin:0 auto; max-width:900px;
       padding:32px 40px; background:#fff; }
h1 { font-size:24px; border-bottom:2px solid var(--accent); padding-bottom:10px; margin:0 0 6px; }
h2 { font-size:18px; margin:26px 0 10px; color:#12233f; border-left:4px solid var(--accent);
     padding-left:10px; }
h3 { font-size:15px; margin:18px 0 8px; color:#233a5e; }
p { margin:8px 0; }
blockquote { border-left:3px solid var(--accent); margin:10px 0; padding:6px 14px;
             color:var(--muted); background:#f7fafd; border-radius:0 8px 8px 0; }
table { border-collapse:collapse; width:100%; margin:12px 0; font-size:13px; }
th,td { border:1px solid var(--border); padding:7px 11px; text-align:left; }
th { background:var(--h-bg); font-weight:600; }
tr:nth-child(even) td { background:#fafcff; }
code { background:var(--code-bg); border:1px solid var(--border); border-radius:4px;
       padding:1px 5px; font-family:Consolas,"Cascadia Code",monospace; font-size:12.5px;
       color:#c7254e; }
pre.code { background:#0f1420; color:#e8ecf4; border-radius:10px; padding:14px 16px;
           overflow:auto; font-family:Consolas,"Cascadia Code",monospace; font-size:12.5px;
           line-height:1.55; }
pre.code code { background:none; border:none; padding:0; color:inherit; }
ul { padding-left:22px; margin:8px 0; }
li { margin:3px 0; }
hr { border:none; border-top:1px solid var(--border); margin:18px 0; }
a { color:var(--accent); }
.footer { margin-top:36px; padding-top:14px; border-top:1px solid var(--border);
          color:var(--muted); font-size:12px; }
@media print {
  body { padding:10mm; max-width:none; }
  h2 { page-break-after:avoid; }
  table, pre, blockquote { page-break-inside:avoid; }
  a { color:inherit; text-decoration:none; }
}
@media (prefers-color-scheme: dark) {
  body { background:#0b0e15; color:#e8ecf4; }
  th { background:#1a2133; color:#c7d2e4; }
  tr:nth-child(even) td { background:#121a2a; }
  blockquote { background:#0f1524; }
  code { background:#161e30; color:#ff9db8; }
}
"""


def render_report_html(md: str, title: str = "安全检测报告") -> str:
    """生成完整自包含 HTML 报告（可直接打印/另存 PDF）。"""
    body = render_markdown(md)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)}</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
{body}
<div class="footer">本报告由 dsh-red-blue-team 自动生成 · 仅限授权测试</div>
</body>
</html>
"""
