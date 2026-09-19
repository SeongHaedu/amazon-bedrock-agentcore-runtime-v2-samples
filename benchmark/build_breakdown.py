"""クライアントの記録と CloudWatch Logs の [ENTRYPOINT_REACHED] を突き合わせ、
リクエストごとの内訳を 1 つの JSON にまとめる。

  startup_ms = [ENTRYPOINT_REACHED] の時刻 - クライアントの dispatched_at
  agent_ms   = (dispatched_at + latency_ms) - [ENTRYPOINT_REACHED] の時刻

本稿でいう pre-entrypoint が startup_ms です。キー名を startup_ms のままにしているのは、
図の生成スクリプトとデータ形式を揃えるためです。

ログ検索のウィンドウは結果 JSON の dispatched_at から自動で決めます。固定ウィンドウにすると、
V1 の pre-entrypoint が 30 秒を超えたときに取りこぼします。

usage:
  python benchmark/build_breakdown.py [suffix]

  suffix    結果 JSON のラベル接尾辞。既定は open。
            results/coldstart_results_{codezip,container}_{v1,v2}_{suffix}.json を読む。

必要な環境変数 (少なくとも一方):
  AGENTCORE_BENCH_CODEZIP_RUNTIME_ID     CodeZip 系列のランタイム ID
  AGENTCORE_BENCH_CONTAINER_RUNTIME_ID   Container 系列のランタイム ID

ランタイムを invoke してからログが Logs Insights で引けるようになるまで数分かかります。
計測直後に実行すると照合できない件が出るため、2 分程度あけてから実行してください。
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import RESULTS_DIR, make_client  # noqa: E402

SUFFIX = sys.argv[1] if len(sys.argv) > 1 else "open"
OUT = RESULTS_DIR / f"breakdown_{SUFFIX}.json"

CODEZIP_RUNTIME_ID = os.environ.get("AGENTCORE_BENCH_CODEZIP_RUNTIME_ID")
CONTAINER_RUNTIME_ID = os.environ.get("AGENTCORE_BENCH_CONTAINER_RUNTIME_ID")

QUERY = """fields @message
| filter @message like /ENTRYPOINT_REACHED/
| parse @message "[ENTRYPOINT_REACHED] * session_id=*" as ep_ts, session_id
| stats min(ep_ts) as first_ep_ts by session_id
| limit 10000"""

logs = make_client("logs")


def log_group_for(runtime_id):
    """ロググループ名はランタイム ID とエンドポイント名から決まる。DEFAULT エンドポイントの場合は
    -DEFAULT が付く。V1 と V2 を同じランタイム ID で切り替えて測る場合、両系列が同じロググループに
    入るため、集計ウィンドウを計測ごとに分ける必要がある。"""
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def build_series():
    series = {}
    if CODEZIP_RUNTIME_ID:
        lg = log_group_for(CODEZIP_RUNTIME_ID)
        series["CodeZip V1"] = (lg, f"coldstart_results_codezip_v1_{SUFFIX}.json")
        series["CodeZip V2"] = (lg, f"coldstart_results_codezip_v2_{SUFFIX}.json")
    if CONTAINER_RUNTIME_ID:
        lg = log_group_for(CONTAINER_RUNTIME_ID)
        series["Container V1"] = (lg, f"coldstart_results_container_v1_{SUFFIX}.json")
        series["Container V2"] = (lg, f"coldstart_results_container_v2_{SUFFIX}.json")
    if not series:
        raise SystemExit(
            "AGENTCORE_BENCH_CODEZIP_RUNTIME_ID か AGENTCORE_BENCH_CONTAINER_RUNTIME_ID の"
            " 少なくとも一方を設定する。"
        )
    # 結果 JSON が無い系列は対象から外す。片方の方式だけ測った場合にも動くようにするためである。
    return {k: v for k, v in series.items() if (RESULTS_DIR / v[1]).exists()}


def entrypoint_timestamps(log_group, start, end):
    qid = logs.start_query(
        logGroupName=log_group, startTime=start, endTime=end, queryString=QUERY
    )["queryId"]
    while True:
        res = logs.get_query_results(queryId=qid)
        if res["status"] == "Complete":
            break
        if res["status"] not in ("Running", "Scheduled"):
            raise SystemExit(f"query {res['status']} for {log_group}")
        time.sleep(2)
    out = {}
    for row in res["results"]:
        cells = {c["field"]: c["value"] for c in row}
        out[cells["session_id"].strip()] = float(cells["first_ep_ts"])
    return out


def main():
    series = build_series()
    print(f"対象系列: {', '.join(series)}", flush=True)

    def fetch(label):
        log_group, name = series[label]
        records = json.loads((RESULTS_DIR / name).read_text())
        dispatched = [r["dispatched_at"] for r in records]
        # 前後に余裕を取る。GLOBAL_INIT_SECS を足した計測では V1 の pre-entrypoint が
        # 30 秒を超えるため、終端側を広めにする。
        start = int(min(dispatched)) - 120
        end = int(max(dispatched)) + 300
        return label, records, entrypoint_timestamps(log_group, start, end)

    # 系列ごとにクエリ ID が独立するため並行実行できる。
    with ThreadPoolExecutor(max_workers=len(series)) as pool:
        fetched = list(pool.map(fetch, series))

    dataset = {}
    for label, records, ep in fetched:
        total, startup, agent, ttft, gen, unmatched = [], [], [], [], [], 0
        for r in records:
            if r["status"] != "ok":
                continue
            ts = ep.get(r["session_id"])
            if ts is None:
                unmatched += 1
                continue
            total.append(r["latency_ms"])
            startup.append((ts - r["dispatched_at"]) * 1000)
            agent.append((r["dispatched_at"] + r["latency_ms"] / 1000 - ts) * 1000)
            ttft.append(r["ttft_ms"])
            gen.append(r["latency_ms"] - r["ttft_ms"])
        if unmatched:
            # 照合できない件を黙って捨てると分布が歪む。ログの取り込み待ちが足りていない可能性が
            # 高いので、時間をあけて再実行する。
            raise SystemExit(
                f"{label}: {unmatched} 件が ENTRYPOINT_REACHED と照合できなかった。"
                " ログの取り込みを待って再実行する。"
            )
        if min(startup) < 0:
            # クライアントとサーバのクロックずれ、または集計ウィンドウに前の計測のログが
            # 混ざっている可能性がある。
            raise SystemExit(f"{label}: startup_ms が負 (最小 {min(startup):.0f} ms)")
        dataset[label] = {
            "total_ms": total, "startup_ms": startup, "agent_ms": agent,
            "ttft_ms": ttft, "gen_ms": gen,
        }
        print(
            f"{label:14s} n={len(total):3d} "
            f"total p50={sorted(total)[len(total)//2]:7.0f} "
            f"pre-entrypoint p50={sorted(startup)[len(startup)//2]:7.0f} "
            f"agent p50={sorted(agent)[len(agent)//2]:6.0f} "
            f"ttft p50={sorted(ttft)[len(ttft)//2]:7.0f} "
            f"gen p50={sorted(gen)[len(gen)//2]:5.0f}",
            flush=True,
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dataset, indent=2))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
