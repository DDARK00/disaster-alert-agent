"""MCP 서버 통합 테스트 — 프로세스 내 클라이언트로 Tool/Resource/Prompt 를 호출 (replay 모드)."""
import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
from fastmcp import Client  # noqa: E402

import server  # noqa: E402

G, M = "gyeonggi_gwangju", "gwangju_metro"


def run(coro):
    return asyncio.run(coro)


async def call(name, args):
    async with Client(server.mcp) as c:
        r = await c.call_tool(name, args)
        return r.data if r.data is not None else json.loads(r.content[0].text)


class Tools(unittest.TestCase):
    def test_capabilities_listed(self):
        async def go():
            async with Client(server.mcp) as c:
                tools = {t.name for t in await c.list_tools()}
                prompts = {p.name for p in await c.list_prompts()}
                templates = {t.name for t in await c.list_resource_templates()}
            return tools, prompts, templates
        tools, prompts, templates = run(go())
        self.assertEqual(tools, {"resolve_location", "get_alert_status", "get_disaster_messages",
                                 "search_action_guide", "list_hazards"})
        self.assertIn("disaster_briefing_prompt", prompts)
        self.assertEqual(len(templates), 1)

    def test_heat_alert_gyeonggi_gwangju_replay(self):
        r = run(call("get_alert_status", {"region_id": G, "as_of": "2026-08-24 12:00"}))
        self.assertEqual(r["source"]["status"], "ok")
        levels = {a["hazard"]: a["level"] for a in r["alerts"]}
        self.assertEqual(levels["폭염"], "경보")
        self.assertEqual(r["mode"], "replay")

    def test_no_alert_statement_is_not_safe_claim(self):
        r = run(call("get_alert_status", {"region_id": M, "as_of": "2026-10-18 17:00"}))
        self.assertEqual(r["source"]["status"], "ok")
        self.assertEqual(r["alerts"], [])
        self.assertIn("확인된 위험 정보 없음", r["statement"])
        self.assertNotIn("안전합니다", r["statement"])
        self.assertIn("안전을 보장하지 않습니다", r["statement"])

    def test_out_of_coverage_is_not_reported_as_no_risk(self):
        r = run(call("get_alert_status", {"region_id": G, "as_of": "2026-06-01 12:00"}))
        self.assertEqual(r["source"]["status"], "no_coverage")
        self.assertEqual(r["alerts"], [])
        self.assertIn("정보 없음", r["statement"])
        self.assertIn("안전하다는 뜻이 아닙니다", r["statement"])
        self.assertNotIn("확인된 위험 정보 없음", r["statement"])

    def test_messages_scope_for_gyeonggi_gwangju(self):
        r = run(call("get_disaster_messages", {"region_id": G, "as_of": "2026-10-18 17:00", "lookback_hours": 12}))
        self.assertEqual(r["source"]["status"], "ok")
        texts = " ".join(m["text"] for m in r["messages"])
        self.assertIn("강우강풍", texts)  # 15:30 광주시 문자
        self.assertTrue(all(m["scope"] in ("direct", "sido_wide") for m in r["messages"]))

    def test_messages_out_of_coverage(self):
        r = run(call("get_disaster_messages", {"region_id": G, "as_of": "2026-08-24 12:00"}))
        self.assertEqual(r["source"]["status"], "no_coverage")

    def test_action_guide_available_and_missing(self):
        r = run(call("search_action_guide", {"hazard": "강풍", "phase": "during"}))
        self.assertTrue(r["available"])
        self.assertGreater(r["count"], 0)
        self.assertTrue(all(x["phase"] == "during" for x in r["rows"]))
        self.assertTrue(all("-" in x["id"] for x in r["rows"]))
        r = run(call("search_action_guide", {"hazard": "건조"}))
        self.assertFalse(r["available"])
        self.assertEqual(r["rows"], [])  # 다른 재난으로 대체하지 않는다

    def test_replay_ignores_live_captures(self):
        import sources
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for n in ("getPwnCd_L1_60d__20261020_012146.json", "getPwnCd_L1_live__20261020_020345.json"):
                Path(d, n).write_text("{}")
            got = [Path(f).name for f in sources._replay_files(str(Path(d) / "getPwnCd_*.json"))]
        self.assertEqual(got, ["getPwnCd_L1_60d__20261020_012146.json"])

    def test_resolve_location_ambiguous(self):
        r = run(call("resolve_location", {"text": "광주 도로상황"}))
        self.assertEqual(r["status"], "ambiguous")

    def test_resource_and_prompt(self):
        async def go():
            async with Client(server.mcp) as c:
                res = await c.read_resource("guide://action/강풍")
                pr = await c.get_prompt("disaster_briefing_prompt",
                                        {"region": "경기도 광주시", "hazards": "강풍", "question": "나가도 돼?"})
            return res, pr
        res, pr = run(go())
        self.assertIn("출처: 행정안전부", res[0].text)
        self.assertIn("안전합니다", pr.messages[0].content.text)  # 금지 규칙으로 언급


if __name__ == "__main__":
    unittest.main()
