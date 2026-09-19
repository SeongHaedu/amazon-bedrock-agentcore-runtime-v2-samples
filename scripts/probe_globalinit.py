# probe_globalinit.py
# Fire several sessions at agent-globalinit-probe at once to see where global initialization
# actually runs.
#
# Sequential invokes let V1 serve from its pre-warmed instances, where global initialization
# was already spent off the request path. Enough concurrent sessions exhaust that pool, and
# then V1 shows the initialization on the request path. V2 keeps it off the path regardless
# of the pool.
#
# Source for the pre-warmed instances that V1 container deployments keep per endpoint:
# https://repost.aws/articles/ARCJIn3t7aRC2FxiRTV1SuCA
#
# usage:
#   python scripts/probe_globalinit.py <agentRuntimeId|agentRuntimeArn> [concurrency]
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import boto3
from botocore.config import Config

from common import PROFILE, REGION, make_client, save_result

USAGE = "usage: python scripts/probe_globalinit.py <agentRuntimeId|agentRuntimeArn> [concurrency]"


def main():
    if len(sys.argv) < 2:
        raise SystemExit(USAGE)
    target = sys.argv[1]
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    control_client = make_client("bedrock-agentcore-control")
    arn = (
        target
        if target.startswith("arn:")
        else control_client.get_agent_runtime(agentRuntimeId=target)["agentRuntimeArn"]
    )

    session = boto3.Session(profile_name=PROFILE) if PROFILE else boto3.Session()
    # One client per thread. Sharing a single client would serialize the requests.
    clients = [
        session.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=Config(retries={"max_attempts": 0}, connect_timeout=5, read_timeout=180),
        )
        for _ in range(concurrency)
    ]

    def invoke_once(index):
        session_id = f"v2sample-burst-{index:03d}-{uuid.uuid4().hex}"
        t0 = time.monotonic()
        try:
            resp = clients[index].invoke_agent_runtime(
                agentRuntimeArn=arn,
                contentType="application/json",
                accept="application/json",
                runtimeSessionId=session_id,
                payload=json.dumps({"probe": True}).encode(),
            )
            text = resp["response"].read().decode("utf-8", "replace")
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                # An entrypoint that returns a generator responds with SSE, which is not
                # readable as JSON. Keep the raw text so the measurement is not lost; the
                # aggregation below tolerates a body without baked.
                body = {"raw": text[:500]}
            return {"i": index, "ok": True, "latency_ms": round((time.monotonic() - t0) * 1000, 1), "body": body}
        except Exception as exc:  # noqa: BLE001
            return {
                "i": index,
                "ok": False,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(invoke_once, range(concurrency)))

    # Save before aggregating so that a failure in the aggregation does not lose the run.
    save_result(f"probe_globalinit_{target.split('/')[-1]}.json", results)

    ok = [r for r in results if r["ok"]]
    print(f"n={len(results)} ok={len(ok)} err={len(results) - len(ok)}", flush=True)
    if ok:
        print(f"latency ms: {sorted(round(r['latency_ms']) for r in ok)}", flush=True)

    for r in results:
        if not r["ok"]:
            print(f"    error i={r['i']}: {r['error']}", flush=True)

    # baked is returned only by agent-globalinit-probe. Running this against agent-basic
    # leaves the key absent, so skip the aggregation and say which target it expects.
    with_baked = [r for r in ok if isinstance(r["body"], dict) and r["body"].get("baked")]
    if not with_baked:
        if ok:
            print(
                "\nNo baked in the responses. This script targets an agent-globalinit-probe"
                " runtime. For agent-basic, use invoke_runtime.py.",
                flush=True,
            )
        return

    # A handful of distinct values means the sessions were restored from that many snapshots
    # (V2). As many distinct values as the concurrency means each session ran global
    # initialization itself (V1).
    uuids = {r["body"]["baked"].get("uuid") for r in with_baked}
    print(f"distinct baked uuid = {len(uuids)} / {len(with_baked)}", flush=True)
    for r in sorted(with_baked, key=lambda r: r["latency_ms"]):
        body = r["body"]
        baked = body["baked"]
        print(
            f"    lat={r['latency_ms']:8.0f}ms lazy_ran={body.get('lazy_ran')!s:5} "
            f"lazy_secs={body.get('lazy_secs', 0):6} "
            f"baked={str(baked.get('uuid'))[:8]} "
            f"baked_wall_clock={str(baked.get('wall_clock'))[11:23]} "
            f"now={str(body.get('now'))[11:23]}",
            flush=True,
        )


if __name__ == "__main__":
    main()
