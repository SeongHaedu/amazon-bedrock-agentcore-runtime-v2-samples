English | [Japanese](README_JA.md)

# Amazon Bedrock AgentCore Runtime V2: platformVersion Samples

Sample code for trying out platform version V2 of Amazon Bedrock AgentCore Runtime. V2 starts your agent by restoring a snapshot, which keeps cold starts fast and consistent regardless of concurrency or image size. You select it per agent runtime with the `platformVersion` field (`V1` or `V2`); V1 is the default.

These scripts let you create a V2 runtime, migrate an existing V1 runtime to V2, confirm the platform version, invoke the runtime, and observe how V2 changes where your startup code runs.

Blog post (Japanese): TBD

## Structure

```
.
├── agent-basic/                 Minimal agent. Prints module-scope and handler markers.
│   ├── main.py
│   └── Dockerfile
├── agent-globalinit-probe/      Probe agent. Global init vs lazy init, 10 s each.
│   ├── main.py
│   └── Dockerfile
├── scripts/
│   ├── common.py                Shared config and helpers. Reads all settings from env vars.
│   ├── check_sdk_version.py     Verify the installed SDK supports platformVersion. No AWS calls.
│   ├── setup_codezip_artifact.py Build an ARM64 ZIP and upload it to S3 (no Docker needed).
│   ├── create_runtime.py        Create a runtime with V1 / V2 / omitted, timing READY.
│   ├── switch_platform_version.py Move an existing runtime between V1 and V2.
│   ├── get_runtime.py           Read platformVersion via get_agent_runtime.
│   ├── invoke_runtime.py        Invoke with fresh sessions and compare against session reuse.
│   ├── probe_globalinit.py      Burst-invoke the probe agent to see where startup work lands.
│   └── cleanup_runtimes.py      Delete only runtimes matching the name prefix.
└── requirements.txt             boto3>=1.43.95
```

## Prerequisites

- Python 3.10+
- `boto3>=1.43.95`. This is the first public release that carries the `platformVersion` field. Earlier versions reject the request before sending it, with `ParamValidationError`.
- An AgentCore Runtime execution role
- A Region where V2 is available: `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `ap-northeast-1`
- For the container path: Docker with `docker buildx` (AgentCore Runtime microVMs run ARM64 Linux) and an ECR repository
- For the direct code deployment path: an existing S3 bucket

> Note: AWS CloudFormation and the AWS CDK do not currently support setting `platformVersion`. Use the AWS SDK, the CLI, or the console.

## Setup

```bash
git clone https://github.com/SeongHaedu/amazon-bedrock-agentcore-runtime-v2-samples.git
cd amazon-bedrock-agentcore-runtime-v2-samples

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

All commands below are run from the repository root.

## Environment variables

```bash
export AWS_REGION=ap-northeast-1
export AGENTCORE_ROLE_ARN=arn:aws:iam::<account-id>:role/<execution-role>

# Container path
export AGENTCORE_CONTAINER_URI=<account-id>.dkr.ecr.$AWS_REGION.amazonaws.com/<repository>:v2sample

# Direct code deployment path
export AGENTCORE_S3_BUCKET=<bucket-name>
export AGENTCORE_S3_PREFIX=agentcore/codezip/agent.zip   # optional, this is the default

# Optional
export AWS_PROFILE=<profile>
export AGENTCORE_NAME_PREFIX=v2sample_                   # cleanup_runtimes.py deletes only this prefix
```

## Step 1: Check your SDK

```bash
python scripts/check_sdk_version.py
```

This makes no AWS calls. It reads the installed botocore service model and reports whether `platformVersion` is present on `CreateAgentRuntime` / `UpdateAgentRuntime` inputs and the `GetAgentRuntime` output. `platformVersion` is response-only on `GetAgentRuntime`, so its absence from that operation's input is expected.

## Step 2: Build an artifact

### Option A: container

```bash
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin \
    $(echo $AGENTCORE_CONTAINER_URI | cut -d/ -f1)

docker buildx build --platform linux/arm64 \
  -t $AGENTCORE_CONTAINER_URI \
  --push agent-basic/
```

### Option B: direct code deployment (no Docker)

```bash
python scripts/setup_codezip_artifact.py agent-basic
```

This vendors dependencies for `manylinux2014_aarch64`, zips them together with `main.py`, and uploads the archive to `s3://$AGENTCORE_S3_BUCKET/$AGENTCORE_S3_PREFIX`.

## Step 3: Create a V2 runtime

```bash
python scripts/create_runtime.py basic_v2 container V2
```

Replace `container` with `codezip` if you built the ZIP. The third argument is the platform version: `V2`, `V1`, or `omit`.

A V2 create prepares and snapshots your environment, so it takes minutes rather than seconds. The script polls `get_agent_runtime` until the status is `READY` or ends in `FAILED`, and prints each status transition with its elapsed time.

To see the difference for yourself, create a V1 runtime from the same artifact:

```bash
python scripts/create_runtime.py basic_v1 container omit
```

## Step 4: Confirm the platform version

```bash
python scripts/get_runtime.py
```

`create_agent_runtime` and `update_agent_runtime` do not return `platformVersion`; `get_agent_runtime` does. `list_agent_runtimes` does not return it either, so this script calls `get_agent_runtime` per runtime.

Write your own checks as `resp.get("platformVersion", "V1")`. V1 is the default.

## Step 5: Migrate V1 to V2

```bash
python scripts/switch_platform_version.py <agentRuntimeId> V2
```

`update_agent_runtime` requires `roleArn` and `agentRuntimeArtifact`, so the script reads the current values with `get_agent_runtime` and passes them through. It also carries `environmentVariables` forward explicitly, because the documentation does not state whether omitting them clears the existing values.

Two behaviors are worth seeing directly.

- `V2` -> `V1` completes in seconds. No snapshot preparation is needed.
- Omitting `platformVersion` on a runtime that is already V2 keeps it on V2 and still prepares a snapshot. Ordinary updates such as swapping the artifact therefore also take minutes on V2.

```bash
python scripts/switch_platform_version.py <agentRuntimeId> V1     # seconds
python scripts/switch_platform_version.py <agentRuntimeId> omit   # minutes, stays on V2
```

The runtime must be in a terminal state (`READY` or `*_FAILED`) before you call update. Calling it during `CREATING` / `UPDATING` / `DELETING` returns `ConflictException`.

## Step 6: Invoke

```bash
python scripts/invoke_runtime.py <agentRuntimeId> 3 2
```

The arguments are the number of sessions and the number of invokes per session. Each session uses a fresh `runtimeSessionId`, so every first invoke goes through the startup path. The second invoke in the same session lands on the existing environment, which gives you the floor for a request that includes no startup work.

`agent-basic` returns a `snapshot_identity` object built at module scope. Watch the `distinct identity uuid` line the script prints at the end:

- On V2 it is 1, however many sessions you created. Every instance was restored from the same snapshot.
- On V1 it matches the number of new execution environments. Each one ran module scope itself.

## Step 7: See where your startup work runs

This is the part that changes how you write agent code. `agent-globalinit-probe` sleeps 10 seconds at module scope and another 10 seconds on the first request of each session.

```bash
docker buildx build --platform linux/arm64 \
  -t <account-id>.dkr.ecr.$AWS_REGION.amazonaws.com/<repository>:probe \
  --push agent-globalinit-probe/

AGENTCORE_CONTAINER_URI=<account-id>.dkr.ecr.$AWS_REGION.amazonaws.com/<repository>:probe \
  python scripts/create_runtime.py probe_v2 container V2

python scripts/probe_globalinit.py <agentRuntimeId> 20
```

On V2, the 10 seconds of global initialization does not appear in any invoke. It was spent once while the snapshot was prepared. The 10 seconds of lazy initialization appears on the first invoke of every session, because the snapshot cannot carry it.

Do the same against a V1 runtime built from the same image and send enough concurrent sessions to exhaust the pre-warmed instances. There the global initialization does show up on the request path.

Note what else the probe reports. `baked.wall_clock` is the time at which module scope ran, so on V2 the gap between it and `now` grows as the snapshot ages. That is the concrete reason not to hold timestamps, credentials, random seeds, or established connections at module scope on V2. See [Optimize your agent for Amazon Bedrock AgentCore Runtime V2](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html).

## Cleanup

```bash
python scripts/cleanup_runtimes.py          # list the targets only
python scripts/cleanup_runtimes.py --yes    # actually delete
```

Only runtimes whose name starts with `AGENTCORE_NAME_PREFIX` (default `v2sample_`) are deleted. Without `--yes` the script prints the list and exits.

Deletion of the runtime itself takes seconds even on V2. The underlying snapshot can take up to 8 hours to disappear, because sessions already running on it continue until they end. That is the maximum session lifetime.

## Notes and current limits

- V2 limits the total size of your agent's environment variables to 1.5 KB for direct code deployments and 2.5 KB for container agents, against 4 KB on V1. Exceeding it fails with `ValidationException`. The documentation states this limit will be raised to match V1.
- Your container must report healthy from `/ping` within 120 seconds of startup, or creation fails with a health check error. With the AgentCore SDK you get this for free: the server does not listen until `app.run()`, so the snapshot captures a fully initialized agent by construction.
- If you run your own HTTP server instead of the AgentCore SDK, report healthy only after initialization completes.
- Bring your own cryptographic libraries in a container? Use snapshot-safe builds so they reseed after a restore. On Amazon Linux 2023, use `openssl-snapsafe-libs`. The service-managed base image for direct code deployments already includes snapshot-safe builds.

## References

- [microVMs — Platform versions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html#runtime-platform-versions)
- [Optimize your agent for Amazon Bedrock AgentCore Runtime V2](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html)
- [Host agent or tools with Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html)
- [The new AgentCore runtime: Elastic, optimized, and consistently fast starts](https://aws.amazon.com/blogs/machine-learning/the-new-agentcore-runtime-elastic-optimized-and-consistently-fast-starts/)
- [create_agent_runtime](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_agent_runtime.html) / [update_agent_runtime](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/update_agent_runtime.html) / [get_agent_runtime](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/get_agent_runtime.html)
