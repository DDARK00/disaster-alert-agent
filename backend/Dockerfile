# 빌드 컨텍스트: 저장소 루트
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY config/ config/
COPY prompts/ prompts/
ENV MCP_URL=http://mcp:8200/mcp DATA_MODE=replay LLM_BACKEND=fake
EXPOSE 8100
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4).status == 200 else 1)"
RUN useradd -m app && chown -R app /app
USER app
CMD ["python", "-m", "uvicorn", "--factory", "server:app_factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8100"]
