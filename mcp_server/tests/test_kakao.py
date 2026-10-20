"""Kakao Local — mock 응답만 사용 (실호출·저장 없음, ADR-011). 실호출은 test_kakao_smoke.py (선택)."""
import asyncio
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
from fastmcp import Client  # noqa: E402

import kakao  # noqa: E402
import server  # noqa: E402
from regions import region_for_admin  # noqa: E402


def opener_for(keyword: dict, region: dict, seen: list | None = None):
    """요청 경로별로 합성 응답을 돌려주는 가짜 urlopen."""
    def _open(req, timeout=None):
        if seen is not None:
            seen.append(req)
        body = keyword if "search/keyword" in req.full_url else region
        return io.BytesIO(json.dumps(body).encode())
    return _open


KW = {"meta": {"total_count": 3}, "documents": [{"id": "12345", "place_name": "합성 장소", "x": "126.8", "y": "35.1"}]}
REG = {"documents": [{"region_type": "H", "code": "x", "region_1depth_name": "전남광주통합특별시", "region_2depth_name": "광산구"},
                     {"region_type": "B", "code": "1220000000", "region_1depth_name": "전남광주통합특별시", "region_2depth_name": "광산구"}]}


class Lookup(unittest.TestCase):
    def test_ok_returns_only_ids_codes_and_counts(self):
        seen: list = []
        r = kakao.lookup("첨단지구", opener=opener_for(KW, REG, seen), key="SECRET-KEY-123")
        self.assertEqual(r, {"status": "ok", "place_id": "12345", "result_count": 3, "b_code": "1220000000",
                             "region_names": ["전남광주통합특별시", "광산구"]})
        self.assertEqual(seen[0].get_header("Authorization"), "KakaoAK SECRET-KEY-123")
        self.assertTrue(all("SECRET-KEY-123" not in q.full_url for q in seen))  # 키는 헤더로만

    def test_no_key_not_found_and_failures(self):
        self.assertEqual(kakao.lookup("x", key="")["status"], "unavailable")
        self.assertEqual(kakao.lookup("x", opener=opener_for({"meta": {}, "documents": []}, REG), key="k")["status"], "not_found")

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
        self.assertEqual(kakao.lookup("x", opener=boom, key="k"), {"status": "failed", "detail": "HTTP 401"})
        bad_id = {"meta": {}, "documents": [{"id": "1/../evil", "x": "1", "y": "2"}]}
        self.assertEqual(kakao.lookup("x", opener=opener_for(bad_id, REG), key="k")["status"], "failed")

    def test_links_use_official_place_id_format(self):
        self.assertEqual(kakao.links("12345"), {"map": "https://map.kakao.com/link/map/12345",
                                                "route": "https://map.kakao.com/link/to/12345"})

    def test_no_file_written(self):
        before = sorted(p.name for p in (ROOT / "data" / "fixtures").rglob("*"))
        kakao.lookup("첨단지구", opener=opener_for(KW, REG), key="k")
        self.assertEqual(before, sorted(p.name for p in (ROOT / "data" / "fixtures").rglob("*")))


class RegionMapping(unittest.TestCase):
    def test_admin_names_to_internal_region(self):
        self.assertEqual(region_for_admin(["전남광주통합특별시", "서구"]), "gwangju_metro")
        self.assertEqual(region_for_admin(["경기도", "광주시"]), "gyeonggi_gwangju")
        self.assertIsNone(region_for_admin(["전남광주통합특별시", "순천시"]))  # 전남권은 지원 지역 아님
        self.assertIsNone(region_for_admin(["서울특별시", "강남구"]))
        self.assertIsNone(region_for_admin([]))


class Tool(unittest.TestCase):
    def call(self, fake):
        async def go():
            async with Client(server.mcp) as c:
                return (await c.call_tool("resolve_place", {"query": "첨단지구"})).data
        with mock.patch.object(kakao, "lookup", return_value=fake):
            return asyncio.run(go())

    def test_ok_maps_region_and_builds_links_with_notice(self):
        r = self.call({"status": "ok", "place_id": "12345", "result_count": 3, "b_code": "1220000000",
                       "region_names": ["전남광주통합특별시", "광산구"]})
        self.assertEqual((r["status"], r["region_id"], r["place_id"]), ("ok", "gwangju_metro", "12345"))
        self.assertEqual(r["links"]["route"], "https://map.kakao.com/link/to/12345")
        self.assertEqual(r["notice"], "경로상의 도로 통제·침수 여부는 반영되지 않았습니다.")
        self.assertNotIn("place_name", r)

    def test_failure_has_no_links(self):
        r = self.call({"status": "failed", "detail": "HTTP 500"})
        self.assertEqual((r["status"], r["links"], r["region_id"]), ("failed", None, None))


if __name__ == "__main__":
    unittest.main()
