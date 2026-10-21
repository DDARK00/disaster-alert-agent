"""환경변수를 한 곳에서 읽는 단일 Settings (backend·mcp 공용).

서비스 키(*_SERVICE_KEY, KAKAO_REST_API_KEY)는 여기서 읽지 않는다 — mcp 의 capture.py 만 읽는다 (키 최소 노출).
LANGSMITH_* 는 langsmith 라이브러리가 직접 읽는다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_mode: str = "replay"            # DATA_MODE: replay | live
    max_rewrites: int = 1                # MAX_REWRITES: 검증 반려 시 재작성 횟수
    # LLM (backend)
    llm_backend: str = "fake"            # LLM_BACKEND: fake | openai_compat
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "qwen3:8b"
    llm_api_key: str = field(default="", repr=False)
    llm_timeout: float = 120.0
    llm_no_think: bool = False           # LLM_NO_THINK=1
    # MCP 연결 (backend) — MCP_URL 이 없으면 stdio 서브프로세스
    mcp_url: str = ""
    mcp_python: str = ""
    cors_origins: str = "*"
    demo_faults: bool = False            # DEMO_FAULTS=1: 시연용 장애 주입 허용 (기본 꺼짐, 공개 배포에서 켜지 않는다, ADR-021)
    # MCP 서버 (mcp)
    mcp_transport: str = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8200
    mcp_path: str = "/mcp"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = env if env is not None else os.environ
        g = env.get
        return cls(
            data_mode=g("DATA_MODE", "replay"), max_rewrites=int(g("MAX_REWRITES", "1")),
            llm_backend=g("LLM_BACKEND", "fake"), llm_base_url=g("LLM_BASE_URL", "http://localhost:11434/v1"),
            llm_model=g("LLM_MODEL", "qwen3:8b"), llm_api_key=g("LLM_API_KEY", ""),
            llm_timeout=float(g("LLM_TIMEOUT", "120")), llm_no_think=g("LLM_NO_THINK", "0") == "1",
            mcp_url=g("MCP_URL", ""), mcp_python=g("MCP_PYTHON", ""), cors_origins=g("CORS_ORIGINS", "*"),
            demo_faults=g("DEMO_FAULTS", "0") == "1",
            mcp_transport=g("MCP_TRANSPORT", "stdio"), mcp_host=g("MCP_HOST", "0.0.0.0"),
            mcp_port=int(g("MCP_PORT", "8200")), mcp_path=g("MCP_PATH", "/mcp"),
        )


def load_dotenv_file(path: Path) -> None:
    """로컬 개발용: .env 를 읽어 비어 있는 환경변수만 채운다. (컨테이너는 compose 가 주입)"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip().strip("\"'")  # 인라인 주석: 공백 1칸 이상 + '#' (T-029)
            if v:
                os.environ.setdefault(k.strip(), v)
