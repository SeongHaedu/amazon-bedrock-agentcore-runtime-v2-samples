# invoke_runtime.py
# Invoke a runtime with invoke_agent_runtime and record the response and the latency.
#
# Each session gets a fresh runtimeSessionId. Re-invoking the same session reuses the
# existing execution environment, which hides the startup of a new one. A second invoke in
# the same session gives the floor for a request that includes no startup work.
#
# usage:
#   python scripts/invoke_runtime.py <agentRuntimeId|agentRuntimeArn> [sessions] [invokes_per_session]
#
# example:
#   python scripts/invoke_runtime.py v2sample_basic_v2-XXXXXXXXXX 3 2
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import boto3
from botocore.config import Config

from common import PROFILE, REGION, make_client, save_result

# runtimeSessionId must be at least 33 characters. uuid4().hex is 32, so it is not enough
# on its own.
SESSION_ID_MIN_LEN = 33

USAGE = (
    "usage: python scripts/invoke_runtime.py "
    "<agentRuntimeId|agentRuntimeArn> [sessions] [invokes_per_session]"
)


def to_arn(value, client):
    if value.startswith("arn:"):
        return value
    # Given an id, look the ARN up with get_agent_runtime. Assembling the ARN by hand invites
    # mixing up the account id and the Region.
    return client.get_agent_runtime(agentRuntimeId=value)["agentRuntimeArn"]


def make_session_id(index):
    session_id = f"v2sample-{index:03d}-{uuid.uuid4().hex}"
    assert len(session_id) >= SESSION_ID_MIN_LEN, f"runtimeSessionId too short: {len(session_id)}"
    return session_id


def invoke_once(data_client, arn, session_id, seq):
    t0 = time.monotonic()
    dispatched_at = time.time()
    try:
        resp = data_client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            contentType="application/json",
            accept="application/json",
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": "ping"}).encode(),
        )
        body = resp["response"].read() if "response" in resp else b""
        latency_ms = (time.monotonic() - t0) * 1000
        text = body.decode("utf-8", "replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # An entrypoint that returns a generator responds with text/event-stream
            # regardless of the accept header. Keep the raw text in that case.
            parsed = {"raw": text[:500]}
        return {
            "session_id": session_id,
            "seq": seq,
            "dispatched_at": dispatched_at,
            "ok": True,
            "latency_ms": round(latency_ms, 1),
            "status_code": resp.get("statusCode"),
            "content_type": resp.get("contentType"),
            "body": parsed,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "session_id": session_id,
            "seq": seq,
            "dispatched_at": dispatched_at,
            "ok": False,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


def main():
    if len(sys.argv) < 2:
        raise SystemExit(USAGE)
    target = sys.argv[1]
    sessions = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    per_session = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    control_client = make_client("bedrock-agentcore-control")
    arn = to_arn(target, control_client)

    # A generous read_timeout leaves room for agent initialization and model generation.
    # retries is 0 so that automatic retries do not leak into the latency numbers.
    session = boto3.Session(profile_name=PROFILE) if PROFILE else boto3.Session()
    data_client = session.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(retries={"max_attempts": 0}, connect_timeout=5, read_timeout=180),
    )

    results = []
    for index in range(sessions):
        session_id = make_session_id(index)
        for seq in range(1, per_session + 1):
            record = invoke_once(data_client, arn, session_id, seq)
            results.append(record)
            if record["ok"]:
                body = record["body"]
                identity = body.get("snapshot_identity") or body.get("baked") or {}
                print(
                    f"session={index} seq={seq} latency={record['latency_ms']:.0f}ms "
                    f"identity_uuid={str(identity.get('uuid'))[:8]} "
                    f"lazy_ran={body.get('lazy_ran')}",
                    flush=True,
                )
            else:
                print(f"session={index} seq={seq} failed: {record['error']}", flush=True)

    ok = [r for r in results if r["ok"]]
    first = [r["latency_ms"] for r in ok if r["seq"] == 1]
    rest = [r["latency_ms"] for r in ok if r["seq"] > 1]
    print(f"\ntotal={len(results)} ok={len(ok)}", flush=True)
    if first:
        print(f"first invoke of a new session: {[round(x) for x in sorted(first)]}", flush=True)
    if rest:
        # Later invokes land on the existing execution environment, which is the floor for a
        # request that includes no startup work.
        print(f"second and later invoke in the same session: {[round(x) for x in sorted(rest)]}", flush=True)

    # Whether identity_uuid changes per new session is what separates V1 from V2.
    identities = {
        json.dumps((r["body"].get("snapshot_identity") or r["body"].get("baked") or {}).get("uuid"))
        for r in ok
    }
    print(f"distinct identity uuid = {len(identities)} / {len(ok)} responses", flush=True)

    save_result(f"invoke_{target.split('/')[-1]}.json", results)


if __name__ == "__main__":
    main()
