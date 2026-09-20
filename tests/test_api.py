"""HTTP 接口端到端测试。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from src.api import make_handler
from tests._support import DAY1_10, clock, links, zones

from src.service import FlowService
import tempfile, os


def _request(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        d = tempfile.mkdtemp()
        self.svc = FlowService(zones(), links(), os.path.join(d, "j.jsonl"), clock=clock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler({"service": self.svc}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.svc.close()

    def test_status_and_event_tick_flow(self) -> None:
        code, status = _request(self.base, "GET", "/status")
        self.assertEqual(200, code)
        self.assertEqual("normal", status["zones"]["waterfall"]["risk"])

        code, res = _request(self.base, "POST", "/events", {
            "event_id": "e1", "event_type": "gate_enter", "source": "gate",
            "zone": "waterfall", "occurred_at": DAY1_10.isoformat(), "quantity": 1801,
        })
        self.assertEqual(200, code)
        self.assertTrue(res["accepted"])

        _request(self.base, "POST", "/events", {
            "event_id": "c1", "event_type": "camera_count", "source": "camera",
            "zone": "waterfall", "occurred_at": DAY1_10.isoformat(), "quantity": 1801,
        })
        _, decision = _request(self.base, "POST", "/ticks", {})
        self.assertTrue(any(a["type"] == "entry_stop" for a in decision["actions"]))

        _, status = _request(self.base, "GET", "/status")
        self.assertEqual("critical", status["zones"]["waterfall"]["risk"])

    def test_override_requires_fields_and_receipt_roundtrip(self) -> None:
        # 缺责任人 -> 400
        import urllib.error
        try:
            _request(self.base, "POST", "/overrides", {
                "override_id": "X", "zone": "waterfall", "reason": "暴雨",
                "expires_at": (DAY1_10.replace(hour=12)).isoformat(),
            })
            self.fail("应当拒绝")
        except urllib.error.HTTPError as e:
            self.assertEqual(400, e.code)

        _, ov = _request(self.base, "POST", "/overrides", {
            "override_id": "OV1", "zone": "gate-east", "reason": "雷电",
            "owner": "值班长", "expires_at": DAY1_10.replace(hour=12).isoformat(),
            "mode": "suppress_auto",
        })
        self.assertEqual("OV1", ov["override_id"])

        _, decision = _request(self.base, "POST", "/ticks", {})
        code, chain = _request(self.base, "GET", f"/chain?decision_id={decision['decision_id']}")
        self.assertEqual(200, code)
        self.assertEqual(decision["decision_id"], chain["anchor"])


if __name__ == "__main__":
    unittest.main()
