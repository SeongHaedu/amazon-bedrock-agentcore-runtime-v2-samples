# probe_globalinit.py
# agent-globalinit-probe を対象に、複数セッションを同時に発行してグローバル初期化の所在を観測する。
#
# 逐次実行では V1 のウォームプールに空きがあり、グローバル初期化がプール側 (リクエスト経路の外)
# で消費されてしまう。同時発行でプールを枯渇させると、V1 ではグローバル初期化がリクエスト経路に
# 現れる。V2 ではプールの状態に関係なく常に経路の外である。
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
    # クライアントはスレッドごとに分ける。同一クライアントの共有による直列化を避けるためである。
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
            body = json.loads(resp["response"].read())
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

    ok = [r for r in results if r["ok"]]
    print(f"n={len(results)} ok={len(ok)} err={len(results) - len(ok)}", flush=True)
    print(f"latency ms: {sorted(round(r['latency_ms']) for r in ok)}", flush=True)

    if ok:
        # distinct が 1 なら全セッションが同一のスナップショットから復元されている (V2)。
        # concurrency と同数なら各セッションが自分でグローバル初期化を実行している (V1)。
        uuids = {r["body"]["baked"]["uuid"] for r in ok}
        print(f"distinct baked uuid = {len(uuids)} / {len(ok)}", flush=True)
        for r in sorted(ok, key=lambda r: r["latency_ms"]):
            baked = r["body"]["baked"]
            print(
                f"    lat={r['latency_ms']:8.0f}ms lazy_ran={r['body']['lazy_ran']!s:5} "
                f"lazy_secs={r['body']['lazy_secs']:6} "
                f"baked={baked['uuid'][:8]} baked_wall_clock={baked['wall_clock'][11:23]} "
                f"now={r['body']['now'][11:23]}",
                flush=True,
            )

    for r in results:
        if not r["ok"]:
            print(f"    error i={r['i']}: {r['error']}", flush=True)

    save_result(f"probe_globalinit_{target.split('/')[-1]}.json", results)


if __name__ == "__main__":
    main()
