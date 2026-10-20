"""장애 주입 시나리오 (ADR-014): 원인마다 다른 원인 코드·문구가 판정·답변·SSE·meta 에 실린다."""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "mcp_server"))
sys.path.insert(0, str(ROOT / "eval"))

import server as mcp_module  # noqa: E402
from agent.deps import Deps, Settings  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.llm import FakeLLM  # noqa: E402
from agent.mcp_client import MCPGateway  # noqa: E402
from agent.validator import validate  # noqa: E402
from config.catalog import msg  # noqa: E402
from faults import API_FAULTS, EXPECTED_CAUSE, DownGateway, api_fault  # noqa: E402

Q = "경기도 광주 강풍 상황 알려줘"


async def run(gw, query=Q, mode="live"):
    await gw.start()
    try:
        graph = build_graph(Deps(mcp=gw, llm=FakeLLM(), settings=Settings(data_mode="replay", max_rewrites=1)))
        events: list = []
        state = await graph.ainvoke({"query": query, "mode": mode, "as_of": ""},
                                    config={"configurable": {"emit": events.append}})
        return state["final"], events
    finally:
        await gw.stop()


class Faults(unittest.TestCase):
    def test_api_faults_give_distinct_cause_and_answer(self):
        answers = {}
        for kind in API_FAULTS:
            with api_fault(kind):
                final, events = asyncio.run(run(MCPGateway(mcp_module.mcp)))
            want = EXPECTED_CAUSE[kind]
            j = final["judgements"][0]
            self.assertEqual(j["level"], "unknown", kind)
            self.assertEqual({g["cause"] for g in j["gaps"] if g["status"] == "failed"}, {want}, kind)
            self.assertIn(msg(f"causes.{want}"), final["answer"], kind)
            self.assertIn("정보 없음", final["answer"])
            self.assertTrue(validate(final["answer"], final["judgements"])["ok"], kind)
            self.assertIn(want, {f["cause"] for f in final["meta"]["failures"]})
            src = [e for e in events if e.get("event") == "source" and e.get("status") == "failed"]
            self.assertTrue(src and all(e["cause"] == want for e in src), kind)
            answers[kind] = final["answer"]
        self.assertEqual(len(set(answers.values())), len(API_FAULTS))

    def test_mcp_down_is_fixed_no_info_with_cause(self):
        final, events = asyncio.run(run(DownGateway()))
        self.assertEqual(final["route"], "unavailable")
        self.assertEqual(final["answer_source"], "fixed")
        self.assertIn("정보 없음", final["answer"])
        self.assertIn(msg("causes.mcp_unreachable"), final["answer"])
        self.assertIn("119", final["answer"])
        self.assertEqual(final["meta"]["failures"][0]["cause"], "mcp_unreachable")
        self.assertTrue(any(e.get("cause") == "mcp_unreachable" for e in events if e.get("event") == "source"))

    def test_mcp_tool_error_on_research_is_isolated(self):
        class ToolFails(MCPGateway):
            async def tool(self, name, args):
                if name in ("get_alert_status", "get_disaster_messages"):
                    from fastmcp.exceptions import ToolError
                    raise ToolError("boom")
                return await super().tool(name, args)
        final, _ = asyncio.run(run(ToolFails(mcp_module.mcp), mode="replay"))
        j = final["judgements"][0]
        self.assertEqual(j["level"], "unknown")
        self.assertEqual({g["cause"] for g in j["gaps"] if g["status"] == "failed"}, {"mcp_tool_error"})
        self.assertIn(msg("causes.mcp_tool_error"), final["answer"])
        self.assertTrue(final["validation"]["ok"])

    def test_llm_unavailable_recorded(self):
        from agent.llm import LLMError

        class Down(FakeLLM):
            async def complete(self, *a, **k):
                raise LLMError("LLM 연결 실패: ConnectError")

        async def go():
            gw = MCPGateway(mcp_module.mcp)
            await gw.start()
            try:
                g = build_graph(Deps(mcp=gw, llm=Down(), settings=Settings()))
                return (await g.ainvoke({"query": Q, "mode": "replay", "as_of": "2026-10-18 16:30"},
                                        config={"configurable": {"emit": lambda e: None}}))["final"]
            finally:
                await gw.stop()
        final = asyncio.run(go())
        self.assertEqual(final["answer_source"], "template_llm_error")
        self.assertTrue(final["validation"]["ok"])
        self.assertIn("llm_unavailable", {f["cause"] for f in final["meta"]["failures"]})


if __name__ == "__main__":
    unittest.main()
