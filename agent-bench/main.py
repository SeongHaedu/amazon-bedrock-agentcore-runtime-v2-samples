# agent-bench/main.py
#
# コールドスタート計測の対象エージェント。V1 と V2 の pre-entrypoint を同一条件で比べるために、
# クライアント側の時刻とサーバ側の時刻を突き合わせるマーカーを出力する。
#
#   [MODULE_START] / [MODULE_END] : モジュールスコープ (グローバルスコープ) の実行
#   [ENTRYPOINT_REACHED]          : リクエストハンドラの 1 行目。pre-entrypoint の終点である。
#   [FIRST_TOKEN]                 : モデルの最初の差分を受け取った時刻
#
# GLOBAL_INIT_SECS を渡すと、モジュールスコープに任意秒のスリープを挿入できる。重い初期化を
# 持つエージェントを模擬するためである。V1 ではこのスリープがリクエストの経路に乗り、
# V2 ではスナップショット準備時に 1 回だけ消費される。既定値は 0 であり、渡さなければ挿入しない。
#
# LLM のレスポンスは stream_async で逐次ストリーミングする。最初の 1 バイトの到達時刻を
# クライアント側で測れるようにするためである。
import os
import time

_module_start = time.time()
print(f"[MODULE_START] {_module_start}", flush=True)

from strands import Agent  # noqa: E402
from strands.models.bedrock import BedrockModel  # noqa: E402
from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

# 重いモジュールスコープ初期化を模したブロック。
# V1 では実行環境の起動ごとに、V2 ではスナップショット準備時に 1 回だけ実行される。
GLOBAL_INIT_SECS = float(os.environ.get("GLOBAL_INIT_SECS", "0"))
if GLOBAL_INIT_SECS > 0:
    print(f"[GLOBAL_INIT_START] {time.time()} secs={GLOBAL_INIT_SECS}", flush=True)
    time.sleep(GLOBAL_INIT_SECS)
    print(f"[GLOBAL_INIT_END] {time.time()}", flush=True)

app = BedrockAgentCoreApp()

# ap-northeast-1 には us. プレフィックスのクロスリージョン推論プロファイルが存在しない。
# そのリージョンで測る場合は BEDROCK_MODEL_ID に jp. プレフィックスのプロファイルを渡す。
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
model = BedrockModel(model_id=MODEL_ID)
agent = Agent(
    name="bench-agent",
    model=model,
    system_prompt="You are a benchmark agent. Follow the instruction exactly and output nothing else.",
)

# 出力長を決め打ちできるプロンプトにする。生成時間を定数として扱えるようにするためである。
FIXED_PROMPT = "List the integers from 1 to 40 separated by commas."

_module_end = time.time()
print(
    f"[MODULE_END] {_module_end} elapsed_ms={(_module_end - _module_start) * 1000:.1f} "
    f"model_id={MODEL_ID} global_init_secs={GLOBAL_INIT_SECS}",
    flush=True,
)


@app.entrypoint
async def invoke(payload, context):
    # この print の時刻が pre-entrypoint の終点である。クライアントの dispatched_at との差が
    # pre-entrypoint である。session_id を載せるのは、クライアント側の記録と突き合わせるためである。
    entrypoint_ts = time.time()
    session_id = getattr(context, "session_id", None)
    print(f"[ENTRYPOINT_REACHED] {entrypoint_ts} session_id={session_id}", flush=True)

    if payload.get("type") == "warmup":
        # warmup sentinel: LLM を呼ばずに即座に返す。事前ウォームアップ用である。
        yield {"status": "warm"}
        return

    first_token_emitted = False
    async for event in agent.stream_async(FIXED_PROMPT):
        # Strands はテキストの差分を {"data": "..."} で yield する。
        # ツール利用やメタデータのイベントはクライアントへ流さない。
        if isinstance(event, dict) and event.get("data"):
            if not first_token_emitted:
                print(f"[FIRST_TOKEN] {time.time()} session_id={session_id}", flush=True)
                first_token_emitted = True
            yield event["data"]

    if not first_token_emitted:
        # 差分が 1 件も出なかった場合でもクライアントが読み切れるようにする。
        print(f"[NO_TOKEN] {time.time()} session_id={session_id}", flush=True)
        yield ""


if __name__ == "__main__":
    app.run()
