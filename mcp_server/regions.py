"""지역 정규화와 모호 지명 해석 (결정론, LLM 미사용). 데이터는 data/regions.json (전국 생성본, ADR-017).

- resolve_location: 사용자 표현 → 후보 지역 ID와 점수/근거
- message_applies : 재난문자 수신지역(RCPTN_RGN_NM)이 특정 지역에 적용되는지
- region_for_admin: Kakao 행정구역 이름 → 지역 ID

별칭은 긴 것부터 맞추고, 이미 맞춘 글자 범위와 겹치는 짧은 별칭은 세지 않는다
('경기도 광주시' 안의 '광주', '강서구' 안의 '서구', '전남광주통합특별시 목포시' 안의 '전남광주').
"""
from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "regions.json"

# 점수 가중치: 명시적 수식어 > 대화 이력 > 브라우저 위치 > 약한 별칭
W_STRONG, W_HISTORY, W_BROWSER, W_WEAK = 10, 5, 2, 3
MARGIN_RESOLVED = 5  # 1,2위 차이가 이 이상이면 확정


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_regions() -> dict:
    return {r["id"]: r for r in _data()["regions"]}


@lru_cache(maxsize=1)
def _alias_index() -> list[tuple[str, list[tuple[str | None, int, str]]]]:
    """별칭 → [(지역 ID 또는 None(도), 가중치, 종류)], 긴 별칭 먼저.
    광역시 구 이름(districts)은 한 광역시에만 있으면 강한 별칭, 여러 곳에 있으면 약한 별칭(되묻기로 이어짐)."""
    idx: dict[str, list] = {}
    regions = load_regions()
    dist_count = Counter(d for r in regions.values() for d in r.get("districts", []))
    for rid, r in regions.items():
        for a in r["aliases_strong"]:
            idx.setdefault(a, []).append((rid, W_STRONG, "strong"))
        for a in r["aliases_weak"]:
            idx.setdefault(a, []).append((rid, W_WEAK, "weak"))
        for d in r.get("districts", []):
            idx.setdefault(d, []).append((rid, W_STRONG if dist_count[d] == 1 else W_WEAK, "district"))
    for p in _data().get("provinces", []):
        for a in p["aliases"]:
            # 도 이름과 같은 약한 별칭(예: '제주' = 제주도 / 제주시 줄임말)은 도로 본다 → 시·군 되묻기
            idx[a] = [t for t in idx.get(a, []) if t[2] != "weak"] + [(None, 0, "province:" + p["name"])]
    return sorted(idx.items(), key=lambda kv: (-len(kv[0]), kv[0]))


def _matches(text: str) -> list[tuple[str, list]]:
    used = [False] * len(text)
    out = []
    for alias, targets in _alias_index():
        spans, start = [], text.find(alias)
        while start >= 0:
            if not any(used[start:start + len(alias)]):
                spans.append(start)
            start = text.find(alias, start + 1)
        if spans:
            for s in spans:
                used[s:s + len(alias)] = [True] * len(alias)
            out.append((alias, targets))
    return out


def _show_both(ids: set[str]) -> bool:
    """같은 이름이어도 둘 다 조회해 보여 주는 묶음인가 (ADR-006 '광주'). 그 밖의 동명은 되묻는다 (ADR-017)."""
    return any(ids <= set(g) for g in _data().get("show_both_groups", []))


def resolve_location(text: str, history_region_id: str | None = None,
                     browser_region_id: str | None = None) -> dict:
    """status: resolved(확정) / assumed(가정 칩 표시) / ambiguous(둘 다 표시, ask=True 면 되묻기) / none.
    none 이면서 province 가 있으면 도 이름만 온 것 — 시·군을 되묻는다.
    districts: 광역시 구 이름으로 정해진 지역 → 그 구 이름 (답변에 '시 전체 기준'을 밝힌다)."""
    regions = load_regions()
    scores: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    districts: dict[str, str] = {}
    province = None

    def add(rid: str, w: int, why: str) -> None:
        scores[rid] = scores.get(rid, 0) + w
        reasons.setdefault(rid, []).append(f"{why}(+{w})")

    for alias, targets in _matches(text):
        for rid, w, kind in targets:
            if rid is None:
                province = province or kind.split(":", 1)[1]
            elif kind == "district":
                add(rid, w, f"{'명시 표현' if w == W_STRONG else '약한 표현'} '{alias}'(구 이름)")
                districts.setdefault(rid, alias)
            else:
                add(rid, w, f"{'명시 표현' if kind == 'strong' else '약한 표현'} '{alias}'")
    if history_region_id in regions:
        add(history_region_id, W_HISTORY, "대화 이력")
    if browser_region_id in regions:
        add(browser_region_id, W_BROWSER, "브라우저 위치")

    ranked = sorted(scores.items(), key=lambda t: (-t[1], t[0]))
    cands = [{"id": rid, "name": regions[rid]["display"], "score": sc, "reasons": reasons[rid]}
             for rid, sc in ranked]
    if not cands:
        return {"status": "none", "chosen": None, "candidates": [], **({"province": province} if province else {})}

    def out(status: str, chosen: str | None, **extra) -> dict:
        d = {k: v for k, v in districts.items() if chosen is None or k == chosen}
        return {"status": status, "chosen": chosen, "candidates": cands, **({"districts": d} if d else {}), **extra}

    # 약한 별칭만 겹친 경우(점수 동률)는 모호 → 광주 묶음은 둘 다 보여 주고, 그 밖은 되묻는다.
    top = ranked[0][1]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if len(ranked) == 1 or top - second >= MARGIN_RESOLVED:
        status = "resolved" if top >= W_STRONG or len(ranked) == 1 and top >= W_HISTORY else "assumed"
        return out(status, ranked[0][0])
    if top > second:
        return out("assumed", ranked[0][0])
    tied = {rid for rid, sc in ranked if sc == top}
    if _show_both({c["id"] for c in cands}):
        return out("ambiguous", None)
    cands = [c for c in cands if c["id"] in tied]
    return out("ambiguous", None, ask=True)


def region_for_admin(names: list[str]) -> str | None:
    """Kakao 행정구역 이름 ['시도', '시군구'] → 내부 지역 ID (지원 지역이 아니면 None). 가장 긴 접두어가 이긴다."""
    full = " ".join(n for n in names if n).strip()
    best, best_len = None, -1
    for rid, r in load_regions().items():
        for p in r.get("kakao_prefixes", []):
            if (full == p or full.startswith(p + " ")) and len(p) > best_len:
                best, best_len = rid, len(p)
    return best


def message_applies(region_id: str, rcptn_rgn_nm: str) -> str | None:
    """'direct' | 'sido_wide' | None.  RCPTN_RGN_NM 은 쉼표 결합, 끝 공백, '전체' 접미가 섞여 있다."""
    r = load_regions()[region_id]
    result = None
    for raw in rcptn_rgn_nm.split(","):
        name = " ".join(raw.split())
        if name.endswith(" 전체"):
            name = name[: -len(" 전체")].strip()
        if not name:
            continue
        if any(name == p or name.startswith(p + " ") for p in r["dssp_prefixes"]):
            return "direct"
        if name in r["dssp_sido_wide"]:
            result = "sido_wide"
    return result
