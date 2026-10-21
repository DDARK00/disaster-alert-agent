# 백엔드 API (`backend/`)

실행: `sh/run_local.sh` (포트 8100) 또는 `docker compose up --build`(프론트 8080 → `/api/*`).

| 경로 | 설명 |
|---|---|
| `GET /health` | `{status, mcp, llm, data_mode, sources, mcp_link}` — MCP 연결 실패 시 503. `sources`: 소스별 최근 live 호출 `{last_ok_at, last_fail_at, last_cause, fail_count}`(키·URL 없음, MCP 프로세스 기동 이후), `mcp_link`: 백엔드→MCP 최근 성공·실패와 원인 (ADR-014) |
| `GET /scenarios` | 시연 시나리오 목록 (`backend/scenarios.json`) |
| `GET /chat` | 최종 결과 JSON |
| `GET /chat/stream` | SSE. 쿼리: `message`, `mode?`, `as_of?`, `history_region_id?`, `browser_region_id?`, `fault?`(`DEMO_FAULTS=1`일 때만, 아니면 404) |

서버는 대화 상태를 보관하지 않는다(Stateless). 직전에 확정된 지역은 클라이언트가 `history_region_id`로 보낸다.

## SSE 이벤트
`start` → (`step` start/done 쌍들, `progress`, `route`, `intent`, `location`, `plan`, `source`×N) → `judgement` → `evidence` → `quick` → (`progress`, `validation`…) → `token`×N → `final` (실패 시 `error`)

단계별 표시 (ADR-019): 검증이 필요 없는 **코드가 만든 정보**를 먼저 보낸다. 검증 전 LLM 문장은 어떤 이벤트에도 싣지 않는다.
- `progress`: `{stage, text, cause?}` — 노드 시작마다 짧은 진행 문구(`messages.yaml progress.*`: 질문 이해 → 지역 확인 → 조회 → 정리 → 작성 → 검증, 재작성이면 `rewrite`, 템플릿 대체면 `fallback`). 조회가 실패하면 `stage: source_failed` + 원인별 문구(`causes.*`, ADR-014).
- `evidence`: `{regions: [{region_id, region, level, level_label, as_of, scope_note, note, alerts[], messages[], guide_groups: [{group, label, hazard, items[]}], shelters: {header, items[]} | null, gaps[]}]}` — `judgement`의 근거를 종류·묶음별로 나눈 것(`judge.evidence_view`). 문장을 만들지 않는다.
- `quick`: `{answer, answer_source: "template", validation}` — 판정 직후 고정 템플릿(`compose.render_answer`, fallback과 같은 것)을 **검증기에 통과시킨 뒤에만** 보낸다(통과 못 하면 보내지 않음). LLM 답변이 검증을 통과하면 `final.answer`가 요약 영역을 교체하고, LLM이 실패하거나 검증이 끝내 실패하면 `final.answer`가 이 요약과 같다(`answer_source` = `template_llm_error` / `template_fallback`).
- 고정 응답(위급·범위 밖·되묻기·MCP 불가)에는 `evidence`·`quick`이 없다(근거가 없고 `final`이 바로 온다).
- 기존 소비자: `final` 형태는 그대로이고 `token` 합 = `final.answer`도 그대로다(평가·시나리오 러너 호환). 프론트는 `quick`을 받은 뒤에는 `token`을 덧쓰지 않고 `final`에서 요약 영역만 바꾼다(근거 카드 유지).
- 버퍼링: 백엔드 `X-Accel-Buffering: no`, nginx `/api/`는 `proxy_buffering off`·`proxy_cache off`(`frontend/nginx.conf`) — 이벤트가 생기는 대로 도착한다.
- 측정(qwen3:8b, replay, 로컬): `quick` 도착 1.7~5.5초(첫 요청은 모델 적재 포함), `final` 24~47초.

- `links`: 카카오맵 링크 칩 `{region, notice, chips:[{kind: map|route, label, url}]}` — 장소가 질문에 있고 조회 지역과 일치할 때만. 답변 본문이 아니라 별도 칩이며 `final.links`에도 실린다. 경로는 판단하지 않는다(고정 문구 "경로상의 도로 통제·침수 여부는 반영되지 않았습니다.").
- `judgement`: 판정 카드(지역·등급·근거·정보 없음 목록). 문장보다 먼저 도착한다. `gaps[]`: `{source, status, cause, reason, detail}` — `reason`은 사용자 문구, `cause`·`detail`은 개발자용.
  - 근거 ID: `E#` 특보, `M#` 재난문자, `G#` 행동요령, `S#` 쉼터·대피장소(지역마다 1부터). 쉼터 근거는 `{kind: shelter, hazard, text: "이름 — 주소", group_label(운영 여부 미확인 안내 제목), ref(관리번호), source, map_url}` — `map_url`은 카카오맵 **검색** 링크(`/link/search/…`, 형식 검사 후에만, 답변 본문이 아니라 카드 칩). 쉼터 조회 실패는 `gaps`에 `쉼터 목록(폭염)` 같은 이름으로 `failed`+원인, 범위 데이터 없음은 gap 없이 생략.
  - `scope_note`: 광역시 구 이름으로 물었을 때 "'○○구' 단위가 아니라 ○○광역시 전체 기준" (ADR-017).
- `source`: `{name, status, cause, count, region_id?}` — 실패면 `cause`(원인 코드, ADR-014). 쉼터는 `name: 쉼터 목록`.
- `plan`: `{steps, source: "rules"}` — 의도별 규칙 표(rules.yaml `plan_steps`, ADR-018). 쉼터는 계획과 무관하게 코드가 붙인다.
- `validation`: `{ok, violations, attempt}` — 위반이면 재작성 또는 템플릿 대체.
- `token`: **검증을 통과한 답변만** 24자씩 (ADR-008).
- `final`: `{route, answer, answer_source, intent, hazards, location, plan, judgements, validation, meta}`

`location.place`: `{status, region_id, result_count}` (Kakao 요약). 장소 → 지역 규칙: 명시 지역 표현 우선 · 이력/브라우저 추정과 다르면 두 지역 모두 · 지명이 없을 때만 `assumed` · '광주'처럼 지역명뿐이면 Kakao 미호출 (T-025).

`final.meta.llm_usage`: `[{task: intent|write, prompt_tokens, completion_tokens, ms}]` — LLM 호출별 토큰(OpenAI 호환 `usage`, 재작성마다 write 한 줄). 개발자 상세용(화면 툴팁). 입력이 모델 컨텍스트를 넘으면 앞이 잘리므로 평가 요약에 write 입력 토큰 최대·중앙값을 낸다.

`final.meta.failures`: `[{stage: source|mcp|llm, source?, region_id?, cause, status}]` — 이번 요청에서 난 실패 전부(LLM 실패 `llm_unavailable` 포함).

`route`: `disaster` / `emergency` / `not_in_data`(공식 데이터에 없는 행동요령 질문 — 고정 "해당 항목이 없습니다", rules.yaml `not_in_official_data`) / `out_of_scope` / `ask_region`(지역 없음 · 같은 이름이 여러 곳 — 후보 나열 · 도 이름만 — 시·군 요청) / `unavailable`(MCP에 연결하지 못함 — 고정 "정보 없음" 응답에 원인 문구).

조회 실패 원인 코드: `api_auth_error` `rate_limited` `api_http_error` `api_result_error` `api_timeout` `network_error` `response_invalid` `mcp_unreachable` `mcp_tool_error` (+ meta 전용 `llm_unavailable`). 문구는 `prompts/messages.yaml` `causes.*`.

`answer_source`: `fixed`(위급·범위 밖·지역 되묻기·MCP 불가) / `llm` / `fake` / `template_fallback`(검증 반복 실패) / `template_llm_error`(LLM 오류).

## 그래프
`route → parse_intent → gate → plan → research → judge → write → validate → finalize` (+ fixed_response, ask_region, unavailable, fallback). LLM 은 parse_intent·write 두 곳만(plan 은 코드 규칙, ADR-018). 설명은 `agent/graph.py` 상단 주석.

## 환경변수
`config/settings.py`의 `Settings.from_env()` 한 곳에서 읽는다: `MCP_URL`(없으면 stdio 서브프로세스), `DATA_MODE`, `LLM_BACKEND`(fake|openai_compat), `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT`, `LLM_NO_THINK`(1이면 `/no_think` + `reasoning_effort=none`, T-020), `MAX_REWRITES`, `CORS_ORIGINS`, `DEMO_FAULTS`(1이면 시연용 장애 주입 허용, 기본 꺼짐, ADR-021). `LANGSMITH_*`는 langsmith가 직접 읽는다.

## 규칙·문구·프롬프트 (ADR-012)
| 파일 | 내용 | 쓰는 곳 |
|---|---|---|
| `config/rules.yaml` | 위급·의도 단어(공백=`\s*`로 컴파일), 재난 동의어, 범위 단어, 막연한 위치 표현, 금지 표현, 레벨 라벨·순위, 근거 상한, 행동요령 대상·묶음, 쉼터 구분·상한, 조회 계획 표(`plan_steps`) | `rules`, `judge`, `validator`, `graph`, MCP |
| `prompts/messages.yaml` | 면책, 119/112·범위 밖·되묻기 고정 응답, 템플릿 문구, 검증 기준 조각(`markers`), MCP statement | `compose`, `validator`, `graph`, MCP |
| `prompts/{intent,write,rewrite}.md` | LLM 지시문 (`=== 구역 ===`, 자리표시자 `$name`). `plan.md`는 비교 스크립트용(ADR-018) | `graph`, `eval/compare_plan.py` |
| `prompts/briefing.md` | MCP Prompt `disaster_briefing_prompt` 원문 (write 의 system) | MCP |

- 판정·라우팅·검증 로직은 코드에 있다. 파일에는 데이터만.
- 검증기 규칙(`agent/validator.py`): 금지 표현 · 면책/119 · 본문 URL 금지 · 모든 '- ' 항목 인용(정보 없음 줄 제외) · 인용 id 실재 · 지역명 · unknown→'정보 없음' · none→'확인된 위험 정보 없음' · 조회 실패 표기 · **지역 섹션별 인용 대조와 발효 중·예고 특보 전부 인용**(T-027) · 행동요령이 없는 유형은 "해당 항목이 없습니다" 표기(ADR-015) · `[S#]` 쉼터를 인용하면 "운영 여부·운영 시간은 이 목록에 없어" 안내 필수, 가까움·운영 단정 금지 표현. · 증상이 있으면(필수 행동) 근거 전부 인용 · 행동요령 묶음 제목 필수 · 다른 유형 행동요령을 쓰면 "전용 항목이 없어" 안내 필수(ADR-020). 검증 전 LLM 출력의 내용 없는 구분선(`---`)만 지운다.
- 행동요령 묶음(ADR-016): 근거 `guide`에 `group`(now|prepare|dont)·`group_label`. 템플릿은 `▶ 지금 할 일` 같은 묶음 제목 아래 항목(한 지역에 유형이 둘 이상일 때만 `(폭염)`처럼 유형을 붙임), write에는 쓸 모양(제목 줄 + 항목 줄)과 ID를 준다. 묶음: 지금 할 일 / 증상이 있으면(필수 행동, 발효 중) / 미리 대비할 일 / 하지 말 것(금지 문장만) — 중복 제거·지역 전체 상한 7(ADR-020). 판정 결과에 `zone_names`(특보구역 표시 이름) — 답변·카드에는 코드 대신 이름.
- 근거 선택(`agent/judge.py`): 금지 표현(T-024)·URL(T-030)이 든 공식 원문 행은 근거에서 뺀다(고쳐 쓰지 않음). 행동요령 근거에는 소스 표시 이름(`source`)이 붙는다.
- 기동 시 검사(`config/catalog.py`): 파일·키·구역 누락, 표준 문구가 검증 기준 조각(`markers`)을 포함하는지, 문구에 금지 표현이 없는지. 어기면 시작하지 않는다.
- 검증 기준 조각·금지 표현은 테스트(`Catalog`)로 고정 — 파일 수정으로 검증이 느슨해지면 테스트가 실패한다.
- LangSmith 루트 트레이스(`disaster_chat`)에 `prompts@<hash>` 태그와 파일별 해시 메타데이터.

## 테스트
`sh/test.sh` — MCP + 백엔드 단위·통합 테스트. 외부 API·LLM 없이 fixture, 프로세스 내 MCP, FakeLLM, 가짜 OpenAI 호환 HTTP 서버로 돈다(Kakao는 mock, 실호출 스모크는 선택 실행).

## 평가 (`eval/`)
- `eval/dataset.jsonl`: 특보 판정(경기 광주·광주광역시·모호 '광주'·이력), 행동요령(소스·묶음), 위급, 되묻기, 범위 밖, 공식 데이터에 없음, 장애 주입, 쉼터(폭염·한파·지진 포함 / 호우·태풍 비포함 / 쉼터 도구 실패), 전국 지역(광역시 구·도 소속 시·고성군 되묻기·신 시도 표기·지역 없음). 기대값은 커밋된 fixture의 **원본 이벤트**를 사람이 읽고 정했다(`basis`). 시스템 판정 코드로 만들지 않는다.
- 지표: `route` `regions` `level`(행동요령 항목은 근거 존재·기대 소스 포함) `no_missed_risk`(특보가 있는데 none/unknown) `no_forbidden` `grounded`(백엔드 검증기 그대로) `emergency_fixed` `cause`(장애 주입 항목의 원인 코드·문구) `not_in_data` `structured`(행동요령 묶음 제목·구역 코드 비노출) `shelter`(쉼터 근거·운영 여부 안내가 기대대로 있거나 없음).
- 장애 주입 항목(`fault` 필드, `eval/faults.py`)은 HTTP가 아니라 프로세스 내 같은 그래프·MCP로 돈다(네트워크 경계만 가짜, `shelter_down`은 쉼터 도구만 실패시키고 replay). LLM은 환경변수로 만든다 — qwen 평가는 `LLM_BACKEND=openai_compat LLM_BASE_URL=http://localhost:11434/v1 LLM_NO_THINK=1`을 함께 준다.
- 실행: `.venv/bin/python eval/run_eval.py` (로컬 채점, 답변 출처·응답 시간 중앙값 출력) / `--langsmith --prefix <이름>` (데이터셋 `disaster-advisor-eval-<sha8>`, 내용이 바뀌면 새 데이터셋).
- 계획 비교: `eval/compare_plan.py` — 같은 질문에서 LLM 계획과 규칙 계획(ADR-018)이 같은지.
- 결과 기록은 트러블슈팅(T-026, T-027, T-032, T-033)에 둔다.
- 시연 시나리오 회귀: `.venv/bin/python eval/run_scenarios.py [--live]`.
