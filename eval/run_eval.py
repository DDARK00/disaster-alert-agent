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
검증기·판정 코드는 백엔드 것을 그대로 쓴다 — 평가용으로 느슨한 기준을 따로 만들지 않는다.

장애 주입 항목(`fault` 필드)은 HTTP 가 아니라 프로세스 내에서 같은 그래프·MCP 서버로 돈다 (eval/faults.py, 네트워크 경계만 바꿈).
LLM 은 환경변수(LLM_BACKEND 등, 기본 fake)로 만든다 — qwen 평가 때는 LLM_BACKEND=openai_compat 를 함께 준다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))
from agent.validator import FORBIDDEN, validate  # noqa: E402
from config.catalog import msg  # noqa: E402

DATASET = HERE / "dataset.jsonl"
RISKY = {"upcoming", "advisory", "warning", "severe"}
METRICS = ["route", "regions", "level", "no_missed_risk", "no_forbidden", "grounded", "emergency_fixed", "cause"]


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
    from faults import DownGateway, api_fault

    async def go():
        gw = DownGateway() if ex["fault"] == "mcp_down" else MCPGateway(mcp_module.mcp)
        await gw.start()
        try:
            llm = make_llm()
            graph = build_graph(Deps(mcp=gw, llm=llm, settings=Settings.from_env()))
            st = await graph.ainvoke({"query": ex["message"], "mode": "live", "as_of": ""},
                                     config={"configurable": {"emit": lambda e: None}})
            return st["final"], llm.name
        finally:
            await gw.stop()
    if ex["fault"] == "mcp_down":
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
    f = client.get(f"{base}/chat", params=params).json()
    return {"route": f["route"], "answer": f["answer"], "answer_source": f["answer_source"],
            "judgements": f.get("judgements") or [], "llm": f["meta"]["llm"], "validation": f.get("validation"),
            "failures": f["meta"].get("failures", [])}


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
    if expect.get("guide"):
        s["level"] &= int(any(e["kind"] == "guide" for j in out["judgements"] for e in j["evidence"]))
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
    print(f"\n{n}건 · LLM {rows[0][1]['llm'] if rows else '-'}")
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
