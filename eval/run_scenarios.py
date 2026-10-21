"""시연 시나리오를 SSE(/chat/stream)로 돌려 합격 기준을 확인한다.

    .venv/bin/python eval/run_scenarios.py                      # nginx 경유 (도커), live 제외
    .venv/bin/python eval/run_scenarios.py --base http://localhost:8100 --live

확인: error 이벤트 없음 · final 도착 · 검증 통과(또는 template_fallback) · 금지 표현 0 · 인용 id 실재
      · 위급은 fixed+119 · 모호 '광주'는 두 지역 · quick 은 검증 통과·final 전. 이벤트 도착 시각으로 SSE 버퍼링 여부도 본다.
      + 시나리오별 check (scenarios.json): route · regions · guide · shelter · max_shelters · ask_candidates · links
        · min_messages · district_scope · cause. 장애 주입 시나리오(fault)는 서버가 DEMO_FAULTS=1 일 때만 목록에 준다.
판정은 백엔드 validator 를 그대로 다시 돌린다 (느슨한 별도 기준을 만들지 않는다).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.validator import FORBIDDEN, validate  # noqa: E402
from config.catalog import msg  # noqa: E402


def stream(client: httpx.Client, base: str, sc: dict) -> dict:
    params = {"message": sc["message"], "mode": sc["mode"]}
    if sc.get("as_of"):
        params["as_of"] = sc["as_of"]
    if sc.get("fault"):
        params["fault"] = sc["fault"]
    t0 = time.perf_counter()
    events: list[tuple[float, str, dict]] = []
    with client.stream("GET", f"{base}/chat/stream", params=params) as r:
        r.raise_for_status()
        name = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                events.append((time.perf_counter() - t0, name, json.loads(line[5:])))
    return {"events": events, "total": time.perf_counter() - t0}


def check(sc: dict, res: dict) -> tuple[list[str], dict]:
    evs = res["events"]
    names = [n for _, n, _ in evs]
    problems: list[str] = []
    errs = [d for _, n, d in evs if n == "error"]
    if errs:
        problems.append(f"error 이벤트: {errs[0].get('message')}")
    finals = [d for _, n, d in evs if n == "final"]
    if not finals:
        return problems + ["final 없음"], {}
    f = finals[0]
    ans, js, src = f["answer"], f.get("judgements") or [], f.get("answer_source")
    hit = [w for w in FORBIDDEN if w in ans]
    if hit:
        problems.append(f"금지 표현 {hit}")
    if js:
        v = validate(ans, js)
        if not v["ok"]:
            problems.append(f"검증 위반 {v['violations'][:3]}")
    if sc["id"] == "emergency" and (src != "fixed" or "119" not in ans):
        problems.append("위급인데 고정 119 응답 아님")
    if sc["id"] == "ambiguous_rain" and len(js) != 2:
        problems.append(f"모호 지명인데 지역 {len(js)}개")
    # 단계별 표시 (ADR-019): quick 은 검증을 통과한 템플릿이어야 하고, 근거가 있는 응답이면 final 전에 와야 한다
    for d in (d for _, n, d in evs if n == "quick"):
        if not validate(d["answer"], js)["ok"] or any(w in d["answer"] for w in FORBIDDEN):
            problems.append("quick 요약이 검증을 통과하지 못함")
    if js and "quick" in names and names.index("quick") > names.index("final"):
        problems.append("quick 이 final 뒤에 옴")
    if "".join(d["text"] for _, n, d in evs if n == "token") != ans:
        problems.append("token 합 ≠ final.answer")
    problems += check_specific(sc.get("check") or {}, f, [d for _, n, d in evs if n == "links"])
    t = {n: round(ts, 2) for ts, n, _ in reversed(evs)}  # 이벤트별 첫 도착 시각
    vals = [d for _, n, d in evs if n == "validation"]
    info = {"src": src, "levels": [j["level"] for j in js], "validations": [(d["attempt"], d["ok"]) for d in vals],
            "llm_errors": f["meta"].get("llm_errors", []), "llm": f["meta"].get("llm"),
            "t_judgement": t.get("judgement"), "t_quick": t.get("quick"), "t_first_token": t.get("token"),
            "t_final": t.get("final"),
            "n_events": len(names)}
    return problems, info


def check_specific(c: dict, f: dict, links: list[dict]) -> list[str]:
    """시나리오별 합격 조건 (scenarios.json check). 판정은 응답 데이터로만 — 답변 문장을 따로 해석하지 않는다."""
    p: list[str] = []
    js = f.get("judgements") or []
    ev = [e for j in js for e in j["evidence"]]
    if "route" in c and f["route"] != c["route"]:
        p.append(f"route {f['route']} ≠ {c['route']}")
    if "regions" in c and [j["region_id"] for j in js] != c["regions"]:
        p.append(f"지역 {[j['region_id'] for j in js]} ≠ {c['regions']}")
    if c.get("guide") and not any(e["kind"] == "guide" for e in ev):
        p.append("행동요령 근거 없음")
    sh = [e for e in ev if e["kind"] == "shelter"]
    if "shelter" in c and bool(sh) != c["shelter"]:
        p.append(f"쉼터 {len(sh)}곳 (기대 {'있음' if c['shelter'] else '없음'})")
    if sh and msg("markers.shelter_notice") not in f["answer"]:
        p.append("쉼터 운영 여부 미확인 안내 없음")
    if "max_shelters" in c and len(sh) > c["max_shelters"]:
        p.append(f"쉼터 {len(sh)}곳 > {c['max_shelters']}")
    if "ask_candidates" in c and len((f.get("location") or {}).get("candidates") or []) != c["ask_candidates"]:
        p.append("되묻기 후보 수가 다름")
    if c.get("links") and not (f.get("links") or links):
        p.append("지도 링크 칩 없음")
    if "min_messages" in c and sum(e["kind"] == "message" for e in ev) < c["min_messages"]:
        p.append(f"재난문자 {sum(e['kind'] == 'message' for e in ev)}건 < {c['min_messages']}")
    if c.get("district_scope") and not all(j.get("scope_note") and j["scope_note"] in f["answer"] for j in js):
        p.append("광역시 전체 기준 안내 없음")
    if c.get("district_scope") and re.search(r"\bL\d{7}\b", f["answer"]):
        p.append("특보구역 코드 노출")
    if "cause" in c and (c["cause"] not in {x.get("cause") for x in f["meta"].get("failures", [])}
                         or msg(f"causes.{c['cause']}") not in f["answer"]):
        p.append(f"원인 {c['cause']} 문구 없음")
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080/api")
    ap.add_argument("--live", action="store_true", help="live 시나리오 포함 (실제 API 호출)")
    ap.add_argument("--only", nargs="*", help="시나리오 id")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--show", action="store_true", help="답변 본문 출력")
    a = ap.parse_args()
    with httpx.Client(timeout=a.timeout) as c:
        scenarios = c.get(f"{a.base}/scenarios").json()
        failed = 0
        for sc in scenarios:
            if a.only and sc["id"] not in a.only:
                continue
            if sc["mode"] == "live" and not a.live:
                print(f"SKIP {sc['id']} (live)")
                continue
            res = stream(c, a.base, sc)
            problems, info = check(sc, res)
            failed += bool(problems)
            print(f"{'PASS' if not problems else 'FAIL'} {sc['id']:<17} {res['total']:6.1f}s {json.dumps(info, ensure_ascii=False)}")
            for p in problems:
                print(f"     - {p}")
            if a.show:
                fin = [d for _, n, d in res["events"] if n == "final"]
                print("     " + (fin[0]["answer"] if fin else "").replace("\n", "\n     "))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
