# 007 — The name

**Status:** accepted · **Date:** 2026-09-28 · **Supersedes:** the working name "Postflight"

## Decision

The product is **Deposition**. Python package `deposition`, CLI `deposition`
with `depo` as the short alias, domain on `.dev` or `.io`.

A deposition is sworn testimony, recorded verbatim outside a courtroom and used
as evidence when someone later disputes what happened. That is the product in one
word, and it puts the name in the evidence-law world rather than the
observability one — which is the entire positioning argument in
[ADR 001](001-positioning.md).

## Why not the working name

`postflight` was **already taken on PyPI**. That settled it regardless of taste.

## What was checked, and how

Every candidate was checked against the PyPI JSON API and against RDAP for
`.com` / `.dev` / `.io`, then — for the shortlist only — searched for existing
products and projects using the name.

**The search step is not optional.** Availability checks pass names that a
product search immediately kills; two of the three finalists died at that stage
after looking clean on PyPI and DNS.

## Candidates and why they lost

| Name | Outcome |
| --- | --- |
| postflight | taken on PyPI |
| agentxray | AWS X-Ray is a distributed *tracing* service — same-category confusion, plus the saturated `agent*` prefix, plus it sells observability, the ground we cannot win |
| tallystick | best story of the lot (split tally sticks: two halves nobody can forge alone), but a live product at tallystick.net, an existing GitHub repo of the name, and — decisively — Tally Solutions is the dominant accounting-software brand in India, where this is being built alongside an accounting product |
| counterfoil | right meaning, `.io` free, but "cheque stub" carries the same accounting association |
| chirograph | clean on PyPI and all TLDs, but a Rust crate, a WebAuthn API called Chirograph Verify, and another tool built on a *"versioned evidence format"* already use it |
| indenture | "indentured servitude" is not a association to hand a launch audience |
| runrecord | clean but generic; nothing to own |
| attestrun | names us after signing, which [ADR 005](005-signing-and-anchoring.md) says we have not shipped — the exact overclaim we just corrected |

## Costs accepted

- Ten letters. Mitigated by shipping `depo` as the CLI.
- Vapour deposition and thin-film deposition will muddy early search results.
- Legal tech sells "remote deposition software" — a category, not a competing
  product, but it is noise in the same word.

## Not yet done

**Trademark clearance is a separate step from availability.** PyPI, DNS and a
product search establish that a name is free to use; none of them is a clearance
search. Complete that step before buying a domain or filing anything.

## Rule for next time

Names are checked in this order: PyPI → product/GitHub search → domains →
trademark. Cheapest disqualifier first, and never buy before the last step.
