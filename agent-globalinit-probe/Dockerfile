FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# 依存は requirements.txt で管理する。scripts/setup_codezip_artifact.py も同じファイルを読む。
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY main.py ./

ENV PYTHONUNBUFFERED=1

# グローバル初期化と遅延初期化の待ち時間。既定は各 10 秒である。
# create / update 時のスナップショット準備は GLOBAL_INIT_SECS の分だけ長くなる。
ENV GLOBAL_INIT_SECS=10
ENV LAZY_INIT_SECS=10

EXPOSE 8080

CMD ["python", "main.py"]
