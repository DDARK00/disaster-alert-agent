"""쉼터·대피장소 어댑터 — 통합대피소(DSSP-IF-10941). 폭염·한파 쉼터와 지진 옥외대피장소만.

- 재난 유형 → 구분(heat·cold·quake)은 hazard_map.json 의 shelter (코드가 정한다, LLM 이 고르지 않음).
  구분 → 요청 코드·이름 필터는 config/rules.yaml shelters.kinds (데이터). 호우·태풍 등은 해당 구분이 없어 붙이지 않는다.
- 지역 범위: regions.json 의 shelter_area.bbox (위경도 사각형) 로 요청하고, 사각형에 섞이는 이웃 시군 시설은
  shelter_area.addr_prefixes (도로명 주소 앞부분, 신구 명칭 모두) 로 거른다. 범위 데이터가 없는 지역은 status=no_range.
- 결과는 가까운 순이 아니다(사용자 좌표를 받지 않는다). 필터 뒤 sort_by 순 정렬, limit 개 — 같은 입력은 같은 출력.
  운영 여부·운영 시간 필드는 응답에 없다 → 안내는 "공식 목록에 있는 장소"까지만 (문구는 messages.yaml shelters.*).
- live: (지역, 구분) 단위 캐시 cache_hours. 실패하면 커밋된 fixture 로 폴백(origin=fixture_fallback, 원인 기록).
  replay: 커밋된 fixture 만. 호출 수·일일 한도는 capture 카운터(sources.yaml daily_limit).
- 좌표는 돌려주지 않는다(경로를 만들지 않는다). 지도 연결은 백엔드가 이름·주소로 카카오맵 검색 링크 칩을 만든다.

fixture 갱신(실제 호출, 커밋 대상):  .venv/bin/python mcp_server/shelters.py capture [지역 ID ...]
"""
from __future__ import annotations

import glob
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import failures  # noqa: E402
import registry  # noqa: E402
from config.catalog import RULES  # noqa: E402
from regions import load_regions  # noqa: E402
from schemas import Envelope, ShelterRow  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "data" / "fixtures"
SOURCE = "shelter"
KST = timezone(timedelta(hours=9))
CFG = RULES["shelters"]
KINDS: dict[str, dict] = CFG["kinds"]
log = logging.getLogger("mcp.shelters")

_cache: dict[tuple[str, str], dict] = {}


class ShelterFetchError(Exception):
    def __init__(self, cause: str, info: str = ""):
        super().__init__(f"{cause} {info}".strip())
        self.cause = cause


def _check_hazard_map() -> None:
    """hazard_map 의 shelter 값이 rules.yaml shelters.kinds 에 있어야 한다 (기동 시 검사)."""
    hm = json.loads((ROOT / "data" / "hazard_map.json").read_text(encoding="utf-8"))["hazards"]
    bad = [f"{hz}: {v['shelter']}" for hz, v in hm.items() if v.get("shelter") and v["shelter"] not in KINDS]
    if bad:
        raise RuntimeError(f"hazard_map shelter 값이 rules.yaml shelters.kinds 에 없음: {bad}")


_check_hazard_map()


def _now() -> datetime:
    return datetime.now(KST)


def area(region_id: str) -> dict | None:
    return load_regions()[region_id].get("shelter_area")


def _tag(region_id: str, code: str, page: int) -> str:
    return f"{region_id}_cd{code}_p{page}"


# ───────── 정규화 + 필터 (코드) ─────────

def _space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def in_area(address: str, prefixes: list[str]) -> bool:
    a = _space(address)
    return any(a == p or a.startswith(p + " ") for p in prefixes)


def kind_match(kind: str, row: dict) -> bool:
    k = KINDS[kind]
    if k["name_word"]:
        return k["name_word"] in (row.get("SHLT_SE_NM") or "")
    codes = {c.strip() for c in str(row.get("SHLT_SE_CD") or "").split(",")}
    return bool(codes & set(k["codes"]))


def select(kind: str, raw: list[dict], prefixes: list[str]) -> tuple[list[dict], dict]:
    """원본 행 → (채택 행 전체 정렬본, 집계). 이름 필터·주소 필터·중복 제거 후 sort_by 순."""
    seen, out = set(), []
    stats = {"rows": len(raw), "outside_area": 0, "other_kind": 0}
    for r in raw:
        if not kind_match(kind, r):
            stats["other_kind"] += 1
            continue
        if not in_area(r["RONA_DADDR"], prefixes):
            stats["outside_area"] += 1
            continue
        if r["MNG_SN"] in seen:
            continue
        seen.add(r["MNG_SN"])
        out.append({"id": r["MNG_SN"], "name": _space(r["REARE_NM"]), "address": _space(r["RONA_DADDR"]),
                    "kind_name": _space(r["SHLT_SE_NM"])})
    out.sort(key=lambda x: tuple(x[k] for k in CFG["sort_by"]))
    return out, stats


# ───────── 호출 (live) ─────────

def _params(a: dict, code: str, page: int) -> dict:
    b = a["bbox"]
    return {"numOfRows": registry.get(SOURCE).page_size, "pageNo": page, "returnType": "json",
            "startLat": b["lat"][0], "endLat": b["lat"][1], "startLot": b["lot"][0], "endLot": b["lot"][1],
            "shlt_se_cd": code}


def fetch(region_id: str, kind: str, tag_suffix: str = "_live") -> tuple[list[dict], str]:
    """구분의 코드마다 쪽을 넘기며 호출 — 주소 필터 뒤 limit 개가 차거나 max_pages 까지. 실패하면 ShelterFetchError."""
    from capture import call
    a = area(region_id)
    size = registry.get(SOURCE).page_size
    raw, fetched = [], None
    for code in KINDS[kind]["codes"]:
        got: list[dict] = []
        for page in range(1, CFG["max_pages"] + 1):
            rec = call(SOURCE, "", _params(a, code, page), _tag(region_id, code, page) + tag_suffix)
            cause = failures.classify(rec)
            failures.note(SOURCE, cause, failures.detail(rec, cause) if cause else "")
            if cause:
                raise ShelterFetchError(cause, f"{region_id}/cd{code} p{page}: {failures.detail(rec, cause)}")
            try:
                env = Envelope[ShelterRow].model_validate(rec["json"])
            except ValidationError as e:
                failures.note(SOURCE, "response_invalid", f"{region_id}/cd{code} p{page}: {e.error_count()} errors")
                raise ShelterFetchError("response_invalid", f"{region_id}/cd{code} p{page}") from e
            body = [r.model_dump() for r in env.body or []]
            got += body
            fetched = fetched or rec["meta"]["fetched_at"]
            if len(body) < size or len(select(kind, got, a["addr_prefixes"])[0]) >= CFG["limit"]:
                break
        raw += got
    from capture import calls_today
    log.info("shelter fetch region=%s kind=%s rows=%d calls_today=%s", region_id, kind, len(raw), calls_today(SOURCE))
    return raw, fetched


# ───────── fixture (replay·폴백) ─────────

def _stamp(f: str) -> str:
    return Path(f).stem.split("__")[-1]


def fixture_rows(region_id: str, kind: str) -> tuple[list[dict], str] | None:
    """커밋된 fixture: 코드·쪽마다 가장 최근 파일. 코드 하나라도 없으면 None (부분 목록을 전체처럼 쓰지 않는다)."""
    d = FIX / registry.get(SOURCE).fixture_dir
    raw, fetched = [], None
    for code in KINDS[kind]["codes"]:
        files = [f for f in glob.glob(str(d / f"{region_id}_cd{code}_p*__*.json")) if "_live__" not in f]
        if not files:
            return None
        by_page: dict[int, str] = {}
        for f in files:
            n = int(re.search(r"_p(\d+)__", f).group(1))
            if n not in by_page or _stamp(f) > _stamp(by_page[n]):
                by_page[n] = f
        for n in sorted(by_page):
            rec = json.loads(Path(by_page[n]).read_text(encoding="utf-8"))
            env = Envelope[ShelterRow].model_validate(rec["json"])
            raw += [r.model_dump() for r in env.body or []]
            fetched = fetched or rec["meta"]["fetched_at"]
    return raw, fetched


# ───────── 진입점 ─────────

def load(region_id: str, kind: str, mode: str) -> dict:
    """get_shelters 용. status: ok | failed(cause) | no_range(지역 범위 데이터 없음) | no_coverage(replay 저장본 없음)
    | not_applicable(소스 비활성). failed 만 '정보 없음' gap 으로 답변에 나간다 — 나머지는 쉼터 없이 정상 응답."""
    cfg = registry.get(SOURCE)
    base = {"kind": kind, "kind_label": KINDS[kind]["label"], "source": cfg.attribution,
            "source_label": cfg.title.removeprefix("행정안전부 "), "shelters": []}
    if not registry.usable(SOURCE):
        return {**base, "status": "not_applicable", "reason": f"소스 상태 {cfg.status} — 서비스 응답에 쓰지 않음"}
    a = area(region_id)
    if not a:
        return {**base, "status": "no_range", "reason": "이 지역의 쉼터 조회 범위 데이터가 없음"}
    got, origin, cause = None, "fixture", None
    if mode == "live":
        key = (region_id, kind)
        c = _cache.get(key)
        if c and _now() - c["at"] < timedelta(hours=CFG["cache_hours"]):
            got, origin = (c["raw"], c["fetched"]), "live_cache"
        else:
            try:
                raw, fetched = fetch(region_id, kind)
                _cache[key] = {"at": _now(), "raw": raw, "fetched": fetched}
                got, origin = (raw, fetched), "live"
            except ShelterFetchError as e:
                cause = e.cause
                log.warning("shelter live 실패 → fixture 폴백 region=%s kind=%s cause=%s", region_id, kind, cause)
                origin = "fixture_fallback"
    if got is None:
        got = fixture_rows(region_id, kind)
        if got is None:
            if cause:
                return {**base, "status": "failed", "cause": cause, "reason": f"조회 실패({cause}), 저장본 없음"}
            return {**base, "status": "no_coverage", "reason": f"replay 저장본 없음: {region_id}/{kind}"}
    rows, stats = select(kind, got[0], a["addr_prefixes"])
    return {**base, "status": "ok", "shelters": rows[:CFG["limit"]], "matched": len(rows), "stats": stats,
            "snapshot_at": got[1], "origin": origin, **({"fallback_cause": cause} if cause else {})}


def capture_all(region_ids: list[str] | None = None) -> None:
    """shelter_area 가 있는 지역 × 구분을 실제 호출로 받아 커밋용 fixture(<지역>_cd<코드>_p<n>__)로 저장한다."""
    for rid, r in load_regions().items():
        if not r.get("shelter_area") or (region_ids and rid not in region_ids):
            continue
        for kind in KINDS:
            raw, _ = fetch(rid, kind, tag_suffix="")
            rows, stats = select(kind, raw, r["shelter_area"]["addr_prefixes"])
            print(f"{rid} {kind}: 원본 {len(raw)}행 → 채택 {len(rows)} (주소 밖 {stats['outside_area']}, 다른 구분 {stats['other_kind']})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if sys.argv[1:2] == ["capture"]:
        capture_all(sys.argv[2:] or None)
    else:
        print(__doc__)
