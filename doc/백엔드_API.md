# 백엔드 API (`backend/`)

실행: `sh/run_local.sh` (포트 8100) 또는 `docker compose up --build`(프론트 8080 → `/api/*`).

| 경로 | 설명 |
|---|---|
| `GET /health` | `{status, mcp, llm, data_mode}` — MCP 연결 실패 시 503 |
| `GET /scenarios` | 시연 시나리오 목록 (`backend/scenarios.json`) |
| `GET /chat` | 최종 결과 JSON |
| `GET /chat/stream` | SSE. 쿼리: `message`, `mode?`, `as_of?`, `history_region_id?`, `browser_region_id?` |

서버는 대화 상태를 보관하지 않는다(Stateless). 직전에 확정된 지역은 클라이언트가 `history_region_id`로 보낸다.

## SSE 이벤트
`start` → (`step` start/done 쌍들, `route`, `intent`, `location`, `plan`, `source`×N, `judgement`, `validation`) → `token`×N → `final` (실패 시 `error`)

- `judgement`: 판정 카드(지역·등급·근거·정보 없음 목록). 문장보다 먼저 도착한다.
- `validation`: `{ok, violations, attempt}` — 위반이면 재작성 또는 템플릿 대체.
- `token`: **검증을 통과한 답변만** 24자씩 (ADR-008).
- `final`: `{route, answer, answer_source, intent, hazards, location, plan, judgements, validation, meta}`

`answer_source`: `fixed`(위급·범위 밖·지역 되묻기) / `llm` / `fake` / `template_fallback`(검증 반복 실패) / `template_llm_error`(LLM 오류).

## 그래프
`route → parse_intent → gate → plan → research → judge → write → validate → finalize` (+ fixed_response, ask_region, fallback). 설명은 `agent/graph.py` 상단 주석.

## 환경변수
`MCP_URL`(없으면 stdio 서브프로세스), `DATA_MODE`, `LLM_BACKEND`(fake|openai_compat), `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_NO_THINK`, `MAX_REWRITES`, `CORS_ORIGINS`, `LANGSMITH_*`.

## 테스트
`sh/test.sh` — MCP 31건 + 백엔드 48건. 외부 API·LLM 없이 fixture, 프로세스 내 MCP, FakeLLM, 가짜 OpenAI 호환 HTTP 서버로 돈다.
