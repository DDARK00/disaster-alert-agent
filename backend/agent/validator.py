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


def _sections(answer: str, judgements: list[dict]) -> tuple[dict[str, str], str]:
    """'### 지역' 제목으로 답변을 지역별로 나눈다. 반환: (지역 → 본문, 어느 지역에도 속하지 않는 본문).
    지역이 하나면 답변 전체가 그 지역의 본문이다."""
    if len(judgements) == 1:
        return {judgements[0]["region"]: answer}, ""
    out: dict[str, list[str]] = {}
    outside: list[str] = []
    cur = None
    for line in answer.splitlines():
        if line.lstrip().startswith("#"):
            cur = next((j["region"] for j in judgements if j["region"] in line), None)
            if cur is not None:
                out.setdefault(cur, [])
            continue
        (out[cur] if cur is not None else outside).append(line)
    return {k: "\n".join(v) for k, v in out.items()}, "\n".join(outside)


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
            if g["status"] == "unavailable" and MK["not_in_data"] not in answer:  # 없는 행동요령을 지어내지 않았다는 표기
                v.append(f"{g['source']} 공식 데이터에 없음 미표기")

    # 지역별 대조: 근거 ID 는 지역마다 E1 부터 다시 시작하므로 전체 합집합이 아니라 그 지역 섹션 안에서 검사한다
    sections, outside = _sections(answer, judgements)
    if len(judgements) > 1:
        for c in sorted(set(CITE.findall(outside))):
            v.append(f"지역 섹션 밖 인용: [{c}]")
    for j in judgements:
        if j["region"] not in sections:
            if len(judgements) > 1:
                v.append(f"지역 섹션 없음: '### {j['region']}'")
            continue
        own = {e["id"] for e in j["evidence"]}
        sec_cited = set(CITE.findall(sections[j["region"]]))
        for c in sorted(sec_cited - own):
            v.append(f"{j['region']}: 이 지역 근거가 아닌 인용 [{c}]")
        # 발효 중·예고 특보는 하나도 빠짐없이 인용 (일부만 말하면 위험을 줄여 전달하게 된다)
        for e in j["evidence"]:
            if e["kind"] in ("alert", "pending") and e["id"] not in sec_cited:
                v.append(f"{j['region']}: 특보 근거 [{e['id']}] 인용 없음")
    return {"ok": not v, "violations": v}
