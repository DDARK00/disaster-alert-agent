"""LangGraph 그래프 — 실습(all_total)의 노드 흐름에 재난 도메인 노드를 더한 구조.

route ─┬─ emergency / not_in_data / out_of_scope ─→ fixed_response ─→ END   (LLM 미사용)
       └─ disaster → parse_intent(LLM+규칙) → gate(코드) ─┬─ ask_region ─→ END
                                                          ├─ unavailable (MCP 불가, 원인 문구) ─→ END
                                                          └─ plan(코드 규칙) → research(병렬+재시도, 쉼터는 코드가 붙임)
                                                             → judge(코드) → write(LLM) → validate(코드)
                                                                 ├ ok → finalize
                                                                 ├ 위반 & 재작성 가능 → write
                                                                 └ 위반 → fallback(템플릿) → finalize

역할 분담: 판단(route/gate/plan/judge/validate)은 코드, 이해·설명(parse_intent/write)은 LLM.
(plan 은 LLM 이었으나 의도만으로 정해져 규칙 표로 바꿨다 — ADR-018)
LLM 이 실패하거나 엉뚱해도 규칙/템플릿으로 떨어진다 (안전 우선).

단계별 표시 (ADR-019): judge 직후 코드가 만든 구조화 근거(evidence)와 검증을 통과한 템플릿 요약(quick)을 먼저 보낸다.
LLM 문장은 validate 를 통과한 뒤에만 final 로 나간다. 노드 시작마다 짧은 진행 문구(progress)를 보낸다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langchain_core.runnables import RunnableConfig

from config.catalog import RULES, msg, prompt, reason

from . import compose, judge, rules
from .deps import Deps
from .llm import LLMError, extract_json
from .mcp_client import mcp_cause
from .retry import with_retry
from .validator import validate, validate_links

ALLOWED_INTENTS = {"status", "go_out", "guide"}
ALLOWED_STEPS = ["alerts", "messages", "guide"]
WRITE_MAX_TOKENS = 1600
PROGRESS = {k: v for k, v in msg("progress").items() if k != "source_failed"}


def progress_failed(source: str, status: str, cause: str | None) -> dict:
    """조회 실패 진행 문구 — 원인별 문구(causes.*)를 쓴다 (ADR-014)."""
    return {"event": "progress", "stage": "source_failed", "cause": cause,
            "text": msg("progress.source_failed", source=source, reason=reason(status, cause))}


def rule_plan(intent: str) -> list[str]:
    """의도 → 조회 단계 (config/rules.yaml plan_steps, ADR-018). 특보 조회는 항상, 순서는 ALLOWED_STEPS.
    알 수 없는 의도는 status 로 본다."""
    table = RULES["plan_steps"]
    want = set(table.get(intent, table["status"])) | {"alerts"}
    return [s for s in ALLOWED_STEPS if s in want]


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
    shelters: dict
    judgements: list
    answer: str
    answer_source: str
    validation: dict
    feedback: list
    rewrites: int
    rejections: list   # AI 답변이 검증에서 반려된 사유 (시도별). 템플릿 대체 뒤에도 meta 에 남는다 (T-040)
    timings: dict
    llm_errors: list
    failures: list
    llm_usage: list
    service_down: str
    final: dict


log = logging.getLogger("backend.agent")


def _failed_alert(rid: str, name: str, mode: str, cause: str, why: str) -> dict:
    return {"region_id": rid, "region": name, "mode": mode, "as_of": "-", "zones": [],
            "source": {"name": "기상특보", "status": "failed", "cause": cause, "detail": why, "window": None},
            "alerts": [], "pending": [], "recently_released": [],
            "statement": msg("mcp.alert_unreachable", reason=reason("failed", cause))}


def _failed_messages(rid: str, cause: str, why: str) -> dict:
    return {"region_id": rid, "source": {"name": "재난문자", "status": "failed", "cause": cause, "detail": why},
            "messages": []}


def annotate_span(**meta) -> None:
    """현재 LangSmith span 에 메타데이터를 단다 (트레이싱이 꺼져 있으면 아무것도 하지 않음)."""
    try:
        from langsmith.run_helpers import get_current_run_tree
        rt = get_current_run_tree()
        if rt is not None:
            rt.add_metadata(meta)
    except Exception:  # noqa: BLE001 — 관측 실패가 답변을 막지 않는다
        pass


def _llm_failure(e: BaseException) -> dict | None:
    return {"stage": "llm", "cause": "llm_unavailable", "detail": str(e)[:120]} if isinstance(e, LLMError) else None


def emitter(config: RunnableConfig):
    """이벤트 sink. config["configurable"]["emit"](dict) — 서버는 큐로, 테스트는 리스트로 받는다.
    (LangGraph 의 get_stream_writer 는 Python<3.11 async 에서 컨텍스트가 전파되지 않아 쓰지 않는다.)"""
    return (config or {}).get("configurable", {}).get("emit") or (lambda _e: None)


def build_graph(deps: Deps):
    hazard_names: list[str] = []
    shelter_kind: dict[str, str] = {}  # 쉼터 구분이 연결된 유형 → 구분 (hazard_map shelter — 폭염·열대야·한파·지진)
    borrowed: set[str] = set()  # 다른 유형의 행동요령·쉼터를 쓰는 유형 (hazard_map guide_from — 열대야)

    async def allowed_hazards() -> list[str]:
        if not hazard_names:
            try:
                r = await deps.mcp.tool("list_hazards", {})
            except Exception:  # noqa: BLE001 — MCP 불가여도 규칙 사전의 유형 이름으로 계속 (gate 에서 원인과 함께 멈춘다)
                return list(rules.HAZARD_SYNONYMS)
            hazard_names.extend(h["name"] for h in r["hazards"])
            shelter_kind.update({h["name"]: h["shelter"] for h in r["hazards"] if h.get("shelter")})
            borrowed.update(h["name"] for h in r["hazards"] if h.get("guide_from"))
        return hazard_names

    def timed(name: str, fn):
        async def wrapper(state: State, config: RunnableConfig):
            writer = emitter(config)
            t0 = time.perf_counter()
            writer({"event": "step", "node": name, "status": "start"})
            stage = "rewrite" if name == "write" and state.get("rewrites") else name
            if stage in PROGRESS:
                writer({"event": "progress", "stage": stage, "text": PROGRESS[stage]})
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
        if state["route"] == "not_in_data":
            text = msg("fixed.not_in_data", topic=rules.not_in_data_topic(state["query"]))
        else:
            text = rules.EMERGENCY_TEXT if state["route"] == "emergency" else rules.OUT_OF_SCOPE_TEXT
        return {"answer": text, "answer_source": "fixed", "judgements": []}

    # ── parse_intent (LLM + 규칙) ──
    async def parse_intent(state: State, writer):
        q = state["query"]
        names = await allowed_hazards()
        rule_h = [h for h in rules.parse_hazards(q)["hazards"] if h in names]
        rule_i = rules.detect_intent(q)
        errors = list(state.get("llm_errors", []))
        intent, hazards, source, place, dropped = rule_i, list(rule_h), "rules", "", []
        usage = list(state.get("llm_usage", []))
        try:
            raw = await deps.llm.complete(
                prompt("intent", hazard_names=", ".join(f'"{n}"' for n in names)),
                q, json_mode=True, task="intent", payload={"text": q, "rule_hazards": rule_h, "rule_intent": rule_i},
                usage=usage)
            obj = extract_json(raw)
            if obj.get("intent") in ALLOWED_INTENTS:
                intent = obj["intent"]
            llm_h = [h for h in (obj.get("hazards") or []) if h in names]
            # AI가 낸 유형은 질문 글자에 근거(유형 이름·동의어)가 있을 때만 채택한다. 근거 없는 유형은 버리고 진단에 남긴다
            dropped = [h for h in llm_h if not rules.hazard_grounded(h, q)]
            hazards = list(dict.fromkeys([h for h in llm_h if h not in dropped] + rule_h))
            if dropped:
                log.info("parse_intent: 질문에 근거 없는 유형 제외 %s", dropped)
            place = rules.place_candidate(obj.get("place"), q)  # 원문에 있는 장소 표현만 (코드 검사)
            source = "llm+rules"
        except (LLMError, ValueError, TypeError) as e:
            errors.append(f"parse_intent: {type(e).__name__}: {e}")
            fail = _llm_failure(e)
        else:
            fail = None
        writer({"event": "intent", "intent": intent, "hazards": hazards, "hazard_source": source, "hazards_dropped": dropped})
        return {"intent": intent, "hazards": hazards, "hazard_source": source, "place": place, "llm_errors": errors,
                "llm_usage": usage, "failures": list(state.get("failures", [])) + ([fail] if fail else [])}

    # ── gate (코드) ──
    async def gate(state: State, writer):
        try:
            loc = await with_retry(lambda: deps.mcp.tool("resolve_location", {
                "text": state["query"], "history_region_id": state.get("history_region_id") or None,
                "browser_region_id": state.get("browser_region_id") or None}))
        except Exception as e:  # noqa: BLE001 — 데이터 서비스(MCP) 자체가 안 되면 원인과 함께 '정보 없음'으로 멈춘다
            cause = mcp_cause(e)
            log.warning("mcp failure at gate: %s %s", cause, type(e).__name__)
            annotate_span(source_failures=[f"MCP:{cause}"])
            writer({"event": "source", "name": "데이터 서비스", "status": "failed", "cause": cause, "count": 0})
            writer(progress_failed("데이터 서비스", "failed", cause))
            return {"location": {"status": "none", "chosen": None, "candidates": []}, "regions": [],
                    "region_names": {}, "service_down": cause,
                    "failures": list(state.get("failures", [])) + [
                        {"stage": "mcp", "source": "데이터 서비스", "cause": cause, "status": "failed",
                         "detail": f"{type(e).__name__}: {e}"[:200]}]}
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
            # '광주'처럼 정해 둔 묶음은 되묻지 않고 둘 다 조회, 그 밖의 동명(고성군·같은 구 이름)은 되묻기 (ADR-006, ADR-017)
            regions = [] if loc.get("ask") else [c["id"] for c in loc["candidates"]]
        else:
            regions = [loc["chosen"]]
        writer({"event": "location", **loc, "regions": regions})
        if links and pid in regions:
            chips = validate_links(links["links"], links["notice"], names[pid])
            if chips:  # 링크는 답변 본문이 아니라 칩으로만, 상태(트레이스)에 넣지 않고 이벤트로만 보낸다
                writer({"event": "links", **chips})
        return {"location": loc, "regions": regions, "region_names": names}

    async def ask_region(state: State, writer):
        loc = state.get("location") or {}
        if loc.get("ask") and loc.get("candidates"):
            names = [c["name"] for c in loc["candidates"]]
            text = msg("fixed.ask_region_candidates", candidates=", ".join(names), example=names[0])
        elif loc.get("province"):
            text = msg("fixed.ask_region_province", province=loc["province"], example=f"{loc['province']} ○○시")
        else:
            text = rules.ASK_REGION_TEXT
        return {"answer": text, "answer_source": "fixed", "judgements": []}

    async def unavailable(state: State, writer):
        return {"route": "unavailable", "answer": msg("fixed.service_unavailable", reason=reason("failed", state["service_down"])),
                "answer_source": "fixed", "judgements": []}

    # ── plan (코드 규칙, ADR-018) ──
    # 예전에는 LLM 이 제안하고 코드가 제약했지만, 단계는 의도만으로 정해졌다(T-032) → 규칙 표(rules.yaml plan_steps).
    async def plan_node(state: State, writer):
        plan = {"steps": rule_plan(state["intent"]), "source": "rules"}
        writer({"event": "plan", **plan})
        return {"plan": plan}

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
                cause, why = mcp_cause(e), f"{type(e).__name__}: {e}"[:200]
                resp = (_failed_alert(rid, names.get(rid, rid), mode, cause, why) if kind == "alerts"
                        else _failed_messages(rid, cause, why))
            src = resp["source"]
            writer({"event": "source", "region_id": rid, "name": src["name"], "status": src["status"],
                    "cause": src.get("cause"), "count": len(resp.get("alerts", resp.get("messages", [])))})
            if src["status"] != "ok":   # 사용자 문구에는 API 이름·ID 대신 사용자 말 (원래 이름은 source 이벤트에만)
                label = "기상특보" if kind == "alerts" else "재난문자"
                writer(progress_failed(f"{names.get(rid, rid)} {label}", src["status"], src.get("cause")))
            return kind, rid, resp

        jobs = [fetch("alerts", r) for r in regions]
        if "messages" in steps:
            jobs += [fetch("messages", r) for r in regions]
        results = await asyncio.gather(*jobs)
        alerts = {rid: resp for k, rid, resp in results if k == "alerts"}
        messages = {rid: resp for k, rid, resp in results if k == "messages"}

        # 관련 재난: 특보가 알려 준 재난 + 사용자가 말한 재난 (재난문자 라벨은 신뢰도 문제로 제외, T-010)
        wanted: dict[str, set] = {}
        for rid in regions:
            a = alerts[rid]
            for hz in [x["hazard"] for x in a["alerts"] + a["pending"]] + list(state.get("hazards", [])):
                wanted.setdefault(rid, set()).add(hz)
        guides: dict[str, dict] = {}
        if "guide" in steps:
            # 모든 단계를 한 번에 받고, 지금 할 일 / 미리 대비할 일 / 하지 말 것 묶음은 judge 가 고른다 (ADR-016)
            calls = {hz for v in wanted.values() for hz in v}

            async def guide(hz: str):
                try:
                    r = await with_retry(lambda: deps.mcp.tool("search_action_guide", {"hazard": hz, "limit": 300, "mode": mode}))
                except Exception as e:  # noqa: BLE001 — '없음'(unavailable)이 아니라 '조회 실패'(failed)로 둔다
                    r = {"hazard": hz, "available": False, "cause": mcp_cause(e),
                         "reason": f"조회 실패: {type(e).__name__}", "rows": []}
                return hz, r
            got = dict(await asyncio.gather(*[guide(h) for h in calls]))
            for rid, items in wanted.items():
                guides[rid] = {hz: got[hz] for hz in sorted(items)}
            bad = [r for r in got.values() if r.get("cause")]
            writer({"event": "source", "name": "행동요령", "status": "failed" if bad else "ok",
                    "cause": bad[0]["cause"] if bad else None, "count": len(calls)})

        # 쉼터·대피장소: 계획과 무관하게 코드가 붙인다 — 관련 재난 중 쉼터 구분이 있는 유형만 (호우·태풍 등은 없음)
        await allowed_hazards()
        pairs = []
        for rid, hzs in sorted(wanted.items()):  # 같은 구분(폭염·열대야 = 무더위쉼터)은 지역마다 한 번만, 자기 구분인 유형 우선
            kinds: dict[str, str] = {}
            for hz in sorted((h for h in hzs if h in shelter_kind), key=lambda h: (h in borrowed, h)):
                kinds.setdefault(shelter_kind[hz], hz)
            pairs += [(rid, hz) for hz in kinds.values()]

        async def shelter(rid: str, hz: str):
            try:
                r = await with_retry(lambda: deps.mcp.tool("get_shelters", {"region_id": rid, "hazard": hz, "mode": mode}))
            except Exception as e:  # noqa: BLE001 — 쉼터 실패는 나머지 답변을 막지 않는다 (해당 항목만 '정보 없음')
                r = {"region_id": rid, "hazard": hz, "status": "failed", "cause": mcp_cause(e),
                     "reason": f"조회 실패: {type(e).__name__}", "shelters": []}
            return rid, hz, r
        shelters: dict[str, dict] = {}
        for rid, hz, r in await asyncio.gather(*[shelter(r, h) for r, h in pairs]):
            shelters.setdefault(rid, {})[hz] = r
        if pairs:
            bad_s = [r for d in shelters.values() for r in d.values() if r["status"] == "failed"]
            writer({"event": "source", "name": msg("shelters.source_name"), "status": "failed" if bad_s else "ok",
                    "cause": bad_s[0].get("cause") if bad_s else None,
                    "count": sum(len(r["shelters"]) for d in shelters.values() for r in d.values())})
        fails = [{"stage": "source", "source": r["source"]["name"], "region_id": rid, "cause": r["source"].get("cause"),
                  "status": r["source"]["status"]}
                 for d in (alerts, messages) for rid, r in d.items() if r["source"]["status"] == "failed"]
        fails += [{"stage": "source", "source": f"행동요령({r['hazard']})", "cause": r["cause"], "status": "failed"}
                  for r in (got.values() if "guide" in steps else []) if r.get("cause")]
        fails += [{"stage": "source", "source": f"{msg('shelters.source_name')}({hz})", "region_id": rid,
                   "cause": r.get("cause"), "status": "failed"}
                  for rid, d in shelters.items() for hz, r in d.items() if r["status"] == "failed"]
        if fails:
            log.warning("source failures: %s", [(f["source"], f.get("region_id"), f["cause"]) for f in fails])
            annotate_span(source_failures=[f"{f['source']}:{f['cause']}" for f in fails])
        return {"alerts": alerts, "messages": messages, "guides": guides, "shelters": shelters,
                "failures": list(state.get("failures", [])) + fails}

    # ── judge (코드) ──
    async def judge_node(state: State, writer):
        js = [judge.judge_region(state["alerts"][rid], state["messages"].get(rid), state.get("guides", {}).get(rid, {}),
                                 state.get("shelters", {}).get(rid, {}))
              for rid in state["regions"]]
        # 광역시 구 이름으로 물었으면 '구가 아니라 시 전체 기준'임을 밝힌다 (특보는 광역시 단위, ADR-017)
        districts = (state.get("location") or {}).get("districts") or {}
        for j in js:
            if j["region_id"] in districts:
                j["scope_note"] = msg("region.district_scope", district=districts[j["region_id"]], region=j["region"])
        writer({"event": "judgement", "judgements": js})
        # 단계별 표시 (ADR-019): 코드가 만든 근거를 먼저, 그다음 검증을 통과한 템플릿 요약. LLM 문장은 아직 보내지 않는다
        writer({"event": "evidence", "regions": [judge.evidence_view(j) for j in js]})
        quick = compose.render_answer(js, state["intent"])
        qv = validate(quick, js)
        if qv["ok"]:  # 템플릿이 검증을 통과하지 못하면(드묾) 요약을 먼저 보내지 않고 final 을 기다린다
            writer({"event": "quick", "answer": quick, "answer_source": "template", "validation": qv})
        return {"judgements": js}

    # ── write (LLM) ──
    async def write_node(state: State, writer):
        js, intent = state["judgements"], state["intent"]
        errors = list(state.get("llm_errors", []))
        usage = list(state.get("llm_usage", []))  # 재작성마다 한 줄씩 (write 호출 수 = 재작성 + 1)
        rules_text = await deps.mcp.prompt("disaster_briefing_prompt", {
            "region": ", ".join(j["region"] for j in js), "hazards": ", ".join(state.get("hazards", [])) or "미지정",
            "question": state["query"]})
        facts = [{"region": j["region"], "level": j["level_label"], "as_of": j["as_of"], "zones": j["zone_names"],
                  "note": j["note"], "gaps": j["gaps"],
                  "evidence": [{k: e[k] for k in ("id", "kind", "text", "source", "group_label") if k in e}
                               for e in j["evidence"]]} for j in js]
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
            symptom_ids = [e["id"] for e in j["evidence"] if e["kind"] == "guide" and e.get("group") == "symptom"]
            if symptom_ids:
                region_ids.append(prompt("write", "must_cite_guides", region=j["region"],
                                         ids=", ".join(f"[{i}]" for i in symptom_ids)))
            # 행동요령 묶음(지금 할 일 / 미리 대비할 일 / 하지 말 것)은 judge 가 정했다 — LLM 은 풀어 쓰기만 (ADR-016)
            groups: dict[str, list[str]] = {}
            for e in j["evidence"]:
                if e["kind"] == "guide" and e.get("group_label"):
                    groups.setdefault(compose.guide_heading(e, j["evidence"]), []).append(e["id"])
            if groups:
                notes = "".join(prompt("write", "guide_notes", note=n) for n in j.get("guide_notes", []))
                # 쓸 모양 그대로 보여 준다 (제목 줄 + 항목 줄). 'head: [G1], [G2]' 목록으로 주면 작은 모델이 그 목록을 베꼈다
                region_ids.append(prompt("write", "guide_groups", region=j["region"], notes=notes, groups="\n".join(
                    head + "\n" + "\n".join(f"- (근거 {i}의 내용) [{i}]" for i in ids) for head, ids in groups.items())))
            # 쉼터: 제목 줄(운영 여부 미확인 안내)을 그대로, 그 아래 [S#] 항목만 — 가까움·운영 여부를 지어내지 않는다
            sgroups: dict[str, list[str]] = {}
            for e in j["evidence"]:
                if e["kind"] == "shelter":
                    sgroups.setdefault(e["group_label"], []).append(e["id"])
            if sgroups:
                region_ids.append(prompt("write", "shelter_groups", region=j["region"], groups="\n".join(
                    f"  {head}\n    → " + ", ".join(f"[{i}]" for i in ids) for head, ids in sgroups.items())))
        user = prompt("write", "user", facts=json.dumps(facts, ensure_ascii=False), region_ids="\n".join(region_ids),
                      disclaimer=compose.DISCLAIMER)
        must = []
        for j in js:
            # 템플릿(compose)과 같은 표준 문구를 요구한다 — 문구 정의는 prompts/messages.yaml 한 곳
            sentences = [j["scope_note"]] if j.get("scope_note") else []
            if j["level"] == "none":
                sentences.append(msg("answer.none", as_of=j["as_of"], zones=compose.zone_text(j)))
            if j["level"] == "unknown":
                sentences.append(msg("answer.unknown"))
            sentences += [compose.gap_line(g) for g in j["gaps"] if g["status"] in ("failed", "no_coverage", "unavailable")]
            sentences += list(dict.fromkeys(e["group_label"] for e in j["evidence"] if e["kind"] == "shelter"))
            sentences += j.get("guide_notes", [])
            must += [prompt("write", "must", region=j["region"], sentence=s) for s in sentences]
        if must:
            user += "\n" + prompt("write", "must_header") + "\n" + "\n".join(f"* {m}" for m in must)
        if state.get("feedback"):
            user += "\n\n" + prompt("rewrite", "user", violations="\n".join(f"- {v}" for v in state["feedback"]))
        try:
            # 답변이 행동요령 묶음·증상 대처·쉼터로 길어져 900 토큰에서 끝이 잘렸다(면책 줄 누락) → 쓰기만 여유를 둔다
            text = compose.tidy_llm(await deps.llm.complete(rules_text, user, task="write", max_tokens=WRITE_MAX_TOKENS,
                                                            payload={"judgements": js, "intent": intent}, usage=usage), js)
            source = "llm" if deps.llm.name != "fake" else "fake"
        except Exception as e:  # noqa: BLE001
            errors.append(f"write: {type(e).__name__}: {e}")
            text, source = compose.render_answer(js, intent), "template_llm_error"
            fail = _llm_failure(e)
            if fail:
                return {"answer": text, "answer_source": source, "llm_errors": errors, "llm_usage": usage,
                        "failures": list(state.get("failures", [])) + [fail]}
        return {"answer": text, "answer_source": source, "llm_errors": errors, "llm_usage": usage}

    # ── validate (코드) ──
    async def validate_node(state: State, writer):
        v = validate(state["answer"], state["judgements"])
        writer({"event": "validation", **v, "attempt": state.get("rewrites", 0) + 1})
        out: dict[str, Any] = {"validation": v}
        if not v["ok"]:
            out["feedback"] = v["violations"]
            out["rewrites"] = state.get("rewrites", 0) + 1
            out["rejections"] = list(state.get("rejections", [])) + [
                {"attempt": state.get("rewrites", 0) + 1, "violations": v["violations"]}]
        return out

    def after_validate(state: State) -> str:
        if state["validation"]["ok"]:
            return "finalize"
        return "write" if state.get("rewrites", 0) <= deps.settings.max_rewrites else "fallback"

    async def fallback(state: State, writer):
        text = compose.render_answer(state["judgements"], state["intent"])
        v = validate(text, state["judgements"])
        # final.validation 은 템플릿 검증 결과가 되므로, AI 답변이 탈락한 사유는 meta.rejections 와 로그에 따로 남긴다
        log.warning("AI 답변 검증 반려 → 템플릿 대체: %s", [r["violations"] for r in state.get("rejections", [])])
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
                     "failures": state.get("failures", []), "llm_usage": state.get("llm_usage", []),
                     "rejections": state.get("rejections", []),
                     "disclaimer": compose.DISCLAIMER},
        }
        return {"final": final}

    g = StateGraph(State)
    for name, fn in [("route", route_node), ("fixed_response", fixed_response), ("parse_intent", parse_intent),
                     ("gate", gate), ("ask_region", ask_region), ("unavailable", unavailable), ("plan", plan_node), ("research", research),
                     ("judge", judge_node), ("write", write_node), ("validate", validate_node),
                     ("fallback", fallback), ("finalize", finalize)]:
        g.add_node(name, timed(name, fn))
    g.add_edge(START, "route")
    g.add_conditional_edges("route", lambda s: "parse_intent" if s["route"] == "disaster" else "fixed_response",
                            ["parse_intent", "fixed_response"])
    g.add_edge("fixed_response", "finalize")
    g.add_edge("parse_intent", "gate")
    g.add_conditional_edges("gate", lambda s: "unavailable" if s.get("service_down") else "plan" if s["regions"] else "ask_region",
                            ["plan", "ask_region", "unavailable"])
    g.add_edge("ask_region", "finalize")
    g.add_edge("unavailable", "finalize")
    g.add_edge("plan", "research")
    g.add_edge("research", "judge")
    g.add_edge("judge", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", after_validate, ["finalize", "write", "fallback"])
    g.add_edge("fallback", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
