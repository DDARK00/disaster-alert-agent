"""데이터 접근층 — replay(저장된 fixture) / live(실제 호출) 두 모드를 같은 모양으로 돌려준다.

안전 원칙
- replay 는 fixture 가 덮는 시간 범위(coverage) 안에서만 '데이터 있음/없음'을 말한다.
  범위 밖이면 status="no_coverage" 이며, 이를 '위험 없음'으로 해석해서는 안 된다.
- live 호출 실패는 status="failed" (안전이 아님) + cause(원인 코드, failures.py).
- 시각은 모두 KST naive datetime.
"""
from __future__ import annotations

import glob
import hashlib
import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import failures
from alert_state import normalize_items
from regions import load_regions, message_applies

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # config/ (Settings)
    sys.path.insert(0, str(ROOT))
from config.settings import Settings  # noqa: E402
FIX = ROOT / "data" / "fixtures"
KST = timezone(timedelta(hours=9))
CONTIGUOUS_TOLERANCE = timedelta(minutes=2)


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None, second=0, microsecond=0)


def parse_as_of(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d%H%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"시각 형식을 해석할 수 없음: {s!r} (예: 2026-10-18 17:00)")


def default_mode() -> str:
    return Settings.from_env().data_mode


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fp:
        return json.load(fp)


def _api_ok(d: dict) -> bool:
    """meta.api_ok 가 없는 예전 fixture 는 본문 header 로 판정한다 (resultCode 0/00 만 정상)."""
    ok = d["meta"].get("api_ok")
    if ok is not None:
        return bool(ok)
    j = d.get("json")
    if not isinstance(j, dict):
        return False
    hdr = (j.get("response") or j).get("header") or {}
    return str(hdr.get("resultCode")) in ("0", "00") and d["meta"].get("http_status") == 200


def _fetched_kst(meta: dict) -> datetime:
    dt = datetime.fromisoformat(meta["fetched_at"])
    return dt.astimezone(KST).replace(tzinfo=None)


def _params(meta: dict) -> dict:
    q = urllib.parse.urlparse(meta["url"]).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}


def _merge(windows: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted(windows):
        if out and s - out[-1][1] <= CONTIGUOUS_TOLERANCE:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _covered(windows, t: datetime):
    return next(((s, e) for s, e in windows if s <= t <= e), None)


def _fetch(source: str, operation: str, params: dict, tag: str, ok_codes=("0", "00")) -> tuple[dict, str | None]:
    """live 호출 1회 + 원인 분류 + 소스별 최근 상태 기록."""
    from capture import call
    rec = call(source, operation, params, tag)
    cause = failures.classify(rec, ok_codes)
    failures.note(source, cause, failures.detail(rec, cause) if cause else "")
    return rec, cause


def _failed(cause: str, info: str, **extra) -> dict:
    return {"status": "failed", "cause": cause, "detail": info, **extra}


# ───────────────────────── 기상특보 ─────────────────────────

def _replay_files(pattern: str) -> list[str]:
    """replay 원본 = 저장소에 커밋된 fixture. live 캡처(*_live__*, gitignore)는 섞지 않는다 (T-023)."""
    return [f for f in sorted(glob.glob(pattern)) if "_live__" not in Path(f).name]


def _weather_replay(zones: list[str]) -> tuple[list[dict], dict[str, list]]:
    """fixture 에서 zone 별 이벤트와 coverage 를 모은다."""
    events: dict[tuple, dict] = {}
    cover: dict[str, list] = {z: [] for z in zones}
    for f in _replay_files(str(FIX / "weather" / "getPwnCd_*.json")):
        d = _load(f)
        meta = d["meta"]
        if not _api_ok(d) or not d.get("json"):
            continue
        p = _params(meta)
        body = d["json"]["response"].get("body") or {}
        items = (body.get("items") or {}).get("item") or []
        if isinstance(items, dict):
            items = [items]
        rows = int(p.get("numOfRows", 10))
        if len(items) >= rows:  # 잘렸을 수 있음 → coverage 로 쓰지 않는다
            continue
        fetched = _fetched_kst(meta)
        f0 = p.get("fromTmFc") or fetched.strftime("%Y%m%d")
        t0 = p.get("toTmFc") or fetched.strftime("%Y%m%d")
        start = datetime.strptime(f0, "%Y%m%d")
        end = min(datetime.strptime(t0, "%Y%m%d") + timedelta(hours=23, minutes=59), fetched)
        scope = p.get("areaCode")
        for z in zones:
            if scope in (None, z):
                cover[z].append((start, end))
        for it in items:
            if scope is None or it["areaCode"] == scope:
                key = (it["areaCode"], str(it["tmFc"]), it["tmSeq"], it["warnVar"], it["command"])
                events[key] = it
    return [e for e in events.values() if e["areaCode"] in zones], {z: _merge(w) for z, w in cover.items()}


def get_weather_events(zones: list[str], as_of: datetime, mode: str) -> dict:
    """반환: {status, events, window, detail}. status: ok | no_coverage | failed"""
    if mode == "replay":
        events, cover = _weather_replay(zones)
        ranges, missing = [], []
        for z in zones:
            w = _covered(cover[z], as_of)
            (ranges if w else missing).append((z, w))
        if missing:
            avail = {z: [(s.strftime("%Y-%m-%d %H:%M"), e.strftime("%Y-%m-%d %H:%M")) for s, e in cover[z]]
                     for z, _ in missing}
            return {"status": "no_coverage", "events": [], "window": None,
                    "detail": f"replay fixture 가 {as_of:%Y-%m-%d %H:%M} 을 덮지 않음 (구역별 범위: {avail})"}
        start = min(w[0] for _, w in ranges)
        end = max(w[1] for _, w in ranges)
        return {"status": "ok", "events": events, "window": [f"{start:%Y-%m-%d %H:%M}", f"{end:%Y-%m-%d %H:%M}"],
                "detail": "replay"}

    # live: 구역마다 60일 창(API 최대)으로 호출 → 오래 지속되는 특보(폭염 등)를 놓치지 않는다.
    events = []
    frm = (as_of - timedelta(days=59)).strftime("%Y%m%d")
    for z in zones:
        rec, cause = _fetch("weather", "getPwnCd",
                            {"numOfRows": 300, "pageNo": 1, "dataType": "JSON", "fromTmFc": frm,
                             "toTmFc": as_of.strftime("%Y%m%d"), "areaCode": z}, f"getPwnCd_{z}_live",
                            ok_codes=("0", "00", "03"))  # 03 NO_DATA = 이벤트 없음 (정상)
        if cause:
            return _failed(cause, f"{z}: {failures.detail(rec, cause)}", events=[], window=None)
        resp = rec["json"].get("response") or rec["json"]
        if str(resp["header"]["resultCode"]) == "03":
            continue
        try:
            items = ((resp.get("body") or {}).get("items") or {}).get("item") or []
            items = [items] if isinstance(items, dict) else items
            normalize_items(items)  # 형식 검사 (필드 누락·타입 오류 → response_invalid)
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            failures.note("weather", "response_invalid", f"{z}: {type(e).__name__}")
            return _failed("response_invalid", f"{z}: response_invalid {type(e).__name__}", events=[], window=None)
        events.extend(items)
    return {"status": "ok", "events": events, "window": [f"{frm}", as_of.strftime("%Y%m%d")], "detail": "live"}


# ───────────────────────── 재난문자 ─────────────────────────

def _msg_row(m: dict, scope: str) -> dict:
    return {"sn": m["SN"], "created_at": m["CRT_DT"].replace("/", "-"), "label": m["DST_SE_NM"],
            "step": m["EMRG_STEP_NM"], "text": m["MSG_CN"].strip(), "scope": scope,
            "receive_regions": [r.strip() for r in m["RCPTN_RGN_NM"].split(",") if r.strip()]}


def _dssp_replay() -> tuple[list[dict], list]:
    msgs: dict[int, dict] = {}
    cover = []
    for f in _replay_files(str(FIX / "dssp" / "*.json")):
        d = _load(f)
        meta = d["meta"]
        if not _api_ok(d) or not d.get("json"):
            continue
        p = _params(meta)
        body = d["json"].get("body") or []
        total = d["json"].get("totalCount", 0)
        if p.get("rgnNm") or len(body) < total:  # 지역 필터 또는 잘린 응답은 coverage 로 쓰지 않음
            continue
        fetched = _fetched_kst(meta)
        cover.append((datetime.strptime(p["crtDt"], "%Y%m%d"), fetched))
        for m in body:
            msgs[m["SN"]] = m
    return list(msgs.values()), _merge(cover)


def get_messages(region_id: str, as_of: datetime, lookback_hours: int, mode: str) -> dict:
    since = as_of - timedelta(hours=lookback_hours)
    if mode == "replay":
        allm, cover = _dssp_replay()
        if not (_covered(cover, as_of) and _covered(cover, since)):
            return {"status": "no_coverage", "messages": [],
                    "detail": f"replay fixture 범위 밖 (범위: {[(f'{s:%Y-%m-%d %H:%M}', f'{e:%Y-%m-%d %H:%M}') for s, e in cover]})"}
    else:
        allm = []
        for page in (1, 2, 3):
            rec, cause = _fetch("dssp", "", {"numOfRows": 1000, "pageNo": page, "returnType": "json",
                                             "crtDt": since.strftime("%Y%m%d")}, "msgs_live")
            if cause:
                return _failed(cause, failures.detail(rec, cause), messages=[])
            j = rec["json"]
            allm.extend(j.get("body") or [])
            if len(allm) >= int(j.get("totalCount") or 0):
                break
    out = []
    try:
        for m in allm:
            t = datetime.strptime(m["CRT_DT"], "%Y/%m/%d %H:%M:%S")
            if not (since <= t <= as_of):
                continue
            scope = message_applies(region_id, m["RCPTN_RGN_NM"])
            if scope:
                out.append(_msg_row(m, scope))
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        if mode == "replay":
            raise
        failures.note("dssp", "response_invalid", type(e).__name__)
        return _failed("response_invalid", f"response_invalid {type(e).__name__}", messages=[])
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return {"status": "ok", "messages": out, "detail": mode}


# ───────────────────────── 행동요령 ─────────────────────────

def _hazard_map() -> dict:
    return json.loads((ROOT / "data" / "hazard_map.json").read_text(encoding="utf-8"))["hazards"]


def load_guide(hazard: str) -> dict:
    hm = _hazard_map().get(hazard)
    if hm is None:
        return {"available": False, "reason": f"알 수 없는 재난 유형: {hazard}", "rows": []}
    cate = hm["action_cate2"]
    if not cate:
        return {"available": False, "reason": f"'{hazard}'는 행동요령 API에 해당 항목이 없음", "rows": []}
    files = sorted(glob.glob(str(FIX / "doing" / f"cate_{cate}__*.json"))) or \
        (sorted(glob.glob(str(FIX / "doing" / "all__*.json"))) if cate == "01001" else [])
    if not files:
        return {"available": False, "reason": f"fixture 없음: {cate}", "rows": []}
    d = _load(files[-1])
    rows = []
    for r in d["json"]["body"]:
        if not r.get("safety_cate3") or not r.get("actRmks"):
            continue  # 제목 행(분류 없음) 제외 (T-012)
        text = r["actRmks"].strip().lstrip("-").strip()
        rid = r["safety_cate3"] + "-" + hashlib.sha1(text.encode()).hexdigest()[:6]
        rows.append({"id": rid, "text": text, "section": r["safety_cate_nm3"],
                     "phase": hm["phases"].get(r["safety_cate3"], "info"), "hazard": hazard})
    return {"available": True, "rows": rows, "snapshot_at": d["meta"]["fetched_at"],
            "source": "행정안전부 자연재난국민행동요령 (DSSP-IF-20588)"}


def region_zones(region_id: str) -> list[str]:
    return list(load_regions()[region_id]["kma_zones"])
