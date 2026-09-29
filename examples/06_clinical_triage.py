"""A triage agent that must escalate, and a record of exactly when it could have.

A symptom-intake agent talks to a patient, and somewhere in the conversation a
red-flag symptom appears. The clinical safety question is never "did it escalate"
- the log already says that. It is "how many turns passed between the red flag
being visible and the escalation firing, and what did the agent do in between?"

That question needs ordering and causality, which is what the chain gives you:
every event has a sequence number nothing can quietly move.

    python examples/06_clinical_triage.py
    deposition view ./deposition/run_*.jsonl
"""

import deposition

RED_FLAGS = {"chest pain", "shortness of breath", "fainting"}

CONVERSATION = [
    "I've had a headache since yesterday.",
    "It's worse when I stand up. I also felt some chest pain this morning.",
    "No, I haven't taken anything for it.",
]

deposition.init(project="clinical-triage", config={"protocol": "intake-v3"})


def screen(utterance: str) -> list[str]:
    return sorted(flag for flag in RED_FLAGS if flag in utterance.lower())


@deposition.record(name="triage-agent")
def intake(conversation: list[str]) -> str:
    flagged_at: int | None = None

    for turn, utterance in enumerate(conversation):
        # The screening step runs on every turn and records what it found,
        # including when it found nothing. An absence recorded is evidence; an
        # absence not recorded is just a gap.
        with deposition.step("tool_call", tool="red_flag_screen") as body:
            body["arguments"] = {"turn": turn, "utterance": utterance}
            found = screen(utterance)
            body["result"] = {"red_flags": found}

        if found and flagged_at is None:
            flagged_at = deposition.log(
                "decision",
                {
                    "label": "red flag detected",
                    "detail": f"{', '.join(found)} reported at turn {turn}",
                    "severity": "high",
                },
            )

        reply = deposition.log(
            "llm_call",
            {
                "provider": "example",
                "gen_ai.request.model": "pretend-model-1",
                "messages": [{"role": "user", "content": utterance}],
                "response": "Noted. Have you taken any medication for it?",
                "usage": {"total_tokens": 210},
            },
        )

        # This is the part that matters in review. The agent asked one more
        # routine question *after* chest pain was on the record, and the trace
        # shows it as an event between the flag and the escalation rather than
        # letting it disappear into a summary.
        if flagged_at is not None and turn < len(conversation) - 1:
            deposition.log(
                "annotation",
                {
                    "note": "routine question asked while a red flag was already open",
                    "author": "safety-monitor",
                },
                caused_by=[flagged_at, reply],
            )

    if flagged_at is not None:
        with deposition.step(
            "decision", label="escalate to clinician", caused_by=[flagged_at]
        ) as body:
            body["detail"] = "red flag present; automated triage stopped"
            body["route"] = "urgent-care-callback"
            body["turns_after_flag"] = 1
        return "Escalated to a clinician."

    return "Routine advice given."


if __name__ == "__main__":
    print(intake(CONVERSATION))
    deposition.shutdown()
    print(
        "\nThe safety-review question this trace answers: chest pain was visible at\n"
        "turn 1, escalation fired one turn later, and the trace names the event that\n"
        "happened in between. Walk `caused_by` back from the escalation in the viewer."
    )
