"""既存ランタイムに artifact / environmentVariables / platformVersion を一度の
update_agent_runtime でまとめて適用し、READY までを計時する。

4 系列 (CodeZip / Container × V1 / V2) を同一アーティファクトで測るには、同じランタイム ID の
platformVersion を切り替えるのが最も条件が揃います。artifact と環境変数と platformVersion を
別々の update で変えると、その間にランタイムのバージョンが増えて条件が動くため、1 回の update
でまとめて適用します。

usage:
  python benchmark/apply_config.py <agentRuntimeId> <artifact> <V1|V2|omit> <global_init_secs|none> <label>

  <artifact>          codeConfiguration なら S3 prefix、containerConfiguration なら container URI。
                      現状のまま使う場合は keep を渡す。
  <global_init_secs>  agent-bench の GLOBAL_INIT_SECS に設定する秒数。none ならキー自体を外す。
  <label>             results/apply_timings.json に記録するラベル。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import RESULTS_DIR, TIMEOUT_SEC, make_client  # noqa: E402

USAGE = (
    "usage: python benchmark/apply_config.py <agentRuntimeId> <artifact|keep> "
    "<V1|V2|omit> <global_init_secs|none> <label>"
)


def main():
    if len(sys.argv) < 6:
        raise SystemExit(USAGE)
    runtime_id, new_artifact, platform_version, global_init_secs, label = sys.argv[1:6]
    if platform_version not in ("V1", "V2", "omit"):
        raise SystemExit(USAGE)

    client = make_client("bedrock-agentcore-control")
    current = client.get_agent_runtime(agentRuntimeId=runtime_id)

    # 現行のアーティファクト定義を深くコピーしてから該当フィールドだけ差し替える。
    artifact = json.loads(json.dumps(current["agentRuntimeArtifact"]))
    if "codeConfiguration" in artifact:
        old_artifact = artifact["codeConfiguration"]["code"]["s3"]["prefix"]
        if new_artifact != "keep":
            artifact["codeConfiguration"]["code"]["s3"]["prefix"] = new_artifact
    else:
        old_artifact = artifact["containerConfiguration"]["containerUri"]
        if new_artifact != "keep":
            artifact["containerConfiguration"]["containerUri"] = new_artifact

    env = dict(current.get("environmentVariables", {}))
    if global_init_secs == "none":
        # キーを残したまま 0 にするのではなく外す。エージェント側の既定値 (0) が使われる状態に
        # 戻し、「前の条件が残っていないか」を get_agent_runtime で判別できるようにするためである。
        env.pop("GLOBAL_INIT_SECS", None)
    else:
        env["GLOBAL_INIT_SECS"] = str(global_init_secs)

    print(
        f"{runtime_id}\n"
        f"  artifact: {old_artifact}\n"
        f"         -> {new_artifact}\n"
        f"  platformVersion: {current.get('platformVersion')} -> {platform_version}\n"
        f"  GLOBAL_INIT_SECS: {global_init_secs}",
        flush=True,
    )

    kwargs = dict(
        agentRuntimeId=runtime_id,
        roleArn=current["roleArn"],
        networkConfiguration=current["networkConfiguration"],
        agentRuntimeArtifact=artifact,
        environmentVariables=env,
    )
    if platform_version != "omit":
        kwargs["platformVersion"] = platform_version

    t0 = time.monotonic()
    response = client.update_agent_runtime(**kwargs)
    print(f"  status: {response['status']}", flush=True)

    last = None
    while True:
        status = client.get_agent_runtime(agentRuntimeId=runtime_id)["status"]
        if status != last:
            print(f"    [{time.monotonic() - t0:7.1f}s] {status}", flush=True)
            last = status
        if status == "READY" or status.endswith("FAILED"):
            break
        if time.monotonic() - t0 > TIMEOUT_SEC:
            raise SystemExit(f"timeout, last status={status}")
        time.sleep(5)

    after = client.get_agent_runtime(agentRuntimeId=runtime_id)
    elapsed = round(time.monotonic() - t0, 1)
    print(
        f"  {status} in {elapsed}s  platformVersion={after.get('platformVersion')} "
        f"v={after['agentRuntimeVersion']} arn={after['agentRuntimeArn']}",
        flush=True,
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / "apply_timings.json"
    history = json.loads(path.read_text()) if path.exists() else []
    history.append(
        {
            "label": label,
            "agent_runtime_id": runtime_id,
            "agent_runtime_arn": after["agentRuntimeArn"],
            "old_artifact": old_artifact,
            "new_artifact": new_artifact,
            "requested_platform_version": platform_version,
            "platform_version_after": after.get("platformVersion"),
            "global_init_secs": global_init_secs,
            "final_status": status,
            "ready_elapsed_sec": elapsed,
            "ready_at_epoch": time.time(),
            "agent_runtime_version_after": after["agentRuntimeVersion"],
        }
    )
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    print(f"appended to {path}", flush=True)

    if status != "READY":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
