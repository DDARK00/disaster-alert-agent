"""FastAPI/SSE 통합 테스트 — 프로세스 내 MCP + FakeLLM."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "mcp_server"))

from fastapi.testclient import TestClient  # noqa: E402

import server as mcp_module  # noqa: E402
from agent.llm import FakeLLM  # noqa: E402
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("backend_server", ROOT / "backend" / "server.py")
backend_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend_server)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        ev = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if ev:
            out.append((ev, data))
    return out


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = backend_server.create_app(mcp_target=mcp_module.mcp, llm=FakeLLM())
        cls.cm = TestClient(cls.app)
        cls.client = cls.cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)

    def test_health_and_version(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mcp"], True)
        self.assertEqual(self.client.get("/version").json()["version"], backend_server.VERSION)

    def test_scenarios_are_valid(self):
        sc = self.client.get("/scenarios").json()
        self.assertGreaterEqual(len(sc), 8)
        self.assertEqual(len({s["id"] for s in sc}), len(sc))
        for s in sc:
            self.assertTrue(s["message"])
            self.assertIn(s["mode"], ("replay", "live"))

    def test_sse_event_sequence_and_final(self):
        r = self.client.get("/chat/stream", params={"message": "경기도 광주 폭염특보 났는데 지금 나가도 돼?",
                                                    "mode": "replay", "as_of": "2026-08-24 12:00"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        events = parse_sse(r.text)
        names = [e for e, _ in events]
        self.assertEqual(names[0], "start")
        self.assertEqual(names[-1], "final")
        for must in ("step", "route", "intent", "location", "plan", "source", "judgement", "validation", "token"):
            self.assertIn(must, names)
        self.assertLess(names.index("judgement"), names.index("token"))  # 카드가 문장보다 먼저
        final = events[-1][1]
        streamed = "".join(d["text"] for e, d in events if e == "token")
        self.assertEqual(streamed, final["answer"])
        self.assertEqual(final["judgements"][0]["level"], "warning")
        self.assertEqual(final["meta"]["mode"], "replay")

    def test_sse_emergency_is_short_and_has_no_sources(self):
        r = self.client.get("/chat/stream", params={"message": "물이 차오르고 있어 도와줘"})
        names = [e for e, _ in parse_sse(r.text)]
        self.assertIn("final", names)
        self.assertNotIn("source", names)
        self.assertNotIn("judgement", names)

    def test_chat_json_matches_stream_shape(self):
        r = self.client.get("/chat", params={"message": "광주 호우 어때?", "mode": "replay", "as_of": "2026-08-27 17:00"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["judgements"]), 2)

    def test_validation_errors(self):
        self.assertEqual(self.client.get("/chat", params={"message": ""}).status_code, 422)
        self.assertEqual(self.client.get("/chat/stream").status_code, 422)

    def test_bad_as_of_surfaces_as_degraded_info_not_crash(self):
        r = self.client.get("/chat/stream", params={"message": "경기도 광주 폭염", "mode": "replay", "as_of": "어제쯤"})
        events = parse_sse(r.text)
        last = events[-1]
        # 잘못된 시각은 도구 오류 → 격리되어 '정보 없음'으로 응답하거나 error 이벤트로 끝난다. 어느 쪽이든 '안전'이라 말하지 않는다.
        if last[0] == "final":
            self.assertEqual(last[1]["judgements"][0]["level"], "unknown")
            self.assertNotIn("안전합니다", last[1]["answer"])
        else:
            self.assertEqual(last[0], "error")


if __name__ == "__main__":
    unittest.main()
