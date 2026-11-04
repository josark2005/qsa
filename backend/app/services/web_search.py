from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from app.core.settings_service import SettingsSnapshot

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
MAX_RESULTS = 10
USER_AGENT = "QSA/0.1 (+https://github.com/josark2005/qsa)"
JSON_FORMAT_HINT = "请在 SearXNG 实例的 settings.yml 中把 search.formats 设为包含 json"


class WebSearchError(RuntimeError):
    """搜索服务未配置或调用失败。"""


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def parse_searxng_results(
    payload: Any, *, limit: int = MAX_RESULTS
) -> list[SearchHit]:
    """解析 SearXNG 的 JSON 结果，兼容顶层对象或直接的 results 列表。"""

    if isinstance(payload, dict):
        raw_results = payload.get("results")
    elif isinstance(payload, list):
        raw_results = payload
    else:
        raw_results = None
    if not isinstance(raw_results, list):
        return []
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url"))
        title = _text(item.get("title"))
        if not url or not title or url in seen:
            continue
        seen.add(url)
        snippet = _text(
            item.get("content") or item.get("snippet") or item.get("description")
        )
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
        if len(hits) >= max(1, int(limit)):
            break
    return hits


def _base_url(snapshot: SettingsSnapshot | Any) -> str:
    try:
        return str(snapshot.get("llm.web_search_base_url") or "").strip()
    except Exception:
        return ""


def _endpoint(base_url: str) -> str:
    """接受 https://searx.example.com 或带 /search 的完整地址。"""

    base = base_url.rstrip("/")
    if base.endswith("/search"):
        return base
    return urljoin(base + "/", "search")


def _search_timeout(snapshot: SettingsSnapshot | Any) -> float:
    try:
        return float(snapshot.get_int("llm.web_search_timeout_seconds"))
    except Exception:
        return float(DEFAULT_TIMEOUT)


def _language(snapshot: SettingsSnapshot | Any) -> str:
    try:
        return str(snapshot.get("llm.web_search_language") or "").strip()
    except Exception:
        return ""


def _http_get(url: str, *, params: dict[str, Any], timeout: float):
    return requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )


def web_search(
    query: str,
    *,
    snapshot: SettingsSnapshot,
    limit: int = MAX_RESULTS,
) -> list[dict[str, str]]:
    """调用外部 SearXNG 实例执行一次搜索，返回标题 / 链接 / 摘要。

    地址来自系统设置 llm.web_search_base_url；未配置、网络异常或实例未开启
    JSON 输出时抛 WebSearchError，由工具层转成纯文本错误交给模型。"""

    needle = (query or "").strip()
    if not needle:
        raise WebSearchError("搜索关键词为空")
    base_url = _base_url(snapshot)
    if not base_url:
        raise WebSearchError(
            "未配置 SearXNG 地址：请在系统设置「LLM 对话」中填写搜索地址（llm.web_search_base_url）"
        )
    limit = max(1, min(int(limit), MAX_RESULTS))
    params: dict[str, Any] = {
        "q": needle,
        "format": "json",
        "categories": "general",
        "safesearch": 0,
    }
    language = _language(snapshot)
    if language:
        params["language"] = language
    try:
        response = _http_get(
            _endpoint(base_url),
            params=params,
            timeout=_search_timeout(snapshot),
        )
    except Exception as exc:
        logger.warning("SearXNG 请求失败：%s", exc)
        raise WebSearchError(
            f"搜索请求失败：{type(exc).__name__}"
        ) from exc

    status = getattr(response, "status_code", None)
    ok = getattr(response, "ok", None)
    if ok is None:
        ok = isinstance(status, int) and 200 <= status < 300
    if not ok:
        if status in (401, 403):
            raise WebSearchError(
                f"SearXNG 返回 HTTP {status}：实例可能未开放 JSON 输出，{JSON_FORMAT_HINT}"
            )
        raise WebSearchError(f"搜索服务返回 HTTP {status}")

    try:
        payload = response.json()
    except Exception as exc:
        raise WebSearchError(
            f"搜索服务返回的不是合法 JSON：{JSON_FORMAT_HINT}"
        ) from exc

    if isinstance(payload, dict) and payload.get("error"):
        raise WebSearchError(
            f"搜索服务返回错误：{_text(payload.get('error'))[:200]}"
        )

    hits = parse_searxng_results(payload, limit=limit)
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

