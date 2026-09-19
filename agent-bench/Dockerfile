FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# 依存は requirements.txt で管理する。直接コードデプロイ用の ZIP を作る
# scripts/setup_codezip_artifact.py も同じファイルを読むため、二重管理にならない。
# strands-agents を入れるため agent-basic よりイメージが大きくなる。V1 ではこのサイズが
# コールドスタートに乗り、V2 ではスナップショットのサイズがイメージの成長に対してほぼ平坦になる。
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY main.py ./

ENV PYTHONUNBUFFERED=1

# モジュールスコープに挿入するスリープの秒数。0 なら挿入しない。
# 120 秒に近い値を設定するとヘルスチェックの期限に間に合わず作成が失敗する。
ENV GLOBAL_INIT_SECS=0

EXPOSE 8080

CMD ["python", "main.py"]
