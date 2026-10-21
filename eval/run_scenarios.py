"""시연 시나리오를 SSE(/chat/stream)로 돌려 합격 기준을 확인한다.

    .venv/bin/python eval/run_scenarios.py                      # nginx 경유 (도커), live 제외
    .venv/bin/python eval/run_scenarios.py --base http://localhost:8100 --live

확인: error 이벤트 없음 · final 도착 · 검증 통과(또는 template_fallback) · 금지 표현 0 · 인용 id 실재
      · 위급은 fixed+119 · 모호 '광주'는 두 지역. 이벤트 도착 시각으로 SSE 버퍼링 여부도 본다.
판정은 백엔드 validator 를 그대로 다시 돌린다 (느슨한 별도 기준을 만들지 않는다).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from agent.validator import FORBIDDEN, validate  # noqa: E402


def stream(client: httpx.Client, base: str, sc: dict) -> dict:
    params = {"message": sc["message"], "mode": sc["mode"]}
    if sc.get("as_of"):
        params["as_of"] = sc["as_of"]
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
    t = {n: round(ts, 2) for ts, n, _ in reversed(evs)}  # 이벤트별 첫 도착 시각
    vals = [d for _, n, d in evs if n == "validation"]
    info = {"src": src, "levels": [j["level"] for j in js], "validations": [(d["attempt"], d["ok"]) for d in vals],
            "llm_errors": f["meta"].get("llm_errors", []), "llm": f["meta"].get("llm"),
            "t_judgement": t.get("judgement"), "t_quick": t.get("quick"), "t_first_token": t.get("token"),
            "t_final": t.get("final"),
            "n_events": len(names)}
    return problems, info


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
