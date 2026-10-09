"""零依赖 Web 可视化界面。

只用标准库 ``http.server`` 起一个本地服务，把 MCP 链路的每一步都可视化出来：
门店定位 → 活动雷达 → 券包盘点 → 菜单/营养 → 多目标求解 → 官方核价。

    from mcd_mcp.web import serve
    serve(port=8000)

或命令行：``mcd serve --port 8000``
"""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .exceptions import McdError
from .models import Constraints, Store
from .pipeline import Planner

__all__ = ["create_app", "serve"]

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class _Handler(BaseHTTPRequestHandler):
    planner: Planner  # 由工厂注入
    silent: bool = False

    # ---------------------------------------------------------------- #
    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        if not self.silent:
            print(f"  [web] {fmt % args}")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ---------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            file = os.path.join(_STATIC_DIR, "index.html")
            try:
                with open(file, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, b"index.html not found", "text/plain; charset=utf-8")
            return
        if path == "/api/tools":
            try:
                self._json(200, {"ok": True, "tools": self.planner.client.tool_names()})
            except McdError as exc:
                self._json(200, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if path == "/api/stores":
                stores = self.planner.find_stores(
                    body.get("city", ""), body.get("kw", ""), int(body.get("be_type", 1))
                )
                self._json(200, {"ok": True, "stores": [vars(s) for s in stores]})
            elif path == "/api/campaigns":
                camps = self.planner.campaigns(body.get("date"))
                self._json(200, {"ok": True, "campaigns": [vars(c) for c in camps]})
            elif path == "/api/coupons":
                available = self.planner.available_coupons()
                mine = self.planner.my_coupons()
                self._json(
                    200,
                    {
                        "ok": True,
                        "available": [vars(c) for c in available],
                        "mine": [vars(c) for c in mine],
                    },
                )
            elif path == "/api/plan":
                self._handle_plan(body)
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except McdError as exc:
            self._json(200, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免服务端 500 断连
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _handle_plan(self, body: Dict[str, Any]) -> None:
        city = body.get("city", "")
        kw = body.get("kw", "")
        store_code = body.get("store") or ""
        if store_code:
            store = Store(store_code=store_code, name=body.get("store_name") or f"门店 {store_code}")
        else:
            stores = self.planner.find_stores(city, kw, int(body.get("be_type", 1)))
            if not stores:
                self._json(200, {"ok": False, "error": "未找到门店，换个关键词试试"})
                return
            store = stores[0]
        c = Constraints(
            budget=float(body.get("budget", 40)),
            kcal_min=float(body.get("kcal_min", 0)),
            kcal_max=float(body.get("kcal_max", 900)),
            protein_min=float(body.get("protein_min", 0)),
            max_items=int(body.get("max_items", 4)),
            people=int(body.get("people", 1)),
            require_drink=bool(body.get("require_drink", True)),
        )
        report = self.planner.recommend(
            store,
            c,
            presets=body.get("presets"),
            order_type=int(body.get("order_type", 1)),
            be_type=int(body.get("be_type", 1)),
            verify_price=bool(body.get("verify_price", True)),
        )
        self._json(200, {"ok": True, "report": report.to_dict()})


def create_app(
    token: Optional[str] = None,
    cache_dir: Optional[str] = None,
    planner: Optional[Planner] = None,
    silent: bool = False,
) -> ThreadingHTTPServer:
    """返回一个已配置好的 HTTP 服务器对象（兼容 ``uvicorn`` 风格的调用点）。"""
    p = planner or Planner(token=token, cache_dir=cache_dir or os.path.join(os.getcwd(), ".cache"))
    handler = type("BoundHandler", (_Handler,), {"planner": p, "silent": silent})
    return ThreadingHTTPServer(("127.0.0.1", 0), handler)


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    token: Optional[str] = None,
    cache_dir: Optional[str] = None,
    open_browser: bool = True,
) -> None:
    """阻塞式启动本地可视化服务。"""
    planner = Planner(token=token, cache_dir=cache_dir or os.path.join(os.getcwd(), ".cache"))
    handler = type("BoundHandler", (_Handler,), {"planner": planner})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"\n  麦麦精算师 Web 界面已启动：{url}\n  按 Ctrl+C 退出。\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
