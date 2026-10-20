"""행동요령 어댑터 — 자연재난·사회재난·생활안전·민방위 4종을 같은 코드 경로로 (ADR-013, ADR-015).

- 소스 정의는 data/sources.yaml, 재난 유형 → (소스, 유형 코드)는 hazard_map.json 의 source·action_cate2 (코드가 정한다).
- 유형 코드(safety_cate, 5자리)별 호출. totalCount 로 쪽 수를 계산하고, 꽉 찬 쪽이면 다음 쪽도 확인한다.
- live: 프로세스 내 일 1회 캐시 (KST 날짜가 바뀌면 다시 호출). 실패하면 커밋된 fixture 로 폴백(origin 표시).
  replay: 커밋된 fixture 만.
- 정규화: 제목 행(safety_cate3 없음)·내용 없는 행(actRmks null/공백) 제외, 앞의 '-'·'○' 제거, 'Q : … A : …' 덩어리는
  'Q … — A …' 한 줄로. 대상 필터(구조대원·관리자용 행 제외)는 config/rules.yaml guide_audience (데이터), 판정은 여기(코드).
  금지 표현 행 제외(T-024)는 judge 가 한다 (모든 근거에 공통).

fixture 갱신(실제 호출, 커밋 대상):  .venv/bin/python mcp_server/guides.py capture [소스 이름 ...]
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import math
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
from schemas import Envelope, GuideRow  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "data" / "fixtures"
KST = timezone(timedelta(hours=9))
MAX_PAGES = 10
log = logging.getLogger("mcp.guides")
AUD = RULES["guide_audience"]
QA = re.compile(r"^\s*Q\s*[:：]\s*(?P<q>.+?)\s*A\s*[:：]\s*(?P<a>.+)$", re.S)
LEAD = re.compile(r"^[\s\-–○●•·※]+")

_cache: dict[tuple[str, str], dict] = {}


class GuideFetchError(Exception):
    def __init__(self, cause: str, info: str = ""):
        super().__init__(f"{cause} {info}".strip())
        self.cause = cause


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


# ───────── 호출 (live) ─────────

def fetch_pages(source: str, cate: str, tag_suffix: str = "_live") -> tuple[list[dict], str]:
    """유형 코드 하나의 모든 쪽을 호출한다. 반환: (원본 행, 수집 시각 ISO). 실패하면 GuideFetchError(cause)."""
    from capture import call
    cfg = registry.get(source)
    rows: list[dict] = []
    page, pages, fetched = 1, 1, None
    while page <= min(pages, MAX_PAGES):
        rec = call(source, "", {"numOfRows": cfg.page_size, "pageNo": page, "returnType": "json", "safety_cate": cate},
                   f"cate_{cate}_p{page}{tag_suffix}")
        cause = failures.classify(rec)
        failures.note(source, cause, failures.detail(rec, cause) if cause else "")
        if cause:
            raise GuideFetchError(cause, f"{source}/{cate} p{page}: {failures.detail(rec, cause)}")
        try:
            env = Envelope[GuideRow].model_validate(rec["json"])
        except ValidationError as e:
            failures.note(source, "response_invalid", f"{cate} p{page}: {e.error_count()} errors")
            raise GuideFetchError("response_invalid", f"{source}/{cate} p{page}") from e
        body = [r.model_dump() for r in env.body or []]
        rows += body
        fetched = fetched or rec["meta"]["fetched_at"]
        pages = max(pages, math.ceil(env.totalCount / cfg.page_size) if env.totalCount else 1)
        if len(body) == cfg.page_size and page == pages:  # 꽉 찬 마지막 쪽 → totalCount 를 믿지 않고 한 쪽 더 (ADR-013)
            pages += 1
        if not body:
            break
        page += 1
    log.info("guide fetch source=%s cate=%s pages=%d rows=%d calls_today=%s", source, cate, page - 1, len(rows),
             _calls_today(source))
    return rows, fetched


def _calls_today(source: str) -> int:
    from capture import calls_today
    return calls_today(source)


# ───────── fixture (replay·폴백) ─────────

def _stamp(f: str) -> str:
    return Path(f).stem.split("__")[-1]


def fixture_pages(source: str, cate: str) -> tuple[list[dict], str] | None:
    """커밋된 fixture 에서 유형 코드의 행. 쪽 단위 파일(cate_<코드>_p<n>__) 우선, 없으면 예전 단일 파일."""
    d = FIX / registry.get(source).fixture_dir
    paged = [f for f in glob.glob(str(d / f"cate_{cate}_p*__*.json")) if "_live__" not in f]
    if paged:
        latest_p1 = max((f for f in paged if f"_p1__" in f), key=_stamp)
        stamp = _stamp(latest_p1)
        by_page: dict[int, str] = {}
        for f in paged:  # 같은 수집 회차 = p1 이후에 저장된 쪽 중 쪽마다 가장 이른 것
            n = int(re.search(r"_p(\d+)__", f).group(1))
            if _stamp(f) >= stamp and (n not in by_page or _stamp(f) < _stamp(by_page[n])):
                by_page[n] = f
        files = [by_page[n] for n in sorted(by_page)]
    else:
        legacy = sorted(glob.glob(str(d / f"cate_{cate}__*.json")))
        if not legacy and source == "doing" and cate == "01001":  # 태풍은 유형 지정 없는 첫 쪽(all__)에만 있다
            legacy = sorted(glob.glob(str(d / "all__*.json")))
        if not legacy:
            return None
        files = legacy[-1:]
    rows, fetched = [], None
    for f in files:
        rec = json.loads(Path(f).read_text(encoding="utf-8"))
        env = Envelope[GuideRow].model_validate(rec["json"])
        rows += [r.model_dump() for r in env.body or []]
        fetched = fetched or rec["meta"]["fetched_at"]
    return rows, fetched


# ───────── 정규화 + 대상 필터 ─────────

def clean_text(raw: str) -> str:
    text = LEAD.sub("", raw.strip()).strip()
    m = QA.match(text)
    if m:  # 'Q : 질문 A : 답' 한 덩어리 → 한 줄 (질문은 맥락으로 남긴다)
        text = f"{m.group('q').strip()} — {LEAD.sub('', m.group('a').strip())}"
    return re.sub(r"\s+", " ", text)


def for_citizens(cate2: str, section: str, text: str) -> bool:
    """일반 시민 대상이 아닌 행(구조대원·관리자·전문가 지침)을 걸러 낸다. 규칙은 rules.yaml guide_audience."""
    for r in AUD["exclude_sections"]:
        if r["cate2"] in (cate2, "*") and any(w in section for w in r["words"]):
            return False
    return not any(w in text for w in AUD["exclude_text_words"])


def normalize(source: str, hazard: str, hm: dict, raw: list[dict]) -> tuple[list[dict], int]:
    """반환: (시민용 행, 대상 필터로 뺀 행 수)."""
    rows, seen, excluded = [], set(), 0
    for r in raw:
        if not r.get("safety_cate3") or not (r.get("actRmks") or "").strip():
            continue  # 제목 행(T-012)·내용 없는 행
        text = clean_text(r["actRmks"])
        section = r.get("safety_cate_nm3") or ""
        if not text:
            continue
        if not for_citizens(r["safety_cate2"], section, text):
            excluded += 1
            continue
        rid = r["safety_cate3"] + "-" + hashlib.sha1(text.encode()).hexdigest()[:6]
        if rid in seen:  # 쪽 경계 중복 방지
            continue
        seen.add(rid)
        rows.append({"id": rid, "text": text, "section": section, "cate3": r["safety_cate3"],
                     "phase": hm["phases"].get(r["safety_cate3"], "info"), "hazard": hazard})
    return rows, excluded


# ───────── 진입점 ─────────

def load(hazard: str, hm: dict, mode: str) -> dict:
    """search_action_guide 용. 반환: {available, rows, snapshot_at, source, origin, excluded, cause?, reason?}"""
    source, cate = hm.get("source"), hm["action_cate2"]
    cfg = registry.get(source)
    if not registry.usable(source):
        return {"available": False, "reason": f"소스 '{source}' 상태 {cfg.status} — 서비스 응답에 쓰지 않음", "rows": []}
    got, origin, cause = None, "fixture", None
    if mode == "live":
        key = (source, cate)
        c = _cache.get(key)
        if c and c["day"] == _today():
            got, origin = (c["raw"], c["fetched"]), "live_cache"
        else:
            try:
                raw, fetched = fetch_pages(source, cate)
                _cache[key] = {"day": _today(), "raw": raw, "fetched": fetched}
                got, origin = (raw, fetched), "live"
            except GuideFetchError as e:
                cause = e.cause
                log.warning("guide live 실패 → fixture 폴백 source=%s cate=%s cause=%s", source, cate, cause)
                origin = "fixture_fallback"
    if got is None:
        got = fixture_pages(source, cate)
        if got is None:
            if cause:
                return {"available": False, "cause": cause, "reason": f"조회 실패({cause}), 저장본 없음", "rows": []}
            return {"available": False, "reason": f"fixture 없음: {source}/{cate}", "rows": []}
    rows, excluded = normalize(source, hazard, hm, got[0])
    return {"available": True, "rows": rows, "snapshot_at": got[1], "source": cfg.attribution, "source_name": source,
            "source_label": cfg.title.removeprefix("행정안전부 "),
            "origin": origin, "excluded": excluded, **({"fallback_cause": cause} if cause else {})}


def capture_all(names: list[str] | None = None) -> None:
    """hazard_map 에 연결된 유형을 실제 호출로 받아 커밋용 fixture(cate_<코드>_p<n>__)로 저장한다."""
    hm = json.loads((ROOT / "data" / "hazard_map.json").read_text(encoding="utf-8"))["hazards"]
    pairs = sorted({(v["source"], v["action_cate2"]) for v in hm.values() if v.get("action_cate2")})
    for source, cate in pairs:
        if names and source not in names:
            continue
        rows, _ = fetch_pages(source, cate, tag_suffix="")
        print(f"{source} {cate}: {len(rows)}행")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if sys.argv[1:2] == ["capture"]:
        capture_all(sys.argv[2:] or None)
    else:
        print(__doc__)
