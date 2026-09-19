# agent-bench/main.py
#
# Target agent for cold start measurement. To compare the pre-entrypoint of V1 and V2 under
# identical conditions, it emits markers that let the client-side clock be joined with the
# server-side clock.
#
#   [MODULE_START] / [MODULE_END] : module scope (global scope) execution
#   [ENTRYPOINT_REACHED]          : first line of the request handler; the end of pre-entrypoint
#   [FIRST_TOKEN]                 : the time the first delta arrived from the model
#
# GLOBAL_INIT_SECS inserts a sleep of that many seconds at module scope, to stand in for an
# agent with heavy initialization. On V1 the sleep lands on the request path; on V2 it is
# spent once while the snapshot is prepared. The default is 0, which inserts nothing.
#
# The model response is streamed with stream_async so that the client can measure when the
# first byte arrives.
import os
import time

_module_start = time.time()
print(f"[MODULE_START] {_module_start}", flush=True)

from strands import Agent  # noqa: E402
from strands.models.bedrock import BedrockModel  # noqa: E402
from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

# Stands in for a heavy module-scope initialization.
# On V1 it runs on every execution environment start; on V2 once, while the snapshot is prepared.
GLOBAL_INIT_SECS = float(os.environ.get("GLOBAL_INIT_SECS", "0"))
if GLOBAL_INIT_SECS > 0:
    print(f"[GLOBAL_INIT_START] {time.time()} secs={GLOBAL_INIT_SECS}", flush=True)
    time.sleep(GLOBAL_INIT_SECS)
    print(f"[GLOBAL_INIT_END] {time.time()}", flush=True)

app = BedrockAgentCoreApp()

# ap-northeast-1 has no cross-Region inference profile with the us. prefix. Measuring in such
# a Region means passing a Region-local profile through BEDROCK_MODEL_ID (a jp.-prefixed
# profile, for example).
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
model = BedrockModel(model_id=MODEL_ID)
agent = Agent(
    name="bench-agent",
    model=model,
    system_prompt="You are a benchmark agent. Follow the instruction exactly and output nothing else.",
)

# A prompt with a fixed output length, so that generation time can be treated as a constant.
FIXED_PROMPT = "List the integers from 1 to 40 separated by commas."

_module_end = time.time()
print(
    f"[MODULE_END] {_module_end} elapsed_ms={(_module_end - _module_start) * 1000:.1f} "
    f"model_id={MODEL_ID} global_init_secs={GLOBAL_INIT_SECS}",
    flush=True,
)


@app.entrypoint
async def invoke(payload, context):
    # The time of this print is the end of pre-entrypoint; its distance from the client's
    # dispatched_at is the pre-entrypoint duration. session_id is included so that the record
    # can be joined with the client-side one.
    entrypoint_ts = time.time()
    session_id = getattr(context, "session_id", None)
    print(f"[ENTRYPOINT_REACHED] {entrypoint_ts} session_id={session_id}", flush=True)

    if payload.get("type") == "warmup":
        # warmup sentinel: return immediately without calling the LLM.
        yield {"status": "warm"}
        return

    first_token_emitted = False
    async for event in agent.stream_async(FIXED_PROMPT):
        # Strands yields text deltas as {"data": "..."}. Tool-use and metadata events are not
        # forwarded to the client.
        if isinstance(event, dict) and event.get("data"):
            if not first_token_emitted:
                print(f"[FIRST_TOKEN] {time.time()} session_id={session_id}", flush=True)
                first_token_emitted = True
            yield event["data"]

    if not first_token_emitted:
        # Keep the response readable for the client even when no delta was produced.
        print(f"[NO_TOKEN] {time.time()} session_id={session_id}", flush=True)
        yield ""


if __name__ == "__main__":
    app.run()
