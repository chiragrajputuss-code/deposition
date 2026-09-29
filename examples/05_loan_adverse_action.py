"""A credit decision that must come with reasons, and a record that supplies them.

Denying credit is a regulated act: the applicant is entitled to the specific
reasons, and the lender has to be able to produce them long after the model that
made the decision has been retrained or retired.

What makes this hard is not logging the outcome - everyone logs the outcome. It
is reconstructing which inputs actually drove it, from the run itself, when the
applicant asks a year later or a regulator asks during an examination.

    python examples/05_loan_adverse_action.py
    deposition view ./deposition/run_*.jsonl
"""

import deposition

MODEL_VERSION = "risk-scorer-v4.2"

APPLICATION = {
    "id": "app_55190",
    "amount_usd": 24000,
    "term_months": 48,
    "purpose": "vehicle",
    "stated_income_usd": 61000,
}

deposition.init(project="lending", config={"model_version": MODEL_VERSION})


def pull_bureau(application_id: str) -> dict:
    return {
        "bureau": "example-bureau",
        "pulled_at": "2026-09-29T09:02:11Z",
        "score": 611,
        "open_accounts": 7,
        "delinquencies_24m": 2,
        "utilization_pct": 84,
    }


def verify_income(application_id: str) -> dict:
    return {"verified_income_usd": 47500, "method": "payroll-connect", "confidence": 0.93}


@deposition.record(name="underwriting-agent")
def underwrite(application: dict) -> dict:
    with deposition.step("tool_call", tool="pull_bureau") as body:
        body["arguments"] = {"application_id": application["id"]}
        bureau = pull_bureau(application["id"])
        body["result"] = bureau

    with deposition.step("tool_call", tool="verify_income") as body:
        body["arguments"] = {"application_id": application["id"]}
        income = verify_income(application["id"])
        body["result"] = income

    # Stated income was $61k; verified income is $47.5k. The discrepancy is the
    # kind of thing that decides a file, so it is recorded as a finding of its
    # own rather than left implicit in a later summary.
    discrepancy = deposition.log(
        "decision",
        {
            "label": "income discrepancy",
            "detail": "stated income exceeds verified income by 28%",
            "stated_usd": application["stated_income_usd"],
            "verified_usd": income["verified_income_usd"],
        },
    )

    assessment = deposition.log(
        "llm_call",
        {
            "provider": "example",
            "gen_ai.request.model": MODEL_VERSION,
            "messages": [{"role": "user", "content": f"Assess: {application} {bureau} {income}"}],
            "response": {
                "recommendation": "decline",
                "reason_codes": ["R07", "R12", "R19"],
                "rationale": (
                    "Revolving utilisation at 84% and two delinquencies in 24 months, "
                    "with verified income 28% below stated."
                ),
            },
            "usage": {"total_tokens": 1042},
            "cost_usd": 0.0118,
        },
        caused_by=[discrepancy],
    )

    # The reason codes are the legally operative part of the decision. They are
    # written as structured fields, not prose, so a later export can render them
    # without parsing English.
    with deposition.step("decision", label="decline", caused_by=[assessment]) as body:
        body["detail"] = "declined under policy; adverse action notice required within 30 days"
        body["reason_codes"] = [
            {"code": "R07", "text": "Proportion of balances to credit limits is too high"},
            {"code": "R12", "text": "Delinquency on accounts within the last 24 months"},
            {"code": "R19", "text": "Income could not be verified as stated"},
        ]
        body["model_version"] = MODEL_VERSION
        body["human_review_required"] = True

    return {"decision": "decline", "reason_codes": ["R07", "R12", "R19"]}


if __name__ == "__main__":
    print(underwrite(APPLICATION))
    deposition.shutdown()
    print(
        "\nThe examination question this trace answers: which three facts drove the\n"
        "decline, which model version produced them, and what the bureau file said\n"
        "at the moment it was pulled - not what it says today."
    )
