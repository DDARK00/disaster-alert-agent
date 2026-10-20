"""행동요령 어댑터 (ADR-013/015): 4개 소스 같은 경로, 쪽 넘기기, 정규화, 대상 필터, 일 1회 캐시·폴백, 레지스트리·환경변수."""
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
sys.path.insert(0, str(ROOT))
import capture  # noqa: E402
import guides  # noqa: E402
import registry  # noqa: E402
import sources  # noqa: E402
from schemas import Envelope, GuideRow, MessageRow, WeatherEnvelope  # noqa: E402

HM = sources._hazard_map()


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve_rows(rows: list[dict], total: int | None = None, calls: list | None = None):
    """safetydata 처럼 pageNo·numOfRows 로 잘라 주는 가짜 서버."""
    def _open(req, timeout=0):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(req.full_url).query))
        n, p = int(q["numOfRows"]), int(q["pageNo"])
        if calls is not None:
            calls.append(p)
        body = {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"}, "numOfRows": n, "pageNo": p,
                "totalCount": len(rows) if total is None else total, "body": rows[(p - 1) * n:p * n]}
        return Resp(json.dumps(body, ensure_ascii=False).encode())
    return _open


class Live(unittest.TestCase):
    """live 경로: 가짜 서버(urlopen) + 임시 fixture 폴더. 키는 가짜 값."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.ps = [mock.patch.object(capture, "FIXTURES", t), mock.patch.object(capture, "COUNTER", t / "_c.json"),
                   mock.patch.object(capture, "ROOT", t),
                   mock.patch.object(capture, "load_env", lambda *a, **k: {
                       "DSSP_GUIDE_SOCIAL_SERVICE_KEY": "K1", "DSSP_GUIDE_NATURAL_SERVICE_KEY": "K2",
                       "DSSP_GUIDE_LIFE_SERVICE_KEY": "K3", "DSSP_GUIDE_CIVIL_DEFENSE_SERVICE_KEY": "K4"}),
                   mock.patch.dict(guides._cache, clear=True)]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()

    def test_live_and_fixture_same_rows_for_all_four_sources(self):
        # 4개 소스 모두: 커밋된 fixture 원본을 가짜 서버로 다시 내보내면 live 결과가 replay 와 같다 (같은 코드 경로)
        for hz in ("호우", "화재", "미세먼지", "민방공"):
            hm = HM[hz]
            raw, _ = guides.fixture_pages(hm["source"], hm["action_cate2"])
            with mock.patch.object(urllib.request, "urlopen", serve_rows(raw)):
                live = guides.load(hz, hm, "live")
            replay = guides.load(hz, hm, "replay")
            self.assertEqual(live["origin"], "live", hz)
            self.assertEqual([r["id"] for r in live["rows"]], [r["id"] for r in replay["rows"]], hz)
            self.assertEqual(live["source"], registry.get(hm["source"]).attribution)

    def test_pages_follow_total_count_and_full_last_page(self):
        row = {"actRmks": "x", "safety_cate1": "02", "safety_cate2": "02011", "safety_cate3": "02011004",
               "safety_cate_nm3": "화재시 행동요령"}
        rows = [{**row, "actRmks": f"행 {i}"} for i in range(214)]
        calls: list = []
        with mock.patch.object(urllib.request, "urlopen", serve_rows(rows, calls=calls)):
            got, _ = guides.fetch_pages("guide_social", "02011")
        self.assertEqual((len(got), calls), (214, [1, 2, 3]))
        # 정확히 100행(꽉 찬 마지막 쪽) → totalCount 를 믿지 않고 한 쪽 더 확인
        calls.clear()
        with mock.patch.object(urllib.request, "urlopen", serve_rows(rows[:100], calls=calls)):
            got, _ = guides.fetch_pages("guide_social", "02011")
        self.assertEqual((len(got), calls), (100, [1, 2]))
        # totalCount 가 실제보다 작게 와도 꽉 찬 쪽이면 다음 쪽을 본다
        calls.clear()
        with mock.patch.object(urllib.request, "urlopen", serve_rows(rows[:150], total=100, calls=calls)):
            got, _ = guides.fetch_pages("guide_social", "02011")
        self.assertEqual(len(got), 150)

    def test_daily_cache_and_fixture_fallback(self):
        hm = HM["화재"]
        raw, _ = guides.fixture_pages("guide_social", "02011")
        calls: list = []
        with mock.patch.object(urllib.request, "urlopen", serve_rows(raw, calls=calls)):
            a = guides.load("화재", hm, "live")
            b = guides.load("화재", hm, "live")
        self.assertEqual((a["origin"], b["origin"], len(calls)), ("live", "live_cache", 3))  # 두 번째는 호출 없음
        guides._cache.clear()
        with mock.patch.object(urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            c = guides.load("화재", hm, "live")
        self.assertEqual((c["origin"], c["fallback_cause"]), ("fixture_fallback", "api_timeout"))
        self.assertEqual([r["id"] for r in c["rows"]], [r["id"] for r in a["rows"]])

    def test_invalid_rows_are_response_invalid(self):
        bad = [{"actRmks": "x"}]  # safety_cate1/2 없음 → 스키마 위반
        with mock.patch.object(urllib.request, "urlopen", serve_rows(bad)):
            with self.assertRaises(guides.GuideFetchError) as cm:
                guides.fetch_pages("guide_social", "02011")
        self.assertEqual(cm.exception.cause, "response_invalid")

    def test_daily_limit_blocks_call(self):
        with mock.patch.object(capture, "calls_today", lambda s: 1000), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("호출하면 안 됨")):
            rec = capture.call("guide_social", "", {}, "t")
        self.assertEqual(rec["meta"]["error_kind"], "daily_limit")


class Normalize(unittest.TestCase):
    def test_title_and_empty_rows_dropped_and_text_cleaned(self):
        raw = [{"actRmks": "제목", "safety_cate1": "02", "safety_cate2": "02011", "safety_cate3": None},
               {"actRmks": None, "safety_cate1": "02", "safety_cate2": "02011", "safety_cate3": "02011004"},
               {"actRmks": "  ", "safety_cate1": "02", "safety_cate2": "02011", "safety_cate3": "02011004"},
               {"actRmks": "- 낮은 자세로 대피합시다.", "safety_cate1": "02", "safety_cate2": "02011",
                "safety_cate3": "02011004", "safety_cate_nm3": "화재시 행동요령"},
               {"actRmks": "Q : 엘리베이터 안에서 정전이 되면? A : 인터폰으로 연락합니다.", "safety_cate1": "02",
                "safety_cate2": "02018", "safety_cate3": "02018001", "safety_cate_nm3": "정전발생 시"}]
        rows, excluded = guides.normalize("guide_social", "화재", HM["화재"], raw)
        self.assertEqual([r["text"] for r in rows],
                         ["낮은 자세로 대피합시다.", "엘리베이터 안에서 정전이 되면? — 인터폰으로 연락합니다."])
        self.assertEqual(rows[0]["phase"], "during")
        self.assertEqual(excluded, 0)

    def test_rescuer_rows_excluded_from_traffic_accident(self):
        g = guides.load("교통사고", HM["교통사고"], "replay")
        sections = {r["section"] for r in g["rows"]}
        self.assertFalse(sections & {"차량에서의 구조", "교통사고시 상황판단", "구출"})
        self.assertNotIn("현장통제를 구축", " ".join(r["text"] for r in g["rows"]))
        self.assertGreaterEqual(g["excluded"], 19 + 12 + 9 - 3)  # 구조대원용 단계 3개 (내용 없는 행 제외)
        self.assertIn("교통사고 예방은", sections)

    def test_admin_basis_rows_excluded(self):  # "※관련근거 : 대기환경 보전법 시행규칙…" (행정 근거, 시민 행동 아님)
        g = guides.load("미세먼지", HM["미세먼지"], "replay")
        self.assertGreaterEqual(g["excluded"], 1)
        self.assertFalse(any("관련근거" in r["text"] for r in g["rows"]))

    def test_all_linked_hazards_have_rows_and_known_phases(self):
        for hz, hm in HM.items():
            if not hm["action_cate2"]:
                continue
            g = guides.load(hz, hm, "replay")
            self.assertTrue(g["available"] and g["rows"], hz)
            self.assertTrue({"prepare", "during"} & {r["phase"] for r in g["rows"]}, hz)  # 쓸 수 있는 단계가 있다

    def test_rain_guide_uses_all_pages(self):  # 예전 fixture 는 69행 중 3행뿐이었다 (T-031)
        raw, _ = guides.fixture_pages("doing", "01003")
        self.assertEqual(len(raw), 69)


class Registry(unittest.TestCase):
    def test_sources_and_hazard_map_consistent(self):
        self.assertEqual({s.status for n, s in registry.SOURCES.items() if n.startswith("guide_")}, {"active"})
        for hz, hm in HM.items():
            if hm["action_cate2"]:
                self.assertIn(hm["action_cate2"], registry.get(hm["source"]).categories, hz)

    def test_bad_registry_fails_at_startup(self):
        import yaml
        data = yaml.safe_load(registry.PATH.read_text(encoding="utf-8"))
        data["sources"]["dssp"]["status"] = "maybe"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True)
        try:
            with mock.patch.object(registry, "PATH", Path(f.name)), self.assertRaises(RuntimeError):
                registry._load()
        finally:
            os.unlink(f.name)


class EnvNames(unittest.TestCase):
    def test_new_name_then_legacy_with_single_warning(self):
        capture._warned_legacy.clear()
        self.assertEqual(capture.service_key("dssp", {"DSSP_MESSAGE_SERVICE_KEY": "new", "DSSP_SERVICE_KEY": "old"}),
                         ("new", "DSSP_MESSAGE_SERVICE_KEY"))
        with self.assertLogs("mcp.capture", logging.WARNING) as cm:
            for _ in range(3):
                self.assertEqual(capture.service_key("dssp", {"DSSP_SERVICE_KEY": "old"}), ("old", "DSSP_SERVICE_KEY"))
        self.assertEqual(len(cm.records), 1)  # 경고는 한 번
        self.assertNotIn("old", cm.output[0].split("—")[0].split()[-1])  # 값이 아니라 이름만
        self.assertEqual(capture.service_key("guide_social", {}), ("", None))

    def test_inline_comment_with_single_space_is_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("A_SERVICE_KEY=abc # 한 칸 주석\nB_SERVICE_KEY=def  # 두 칸\nC_SERVICE_KEY=g#h\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                env = capture.load_env(p)
        self.assertEqual((env["A_SERVICE_KEY"], env["B_SERVICE_KEY"], env["C_SERVICE_KEY"]), ("abc", "def", "g#h"))

    def test_dotenv_loader_same_rule(self):
        from config.settings import load_dotenv_file
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("ZZ_TEST_VALUE=abc # 주석\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                load_dotenv_file(p)
                self.assertEqual(os.environ["ZZ_TEST_VALUE"], "abc")


class Schemas(unittest.TestCase):
    def test_committed_fixtures_validate(self):
        import glob
        for f in glob.glob(str(ROOT / "data/fixtures/weather/getPwnCd_*.json")):
            if "_live__" not in f:
                WeatherEnvelope.model_validate(json.loads(Path(f).read_text(encoding="utf-8"))["json"])
        for f in glob.glob(str(ROOT / "data/fixtures/dssp/*.json")):
            if "_live__" not in f:
                Envelope[MessageRow].model_validate(json.loads(Path(f).read_text(encoding="utf-8"))["json"])
        for d in ("doing", "guide_social", "guide_life", "guide_civil_defense"):
            for f in glob.glob(str(ROOT / f"data/fixtures/{d}/cate_*.json")):
                Envelope[GuideRow].model_validate(json.loads(Path(f).read_text(encoding="utf-8"))["json"])


if __name__ == "__main__":
    unittest.main()
