"""An agent that fails, and a trace that shows why.

A tool raises, the agent retries with different arguments, and the whole causal
path is recorded. This is the run Deposition exists for: open it in the viewer
and walk `why this happened` backwards from the error.

    python examples/02_tool_loop.py
    deposition view ./deposition/run_*.jsonl
"""

import random

import deposition

deposition.init(project="examples")

INVENTORY = {"widget": 3, "gizmo": 0}


def check_stock(item: str) -> int:
    if item not in INVENTORY:
        raise KeyError(f"unknown item {item!r}")
    return INVENTORY[item]


@deposition.record(name="order-agent")
def run_agent(order: str) -> str:
    plan = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "messages": [{"role": "user", "content": f"Can we fulfil: {order}?"}],
            "response": "Call check_stock('widgit').",
            "usage": {"total_tokens": 31},
        },
    )

    # First attempt: the model hallucinated the item name. The exception is
    # recorded on the step and then handled, exactly as a real agent would.
    try:
        with deposition.step("tool_call", tool="check_stock", caused_by=[plan]) as body:
            body["arguments"] = {"item": "widgit"}
            body["result"] = check_stock("widgit")
    except KeyError:
        pass

    retry = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "messages": [{"role": "user", "content": "That item does not exist. Try again."}],
            "response": "Call check_stock('widget').",
            "usage": {"total_tokens": 44},
        },
    )

    with deposition.step("tool_call", tool="check_stock", caused_by=[retry]) as body:
        body["arguments"] = {"item": "widget"}
        stock = check_stock("widget")
        body["result"] = stock

    with deposition.step("decision", label="accept order", caused_by=[retry]) as body:
        body["detail"] = f"{stock} in stock"

    return f"Order accepted ({stock} in stock)."


if __name__ == "__main__":
    random.seed(0)
    print(run_agent("2 widgets"))
    deposition.shutdown()
    print("\nTrace written to ./deposition/ - open the failed step in the viewer.")
