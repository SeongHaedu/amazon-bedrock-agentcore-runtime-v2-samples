# agent-basic/main.py
#
# Smallest agent that shows the difference between platformVersion V1 and V2, built on
# Strands Agents the same way agent-bench is.
#
# The Strands agent is constructed at module scope, so it is part of what the snapshot
# captures on V2 and part of what every new execution environment repeats on V1. The model is
# only called when the payload carries "query"; the default invoke takes the no-LLM path, so
# the latency of a plain invoke stays free of model-side variance.
#
# Three markers go to stdout, so the V1 / V2 difference reads directly in CloudWatch Logs:
#   [MODULE_START] / [MODULE_END] : module scope (global scope) execution
#   [ENTRYPOINT]                  : request handler execution
#
# On V1, MODULE_START / MODULE_END appear once per new execution environment.
# On V2 they appear once, while the snapshot is prepared on create or update, and not in the
# logs of an invoke.
import json
import os
import time
import uuid
from datetime import datetime, timezone

MODULE_START = time.time()
print(f"[MODULE_START] {MODULE_START}", flush=True)

from strands import Agent  # noqa: E402
from strands.models.bedrock import BedrockModel  # noqa: E402
from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

app = BedrockAgentCoreApp()

# ap-northeast-1 has no cross-Region inference profile with the us. prefix. Pass a
# Region-local profile through BEDROCK_MODEL_ID (a jp.-prefixed profile, for example).
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
agent = Agent(
    name="basic-agent",
    model=BedrockModel(model_id=MODEL_ID),
    system_prompt="Answer in three sentences or fewer.",
)

# Captured in the snapshot. On V2 every restored instance shares this same value, and the
# invoke response is where you can confirm it is not regenerated per restore.
SNAPSHOT_IDENTITY = {
    "uuid": str(uuid.uuid4()),
    "pid": os.getpid(),
    "module_wall_clock": datetime.now(timezone.utc).isoformat(),
}

MODULE_END = time.time()
print(
    f"[MODULE_END] {MODULE_END} "
    f"elapsed_ms={(MODULE_END - MODULE_START) * 1000:.1f} "
    f"model_id={MODEL_ID} "
    f"snapshot_identity={json.dumps(SNAPSHOT_IDENTITY)}",
    flush=True,
)


@app.entrypoint
def invoke(payload, context):
    print(
        f"[ENTRYPOINT] {time.time()} session_id={getattr(context, 'session_id', None)}",
        flush=True,
    )

    # Only a payload carrying "query" reaches the model. Every other payload keeps the
    # no-LLM path, so a plain invoke stays comparable across V1 and V2.
    query = payload.get("query") if isinstance(payload, dict) else None
    answer = None
    if query:
        try:
            answer = str(agent(query))
        except Exception as exc:  # noqa: BLE001
            # A missing Region-local inference profile or a denied InvokeModel shows up here.
            # Report it in the response instead of failing the whole invoke.
            answer = f"model call failed: {type(exc).__name__}: {exc}"
        print(f"[ANSWERED] {time.time()} model_id={MODEL_ID}", flush=True)

    return {
        # On V2 this value matches across sessions. On V1 it differs per session.
        "snapshot_identity": SNAPSHOT_IDENTITY,
        # Anything that has to change per request is generated in the handler.
        "request_uuid": str(uuid.uuid4()),
        "now": datetime.now(timezone.utc).isoformat(),
        "answer": answer,
        "model_id": MODEL_ID if query else None,
        "echo": payload,
        "session_id": getattr(context, "session_id", None),
    }


if __name__ == "__main__":
    app.run()
