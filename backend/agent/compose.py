"""근거 기반 답변 템플릿 (결정론). FakeLLM 의 출력이자, LLM 답변이 검증을 통과하지 못할 때의 안전한 대체."""
from __future__ import annotations

from config.catalog import msg, reason

DISCLAIMER = msg("disclaimer")


def _cite(e: dict) -> str:
    return f"[{e['id']}]"


def zone_text(j: dict) -> str:
    """조회 구역은 표시 이름으로 (특보구역 코드는 사용자에게 보이지 않는다, 개발자 상세에만)."""
    return "·".join(j.get("zone_names") or j["zones"])


def gap_line(g: dict) -> str:
    """조회 실패·범위 밖·데이터에 없음 표준 문구 — 원인별 사유를 붙인다 (ADR-014). 템플릿과 write 지시문이 같은 문구를 쓴다."""
    if g["status"] == "unavailable":
        return msg("answer.gap_unavailable", source=g["source"], reason=msg("causes.not_in_data"))
    return msg("answer.gap", source=g["source"], reason=reason(g["status"], g.get("cause")))


def render_answer(judgements: list[dict], intent: str = "status") -> str:
    out: list[str] = []
    scope_label = msg("answer.message_scope")
    for j in judgements:
        out.append(f"### {j['region']}")
        evs = j["evidence"]
        alerts = [e for e in evs if e["kind"] in ("alert", "pending", "released")]
        if j["level"] == "unknown":
            out.append(msg("answer.unknown"))
        elif j["level"] == "none":
            out.append(msg("answer.none", as_of=j["as_of"], zones=zone_text(j)))
        else:
            out.append(msg("answer.level", level_label=j["level_label"], as_of=j["as_of"]))
        for e in alerts:
            out.append(f"- {e['text']} {_cite(e)}" + (msg("answer.uncertain_suffix") if e.get("uncertain") else ""))
        if j.get("note"):
            out.append(j["note"])
        for e in [x for x in evs if x["kind"] == "message"]:
            body = e["text"].replace("\n", " ")
            out.append(f"- 최근 안내 문자({e['created_at']}, {scope_label.get(e['scope'], scope_label['direct'])}): "
                       f"{body[:90]}{'…' if len(body) > 90 else ''} {_cite(e)}")
        guides = [x for x in evs if x["kind"] == "guide"]
        if guides:
            out.append(msg("answer.guide_header"))
            last = None
            for e in guides:  # judge 가 묶음 순서대로 골라 둔 것을 그대로 (지금 할 일 / 미리 대비할 일 / 하지 말 것)
                head = (e["hazard"], e.get("group_label"))
                if e.get("group_label") and head != last:
                    out.append(msg("answer.guide_group", label=e["group_label"], hazard=e["hazard"]))
                    last = head
                src = f" · {e['source']}" if e.get("source") else ""
                out.append(f"- [{e['hazard']}{src}] {e['text']} {_cite(e)}")
        for g in j["gaps"]:
            if g["status"] == "not_queried":
                continue
            out.append(gap_line(g))
        if intent == "go_out":
            out.append(msg("answer.go_out"))
        out.append("")
    out.append(DISCLAIMER)
    return "\n".join(out)
