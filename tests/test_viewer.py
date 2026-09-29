"""Local replay viewer tests.

The viewer must open a broken trace as readily as a good one - a tampered run is
exactly the run you most want to look at - and it must never serve a file from
outside the trace's own blob directory.
"""

from __future__ import annotations

import json

import pytest

from deposition.chain import ChainBuilder
from deposition.schema import EventType, blob_ref, canonical_json

fastapi = pytest.importorskip("fastapi", reason="viewer extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from deposition.viewer import create_app, load_trace, resolve_blob_refs  # noqa: E402

TS = "2026-09-27T08:14:03.412Z"


@pytest.fixture
def trace(tmp_path):
    builder = ChainBuilder("run_9f3kQ2")
    events = [
        builder.append(
            EventType.RUN_START,
            {"agent": "triage-agent", "project": "support", "env": {"python": "3.12.7"}},
            ts=TS,
        ),
        builder.append(EventType.LLM_CALL, {"gen_ai.request.model": "claude-sonnet-5"}, ts=TS),
        builder.append(EventType.TOOL_CALL, {"tool": "search"}, caused_by=[1], ts=TS),
        builder.append(
            EventType.RUN_END,
            {"status": "ok", "totals": {"steps": 2, "tokens": 340, "duration_ms": 1200}},
            ts=TS,
        ),
    ]
    path = tmp_path / "run_9f3kQ2.jsonl"
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")
    return path


@pytest.fixture
def client(trace):
    with TestClient(create_app(trace)) as client:
        yield client


def tamper(path, seq):
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        event = json.loads(line)
        if event["seq"] == seq:
            event["body"]["tool"] = "delete_everything"
            lines[index] = canonical_json(event)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# -- load_trace -------------------------------------------------------------


def test_load_trace_summarises_the_run(trace):
    data = load_trace(trace)
    assert data["run"]["run_id"] == "run_9f3kQ2"
    assert data["run"]["agent"] == "triage-agent"
    assert data["run"]["status"] == "ok"
    assert data["run"]["totals"]["tokens"] == 340
    assert data["trace"]["name"] == "run_9f3kQ2.jsonl"
    assert len(data["events"]) == 4


def test_load_trace_reports_a_clean_chain(trace):
    integrity = load_trace(trace)["integrity"]
    assert integrity["ok"] is True
    assert integrity["events_checked"] == 4
    assert len(integrity["head_hash"]) == 64


def test_a_broken_trace_still_loads_with_its_events(trace):
    tamper(trace, 2)
    data = load_trace(trace)
    assert data["integrity"]["ok"] is False
    assert data["integrity"]["errors"][0]["code"] == "hash_mismatch"
    assert len(data["events"]) == 4  # you can still read every step


def test_an_empty_trace_loads_without_raising(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    data = load_trace(path)
    assert data["events"] == []
    assert data["integrity"]["ok"] is False


# -- HTTP API ---------------------------------------------------------------


def test_the_index_page_is_served_from_the_bundled_static_file(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Deposition" in response.text
    # No build step, no CDN: the page must be self-contained.
    assert "http://" not in response.text.split("<script>")[0].replace("http://www.w3.org", "")


def test_the_trace_api_returns_the_whole_run(client):
    payload = client.get("/api/trace").json()
    assert payload["run"]["run_id"] == "run_9f3kQ2"
    assert payload["events"][2]["caused_by"] == [1]


def test_the_trace_api_reflects_events_appended_after_startup(client, trace):
    # A run still being written should be refreshable without a restart.
    before = client.get("/api/trace").json()
    builder = ChainBuilder("run_9f3kQ2", prev_hash=before["integrity"]["head_hash"], next_seq=4)
    with trace.open("a", encoding="utf-8") as fh:
        fh.write(canonical_json(builder.append(EventType.ANNOTATION, {"note": "later"}, ts=TS)))
        fh.write("\n")
    after = client.get("/api/trace").json()
    assert len(after["events"]) == len(before["events"]) + 1


def test_a_blob_is_served_from_next_to_the_trace(client, trace):
    blobs = trace.parent / "blobs"
    blobs.mkdir()
    digest = "a" * 64
    (blobs / digest).write_text("a very long prompt", encoding="utf-8")
    response = client.get(f"/api/blob/{digest}")
    assert response.status_code == 200
    assert response.text == "a very long prompt"


def test_a_missing_blob_is_a_404(client):
    assert client.get(f"/api/blob/{'b' * 64}").status_code == 404


@pytest.mark.parametrize(
    "digest",
    ["../../../etc/passwd", "short", "a" * 65, "..%2f..%2fetc%2fpasswd", "a" * 63 + "/"],
)
def test_blob_paths_outside_the_blob_directory_are_refused(client, digest):
    # The viewer runs on the user's own machine, but it is still an HTTP server:
    # it serves blob digests, never paths.
    assert client.get(f"/api/blob/{digest}").status_code in (400, 404)


# -- blob resolution --------------------------------------------------------


def test_resolve_blob_refs_inlines_stored_content(trace):
    blobs = trace.parent / "blobs"
    blobs.mkdir()
    digest = "c" * 64
    (blobs / digest).write_text("the full prompt", encoding="utf-8")
    body = {"messages": [{"content": blob_ref(digest, 15)}]}
    assert resolve_blob_refs(body, trace) == {"messages": [{"content": "the full prompt"}]}


def test_resolve_blob_refs_leaves_a_missing_blob_as_a_reference(trace):
    ref = blob_ref("d" * 64, 15)
    assert resolve_blob_refs({"prompt": ref}, trace) == {"prompt": ref}


# -- the three claims -------------------------------------------------------
#
# The viewer used to answer "can this be trusted?" with one badge driven only by
# the hash chain. Three separate things were being blended, and a trace could be
# re-sealed, incomplete, or unsigned and still show green.


def sign_beside(path, key):
    from deposition.chain import verify_file
    from deposition.signing import sign_head, write_sidecar

    result = verify_file(path)
    write_sidecar(
        sign_head(
            key,
            run_id=result.run_id,
            head_hash=result.head_hash,
            seq=result.events_checked - 1,
            events=result.events_checked,
        ),
        path,
    )


def reseal(path, seq, key, value):
    """Edit an event and rebuild the chain, exactly as a holder of the file could."""
    events = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    builder = ChainBuilder(events[0]["run_id"])
    rebuilt = []
    for event in events:
        body = dict(event["body"])
        if event["seq"] == seq:
            body[key] = value
        rebuilt.append(
            builder.append(event["type"], body, caused_by=event.get("caused_by"), ts=event["ts"])
        )
    path.write_text("".join(canonical_json(e) + "\n" for e in rebuilt), encoding="utf-8")


def test_an_unsigned_trace_reports_its_authorship_as_unsigned(trace):
    claims = load_trace(trace)
    assert claims["integrity"]["ok"]
    assert claims["signature"]["status"] == "unsigned"


def test_a_signed_trace_without_a_pinned_key_is_not_reported_as_verified(trace):
    from deposition.signing import generate_key

    sign_beside(trace, generate_key())
    signature = load_trace(trace)["signature"]
    assert signature["status"] == "unpinned"
    assert signature["ok"] is False


def test_a_signed_trace_is_verified_against_the_pinned_key(trace):
    from deposition.signing import generate_key

    key = generate_key()
    sign_beside(trace, key)
    assert load_trace(trace, public_key=key.public_hex)["signature"]["ok"]


def test_a_resealed_trace_shows_an_intact_chain_and_a_failed_signature(trace):
    """The case a single badge got wrong: the chain holds and the record is false."""
    from deposition.signing import generate_key

    key = generate_key()
    sign_beside(trace, key)
    reseal(trace, 2, "tool", "wire_transfer")

    claims = load_trace(trace, public_key=key.public_hex)
    assert claims["integrity"]["ok"], "re-sealing produces a clean chain - that is the point"
    assert claims["signature"]["status"] == "head_mismatch"
    assert claims["signature"]["ok"] is False


def test_completeness_reports_no_known_gaps_for_an_ordinary_run(trace):
    assert load_trace(trace)["completeness"]["status"] == "complete"


def test_completeness_reports_dropped_events_the_recorder_admits_to(trace, tmp_path):
    builder = ChainBuilder("run_lossy")
    events = [
        builder.append(EventType.RUN_START, {"agent": "a"}, ts=TS),
        builder.append(
            EventType.RUN_END,
            {"status": "ok", "totals": {"steps": 1, "dropped": 3, "adopted": 0}},
            ts=TS,
        ),
    ]
    path = tmp_path / "run_lossy.jsonl"
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")

    completeness = load_trace(path)["completeness"]
    assert completeness["status"] == "lossy"
    assert completeness["dropped"] == 3
    assert completeness["ok"] is False


def test_completeness_flags_events_that_were_attributed_by_elimination(trace, tmp_path):
    builder = ChainBuilder("run_guessed")
    events = [
        builder.append(EventType.RUN_START, {"agent": "a"}, ts=TS),
        builder.append(
            EventType.RUN_END,
            {"status": "ok", "totals": {"steps": 1, "dropped": 0, "adopted": 2}},
            ts=TS,
        ),
    ]
    path = tmp_path / "run_guessed.jsonl"
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")
    assert load_trace(path)["completeness"]["status"] == "inferred"


def test_a_run_with_no_run_end_is_reported_as_truncated(trace):
    lines = trace.read_text("utf-8").splitlines()[:-1]
    trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert load_trace(trace)["completeness"]["status"] == "truncated"


def test_the_api_serves_all_three_claims(client):
    payload = client.get("/api/trace").json()
    assert {"integrity", "signature", "completeness"} <= set(payload)
