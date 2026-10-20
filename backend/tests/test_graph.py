"""그래프 통합 테스트 — 프로세스 내 MCP 서버(replay) + FakeLLM."""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "mcp_server"))

import server as mcp_module  # noqa: E402  (MCP 서버)
from agent.deps import Deps, Settings  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.llm import FakeLLM  # noqa: E402
from agent.mcp_client import MCPGateway  # noqa: E402
from config.catalog import msg  # noqa: E402


async def run(query, *, llm=None, as_of="2026-08-24 12:00", mode="replay", mcp=None, **extra):
    gw = MCPGateway(mcp or mcp_module.mcp)
    await gw.start()
    try:
        llm = llm or FakeLLM()
        graph = build_graph(Deps(mcp=gw, llm=llm, settings=Settings(data_mode="replay", max_rewrites=1)))
        events: list = []
        state = await graph.ainvoke({"query": query, "mode": mode, "as_of": as_of, **extra},
                                    config={"configurable": {"emit": events.append}})
        return state["final"], events, llm
    finally:
        await gw.stop()


def R(*a, **k):
    return asyncio.run(run(*a, **k))


class Flow(unittest.TestCase):
    def test_active_heat_warning_end_to_end(self):
        final, events, _ = R("경기도 광주 폭염 어때? 나가도 돼?")
        self.assertEqual(final["route"], "disaster")
        self.assertEqual(final["intent"], "go_out")
        j = final["judgements"][0]
        self.assertEqual((j["region_id"], j["level"]), ("gyeonggi_gwangju", "warning"))
        self.assertIn("폭염", j["hazards"])
        self.assertTrue(final["validation"]["ok"])
        self.assertIn("외출 가능 여부는 이 서비스가 판단하지 않습니다", final["answer"])
        names = [e["node"] for e in events if e.get("event") == "step" and e["status"] == "done"]
        self.assertEqual(names, ["route", "parse_intent", "gate", "plan", "research", "judge", "write", "validate", "finalize"])
        self.assertIn("meta", final)
        self.assertEqual(final["meta"]["mode"], "replay")

    def test_ambiguous_gwangju_queries_both_regions(self):
        final, events, _ = R("광주 호우 어때", as_of="2026-08-27 17:00")
        self.assertEqual({j["region_id"] for j in final["judgements"]}, {"gwangju_metro", "gyeonggi_gwangju"})
        self.assertEqual(final["location"]["status"], "ambiguous")
        self.assertTrue(final["validation"]["ok"])
        by = {j["region_id"]: j for j in final["judgements"]}
        self.assertIn("호우", by["gwangju_metro"]["hazards"])

    def test_no_alert_is_not_safe_claim_and_messages_do_not_raise_level(self):
        final, _, _ = R("경기도 광주 강풍 상황 알려줘", as_of="2026-10-18 16:30")
        j = final["judgements"][0]
        self.assertEqual(j["level"], "none")
        self.assertTrue(any(e["kind"] == "message" for e in j["evidence"]))
        self.assertIn("확인된 위험 정보 없음", final["answer"])
        self.assertNotIn("안전합니다", final["answer"])

    def test_out_of_coverage_is_unknown(self):
        final, _, _ = R("경기도 광주 폭염", as_of="2026-06-01 12:00")
        self.assertEqual(final["judgements"][0]["level"], "unknown")
        self.assertIn("정보 없음", final["answer"])
        self.assertNotIn("확인된 위험 정보 없음", final["answer"])

    def test_emergency_skips_llm_and_tools(self):
        final, events, llm = R("지하차도에 물이 차오르고 있어")
        self.assertEqual(final["route"], "emergency")
        self.assertIn("119", final["answer"])
        self.assertEqual(llm.calls, [])
        self.assertEqual([e for e in events if e.get("event") == "source"], [])

    def test_out_of_scope_and_ask_region(self):
        final, _, llm = R("오늘 점심 뭐 먹지")
        self.assertEqual(final["route"], "out_of_scope")
        self.assertEqual(llm.calls, [])
        final, _, _ = R("폭염 특보 있어?")
        self.assertEqual(final["route"], "ask_region")
        self.assertIn("어느 지역", final["answer"])

    def test_history_region_makes_bare_gwangju_assumed(self):
        final, _, _ = R("광주 폭염 어때", history_region_id="gyeonggi_gwangju")
        self.assertEqual(final["location"]["status"], "assumed")
        self.assertEqual([j["region_id"] for j in final["judgements"]], ["gyeonggi_gwangju"])

    def test_missing_guide_hazard_not_substituted(self):
        final, _, _ = R("경기도 광주 열대야 어때", as_of="2026-08-25 12:00")
        j = final["judgements"][0]
        self.assertIn("열대야", j["hazards"])
        self.assertTrue(any("행동요령(열대야)" in g["source"] for g in j["gaps"]))
        self.assertFalse(any(e["kind"] == "guide" and e["hazard"] == "열대야" for e in j["evidence"]))


class Resilience(unittest.TestCase):
    def test_bad_llm_write_is_rewritten_then_passes(self):
        final, events, _ = R("경기도 광주 폭염", llm=FakeLLM(bad_write_times=1))
        self.assertTrue(final["validation"]["ok"])
        self.assertEqual(final["answer_source"], "fake")
        vals = [e for e in events if e.get("event") == "validation"]
        self.assertEqual([v["ok"] for v in vals], [False, True])

    def test_persistently_bad_llm_falls_back_to_template(self):
        final, events, _ = R("경기도 광주 폭염", llm=FakeLLM(bad_write_times=99))
        self.assertEqual(final["answer_source"], "template_fallback")
        self.assertTrue(final["validation"]["ok"])
        self.assertNotIn("안전합니다", final["answer"])

    def test_llm_down_everywhere_still_answers_from_rules_and_template(self):
        llm = FakeLLM(fail_tasks={"intent", "plan", "write"})
        final, _, _ = R("경기도 광주 폭염 나가도 돼?", llm=llm)
        self.assertEqual(final["answer_source"], "template_llm_error")
        self.assertEqual(final["hazard_source"] if "hazard_source" in final else "rules", "rules")
        self.assertEqual(final["judgements"][0]["level"], "warning")
        self.assertGreaterEqual(len(final["meta"]["llm_errors"]), 3)
        self.assertTrue(final["validation"]["ok"])

    def test_garbage_json_from_llm_is_ignored(self):
        llm = FakeLLM(garbage_tasks={"intent", "plan"})
        final, _, _ = R("경기도 광주 폭염", llm=llm)
        self.assertIn("폭염", final["hazards"])
        self.assertIn("alerts", final["plan"]["steps"])  # 특보 조회는 제약으로 항상 포함

    def test_tool_failure_is_isolated_as_unknown(self):
        class Broken(MCPGateway):
            async def tool(self, name, args):
                if name == "get_alert_status":
                    raise ValueError("boom")
                return await super().tool(name, args)

        async def go():
            gw = Broken(mcp_module.mcp)
            await gw.start()
            try:
                g = build_graph(Deps(mcp=gw, llm=FakeLLM(), settings=Settings(max_rewrites=1)))
                s = await g.ainvoke({"query": "경기도 광주 폭염", "mode": "replay", "as_of": "2026-08-24 12:00"})
                return s["final"]
            finally:
                await gw.stop()
        final = asyncio.run(go())
        self.assertEqual(final["judgements"][0]["level"], "unknown")
        self.assertIn("정보 없음", final["answer"])
        self.assertTrue(final["validation"]["ok"])


class Shelters(unittest.TestCase):
    """쉼터·대피장소: 폭염·한파·지진에만, 근거 [S#]·운영 여부 미확인 안내와 함께, 실패해도 나머지 답변은 정상."""

    def test_heat_warning_attaches_official_list_shelters(self):
        final, events, _ = R("경기도 광주 폭염 어때?")
        j = final["judgements"][0]
        sh = [e for e in j["evidence"] if e["kind"] == "shelter"]
        self.assertEqual(len(sh), 3)
        self.assertTrue(all(e["id"].startswith("S") and "경기도 광주시" in e["text"] for e in sh))
        self.assertTrue(all(e["map_url"].startswith("https://map.kakao.com/link/search/") for e in sh))
        self.assertIn(msg("markers.shelter_notice"), final["answer"])
        self.assertIn("[S1]", final["answer"])
        self.assertNotIn("kakao", final["answer"].lower())  # 링크는 본문이 아니라 카드 칩으로만
        self.assertTrue(final["validation"]["ok"])
        self.assertTrue(any(e.get("event") == "source" and e["name"] == "쉼터 목록" for e in events))

    def test_cold_and_quake_attach_shelters(self):
        for q, as_of, word in (("광주광역시 한파 대비 행동요령 알려줘", "2026-10-19 12:00", "한파쉼터"),
                               ("광주광역시 지진 나면 어떻게 해?", "2026-10-19 12:00", "지진 옥외대피장소")):
            final, _, _ = R(q, as_of=as_of)
            j = final["judgements"][0]
            sh = [e for e in j["evidence"] if e["kind"] == "shelter"]
            self.assertTrue(sh, q)
            self.assertIn(word, sh[0]["group_label"])
            self.assertTrue(final["validation"]["ok"], final["validation"])

    def test_rain_and_typhoon_never_get_shelters(self):
        for q, as_of in (("광주광역시 호우 어때?", "2026-08-27 16:00"), ("광주광역시 태풍 대비 행동요령 알려줘", "2026-10-19 12:00")):
            final, events, _ = R(q, as_of=as_of)
            self.assertFalse([e for j in final["judgements"] for e in j["evidence"] if e["kind"] == "shelter"], q)
            self.assertNotIn(msg("markers.shelter_notice"), final["answer"])
            self.assertFalse([e for e in events if e.get("event") == "source" and e["name"] == "쉼터 목록"])

    def test_shelter_failure_keeps_rest_of_answer(self):
        class ShelterDown(MCPGateway):
            async def tool(self, name, args):
                if name == "get_shelters":
                    from fastmcp.exceptions import ToolError
                    raise ToolError("boom")
                return await super().tool(name, args)
        final, events, _ = asyncio.run(run_with(ShelterDown, "경기도 광주 폭염 어때?"))
        j = final["judgements"][0]
        self.assertEqual(j["level"], "warning")  # 특보 판정은 그대로
        self.assertFalse([e for e in j["evidence"] if e["kind"] == "shelter"])
        gap = [g for g in j["gaps"] if g["source"] == "쉼터 목록(폭염)"]
        self.assertEqual((gap[0]["status"], gap[0]["cause"]), ("failed", "mcp_tool_error"))
        self.assertIn("정보 없음: 쉼터 목록(폭염)", final["answer"])
        self.assertTrue(final["validation"]["ok"])
        self.assertIn("mcp_tool_error", {f["cause"] for f in final["meta"]["failures"]})


async def run_with(gateway_cls, query, as_of="2026-08-24 12:00"):
    gw = gateway_cls(mcp_module.mcp)
    await gw.start()
    try:
        graph = build_graph(Deps(mcp=gw, llm=FakeLLM(), settings=Settings(data_mode="replay", max_rewrites=1)))
        events: list = []
        state = await graph.ainvoke({"query": query, "mode": "replay", "as_of": as_of},
                                    config={"configurable": {"emit": events.append}})
        return state["final"], events, None
    finally:
        await gw.stop()


if __name__ == "__main__":
    unittest.main()
