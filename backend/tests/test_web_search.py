from __future__ import annotations

from typing import Any

import pytest

from app.core.settings_service import SettingsSnapshot
from app.services import web_search as module
from app.services.web_search import (
    MAX_RESULTS,
    WebSearchError,
    parse_searxng_results,
    web_search,
)


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _snapshot(**overrides: Any) -> SettingsSnapshot:
    values: dict[str, Any] = {
        "llm.web_search_enabled": True,
        "llm.web_search_base_url": "http://searx.local:8888",
        "llm.web_search_language": "",
        "llm.web_search_timeout_seconds": 7,
    }
    values.update(overrides)
    return SettingsSnapshot(values, {}, None)


def _payload(*items: dict[str, Any]) -> dict[str, Any]:
    return {"query": "q", "results": list(items)}


def test_parse_searxng_results_fields_and_dedup() -> None:
    payload = {
        "results": [
            {"title": " 茅台  公告 ", "url": "https://a", "content": "摘要\n内容"},
            {"title": "", "url": "https://b", "content": ""},
            {"title": "重复", "url": "https://a", "content": ""},
            {"title": "第二条", "url": "https://c", "snippet": "s"},
        ]
    }
    hits = parse_searxng_results(payload)
    assert [hit.url for hit in hits] == ["https://a", "https://c"]
    assert hits[0].title == "茅台 公告"
    assert hits[0].snippet == "摘要 内容"
    assert hits[1].snippet == "s"


def test_parse_searxng_results_tolerates_bad_payload() -> None:
    assert parse_searxng_results(None) == []
    assert parse_searxng_results({"results": "oops"}) == []
    assert parse_searxng_results({"results": [1, None, {"url": ""}]}) == []
    assert parse_searxng_results([{"title": "t", "url": "https://x"}]) != []


def test_web_search_builds_searxng_request(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get(url: str, *, params: dict[str, Any], timeout: float):
        captured.update(url=url, params=params, timeout=timeout)
        return FakeResponse(
            _payload({"title": "标题", "url": "https://x", "content": "摘要"})
        )

    monkeypatch.setattr(module, "_http_get", fake_get)
    hits = web_search("贵州茅台 公告", snapshot=_snapshot(), limit=3)
    assert captured["url"] == "http://searx.local:8888/search"
    assert captured["params"] == {
        "q": "贵州茅台 公告",
        "format": "json",
        "categories": "general",
        "safesearch": 0,
    }
    assert captured["timeout"] == 7
    assert hits == [{"title": "标题", "url": "https://x", "snippet": "摘要"}]


def test_web_search_accepts_full_search_url(monkeypatch) -> None:
    urls: list[str] = []

    def fake_get(url: str, *, params: dict[str, Any], timeout: float):
        urls.append(url)
        return FakeResponse(_payload())

    monkeypatch.setattr(module, "_http_get", fake_get)
    web_search("q", snapshot=_snapshot(**{"llm.web_search_base_url": "http://x/search/"}))
    web_search("q", snapshot=_snapshot(**{"llm.web_search_base_url": "http://y:8080//"}))
    assert urls == ["http://x/search", "http://y:8080/search"]


def test_web_search_language_and_limit_clamp(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    many = [
        {"title": f"t{index}", "url": f"https://e/{index}", "content": "c"}
        for index in range(30)
    ]

    def fake_get(url: str, *, params: dict[str, Any], timeout: float):
        captured.update(params=params)
        return FakeResponse({"results": many})

    monkeypatch.setattr(module, "_http_get", fake_get)
    hits = web_search(
        "q", snapshot=_snapshot(**{"llm.web_search_language": "zh-CN"}), limit=50
    )
    assert captured["params"]["language"] == "zh-CN"
    assert len(hits) == MAX_RESULTS


def test_web_search_requires_base_url(monkeypatch) -> None:
    def fake_get(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("未配置地址时不应发请求")

    monkeypatch.setattr(module, "_http_get", fake_get)
    with pytest.raises(WebSearchError) as excinfo:
        web_search("q", snapshot=_snapshot(**{"llm.web_search_base_url": "  "}))
    assert "未配置 SearXNG 地址" in str(excinfo.value)


def test_web_search_rejects_empty_query() -> None:
    with pytest.raises(WebSearchError) as excinfo:
        web_search("   ", snapshot=_snapshot())
    assert "搜索关键词为空" in str(excinfo.value)


def test_web_search_json_forbidden_hints_format(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "_http_get",
        lambda url, *, params, timeout: FakeResponse(status_code=403),
    )
    with pytest.raises(WebSearchError) as excinfo:
        web_search("q", snapshot=_snapshot())
    message = str(excinfo.value)
    assert "HTTP 403" in message
    assert "json" in message


def test_web_search_http_error(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "_http_get",
        lambda url, *, params, timeout: FakeResponse(status_code=502),
    )
    with pytest.raises(WebSearchError) as excinfo:
        web_search("q", snapshot=_snapshot())
    assert "HTTP 502" in str(excinfo.value)


def test_web_search_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "_http_get",
        lambda url, *, params, timeout: FakeResponse(None, text="<html>"),
    )
    with pytest.raises(WebSearchError) as excinfo:
        web_search("q", snapshot=_snapshot())
    assert "不是合法 JSON" in str(excinfo.value)


def test_web_search_request_exception(monkeypatch) -> None:
    def fake_get(url, *, params, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(module, "_http_get", fake_get)
    with pytest.raises(WebSearchError) as excinfo:
        web_search("q", snapshot=_snapshot())
    assert "搜索请求失败" in str(excinfo.value)


def test_web_search_payload_error(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "_http_get",
        lambda url, *, params, timeout: FakeResponse({"error": "引擎不可用"}),
    )
    with pytest.raises(WebSearchError) as excinfo:
        web_search("q", snapshot=_snapshot())
    assert "引擎不可用" in str(excinfo.value)


def test_web_search_empty_results_returns_list(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "_http_get",
        lambda url, *, params, timeout: FakeResponse({"results": []}),
    )
    assert web_search("q", snapshot=_snapshot()) == []

