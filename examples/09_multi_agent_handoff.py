"""A planner, a researcher and a writer in one run - and which of them was wrong.

Multi-agent systems fail in a characteristic way: the output is wrong, every
individual agent looks like it did its job, and nobody can say where the error
entered. The answer is almost always a handoff - one agent's hedged finding
becoming the next agent's stated fact.

Note the shape here: **one run, with each sub-agent's work recorded as steps
inside it**. Sub-agents are not separate recorded runs. Keeping the handoff and
the work either side of it on a single chain is what lets you walk the error
backwards across an agent boundary.

    python examples/09_multi_agent_handoff.py
    deposition view ./deposition/run_*.jsonl
"""

import deposition

deposition.init(project="research-desk")

SOURCES = {
    "s1": "Market grew an estimated 11-18% in 2025 (preliminary, unaudited).",
    "s2": "Industry body declined to publish a 2025 figure pending revisions.",
}


def agent_turn(agent: str, prompt: str, response, tokens: int, caused_by=None) -> int:
    """One sub-agent's model call, tagged with which agent made it.

    The tag lives in the event body rather than in the run, so the whole
    collaboration stays on one chain and `caused_by` can cross agents.
    """
    return deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "agent": agent,
            "messages": [{"role": "user", "content": prompt}],
            "response": response,
            "usage": {"total_tokens": tokens},
        },
        caused_by=caused_by,
    )


@deposition.record(name="research-desk")
def produce_brief(topic: str) -> str:
    plan = agent_turn(
        "planner",
        f"Break '{topic}' into research tasks.",
        "1. Find 2025 market growth. 2. Draft a one-paragraph brief.",
        180,
    )

    with deposition.step("retrieval", caused_by=[plan]) as body:
        body["query"] = "2025 market growth rate"
        body["source"] = "internal-sources"
        body["doc_ids"] = list(SOURCES)
        body["passages"] = SOURCES

    # The researcher is careful: it reports a range and says it is preliminary.
    research = agent_turn(
        "researcher",
        f"What was 2025 growth? Sources: {SOURCES}",
        "Preliminary estimates put 2025 growth between 11% and 18%; unaudited, "
        "and the industry body has not published a figure.",
        420,
        caused_by=[plan],
    )

    # The handoff. This is the event that will matter in the post-mortem: the
    # hedge is dropped here, not by the researcher who wrote it and not by the
    # writer who received it already flattened.
    handoff = deposition.log(
        "decision",
        {
            "label": "handoff to writer",
            "detail": "researcher's range summarised for drafting",
            "from_agent": "researcher",
            "to_agent": "writer",
            "passed_as": "2025 growth was about 15%",
            "dropped": ["range 11-18%", "preliminary/unaudited", "no official figure"],
        },
        caused_by=[research],
    )

    draft = agent_turn(
        "writer",
        "Write the brief. Finding: 2025 growth was about 15%.",
        "The market grew 15% in 2025, continuing its expansion.",
        260,
        caused_by=[handoff],
    )

    with deposition.step("decision", label="brief published", caused_by=[draft]) as body:
        body["detail"] = "single stated figure, no uncertainty language"

    return "The market grew 15% in 2025, continuing its expansion."


if __name__ == "__main__":
    print(produce_brief("2025 market growth"))
    deposition.shutdown()
    print(
        "\nThe post-mortem this trace settles: the researcher was not wrong. An\n"
        "11-18% preliminary range became a flat '15%' at the handoff, and the brief\n"
        "is faithful to what the writer was given. Walk caused_by back from the\n"
        "published brief and the error has an event number."
    )
