"""카카오 장소(resolve_place) 흐름 — Kakao 는 mock 만 사용 (ADR-011). 실호출·저장 없음."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "mcp_server"))

import kakao  # noqa: E402
import server as mcp_module  # noqa: E402
from agent.deps import Deps, Settings  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.llm import FakeLLM  # noqa: E402
from agent.mcp_client import MCPGateway  # noqa: E402
from agent.validator import validate, validate_links  # noqa: E402

METRO = {"status": "ok", "place_id": "111", "result_count": 5, "b_code": "1220000000", "region_names": ["전남광주통합특별시", "광산구"]}
GYEONGGI = {"status": "ok", "place_id": "222", "result_count": 5, "b_code": "4161000000", "region_names": ["경기도", "광주시"]}
GOYANG = {"status": "ok", "place_id": "333", "result_count": 9, "b_code": "4128100000", "region_names": ["경기도", "고양시 덕양구"]}


def run(query, place, kakao_result, **extra):
    async def go():
        gw = MCPGateway(mcp_module.mcp)
        await gw.start()
        try:
            g = build_graph(Deps(mcp=gw, llm=FakeLLM(place=place), settings=Settings(max_rewrites=1)))
            events: list = []
            st = await g.ainvoke({"query": query, "mode": "replay", "as_of": "2026-08-27 17:00", **extra},
                                 config={"configurable": {"emit": events.append}})
            return st, events
        finally:
            await gw.stop()
    with mock.patch.object(kakao, "lookup", return_value=kakao_result) as m:
        st, events = asyncio.run(go())
    links = [e for e in events if e.get("event") == "links"]
    return st, links, m


class PlaceFlow(unittest.TestCase):
    def test_no_region_words_place_gives_assumed_region_and_chips(self):
        st, links, m = run("첨단지구 비 많이 와?", "첨단지구", METRO)
        m.assert_called_once_with("첨단지구")
        self.assertEqual((st["location"]["status"], st["regions"]), ("assumed", ["gwangju_metro"]))
        self.assertEqual([c["url"] for c in links[0]["chips"]],
                         ["https://map.kakao.com/link/map/111", "https://map.kakao.com/link/to/111"])
        self.assertEqual(links[0]["notice"], "경로상의 도로 통제·침수 여부는 반영되지 않았습니다.")
        self.assertTrue(st["final"]["validation"]["ok"])

    def test_links_never_enter_graph_state(self):  # 상태는 LangSmith 트레이스로 가므로 링크·장소 ID 를 두지 않는다
        st, links, _ = run("첨단지구 비 많이 와?", "첨단지구", METRO)
        self.assertTrue(links)
        blob = json.dumps(st, ensure_ascii=False, default=str)
        self.assertNotIn("map.kakao.com", blob)
        self.assertNotIn('"111"', blob)

    def test_explicit_region_is_not_overridden(self):
        st, links, _ = run("경기도 광주 터미널 비 와?", "터미널", GOYANG)
        self.assertEqual(st["regions"], ["gyeonggi_gwangju"])
        self.assertEqual(links, [])  # 지원 지역 밖 장소 → 칩 없음
        st, links, _ = run("경기도 광주 첨단지구 비 와?", "첨단지구", METRO)
        self.assertEqual(st["regions"], ["gyeonggi_gwangju"])
        self.assertEqual(links, [])  # 장소 지역 ≠ 명시 지역 → 칩 없음

    def test_ambiguous_stays_ambiguous(self):
        st, links, _ = run("광주 첨단지구 호우 어때?", "광주 첨단지구", METRO)
        self.assertEqual(st["location"]["status"], "ambiguous")
        self.assertEqual(set(st["regions"]), {"gwangju_metro", "gyeonggi_gwangju"})

    def test_conflict_with_history_shows_both(self):
        st, _, _ = run("첨단지구 비 와?", "첨단지구", METRO, history_region_id="gyeonggi_gwangju")
        self.assertEqual(st["location"]["status"], "ambiguous")
        self.assertEqual(set(st["regions"]), {"gwangju_metro", "gyeonggi_gwangju"})
        st, links, _ = run("곤지암 쪽 비 와?", "곤지암 쪽", GYEONGGI, history_region_id="gyeonggi_gwangju")
        self.assertEqual(st["regions"], ["gyeonggi_gwangju"])
        self.assertTrue(links)

    def test_region_only_or_invented_place_is_not_sent(self):
        st, _, m = run("광주 호우 어때?", "광주", METRO)
        m.assert_not_called()
        self.assertEqual(st["location"]["status"], "ambiguous")
        _, _, m = run("광주 호우 어때?", "서울역", METRO)  # 원문에 없는 장소(LLM 지어냄)
        m.assert_not_called()

    def test_vague_place_is_not_sent(self):  # T-033: '우리 동네'가 Kakao 1위 장소의 지역으로 가정되던 문제
        for q, place in (("우리 동네 날씨 위험해?", "우리 동네"), ("여기 비 와?", "여기"), ("이 근처 폭염 어때?", "이 근처")):
            st, links, m = run(q, place, GOYANG)
            m.assert_not_called()
            self.assertEqual(st["final"]["route"], "ask_region", q)
            self.assertEqual(links, [])

    def test_kakao_failure_changes_nothing(self):
        for r in ({"status": "failed", "detail": "HTTP 500"}, {"status": "unavailable", "detail": "키 없음"}):
            st, links, _ = run("첨단지구 비 와?", "첨단지구", r)
            self.assertEqual(st["regions"], [])  # 지역 미확인 → 되묻기 (기존 동작)
            self.assertEqual(st["final"]["route"], "ask_region")
            self.assertEqual(links, [])


class LinkRules(unittest.TestCase):
    def test_answer_with_url_rejected(self):
        st, _, _ = run("첨단지구 비 많이 와?", "첨단지구", METRO)
        js = st["judgements"]
        v = validate(st["final"]["answer"] + "\n자세한 위치: https://map.kakao.com/link/map/111", js)
        self.assertIn("답변 본문에 링크 금지", v["violations"])

    def test_validate_links(self):
        ok = {"map": "https://map.kakao.com/link/map/1", "route": "https://map.kakao.com/link/to/1"}
        notice = "경로상의 도로 통제·침수 여부는 반영되지 않았습니다."
        self.assertIsNotNone(validate_links(ok, notice, "R"))
        self.assertIsNone(validate_links(ok, None, "R"))
        self.assertIsNone(validate_links({**ok, "route": "https://evil.example/link/to/1"}, notice, "R"))
        self.assertIsNone(validate_links({**ok, "map": "https://map.kakao.com/link/map/1?x=<script>"}, notice, "R"))

    def test_route_safety_wording_forbidden(self):
        st, _, _ = run("첨단지구 비 많이 와?", "첨단지구", METRO)
        for w in ("가장 안전한 경로입니다", "안전한 길로 가세요"):
            self.assertFalse(validate(st["final"]["answer"] + "\n" + w, st["judgements"])["ok"])


class Api(unittest.TestCase):
    def test_chat_and_stream_carry_links_outside_answer(self):
        import importlib.util
        from fastapi.testclient import TestClient
        spec = importlib.util.spec_from_file_location("backend_server_place", ROOT / "backend" / "server.py")
        bs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bs)
        app = bs.create_app(mcp_target=mcp_module.mcp, llm=FakeLLM(place="첨단지구"))
        q = {"message": "첨단지구 비 많이 와?", "mode": "replay", "as_of": "2026-08-27 17:00"}
        with mock.patch.object(kakao, "lookup", return_value=METRO), TestClient(app) as c:
            f = c.get("/chat", params=q).json()
            self.assertEqual(f["links"]["chips"][1]["url"], "https://map.kakao.com/link/to/111")
            self.assertNotIn("kakao", f["answer"])
            body = c.get("/chat/stream", params=q).text
        names = [b.split("\n")[0][7:] for b in body.strip().split("\n\n")]
        self.assertLess(names.index("links"), names.index("final"))
        with TestClient(bs.create_app(mcp_target=mcp_module.mcp, llm=FakeLLM())) as c:
            self.assertIsNone(c.get("/chat", params={**q, "message": "경기도 광주 비 와?"}).json()["links"])


if __name__ == "__main__":
    unittest.main()
