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
        h = r.json()
        self.assertIn("sources", h)  # 소스별 최근 성공·실패·원인 (ADR-014)
        self.assertIn("last_cause", h["mcp_link"])
        self.assertNotIn("serviceKey", r.text)
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


class Staged(unittest.TestCase):
    """단계별 표시 (ADR-019): judgement → evidence → quick → final. 검증 전 LLM 문장은 보내지 않는다."""
    Q = {"message": "경기도 광주 폭염특보 났는데 지금 나가도 돼?", "mode": "replay", "as_of": "2026-08-24 12:00"}
    BAD = "지금은 안전합니다"  # FakeLLM(bad_write_times) 의 검증 위반 답변

    def stream(self, llm, params=None):
        app = backend_server.create_app(mcp_target=mcp_module.mcp, llm=llm)
        with TestClient(app) as c:
            return parse_sse(c.get("/chat/stream", params=params or self.Q).text)

    def test_order_and_quick_is_validated_template(self):
        events = self.stream(FakeLLM())
        names = [e for e, _ in events]
        order = [names.index(n) for n in ("judgement", "evidence", "quick", "final")]
        self.assertEqual(order, sorted(order))
        self.assertLess(names.index("quick"), names.index("token"))
        self.assertIn("progress", names)
        quick = dict(events)["quick"]
        self.assertTrue(quick["validation"]["ok"])
        self.assertEqual(quick["answer_source"], "template")
        ev = dict(events)["evidence"]["regions"][0]
        self.assertEqual(ev["level"], "warning")
        self.assertTrue(ev["alerts"] and ev["guide_groups"] and ev["shelters"])
        self.assertTrue(all(g["items"] for g in ev["guide_groups"]))

    def test_unvalidated_llm_text_never_sent_and_quick_becomes_final(self):
        events = self.stream(FakeLLM(bad_write_times=99))
        final = events[-1][1]
        self.assertEqual(events[-1][0], "final")
        self.assertEqual(final["answer_source"], "template_fallback")
        self.assertEqual(final["answer"], dict(events)["quick"]["answer"])  # 검증이 끝내 실패하면 quick 이 최종
        self.assertFalse(any(self.BAD in json.dumps(d, ensure_ascii=False) for _, d in events))

    def test_llm_down_quick_is_final(self):
        events = self.stream(FakeLLM(fail_tasks={"intent", "write"}))
        final = events[-1][1]
        self.assertEqual(final["answer_source"], "template_llm_error")
        self.assertEqual(final["answer"], dict(events)["quick"]["answer"])

    def test_frontend_banner_is_the_answer_disclaimer(self):
        # 화면은 답변의 면책 줄을 배너와 같은 문장일 때만 생략한다 — 문구가 어긋나면 면책이 사라지지 않게 고정
        from config.catalog import msg
        self.assertIn(msg("disclaimer"), (ROOT / "frontend" / "index.html").read_text(encoding="utf-8"))

    def test_fixed_route_has_no_evidence_or_quick(self):
        names = [e for e, _ in self.stream(FakeLLM(), {"message": "물이 차오르고 있어 도와줘"})]
        self.assertNotIn("evidence", names)
        self.assertNotIn("quick", names)
        self.assertEqual(names[-1], "final")

    def test_progress_texts_are_plain_and_failure_uses_cause_phrase(self):
        sys.path.insert(0, str(ROOT / "eval"))
        import asyncio
        from faults import DownGateway
        from agent.deps import Deps, Settings
        from agent.graph import build_graph
        from config.catalog import msg

        async def go():
            graph = build_graph(Deps(mcp=DownGateway(), llm=FakeLLM(), settings=Settings(data_mode="replay")))
            evs: list = []
            await graph.ainvoke({"query": "경기도 광주 폭염", "mode": "replay", "as_of": ""},
                                config={"configurable": {"emit": evs.append}})
            return evs
        evs = asyncio.run(go())
        prog = [e for e in evs if e["event"] == "progress"]
        self.assertTrue(any(e["stage"] == "source_failed" and msg("causes.mcp_unreachable") in e["text"] for e in prog))
        self.assertFalse(any(e["event"] in ("evidence", "quick") for e in evs))  # MCP 중단: 근거 없음 → 고정 응답만


class DemoFaults(unittest.TestCase):
    """시연용 장애 주입은 DEMO_FAULTS=1 일 때만 (ADR-021). 기본은 목록에 없고 요청은 404."""
    Q = {"message": "경기도 광주 폭염 어때?", "mode": "replay", "as_of": "2026-08-24 12:00"}

    def client(self, on: bool):
        import os
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k != "DEMO_FAULTS"} | ({"DEMO_FAULTS": "1"} if on else {})
        with mock.patch.dict(os.environ, env, clear=True):
            cm = TestClient(backend_server.create_app(mcp_target=mcp_module.mcp, llm=FakeLLM()))
            c = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        return c

    def test_off_by_default_hidden_and_rejected(self):
        c = self.client(False)
        self.assertFalse([s for s in c.get("/scenarios").json() if s.get("fault")])
        for path in ("/chat", "/chat/stream"):
            self.assertEqual(c.get(path, params={**self.Q, "fault": "mcp_down"}).status_code, 404, path)
        self.assertEqual(c.get("/chat", params=self.Q).status_code, 200)  # fault 없는 요청은 그대로

    def test_on_each_fault_shows_its_cause_phrase(self):
        from config.catalog import msg
        c = self.client(True)
        scs = [s for s in c.get("/scenarios").json() if s.get("fault")]
        self.assertEqual({s["fault"] for s in scs}, set(backend_server.FAULTS))
        self.assertEqual(c.get("/chat", params={**self.Q, "fault": "nope"}).status_code, 400)
        for s in scs:
            f = c.get("/chat", params={"message": s["message"], "mode": s["mode"], "as_of": s["as_of"], "fault": s["fault"]}).json()
            want = s["check"]["cause"]
            self.assertIn(want, {x.get("cause") for x in f["meta"]["failures"]}, s["id"])
            self.assertIn(msg(f"causes.{want}"), f["answer"], s["id"])
            self.assertIn("정보 없음", f["answer"])
        # 장애 주입은 요청 단위 — 다음 일반 요청은 정상
        self.assertEqual(c.get("/chat", params=self.Q).json()["judgements"][0]["level"], "warning")


if __name__ == "__main__":
    unittest.main()
