"""답변 검증 (결정론). 위반이 있으면 재작성 → 그래도 실패하면 템플릿 답변으로 대체한다."""
from __future__ import annotations

import re

from config.catalog import RULES, msg

FORBIDDEN: list[str] = RULES["forbidden"]
MK: dict[str, str] = msg("markers")
CITE = re.compile(r"\[([EMG]\d+)\]")
URL = re.compile(r"https?://|www\.|kakao\.com", re.I)
KAKAO_LINK = re.compile(r"https://map\.kakao\.com/link/(map|to)/\d+")


def validate_links(links: dict, notice: str | None, region: str) -> dict | None:
    """지도 링크 칩 검사 (ADR-011): 공식 형식(장소 ID)만, 고정 안내 문구 필수. 어기면 칩을 내보내지 않는다."""
    if notice != msg("kakao.route_notice"):
        return None
    urls = [links.get("map"), links.get("route")]
    if not all(isinstance(u, str) and KAKAO_LINK.fullmatch(u) for u in urls):
        return None
    return {"region": region, "notice": notice,
            "chips": [{"kind": "map", "label": msg("kakao.map_label"), "url": urls[0]},
                      {"kind": "route", "label": msg("kakao.route_label"), "url": urls[1]}]}


def validate(answer: str, judgements: list[dict]) -> dict:
    v: list[str] = []
    ids = {e["id"] for j in judgements for e in j["evidence"]}

    for w in FORBIDDEN:
        if w in answer:
            v.append(f"금지 표현: '{w}'")
    if MK["official"] not in answer or MK["emergency_call"] not in answer:
        v.append("공식 경보 아님/119 안내 문구 누락")

    if URL.search(answer):  # 링크는 코드가 만든 칩으로만 (LLM 이 지어낸 링크 차단)
        v.append("답변 본문에 링크 금지")

    cited = set(CITE.findall(answer))
    for c in cited - ids:
        v.append(f"존재하지 않는 근거 인용: [{c}]")

    for line in answer.splitlines():
        s = line.strip()
        if s.startswith(("-", "•", "*")) and not CITE.search(s) and MK["no_info"] not in s and MK["no_guarantee"] not in s:
            v.append(f"근거 없는 항목: '{s[:40]}'")

    for j in judgements:
        if j["region"] not in answer:
            v.append(f"지역 누락: {j['region']}")
        if j["level"] == "unknown" and MK["no_info"] not in answer:
            v.append("조회 실패인데 '정보 없음' 표기 없음")
        if j["level"] == "none" and MK["no_risk"] not in answer:
            v.append("위험 없음 표현이 규정 문구가 아님")
        if j["level"] in ("advisory", "warning", "severe", "upcoming"):
            alert_ids = {e["id"] for e in j["evidence"] if e["kind"] in ("alert", "pending")}
            if alert_ids and not (alert_ids & cited):
                v.append(f"{j['region']}: 발효 중 특보 근거 인용 없음")
        for g in j["gaps"]:
            if g["status"] in ("failed", "no_coverage") and MK["no_info"] not in answer:
                v.append(f"{g['source']} 조회 실패 미표기")
    return {"ok": not v, "violations": v}
