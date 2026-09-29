"""Recording an agent that handles personal data, without recording the data.

The objection to a verbatim recorder is immediate and correct: an agent that
sees card numbers and medical details would be writing them to disk forever.

`redact` is the answer, and where it runs is the whole point. It is applied to
every event body *before* anything is hashed or written, so the plaintext never
reaches the trace, the blob store, or the hash - and the redacted event is the
one the chain commits to. Nothing has to be scrubbed afterwards, which is good,
because scrubbing an append-only chain is exactly what it is built to prevent.

    python examples/13_pii_redaction.py
    grep -c 4111 ./deposition/run_*.jsonl   # zero hits: the card never landed
"""

import re

import deposition

CARD = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
MRN = re.compile(r"\bMRN[- ]?\d{6,}\b", re.IGNORECASE)


def scrub(value):
    if isinstance(value, str):
        value = CARD.sub("[card]", value)
        value = EMAIL.sub("[email]", value)
        return MRN.sub("[mrn]", value)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def redact(body: dict) -> dict:
    """Called on every event body before it is hashed or written.

    Deliberately conservative: it runs on all bodies, not on a list of fields
    known to be risky, because the field that leaks is always the one nobody
    listed.
    """
    return scrub(body)


deposition.init(project="billing-support", redact=redact)

CUSTOMER_MESSAGE = (
    "Hi, my card 4111 1111 1111 1111 was charged twice. "
    "Email me at jane.doe@example.com. My record is MRN-449182."
)


@deposition.record(name="billing-agent")
def handle(message: str) -> str:
    reply = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "messages": [{"role": "user", "content": message}],
            "response": "I can see a duplicate charge. Refunding the second one now.",
            "usage": {"total_tokens": 260},
        },
    )

    with deposition.step("tool_call", tool="refund_duplicate", caused_by=[reply]) as body:
        body["arguments"] = {"customer_email": "jane.doe@example.com", "amount_usd": 42.00}
        body["result"] = {"refund_id": "rfnd_9912", "notified": "jane.doe@example.com"}

    return "Refunded the duplicate charge."


if __name__ == "__main__":
    print(handle(CUSTOMER_MESSAGE))
    deposition.shutdown()
    print(
        "\nOpen the trace: the card number, the email and the MRN are gone, replaced\n"
        "at the point of recording. The chain still verifies, because it committed to\n"
        "the redacted event - there is no earlier version of it anywhere."
    )
