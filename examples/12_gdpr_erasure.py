"""An agent that deletes personal data, and proof of exactly what it deleted.

An erasure request is irreversible and legally binding in both directions: you
must delete what was asked for, and you must be able to show later that you
deleted it - and that you did not delete something you were still required to
keep.

The agent cannot produce that proof afterwards by inspecting the database,
because the evidence is precisely what is no longer there. It has to be recorded
as the work happens.

    python examples/12_gdpr_erasure.py
    deposition verify ./deposition/run_*.jsonl
"""

import deposition

SUBJECT = {"id": "usr_31882", "email": "a***@example.com", "verified": True}

RECORDS = {
    "profiles": ["usr_31882"],
    "support_tickets": ["tkt_9001", "tkt_9114"],
    "order_history": ["ord_4410", "ord_5521"],
    "audit_log": ["evt_77120", "evt_77121"],
}

# Financial records carry a statutory retention period that outlives the erasure
# right. Recording the exemption is as important as recording the deletion.
EXEMPT = {"order_history": "retained 7 years under tax law", "audit_log": "security records"}

deposition.init(project="privacy-ops", config={"request_type": "gdpr-17-erasure"})


def locate(subject_id: str) -> dict:
    return RECORDS


def delete(store: str, ids: list[str]) -> dict:
    return {"store": store, "deleted": len(ids), "ids": ids}


@deposition.record(name="erasure-agent")
def erase(subject: dict) -> dict:
    # Identity verification first. If this event is missing from the trace, the
    # deletion cannot be defended at all, whatever else the log says.
    with deposition.step("decision", label="identity verified") as body:
        body["detail"] = "verified by signed email link before any destructive step"
        body["subject_id"] = subject["id"]
        body["verified"] = subject["verified"]

    with deposition.step("tool_call", tool="locate_records") as body:
        body["arguments"] = {"subject_id": subject["id"]}
        located = locate(subject["id"])
        body["result"] = located

    plan = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "messages": [{"role": "user", "content": f"Erasure plan for {located}"}],
            "response": "Delete profiles and support_tickets. Retain order_history and "
            "audit_log under statutory exemptions.",
            "usage": {"total_tokens": 470},
        },
    )

    deleted, retained = {}, {}
    for store, ids in located.items():
        if store in EXEMPT:
            # A refusal to delete is a decision with legal consequences, so it
            # gets an event of its own rather than being silently skipped.
            with deposition.step(
                "decision", label="retained under exemption", caused_by=[plan]
            ) as body:
                body["detail"] = EXEMPT[store]
                body["store"] = store
                body["record_ids"] = ids
            retained[store] = ids
            continue

        with deposition.step("tool_call", tool="delete_records", caused_by=[plan]) as body:
            body["arguments"] = {"store": store, "ids": ids}
            body["result"] = delete(store, ids)
            deleted[store] = ids

    with deposition.step("decision", label="erasure complete", caused_by=[plan]) as body:
        body["detail"] = "response to data subject due within 30 days"
        body["deleted_record_ids"] = deleted
        body["retained_record_ids"] = retained

    return {"deleted": deleted, "retained": retained}


if __name__ == "__main__":
    print(erase(SUBJECT))
    deposition.shutdown()
    print(
        "\nThe compliance question this trace answers: four records were deleted, four\n"
        "were retained under a named exemption, identity was verified before any of it,\n"
        "and the ids are in the record - which the database can no longer tell you."
    )
