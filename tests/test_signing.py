"""Signature tests (ADR 005, layer 1).

The chain alone cannot survive its own holder: edit an event, re-seal the rest,
and ``verify`` calls the result clean. These tests are the claim that a
signature closes that hole - and the equally important claim that a signature
checked against no particular key is *not* reported as proof of anything.
"""

from __future__ import annotations

import json
import stat

import pytest

import deposition
from deposition import signing
from deposition.chain import ChainBuilder, verify_file
from deposition.cli import EXIT_BROKEN, EXIT_OK, EXIT_USAGE, main
from deposition.schema import EventType, canonical_json
from deposition.signing import (
    SignatureError,
    generate_key,
    load_key,
    load_public_key,
    read_sidecar,
    sidecar_path,
    sign_head,
    verify_sidecar,
    write_sidecar,
)

TS = "2026-09-27T08:14:03.412Z"


def write_run(path, *, run_id="run_9f3kQ2", tool="search"):
    """An ordinary finished run on disk, unsigned."""
    builder = ChainBuilder(run_id)
    events = [
        builder.append(EventType.RUN_START, {"agent": "triage-agent"}, ts=TS),
        builder.append(EventType.LLM_CALL, {"gen_ai.request.model": "claude-sonnet-5"}, ts=TS),
        builder.append(EventType.TOOL_CALL, {"tool": tool}, caused_by=[1], ts=TS),
        builder.append(EventType.RUN_END, {"status": "ok"}, ts=TS),
    ]
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")
    return path


def sign_file(path, key, **overrides):
    """Sign a trace on disk exactly as the SDK does at run end."""
    result = verify_file(path)
    claims = {
        "run_id": result.run_id,
        "head_hash": result.head_hash,
        "seq": result.events_checked - 1,
        "events": result.events_checked,
    }
    claims.update(overrides)
    sidecar = sign_head(key, **claims)
    write_sidecar(sidecar, path)
    return sidecar


def rewrite_history(path, seq, key, value, *, run_id="run_9f3kQ2"):
    """The attack the signature exists for: edit an event and re-seal the chain.

    The output is a chain that verifies perfectly. Only a signature over the old
    head hash can tell anyone it is not the run that happened.
    """
    events = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    builder = ChainBuilder(run_id)
    rebuilt = []
    for event in events:
        body = dict(event["body"])
        if event["seq"] == seq:
            body[key] = value
        rebuilt.append(
            builder.append(event["type"], body, caused_by=event.get("caused_by"), ts=event["ts"])
        )
    path.write_text("".join(canonical_json(e) + "\n" for e in rebuilt), encoding="utf-8")
    return path


@pytest.fixture
def key():
    return generate_key()


# -- keys -------------------------------------------------------------------


def test_a_generated_key_round_trips_through_a_pem_file(tmp_path, key):
    path = key.write(tmp_path / "signing.pem")
    assert load_key(path).public_hex == key.public_hex


def test_a_written_private_key_is_readable_only_by_its_owner(tmp_path, key):
    path = key.write(tmp_path / "signing.pem")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writing_a_key_also_writes_its_public_half(tmp_path, key):
    path = key.write(tmp_path / "signing.pem")
    assert path.with_suffix(".pem.pub").read_text("utf-8").strip() == key.public_hex


def test_a_raw_32_byte_key_loads_so_a_key_can_live_in_an_env_var(key):
    raw = key._private.private_bytes_raw()
    assert len(raw) == 32
    assert load_key(raw).public_hex == key.public_hex


def test_loading_a_missing_key_says_which_file_is_missing(tmp_path):
    with pytest.raises(SignatureError, match="no such signing key"):
        load_key(tmp_path / "absent.pem")


def test_loading_something_that_is_not_a_key_is_refused(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("this is not a key", encoding="utf-8")
    with pytest.raises(SignatureError, match="not an unencrypted Ed25519"):
        load_key(path)


def test_a_pinned_public_key_may_be_hex_or_a_file(tmp_path, key):
    path = tmp_path / "key.pub"
    path.write_text(key.public_hex + "\n", encoding="utf-8")
    assert load_public_key(str(path)) == key.public_hex
    assert load_public_key(key.public_hex.upper()) == key.public_hex


def test_a_public_key_of_the_wrong_length_is_refused():
    with pytest.raises(SignatureError, match="64 hex characters"):
        load_public_key("abcd")


# -- the attack this layer exists for ---------------------------------------


def test_a_signed_trace_verifies_against_its_key(tmp_path, key):
    path = write_run(tmp_path / "run.jsonl")
    sign_file(path, key)
    result = verify_file(path)
    signature = verify_sidecar(
        read_sidecar(path),
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=key.public_hex,
    )
    assert signature.ok, signature.message


def test_an_edited_and_resealed_trace_still_passes_the_chain_but_fails_the_signature(tmp_path, key):
    path = write_run(tmp_path / "run.jsonl")
    sign_file(path, key)
    rewrite_history(path, 2, "tool", "delete_everything")

    result = verify_file(path)
    assert result.ok, "re-sealing is supposed to produce a clean chain - that is the problem"

    signature = verify_sidecar(
        read_sidecar(path),
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=key.public_hex,
    )
    assert signature.status == signing.HEAD_MISMATCH
    assert not signature.ok


def test_resigning_a_rewritten_trace_with_another_key_fails_against_the_expected_one(tmp_path, key):
    path = write_run(tmp_path / "run.jsonl")
    sign_file(path, key)
    rewrite_history(path, 2, "tool", "delete_everything")
    sign_file(path, generate_key())  # the attacker signs their own version

    result = verify_file(path)
    signature = verify_sidecar(
        read_sidecar(path),
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=key.public_hex,
    )
    assert signature.status == signing.KEY_MISMATCH


def test_without_a_pinned_key_a_signature_is_reported_as_unverified_not_as_proof(tmp_path, key):
    path = write_run(tmp_path / "run.jsonl")
    sign_file(path, key)
    rewrite_history(path, 2, "tool", "delete_everything")
    sign_file(path, generate_key())

    result = verify_file(path)
    signature = verify_sidecar(read_sidecar(path), head_hash=result.head_hash, run_id=result.run_id)
    assert signature.status == signing.UNPINNED
    assert not signature.ok
    assert "unverified key" in signature.message


def test_a_signature_cannot_be_lifted_onto_another_run(tmp_path, key):
    first = write_run(tmp_path / "first.jsonl", run_id="run_aaaaaa")
    second = write_run(tmp_path / "second.jsonl", run_id="run_bbbbbb", tool="refund")
    sidecar = sign_file(first, key)
    write_sidecar(sidecar, second)

    result = verify_file(second)
    signature = verify_sidecar(
        read_sidecar(second),
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=key.public_hex,
    )
    assert signature.status == signing.HEAD_MISMATCH


def test_editing_the_sidecar_itself_is_detected(tmp_path, key):
    path = write_run(tmp_path / "run.jsonl")
    sidecar = sign_file(path, key)
    sidecar["head_hash"] = "f" * 64
    write_sidecar(sidecar, path)

    signature = verify_sidecar(read_sidecar(path), head_hash="f" * 64)
    assert signature.status == signing.BAD_SIGNATURE


def test_editing_the_run_id_in_the_sidecar_is_detected(tmp_path, key):
    path = write_run(tmp_path / "run.jsonl")
    sidecar = sign_file(path, key)
    sidecar["run_id"] = "run_zzzzzz"
    write_sidecar(sidecar, path)

    result = verify_file(path)
    signature = verify_sidecar(read_sidecar(path), head_hash=result.head_hash)
    assert signature.status == signing.BAD_SIGNATURE


def test_an_unsigned_trace_is_reported_as_unsigned_not_as_broken(tmp_path):
    path = write_run(tmp_path / "run.jsonl")
    signature = verify_sidecar(read_sidecar(path), head_hash=verify_file(path).head_hash)
    assert signature.status == signing.UNSIGNED
    assert not signature.trusted


def test_a_sidecar_naming_an_unknown_algorithm_is_refused(tmp_path, key):
    path = write_run(tmp_path / "run.jsonl")
    sidecar = sign_file(path, key)
    sidecar["alg"] = "rsa-please-trust-me"
    write_sidecar(sidecar, path)
    assert verify_sidecar(read_sidecar(path), head_hash=None).status == signing.MALFORMED


# -- the SDK writes one ------------------------------------------------------


def test_a_run_recorded_with_a_signing_key_writes_a_verifiable_sidecar(tmp_path, trace_dir, key):
    key_path = key.write(tmp_path / "signing.pem")
    deposition.init("signed", directory=trace_dir, instrument=False, signing_key=key_path)

    @deposition.record(name="agent")
    def run():
        deposition.log(EventType.DECISION, {"label": "refund"})

    run()
    deposition.shutdown()

    traces = list(trace_dir.glob("run_*.jsonl"))
    assert len(traces) == 1
    result = verify_file(traces[0])
    signature = verify_sidecar(
        read_sidecar(traces[0]),
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=key.public_hex,
    )
    assert signature.ok, signature.message


def test_the_sidecar_lands_next_to_its_trace(tmp_path, trace_dir, key):
    deposition.init(
        "signed", directory=trace_dir, instrument=False, signing_key=key.write(tmp_path / "k.pem")
    )

    @deposition.record(name="agent")
    def run():
        pass

    run()
    deposition.shutdown()
    trace = next(trace_dir.glob("run_*.jsonl"))
    assert sidecar_path(trace).exists()


def test_a_run_without_a_signing_key_writes_no_sidecar(trace_dir):
    deposition.init("plain", directory=trace_dir, instrument=False)

    @deposition.record(name="agent")
    def run():
        pass

    run()
    deposition.shutdown()
    trace = next(trace_dir.glob("run_*.jsonl"))
    assert not sidecar_path(trace).exists()


def test_init_refuses_a_key_it_cannot_load_rather_than_recording_unsigned(tmp_path, trace_dir):
    # Silently downgrading to unsigned traces would leave the caller believing in
    # signatures they do not have, so this is the one place the SDK raises.
    with pytest.raises(SignatureError):
        deposition.init(
            "signed",
            directory=trace_dir,
            instrument=False,
            signing_key=tmp_path / "absent.pem",
        )


# -- CLI ---------------------------------------------------------------------


def test_verify_reports_an_unsigned_trace_but_still_exits_zero(tmp_path, capsys):
    path = write_run(tmp_path / "run.jsonl")
    assert main(["verify", str(path)]) == EXIT_OK
    assert "unsigned" in capsys.readouterr().out


def test_verify_does_not_flag_an_unsigned_trace_as_an_alarm(tmp_path, capsys):
    path = write_run(tmp_path / "run.jsonl")
    main(["verify", str(path)])
    assert "!!" not in capsys.readouterr().out


def test_verify_fails_an_unsigned_trace_when_a_signature_is_required(tmp_path, capsys):
    path = write_run(tmp_path / "run.jsonl")
    assert main(["verify", str(path), "--require-signature"]) == EXIT_BROKEN


def test_verify_confirms_a_signature_against_a_pinned_key(tmp_path, capsys, key):
    path = write_run(tmp_path / "run.jsonl")
    sign_file(path, key)
    assert main(["verify", str(path), "--pubkey", key.public_hex]) == EXIT_OK
    assert "signature verified" in capsys.readouterr().out


def test_verify_fails_a_resealed_trace_even_though_its_chain_is_clean(tmp_path, capsys, key):
    path = write_run(tmp_path / "run.jsonl")
    sign_file(path, key)
    rewrite_history(path, 2, "tool", "delete_everything")

    assert main(["verify", str(path), "--pubkey", key.public_hex]) == EXIT_BROKEN
    out = capsys.readouterr().out
    assert "OK" in out  # the chain itself still verifies
    assert "rewritten after it was signed" in out


def test_verify_fails_a_broken_signature_without_being_asked_to_require_one(tmp_path, key):
    path = write_run(tmp_path / "run.jsonl")
    sign_file(path, key)
    rewrite_history(path, 2, "tool", "delete_everything")
    assert main(["verify", str(path)]) == EXIT_BROKEN


def test_verify_does_not_call_an_unpinned_signature_verified(tmp_path, capsys, key):
    path = write_run(tmp_path / "run.jsonl")
    sign_file(path, key)
    assert main(["verify", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "unverified key" in out
    assert "signature verified" not in out


def test_verify_json_carries_the_signature_status(tmp_path, capsys, key):
    path = write_run(tmp_path / "run.jsonl")
    sign_file(path, key)
    assert main(["verify", "--json", str(path), "--pubkey", key.public_hex]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["signature"]["status"] == signing.VERIFIED
    assert payload[0]["signature"]["public_key"] == key.public_hex
    assert payload[0]["chain_ok"] is True


def test_verify_rejects_a_malformed_pinned_key_as_a_usage_error(tmp_path):
    path = write_run(tmp_path / "run.jsonl")
    with pytest.raises(SystemExit) as exit_info:
        main(["verify", str(path), "--pubkey", "nonsense"])
    assert exit_info.value.code == EXIT_USAGE


def test_keygen_writes_a_usable_keypair(tmp_path, capsys):
    out_path = tmp_path / "signing.pem"
    assert main(["keygen", "--out", str(out_path)]) == EXIT_OK
    printed = capsys.readouterr().out
    assert load_key(out_path).public_hex in printed
    assert out_path.with_suffix(".pem.pub").exists()
