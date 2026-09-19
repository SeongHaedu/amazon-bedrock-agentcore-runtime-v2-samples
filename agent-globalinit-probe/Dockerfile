FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Dependencies are managed in requirements.txt, the same file scripts/setup_codezip_artifact.py reads.
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY main.py ./

ENV PYTHONUNBUFFERED=1

# Sleep durations for global initialization and lazy initialization; 10 seconds each by
# default. Snapshot preparation on create / update grows by GLOBAL_INIT_SECS.
ENV GLOBAL_INIT_SECS=10
ENV LAZY_INIT_SECS=10

EXPOSE 8080

CMD ["python", "main.py"]
