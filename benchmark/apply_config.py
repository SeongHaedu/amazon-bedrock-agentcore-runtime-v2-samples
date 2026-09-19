"""Apply artifact, environment variables and platformVersion to an existing runtime in a
single update_agent_runtime call, timing the transition to READY.

Measuring four series (CodeZip / Container x V1 / V2) against the same artifact is easiest
when the same runtime id is switched between platform versions. Changing the artifact, the
environment variables and platformVersion in separate updates bumps the runtime version in
between and moves the conditions, so all three are applied in one update.

usage:
  python benchmark/apply_config.py <agentRuntimeId> <artifact> <V1|V2|omit> <global_init_secs|none> <label>

  <artifact>          S3 prefix for codeConfiguration, container URI for
                      containerConfiguration. Pass keep to leave it as it is.
  <global_init_secs>  Value for GLOBAL_INIT_SECS on agent-bench. none removes the key.
  <label>             Label recorded in results/apply_timings.json.

Environment variables read here, on top of what the runtime already carries:
  BEDROCK_MODEL_ID     Forwarded under the same name. Regions without a us.-prefixed
                       cross-Region inference profile need a Region-local profile.
  AGENTCORE_ENV_EXTRA  "KEY=VALUE,KEY2=VALUE2" for anything else the agent reads.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import RESULTS_DIR, TIMEOUT_SEC, make_client, parse_env_extra  # noqa: E402

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

    # Deep-copy the current artifact definition and replace only the relevant field.
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
    # Carry the current values forward, then layer the caller's settings on top. This is the
    # path for setting BEDROCK_MODEL_ID after the runtime was created.
    env.update(parse_env_extra(os.environ.get("AGENTCORE_ENV_EXTRA")))
    model_id = os.environ.get("BEDROCK_MODEL_ID")
    if model_id:
        env["BEDROCK_MODEL_ID"] = model_id
    if global_init_secs == "none":
        # Remove the key rather than setting it to 0. That restores the agent's own default
        # and makes it possible to tell from get_agent_runtime whether a previous condition
        # is still in place.
        env.pop("GLOBAL_INIT_SECS", None)
    else:
        env["GLOBAL_INIT_SECS"] = str(global_init_secs)

    print(
        f"{runtime_id}\n"
        f"  artifact: {old_artifact}\n"
        f"         -> {new_artifact}\n"
        f"  platformVersion: {current.get('platformVersion')} -> {platform_version}\n"
        f"  GLOBAL_INIT_SECS: {global_init_secs}\n"
        f"  environmentVariables: {sorted(env)}",
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
            "environment_variables": sorted(env),
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
