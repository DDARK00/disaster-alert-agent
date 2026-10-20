# 빌드 컨텍스트: 저장소 루트 (data/ 를 함께 담는다)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY mcp_server/requirements.txt mcp_server/requirements.txt
RUN pip install --no-cache-dir -r mcp_server/requirements.txt
COPY mcp_server/ mcp_server/
COPY data/ data/
ENV MCP_TRANSPORT=http MCP_HOST=0.0.0.0 MCP_PORT=8200 MCP_PATH=/mcp DATA_MODE=replay
EXPOSE 8200
# 포트가 열렸는지만 본다 (MCP 핸드셰이크는 백엔드 /health 가 확인)
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8200), 2).close()"
RUN useradd -m app && chown -R app /app
USER app
CMD ["python", "mcp_server/server.py"]
