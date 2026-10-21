"""평가 세트(eval/dataset.jsonl)를 백엔드 /chat 에 돌려 채점한다. 기대값은 커밋된 fixture 의 원본 특보 이벤트에서 사람이 정했다(basis).

    .venv/bin/python eval/run_eval.py                       # 로컬 채점만 (LangSmith 미사용)
    .venv/bin/python eval/run_eval.py --langsmith           # LangSmith 데이터셋·실험으로 업로드
    --base http://localhost:8100   (기본: 도커 nginx http://localhost:8080/api)

지표 (1=통과)
  route · regions · level        : 분기·조회 지역·지역별 위험 수준이 기대와 같은가
  no_missed_risk                 : 특보가 있는데 none/unknown 으로 답하지 않았는가 (놓침, recall 우선)
  no_forbidden                   : 금지 표현 0건
  grounded                       : 백엔드 검증기 기준 통과 (모든 항목 인용, 인용 id 실재, 정보 없음·면책 문구)
  emergency_fixed                : 위급 표현은 LLM 없이 고정 119/112 응답
  cause                          : 장애 주입 항목(fault)에서 기대한 원인 코드가 meta.failures 에 있고 원인별 문구가 답변에 있는가
  not_in_data                    : 공식 데이터에 없는 행동요령을 "해당 항목이 없습니다"로 밝혔는가 (지어내지 않음)
  shelter                        : 쉼터 기대값(shelter: true/false)과 같은가 — true 면 [S#] 근거가 있고 운영 여부 미확인 안내가 답변에,
                                   false 면 쉼터 근거·안내가 없어야 한다 (호우·태풍에는 붙지 않음)
  structured                     : 기대한 행동요령 묶음(지금 할 일 / 미리 대비할 일 / 하지 말 것) 제목이 답변에 있는가 + 특보구역 코드 비노출
                                   + groups_absent 묶음은 없어야 하고, borrowed 면 다른 유형 행동요령 사용 안내가 답변에
  no_duplicate                   : 중복률 0 — 한 지역의 행동요령 근거 중 유사 쌍(judge.similar, rules.yaml similar_threshold)이 없다
  group_ok                       : 묶음 정합성 — '하지 말 것'에 든 행이 모두 금지 문장(judge.is_prohibition, ADR-020)인가
  must_action                    : 필수 행동 포함 — must_actions 단어를 모두 포함한 '증상이 있으면' 근거가 답변에 인용됐는가
  hazard_scope                   : 질문에 없는 유형이 끼지 않았는가 (hazards_only 항목, T-039) — 행동요령·쉼터 근거의 유형이 허용 목록
                                   (발효 중·예고 특보의 유형) 안에 있고, 그 밖의 유형 이름이 근거 원문에 없는데 답변에 나오지 않는다
검증기·판정 코드는 백엔드 것을 그대로 쓴다 — 평가용으로 느슨한 기준을 따로 만들지 않는다.

장애 주입 항목(`fault` 필드)은 HTTP 가 아니라 프로세스 내에서 같은 그래프·MCP 서버로 돈다 (eval/faults.py, 네트워크 경계만 바꿈).
LLM 은 환경변수(LLM_BACKEND 등, 기본 fake)로 만든다 — qwen 평가 때는 LLM_BACKEND=openai_compat 를 함께 준다.
"""
from __future__ import annotations

import argparse
import hashlib
from collections import Counter
import json
import re
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))
from agent.judge import is_prohibition, similar  # noqa: E402
from agent.rules import HAZARD_SYNONYMS  # noqa: E402
from agent.validator import CITE, FORBIDDEN, validate  # noqa: E402
from config.catalog import msg  # noqa: E402

DATASET = HERE / "dataset.jsonl"
RISKY = {"upcoming", "advisory", "warning", "severe"}
ZONE_CODE = re.compile(r"\bL\d{7}\b")
METRICS = ["route", "regions", "level", "no_missed_risk", "no_forbidden", "grounded", "emergency_fixed", "cause",
           "not_in_data", "structured", "shelter", "no_duplicate", "group_ok", "must_action", "hazard_scope"]


def load() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def ask_fault(ex: dict) -> dict:
    """장애 주입 항목: 프로세스 내 MCP 서버 + 같은 그래프 (mode=live, 네트워크 경계만 가짜)."""
    import asyncio

    sys.path.insert(0, str(ROOT / "mcp_server"))
    import server as mcp_module
    from agent.deps import Deps, Settings
    from agent.graph import build_graph
    from agent.llm import make_llm
    from agent.mcp_client import MCPGateway
    from faults import DownGateway, api_fault, shelter_down_gateway

    async def go():
        gw = (DownGateway() if ex["fault"] == "mcp_down" else shelter_down_gateway() if ex["fault"] == "shelter_down"
              else MCPGateway(mcp_module.mcp))
        await gw.start()
        try:
            llm = make_llm()
            graph = build_graph(Deps(mcp=gw, llm=llm, settings=Settings.from_env()))
            # shelter_down 은 쉼터만 실패시키고 나머지는 replay 그대로 (기준 시각 사용)
            run = ({"mode": "replay", "as_of": ex["as_of"]} if ex["fault"] == "shelter_down" else {"mode": "live", "as_of": ""})
            st = await graph.ainvoke({"query": ex["message"], **run},
                                     config={"configurable": {"emit": lambda e: None}})
            return st["final"], llm.name
        finally:
            await gw.stop()
    if ex["fault"] in ("mcp_down", "shelter_down"):
        f, name = asyncio.run(go())
    else:
        with api_fault(ex["fault"]):
            f, name = asyncio.run(go())
    return {"route": f["route"], "answer": f["answer"], "answer_source": f["answer_source"],
            "judgements": f.get("judgements") or [], "llm": name, "validation": f.get("validation"),
            "failures": f["meta"].get("failures", [])}


def ask(client: httpx.Client, base: str, ex: dict) -> dict:
    if ex.get("fault"):
        return ask_fault(ex)
    params = {"message": ex["message"], "mode": "replay"}
    if ex.get("as_of"):
        params["as_of"] = ex["as_of"]
    if ex.get("history_region_id"):
        params["history_region_id"] = ex["history_region_id"]
    import time
    t0 = time.perf_counter()
    f = client.get(f"{base}/chat", params=params).json()
    f["_elapsed_ms"] = (time.perf_counter() - t0) * 1000
    return {"route": f["route"], "answer": f["answer"], "answer_source": f["answer_source"],
            "judgements": f.get("judgements") or [], "llm": f["meta"]["llm"], "validation": f.get("validation"),
            "failures": f["meta"].get("failures", []), "timings_ms": f["meta"].get("timings_ms", {}),
            "llm_usage": f["meta"].get("llm_usage", []), "elapsed_ms": int(f.get("_elapsed_ms", 0))}


def score(out: dict, expect: dict) -> dict[str, int]:
    got = {j["region_id"]: j["level"] for j in out["judgements"]}
    want = expect.get("levels", {})
    s = {"route": int(out["route"] == expect["route"]),
         "regions": int(set(got) == set(want)),
         "level": int(got == want),
         "no_missed_risk": int(not any(lv in RISKY and got.get(rid) in (None, "none", "unknown")
                                       for rid, lv in want.items())),
         "no_forbidden": int(not any(w in out["answer"] for w in FORBIDDEN)),
         "grounded": int(validate(out["answer"], out["judgements"])["ok"]) if out["judgements"] else 1,
         "emergency_fixed": int(out["answer_source"] == "fixed" and "119" in out["answer"])
         if expect["route"] == "emergency" else 1,
         "cause": int(expect["cause"] in {x.get("cause") for x in out.get("failures", [])}
                      and msg(f"causes.{expect['cause']}") in out["answer"]) if expect.get("cause") else 1}
    s["not_in_data"] = int(msg("markers.not_in_data") in out["answer"]) if expect.get("not_in_data") else 1
    # 행동요령 묶음: judge 가 정한 묶음이 있고(코드), 답변에 묶음 제목이 보이는가. 특보구역 코드는 답변에 보이지 않는다
    s["structured"] = int(not ZONE_CODE.search(out["answer"]) and all(
        lb in out["answer"] and any(e.get("group_label") == lb for j in out["judgements"] for e in j["evidence"])
        for lb in expect.get("groups", [])))
    s["structured"] &= int(not any(f"▶ {lb}" in out["answer"] for lb in expect.get("groups_absent", [])))
    if expect.get("borrowed"):
        s["structured"] &= int(msg("markers.guide_borrowed") in out["answer"])
    sh_ev = [e for j in out["judgements"] for e in j["evidence"] if e["kind"] == "shelter"]
    notice = msg("markers.shelter_notice") in out["answer"]
    if "shelter" in expect:
        s["shelter"] = int(bool(sh_ev) and notice) if expect["shelter"] else int(not sh_ev and not notice)
    else:
        s["shelter"] = 1
    s["shelter"] &= int(not any(e["hazard"] in expect.get("no_shelter_for", []) for e in sh_ev))
    # 행동요령 품질 (ADR-020): 중복 0 · '하지 말 것' 정합성 · 필수 행동(증상 대처) 인용
    gs = [[e for e in j["evidence"] if e["kind"] == "guide"] for j in out["judgements"]]
    s["no_duplicate"] = int(not any(similar(a["text"], b["text"]) for g in gs for i, a in enumerate(g) for b in g[i + 1:]))
    s["group_ok"] = int(all(is_prohibition(e["text"]) for g in gs for e in g if e.get("group") == "dont"))
    cited = set(CITE.findall(out["answer"]))
    words = expect.get("must_actions")
    s["must_action"] = int(any(e["id"] in cited and e.get("group") == "symptom" and all(w in e["text"] for w in words)
                               for g in gs for e in g)) if words else 1
    s["hazard_scope"] = 1
    if "hazards_only" in expect:  # 유형 단어 없는 질문: AI가 지어낸 유형의 행동요령·문장이 없어야 한다
        allowed = set(expect["hazards_only"])
        ev = [e for j in out["judgements"] for e in j["evidence"]]
        texts = " ".join(e.get("text", "") for e in ev)
        s["hazard_scope"] = int(all(e["hazard"] in allowed for e in ev if e["kind"] in ("guide", "shelter"))
                                and not any(h in out["answer"] and h not in texts for h in set(HAZARD_SYNONYMS) - allowed))
    if expect.get("guide"):
        guide_ev = [e for j in out["judgements"] for e in j["evidence"] if e["kind"] == "guide"]
        s["level"] &= int(bool(guide_ev))
        for src in expect.get("guide_sources", []):  # 소스는 코드가 hazard_map 으로 정한다 — 기대한 소스의 행인가
            s["level"] &= int(any(src in (e.get("source") or "") for e in guide_ev))
    return s


def run_local(base: str, only: list[str] | None) -> int:
    rows, total = [], {m: 0 for m in METRICS}
    with httpx.Client(timeout=600) as c:
        for ex in load():
            if only and ex["id"] not in only:
                continue
            out = ask(c, base, ex)
            s = score(out, ex["expect"])
            rows.append((ex, out, s))
            for m in METRICS:
                total[m] += s[m]
            bad = [m for m in METRICS if not s[m]]
            got = {j["region_id"]: j["level"] for j in out["judgements"]}
            print(f"{'PASS' if not bad else 'FAIL'} {ex['id']:<32} src={out['answer_source']:<17} got={got} {'✗ ' + ','.join(bad) if bad else ''}")
    n = len(rows)
    print(f"\n{n}건 · LLM {rows[0][1]['llm'] if rows else '-'} · 답변 출처 "
          + str(dict(sorted(Counter(o["answer_source"] for _, o, _ in rows).items()))))
    timed = [o for ex, o, _ in rows if o.get("elapsed_ms") and o["answer_source"] != "fixed"]
    if timed:  # 응답 시간 (HTTP 항목 중 고정 응답 제외, final.meta.timings_ms)
        med = lambda xs: sorted(xs)[len(xs) // 2]
        nodes = sorted({k for o in timed for k in o["timings_ms"]})
        print(f"  응답 시간 중앙값({len(timed)}건): 전체 {med([o['elapsed_ms'] for o in timed]) / 1000:.1f}초 · "
              + " · ".join(f"{k} {med([o['timings_ms'].get(k, 0) for o in timed]) / 1000:.1f}초" for k in nodes))
    writes = [u for _, o, _ in rows for u in o.get("llm_usage", []) if u["task"] == "write" and u.get("prompt_tokens")]
    if writes:  # write 호출별 토큰 (K7: 답변 길이 vs 검증 반려 vs 서버 속도를 숫자로 가른다)
        med = lambda xs: sorted(xs)[len(xs) // 2]
        rew = sum(max(0, sum(u["task"] == "write" for u in o.get("llm_usage", [])) - 1) for _, o, _ in rows)
        tps = [u["completion_tokens"] / (u["ms"] / 1000) for u in writes if u.get("ms")]
        print(f"  write 호출 {len(writes)}회 · 입력 토큰 최대 {max(u['prompt_tokens'] for u in writes)} · 중앙값 "
              f"{med([u['prompt_tokens'] for u in writes])} · 출력 토큰 중앙값 {med([u['completion_tokens'] for u in writes])} · "
              f"초당 생성 중앙값 {med(tps):.1f} · 재작성 {rew}회 · 템플릿 대체 "
              f"{sum(o['answer_source'] == 'template_fallback' for _, o, _ in rows)}건")
    for m in METRICS:
        print(f"  {m:<16} {total[m]}/{n}")
    return 0 if all(total[m] == n for m in METRICS) else 1


def run_langsmith(base: str, prefix: str) -> int:
    from langsmith import Client, evaluate

    from config.settings import load_dotenv_file
    load_dotenv_file(ROOT / ".env")
    client = Client()
    data = load()
    digest = hashlib.sha256(DATASET.read_bytes()).hexdigest()[:8]
    name = f"disaster-advisor-eval-{digest}"  # 내용이 바뀌면 새 데이터셋 (예전 실험과 섞이지 않게)
    if not client.has_dataset(dataset_name=name):
        ds = client.create_dataset(name, description=f"eval/dataset.jsonl ({len(data)}건, sha {digest}). 기대값 근거는 basis.")
        client.create_examples(dataset_id=ds.id,
                               inputs=[{k: ex.get(k, "") for k in ("message", "as_of", "history_region_id", "fault")} for ex in data],
                               outputs=[{"expect": ex["expect"], "id": ex["id"], "basis": ex["basis"]} for ex in data])
    http = httpx.Client(timeout=600)

    def target(inputs: dict) -> dict:
        return ask(http, base, inputs)

    def make(metric: str):
        def ev(outputs: dict, reference_outputs: dict) -> dict:
            return {"key": metric, "score": score(outputs, reference_outputs["expect"])[metric]}
        ev.__name__ = metric
        return ev

    res = evaluate(target, data=name, evaluators=[make(m) for m in METRICS], experiment_prefix=prefix,
                   metadata={"base": base, "dataset_sha": digest}, max_concurrency=1, client=client)
    total, n = {m: 0 for m in METRICS}, 0
    for r in res:  # pandas 없이 요약
        n += 1
        for e in r["evaluation_results"]["results"]:
            total[e.key] = total.get(e.key, 0) + int(e.score or 0)
    print(f"실험: {res.experiment_name}  데이터셋: {name}  ({n}건)")
    for m in METRICS:
        print(f"  {m:<16} {total[m]}/{n}")
    return 0 if all(total[m] == n for m in METRICS) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080/api")
    ap.add_argument("--langsmith", action="store_true")
    ap.add_argument("--prefix", default="eval")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    return run_langsmith(a.base, a.prefix) if a.langsmith else run_local(a.base, a.only)


if __name__ == "__main__":
    sys.exit(main())
