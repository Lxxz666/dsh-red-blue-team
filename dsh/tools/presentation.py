"""
dsh.tools.presentation —— 工具展示词汇（对应 TS 版 render-intent union）。

provider 中立：工具描述自己想被 UI 如何展示，不依赖任何客户端协议。
渲染意图是 ``card`` 标记的 dict union，UI 桥按 card 类型 switch。
投影函数必须纯、无副作用（实时流式与日志回放都会调用）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def generic_call(title: str, kind: str = "other",
                 raw_input: Optional[Any] = None,
                 locations: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """pending 态通用卡片。``kind`` ∈ read/edit/delete/move/search/execute/fetch/other。"""
    out: Dict[str, Any] = {"card": "generic", "title": title, "kind": kind}
    if raw_input is not None:
        out["raw_input"] = raw_input
    if locations:
        out["locations"] = locations
    return out


def terminal_call(title: str, description: Optional[str] = None,
                  cwd: Optional[str] = None) -> Dict[str, Any]:
    """pending 态终端卡片（shell 命令）。"""
    out: Dict[str, Any] = {"card": "terminal", "title": title}
    if description:
        out["description"] = description
    if cwd:
        out["cwd"] = cwd
    return out


def generic_result(title: str, content: Optional[str] = None) -> Dict[str, Any]:
    """完成态通用卡片。"""
    out: Dict[str, Any] = {"card": "generic", "title": title}
    if content is not None:
        out["content"] = content
    return out


def terminal_result(title: str, output: str, exit_code: Optional[int] = None,
                    signal: Optional[str] = None) -> Dict[str, Any]:
    """完成态终端卡片（运行输出 + 退出码）。"""
    out: Dict[str, Any] = {"card": "terminal", "title": title, "output": output}
    if exit_code is not None:
        out["exit_code"] = exit_code
    if signal:
        out["signal"] = signal
    return out


def diff_result(title: str, diffs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """完成态 diff 卡片。``diffs`` = [{"path", "old_text"|None, "new_text"}]。"""
    return {"card": "diff", "title": title, "diffs": diffs}


def read_result(title: str, path: str, offset: int, lines: List[Dict[str, Any]],
                total_lines: int, lang: Optional[str] = None,
                content: Optional[str] = None) -> Dict[str, Any]:
    """完成态读取卡片（带行号代码视图）。"""
    out: Dict[str, Any] = {"card": "read", "title": title, "path": path,
                           "offset": offset, "lines": lines,
                           "total_lines": total_lines}
    if lang:
        out["lang"] = lang
    if content is not None:
        out["content"] = content
    return out


def search_result(title: str, shape: str, matches: Any, total: int,
                  truncated: bool = False) -> Dict[str, Any]:
    """完成态搜索卡片。``shape`` ∈ matches(grep)/paths(glob)。"""
    return {"card": "search", "title": title, "shape": shape, "matches": matches,
            "total": total, "truncated": truncated}


def web_result(title: str, kind: str, *, url: Optional[str] = None,
               status_code: Optional[int] = None,
               truncated: bool = False,
               sources: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    完成态 Web 卡片（对应 TS 版 web card）。

    ``kind='fetch'``：携带 url/status_code/truncated；
    ``kind='search'``：携带结构化 sources 与 truncated。
    """
    out: Dict[str, Any] = {"card": "web", "title": title, "kind": kind,
                           "truncated": truncated}
    if url is not None:
        out["url"] = url
    if status_code is not None:
        out["status_code"] = status_code
    if sources is not None:
        out["sources"] = sources
    return out
