"""值班室 HTTP 接口（仅标准库，可独立运行）。

路由：
* GET  /status                 当前风险、数据新鲜度、动作与回执
* POST /events                 上报一条事件（迟到/乱序/重复均可）
* POST /ticks                  驱动一次策略决策
* POST /overrides              人工接管（必须含期限、理由、责任人）
* DELETE /overrides?id=        提前解除接管
* POST /closures              临时封闭通道
* POST /closures/reopen        解除封闭
* POST /receipts               登记动作执行回执
* GET  /chain?decision_id=&window=5  某次拥堵前后的完整决策链

启动：
    python -m src.api --zones fixtures/zones.json --links fixtures/links.json \
        --journal data/journal.jsonl --port 8080
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .clock import SystemClock
from .domain import PolicyConfig, Schedule, load_zones
from .service import FlowService
from .topology import load_links


def build_service(zones_path: str, links_path: str, journal_path: str,
                  schedule: Schedule | None = None) -> FlowService:
    return FlowService(
        zones=load_zones(zones_path),
        links=load_links(links_path),
        journal_path=journal_path,
        clock=SystemClock(),
        config=PolicyConfig(),
        schedule=schedule or Schedule(),
        entry_zones=("gate-east",),
        exit_zones=("gate-east",),
    )


def make_handler(service_holder: dict):
    service: FlowService = service_holder["service"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args) -> None:  # 安静日志
            pass

        def _send(self, code: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        # ---------------------------------------------------------- GET
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/status":
                    self._send(200, service.status())
                elif parsed.path == "/chain":
                    q = parse_qs(parsed.query)
                    self._send(200, service.chain(
                        q.get("decision_id", [None])[0],
                        int(q.get("window", ["5"])[0]),
                    ))
                elif parsed.path == "/health":
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "not found"})
            except Exception as exc:  # 接口层不外泄堆栈，返回结构化错误
                self._send(400, {"error": type(exc).__name__, "message": str(exc)})

        # ---------------------------------------------------------- POST
        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                body = self._body()
                if parsed.path == "/events":
                    result = service.ingest(
                        event_id=body["event_id"],
                        event_type=body["event_type"],
                        source=body["source"],
                        zone=body["zone"],
                        occurred_at=datetime.fromisoformat(body["occurred_at"]),
                        quantity=int(body.get("quantity", 1)),
                        slot_start=_dt(body.get("slot_start")),
                        correlation_id=body.get("correlation_id"),
                        target_zone=body.get("target_zone"),
                    )
                    self._send(200 if result["accepted"] else 202, result)
                elif parsed.path == "/ticks":
                    self._send(200, service.tick())
                elif parsed.path == "/overrides":
                    payload = service.add_override(
                        override_id=body["override_id"],
                        zone=body["zone"],
                        reason=body["reason"],
                        owner=body["owner"],
                        expires_at=datetime.fromisoformat(body["expires_at"]),
                        mode=body.get("mode", "manual_actions"),
                        actions=body.get("actions"),
                    )
                    self._send(201, payload)
                elif parsed.path == "/closures":
                    payload = service.add_closure(
                        link_key=tuple(body["link_key"]),
                        reason=body["reason"],
                        owner=body["owner"],
                        expires_at=datetime.fromisoformat(body["expires_at"]),
                    )
                    self._send(201, payload)
                elif parsed.path == "/closures/reopen":
                    service.reopen(tuple(body["link_key"]))
                    self._send(200, {"reopened": body["link_key"]})
                elif parsed.path == "/receipts":
                    payload = service.record_receipt(
                        action_id=body["action_id"],
                        status=body["status"],
                        operator=body["operator"],
                        note=body.get("note", ""),
                    )
                    self._send(201, payload)
                else:
                    self._send(404, {"error": "not found"})
            except Exception as exc:
                self._send(400, {"error": type(exc).__name__, "message": str(exc)})

        # ---------------------------------------------------------- DELETE
        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/overrides":
                    q = parse_qs(parsed.query)
                    service.cancel_override(q["id"][0])
                    self._send(200, {"cancelled": q["id"][0]})
                else:
                    self._send(404, {"error": "not found"})
            except Exception as exc:
                self._send(400, {"error": type(exc).__name__, "message": str(exc)})

    return Handler


def serve(zones_path: str, links_path: str, journal_path: str, port: int = 8080) -> None:
    holder = {"service": build_service(zones_path, links_path, journal_path)}
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(holder))
    print(f"客流联动服务监听 :{port}，日志 {journal_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        holder["service"].close()
        server.server_close()


def _dt(value):
    return datetime.fromisoformat(value) if value else None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--zones", default="fixtures/zones.json")
    parser.add_argument("--links", default="fixtures/links.json")
    parser.add_argument("--journal", default="data/journal.jsonl")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve(args.zones, args.links, args.journal, args.port)
