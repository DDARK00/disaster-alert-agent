"""LLM 백엔드 교체 지점. 노드는 이 인터페이스만 안다 (모델이 8b든 9b든 호스팅 API든 코드 변경 없음).

- FakeLLM        : 결정론 (테스트·replay 시연·LLM 없이 개발)
- OpenAICompatLLM: OpenAI 호환 /chat/completions (Ollama /v1, OpenAI, Gemini 호환 엔드포인트 등)

환경변수: LLM_BACKEND=fake|openai_compat, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, LLM_TIMEOUT, LLM_NO_THINK=1
"""
from __future__ import annotations

import json
import re

import httpx
from langsmith import traceable

from config.settings import Settings

from . import compose


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    """모델 출력에서 첫 JSON 객체를 꺼낸다 (코드펜스·잡설 허용). 실패 시 ValueError."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError("JSON 객체 없음")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON 객체가 아님")
    return obj


class BaseLLM:
    name = "base"

    async def complete(self, system: str, user: str, *, json_mode: bool = False, task: str = "",
                       payload: dict | None = None, max_tokens: int = 900) -> str:
        raise NotImplementedError


class FakeLLM(BaseLLM):
    """task/payload 로 결정론 출력을 만든다. bad_write_times 로 검증 실패 경로를 시험할 수 있다."""

    def __init__(self, bad_write_times: int = 0, fail_tasks: set[str] | None = None, garbage_tasks: set[str] | None = None,
                 place: str = ""):
        self.name = "fake"
        self.place = place  # intent 가 돌려줄 장소 표현 (테스트용)
        self.bad_write_times = bad_write_times
        self.fail_tasks = fail_tasks or set()
        self.garbage_tasks = garbage_tasks or set()
        self.calls: list[str] = []

    async def complete(self, system, user, *, json_mode=False, task="", payload=None, max_tokens=900):
        self.calls.append(task)
        p = payload or {}
        if task in self.fail_tasks:
            raise LLMError(f"fake failure: {task}")
        if task in self.garbage_tasks:
            return "죄송합니다, 잘 모르겠어요 ```not json```"
        if task == "intent":
            out = {"intent": p.get("rule_intent", "status"), "hazards": p.get("rule_hazards", [])}
            if self.place:
                out["place"] = self.place
            return json.dumps(out, ensure_ascii=False)
        if task == "write":
            if self.bad_write_times > 0:
                self.bad_write_times -= 1
                return "지금은 안전합니다. 걱정하지 마세요. - 밖에 나가도 됩니다"
            return compose.render_answer(p["judgements"], p.get("intent", "status"))
        raise LLMError(f"unknown task {task}")


class OpenAICompatLLM(BaseLLM):
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 120.0, no_think: bool = False):
        self.name = f"openai_compat:{model}"
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.no_think = no_think

    # LANGSMITH_TRACING=true 일 때만 기록된다. payload 는 FakeLLM 용이라 트레이스에서 뺀다.
    @traceable(run_type="llm", name="llm", process_inputs=lambda i: {k: i.get(k) for k in ("system", "user", "task", "json_mode")})
    async def complete(self, system, user, *, json_mode=False, task="", payload=None, max_tokens=900):
        if self.no_think:
            user = user + "\n/no_think"
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.no_think:  # Ollama 0.32 /v1 은 '/no_think' 문구를 무시하고 사고에 토큰을 다 쓴다 (T-020)
            body["reasoning_effort"] = "none"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise LLMError(f"LLM 연결 실패: {type(e).__name__}") from e
        if r.status_code != 200:
            raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
        try:
            text = r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError("LLM 응답 형식 오류") from e
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def make_llm(env: dict | None = None) -> BaseLLM:
    s = Settings.from_env(env)
    if s.llm_backend == "fake":
        return FakeLLM()
    if s.llm_backend == "openai_compat":
        return OpenAICompatLLM(s.llm_base_url, s.llm_model, s.llm_api_key, s.llm_timeout, s.llm_no_think)
    raise ValueError(f"알 수 없는 LLM_BACKEND: {s.llm_backend}")
