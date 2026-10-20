"""근거 기반 답변 템플릿 (결정론). FakeLLM 의 출력이자, LLM 답변이 검증을 통과하지 못할 때의 안전한 대체."""
from __future__ import annotations

from config.catalog import msg, reason

DISCLAIMER = msg("disclaimer")


def _cite(e: dict) -> str:
    return f"[{e['id']}]"


def gap_line(g: dict) -> str:
    """조회 실패·범위 밖 표준 문구 — 원인별 사유를 붙인다 (ADR-014). 템플릿과 write 지시문이 같은 문구를 쓴다."""
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
            out.append(msg("answer.none", as_of=j["as_of"], zones=", ".join(j["zones"])))
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
            for e in guides:
                out.append(f"- [{e['hazard']}] {e['text']} {_cite(e)}")
        for g in j["gaps"]:
            if g["status"] == "not_queried":
                continue
            out.append(gap_line(g) if g["status"] != "unavailable" else msg("answer.gap_unavailable", source=g["source"], reason=msg("causes.not_in_data")))
        if intent == "go_out":
            out.append(msg("answer.go_out"))
        out.append("")
    out.append(DISCLAIMER)
    return "\n".join(out)
