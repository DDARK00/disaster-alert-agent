#!/usr/bin/env bash
# 전체 테스트 (MCP 서버 + 백엔드). 외부 API·LLM 없이 fixture/가짜 서버로 돈다.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
$PY -W error::ResourceWarning -m unittest discover -s mcp_server/tests
$PY -W error::ResourceWarning -m unittest discover -s backend/tests
