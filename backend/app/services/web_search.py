from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import requests

from app.core.settings_service import SettingsSnapshot

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
MAX_RESULTS = 5
DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.S,
)
_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.S
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_REDIRECT_RE = re.compile(r"[?&]uddg=([^&]+)")


class WebSearchError(RuntimeError):
    """搜索服务不可用或返回异常。"""


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def _clean(text: str) -> str:
    return _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub("", text))).strip()


def _decode_url(href: str) -> str:
    href = html.unescape(href)
    match = _REDIRECT_RE.search(href)
    if match:
        return unquote(match.group(1))
    if href.startswith("//"):
        return f"https:{href}"
    return href


def parse_duckduckgo_html(markup: str, *, limit: int = MAX_RESULTS) -> list[SearchHit]:
    """解析 DuckDuckGo html 版结果页；结构变化时返回空列表而不是抛错。"""

    titles = list(_RESULT_RE.finditer(markup))
    snippets = [
        _clean(match.group("snippet"))
        for match in _SNIPPET_RE.finditer(markup)
    ]
    hits: list[SearchHit] = []
    for index, match in enumerate(titles[:limit]):
        url = _decode_url(match.group("href"))
        title = _clean(match.group("title"))
        if not url or not title:
            continue
        snippet = snippets[index] if index < len(snippets) else ""
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
    return hits


def _search_timeout(snapshot: SettingsSnapshot) -> float:
    try:
        return float(snapshot.get_int("llm.web_search_timeout_seconds"))
    except Exception:
        return float(DEFAULT_TIMEOUT)


def web_search(
    query: str,
    *,
    snapshot: SettingsSnapshot,
    limit: int = MAX_RESULTS,
) -> list[dict[str, str]]:
    """执行一次网页搜索，返回标题 / 链接 / 摘要。

    使用不需要 API Key 的 DuckDuckGo html 版；网络或结构异常时抛
    `WebSearchError`，由工具层转成纯文本错误交给模型，不向上冒泡。
    """

    needle = (query or "").strip()
    if not needle:
        raise WebSearchError("搜索关键词为空")
    limit = max(1, min(int(limit), MAX_RESULTS))
    try:
        response = requests.get(
            DUCKDUCKGO_ENDPOINT,
            params={"q": needle},
            headers={"User-Agent": USER_AGENT},
            timeout=_search_timeout(snapshot),
        )
    except Exception as exc:
        logger.warning("web search request failed: %s", exc)
        raise WebSearchError(
            f"搜索请求失败：{type(exc).__name__}"
        ) from exc
    if not response.ok:
        raise WebSearchError(f"搜索服务返回 HTTP {response.status_code}")
    hits = parse_duckduckgo_html(response.text, limit=limit)
    if not hits:
        raise WebSearchError("搜索没有返回可解析的结果")
    return [hit.to_dict() for hit in hits]


def format_hits(hits: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        title = str(hit.get("title") or "").strip()
        url = str(hit.get("url") or "").strip()
        snippet = str(hit.get("snippet") or "").strip()
        block = f"{index}. {title}"
        block = block + "\n" + url
        if snippet:
            block = block + "\n" + snippet
        lines.append(block)
    return "\n\n".join(lines)

