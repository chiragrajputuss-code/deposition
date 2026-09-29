"""A RAG agent whose answer is challenged, and the retrieval that produced it.

Someone acts on an agent's reading of a contract, it turns out to be wrong, and
the argument begins: did the model invent the clause, or did retrieval hand it
the wrong document? Those are opposite failures with opposite fixes, and a log
of the final answer cannot tell them apart.

Deposition records the retrieval as its own event with the document ids, and the
chunk text travels with the trace. This example's contracts are large enough to
cross the 64KB threshold, so the passages are written to content-addressed blobs
under ./deposition/blobs/ and the event carries a `$blob` reference - the trace
stays scannable, the text is still there, and its hash is inside the chain.

    python examples/07_rag_contract_qa.py
    deposition view ./deposition/run_*.jsonl
"""

import deposition

INDEX_VERSION = "contracts-2026-09-14"

CORPUS = {
    "msa-2024-v1": (
        "12.3 Termination for convenience. Either party may terminate this Agreement "
        "upon ninety (90) days written notice. " + "Boilerplate clause text. " * 3000
    ),
    "msa-2026-v3": (
        "12.3 Termination for convenience. Either party may terminate this Agreement "
        "upon thirty (30) days written notice. " + "Boilerplate clause text. " * 3000
    ),
    "nda-2025": (
        "This agreement shall remain in force for two (2) years. " + "Filler clause text. " * 3000
    ),
}

deposition.init(project="contract-qa", config={"index_version": INDEX_VERSION})


def search(query: str) -> list[str]:
    """A stale index: the superseded 2024 MSA still ranks first."""
    return ["msa-2024-v1", "msa-2026-v3"]


@deposition.record(name="contract-agent")
def answer(question: str) -> str:
    # `retrieval` is a first-class event type precisely so this question -
    # which documents were in the window? - never depends on the model's
    # own account of what it read.
    hits = deposition.log(
        "retrieval",
        {
            "query": question,
            "source": f"vector-index/{INDEX_VERSION}",
            "doc_ids": search(question),
            "top_k": 2,
            "index_version": INDEX_VERSION,
        },
    )

    # The passage actually put in front of the model. This is the field that
    # settles the argument, so it is recorded verbatim rather than summarised.
    context = "\n\n".join(f"[{doc_id}] {CORPUS[doc_id]}" for doc_id in search(question))

    reply = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": "pretend-model-1",
            "messages": [
                {"role": "system", "content": context},
                {"role": "user", "content": question},
            ],
            "response": "Either party may terminate on ninety (90) days written notice.",
            "usage": {"total_tokens": 2140},
            "cost_usd": 0.0064,
        },
        caused_by=[hits],
    )

    with deposition.step("decision", label="answer delivered", caused_by=[reply]) as body:
        body["detail"] = "cited the 2024 MSA"
        body["cited_doc_ids"] = ["msa-2024-v1"]

    return "Either party may terminate on ninety (90) days written notice."


if __name__ == "__main__":
    print(answer("How much notice do we need to give to terminate for convenience?"))
    deposition.shutdown()
    print(
        "\nThe post-mortem this trace settles: the model did not hallucinate. Retrieval\n"
        "returned the superseded msa-2024-v1 above msa-2026-v3, and the answer follows\n"
        "the text it was given. The fix is in the index, not the prompt."
    )
