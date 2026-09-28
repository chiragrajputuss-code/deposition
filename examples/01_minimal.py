"""The smallest useful recording: one run, a few steps, a causal link.

No API key, no network, no dependencies. Run it and look at what comes out:

    python examples/01_minimal.py
    deposition verify ./deposition/run_*.jsonl
    deposition view ./deposition/run_*.jsonl
"""

import deposition

deposition.init(project="examples")


@deposition.record(name="minimal-agent")
def run_agent(question: str) -> str:
    # A real LLM call would be recorded automatically; here we write the event
    # ourselves so the example runs with nothing installed.
    thought = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "messages": [{"role": "user", "content": question}],
            "response": "I should look that up.",
            "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
        },
    )

    # caused_by is what makes the trace explain itself: this tool call happened
    # *because* of the model output above.
    with deposition.step("tool_call", tool="search", caused_by=[thought]) as body:
        body["arguments"] = {"query": question}
        body["result"] = ["Refunds are available within 30 days."]

    with deposition.step("decision", label="answer directly", caused_by=[thought]) as body:
        body["detail"] = "the search returned a direct answer"

    return "Refunds are available within 30 days."


if __name__ == "__main__":
    print(run_agent("what is the refund window?"))
    deposition.shutdown()
    print("\nTrace written to ./deposition/ - try `deposition view` on it.")
