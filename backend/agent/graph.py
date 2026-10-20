"""LangGraph 그래프 — 실습(all_total)의 노드 흐름에 재난 도메인 노드를 더한 구조.

route ─┬─ emergency / out_of_scope ──────────────→ fixed_response ─→ END   (LLM 미사용)
       └─ disaster → parse_intent(LLM+규칙) → gate(코드) ─┬─ ask_region ─→ END
                                                          └─ plan(LLM+제약) → research(병렬+재시도)
                                                             → judge(코드) → write(LLM) → validate(코드)
                                                                 ├ ok → finalize
                                                                 ├ 위반 & 재작성 가능 → write
                                                                 └ 위반 → fallback(템플릿) → finalize

역할 분담: 판단(route/gate/judge/validate)은 코드, 이해·계획·설명(parse_intent/plan/write)은 LLM.
LLM 이 실패하거나 엉뚱해도 규칙/템플릿으로 떨어진다 (안전 우선).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langchain_core.runnables import RunnableConfig

from config.catalog import msg, prompt

from . import compose, judge, rules
from .deps import Deps
from .llm import LLMError, extract_json
from .retry import with_retry
from .validator import validate, validate_links

ALLOWED_INTENTS = {"status", "go_out", "guide"}
ALLOWED_STEPS = ["alerts", "messages", "guide"]


class State(TypedDict, total=False):
    query: str
    mode: str
    as_of: str
    history_region_id: str
    browser_region_id: str
    run_id: str
    route: str
    intent: str
    hazards: list
    hazard_source: str
    place: str
    location: dict
    regions: list
    region_names: dict
    plan: dict
    alerts: dict
    messages: dict
    guides: dict
    judgements: list
    answer: str
    answer_source: str
    validation: dict
    feedback: list
    rewrites: int
    timings: dict
    llm_errors: list
    final: dict


def _failed_alert(rid: str, name: str, mode: str, why: str) -> dict:
    return {"region_id": rid, "region": name, "mode": mode, "as_of": "-", "zones": [],
            "source": {"name": "기상특보", "status": "failed", "detail": why, "window": None},
            "alerts": [], "pending": [], "recently_released": [],
            "statement": msg("mcp.alert_unreachable")}


def _failed_messages(rid: str, why: str) -> dict:
    return {"region_id": rid, "source": {"name": "재난문자", "status": "failed", "detail": why}, "messages": []}


def emitter(config: RunnableConfig):
    """이벤트 sink. config["configurable"]["emit"](dict) — 서버는 큐로, 테스트는 리스트로 받는다.
    (LangGraph 의 get_stream_writer 는 Python<3.11 async 에서 컨텍스트가 전파되지 않아 쓰지 않는다.)"""
    return (config or {}).get("configurable", {}).get("emit") or (lambda _e: None)


def build_graph(deps: Deps):
    hazard_names: list[str] = []

    async def allowed_hazards() -> list[str]:
        if not hazard_names:
            r = await deps.mcp.tool("list_hazards", {})
            hazard_names.extend(h["name"] for h in r["hazards"])
        return hazard_names

    def timed(name: str, fn):
        async def wrapper(state: State, config: RunnableConfig):
            writer = emitter(config)
            t0 = time.perf_counter()
            writer({"event": "step", "node": name, "status": "start"})
            out = await fn(state, writer)
            ms = int((time.perf_counter() - t0) * 1000)
            writer({"event": "step", "node": name, "status": "done", "ms": ms})
            out["timings"] = {**state.get("timings", {}), name: ms}
            return out
        return wrapper

    # ── route (코드) ──
    async def route_node(state: State, writer):
        r = rules.route(state["query"])
        writer({"event": "route", "route": r})
        return {"route": r}

    async def fixed_response(state: State, writer):
        text = rules.EMERGENCY_TEXT if state["route"] == "emergency" else rules.OUT_OF_SCOPE_TEXT
        return {"answer": text, "answer_source": "fixed", "judgements": []}

    # ── parse_intent (LLM + 규칙) ──
    async def parse_intent(state: State, writer):
        q = state["query"]
        names = await allowed_hazards()
        rule_h = [h for h in rules.parse_hazards(q)["hazards"] if h in names]
        rule_i = rules.detect_intent(q)
        errors = list(state.get("llm_errors", []))
        intent, hazards, source, place = rule_i, list(rule_h), "rules", ""
        try:
            raw = await deps.llm.complete(
                prompt("intent", hazard_names=", ".join(f'"{n}"' for n in names)),
                q, json_mode=True, task="intent", payload={"text": q, "rule_hazards": rule_h, "rule_intent": rule_i})
            obj = extract_json(raw)
            if obj.get("intent") in ALLOWED_INTENTS:
                intent = obj["intent"]
            llm_h = [h for h in (obj.get("hazards") or []) if h in names]
            hazards = list(dict.fromkeys(llm_h + rule_h))  # recall 우선: 합집합
            place = rules.place_candidate(obj.get("place"), q)  # 원문에 있는 장소 표현만 (코드 검사)
            source = "llm+rules"
        except (LLMError, ValueError, TypeError) as e:
            errors.append(f"parse_intent: {type(e).__name__}: {e}")
        writer({"event": "intent", "intent": intent, "hazards": hazards, "hazard_source": source})
        return {"intent": intent, "hazards": hazards, "hazard_source": source, "place": place, "llm_errors": errors}

    # ── gate (코드) ──
    async def gate(state: State, writer):
        loc = await with_retry(lambda: deps.mcp.tool("resolve_location", {
            "text": state["query"], "history_region_id": state.get("history_region_id") or None,
            "browser_region_id": state.get("browser_region_id") or None}))
        names = {c["id"]: c["name"] for c in loc["candidates"]}
        pid, links = None, None
        if state.get("place"):
            # 카카오 장소 → 행정구역은 보조 신호. 명시 지역을 덮지 않고, 추정과 어긋나면 둘 다 보여준다 (ADR-011)
            try:
                p = await deps.mcp.place(state["place"])
            except Exception as e:  # noqa: BLE001 — 장소 조회 실패는 지역 해석에 영향 없음
                p = {"status": "failed", "detail": type(e).__name__}
            pid = p.get("region_id") if p.get("status") == "ok" else None
            loc = {**loc, "place": {"status": p.get("status"), "region_id": pid, "result_count": p.get("result_count", 0)}}
            if pid:
                names.setdefault(pid, p["region_display"])
                links = {"links": p["links"], "notice": p.get("notice")}
                kakao_cand = {"id": pid, "name": p["region_display"], "score": 0, "reasons": ["카카오 장소 검색 1위의 행정구역"]}
                explicit = any(r.startswith("명시 표현") for c in loc["candidates"] for r in c["reasons"])
                if loc["status"] == "none":
                    loc.update(status="assumed", chosen=pid, candidates=[kakao_cand])
                elif not explicit and loc["status"] != "ambiguous" and loc["chosen"] != pid:
                    # 이력·브라우저 위치로 정한 지역과 장소의 지역이 다르면 추측하지 않고 둘 다 보여준다
                    loc.update(status="ambiguous", chosen=None,
                               candidates=[c for c in loc["candidates"] if c["id"] != pid] + [kakao_cand])
        if loc["status"] == "none":
            regions = []
        elif loc["status"] == "ambiguous":
            regions = [c["id"] for c in loc["candidates"]]  # 되묻지 않고 둘 다 조회
        else:
            regions = [loc["chosen"]]
        writer({"event": "location", **loc, "regions": regions})
        if links and pid in regions:
            chips = validate_links(links["links"], links["notice"], names[pid])
            if chips:  # 링크는 답변 본문이 아니라 칩으로만, 상태(트레이스)에 넣지 않고 이벤트로만 보낸다
                writer({"event": "links", **chips})
        return {"location": loc, "regions": regions, "region_names": names}

    async def ask_region(state: State, writer):
        return {"answer": rules.ASK_REGION_TEXT, "answer_source": "fixed", "judgements": []}

    # ── plan (LLM 제안 + 코드 제약) ──
    async def plan_node(state: State, writer):
        intent = state["intent"]
        mandatory = ["alerts"] if intent == "guide" else ["alerts", "messages"]  # 특보 조회는 생략 불가
        steps, source = list(mandatory), "rules"
        errors = list(state.get("llm_errors", []))
        try:
            raw = await deps.llm.complete(
                prompt("plan", steps=str(ALLOWED_STEPS)),
                prompt("plan", "user", query=state["query"], intent=intent, hazards=str(state.get("hazards"))),
                json_mode=True, task="plan", payload={"intent": intent})
            proposed = [s for s in (extract_json(raw).get("steps") or []) if s in ALLOWED_STEPS]
            steps = [s for s in ALLOWED_STEPS if s in set(proposed) | set(mandatory)]
            source = "llm+constraints"
        except (LLMError, ValueError, TypeError) as e:
            errors.append(f"plan: {type(e).__name__}: {e}")
            steps = [s for s in ALLOWED_STEPS if s in mandatory or s == "guide"]
        if "guide" not in steps and intent == "guide":
            steps.append("guide")
        plan = {"steps": steps, "source": source}
        writer({"event": "plan", **plan})
        return {"plan": plan, "llm_errors": errors}

    # ── research (병렬 + 재시도 + 실패 격리) ──
    async def research(state: State, writer):
        regions, steps, mode = state["regions"], state["plan"]["steps"], state.get("mode") or deps.settings.data_mode
        names = state["region_names"]
        base = {"mode": mode}
        if state.get("as_of"):
            base["as_of"] = state["as_of"]

        async def fetch(kind: str, rid: str):
            tool = "get_alert_status" if kind == "alerts" else "get_disaster_messages"
            try:
                resp = await with_retry(lambda: deps.mcp.tool(tool, {"region_id": rid, **base}))
            except Exception as e:  # noqa: BLE001 — 실패는 '정보 없음'으로 격리
                why = f"{type(e).__name__}: {e}"[:200]
                resp = _failed_alert(rid, names.get(rid, rid), mode, why) if kind == "alerts" else _failed_messages(rid, why)
            writer({"event": "source", "region_id": rid, "name": resp["source"]["name"],
                    "status": resp["source"]["status"], "count": len(resp.get("alerts", resp.get("messages", [])))})
            return kind, rid, resp

        jobs = [fetch("alerts", r) for r in regions]
        if "messages" in steps:
            jobs += [fetch("messages", r) for r in regions]
        results = await asyncio.gather(*jobs)
        alerts = {rid: resp for k, rid, resp in results if k == "alerts"}
        messages = {rid: resp for k, rid, resp in results if k == "messages"}

        # 행동요령: 특보가 알려 준 재난 + 사용자가 말한 재난 (재난문자 라벨은 신뢰도 문제로 제외, T-010)
        guides: dict[str, dict] = {}
        if "guide" in steps:
            wanted: dict[str, set] = {}
            for rid in regions:
                a = alerts[rid]
                active = {x["hazard"] for x in a["alerts"]}
                pending = {x["hazard"] for x in a["pending"]}
                for hz in dict.fromkeys(list(active) + list(pending) + list(state.get("hazards", []))):
                    wanted.setdefault(rid, set()).add((hz, "during" if hz in active else "prepare"))
            calls = {k for v in wanted.values() for k in v}

            async def guide(hz: str, ph: str):
                try:
                    r = await with_retry(lambda: deps.mcp.tool("search_action_guide", {"hazard": hz, "phase": ph}))
                except Exception as e:  # noqa: BLE001
                    r = {"hazard": hz, "available": False, "reason": f"조회 실패: {type(e).__name__}", "rows": []}
                return (hz, ph), r
            got = dict(await asyncio.gather(*[guide(h, p) for h, p in calls]))
            for rid, items in wanted.items():
                guides[rid] = {hz: got[(hz, ph)] for hz, ph in sorted(items)}
            writer({"event": "source", "name": "행동요령", "status": "ok", "count": len(calls)})
        return {"alerts": alerts, "messages": messages, "guides": guides}

    # ── judge (코드) ──
    async def judge_node(state: State, writer):
        js = [judge.judge_region(state["alerts"][rid], state["messages"].get(rid), state.get("guides", {}).get(rid, {}))
              for rid in state["regions"]]
        writer({"event": "judgement", "judgements": js})
        return {"judgements": js}

    # ── write (LLM) ──
    async def write_node(state: State, writer):
        js, intent = state["judgements"], state["intent"]
        errors = list(state.get("llm_errors", []))
        rules_text = await deps.mcp.prompt("disaster_briefing_prompt", {
            "region": ", ".join(j["region"] for j in js), "hazards": ", ".join(state.get("hazards", [])) or "미지정",
            "question": state["query"]})
        facts = [{"region": j["region"], "level": j["level_label"], "as_of": j["as_of"], "zones": j["zones"],
                  "note": j["note"], "gaps": j["gaps"],
                  "evidence": [{k: e[k] for k in ("id", "kind", "text") if k in e} for e in j["evidence"]]} for j in js]
        # 작은 모델이 형식 예시의 ID를 그대로 베끼거나 gap 문구를 빠뜨리지 않도록 데이터에서 지시를 만든다 (T-021)
        # 근거 ID 는 지역마다 E1부터 — 지역별로 인용 가능 ID 와 반드시 인용할 특보 ID 를 준다 (validator 지역별 대조)
        region_ids = []
        for j in js:
            ids = [e["id"] for e in j["evidence"]]
            region_ids.append(prompt("write", "region_ids", region=j["region"],
                                     ids=", ".join(f"[{i}]" for i in ids) if ids else prompt("write", "ids_none")))
            alert_ids = [e["id"] for e in j["evidence"] if e["kind"] in ("alert", "pending")]
            if alert_ids:
                region_ids.append(prompt("write", "must_cite", region=j["region"],
                                         alert_ids=", ".join(f"[{i}]" for i in alert_ids)))
        user = prompt("write", "user", facts=json.dumps(facts, ensure_ascii=False), region_ids="\n".join(region_ids))
        must = []
        for j in js:
            # 템플릿(compose)과 같은 표준 문구를 요구한다 — 문구 정의는 prompts/messages.yaml 한 곳
            sentences = []
            if j["level"] == "none":
                sentences.append(msg("answer.none", as_of=j["as_of"], zones=", ".join(j["zones"])))
            if j["level"] == "unknown":
                sentences.append(msg("answer.unknown"))
            sentences += [msg("answer.gap", source=g["source"], status=g["status"])
                          for g in j["gaps"] if g["status"] in ("failed", "no_coverage")]
            must += [prompt("write", "must", region=j["region"], sentence=s) for s in sentences]
        if must:
            user += "\n" + prompt("write", "must_header") + "\n" + "\n".join(f"* {m}" for m in must)
        if state.get("feedback"):
            user += "\n\n" + prompt("rewrite", "user", violations="\n".join(f"- {v}" for v in state["feedback"]))
        try:
            text = await deps.llm.complete(rules_text, user, task="write", payload={"judgements": js, "intent": intent})
            source = "llm" if deps.llm.name != "fake" else "fake"
        except Exception as e:  # noqa: BLE001
            errors.append(f"write: {type(e).__name__}: {e}")
            text, source = compose.render_answer(js, intent), "template_llm_error"
        return {"answer": text, "answer_source": source, "llm_errors": errors}

    # ── validate (코드) ──
    async def validate_node(state: State, writer):
        v = validate(state["answer"], state["judgements"])
        writer({"event": "validation", **v, "attempt": state.get("rewrites", 0) + 1})
        out: dict[str, Any] = {"validation": v}
        if not v["ok"]:
            out["feedback"] = v["violations"]
            out["rewrites"] = state.get("rewrites", 0) + 1
        return out

    def after_validate(state: State) -> str:
        if state["validation"]["ok"]:
            return "finalize"
        return "write" if state.get("rewrites", 0) <= deps.settings.max_rewrites else "fallback"

    async def fallback(state: State, writer):
        text = compose.render_answer(state["judgements"], state["intent"])
        v = validate(text, state["judgements"])
        writer({"event": "validation", **v, "attempt": "template"})
        return {"answer": text, "answer_source": "template_fallback", "validation": v}

    # ── finalize ──
    async def finalize(state: State, writer):
        js = state.get("judgements", [])
        mode = state.get("mode") or deps.settings.data_mode
        final = {
            "route": state["route"] if state.get("regions") or state["route"] != "disaster" else "ask_region",
            "answer": state["answer"], "answer_source": state.get("answer_source"),
            "intent": state.get("intent"), "hazards": state.get("hazards", []),
            "location": state.get("location"), "plan": state.get("plan"), "judgements": js,
            "validation": state.get("validation"),
            "meta": {"run_id": state.get("run_id") or uuid.uuid4().hex[:12], "mode": mode,
                     "as_of": js[0]["as_of"] if js else state.get("as_of"), "llm": deps.llm.name,
                     "timings_ms": state.get("timings", {}), "llm_errors": state.get("llm_errors", []),
                     "disclaimer": compose.DISCLAIMER},
        }
        return {"final": final}

    g = StateGraph(State)
    for name, fn in [("route", route_node), ("fixed_response", fixed_response), ("parse_intent", parse_intent),
                     ("gate", gate), ("ask_region", ask_region), ("plan", plan_node), ("research", research),
                     ("judge", judge_node), ("write", write_node), ("validate", validate_node),
                     ("fallback", fallback), ("finalize", finalize)]:
        g.add_node(name, timed(name, fn))
    g.add_edge(START, "route")
    g.add_conditional_edges("route", lambda s: "parse_intent" if s["route"] == "disaster" else "fixed_response",
                            ["parse_intent", "fixed_response"])
    g.add_edge("fixed_response", "finalize")
    g.add_edge("parse_intent", "gate")
    g.add_conditional_edges("gate", lambda s: "plan" if s["regions"] else "ask_region", ["plan", "ask_region"])
    g.add_edge("ask_region", "finalize")
    g.add_edge("plan", "research")
    g.add_edge("research", "judge")
    g.add_edge("judge", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", after_validate, ["finalize", "write", "fallback"])
    g.add_edge("fallback", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
