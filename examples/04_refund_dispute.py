"""A support agent that moves money, and the trace you produce when it is disputed.

The scenario: six weeks after the fact, a customer insists they were promised a
full refund and a chargeback lands. Someone has to answer, from the record:
what did the agent actually decide, what policy did it read, and what did the
model see before it decided?

This example also signs the trace, because a refund record that its own holder
can rewrite is not worth much in a chargeback file.

    pip install 'deposition[signing]'
    python examples/04_refund_dispute.py
    deposition verify ./deposition/run_*.jsonl --pubkey ./deposition/refunds.pem.pub
    deposition view ./deposition/run_*.jsonl
"""

from pathlib import Path

import deposition

POLICY_VERSION = "refunds-2026.09"
TRACE_DIR = Path("./deposition")
KEY_PATH = TRACE_DIR / "refunds.pem"

ORDER = {
    "id": "ord_88213",
    "total_usd": 412.50,
    "delivered_on": "2026-08-02",
    "category": "electronics",
    "customer_tier": "standard",
}


def refund_policy(order: dict) -> dict:
    """The rule the decision has to be defensible against."""
    days_since = 58  # pretend today is 2026-09-29
    return {
        "version": POLICY_VERSION,
        "window_days": 30,
        "days_since_delivery": days_since,
        "within_window": days_since <= 30,
        "max_goodwill_usd": 50.00,
    }


def issue_refund(order_id: str, amount_usd: float) -> dict:
    return {"refund_id": "rfnd_4471", "order_id": order_id, "amount_usd": amount_usd}


def signing_key() -> Path | None:
    """Generate a key on first run so the example is one command, not three.

    A real deployment would never keep the key beside the traces it signs - see
    ADR 005. Here it is a demonstration, not a recommendation.
    """
    try:
        from deposition.signing import generate_key, load_key
    except ImportError:
        return None
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        load_key(KEY_PATH)
        return KEY_PATH
    return generate_key().write(KEY_PATH)


deposition.init(
    project="support-refunds",
    signing_key=signing_key(),
    config={"policy_version": POLICY_VERSION},
)


@deposition.record(name="refund-agent")
def handle_request(message: str) -> str:
    # The policy lookup is recorded as its own step. Six weeks from now this is
    # the event that answers "which version of the rules was in force?".
    with deposition.step("tool_call", tool="refund_policy") as body:
        body["arguments"] = {"order_id": ORDER["id"]}
        policy = refund_policy(ORDER)
        body["result"] = policy

    read = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "messages": [
                {"role": "system", "content": f"Refund policy {POLICY_VERSION}: {policy}"},
                {"role": "user", "content": message},
            ],
            "response": (
                "Order is 58 days old, outside the 30-day window. Full refund not "
                "authorised. Goodwill credit up to $50 is available."
            ),
            "usage": {"total_tokens": 388},
            "cost_usd": 0.0041,
        },
    )

    # The decision is its own event, with the number in it. A summary written
    # into a chat log is not evidence; this is.
    with deposition.step("decision", label="partial refund", caused_by=[read]) as body:
        body["detail"] = "outside 30-day window; goodwill credit at policy maximum"
        body["requested_usd"] = ORDER["total_usd"]
        body["approved_usd"] = 50.00
        body["policy_version"] = POLICY_VERSION

    with deposition.step("tool_call", tool="issue_refund", caused_by=[read]) as body:
        body["arguments"] = {"order_id": ORDER["id"], "amount_usd": 50.00}
        body["result"] = issue_refund(ORDER["id"], 50.00)

    return "Issued a $50 goodwill credit; the order is outside the 30-day refund window."


if __name__ == "__main__":
    print(handle_request("I want my money back for order 88213, all $412.50 of it."))
    deposition.shutdown()
    print(
        "\nThe dispute question this trace answers: the agent approved $50, not $412.50,\n"
        "under policy refunds-2026.09, because the tool it called reported 58 days.\n"
        "Verify it with the public key and the record stands even if the file was moved:\n"
        "  deposition verify ./deposition/run_*.jsonl --pubkey ./deposition/refunds.pem.pub"
    )
