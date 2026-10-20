"""조회 실패 원인 분류 (ADR-014): capture 레코드 → 원인 코드, 소스 상태·statement 에 원인별 문구."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
sys.path.insert(0, str(ROOT / "eval"))
from fastmcp import Client  # noqa: E402

import capture  # noqa: E402
import failures  # noqa: E402
import server  # noqa: E402
from config.catalog import msg  # noqa: E402
from faults import API_FAULTS, EXPECTED_CAUSE, api_fault  # noqa: E402


def rec(http=200, code="00", js=True, error=None, kind=None, text=None, msg_=None):
    j = {"response": {"header": {"resultCode": code, "resultMsg": msg_}}} if js else None
    return {"meta": {"http_status": http, "api_result_code": code if js else None, "api_result_msg": msg_,
                     "error": error, "error_kind": kind}, "json": j, "text": text}


class Classify(unittest.TestCase):
    def test_codes(self):
        cases = [
            (rec(), None),
            (rec(code="03"), "api_result_error"),
            (rec(http=None, js=False, error="URLError: timed out", kind="timeout"), "api_timeout"),
            (rec(http=None, js=False, error="URLError: refused", kind="network"), "network_error"),
            (rec(http=None, js=False, error="서비스키 미설정", kind="key_missing"), "api_auth_error"),
            (rec(http=403, js=False, text="SERVICE_KEY_IS_NOT_REGISTERED_ERROR"), "api_auth_error"),
            (rec(code="30", msg_="SERVICE KEY IS NOT REGISTERED ERROR"), "api_auth_error"),
            (rec(code="22"), "rate_limited"),
            (rec(http=500, js=False, text="<html>"), "api_http_error"),
            (rec(js=False, text='{"response": {"hea'), "response_invalid"),
            (rec(code="99"), "api_result_error"),
        ]
        for r, want in cases:
            self.assertEqual(failures.classify(r), want, r)
        self.assertIsNone(failures.classify(rec(code="03"), ok_codes=("0", "00", "03")))

    def test_every_cause_has_message(self):
        texts = [msg(f"causes.{c}") for c in failures.CAUSES + ("mcp_unreachable", "mcp_tool_error")]
        self.assertTrue(all(texts))
        self.assertEqual(len(set(texts)), len(texts))  # 원인마다 다른 문구

    def test_key_missing_does_not_call_network(self):
        with mock.patch.object(capture, "load_env", lambda *a, **k: {}), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("호출하면 안 됨")):
            r = capture.call("weather", "getPwnCd", {}, "t")
        self.assertEqual(r["meta"]["error_kind"], "key_missing")
        self.assertEqual(failures.classify(r), "api_auth_error")


async def tool(name, args):
    async with Client(server.mcp) as c:
        r = await c.call_tool(name, args)
        return r.data if r.data is not None else json.loads(r.content[0].text)


class InjectedFaults(unittest.TestCase):
    """장애 5종 → 서로 다른 원인 코드와 문구 (기상특보·재난문자 모두)."""

    def test_each_fault_distinct_cause_and_statement(self):
        seen = {}
        for kind in API_FAULTS:
            with api_fault(kind):
                a = asyncio.run(tool("get_alert_status", {"region_id": "gyeonggi_gwangju", "mode": "live"}))
                m = asyncio.run(tool("get_disaster_messages", {"region_id": "gyeonggi_gwangju", "mode": "live"}))
                health = asyncio.run(tool("source_health", {}))["sources"]
            want = EXPECTED_CAUSE[kind]
            for r in (a, m):
                self.assertEqual((r["source"]["status"], r["source"]["cause"]), ("failed", want), kind)
                self.assertIn("정보 없음", r["statement"])
                self.assertIn(msg(f"causes.{want}"), r["statement"])
                self.assertNotIn("FAKEKEY", json.dumps(r, ensure_ascii=False))
            self.assertEqual(health["weather"]["last_cause"], want)
            self.assertNotIn("FAKEKEY", json.dumps(health))
            seen[kind] = a["statement"]
        self.assertEqual(len(set(seen.values())), len(API_FAULTS))


if __name__ == "__main__":
    unittest.main()
