"""전국 지역 데이터 생성: data/kma_zones.json + data/region_build.yaml → data/regions.json (ADR-017).

규칙 (데이터는 region_build.yaml, 해석은 regions.py):
- 지역 단위: 광역시(시 전체) · 도 소속 시군 · 광역시 소속 군(강화·옹진·군위) · 특수 구역 독립 지역(울릉군).
  도 전체는 지역이 아니다 — 상위 구역은 특보가 발표되지 않아(NO_DATA, T-013) '위험 없음'으로 잘못 읽힐 수 있다.
  도 이름만 오면 resolve_location 이 시·군을 되묻는다(provinces).
- 특보 구역 = 자기 + 하위 전체(세부 권역까지)의 합집합 + attach 로 붙인 특수 구역.
- 재난문자·카카오 접두어 = 시도 허용 목록(신구 명칭) × 시군 이름.
- 별칭: 정식 이름(시·군 포함)과 '시도 + 이름'은 강한 별칭, 줄임말은 약한 별칭(일상어와 겹치면 제외).
  같은 이름이 여러 지역에 있으면 약한 별칭으로 내린다(되묻기). 광역시의 구 이름은 districts(시 전체 기준 안내).
- 손으로 쓴 지역(gwangju_metro, gyeonggi_gwangju)은 그대로 두고, 그 특보 구역을 덮는 생성 지역은 만들지 않는다.

    .venv/bin/python mcp_server/tools/gen_regions.py          # data/regions.json 갱신 (커밋 대상)
    .venv/bin/python mcp_server/tools/gen_regions.py --check  # 생성 결과가 커밋된 파일과 같은지 (테스트용)
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ZONES = ROOT / "data" / "kma_zones.json"
BUILD = ROOT / "data" / "region_build.yaml"
OUT = ROOT / "data" / "regions.json"
HAND = ("gwangju_metro", "gyeonggi_gwangju")
PAREN = re.compile(r"\(.*?\)")


def _land_zones() -> dict[str, dict]:
    zs = json.loads(ZONES.read_text(encoding="utf-8"))["zones"]
    return {z["REG_ID"]: z for z in zs if z["REG_ID"].startswith("L1") and z["TM_ED"].startswith("2100")}


def _children(zones: dict) -> dict[str, list[str]]:
    ch: dict[str, list[str]] = {}
    for zid, z in zones.items():
        ch.setdefault(z["REG_UP"], []).append(zid)
    return {k: sorted(v) for k, v in ch.items()}


def _subtree(zid: str, ch: dict) -> list[str]:
    out = [zid]
    for c in ch.get(zid, []):
        out += _subtree(c, ch)
    return out


def _strip(name: str) -> str:
    """'수원시' → '수원', '고성군' → '고성' (두 글자 미만이 되면 빈 문자열)."""
    s = re.sub(r"(시|군)$", "", name)
    return s if len(s) >= 2 and s != name else ""


def build() -> dict:
    cfg = yaml.safe_load(BUILD.read_text(encoding="utf-8"))
    zones = _land_zones()
    ch = _children(zones)
    old = json.loads(OUT.read_text(encoding="utf-8"))
    hand = [r for r in old["regions"] if r["id"] in HAND]
    hand_zones = {z for r in hand for z in r["kma_zones"] + r.get("kma_parent_zones", [])}
    stop = set(cfg["short_alias_stopwords"])

    def zname(z: str) -> str:
        return zones[z]["REG_NAME"]

    units: list[dict] = []        # (level, sido_key, admin name, zones)
    provinces: list[dict] = []
    sido_of_zone: dict[str, str] = {}
    by_name = {PAREN.sub("", zones[z]["REG_NAME"]).strip(): z for z in zones}
    attach_to: dict[str, list[str]] = {}
    for special, targets in cfg["attach"].items():
        for t in targets:
            attach_to.setdefault(t, []).append(by_name[special])
    attached = {by_name[s] for s in cfg["attach"]}

    for top in ch["L1000000"]:
        nm = zname(top)
        if nm in cfg["sido"]:
            sido_of_zone[top] = nm
            if nm.endswith("도"):
                provinces.append({"name": cfg["sido"][nm]["prefixes"][0], "sido": nm,
                                  "aliases": cfg["sido"][nm]["prefixes"] + cfg["sido"][nm]["short"]})
                for c in ch.get(top, []):
                    if c in attached:
                        continue
                    units.append({"level": "sigun", "sido": nm, "name": PAREN.sub("", zname(c)).strip(),
                                  "zones": _subtree(c, ch)})
            else:
                units.append({"level": "metro", "sido": nm, "name": nm, "zones": _subtree(top, ch)})
                for c in ch.get(top, []):  # 광역시 소속 군(강화·옹진·군위)은 자기 구역이 있어 시군 단위로도 둔다
                    if zname(c).endswith("군"):
                        units.append({"level": "sigun", "sido": nm, "name": zname(c), "zones": _subtree(c, ch)})
        elif nm in cfg["special_regions"]:
            sp = cfg["special_regions"][nm]
            units.append({"level": "sigun", "sido": sp["sido"], "name": sp["name"], "zones": _subtree(top, ch),
                          "extra": sp.get("aliases", [])})
        elif nm not in cfg["attach"]:
            raise SystemExit(f"분류되지 않은 시도급 구역: {nm}")
    for u in units:
        for z in attach_to.get(u["name"], []):
            u["zones"] += _subtree(z, ch)

    names = Counter(u["name"] for u in units if u["level"] == "sigun")
    regions = list(hand)
    for u in units:
        if set(u["zones"]) & hand_zones:  # 손으로 쓴 지역이 담당
            continue
        s = cfg["sido"][u["sido"]]
        pre, short = s["prefixes"], s["short"]
        if u["level"] == "metro":
            display = pre[0]
            strong, weak = pre + short, list(s.get("weak", []))
            dssp, wide = list(pre), []
            districts = cfg["metro_districts"].get(u["sido"], [])
        else:
            n, st = u["name"], _strip(u["name"])
            display = f"{pre[0]} {n}"
            forms = [f"{p} {x}" for p in pre + short for x in (n, st) if x]
            strong = forms + ([n] if names[n] == 1 else [])
            weak = ([n] if names[n] > 1 else []) + ([st] if st and st not in stop else [])
            strong += [a for a in u.get("extra", []) + cfg["extra_aliases"].get(n, [])]
            dssp, wide = [f"{p} {n}" for p in pre], list(pre)
            districts = []
        zs = sorted(dict.fromkeys(u["zones"]))
        rid = "kr_" + u["zones"][0]  # 단위의 뿌리 구역 코드 (subtree 첫 값)
        regions.append({"id": rid, "name": display, "display": display, "level": u["level"], "sido": pre[0],
                        "kma_zones": zs, "kma_zone_names": {z: zname(z) for z in zs},
                        "dssp_prefixes": dssp, "dssp_sido_wide": wide,
                        "aliases_strong": list(dict.fromkeys(strong)), "aliases_weak": list(dict.fromkeys(weak)),
                        **({"districts": districts} if districts else {}),
                        "kakao_prefixes": list(dssp), "generated": True})
    ids = Counter(r["id"] for r in regions)
    dup = [k for k, v in ids.items() if v > 1]
    if dup:
        raise SystemExit(f"지역 ID 중복: {dup}")
    return {"_note": old["_note"], "regions": regions, "provinces": provinces,
            "show_both_groups": cfg["show_both_groups"], "assumptions": old["assumptions"]}


def main() -> int:
    out = build()
    text = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
    if sys.argv[1:2] == ["--check"]:
        same = OUT.read_text(encoding="utf-8") == text
        print("regions.json 최신" if same else "regions.json 이 생성 결과와 다름 — gen_regions.py 를 다시 실행")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    lv = Counter(r.get("level", "hand") for r in out["regions"])
    print(f"지역 {len(out['regions'])}개 {dict(lv)} · 도 {len(out['provinces'])}개 → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
