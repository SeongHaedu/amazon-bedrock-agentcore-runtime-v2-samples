FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

RUN uv pip install --system --no-cache bedrock-agentcore

COPY main.py ./

ENV PYTHONUNBUFFERED=1

# グローバル初期化と遅延初期化の待ち時間。既定は各 10 秒である。
# create / update 時のスナップショット準備は GLOBAL_INIT_SECS の分だけ長くなる。
ENV GLOBAL_INIT_SECS=10
ENV LAZY_INIT_SECS=10

EXPOSE 8080

CMD ["python", "main.py"]
