"""Dispatch new sessions in an open loop and record the latency of every request.

Do not make this a closed loop. `ThreadPoolExecutor(max_workers=TPS)` caps the number of
requests in flight at TPS, so as soon as one request takes longer than a second the next
dispatch is held back and the effective rate falls below the target.

That changes the V1 numbers substantially. Stretching the dispatch out gives the pre-warmed
instances time to be replenished, so Container V1 shows more warm hits than it really has.
In our own runs a closed loop achieved only 1.0 - 1.35 effective TPS and 60 of 100 requests
hit a warm instance; switching to an open loop reached 5.26 effective TPS and that dropped
to 20.

max_workers is TPS x duration. Requests are dispatched at TPS per second without waiting for
completions. The effective TPS and the dispatch span are always printed; check them against
the target on every run.

usage:
  python benchmark/tps_bench_open.py <agentRuntimeId|agentRuntimeArn> <label> [tps] [duration_sec]

invoke_agent_runtime takes the ARN. An id is accepted here as well and resolved with
get_agent_runtime, so the runtime ids exported for build_breakdown.py can be reused as is.
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

USAGE = (
    "usage: python benchmark/tps_bench_open.py "
    "<agentRuntimeId|agentRuntimeArn> <label> [tps] [duration_sec]"
)

if len(sys.argv) < 3:
    raise SystemExit(USAGE)


def resolve_arn(value: str) -> str:
    """Accept either an id or an ARN. invoke_agent_runtime takes the ARN only, so an id is
    resolved with a single get_agent_runtime call before the measurement starts."""
    if value.startswith("arn:"):
        return value
    session = boto3.Session(profile_name=PROFILE) if PROFILE else boto3.Session()
    control = session.client("bedrock-agentcore-control", region_name=REGION)
    return control.get_agent_runtime(agentRuntimeId=value)["agentRuntimeArn"]


AGENT_RUNTIME_ARN = resolve_arn(sys.argv[1])
LABEL = sys.argv[2]
TPS = int(sys.argv[3]) if len(sys.argv) > 3 else 5
DURATION_SEC = int(sys.argv[4]) if len(sys.argv) > 4 else 20

MAX_WORKERS = TPS * DURATION_SEC
# Spread the connections over several clients. botocore's max_pool_connections defaults to 10,
# and at 100 concurrent requests the wait for a connection leaks into the latency.
N_CLIENTS = 20

session = boto3.Session(profile_name=PROFILE) if PROFILE else boto3.Session()
clients = [
    session.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(
            # Automatic retries would leak into the measurement.
            retries={"max_attempts": 0},
            connect_timeout=5,
            # A V1 cold start can push end-to-end into the tens of seconds.
            read_timeout=180,
            max_pool_connections=MAX_WORKERS // N_CLIENTS + 5,
        ),
    )
    for _ in range(N_CLIENTS)
]


def invoke_once(client, sec: int, idx: int) -> dict:
    # runtimeSessionId must be at least 33 characters. Every request gets a fresh session id
    # so that no session is ever reused.
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
        # Do not use iter_lines(). It buffers internally, which makes the time the first line
        # returns equal the time the whole body was read and destroys the TTFT measurement.
        # Read raw bytes and split the two apart.
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
            # Dispatch TPS requests every second without waiting for completions.
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
    # Always confirm the effective TPS reached the target. Below it, the same problem as a
    # closed loop is happening.
    print(f"[{LABEL}] dispatch span={span:.1f}s effective TPS={len(results)/span:.2f} "
          f"(target {TPS} TPS x {DURATION_SEC}s)")
    if not ok:
        print("no valid response was returned")
        return
    print(summary("ttft", [r["ttft_ms"] for r in ok]))
    print(summary("lastbyte", [r["latency_ms"] for r in ok]))
    print(summary("gen", [r["latency_ms"] - r["ttft_ms"] for r in ok]))
    ev = [r["sse_events"] for r in ok]
    print(f"[{LABEL}] sse_events min={min(ev)} median={statistics.median(ev):.0f} max={max(ev)}")
    print(f"saved: {output_path}")


if __name__ == "__main__":
    run()
