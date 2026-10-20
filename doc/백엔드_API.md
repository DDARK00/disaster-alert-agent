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

- `links`: 카카오맵 링크 칩 `{region, notice, chips:[{kind: map|route, label, url}]}` — 장소가 질문에 있고 조회 지역과 일치할 때만. 답변 본문이 아니라 별도 칩이며 `final.links`에도 실린다. 경로는 판단하지 않는다(고정 문구 "경로상의 도로 통제·침수 여부는 반영되지 않았습니다.").
- `judgement`: 판정 카드(지역·등급·근거·정보 없음 목록). 문장보다 먼저 도착한다.
- `validation`: `{ok, violations, attempt}` — 위반이면 재작성 또는 템플릿 대체.
- `token`: **검증을 통과한 답변만** 24자씩 (ADR-008).
- `final`: `{route, answer, answer_source, intent, hazards, location, plan, judgements, validation, meta}`

`location.place`: `{status, region_id, result_count}` (Kakao 요약). 장소 → 지역 규칙: 명시 지역 표현 우선 · 이력/브라우저 추정과 다르면 두 지역 모두 · 지명이 없을 때만 `assumed` · '광주'처럼 지역명뿐이면 Kakao 미호출 (T-025).

`answer_source`: `fixed`(위급·범위 밖·지역 되묻기) / `llm` / `fake` / `template_fallback`(검증 반복 실패) / `template_llm_error`(LLM 오류).

## 그래프
`route → parse_intent → gate → plan → research → judge → write → validate → finalize` (+ fixed_response, ask_region, fallback). 설명은 `agent/graph.py` 상단 주석.

## 환경변수
`config/settings.py`의 `Settings.from_env()` 한 곳에서 읽는다: `MCP_URL`(없으면 stdio 서브프로세스), `DATA_MODE`, `LLM_BACKEND`(fake|openai_compat), `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT`, `LLM_NO_THINK`(1이면 `/no_think` + `reasoning_effort=none`, T-020), `MAX_REWRITES`, `CORS_ORIGINS`. `LANGSMITH_*`는 langsmith가 직접 읽는다.

## 규칙·문구·프롬프트 (ADR-012)
| 파일 | 내용 | 쓰는 곳 |
|---|---|---|
| `config/rules.yaml` | 위급·의도 단어(공백=`\s*`로 컴파일), 재난 동의어, 범위 단어, 금지 표현, 레벨 라벨·순위, 근거 상한 | `rules`, `judge`, `validator` |
| `prompts/messages.yaml` | 면책, 119/112·범위 밖·되묻기 고정 응답, 템플릿 문구, 검증 기준 조각(`markers`), MCP statement | `compose`, `validator`, `graph`, MCP |
| `prompts/{intent,plan,write,rewrite}.md` | LLM 지시문 (`=== 구역 ===`, 자리표시자 `$name`) | `graph` |
| `prompts/briefing.md` | MCP Prompt `disaster_briefing_prompt` 원문 (write 의 system) | MCP |

- 판정·라우팅·검증 로직은 코드에 있다. 파일에는 데이터만.
- 기동 시 검사(`config/catalog.py`): 파일·키·구역 누락, 표준 문구가 검증 기준 조각(`markers`)을 포함하는지, 문구에 금지 표현이 없는지. 어기면 시작하지 않는다.
- 검증 기준 조각·금지 표현은 테스트(`Catalog`)로 고정 — 파일 수정으로 검증이 느슨해지면 테스트가 실패한다.
- LangSmith 루트 트레이스(`disaster_chat`)에 `prompts@<hash>` 태그와 파일별 해시 메타데이터.

## 테스트
`sh/test.sh` — MCP 41건(선택 스모크 2건 skip) + 백엔드 64건. Kakao는 mock만. 외부 API·LLM 없이 fixture, 프로세스 내 MCP, FakeLLM, 가짜 OpenAI 호환 HTTP 서버로 돈다.
