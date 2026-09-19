FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Dependencies are managed in requirements.txt. scripts/setup_codezip_artifact.py, which
# builds the ZIP for direct code deployment, reads the same file, so there is nothing to keep
# in sync by hand. Only bedrock-agentcore is listed: this sample calls no LLM, and a smaller
# image also means a smaller effect on how long snapshot preparation takes.
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY main.py ./

# Disable stdout buffering. Unless markers such as [MODULE_START] reach the logs immediately,
# there is no way to tell which log stream carries them.
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "main.py"]
