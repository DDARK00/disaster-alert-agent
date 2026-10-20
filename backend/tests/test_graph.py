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


if __name__ == "__main__":
    unittest.main()
