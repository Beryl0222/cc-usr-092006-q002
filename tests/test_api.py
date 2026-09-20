import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

from src.api import build_server


def _request(port, method, path, body=None):
    # 对 query string 中的保留字符（如时区偏移的 +）做编码
    if "?" in path:
        base, query = path.split("?", 1)
        path = base + "?" + urllib.parse.quote(query, safe="=&")
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.journal = str(Path(self._tmp.name) / "run.jsonl")
        # tick_seconds=0：不启动后台定时器，结果只由显式 /evaluate 驱动，保持确定
        self.httpd, self.ctx = build_server(
            str(Path(__file__).resolve().parents[1] / "fixtures" / "zones.json"),
            str(Path(__file__).resolve().parents[1] / "fixtures" / "site.json"),
            self.journal, port=0, tick_seconds=0,
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.ctx.engine.journal.close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    NOW = "2026-09-20T10:00:00+00:00"

    def test_health(self):
        code, body = _request(self.port, "GET", "/health")
        self.assertEqual(200, code)
        self.assertEqual("ok", body["status"])

    def test_dedup_evaluate_status_receipt_override(self):
        # 1) 上报：含重复事件
        code, body = _request(self.port, "POST", f"/events?now={self.NOW}", {
            "events": [
                {"event_id": "c1", "source": "camera", "type": "snapshot",
                 "zone": "waterfall", "value": 1760, "occurred_at": "2026-09-20T09:59:00+00:00"},
                {"event_id": "c1", "source": "camera", "type": "snapshot",
                 "zone": "waterfall", "value": 1760, "occurred_at": "2026-09-20T09:59:00+00:00"},
                {"event_id": "g1", "source": "gate", "type": "flow",
                 "zone": "gate-east", "value": 100, "occurred_at": "2026-09-20T09:59:30+00:00"},
            ]})
        self.assertEqual(202, code)
        self.assertEqual(2, body["accepted"])
        self.assertEqual(1, body["duplicates"])

        # 2) 评估
        code, status = _request(self.port, "POST", f"/evaluate?now={self.NOW}", {})
        self.assertEqual(200, code)
        self.assertEqual("red", status["overall_risk"])
        measures = {m["code"]: m for m in status["measures"]}
        self.assertIn("throttle:gate-east", measures)
        self.assertEqual(40, measures["throttle:gate-east"]["params"]["admit_per_interval"])

        # 3) 数据新鲜度可见
        wf = status["zones"]["waterfall"]
        self.assertTrue(any(f["source"] == "camera" for f in wf["freshness"]))

        # 4) 接管缺责任人被拒（400 缺字段 / 422 语义错误）
        code, _ = _request(self.port, "POST", f"/overrides?now={self.NOW}", {
            "override_id": "o1", "kind": "throttle", "ttl_seconds": 600,
            "reason": "雷电", "params": {"zone": "gate-east", "admit_per_interval": 10}})
        self.assertEqual(400, code)
        code, _ = _request(self.port, "POST", f"/overrides?now={self.NOW}", {
            "override_id": "o1", "kind": "throttle", "ttl_seconds": 600,
            "owner": "赵", "reason": "雷电",
            "params": {"zone": "gate-east", "admit_per_interval": 10}})
        self.assertEqual(201, code)

        # 5) 非法回执流转被拒
        code, _ = _request(self.port, "POST", "/receipts", {
            "code": "throttle:gate-east", "status": "executed", "by": "岗",
            "day": "2026-09-20"})
        self.assertEqual(400, code)
        code, _ = _request(self.port, "POST", "/receipts", {
            "code": "throttle:gate-east", "status": "ack", "by": "岗",
            "day": "2026-09-20"})
        self.assertEqual(200, code)

        # 6) /replay 可还原决策链
        code, chain = _request(self.port, "GET", "/replay?day=2026-09-20")
        self.assertEqual(200, code)
        self.assertEqual(2, len(chain["events"]))
        self.assertGreaterEqual(len(chain["ticks"]), 1)
        self.assertEqual(1, len(chain["receipts"]))


if __name__ == "__main__":
    unittest.main()
