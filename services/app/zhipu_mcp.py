"""zhipu_mcp.py — 智谱 GLM Coding Plan 专属 MCP 客户端（HTTP 传输）。

能力：
  - web_search_prime：联网搜索（返回标题/URL/摘要/站点）
  - webReader：抓取指定 URL 的网页正文（标题/内容/元数据/链接）

协议：MCP streamable HTTP（initialize → notifications/initialized → tools/call）。
注意：智谱 MCP 有内容安全过滤（泛化新闻类 query 可能被拦或返回空），
调用方需处理空结果；key 须为 GLM 编程套餐 key。
"""
from __future__ import annotations

import json
import logging

import requests

logger = logging.getLogger(__name__)

SEARCH_URL = "https://open.bigmodel.cn/api/mcp/web_search_prime/mcp"
READER_URL = "https://open.bigmodel.cn/api/mcp/web_reader/mcp"

_SESSION_CACHE: dict[str, tuple[str, str]] = {}   # url -> (key, session_id)


class ZhipuMcpError(Exception):
    pass


class McpSession:
    """轻量 MCP HTTP 会话（streamable HTTP + session id）。"""

    def __init__(self, url: str, api_key: str):
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {api_key}",
        }
        self.session_id = None
        self._init()

    def _post(self, payload: dict, timeout: int = 30, allow_empty: bool = False) -> dict:
        r = requests.post(self.url, headers=self.headers, json=payload, timeout=timeout)
        r.raise_for_status()
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
            self.headers["Mcp-Session-Id"] = sid
        texts = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        if not texts:
            if allow_empty:
                return {}
            raise ZhipuMcpError(f"empty MCP response: {r.text[:200]}")
        return json.loads(texts[-1])

    def _init(self) -> None:
        res = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "framedphoto", "version": "0.1"}},
        })
        if "result" not in res:
            raise ZhipuMcpError(f"initialize failed: {res}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                   timeout=10, allow_empty=True)

    def call_tool(self, name: str, arguments: dict, timeout: int = 60) -> dict:
        res = self._post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, timeout=timeout)
        result = res.get("result", {})
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
            raise ZhipuMcpError(texts[0][:300] if texts else "tool error")
        return result


def _get_session(kind: str, api_key: str) -> McpSession:
    url = SEARCH_URL if kind == "search" else READER_URL
    key = f"{kind}:{api_key}"
    cached = _SESSION_CACHE.get(url)
    if cached and cached[0] == key:
        return cached[1]
    s = McpSession(url, api_key)
    _SESSION_CACHE[url] = (key, s)
    return s


def _deep_json(text: str):
    """智谱 MCP 偶有双重 JSON 编码（str 里嵌 str），递归解析到非 str。"""
    data = json.loads(text)
    while isinstance(data, str):
        data = json.loads(data)
    return data


def search_news(api_key: str, query: str, count: int = 5) -> list[dict] | None:
    """联网搜索，返回 [{title, link, content, site}]。被安全过滤/失败返回 None。"""
    try:
        session = _get_session("search", api_key)
        result = session.call_tool("web_search_prime", {
            "search_query": query, "content_size": "small"})
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        if not texts:
            return None
        # 返回的 text 是 JSON 数组字符串（可能双重编码）
        items = _deep_json(texts[0])
        out = []
        for it in items[:count]:
            if it.get("title"):
                out.append({"title": it["title"], "link": it.get("link", ""),
                            "content": (it.get("content") or "")[:200],
                            "site": it.get("site_name", "") or it.get("from", "")})
        return out or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("zhipu search failed (%s): %s", query[:20], exc)
        return None


def read_page(api_key: str, url: str) -> dict | None:
    """读取网页正文，返回 {title, description, content, url}。失败返回 None。"""
    try:
        session = _get_session("reader", api_key)
        result = session.call_tool("webReader", {"url": url, "content_size": "small"})
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        if not texts:
            return None
        data = _deep_json(texts[0])
        return {"title": data.get("title", ""), "description": data.get("description", ""),
                "content": data.get("content", ""), "url": data.get("url", url)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("zhipu read failed (%s): %s", url[:40], exc)
        return None
