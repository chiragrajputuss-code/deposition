"""RFC 8785 canonicalisation (ADR 008).

The claim under test: a verifier in any language computes the same bytes and
therefore the same hash. The pinned vectors below are the output of Node's own
JSON.stringify / String(), captured verbatim - if these drift, the trace format
has broken interoperability, whatever the rest of the suite says. The live
cross-check against Node runs wherever node exists and must run in CI.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess

import pytest

from deposition.chain import ChainBuilder, event_hash, verify
from deposition.schema import SCHEMA_VERSION, SchemaError, _es_number, canonical_json

TS = "2026-10-02T09:00:00.000Z"

#: value -> exactly what Node prints for it. Captured from node v20, 2026-10-02.
NODE_PINNED = {
    50.0: "50",
    0.0041: "0.0041",
    123.456: "123.456",
    1e16: "10000000000000000",
    1e21: "1e+21",
    1e-6: "0.000001",
    1e-7: "1e-7",
    5e-324: "5e-324",
    1.7976931348623157e308: "1.7976931348623157e+308",
    0.1: "0.1",
    2.5: "2.5",
    1000000.0: "1000000",
    9007199254740991.0: "9007199254740991",
    0.000001234: "0.000001234",
    4.35: "4.35",
    1e-10: "1e-10",
    -1.5e-8: "-1.5e-8",
    333333333.33333334: "333333333.3333333",
}


def test_numbers_print_exactly_as_ecmascript_prints_them():
    for value, expected in NODE_PINNED.items():
        assert _es_number(value) == expected, f"{value!r}"


def test_negative_zero_prints_as_zero_like_json_stringify():
    assert _es_number(-0.0) == "0"


def test_nan_and_infinity_are_refused():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(SchemaError):
            _es_number(bad)


def test_integers_keep_exact_form_at_any_size():
    assert canonical_json({"n": 2**60}) == '{"n":1152921504606846976}'


def test_keys_sort_by_utf16_code_unit_not_code_point():
    """U+10000 is a surrogate pair starting D800; U+FF61 is a single unit FF61.

    Code-point order puts U+FF61 first. JCS order puts the surrogate first,
    because D800 < FF61 - the exact disagreement RFC 8785 exists to settle.
    """
    out = canonical_json({"｡": 1, "\U00010000": 2})
    assert out.index("\U00010000") < out.index("｡")


def test_the_measured_adr_008_case_now_matches_across_languages():
    # The event that exposed the defect: Python wrote 50.0, every other runtime
    # writes 50. Both serialisers are kept; they disagree on purpose.
    body = {"approved_usd": 50.0}
    assert canonical_json(body) == '{"approved_usd":50}'
    assert canonical_json(body, version="0.1") == '{"approved_usd":50.0}'


def test_a_v0_trace_still_verifies_with_the_serialiser_that_wrote_it():
    """No trace ever becomes unverifiable by its own tool - the migration rule."""
    from deposition.schema import canonical_bytes, sha256_hex

    prev = "0" * 64
    events = []
    for seq, kind in enumerate(("run_start", "run_end")):
        event = {
            "v": "0.1",
            "run_id": "run_old001",
            "seq": seq,
            "ts": TS,
            "type": kind,
            "body": {"agent": "a", "cost_usd": 50.0}
            if seq == 0
            else {"status": "ok", "totals": {}},
            "prev_hash": prev,
        }
        event["hash"] = sha256_hex(
            canonical_bytes({k: v for k, v in event.items() if k != "hash"}, version="0.1")
        )
        prev = event["hash"]
        events.append(event)
    result = verify(events)
    assert result.ok, result.summary()


def test_new_events_carry_v1_and_hash_with_jcs():
    sealed = ChainBuilder("run_new001").append("run_start", {"agent": "a"}, ts=TS)
    assert sealed["v"] == SCHEMA_VERSION == "1.0"
    assert event_hash(sealed) == sealed["hash"]


# -- the cross-language check that must run in CI ----------------------------

node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node not available; CI must provide it")
def test_node_reproduces_our_canonical_bytes_and_hash():
    """One sealed event; Node recomputes the canonical string and the SHA-256."""
    sealed = ChainBuilder("run_x4e9f1").append(
        "run_start",
        {
            "agent": "refund-agent",
            "cost_usd": 50.0,
            "ratio": 0.0041,
            "big": 1e21,
            "tiny": 1e-7,
            "n": 164,
            "nested": {"b": [1.5, "x"], "a": None},
        },
        ts=TS,
    )
    payload = {k: v for k, v in sealed.items() if k != "hash"}
    ours = canonical_json(payload)

    script = """
const crypto = require("crypto");
let data = "";
process.stdin.on("data", (c) => (data += c));
process.stdin.on("end", () => {
  const obj = JSON.parse(data);
  const jcs = (v) => {
    if (v === null || typeof v !== "object") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(jcs).join(",") + "]";
    const keys = Object.keys(v).sort();  // JS sorts strings by UTF-16 units
    return "{" + keys.map((k) => JSON.stringify(k) + ":" + jcs(v[k])).join(",") + "}";
  };
  const canon = jcs(obj);
  process.stdout.write(canon.length + "\\n");
  process.stdout.write(crypto.createHash("sha256").update(canon, "utf8").digest("hex"));
});
"""
    out = subprocess.run(
        [node, "-e", script], input=ours, capture_output=True, text=True, timeout=30
    )
    length, their_hash = out.stdout.strip().split("\n")
    assert int(length) == len(ours), "canonical byte lengths differ"
    assert their_hash == sealed["hash"], "node computes a different hash - interop broken"


@pytest.mark.skipif(node is None, reason="node not available; CI must provide it")
def test_four_hundred_random_doubles_agree_with_node():
    import random

    random.seed(8785)
    values = []
    while len(values) < 400:
        value = struct.unpack("<d", struct.pack("<Q", random.getrandbits(64)))[0]
        if value == value and value not in (float("inf"), float("-inf")):
            values.append(value)
    ours = [_es_number(v) for v in values]
    script = (
        'let d="";process.stdin.on("data",c=>d+=c);process.stdin.on("end",()=>{'
        'console.log(JSON.parse(d).map(String).join("\\n"))});'
    )
    out = subprocess.run(
        [node, "-e", script], input=json.dumps(ours), capture_output=True, text=True, timeout=30
    )
    theirs = out.stdout.strip().split("\n")
    mismatches = [(o, t) for o, t in zip(ours, theirs, strict=True) if o != t]
    assert not mismatches, f"{len(mismatches)} of 400 disagree; first: {mismatches[0]}"
