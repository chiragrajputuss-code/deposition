"""The same task run twice, so two traces can be compared.

Agents are not deterministic, and the interesting failures are the ones that
happen on one run and not the next. `deposition diff` aligns two runs by their
steps and names the first place they diverge - which is how you tell "the model
answered differently" from "the agent took a different path".

This run also shows a provider failure and a fallback to a cheaper model, which
is where cost and quality regressions usually hide.

    python examples/11_model_fallback_diff.py
    deposition diff ./deposition/run_*.jsonl   # pass the two files it prints
"""

import deposition

deposition.init(project="fallback-demo")

PRIMARY = "pretend-model-large"
FALLBACK = "pretend-model-small"


class ProviderDown(RuntimeError):
    pass


def call_model(model: str, fail_primary: bool):
    if model == PRIMARY and fail_primary:
        raise ProviderDown("503 from provider")
    return {
        PRIMARY: ("Refund approved under clause 4.2.", 1200, 0.0140),
        FALLBACK: ("Refund approved.", 310, 0.0007),
    }[model]


@deposition.record(name="fallback-agent")
def run_task(question: str, *, fail_primary: bool) -> str:
    for attempt, model in enumerate((PRIMARY, FALLBACK)):
        try:
            with deposition.step("llm_call", **{"gen_ai.request.model": model}) as body:
                body["provider"] = "example"
                body["messages"] = [{"role": "user", "content": question}]
                body["attempt"] = attempt
                text, tokens, cost = call_model(model, fail_primary)
                body["response"] = text
                body["usage"] = {"total_tokens": tokens}
                body["cost_usd"] = cost
        except ProviderDown:
            # The exception is already on the failed step, recorded by `step`
            # before it re-raised. The fallback below is a separate event, so
            # the trace shows a degraded run rather than a clean one.
            deposition.log(
                "annotation",
                {"note": f"{model} unavailable; falling back", "author": "runtime"},
            )
            continue

        with deposition.step("decision", label="answered") as body:
            body["detail"] = f"served by {model}"
            body["degraded"] = model != PRIMARY
        return text

    raise RuntimeError("all models failed")


if __name__ == "__main__":
    print("healthy run: ", run_task("Approve this refund?", fail_primary=False))
    print("degraded run:", run_task("Approve this refund?", fail_primary=True))
    deposition.shutdown()
    print(
        "\nTwo traces were written. Diff them:\n"
        "  deposition diff ./deposition/<first>.jsonl ./deposition/<second>.jsonl\n\n"
        "The divergence is not the wording of the answer. It is that the degraded run\n"
        "was served by a smaller model at 1/20th the cost, and dropped the clause\n"
        "citation the primary model gave - the kind of quality regression that never\n"
        "shows up in an error rate."
    )
