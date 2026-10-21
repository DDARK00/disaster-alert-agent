import glob
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server"))
from regions import load_regions, message_applies, resolve_location  # noqa: E402

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

    def test_shared_name_ignores_history(self):
        # 같은 이름('광주')이면 이전 질문의 지역으로 한쪽만 고르지 않고 둘 다 조회한다 (T-041 — 이전엔 assumed)
        r = resolve_location("광주 지금 어때", history_region_id=M)
        self.assertEqual((r["status"], r["chosen"]), ("ambiguous", None))
        self.assertEqual({c["id"] for c in r["candidates"]}, {M, G})

    def test_history_used_when_no_region_named(self):
        r = resolve_location("지금 비 와?", history_region_id=M)
        self.assertEqual((r["status"], r["chosen"]), ("resolved", M))

    def test_explicit_beats_history(self):
        r = resolve_location("경기 광주는?", history_region_id=M)
        self.assertEqual(r["chosen"], G)

    def test_unknown_place(self):
        self.assertEqual(resolve_location("강남역 근처 날씨")["status"], "none")
        self.assertEqual(resolve_location("오늘 날씨")["status"], "none")


# ── 전국 확대 (ADR-017): 시도 / 시군 / 동명 / 광역시 구 / 신구 표기 ──
NATION = [  # (입력, 상태, 확정 지역 이름 또는 None)
    ("부산 폭염 어때", "resolved", "부산광역시"),
    ("서울특별시 호우", "resolved", "서울특별시"),
    ("경기도 수원시 폭염", "resolved", "경기도 수원시"),
    ("수원시 대설", "resolved", "경기도 수원시"),
    ("수원 대설", "assumed", "경기도 수원시"),            # 줄임말은 약한 별칭 → 가정
    ("충북 청주 폭염", "resolved", "충청북도 청주시"),
    ("강원 고성 대설", "resolved", "강원특별자치도 고성군"),
    ("경남 고성군 태풍", "resolved", "경상남도 고성군"),
    ("전라남도 목포시 호우", "resolved", "전라남도 목포시"),
    ("전남광주통합특별시 목포시 호우", "resolved", "전라남도 목포시"),  # 신구 시도 표기
    ("전북특별자치도 전주시 폭염", "resolved", "전북특별자치도 전주시"),
    ("전라북도 전주시 폭염", "resolved", "전북특별자치도 전주시"),
    ("강원도 강릉시 대설", "resolved", "강원특별자치도 강릉시"),
    ("제주시 태풍", "resolved", "제주특별자치도 제주시"),
    ("흑산도 태풍", "resolved", "전라남도 신안군"),
    ("울릉도 대설", "resolved", "경상북도 울릉군"),
    ("강남구 폭염", "resolved", "서울특별시"),             # 광역시 구 → 광역시 (시 전체 기준)
    ("해운대구 호우", "resolved", "부산광역시"),
    ("고성 대설", "ambiguous", None),                       # 동명 → 되묻기
    ("고성군 폭염", "ambiguous", None),
    ("중구 날씨", "ambiguous", None),                       # 여러 광역시의 같은 구 이름 → 되묻기
    ("강서구 비", "ambiguous", None),
    ("경기도 폭염", "none", None),                          # 도 이름만 → 시·군 되묻기
    ("제주 태풍", "none", None),
    ("진도 5 지진 나면", "none", None),                     # 일상어와 겹치는 줄임말은 별칭이 아님
    ("양산 챙겨야 해 폭염", "none", None),
]


class Nationwide(unittest.TestCase):
    def test_generated_regions_have_sido_shelter_range_and_own_prefixes(self):
        # 쉼터 범위: 시도 사각형 + 그 지역 주소 접두어(신구 시도 표기) — 다른 시군 시설은 주소로 걸러진다
        import shelters
        for r in load_regions().values():
            a = r.get("shelter_area")
            self.assertTrue(a, r["id"])
            self.assertEqual(set(a), {"bbox", "addr_prefixes"} | ({"bbox_scope"} if r.get("generated") else set()))
            if r.get("generated"):
                self.assertEqual(a["addr_prefixes"], r["dssp_prefixes"])
        suwon = next(r for r in load_regions().values() if r["name"] == "경기도 수원시")
        self.assertTrue(shelters.in_area("경기도 수원시 장안구 정조로 1", suwon["shelter_area"]["addr_prefixes"]))
        self.assertFalse(shelters.in_area("경기도 수원군 가상로 1", suwon["shelter_area"]["addr_prefixes"]))
        self.assertFalse(shelters.in_area("경기도 화성시 봉담읍 1", suwon["shelter_area"]["addr_prefixes"]))

    def test_resolution_table(self):
        regions = load_regions()
        for text, status, name in NATION:
            with self.subTest(text=text):
                r = resolve_location(text)
                self.assertEqual(r["status"], status, r)
                if name:
                    self.assertEqual(regions[r["chosen"]]["display"], name)

    def test_same_name_asks_but_gwangju_shows_both(self):
        r = resolve_location("고성 대설")
        self.assertTrue(r.get("ask"))
        self.assertEqual({c["name"] for c in r["candidates"]}, {"강원특별자치도 고성군", "경상남도 고성군"})
        self.assertFalse(resolve_location("광주 날씨").get("ask"))  # ADR-006 묶음은 둘 다 조회 (불변)
        self.assertEqual(resolve_location("경기도 폭염").get("province"), "경기도")

    def test_district_alias_marks_city_wide_scope(self):
        r = resolve_location("강남구 폭염")
        self.assertEqual(r["districts"], {r["chosen"]: "강남구"})
        self.assertNotIn("districts", resolve_location("서울 폭염"))

    def test_messages_old_and_new_sido_names(self):
        jn = "kr_L1052100"  # 전라남도 목포시
        self.assertEqual(message_applies(jn, "전남광주통합특별시 목포시 "), "direct")
        self.assertEqual(message_applies(jn, "전라남도 목포시"), "direct")
        self.assertEqual(message_applies(jn, "전라남도 전체"), "sido_wide")
        self.assertEqual(message_applies(jn, "전남광주통합특별시"), "sido_wide")
        self.assertIsNone(message_applies(jn, "전라남도 영암군"))
        self.assertIsNone(message_applies(M, "전라남도 목포시"))
        gw = [rid for rid, r in load_regions().items() if r["display"] == "강원특별자치도 강릉시"][0]
        self.assertEqual(message_applies(gw, "강원특별자치도 강릉시 "), "direct")
        self.assertEqual(message_applies(gw, "강원도 강릉시"), "direct")
        self.assertEqual(message_applies("kr_L1100000", "서울특별시 강남구 "), "direct")
        self.assertEqual(message_applies("kr_L1100000", "서울특별시 전체"), "direct")

    def test_real_fixture_jeonnam_both_prefixes_match(self):
        bodies = []
        for f in glob.glob(str(ROOT / "data/fixtures/dssp/*.json")):
            if "_live__" in f:
                continue
            with open(f, encoding="utf-8") as fp:
                bodies += (json.load(fp).get("json") or {}).get("body") or []
        names = {n.strip() for m in bodies for n in m["RCPTN_RGN_NM"].split(",")}
        jn_new = [n for n in names if n.startswith("전남광주통합특별시 ") and not n.endswith(("구", "전체"))]
        jn_old = [n for n in names if n.startswith("전라남도 ") and not n.endswith("전체")]
        self.assertTrue(jn_new and jn_old)  # 실제로 두 표기가 섞여 온다
        regions = load_regions()
        for n in jn_new + jn_old:
            hit = [rid for rid in regions if message_applies(rid, n) == "direct"]
            self.assertTrue(hit, n)

    def test_generated_file_is_up_to_date_and_hand_regions_kept(self):
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / "mcp_server/tools/gen_regions.py"), "--check"], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stdout.decode())
        regions = load_regions()
        self.assertEqual(regions[M]["kma_zones"], ["L1130110", "L1130120"])  # 손으로 쓴 값 유지
        self.assertEqual(regions[G]["kma_zones"], ["L1013400"])
        # 구역은 한 시군에만 — 예외: 광역시 합집합(소속 군 포함), 제주시·서귀포시가 함께 쓰는 '제주도산지'
        zones = [z for r in regions.values() if r.get("level") != "metro" for z in r["kma_zones"]]
        shared = {z for r in regions.values() for z, n in r["kma_zone_names"].items() if n == "제주도산지"}
        self.assertEqual({z for z in zones if zones.count(z) > 1}, shared)


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
