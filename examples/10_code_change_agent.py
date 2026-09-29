"""An agent that edits a repository and opens a PR, recorded file by file.

An autonomous coding agent merges a change and production breaks. The pull
request shows the final diff, which is the one thing nobody disputes. What is
missing is everything before it: which files the agent read, what the test run
actually reported, and whether it proceeded past a failure it had already seen.

    python examples/10_code_change_agent.py
    deposition view ./deposition/run_*.jsonl
"""

import deposition

REPO = "example-org/checkout-service"

deposition.init(project="coding-agent", config={"repo": REPO, "autonomy": "open-pr"})


def read_file(path: str) -> str:
    return f"# contents of {path}\n"


def write_file(path: str, patch: str) -> dict:
    return {"path": path, "lines_changed": patch.count("\n")}


def run_tests(selector: str) -> dict:
    """A partial suite that passes while the suite that matters is not run."""
    return {
        "selector": selector,
        "passed": 41,
        "failed": 0,
        "skipped": 6,
        "not_run": ["tests/test_payment_retry.py"],
        "duration_s": 18.2,
    }


@deposition.record(name="refactor-agent")
def apply_change(issue: str) -> str:
    plan = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "messages": [{"role": "user", "content": issue}],
            "response": "Read checkout/retry.py and checkout/client.py, then simplify the "
            "retry backoff.",
            "usage": {"total_tokens": 540},
        },
    )

    for path in ("checkout/retry.py", "checkout/client.py"):
        with deposition.step("tool_call", tool="read_file", caused_by=[plan]) as body:
            body["arguments"] = {"path": path}
            body["result"] = read_file(path)

    edit = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "messages": [{"role": "user", "content": "Produce the patch."}],
            "response": "Replace exponential backoff with a fixed 200ms retry.",
            "usage": {"total_tokens": 880},
            "cost_usd": 0.0096,
        },
        caused_by=[plan],
    )

    with deposition.step("tool_call", tool="write_file", caused_by=[edit]) as body:
        body["arguments"] = {"path": "checkout/retry.py"}
        body["result"] = write_file("checkout/retry.py", "-\n+\n+\n")

    # The test run the agent chose. `not_run` is the field that will matter,
    # and it is recorded because the tool reported it - not because anyone
    # anticipated this incident.
    with deposition.step("tool_call", tool="run_tests", caused_by=[edit]) as body:
        body["arguments"] = {"selector": "tests/unit"}
        results = run_tests("tests/unit")
        body["result"] = results

    with deposition.step("decision", label="open pull request", caused_by=[edit]) as body:
        body["detail"] = "unit tests green; proceeding without the integration suite"
        body["tests_selected"] = "tests/unit"
        body["tests_not_run"] = results["not_run"]
        body["human_approval"] = None

    return "Opened PR #4412: simplify retry backoff"


if __name__ == "__main__":
    print(apply_change("Retry logic is too complicated - simplify it."))
    deposition.shutdown()
    print(
        "\nThe incident question this trace answers: the agent never ran\n"
        "tests/test_payment_retry.py, and recorded that it was choosing not to. The\n"
        "PR is not where that is visible; the trace is. Note human_approval: null."
    )
