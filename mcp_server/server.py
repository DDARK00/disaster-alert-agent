"""재난·특보 상황 판단 에이전트용 MCP 서버 (Tool / Resource / Prompt).

- Tool     : 지역 해석, 특보 상태, 재난문자, 행동요령 검색, 쉼터 목록  (모두 결정론; LLM 미사용)
- Resource : guide://action/{hazard} — 행동요령 전문(정적 문서)
- Prompt   : disaster_briefing_prompt — 답변 작성 규칙 (안전 문구 규칙 포함)

DATA_MODE=replay(기본) | live. 모든 응답에 mode, 기준 시각, 소스 상태를 싣는다.
조회 실패·범위 밖은 '정보 없음'이며 '안전'이 아니다.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # config/, prompts/

from fastmcp import FastMCP  # noqa: E402

import sources  # noqa: E402
from alert_state import fold_alerts  # noqa: E402
import kakao  # noqa: E402
import shelters  # noqa: E402
from regions import load_regions, region_for_admin, resolve_location as _resolve  # noqa: E402
import failures  # noqa: E402
from config.catalog import msg, prompt, reason  # noqa: E402
from config.settings import Settings  # noqa: E402

mcp = FastMCP(name="disaster-advisor")

DISCLAIMER = msg("disclaimer")
RECENT_RELEASE_HOURS = 6


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def _resolve_time_mode(as_of: str | None, mode: str | None) -> tuple[datetime, str]:
    mode = mode or sources.default_mode()
    if mode not in ("replay", "live"):
        raise ValueError("mode 는 replay 또는 live")
    t = sources.parse_as_of(as_of)
    if t is None:
        t = sources.now_kst() if mode == "live" else sources.parse_as_of("2026-10-19 23:59")
    return t, mode


@mcp.tool()
def resolve_location(text: str, history_region_id: str | None = None,
                     browser_region_id: str | None = None) -> dict:
    """사용자 표현에서 지역 후보를 해석한다. status: resolved/assumed/ambiguous/none.
    ambiguous 이면 호출자는 후보를 모두 조회해 둘 다 보여준다(위급하지 않으면 되묻지 않음)."""
    return _resolve(text, history_region_id, browser_region_id)


@mcp.tool()
def resolve_place(query: str) -> dict:
    """장소·주소 표현 → 행정구역(지원 지역이면 내부 지역 ID)과 카카오맵 링크 (ADR-011).
    Kakao Local 을 요청마다 라이브 호출하고 결과를 저장하지 않는다. 장소명·좌표는 돌려주지 않는다.
    status: ok | not_found | failed | unavailable(키 없음). 경로를 계산하지 않는다 — links 는 카카오맵으로 넘기는 용도."""
    r = kakao.lookup(query)
    out = {"status": r["status"], "query": query, "result_count": r.get("result_count", 0), "detail": r.get("detail"),
           "region_id": None, "b_code": None, "region_names": [], "place_id": None, "links": None}
    if r["status"] == "ok":
        rid = region_for_admin(r["region_names"])
        out.update(region_id=rid, region_display=load_regions()[rid]["display"] if rid else None,
                   b_code=r["b_code"], region_names=r["region_names"],
                   place_id=r["place_id"], links=kakao.links(r["place_id"]), notice=msg("kakao.route_notice"))
    return out


@mcp.tool()
def get_alert_status(region_id: str, as_of: str | None = None, mode: str | None = None) -> dict:
    """지역의 기상특보 발효 상태를 기준 시각(as_of, KST 'YYYY-MM-DD HH:MM')으로 접어서 돌려준다.
    alerts=발효 중, pending=발효 예고, recently_released=최근 해제. source.status 가 ok 가 아니면
    '정보 없음'이다(안전 아님)."""
    t, mode = _resolve_time_mode(as_of, mode)
    region = load_regions()[region_id]
    zones = sources.region_zones(region_id)
    src = sources.get_weather_events(zones, t, mode)
    zone_names = [region.get("kma_zone_names", {}).get(z, z) for z in zones]  # 사용자 표시는 이름, 코드는 개발자용
    out = {"region_id": region_id, "region": region["display"], "mode": mode, "as_of": _fmt(t),
           "zones": zones, "zone_names": zone_names, "source": {"name": "기상청 기상특보 조회서비스 (WthrWrnInfoService)",
                                      "status": src["status"], "cause": src.get("cause"), "detail": src["detail"],
                                      "window": src["window"]},
           "alerts": [], "pending": [], "recently_released": [], "disclaimer": DISCLAIMER}
    if src["status"] != "ok":
        out["statement"] = msg("mcp.alert_failed", reason=reason(src["status"], src.get("cause")))
        return out
    for s in fold_alerts(src["events"], t, set(zones)):
        row = {"zone": s["area_code"], "zone_name": s["area_name"], "hazard": s["hazard"], "warn_var": s["warn_var"],
               "level": s["stress_name"], "since": _fmt(s["since"]), "uncertain": s["uncertain"],
               "release_at": _fmt(s.get("release_at")), "inferred": s.get("inferred", False),
               "announced_at": _fmt(s["last_event"]["tmFc"]), "source_event": f"{s['area_code']}/{s['last_event']['tmFc']:%Y%m%d%H%M}/{s['last_event']['tmSeq']}"}
        if s["status"] == "active":
            out["alerts"].append(row)
        elif s["status"] == "pending_start":
            out["pending"].append(row)
        elif s.get("released_at") and t - s["released_at"] <= timedelta(hours=RECENT_RELEASE_HOURS):
            row["released_at"] = _fmt(s["released_at"])
            out["recently_released"].append(row)
    if out["alerts"]:
        out["statement"] = msg("mcp.alert_active", count=len(out["alerts"]), as_of=_fmt(t))
    else:
        pending = (msg("mcp.alert_none_pending", count=len(out["pending"])) if out["pending"]
                   else msg("mcp.alert_none_no_pending"))
        out["statement"] = msg("mcp.alert_none", as_of=_fmt(t), zones="·".join(zone_names), pending=pending)
    return out


@mcp.tool()
def get_disaster_messages(region_id: str, as_of: str | None = None, lookback_hours: int = 24,
                          hazards: list[str] | None = None, limit: int = 20, mode: str | None = None) -> dict:
    """지역에 적용되는 긴급재난문자(최근 lookback_hours). scope: direct(해당 시군구) / sido_wide(시도 전체).
    label(재해구분)은 본문과 어긋날 수 있어 1차 필터로만 쓴다 (트러블슈팅 T-010)."""
    t, mode = _resolve_time_mode(as_of, mode)
    src = sources.get_messages(region_id, t, lookback_hours, mode)
    out = {"region_id": region_id, "mode": mode, "as_of": _fmt(t), "lookback_hours": lookback_hours,
           "source": {"name": "행정안전부 긴급재난문자 (DSSP-IF-00247)", "status": src["status"],
                      "cause": src.get("cause"), "detail": src["detail"]},
           "messages": [], "total_matched": 0, "label_note": "재해구분 라벨은 본문과 다를 수 있음"}
    if src["status"] != "ok":
        out["statement"] = msg("mcp.messages_failed", reason=reason(src["status"], src.get("cause")))
        return out
    msgs = src["messages"]
    if hazards:
        msgs = [m for m in msgs if m["label"] in hazards]
    out["total_matched"] = len(msgs)
    out["messages"] = msgs[:limit]
    out["statement"] = msg("mcp.messages_count", hours=lookback_hours, count=len(msgs))
    return out


@mcp.tool()
def search_action_guide(hazard: str, phase: str | None = None, limit: int = 30, mode: str | None = None) -> dict:
    """재난 유형별 국민행동요령 (자연재난·사회재난·생활안전·민방위 — 소스는 hazard_map 으로 코드가 정한다).
    phase: prepare(사전/예보) | during(특보·발생 중) | after(이후) | info(참고).
    API에 없는 유형(건조·열대야)은 available=False — 다른 재난의 행동요령으로 대체하지 않는다.
    live 는 일 1회 캐시, 실패하면 저장본으로 폴백(origin). 조회 실패로 저장본도 없으면 cause 를 싣는다."""
    mode = mode or sources.default_mode()
    g = sources.load_guide(hazard, mode)
    if not g["available"]:
        return {"hazard": hazard, "available": False, "reason": g["reason"], "rows": [],
                **({"cause": g["cause"]} if g.get("cause") else {})}
    rows = [r for r in g["rows"] if phase in (None, r["phase"])]
    return {"hazard": hazard, "available": True, "phase": phase, "source": g["source"], "source_name": g["source_name"],
            "source_label": g["source_label"],
            "snapshot_at": g["snapshot_at"], "origin": g["origin"], "excluded_non_citizen": g["excluded"],
            "count": len(rows), "rows": rows[:limit], "phases_present": sorted({r["phase"] for r in g["rows"]})}


@mcp.tool()
def get_shelters(region_id: str, hazard: str, mode: str | None = None) -> dict:
    """공식 목록(행정안전부 통합대피소)에 있는 쉼터·대피장소 — 폭염·한파 쉼터, 지진 옥외대피장소만.
    재난 유형 → 구분은 hazard_map 의 shelter (코드). 그 밖의 유형(호우·태풍 등)은 status=not_applicable.
    가까운 순이 아니고(좌표를 받지 않는다) 운영 여부·시간은 데이터에 없다 — '공식 목록에 있는 장소'로만 안내한다.
    status: ok | failed(cause) | no_range(지역 범위 데이터 없음) | no_coverage(replay 저장본 없음) | not_applicable."""
    mode = mode or sources.default_mode()
    kind = (sources._hazard_map().get(hazard) or {}).get("shelter")
    if not kind:
        return {"region_id": region_id, "hazard": hazard, "status": "not_applicable", "shelters": [],
                "reason": f"'{hazard}'에 연결된 쉼터·대피장소 구분이 없음"}
    return {"region_id": region_id, "hazard": hazard, "mode": mode, **shelters.load(region_id, kind, mode)}


@mcp.tool()
def list_hazards() -> dict:
    """지원 재난 유형과 행동요령·쉼터 구분 유무."""
    return {"hazards": [{"name": n, "warn_var": v["warnVar"], "has_action_guide": bool(v["action_cate2"]),
                         "guide_source": v.get("source"), "shelter": v.get("shelter")}
                        for n, v in sources._hazard_map().items()]}


@mcp.tool()
def source_health() -> dict:
    """소스별 최근 live 호출 성공·실패 시각과 원인 코드 (키·URL 없음). 이 프로세스가 기동한 뒤의 기록만."""
    return {"mode": sources.default_mode(), "sources": failures.snapshot()}


@mcp.resource("guide://action/{hazard}")
def action_guide_doc(hazard: str) -> str:
    """재난 유형별 행동요령 전문 (단계별 정리, 출처 포함)."""
    g = sources.load_guide(hazard, sources.default_mode())
    if not g["available"]:
        return f"# {hazard} 행동요령\n\n해당 행동요령이 없습니다: {g['reason']}"
    lines = [f"# {hazard} 행동요령", f"출처: {g['source']} (수집 {g['snapshot_at']})", ""]
    for phase, title in (("prepare", "사전 준비 / 예보 시"), ("during", "특보 중 / 발생 시"), ("after", "이후"), ("info", "참고")):
        rows = [r for r in g["rows"] if r["phase"] == phase]
        if rows:
            lines.append(f"## {title}")
            lines += [f"- {r['text']} [{r['id']}]" for r in rows]
            lines.append("")
    return "\n".join(lines)


@mcp.prompt()
def disaster_briefing_prompt(region: str, hazards: str, question: str) -> str:
    """답변 작성 규칙 — write 노드가 이 규칙을 따른다. (원문: prompts/briefing.md)"""
    return prompt("briefing", region=region, hazards=hazards, question=question, disclaimer=DISCLAIMER)


if __name__ == "__main__":
    cfg = Settings.from_env()
    if cfg.mcp_transport == "http":  # 컨테이너: Streamable HTTP
        mcp.run(transport="http", host=cfg.mcp_host, port=cfg.mcp_port, path=cfg.mcp_path, show_banner=False)
    else:  # 로컬 개발: stdio (백엔드가 서브프로세스로 기동)
        mcp.run(show_banner=False)
