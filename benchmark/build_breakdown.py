"""Join the client records with [ENTRYPOINT_REACHED] from CloudWatch Logs and write a
per-request breakdown to a single JSON file.

  startup_ms = [ENTRYPOINT_REACHED] timestamp - the client's dispatched_at
  agent_ms   = (dispatched_at + latency_ms) - the [ENTRYPOINT_REACHED] timestamp

What the article calls pre-entrypoint is startup_ms. The key keeps the name startup_ms so that
the data format matches what the chart script reads.

The log search window is derived from the dispatched_at values in the result JSON. A fixed
window would drop requests once the V1 pre-entrypoint exceeds 30 seconds.

usage:
  python benchmark/build_breakdown.py [suffix]

  suffix    Label suffix of the result JSON files; defaults to open.
            Reads results/coldstart_results_{codezip,container}_{v1,v2}_{suffix}.json.

Environment variables (at least one is required):
  AGENTCORE_BENCH_CODEZIP_RUNTIME_ID     Runtime id for the CodeZip series
  AGENTCORE_BENCH_CONTAINER_RUNTIME_ID   Runtime id for the Container series

It takes a few minutes after an invoke before the logs can be queried from Logs Insights.
Running this immediately after a measurement leaves requests unmatched, so wait about two
minutes first.
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
    """The log group name follows from the runtime id and the endpoint name; the DEFAULT
    endpoint appends -DEFAULT. When both platform versions are measured by switching the same
    runtime id, both series land in the same log group, so the aggregation window has to be
    separated per measurement."""
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
            "Set at least one of AGENTCORE_BENCH_CODEZIP_RUNTIME_ID or"
            " AGENTCORE_BENCH_CONTAINER_RUNTIME_ID."
        )
    # Drop series whose result JSON is missing, so that measuring only one deployment mode
    # still works.
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
    print(f"series: {', '.join(series)}", flush=True)

    def fetch(label):
        log_group, name = series[label]
        records = json.loads((RESULTS_DIR / name).read_text())
        dispatched = [r["dispatched_at"] for r in records]
        # Pad both ends. With GLOBAL_INIT_SECS added, the V1 pre-entrypoint exceeds 30 seconds,
        # so the trailing side is padded more generously.
        start = int(min(dispatched)) - 120
        end = int(max(dispatched)) + 300
        return label, records, entrypoint_timestamps(log_group, start, end)

    # Query ids are independent per series, so the queries can run concurrently.
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
            # Dropping unmatched requests silently would skew the distribution. Log ingestion
            # most likely needs more time, so wait and re-run.
            raise SystemExit(
                f"{label}: {unmatched} requests could not be matched with ENTRYPOINT_REACHED."
                " Wait for log ingestion and re-run."
            )
        if min(startup) < 0:
            # Either the client and server clocks disagree, or logs from a previous
            # measurement leaked into the aggregation window.
            raise SystemExit(f"{label}: startup_ms is negative (minimum {min(startup):.0f} ms)")
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
