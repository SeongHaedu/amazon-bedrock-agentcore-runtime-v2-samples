"""開ループで新規セッションを投入し、リクエストごとのレイテンシーを記録する。

閉ループにしてはいけません。`ThreadPoolExecutor(max_workers=TPS)` にすると同時に走る
リクエストが TPS 件に制限され、1 リクエストの所要時間が 1 秒を超えた時点で次の投入が
待たされます。結果として実効レートが目標を下回ります。

これは V1 の測定結果を大きく変えます。投入が引き延ばされるとその間に pre-warmed instance の
補充が進むため、Container V1 のウォーム命中が実態より多く出ます。手元の検証では、閉ループで
実効 1.0 〜 1.35 TPS しか出ておらず、100 件中 60 件がウォーム命中でした。開ループに直して
実効 5.26 TPS を達成すると 20 件に下がりました。

max_workers を TPS × 継続秒数 にし、投入ペースは毎秒 TPS 件のまま、完了を待たずに投入します。
実効 TPS と dispatch span を必ず出力するので、目標どおり出ているかを毎回確認してください。

usage:
  python benchmark/tps_bench_open.py <agentRuntimeArn> <label> [tps] [duration_sec]
"""
import json
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

from common import PROFILE, REGION, RESULTS_DIR  # noqa: E402

USAGE = "usage: python benchmark/tps_bench_open.py <agentRuntimeArn> <label> [tps] [duration_sec]"

if len(sys.argv) < 3:
    raise SystemExit(USAGE)

AGENT_RUNTIME_ARN = sys.argv[1]
LABEL = sys.argv[2]
TPS = int(sys.argv[3]) if len(sys.argv) > 3 else 5
DURATION_SEC = int(sys.argv[4]) if len(sys.argv) > 4 else 20

MAX_WORKERS = TPS * DURATION_SEC
# クライアントを分けて同時接続を分散する。botocore の max_pool_connections は既定 10 であり、
# 100 並行では接続待ちがレイテンシーに混入する。
N_CLIENTS = 20

session = boto3.Session(profile_name=PROFILE) if PROFILE else boto3.Session()
clients = [
    session.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(
            # 自動リトライは計測に混入するため無効化する。
            retries={"max_attempts": 0},
            connect_timeout=5,
            # V1 のコールドスタートでは end-to-end が数十秒に達する場合がある。
            read_timeout=180,
            max_pool_connections=MAX_WORKERS // N_CLIENTS + 5,
        ),
    )
    for _ in range(N_CLIENTS)
]


def invoke_once(client, sec: int, idx: int) -> dict:
    # runtimeSessionId は 33 文字以上必要である。全リクエストで新規のセッション ID を発行し、
    # セッション再利用が起きないようにする。
    session_id = f"coldstart-{sec:03d}-{idx:02d}-{uuid.uuid4().hex}"
    dispatched_at = time.time()
    t0 = time.monotonic()
    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            contentType="application/json",
            accept="application/json",
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": "Say OK."}).encode(),
        )
        stream = resp["response"]
        # iter_lines() を使ってはいけない。内部バッファに読み溜めるため、最初の行が返る時刻が
        # 読み切り時刻と一致してしまい TTFT が測れない。生バイトの read で分ける。
        first = stream.read(1)
        ttft_ms = (time.monotonic() - t0) * 1000
        body = [first]
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            body.append(chunk)
        latency_ms = (time.monotonic() - t0) * 1000
        joined = b"".join(body)
        events = joined.count(b"data:")
        if b'"error"' in joined:
            return {
                "session_id": session_id, "dispatched_at": dispatched_at,
                "status": "agent_error", "latency_ms": latency_ms, "ttft_ms": ttft_ms,
                "sse_events": events, "error": joined[:300].decode("utf-8", "replace"),
            }
        return {
            "session_id": session_id, "dispatched_at": dispatched_at, "status": "ok",
            "latency_ms": latency_ms, "ttft_ms": ttft_ms, "sse_events": events, "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.monotonic() - t0) * 1000
        error_name = type(exc).__name__
        status = "throttled" if "Throttl" in error_name or "Throttl" in str(exc) else "error"
        return {
            "session_id": session_id, "dispatched_at": dispatched_at, "status": status,
            "latency_ms": latency_ms, "ttft_ms": None, "sse_events": 0, "error": str(exc)[:300],
        }


def summary(name, values):
    s = sorted(values)
    n = len(s)
    pct = lambda q: s[min(int(n * q), n - 1)]  # noqa: E731
    return (
        f"[{LABEL}] {name:9s} avg={sum(s)/n:.0f}ms sd={statistics.stdev(s):.0f}ms "
        f"p50={pct(.50):.0f}ms p90={pct(.90):.0f}ms p99={pct(.99):.0f}ms "
        f"min={s[0]:.0f}ms max={s[-1]:.0f}ms"
    )


def run():
    results = []
    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = []
        for sec in range(DURATION_SEC):
            # 毎秒 TPS 件を投入する。完了は待たない。
            batch_start = t_start + sec
            now = time.monotonic()
            if batch_start > now:
                time.sleep(batch_start - now)
            for i in range(TPS):
                futures.append(pool.submit(invoke_once, clients[i % len(clients)], sec, i))
        for f in as_completed(futures):
            results.append(f.result())

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESULTS_DIR / f"coldstart_results_{LABEL}.json"
    output_path.write_text(json.dumps(results, indent=2))

    ok = [r for r in results if r["status"] == "ok"]
    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in ("ok", "throttled", "error", "agent_error")}
    span = max(r["dispatched_at"] for r in results) - min(r["dispatched_at"] for r in results)
    print(f"[{LABEL}] total={len(results)} " + " ".join(f"{k}={v}" for k, v in counts.items()))
    # 実効 TPS が目標に達しているかを必ず確認する。下回っていれば閉ループと同じ問題が起きている。
    print(f"[{LABEL}] dispatch span={span:.1f}s 実効 TPS={len(results)/span:.2f} "
          f"(目標 {TPS} TPS x {DURATION_SEC}s)")
    if not ok:
        print("有効なレスポンスが 0 件だった")
        return
    print(summary("ttft", [r["ttft_ms"] for r in ok]))
    print(summary("lastbyte", [r["latency_ms"] for r in ok]))
    print(summary("gen", [r["latency_ms"] - r["ttft_ms"] for r in ok]))
    ev = [r["sse_events"] for r in ok]
    print(f"[{LABEL}] sse_events min={min(ev)} median={statistics.median(ev):.0f} max={max(ev)}")
    print(f"saved: {output_path}")


if __name__ == "__main__":
    run()
