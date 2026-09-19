FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# 依存は requirements.txt で管理する。直接コードデプロイ用の ZIP を作る
# scripts/setup_codezip_artifact.py も同じファイルを読むため、二重管理にならない。
# bedrock-agentcore のみにしているのは、LLM を呼ばないサンプルであり、イメージを小さく
# 保つほどスナップショット準備の所要時間への影響も小さくなるためである。
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY main.py ./

# stdout のバッファリングを無効化する。[MODULE_START] 等のマーカーが即座にログへ出ないと、
# ログストリームごとの有無を判定できないためである。
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "main.py"]
