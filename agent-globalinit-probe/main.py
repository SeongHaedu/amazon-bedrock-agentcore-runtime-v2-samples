# agent-globalinit-probe/main.py
#
# Probe for feeling out the V2 property that startup work is captured in the snapshot. It
# places a deliberate sleep (10 s by default) in both global scope and lazy initialization,
# so a single run shows which of the two stays on the invoke latency.
#
# Expected observations:
#   V2 : the 10 s of global initialization does not appear in any invoke; it was spent while
#        the snapshot was prepared. The 10 s of lazy initialization appears on the first
#        invoke of every session.
#   V1 : with no room left in the pre-warmed pool, both sleeps appear on the invoke.
#
# Calls no LLM, which removes model-side variance.
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone

GLOBAL_INIT_SECS = float(os.environ.get("GLOBAL_INIT_SECS", "10"))
LAZY_INIT_SECS = float(os.environ.get("LAZY_INIT_SECS", "10"))


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"


def _pid1_starttime():
    """Field 22 (index 21) of /proc/1/stat is starttime, in clock ticks since boot. On V2 this
    value matches across sessions because they are restored from the same snapshot."""
    stat = _read("/proc/1/stat")
    if stat.startswith("unavailable"):
        return stat
    return stat.split()[21]


# --- Global scope: on V2, the region that runs once while the snapshot is prepared ---
_t0 = time.time()
print(f"[MODULE_START] {_t0}", flush=True)
time.sleep(GLOBAL_INIT_SECS)

# Captured in the snapshot. Returned in the invoke response so that you can check whether it
# is identical across sessions.
#
# wall_clock is taken here on purpose. On V2 it is pinned to the time the snapshot was built,
# and the gap between it and the invoke grows as the snapshot ages. That is exactly why
# timestamps, credentials, random seeds and established network connections must not be held
# at module scope.
BAKED = {
    "uuid": str(uuid.uuid4()),
    "pid": os.getpid(),
    "wall_clock": datetime.now(timezone.utc).isoformat(),
    "rand": random.random(),
    "global_init_secs": round(time.time() - _t0, 3),
    "monotonic": round(time.monotonic(), 3),
    "pid1_starttime": _pid1_starttime(),
    "uptime": _read("/proc/uptime"),
}
print(f"[MODULE_END] {json.dumps(BAKED)}", flush=True)

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

app = BedrockAgentCoreApp()

# --- Lazy initialization: not captured in the snapshot; runs on the first invoke of each session ---
_lazy = None


@app.entrypoint
def invoke(payload, context):
    started = time.time()
    global _lazy
    lazy_ran = _lazy is None
    if lazy_ran:
        time.sleep(LAZY_INIT_SECS)
        _lazy = {"ready_at": datetime.now(timezone.utc).isoformat()}

    session_id = getattr(context, "session_id", None)
    print(f"[ENTRYPOINT] session_id={session_id} lazy_ran={lazy_ran}", flush=True)
    return {
        "baked": BAKED,
        "now": datetime.now(timezone.utc).isoformat(),
        "now_monotonic": round(time.monotonic(), 3),
        "lazy_ran": lazy_ran,
        "lazy_secs": round(time.time() - started, 3),
        "lazy_ready_at": _lazy["ready_at"],
        "pid_now": os.getpid(),
        "pid1_starttime_now": _pid1_starttime(),
        "uptime_now": _read("/proc/uptime"),
        "session_id": session_id,
    }


if __name__ == "__main__":
    app.run()
