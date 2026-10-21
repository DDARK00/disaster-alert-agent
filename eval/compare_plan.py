"""계획(plan) 단계 비교: LLM 계획 vs 규칙 계획 (config/rules.yaml plan_steps). 평가 세트의 disaster 항목만.

같은 질문에 대해 LLM 의도(parse_intent 와 같은 프롬프트) → LLM 계획(plan 프롬프트 + 코드 제약)을 구하고,
같은 의도로 규칙 계획을 구해 조회 단계가 같은지 본다. MCP·답변 작성은 하지 않는다(LLM 호출만, 항목당 2회).

    LLM_BACKEND=openai_compat LLM_BASE_URL=http://localhost:11434/v1 LLM_NO_THINK=1 \
        .venv/bin/python eval/compare_plan.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))
from agent import rules  # noqa: E402
from agent.graph import ALLOWED_INTENTS, ALLOWED_STEPS, rule_plan  # noqa: E402
from agent.llm import extract_json, make_llm  # noqa: E402
from config.catalog import prompt  # noqa: E402


def llm_plan(steps_raw: list, intent: str) -> list[str]:
    """graph 의 예전 plan_node 와 같은 코드 제약 (특보 필수, guide 의도면 guide 추가)."""
    mandatory = ["alerts"] if intent == "guide" else ["alerts", "messages"]
    proposed = [s for s in steps_raw if s in ALLOWED_STEPS]
    steps = [s for s in ALLOWED_STEPS if s in set(proposed) | set(mandatory)]
    if "guide" not in steps and intent == "guide":
        steps.append("guide")
    return steps


async def main() -> int:
    llm = make_llm()
    names = list(json.loads((ROOT / "data/hazard_map.json").read_text(encoding="utf-8"))["hazards"])
    data = [json.loads(x) for x in (ROOT / "eval/dataset.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    same, rows, ms = 0, [], []
    for ex in data:
        q = ex["message"]
        if rules.route(q) != "disaster" or ex["expect"]["route"] != "disaster":
            continue
        rule_h = [h for h in rules.parse_hazards(q)["hazards"] if h in names]
        rule_i = rules.detect_intent(q)
        try:
            obj = extract_json(await llm.complete(prompt("intent", hazard_names=", ".join(f'"{n}"' for n in names)), q,
                                                  json_mode=True, task="intent",
                                                  payload={"text": q, "rule_hazards": rule_h, "rule_intent": rule_i}))
            intent = obj["intent"] if obj.get("intent") in ALLOWED_INTENTS else rule_i
            hazards = list(dict.fromkeys([h for h in (obj.get("hazards") or []) if h in names] + rule_h))
        except Exception:  # noqa: BLE001
            intent, hazards = rule_i, rule_h
        t0 = time.perf_counter()
        try:
            raw = await llm.complete(prompt("plan", steps=str(ALLOWED_STEPS)),
                                     prompt("plan", "user", query=q, intent=intent, hazards=str(hazards)),
                                     json_mode=True, task="plan", payload={"intent": intent})
            got = llm_plan(extract_json(raw).get("steps") or [], intent)
        except Exception as e:  # noqa: BLE001
            got = [f"error:{type(e).__name__}"]
        ms.append(int((time.perf_counter() - t0) * 1000))
        want = rule_plan(intent)
        ok = got == want
        same += ok
        rows.append((ex["id"], intent, got, want))
        print(f"{'SAME' if ok else 'DIFF'} {ex['id']:<30} intent={intent:<7} llm={got} rule={want}")
    print(f"\n{same}/{len(rows)} 같음 · LLM {llm.name} · plan 호출 중앙값 {sorted(ms)[len(ms) // 2] if ms else 0}ms")
    print("의도별:", dict(Counter(r[1] for r in rows)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
