# agent-basic/main.py
#
# Smallest agent that shows the difference between platformVersion V1 and V2. It calls no
# LLM, which removes model-side variance and leaves only the startup semantics.
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

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

app = BedrockAgentCoreApp()

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
    f"snapshot_identity={json.dumps(SNAPSHOT_IDENTITY)}",
    flush=True,
)


@app.entrypoint
def invoke(payload, context):
    print(
        f"[ENTRYPOINT] {time.time()} session_id={getattr(context, 'session_id', None)}",
        flush=True,
    )
    return {
        # On V2 this value matches across sessions. On V1 it differs per session.
        "snapshot_identity": SNAPSHOT_IDENTITY,
        # Anything that has to change per request is generated in the handler.
        "request_uuid": str(uuid.uuid4()),
        "now": datetime.now(timezone.utc).isoformat(),
        "echo": payload,
        "session_id": getattr(context, "session_id", None),
    }


if __name__ == "__main__":
    app.run()
