"""Before/After 측정 — 같은 질문을 두 커밋의 코드로 돌려 같은 기준으로 센다. 측정 대상 코드는 고치지 않는다.

    git worktree add ../baseline_work/aec2694 aec2694          # Before 를 저장소 밖에 꺼낸다
    .venv/bin/python eval/compare_before_after.py dup    --tree ../baseline_work/aec2694 --tree .
    .venv/bin/python eval/compare_before_after.py faults --tree ../baseline_work/aec2694 --tree .
    .venv/bin/python eval/compare_before_after.py tests  --tree ../baseline_work/aec2694 --tree .
    .venv/bin/python eval/compare_before_after.py timing --base http://localhost:8080/api --n 3   # qwen, 실행 중인 서버
    .venv/bin/python eval/compare_before_after.py eval   --base http://localhost:8110 --label before

방식
  dup    : 질문 세트(DUP_QUESTIONS)를 각 커밋의 그래프로 프로세스 안에서 돌린다(FakeLLM + replay — 답변 = 그 커밋의 템플릿).
           각 답변에 인용된 행동요령 근거([G#]) 문장끼리 짝을 지어 이 저장소의 judge.similarity(글자 2-gram Dice) ≥ 0.4 를 센다.
  faults : 외부 API 장애 5종(urlopen 경계만 가짜, eval/faults.py 와 같은 모양) + MCP 중단을 각 커밋에 주입하고
           답변에서 그 장애 때문에 달라지는 사용자 문구('정보 없음' 줄)를 모아 서로 다른 문구 수를 센다.
  tests  : 각 커밋의 sh/test.sh (같은 가상환경 파이썬, requirements 동일 확인) — 실행 수·skip.
  timing : SSE 로 사용자에게 보이는 첫 내용(판정 카드 judgement 등)·첫 답변 글(After quick, Before 첫 token)·final 도착 시각.
  eval   : eval/dataset.jsonl 을 /chat 으로 돌려 run_eval.score 로 채점(커밋에 없는 기능의 지표·항목은 해당 없음으로 뺀다).
측정 대상 커밋의 코드는 각자의 하위 프로세스에서만 import 한다(모듈이 섞이지 않게). 채점·유사도 함수는 이 저장소(After) 것을 쓴다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# 중복 측정 질문 (폭염·호우·태풍·지진·한파 × Before 에도 있는 광주 두 지역 × 표현 2). 기준 시각은 특보 원본으로 고른 것:
#   폭염 08-24 12:00 경기 광주 폭염경보 발효, 호우 08-27 16:00 광주광역시 호우주의보 발효, 나머지 10-19 12:00 특보 없음
DUP_ASOF = {"폭염": "2026-08-24 12:00", "호우": "2026-08-27 16:00", "태풍": "2026-10-19 12:00",
            "지진": "2026-10-19 12:00", "한파": "2026-10-19 12:00"}
DUP_REGIONS = ["경기도 광주", "광주광역시"]
DUP_FORMS = ["{r} {h} 행동요령 알려줘", "{r} {h} 때 어떻게 해야 해?"]
DUP_QUESTIONS = [(f.format(r=r, h=h), DUP_ASOF[h]) for h in DUP_ASOF for r in DUP_REGIONS for f in DUP_FORMS]

FAULT_QUERY = "경기도 광주 강풍 상황 알려줘"
API_FAULTS = ("wrong_key", "result_99", "truncated_json", "timeout", "rate_limited")
CITE = re.compile(r"\[([EMGS]\d+)\]")

# 첫 정보 측정용 대표 시연 질문 (두 커밋 모두에 있는 기능만)
TIMING_QUESTIONS = [
    ("경기도 광주 폭염특보 났는데 지금 나가도 돼?", "2026-08-24 12:00"),
    ("광주 호우 어때?", "2026-08-27 17:00"),
    ("경기도 광주 열대야 어때?", "2026-08-24 17:00"),
    ("경기도 광주 강풍 상황 알려줘", "2026-10-18 16:30"),
    ("광주광역시 날씨 위험해?", "2026-10-19 12:00"),
    ("경기도 광주 폭염 어때?", "2026-06-01 12:00"),
    ("광주광역시 호우주의보 행동요령 알려줘", "2026-08-27 16:00"),
]


# ───────── 하위 프로세스 (측정 대상 커밋의 코드만 import) ─────────

def _worker_paths(tree: Path) -> None:
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() not in (HERE, ROOT)]
    for p in (tree, tree / "backend", tree / "mcp_server"):
        sys.path.insert(0, str(p))


async def _run(graph, query: str, mode: str, as_of: str) -> dict:
    st = await graph.ainvoke({"query": query, "mode": mode, "as_of": as_of},
                             config={"configurable": {"emit": lambda e: None}})
    return st["final"]


def _graph(gw):
    from agent.deps import Deps, Settings
    from agent.graph import build_graph
    from agent.llm import FakeLLM
    return build_graph(Deps(mcp=gw, llm=FakeLLM(), settings=Settings(data_mode="replay", max_rewrites=1)))


def worker_dup(tree: Path) -> dict:
    import server as mcp_module
    from agent.mcp_client import MCPGateway

    async def go():
        gw = MCPGateway(mcp_module.mcp)
        await gw.start()
        try:
            g, out = _graph(gw), []
            for q, as_of in DUP_QUESTIONS:
                f = await _run(g, q, "replay", as_of)
                cited = set(CITE.findall(f["answer"]))
                guides = [e["text"] for j in f.get("judgements") or [] for e in j["evidence"]
                          if e["kind"] == "guide" and e["id"] in cited]
                out.append({"q": q, "as_of": as_of, "route": f["route"], "guides": guides})
            return out
        finally:
            await gw.stop()
    return {"rows": asyncio.run(go())}


def worker_faults(tree: Path) -> dict:
    """eval/faults.py 와 같은 응답 모양을 urlopen 경계에서 돌려준다(측정 대상 코드에 장애 모듈이 없어도 쓸 수 있게 여기 둔다)."""
    import io
    import socket
    import tempfile
    import urllib.error
    import urllib.request
    from contextlib import ExitStack
    from unittest import mock

    import capture
    import server as mcp_module
    from agent.mcp_client import MCPGateway

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def body(kind, weather):
        def hdr(code, m):
            h = {"resultCode": code, "resultMsg": m}
            return {"response": {"header": h}} if weather else {"header": h, "body": None}
        return {"result_99": lambda: json.dumps(hdr("99", "UNKNOWN_ERROR")).encode(),
                "rate_limited": lambda: json.dumps(hdr("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR")).encode(),
                "wrong_key": lambda: json.dumps(hdr("30", "SERVICE KEY IS NOT REGISTERED ERROR")).encode(),
                "truncated_json": lambda: (b'{"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL' if weather
                                           else b'{"header": {"resultCode": "00"')}[kind]()

    def fake_urlopen(kind):
        def _open(req, timeout=0):
            url = req.full_url
            weather = "WthrWrnInfoService" in url
            if kind == "timeout":
                raise urllib.error.URLError(socket.timeout("timed out"))
            if kind == "wrong_key" and weather:
                b = json.dumps({"OpenAPI_ServiceResponse": {"cmmMsgHeader": {
                    "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "returnReasonCode": "30"}}}).encode()
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b))
            return Resp(body(kind, weather))
        return _open

    class Down:
        health = {"last_ok_at": None, "last_fail_at": None, "last_cause": None}

        async def start(self):
            return None

        async def stop(self):
            return None

        async def tool(self, name, args):
            raise RuntimeError("Client failed to connect: All connection attempts failed")

        async def place(self, query):
            return await self.tool("resolve_place", {})

        async def prompt(self, name, args):
            return await self.tool(name, args)

    keys = {"WEATHER_ALERTS_SERVICE_KEY": "FAKEKEY0001", "DSSP_SERVICE_KEY": "FAKEKEY0002",
            "DSSP_MESSAGE_SERVICE_KEY": "FAKEKEY0002", "DSSP_SHELTER_SERVICE_KEY": "FAKEKEY0003"}

    async def one(gw):
        await gw.start()
        try:
            try:
                f = await _run(_graph(gw), FAULT_QUERY, "live", "")
            except Exception as e:  # noqa: BLE001 — 그 커밋이 요청 전체를 실패시키면 그대로 기록
                return {"error": f"{type(e).__name__}"}
            return {"route": f["route"], "answer": f["answer"],
                    "levels": [j["level"] for j in f.get("judgements") or []]}
        finally:
            await gw.stop()

    out = {}
    for kind in API_FAULTS:
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as st:
            st.enter_context(mock.patch.object(capture, "FIXTURES", Path(tmp)))
            st.enter_context(mock.patch.object(capture, "COUNTER", Path(tmp) / "_c.json"))
            st.enter_context(mock.patch.object(capture, "ROOT", Path(tmp)))
            st.enter_context(mock.patch.object(capture, "load_env", lambda *a, **k: dict(keys)))
            st.enter_context(mock.patch.object(urllib.request, "urlopen", fake_urlopen(kind)))
            try:
                import failures  # After 에만 있다 — 프로세스 내 소스 상태 기록을 비운다
                st.enter_context(mock.patch.dict(failures.HEALTH, clear=True))
            except ImportError:
                pass
            out[kind] = asyncio.run(one(MCPGateway(mcp_module.mcp)))
    out["mcp_down"] = asyncio.run(one(Down()))
    return out


# ───────── 상위 프로세스 (채점은 이 저장소 기준) ─────────

def run_worker(kind: str, tree: Path) -> dict:
    py = ROOT / ".venv" / "bin" / "python"
    env = {**os.environ, "LLM_BACKEND": "fake", "DATA_MODE": "replay", "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([str(py), str(Path(__file__).resolve()), "_worker", kind, str(tree)], cwd=tree, env=env,
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise SystemExit(f"worker {kind} @ {tree} 실패:\n{r.stderr[-2000:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def commit_of(tree: Path) -> str:
    return subprocess.run(["git", "-C", str(tree), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()


def cmd_dup(trees: list[Path]) -> dict:
    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(ROOT))
    from agent.judge import similarity
    from config.catalog import RULES
    th = RULES["guide_groups"]["similar_threshold"]
    res = {}
    for t in trees:
        rows = run_worker("dup", t)["rows"]
        n_sent = n_dup_sent = n_ans_dup = n_with_guides = 0
        detail = []
        for r in rows:
            g = r["guides"]
            pairs = [(i, k) for i in range(len(g)) for k in range(i + 1, len(g)) if similarity(g[i], g[k]) >= th]
            dup_idx = {x for p in pairs for x in p}
            n_sent += len(g)
            n_dup_sent += len(dup_idx)
            n_with_guides += bool(g)
            n_ans_dup += bool(pairs)
            detail.append({"q": r["q"], "guides": len(g), "dup_pairs": len(pairs),
                           "examples": [[g[i][:40], g[k][:40]] for i, k in pairs[:2]]})
        res[commit_of(t)] = {"questions": len(rows), "answers_with_guides": n_with_guides, "guide_sentences": n_sent,
                             "dup_sentences": n_dup_sent, "dup_sentence_ratio": round(n_dup_sent / n_sent, 3) if n_sent else None,
                             "answers_with_dup": n_ans_dup,
                             "answer_dup_ratio": round(n_ans_dup / n_with_guides, 3) if n_with_guides else None,
                             "threshold": th, "detail": detail}
    return res


NO_INFO_LINE = re.compile(r"[^\n]*정보 없음[^\n]*")


def cmd_faults(trees: list[Path]) -> dict:
    res = {}
    for t in trees:
        out = run_worker("faults", t)
        phrases = {}
        for kind, r in out.items():
            if "error" in r:
                phrases[kind] = f"(답변 없음 — 요청 실패 {r['error']})"
            else:
                phrases[kind] = " / ".join(dict.fromkeys(NO_INFO_LINE.findall(r["answer"])))
        res[commit_of(t)] = {"faults": list(out), "distinct_phrases": len(set(phrases.values())),
                             "phrases": phrases, "levels": {k: r.get("levels") for k, r in out.items()}}
    return res


def cmd_tests(trees: list[Path]) -> dict:
    py = ROOT / ".venv" / "bin" / "python"
    res = {}
    for t in trees:
        r = subprocess.run(["sh", "sh/test.sh"], cwd=t, env={**os.environ, "PY": str(py), "PYTHONDONTWRITEBYTECODE": "1"},
                           capture_output=True, text=True, timeout=900)
        ran = [int(x) for x in re.findall(r"^Ran (\d+) tests", r.stderr + r.stdout, re.M)]
        skip = [int(x) for x in re.findall(r"^OK \(skipped=(\d+)\)", r.stderr + r.stdout, re.M)]
        res[commit_of(t)] = {"mcp": ran[0] if ran else None, "backend": ran[1] if len(ran) > 1 else None,
                             "skipped": sum(skip), "ok": r.returncode == 0, "total": sum(ran)}
    return res


def cmd_timing(base: str, n: int) -> dict:
    import httpx
    first_names = ("quick", "evidence", "judgement", "token", "final")  # 화면에 내용을 그리는 이벤트 (진행 표시 제외)
    rows = []
    with httpx.Client(timeout=900) as c:
        for q, as_of in TIMING_QUESTIONS:
            runs = []
            for _ in range(n):
                t0 = time.perf_counter()
                first = final = first_name = text = None
                name = None
                with c.stream("GET", f"{base}/chat/stream", params={"message": q, "mode": "replay", "as_of": as_of}) as r:
                    for line in r.iter_lines():
                        if line.startswith("event:"):
                            name = line[6:].strip()
                        elif line.startswith("data:"):
                            ts = time.perf_counter() - t0
                            if first is None and name in first_names:
                                first, first_name = ts, name
                            if text is None and name in ("quick", "token"):  # 답변 글이 처음 보이는 시각
                                text = ts
                            if name == "final":
                                final = ts
                runs.append({"first": round(first, 2), "first_event": first_name, "first_text": round(text or final, 2),
                             "final": round(final, 2)})
            rows.append({"q": q, "runs": runs, **{f"{k}_median": statistics.median(x[k] for x in runs)
                                                  for k in ("first", "first_text", "final")}})

    def agg(k):
        xs = [r[f"{k}_median"] for r in rows]
        return {"median": statistics.median(xs), "min": min(xs), "max": max(xs)}
    return {"base": base, "n": n, "rows": rows, "first": agg("first"), "first_text": agg("first_text"), "final": agg("final")}


def cmd_eval(base: str, metrics: list[str] | None, skip_ids: set[str], own_grounded: bool = False) -> dict:
    """own_grounded: grounded 를 이 저장소 검증기가 아니라 그 커밋 응답의 validation.ok(그 커밋 자신의 검증기)로 본다 —
    나중에 생긴 검증 규칙으로 이전 커밋을 채점하지 않기 위해."""
    sys.path.insert(0, str(HERE))
    import httpx
    import run_eval
    ms = metrics or run_eval.METRICS
    if own_grounded:  # 이 저장소 검증기는 그 커밋의 근거 모양(필드)을 모를 수 있다 — 채점에서 빼고 그 커밋의 validation.ok 를 쓴다
        run_eval.validate = lambda answer, judgements: {"ok": True, "violations": []}
    total, n, skipped, fails = {m: 0 for m in ms}, 0, [], {}
    with httpx.Client(timeout=900) as c:
        for ex in run_eval.load():
            if ex.get("fault") or ex["id"] in skip_ids:
                skipped.append(ex["id"])
                continue
            out = run_eval.ask(c, base, ex)
            s = run_eval.score(out, ex["expect"])
            if own_grounded:
                s["grounded"] = int(bool((out.get("validation") or {}).get("ok", True)))
            n += 1
            for m in ms:
                total[m] += s[m]
            bad = [m for m in ms if not s[m]]
            if bad:
                fails[ex["id"]] = {"metrics": bad, "got": {j["region_id"]: j["level"] for j in out["judgements"]},
                                   "route": out["route"], "src": out["answer_source"]}
    return {"base": base, "n": n, "skipped": skipped, "metrics": {m: f"{total[m]}/{n}" for m in ms}, "fails": fails,
            "all_pass": all(total[m] == n for m in ms)}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_worker":
        kind, tree = sys.argv[2], Path(sys.argv[3]).resolve()
        _worker_paths(tree)
        import logging
        logging.disable(logging.CRITICAL)
        out = {"dup": worker_dup, "faults": worker_faults}[kind](tree)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dup", "faults", "tests", "timing", "eval"])
    ap.add_argument("--tree", action="append", type=Path, help="측정할 커밋의 작업 트리 (여러 번)")
    ap.add_argument("--base", help="timing/eval: 실행 중인 백엔드 (도커는 http://localhost:8080/api)")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--metrics", nargs="*", help="eval: 채점할 지표 (기본: 전부)")
    ap.add_argument("--skip", nargs="*", default=[], help="eval: 해당 없음으로 뺄 항목 id")
    ap.add_argument("--own-grounded", action="store_true", help="eval: grounded 를 그 커밋 자신의 검증 결과로")
    ap.add_argument("--out", type=Path, help="결과 JSON 저장 경로 (저장소 밖 권장)")
    a = ap.parse_args()
    trees = [t.resolve() for t in (a.tree or [])]
    res = {"dup": lambda: cmd_dup(trees), "faults": lambda: cmd_faults(trees), "tests": lambda: cmd_tests(trees),
           "timing": lambda: cmd_timing(a.base, a.n), "eval": lambda: cmd_eval(a.base, a.metrics, set(a.skip), a.own_grounded)}[a.cmd]()
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
