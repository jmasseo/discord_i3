FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /app/state
ENV I3B_CONFIG=/app/config.toml \
    I3B_STATE__PATH=/app/state/bridge.db \
    I3B_HEALTH__BIND=0.0.0.0 \
    PYTHONUNBUFFERED=1
VOLUME ["/app/state"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"
CMD ["i3bridge", "run"]
