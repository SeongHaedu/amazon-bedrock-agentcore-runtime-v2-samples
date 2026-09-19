FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Dependencies are managed in requirements.txt. scripts/setup_codezip_artifact.py, which
# builds the ZIP for direct code deployment, reads the same file, so there is nothing to keep
# in sync by hand. strands-agents makes this image larger than agent-basic. On V1 that size
# lands on the cold start; on V2 the snapshot size stays nearly flat as the image grows.
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY main.py ./

ENV PYTHONUNBUFFERED=1

# Seconds to sleep at module scope; 0 inserts nothing. A value close to 120 misses the health
# check deadline and creation fails.
ENV GLOBAL_INIT_SECS=0

EXPOSE 8080

CMD ["python", "main.py"]
