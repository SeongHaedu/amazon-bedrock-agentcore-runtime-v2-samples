FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# 依存は bedrock-agentcore のみにする。LLM を呼ばないサンプルであり、イメージを小さく保つほど
# スナップショット準備の所要時間への影響も小さくなる。
RUN uv pip install --system --no-cache bedrock-agentcore

COPY main.py ./

# stdout のバッファリングを無効化する。[MODULE_START] 等のマーカーが即座にログへ出ないと、
# ログストリームごとの有無を判定できないためである。
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "main.py"]
