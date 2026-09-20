"""值班室 HTTP 接口（仅标准库，可独立运行）。

端点：
  GET  /health                      存活与资料概况
  GET  /status                      当前风险、数据新鲜度、动作依据与执行回执
  POST /events                      上报一批原始事件（可迟到/乱序/重复）
  POST /evaluate                    立即评估（可传 now=ISO8601 用于演练/补算）
  POST /overrides                   新建人工接管（必须带期限/理由/责任人）
  POST /overrides/{id}/revoke       提前撤销接管
  POST /receipts                    执行回执 ack/executed/failed
  GET  /replay?day=YYYY-MM-DD       回放某日完整决策链
  GET  /overrides                   接管记录（含已过期/已撤销）

启动：
  python -m src.api --zones fixtures/zones.json --site fixtures/site.json \
      --journal data/run.jsonl --port 8080 --tick-seconds 60
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .domain import load_site
from .engine import FlowEngine
from .events import parse_time
from .overrides import OverrideError
from .store import Journal


class ApiContext:
    def __init__(self, engine: FlowEngine, tick_seconds: int):
        self.engine = engine
        self.tick_seconds = tick_seconds
        self.lock = threading.RLock()
        self.last_evaluation = None
        self._timer: threading.Timer | None = None

    def evaluate(self, now: datetime | None = None, persist: bool = True):
        with self.lock:
            self.last_evaluation = self.engine.evaluate(now=now, persist=persist)
            return self.last_evaluation

    def start_ticker(self) -> None:
        if self.tick_seconds <= 0:
            return

        def _tick():
            try:
                self.evaluate()
            finally:
                self._timer = threading.Timer(self.tick_seconds, _tick)
                self._timer.daemon = True
                self._timer.start()

        self._timer = threading.Timer(self.tick_seconds, _tick)
        self._timer.daemon = True
        self._timer.start()


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict | list) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    ctx: ApiContext = None  # 由 build_server 注入到子类

    def log_message(self, fmt, *args):  # 精简访问日志
        pass

    # ---------- GET ----------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        try:
            if path == "/health":
                site = self.ctx.engine.site
                _json_response(self, 200, {
                    "status": "ok",
                    "zones": len(site.zones),
                    "passages": len(site.passages),
                    "shuttles": len(site.shuttles),
                    "tick_seconds": self.ctx.tick_seconds,
                })
            elif path == "/status":
                ev = self.ctx.last_evaluation or self.ctx.evaluate()
                _json_response(self, 200, self.ctx.engine.status_payload(ev))
            elif path == "/replay":
                day = qs.get("day", [None])[0]
                if not day:
                    _json_response(self, 400, {"error": "需要 day=YYYY-MM-DD"})
                    return
                _json_response(self, 200, self.ctx.engine.replay(day))
            elif path == "/overrides":
                now = datetime.now(timezone.utc)
                view = self.ctx.engine.override_view(now)
                _json_response(self, 200, {
                    "active": [o.to_dict() for o in view.active],
                    "inactive": [o.to_dict() for o in view.inactive],
                })
            else:
                _json_response(self, 404, {"error": f"未知路径 {path}"})
        except Exception as exc:  # noqa: BLE001
            _json_response(self, 500, {"error": f"{type(exc).__name__}: {exc}"})

    # ---------- POST ----------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path.rstrip("/") or "/")
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            body = self._read_json()
        except ValueError as exc:
            _json_response(self, 400, {"error": str(exc)})
            return

        try:
            if path == "/events":
                now = _opt_time(qs.get("now"))
                raws = body["events"] if isinstance(body, dict) and "events" in body else body
                if not isinstance(raws, list):
                    _json_response(self, 400, {"error": "events 必须是数组或 {'events': [...]}"})
                    return
                with self.ctx.lock:
                    summary = self.ctx.engine.ingest_many(raws, now)
                _json_response(self, 202, summary)

            elif path == "/evaluate":
                now = _opt_time(qs.get("now")) or _opt_time_from_body(body)
                persist = bool(qs.get("persist", ["1"])[0] not in ("0", "false"))
                ev = self.ctx.evaluate(now=now, persist=persist)
                _json_response(self, 200, self.ctx.engine.status_payload(ev))

            elif path == "/overrides":
                required = ("override_id", "kind", "ttl_seconds", "owner", "reason", "params")
                missing = [k for k in required if k not in body]
                if missing:
                    _json_response(self, 400, {"error": f"缺少字段: {missing}"})
                    return
                now = _opt_time(qs.get("now"))
                with self.ctx.lock:
                    ov = self.ctx.engine.add_override(
                        body["override_id"], body["kind"], int(body["ttl_seconds"]),
                        body["owner"], body["reason"], body["params"], now=now,
                    )
                _json_response(self, 201, ov.to_dict())

            elif path.startswith("/overrides/") and path.endswith("/revoke"):
                ov_id = path.split("/")[2]
                now = _opt_time(qs.get("now"))
                with self.ctx.lock:
                    ov = self.ctx.engine.revoke_override(
                        ov_id, body.get("by", ""), body.get("reason", ""), now=now
                    )
                _json_response(self, 200, ov.to_dict())

            elif path == "/receipts":
                required = ("code", "status", "by")
                missing = [k for k in required if k not in body]
                if missing:
                    _json_response(self, 400, {"error": f"缺少字段: {missing}"})
                    return
                now = _opt_time(qs.get("now"))
                with self.ctx.lock:
                    rec = self.ctx.engine.post_receipt(
                        body["code"], body["status"], body["by"],
                        body.get("detail", ""), now=now, day=body.get("day"),
                    )
                _json_response(self, 200, rec)

            else:
                _json_response(self, 404, {"error": f"未知路径 {path}"})
        except OverrideError as exc:
            _json_response(self, 422, {"error": str(exc)})
        except (KeyError, ValueError) as exc:
            _json_response(self, 400, {"error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:  # noqa: BLE001
            _json_response(self, 500, {"error": f"{type(exc).__name__}: {exc}"})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, (dict, list)):
            raise ValueError("JSON 顶层必须是对象或数组")
        return data


def _opt_time(values: list[str] | None) -> datetime | None:
    if not values:
        return None
    return parse_time(values[0])


def _opt_time_from_body(body: dict) -> datetime | None:
    if isinstance(body, dict) and body.get("now"):
        return parse_time(body["now"])
    return None


def build_server(zones_path: str, site_path: str, journal_path: str,
                 port: int, tick_seconds: int,
                 static_blocks: frozenset[str] = frozenset()) -> tuple[ThreadingHTTPServer, ApiContext]:
    site = load_site(zones_path, site_path)
    engine = FlowEngine(site, Journal(journal_path), block_passages=static_blocks)
    engine.restore()
    ctx = ApiContext(engine, tick_seconds)

    class _BoundHandler(Handler):
        pass

    _BoundHandler.ctx = ctx
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _BoundHandler)
    return httpd, ctx


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="景区客流联动服务")
    ap.add_argument("--zones", default="fixtures/zones.json")
    ap.add_argument("--site", default="fixtures/site.json")
    ap.add_argument("--journal", default="data/run.jsonl")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--tick-seconds", type=int, default=60)
    ap.add_argument("--block-passage", action="append", default=[],
                    help="启动时常态封闭的通道代码，可重复")
    args = ap.parse_args(argv)

    httpd, ctx = build_server(
        args.zones, args.site, args.journal, args.port, args.tick_seconds,
        static_blocks=frozenset(args.block_passage),
    )
    ctx.start_ticker()
    print(f"客流联动服务已启动: http://0.0.0.0:{args.port} （每 {args.tick_seconds}s 评估一次）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        ctx.engine.journal.close()


if __name__ == "__main__":
    main()
