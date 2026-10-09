"""
麦麦精算师 - McDonald's MCP 客户端

基于 Streamable HTTP 传输协议直连麦当劳中国 MCP Server (https://mcp.mcd.cn)。

设计要点：
  * 无第三方依赖（仅标准库），方便在任何环境直接运行；
  * 自动处理 JSON / SSE( text/event-stream ) 两种响应体；
  * 维护 Mcp-Session-Id，兼容需要会话保持的网关；
  * 内置令牌桶限流（官方限制 600 次/分钟）与指数退避重试；
  * 对 401 / 429 做语义化异常。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .exceptions import (
    McpAuthError,
    McpRateLimitError,
    McpToolError,
    McpTransportError,
)

__all__ = ["McdMcpClient", "McpToolResult", "ToolSpec"]

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"
DEFAULT_TIMEOUT = 45.0
# 官方限流 600 次/分钟，这里保守取 8 次/秒
DEFAULT_RATE_PER_SEC = 8.0


@dataclass
class ToolSpec:
    """MCP 工具元信息。"""

    name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)

    @property
    def parameters(self) -> List[str]:
        return list(self.input_schema.get("properties", {}).keys())

    @property
    def required(self) -> List[str]:
        return list(self.input_schema.get("required", []))


@dataclass
class McpToolResult:
    """一次 tools/call 的结果。"""

    name: str
    text: str
    is_error: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 调试便利
        return self.text


class _TokenBucket:
    """极简令牌桶限流器，线程安全。"""

    def __init__(self, rate_per_sec: float, capacity: Optional[float] = None) -> None:
        self.rate = max(rate_per_sec, 0.1)
        self.capacity = capacity or max(self.rate, 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rate
            time.sleep(max(sleep_for, 0.01))


class McdMcpClient:
    """麦当劳 MCP Server 客户端。

    用法::

        client = McdMcpClient(token=os.environ["MCD_MCP_TOKEN"])
        tools = client.list_tools()
        res = client.call("now-time-info")
    """

    def __init__(
        self,
        token: Optional[str] = None,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        max_retries: int = 3,
        user_agent: str = "mcd-smart-planner/1.0 (+https://github.com/M-China/mcd-developer-innovation-challenge)",
    ) -> None:
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not self.token:
            raise McpAuthError(
                "缺少 MCP Token：请设置环境变量 MCD_MCP_TOKEN，或显式传入 token 参数。"
            )
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self._bucket = _TokenBucket(rate_per_sec)
        self._session_id: Optional[str] = None
        self._id = 0
        self._lock = threading.Lock()
        self._tool_cache: Optional[List[ToolSpec]] = None

    # ------------------------------------------------------------------ #
    # 底层 JSON-RPC
    # ------------------------------------------------------------------ #
    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": self.user_agent,
            # 网关对大体积响应使用分块传输，短连接可避免 IncompleteRead
            "Connection": "close",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发送一次 JSON-RPC 请求，自动重试与限流。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_err: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            self._bucket.acquire()
            req = urllib.request.Request(
                self.endpoint, data=body, headers=self._headers(), method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    sid = resp.headers.get("Mcp-Session-Id")
                    if sid:
                        self._session_id = sid
                    return self._decode(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code == 401:
                    raise McpAuthError(
                        "MCP Token 无效、已过期或未提供（HTTP 401）。"
                    ) from exc
                if exc.code == 429:
                    last_err = McpRateLimitError(
                        "触发限流（HTTP 429，官方上限 600 次/分钟）。"
                    )
                else:
                    last_err = McpTransportError(f"HTTP {exc.code}: {detail}")
            except Exception as exc:  # noqa: BLE001 - 网络层异常统一收敛
                last_err = McpTransportError(f"网络异常：{exc}")

            if attempt < self.max_retries:
                time.sleep(min(2**attempt, 8))

        raise last_err or McpTransportError("未知传输错误")

    @staticmethod
    def _decode(raw: str) -> Dict[str, Any]:
        """兼容纯 JSON 与 SSE 两种响应体。"""
        raw = raw.strip()
        if not raw:
            raise McpTransportError("空响应")
        if raw.startswith("{"):
            return json.loads(raw)
        # SSE: 逐条解析 `data:` 行
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    return json.loads(data)
        raise McpTransportError(f"无法解析响应：{raw[:200]}")

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            payload["params"] = params
        resp = self._post(payload)
        if "error" in resp:
            err = resp["error"]
            raise McpToolError(f"{method} 失败 [{err.get('code')}]: {err.get('message')}")
        return resp.get("result")

    # ------------------------------------------------------------------ #
    # MCP 能力
    # ------------------------------------------------------------------ #
    def initialize(self) -> Dict[str, Any]:
        return self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcd-smart-planner", "version": "1.0.0"},
            },
        )

    def list_tools(self, refresh: bool = False) -> List[ToolSpec]:
        if self._tool_cache is not None and not refresh:
            return self._tool_cache
        result = self.request("tools/list", {}) or {}
        self._tool_cache = [
            ToolSpec(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            )
            for t in result.get("tools", [])
        ]
        return self._tool_cache

    def tool_names(self) -> List[str]:
        return [t.name for t in self.list_tools()]

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> McpToolResult:
        result = self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        content = (result or {}).get("content", [])
        text = "\n".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
        structured = (result or {}).get("structuredContent")
        return McpToolResult(
            name=name,
            text=text,
            is_error=bool((result or {}).get("isError")),
            raw=result or {},
        )

    def call_many(
        self, calls: Iterable["tuple[str, Dict[str, Any]] | str"]
    ) -> Dict[str, McpToolResult]:
        """批量调用，单个失败不影响整体（结果中标记 error）。"""
        out: Dict[str, McpToolResult] = {}
        for call in calls:
            if isinstance(call, str):
                name, args = call, {}
            else:
                name, args = call
            try:
                out[name] = self.call(name, args)
            except Exception as exc:  # noqa: BLE001
                out[name] = McpToolResult(name=name, text=str(exc), is_error=True)
        return out


def parse_tool_payload(result: McpToolResult) -> Any:
    """从 MCP 返回的说明性文本中还原 `Original Response` 里的 JSON。

    麦当劳 MCP 的 text 形如::

        # API Response Information
        ...
        ## Original Response

        {"success":true, ...}

    本函数定位最后一个顶层 JSON 对象并解析，失败则返回 None。
    """
    if isinstance(result, str):
        text = result
    else:
        text = result.text or ""
    marker = "## Original Response"
    idx = text.find(marker)
    if idx >= 0:
        text = text[idx + len(marker) :]

    # 从第一个 '{' 开始做括号配平，正确跳过字符串内部的括号
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
