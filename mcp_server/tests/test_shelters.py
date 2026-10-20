"""쉼터 어댑터 (통합대피소): 구분 이름 필터, 이웃 시군 주소 제거(신구 명칭), 상한·결정성, 캐시·폴백·원인 코드."""
import io
import json
import random
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
sys.path.insert(0, str(ROOT))
import capture  # noqa: E402
import server  # noqa: E402
import shelters  # noqa: E402

GJ = "gwangju_metro"
GG = "gyeonggi_gwangju"
GJ_PREFIXES = shelters.area(GJ)["addr_prefixes"]


def row(name, addr, nm="한파쉼터,무더위쉼터", cd="1,2", sn=None):
    return {"REARE_NM": name, "RONA_DADDR": addr, "LAT": 35.1, "LOT": 126.9, "SHLT_SE_CD": cd, "SHLT_SE_NM": nm,
            "MNG_SN": sn or f"T{abs(hash((name, addr))) % 10**9}"}


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(rows_by_code: dict, calls: list | None = None, bad: bool = False):
    def _open(req, timeout=0):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(req.full_url).query))
        if calls is not None:
            calls.append((q["shlt_se_cd"], int(q["pageNo"])))
        rows = rows_by_code.get(q["shlt_se_cd"], [])
        n, p = int(q["numOfRows"]), int(q["pageNo"])
        body = [{k: v for k, v in r.items() if k != "MNG_SN"} for r in rows] if bad else rows[(p - 1) * n:p * n]
        return Resp(json.dumps({"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"}, "numOfRows": n,
                                "pageNo": p, "totalCount": len(rows), "body": body}, ensure_ascii=False).encode())
    return _open


class Replay(unittest.TestCase):
    """커밋된 fixture (2026-10-20 실제 응답) 기준."""

    def test_neighbor_cities_removed_by_address(self):
        r = shelters.load(GG, "heat", "replay")
        self.assertEqual(r["status"], "ok")
        self.assertGreater(r["stats"]["outside_area"], 0)  # 사각형에 하남·성남·이천 등이 섞여 온다
        raw, _ = shelters.fixture_rows(GG, "heat")
        self.assertTrue(any("하남시" in x["RONA_DADDR"] for x in raw))
        rows, _ = shelters.select("heat", raw, shelters.area(GG)["addr_prefixes"])
        self.assertTrue(rows and all(x["address"].startswith("경기도 광주시 ") for x in rows))

    def test_gwangju_new_and_old_names_both_accepted_neighbors_rejected(self):
        self.assertTrue(shelters.in_area("전남광주통합특별시 북구 하서로229번길 57", GJ_PREFIXES))
        self.assertTrue(shelters.in_area("광주광역시  북구 하서로 57", GJ_PREFIXES))  # 옛 명칭 + 이중 공백
        self.assertFalse(shelters.in_area("전남광주통합특별시 장성군 장성읍 1", GJ_PREFIXES))
        self.assertFalse(shelters.in_area("경기도 광주시 경안동 1", GJ_PREFIXES))
        raw, _ = shelters.fixture_rows(GJ, "heat")
        self.assertTrue(any("장성군" in x["RONA_DADDR"] or "담양군" in x["RONA_DADDR"] or "나주시" in x["RONA_DADDR"]
                            for x in raw))
        rows, _ = shelters.select("heat", raw, GJ_PREFIXES)
        self.assertFalse(any(w in x["address"] for x in rows for w in ("장성군", "담양군", "나주시", "화순군")))

    def test_kind_filter_by_name_not_code(self):
        # 코드 1 요청에도 "한파쉼터,무더위쉼터"가 섞인다 → 무더위쉼터는 이름으로
        mixed = [row("가", "광주광역시 동구 1", nm="한파쉼터", cd="1"), row("나", "광주광역시 동구 2")]
        self.assertEqual([x["name"] for x in shelters.select("heat", mixed, GJ_PREFIXES)[0]], ["나"])
        self.assertEqual([x["name"] for x in shelters.select("cold", mixed, GJ_PREFIXES)[0]], ["가", "나"])
        quake = [row("다", "광주광역시 동구 3", nm="지진옥외대피장소", cd="3"), row("라", "광주광역시 동구 4", nm="x", cd="4"),
                 row("마", "광주광역시 동구 5")]
        self.assertEqual([x["name"] for x in shelters.select("quake", quake, GJ_PREFIXES)[0]], ["다", "라"])

    def test_limit_and_deterministic_order(self):
        raw, _ = shelters.fixture_rows(GJ, "cold")
        a = shelters.select("cold", raw, GJ_PREFIXES)[0]
        shuffled = raw[:]
        random.Random(7).shuffle(shuffled)
        self.assertEqual(a, shelters.select("cold", shuffled, GJ_PREFIXES)[0])  # 응답 순서와 무관
        r1, r2 = shelters.load(GJ, "cold", "replay"), shelters.load(GJ, "cold", "replay")
        self.assertEqual(r1["shelters"], r2["shelters"])
        self.assertEqual(len(r1["shelters"]), shelters.CFG["limit"])
        self.assertEqual(r1["shelters"], a[:shelters.CFG["limit"]])
        self.assertNotIn("LAT", json.dumps(r1))  # 좌표는 내보내지 않는다

    def test_all_demo_regions_have_all_kinds(self):
        for rid in (GJ, GG):
            for kind in shelters.KINDS:
                r = shelters.load(rid, kind, "replay")
                self.assertEqual(r["status"], "ok", (rid, kind))
                self.assertTrue(r["shelters"], (rid, kind))

    def test_region_without_range_is_no_range_not_failure(self):
        with mock.patch.object(shelters, "area", lambda rid: None):
            r = shelters.load(GJ, "heat", "replay")
        self.assertEqual(r["status"], "no_range")
        self.assertNotIn("cause", r)

    def test_tool_only_for_heat_cold_quake(self):
        self.assertEqual(server.get_shelters(GJ, "호우", mode="replay")["status"], "not_applicable")
        self.assertEqual(server.get_shelters(GJ, "태풍", mode="replay")["status"], "not_applicable")
        for hz in ("폭염", "한파", "지진"):
            self.assertEqual(server.get_shelters(GJ, hz, mode="replay")["status"], "ok", hz)
        hz = {h["name"]: h.get("shelter") for h in server.list_hazards()["hazards"]}
        self.assertEqual({k for k, v in hz.items() if v}, {"폭염", "한파", "지진"})


class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.ps = [mock.patch.object(capture, "FIXTURES", t), mock.patch.object(capture, "COUNTER", t / "_c.json"),
                   mock.patch.object(capture, "ROOT", t),
                   mock.patch.object(capture, "load_env", lambda *a, **k: {"DSSP_SHELTER_SERVICE_KEY": "K9"}),
                   mock.patch.dict(shelters._cache, clear=True)]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in reversed(self.ps):
            p.stop()
        self.tmp.cleanup()

    def test_live_pages_until_limit_after_address_filter_then_cache(self):
        size = shelters.registry.get("shelter").page_size
        neighbors = [row(f"하남{i}", f"경기도 하남시 {i}") for i in range(size)]  # 1쪽은 전부 이웃 시
        mine = [row(f"광주{i}", f"경기도 광주시 경안동 {i}") for i in range(5)]
        calls = []
        with mock.patch.object(urllib.request, "urlopen", serve({"2": neighbors + mine}, calls)):
            r = shelters.load(GG, "heat", "live")
            again = shelters.load(GG, "heat", "live")
        self.assertEqual(calls, [("2", 1), ("2", 2)])  # 2쪽에서 상한이 차서 멈춤, 두 번째는 캐시
        self.assertEqual((r["origin"], again["origin"]), ("live", "live_cache"))
        self.assertEqual([x["name"] for x in r["shelters"]], ["광주0", "광주1", "광주2"])

    def test_failure_falls_back_to_committed_fixture_with_cause(self):
        def down(req, timeout=0):
            raise urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch.object(urllib.request, "urlopen", down):
            r = shelters.load(GJ, "heat", "live")
        self.assertEqual((r["status"], r["origin"], r["fallback_cause"]), ("ok", "fixture_fallback", "api_timeout"))
        self.assertTrue(r["shelters"])

    def test_failure_without_fixture_is_failed_with_cause(self):
        with mock.patch.object(urllib.request, "urlopen", serve({"2": [row("a", "경기도 광주시 1")]}, bad=True)), \
                mock.patch.object(shelters, "fixture_rows", lambda *a: None):
            r = shelters.load(GG, "heat", "live")
        self.assertEqual((r["status"], r["cause"]), ("failed", "response_invalid"))  # 필드 누락 → pydantic

    def test_result_code_error_is_classified(self):
        def err(req, timeout=0):
            return Resp(json.dumps({"header": {"resultCode": "30", "resultMsg": "SERVICE KEY IS NOT REGISTERED ERROR"},
                                    "body": None}).encode())
        with mock.patch.object(urllib.request, "urlopen", err), mock.patch.object(shelters, "fixture_rows", lambda *a: None):
            r = shelters.load(GJ, "quake", "live")
        self.assertEqual((r["status"], r["cause"]), ("failed", "api_auth_error"))


if __name__ == "__main__":
    unittest.main()
