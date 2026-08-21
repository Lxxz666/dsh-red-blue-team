"""
dsh.web.tool —— Web 工具（web_fetch / web_search）。

对应 TS 版 tool-web（web card: kind=fetch/search）：

- ``web_fetch``：httpx GET，返回状态码 + 正文（截断到 max_chars）；
- ``web_search``：DuckDuckGo lite HTML 端点做尽力而为的搜索结果解析
  （无密钥；被限流时返回结构化错误而非崩溃）。
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from ..errors import ToolError
from ..kernel import Service
from ..tools import define_tool
from ..tools.presentation import web_result

DDG_LITE = "https://html.duckduckgo.com/html/"

_LINK_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)


def build_web_tools() -> List[Any]:
    """构造 web 工具族（注册由 WebPlugin.apply 完成）。"""

    @define_tool(
        name="web_fetch",
        description="抓取一个 URL 的文本内容（http/https）。",
        parameters={"url": {"type": "string", "required": True},
                    "max_chars": {"type": "integer",
                                  "description": "正文截断长度（默认 8000）"}},
        output={"type": "string"},
        timeout_ms=60_000,
        present_result=lambda args, result: web_result(
            title=f"抓取 {args['url']}", kind="fetch", url=args["url"],
            status_code=200, truncated=False))
    async def web_fetch(args, run_ctx):
        url = args["url"]
        if not url.startswith(("http://", "https://")):
            raise ToolError(f"unsupported url scheme: {url}", code="BAD_URL")
        max_chars = int(args.get("max_chars") or 8000)
        try:
            async with httpx.AsyncClient(
                    timeout=30, follow_redirects=True,
                    headers={"User-Agent": "dsh-python/0.1"}) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise ToolError(f"fetch failed: {exc}", code="FETCH_FAILED")
        text = response.text
        stripped = _strip_html(text)
        truncated = len(stripped) > max_chars
        body = stripped[:max_chars]
        header = f"status: {response.status_code}\nurl: {str(response.url)}\n"
        return header + body + ("\n[truncated]" if truncated else "")

    @define_tool(
        name="web_search",
        description="网页搜索（DuckDuckGo lite，尽力而为）。",
        parameters={"query": {"type": "string", "required": True},
                    "max_results": {"type": "integer",
                                    "description": "最多结果数（默认 5）"}},
        output={"type": "array", "items": {"type": "object"}},
        timeout_ms=60_000,
        render=lambda args, value: _render_results(value))
    async def web_search(args, run_ctx):
        query = args["query"]
        max_results = int(args.get("max_results") or 5)
        try:
            async with httpx.AsyncClient(
                    timeout=30, follow_redirects=True,
                    headers={"User-Agent": "dsh-python/0.1"}) as client:
                response = await client.get(DDG_LITE, params={"q": query})
        except httpx.HTTPError as exc:
            raise ToolError(f"search failed: {exc}", code="SEARCH_FAILED")
        if response.status_code != 200:
            raise ToolError(f"search http {response.status_code}",
                            code="SEARCH_FAILED")
        results: List[Dict[str, str]] = []
        links = _LINK_RE.findall(response.text)
        snippets = _SNIPPET_RE.findall(response.text)
        for index, (href, title) in enumerate(links[:max_results]):
            snippet = ""
            if index < len(snippets):
                snippet = _strip_html(snippets[index]).strip()
            results.append({
                "url": _resolve_url(href),
                "title": _strip_html(title).strip(),
                "snippet": snippet[:300],
            })
        if not results:
            raise ToolError("no results (search endpoint may be rate-limited)",
                            code="NO_RESULTS")
        return results

    return [web_fetch, web_search]


def _strip_html(text: str) -> str:
    """剥掉标签并把常见实体还原。"""
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html_lib.unescape(re.sub(r"\s+", " ", text)).strip()


def _resolve_url(href: str) -> str:
    """DDG lite 的 href 形如 //duckduckgo.com/l/?uddg=<url>。"""
    match = re.search(r"uddg=([^&]+)", href)
    if match:
        return html_lib.unescape(match.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


def _render_results(value: List[Dict[str, str]]) -> str:
    if not value:
        return "(无结果)"
    lines = []
    for index, item in enumerate(value, 1):
        lines.append(f"{index}. {item['title']}\n   {item['url']}\n   {item['snippet']}")
    return "\n".join(lines)


class WebPlugin(Service):
    """注册 web 工具的插件（base bundle 行）。"""

    inject = ("tools",)

    def __init__(self, ctx, config: Optional[dict] = None) -> None:
        super().__init__(ctx, config)
        self._disposers: List[Any] = []

    def apply(self, ctx) -> None:
        for tool in build_web_tools():
            self._disposers.append(ctx.tools.register(tool))

        def cleanup() -> None:
            for disposer in self._disposers:
                disposer()
            self._disposers.clear()
        return cleanup
