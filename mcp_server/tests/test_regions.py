import glob
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
from regions import message_applies, resolve_location  # noqa: E402

M, G = "gwangju_metro", "gyeonggi_gwangju"


class Resolve(unittest.TestCase):
    def test_bare_gwangju_is_ambiguous(self):
        r = resolve_location("광주 날씨 위험해?")
        self.assertEqual(r["status"], "ambiguous")
        self.assertEqual({c["id"] for c in r["candidates"]}, {M, G})

    def test_explicit_qualifier_resolves(self):
        self.assertEqual(resolve_location("경기도 광주 호우")["chosen"], G)
        self.assertEqual(resolve_location("경기도 광주 호우")["status"], "resolved")
        self.assertEqual(resolve_location("광주광역시 강풍")["chosen"], M)
        self.assertEqual(resolve_location("광산구 비 많이 와")["chosen"], M)

    def test_history_makes_assumption_not_certainty(self):
        r = resolve_location("광주 지금 어때", history_region_id=M)
        self.assertEqual((r["status"], r["chosen"]), ("assumed", M))

    def test_explicit_beats_history(self):
        r = resolve_location("경기 광주는?", history_region_id=M)
        self.assertEqual(r["chosen"], G)

    def test_unknown_place(self):
        self.assertEqual(resolve_location("부산 날씨")["status"], "none")


class MessageScope(unittest.TestCase):
    def test_direct_and_sido_wide(self):
        self.assertEqual(message_applies(G, "경기도 광주시 "), "direct")
        self.assertEqual(message_applies(G, "경기도 광주시 도척면 "), "direct")
        self.assertEqual(message_applies(G, "경기도  "), "sido_wide")
        self.assertEqual(message_applies(G, "경기도 전체 ,강원특별자치도 전체 "), "sido_wide")
        self.assertIsNone(message_applies(G, "경기도 광명시 "))  # '광주시'로 시작하지 않음
        self.assertEqual(message_applies(M, "전남광주통합특별시 동구 "), "direct")
        self.assertEqual(message_applies(M, "전남광주통합특별시 "), "sido_wide")
        self.assertIsNone(message_applies(M, "전남광주통합특별시 신안군 "))  # 전남권은 광주 아님
        self.assertIsNone(message_applies(M, "경기도 광주시 "))

    def test_real_october_fixture(self):
        f = sorted(glob.glob(str(ROOT / "data/fixtures/dssp/oct2026_all__*.json")))[-1]
        with open(f, encoding="utf-8") as fp:
            body = json.load(fp)["json"]["body"]
        g = [m for m in body if message_applies(G, m["RCPTN_RGN_NM"]) == "direct"]
        self.assertTrue(any("강우강풍" in m["MSG_CN"] for m in g), "10/18 15:30 광주시 문자를 찾아야 함")
        m = [m for m in body if message_applies(M, m["RCPTN_RGN_NM"]) == "direct"]
        self.assertTrue(all("신안" not in x["RCPTN_RGN_NM"] for x in m))


if __name__ == "__main__":
    unittest.main()
