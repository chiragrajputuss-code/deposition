"""Trace schema v0 tests.

The schema is a wire format: every trace ever written depends on these rules
holding byte for byte. Changing a behaviour asserted here is a schema version
bump, not a bug fix.
"""

from __future__ import annotations

import json

import pytest

from postflight.schema import (
    BLOB_THRESHOLD_BYTES,
    SCHEMA_VERSION,
    Event,
    EventType,
    SchemaError,
    blob_ref,
    canonical_bytes,
    canonical_json,
    is_blob_ref,
    now_ts,
    sha256_hex,
    validate_event,
)

TS = "2026-09-27T08:14:03.412Z"
H = "a" * 64


def sealed_event(**overrides):
    base = {
        "v": SCHEMA_VERSION,
        "run_id": "run_9f3kQ2",
        "seq": 1,
        "ts": TS,
        "type": "tool_call",
        "body": {"tool": "search"},
        "prev_hash": H,
        "hash": "b" * 64,
    }
    base.update(overrides)
    return base


# -- canonical JSON ---------------------------------------------------------


def test_canonical_json_sorts_keys_and_drops_whitespace():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_json_is_independent_of_insertion_order():
    # The hash chain's entire guarantee rests on this: two dicts that are equal
    # must serialise to identical bytes regardless of how they were built.
    one = {"z": [1, {"y": 2, "x": 3}], "a": "v"}
    two = {"a": "v", "z": [1, {"x": 3, "y": 2}]}
    assert canonical_bytes(one) == canonical_bytes(two)


def test_canonical_json_keeps_unicode_literal():
    # ensure_ascii=False, so the UTF-8 bytes are the payload's own bytes.
    assert canonical_json({"note": "café"}) == '{"note":"café"}'
    assert canonical_bytes({"note": "café"}) == '{"note":"café"}'.encode()


def test_canonical_json_rejects_non_finite_numbers():
    # NaN/Infinity are not JSON; allowing them would make traces unparseable by
    # anything but Python.
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"x": float("inf")})


def test_sha256_hex_is_lowercase_hex_of_the_right_length():
    digest = sha256_hex(b"postflight")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert int(digest, 16) >= 0


def test_now_ts_matches_the_schema_timestamp_format():
    validate_event(sealed_event(ts=now_ts()))


# -- Event ------------------------------------------------------------------


def test_to_dict_omits_unset_optional_fields():
    # Unset fields must not appear, or they would change the hash input.
    data = Event(run_id="run_x", seq=0, type=EventType.RUN_START, ts=TS).to_dict()
    assert "caused_by" not in data
    assert "prev_hash" not in data
    assert "hash" not in data
    assert data["v"] == SCHEMA_VERSION


def test_to_dict_omits_empty_caused_by():
    data = Event(run_id="run_x", seq=1, type=EventType.DECISION, ts=TS, caused_by=[]).to_dict()
    assert "caused_by" not in data


def test_to_dict_includes_caused_by_when_set():
    data = Event(run_id="run_x", seq=5, type=EventType.TOOL_CALL, ts=TS, caused_by=[2, 3]).to_dict()
    assert data["caused_by"] == [2, 3]


def test_from_dict_roundtrips_a_sealed_event():
    original = sealed_event(caused_by=[0])
    assert Event.from_dict(original).to_dict() == original


def test_from_dict_rejects_a_malformed_event():
    with pytest.raises(SchemaError):
        Event.from_dict(sealed_event(type="teleport"))


def test_to_json_line_is_canonical_and_single_line():
    line = Event.from_dict(sealed_event()).to_json_line()
    assert "\n" not in line
    assert json.loads(line) == sealed_event()
    assert line == canonical_json(sealed_event())


def test_event_type_str_is_the_wire_value():
    assert str(EventType.RUN_START) == "run_start"
    assert f"{EventType.LLM_CALL}" == "llm_call"


# -- validate_event ---------------------------------------------------------


@pytest.mark.parametrize("field", ["v", "run_id", "seq", "ts", "type"])
def test_missing_required_field_is_rejected(field):
    event = sealed_event()
    del event[field]
    with pytest.raises(SchemaError, match=field):
        validate_event(event)


@pytest.mark.parametrize("field", ["prev_hash", "hash"])
def test_sealed_events_require_their_hashes(field):
    event = sealed_event()
    del event[field]
    with pytest.raises(SchemaError, match=field):
        validate_event(event, sealed=True)
    validate_event(event, sealed=False)  # unsealed is a legitimate in-flight state


def test_wrong_schema_version_is_rejected_by_name():
    with pytest.raises(SchemaError, match="unsupported schema version"):
        validate_event(sealed_event(v="0.2"))


def test_non_dict_event_is_rejected():
    with pytest.raises(SchemaError, match="must be a JSON object"):
        validate_event(["not", "an", "event"])


@pytest.mark.parametrize("run_id", ["", 42, None])
def test_run_id_must_be_a_non_empty_string(run_id):
    with pytest.raises(SchemaError, match="run_id"):
        validate_event(sealed_event(run_id=run_id))


@pytest.mark.parametrize("seq", [-1, 1.0, "1", None])
def test_seq_must_be_a_non_negative_integer(seq):
    with pytest.raises(SchemaError, match="seq"):
        validate_event(sealed_event(seq=seq))


def test_boolean_seq_is_rejected_even_though_bool_is_an_int():
    with pytest.raises(SchemaError, match="seq"):
        validate_event(sealed_event(seq=True))


@pytest.mark.parametrize(
    "ts",
    [
        "2026-09-27 08:14:03Z",  # space instead of T
        "2026-09-27T08:14:03",  # no zone
        "27/09/2026",
        1759000000,
    ],
)
def test_ts_must_be_iso_8601_with_a_zone(ts):
    with pytest.raises(SchemaError, match="ts"):
        validate_event(sealed_event(ts=ts))


@pytest.mark.parametrize(
    "ts",
    [
        "2026-09-27T08:14:03Z",
        "2026-09-27T08:14:03.412Z",
        "2026-09-27T08:14:03.412839Z",
        "2026-09-27T08:14:03+05:30",
    ],
)
def test_accepted_timestamp_shapes(ts):
    validate_event(sealed_event(ts=ts))


def test_unknown_event_type_is_rejected():
    with pytest.raises(SchemaError, match="unknown event type"):
        validate_event(sealed_event(type="deploy_to_prod"))


@pytest.mark.parametrize("event_type", [t.value for t in EventType])
def test_every_declared_event_type_validates(event_type):
    validate_event(sealed_event(type=event_type))


def test_body_must_be_an_object():
    with pytest.raises(SchemaError, match="body"):
        validate_event(sealed_event(body=["tool"]))


def test_body_may_be_omitted_entirely():
    event = sealed_event()
    del event["body"]
    validate_event(event)


def test_unknown_body_keys_are_allowed():
    # Instrumentation must be able to move faster than the schema.
    validate_event(sealed_event(body={"tool": "search", "vendor_specific": {"x": 1}}))


def test_unknown_envelope_keys_are_rejected():
    # The envelope is the hashed surface; a stray field there is a schema break.
    with pytest.raises(SchemaError, match="unknown envelope field"):
        validate_event(sealed_event(trace_id="abc"))


@pytest.mark.parametrize("caused_by", ["0", [None], [1.5], [True]])
def test_caused_by_must_be_a_list_of_integers(caused_by):
    with pytest.raises(SchemaError, match="caused_by"):
        validate_event(sealed_event(seq=9, caused_by=caused_by))


def test_caused_by_cannot_point_forward():
    # Causality only runs backwards; a forward reference is either a bug or a
    # forgery attempt, and both deserve a loud failure.
    with pytest.raises(SchemaError, match="not earlier"):
        validate_event(sealed_event(seq=3, caused_by=[7]))


def test_caused_by_cannot_point_at_itself():
    with pytest.raises(SchemaError, match="not earlier"):
        validate_event(sealed_event(seq=3, caused_by=[3]))


@pytest.mark.parametrize("value", ["A" * 64, "z" * 64, "a" * 63, "", 1234])
def test_hashes_must_be_64_lowercase_hex_characters(value):
    with pytest.raises(SchemaError, match="prev_hash"):
        validate_event(sealed_event(prev_hash=value))


# -- blobs ------------------------------------------------------------------


def test_blob_ref_shape_is_recognised():
    ref = blob_ref("c" * 64, 70_000)
    assert ref == {"$blob": "c" * 64, "size": 70_000, "encoding": "utf-8"}
    assert is_blob_ref(ref)


def test_is_blob_ref_rejects_ordinary_bodies():
    assert not is_blob_ref({"tool": "search"})
    assert not is_blob_ref("$blob")
    assert not is_blob_ref(None)


def test_blob_threshold_is_64_kib():
    assert BLOB_THRESHOLD_BYTES == 64 * 1024
