#!/usr/bin/env bash
# 도커 없이 백엔드 실행 (MCP 서버는 백엔드가 stdio 서브프로세스로 기동). http://localhost:8100
set -euo pipefail
cd "$(dirname "$0")/.."
export DATA_MODE=${DATA_MODE:-replay}
export LLM_BACKEND=${LLM_BACKEND:-fake}
exec .venv/bin/python -m uvicorn --factory server:app_factory --app-dir backend --port 8100
