"""The standalone verifier and the SDK must never disagree.

``scripts/verify_standalone.py`` is a second, independent copy of the chain and
signature checks, so that someone who does not trust this package can verify a
trace with one stdlib-only file. Two implementations of the same check can
drift apart, and a drift here is the worst bug this project can have: one tool
saying "verified" where the other says "broken". So every fixture is run
through both - the script as a subprocess, ``depo verify`` in-process - and
their verdicts, error codes, first broken seq, signature status and exit codes
are compared field by field.
"""

from __future__ import annotations

import ast
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from deposition import _demo
from deposition.chain import ChainBuilder, event_hash, seal
from deposition.cli import main
from deposition.mandate import audit
from deposition.schema import canonical_json
from deposition.signing import generate_key, sign_head, write_sidecar

SCRIPT = Path(__file__).parent.parent / "scripts" / "verify_standalone.py"
TS = "2026-10-26T09:00:00.000Z"


def standalone(*args, bare=False):
    """Run the script as a user would. ``bare`` drops site-packages entirely
    (``-S``), which is the closest local stand-in for an interpreter with
    nothing installed."""
    flags = ["-I", "-S"] if bare else ["-I"]
    return subprocess.run(
        [sys.executable, *flags, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
    )


def standalone_json(path, *args, bare=False):
    proc = standalone(path, "--json", *args, bare=bare)
    return proc.returncode, json.loads(proc.stdout)[0]


def sdk_json(path, capsys, *args):
    try:
        code = main(["verify", str(path), "--json", *map(str, args)])
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)[0]


def assert_agree(path, capsys, *args):
    code_s, s = standalone_json(path, *args)
    code_d, d = sdk_json(path, capsys, *args)
    assert {k: s.get(k) for k in d} == d
    assert code_s == code_d
    return s


# -- fixtures: a trace and every way the chain tests break one ----------------


def build_events(run_id="run_agree1", *, dropped=0):
    """Floats, a negative zero and non-ASCII text, so the canonicaliser is
    exercised on the paths where Python and RFC 8785 differ."""
    b = ChainBuilder(run_id)
    return [
        b.append("run_start", {"agent": "agreement-test"}, ts=TS),
        b.append("llm_call", {"approved_usd": 50.0, "usage": {"total_tokens": 12}}, ts=TS),
        b.append(
            "tool_call",
            {"tool": "search", "arguments": {"q": "naïve 🔍"}, "result": 1e21},
            caused_by=[1],
            ts=TS,
        ),
        b.append("decision", {"label": "refund", "detail": -0.0}, caused_by=[2], ts=TS),
        b.append("run_end", {"status": "ok", "totals": {"steps": 3, "dropped": dropped}}, ts=TS),
    ]


def write(path, events_or_lines):
    lines = [e if isinstance(e, str) else canonical_json(e) for e in events_or_lines]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def edited(events, index, **body):
    out = [dict(e) for e in events]
    out[index] = dict(out[index], body={**out[index]["body"], **body})
    return out


def resealed(events, start):
    out = [dict(e) for e in events]
    prev = out[start - 1]["hash"]
    for i in range(start, len(out)):
        out[i] = seal({k: v for k, v in out[i].items() if k != "hash"}, prev)
        prev = out[i]["hash"]
    return out


def forward_causality(events):
    out = [dict(e) for e in events]
    forged = {k: v for k, v in out[2].items() if k != "hash"}
    forged["caused_by"] = [4]
    forged["hash"] = event_hash(forged)
    out[2] = forged
    return out


def negative_causality():
    b = ChainBuilder("run_agree1")
    return [
        b.append("run_start", {"agent": "agreement-test"}, ts=TS),
        b.append("tool_call", {"tool": "search"}, caused_by=[-1], ts=TS),
        b.append("run_end", {"status": "ok"}, ts=TS),
    ]


def v0_1_events():
    events, prev = [], "0" * 64
    bodies = [("run_start", {"agent": "old"}), ("decision", {"approved_usd": 50.0})]
    for seq, (kind, body) in enumerate(bodies):
        event = seal(
            {"v": "0.1", "run_id": "run_old001", "seq": seq, "ts": TS, "type": kind, "body": body},
            prev,
        )
        events.append(event)
        prev = event["hash"]
    return events


def after_run_end():
    b = ChainBuilder("run_agree1")
    return [
        b.append("run_start", {"agent": "agreement-test"}, ts=TS),
        b.append("run_end", {"status": "ok"}, ts=TS),
        b.append("annotation", {"note": "written after the end"}, ts=TS),
    ]


def no_ts(events):
    out = [dict(e) for e in events]
    del out[2]["ts"]
    return out


def lines(events):
    return [json.dumps(e) for e in events]


CASES = {
    "a_clean_trace": lambda e: e,
    "an_edited_body": lambda e: edited(e, 2, tool="delete_everything"),
    "an_edited_timestamp": lambda e: [*e[:1], dict(e[1], ts="2027-01-01T00:00:00.000Z"), *e[2:]],
    "a_dropped_event": lambda e: e[:2] + e[3:],
    "reordered_events": lambda e: [e[0], e[1], e[3], e[2], e[4]],
    "a_duplicated_event": lambda e: e[:3] + [e[2]] + e[3:],
    "an_inserted_forged_event": lambda e: e[:3] + edited(e, 3, label="forged")[3:4] + e[3:],
    "an_edit_and_reseal": lambda e: resealed(edited(e, 2, tool="delete_everything"), 2),
    "a_truncated_tail": lambda e: e[:-1],
    "events_after_run_end": lambda e: after_run_end(),
    "an_event_from_another_run": lambda e: [*e[:2], dict(e[2], run_id="run_other"), *e[3:]],
    "a_missing_run_start": lambda e: e[1:],
    "a_wrong_genesis_hash": lambda e: [dict(e[0], prev_hash="1" * 64), *e[1:]],
    "an_unparseable_line": lambda e: lines(e[:2]) + ["{not json"] + lines(e[2:]),
    "an_invalid_event": no_ts,
    "an_empty_file": lambda e: [],
    "a_negative_caused_by": lambda e: negative_causality(),
    "a_forward_caused_by": forward_causality,
    "a_v0_1_trace": lambda e: v0_1_events(),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_standalone_verifier_agrees_with_the_sdk(tmp_path, capsys, case):
    path = write(tmp_path / "run_agree1.jsonl", CASES[case](build_events()))
    assert_agree(path, capsys)


def test_standalone_verifier_agrees_with_the_sdk_on_an_edited_body(tmp_path, capsys):
    path = write(tmp_path / "run.jsonl", edited(build_events(), 2, tool="delete_everything"))
    result = assert_agree(path, capsys)
    assert result["chain_ok"] is False
    assert [(e["code"], e["seq"]) for e in result["errors"]] == [("hash_mismatch", 2)]


def test_standalone_verifier_agrees_with_the_sdk_on_the_bundled_demo_trace(capsys):
    result = assert_agree(_demo.trace_path(), capsys, "--pubkey", _demo.public_key())
    assert result["ok"] is True
    assert result["signature"]["status"] == "verified"


# -- signatures -----------------------------------------------------------------

KEY = generate_key()
OTHER = generate_key()


def signed_trace(tmp_path):
    events = build_events()
    path = write(tmp_path / "run_agree1.jsonl", events)
    head = events[-1]
    write_sidecar(
        sign_head(KEY, run_id="run_agree1", head_hash=head["hash"], seq=4, events=5), path
    )
    return path, events


def _resealed_after_signing(path, events):
    write(path, resealed(edited(events, 2, tool="delete_everything"), 2))


def _sidecar_edited(path, events):
    sig = path.with_suffix(".sig")
    claims = json.loads(sig.read_text("utf-8"))
    claims["signed_at"] = "2020-01-01T00:00:00.000Z"
    sig.write_text(json.dumps(claims), encoding="utf-8")


SIGNATURE_CASES = {
    "pinned_to_the_right_key": (None, ["--pubkey", KEY.public_hex], "verified", 0),
    "not_pinned": (None, [], "unpinned", 0),
    "pinned_to_the_wrong_key": (None, ["--pubkey", OTHER.public_hex], "key_mismatch", 1),
    "resealed_after_signing": (
        _resealed_after_signing,
        ["--pubkey", KEY.public_hex],
        "head_mismatch",
        1,
    ),
    "sidecar_edited": (_sidecar_edited, ["--pubkey", KEY.public_hex], "bad_signature", 1),
    "sidecar_not_json": (
        lambda p, e: p.with_suffix(".sig").write_text("not json", "utf-8"),
        [],
        "malformed",
        1,
    ),
    "unsigned": (lambda p, e: p.with_suffix(".sig").unlink(), [], "unsigned", 0),
    "required_but_unpinned": (None, ["--require-signature"], "unpinned", 1),
    "required_and_pinned": (
        None,
        ["--require-signature", "--pubkey", KEY.public_hex],
        "verified",
        0,
    ),
}


@pytest.mark.parametrize("case", sorted(SIGNATURE_CASES))
def test_standalone_verifier_agrees_with_the_sdk_on_signature_status(tmp_path, capsys, case):
    mutate, args, status, exit_code = SIGNATURE_CASES[case]
    path, events = signed_trace(tmp_path)
    if mutate:
        mutate(path, events)
    result = assert_agree(path, capsys, *args)
    assert result["signature"]["status"] == status
    code, _ = standalone_json(path, *args)
    assert code == exit_code


@pytest.mark.parametrize("mode", [[], ["--openssl"]], ids=["cryptography", "openssl"])
def test_standalone_verifier_never_says_verified_without_a_pinned_key(tmp_path, mode):
    path, _ = signed_trace(tmp_path)
    code, result = standalone_json(path, *mode)
    assert result["signature"]["status"] in ("unpinned", "unavailable")
    assert "OK signature" not in standalone(path, *mode).stdout
    assert standalone(path, "--require-signature", *mode).returncode == 1


# -- no cryptography: the openssl fallback --------------------------------------


def test_without_cryptography_the_standalone_verifier_prints_an_openssl_command_and_does_not_claim_verified(  # noqa: E501
    tmp_path,
):
    path, _ = signed_trace(tmp_path)
    code, result = standalone_json(path, "--pubkey", KEY.public_hex, bare=True)
    assert result["signature"]["status"] == "unavailable"
    assert result["openssl_command"].startswith("openssl pkeyutl -verify -pubin")
    assert code == 0
    proc = standalone(path, "--pubkey", KEY.public_hex, bare=True)
    assert "OK signature" not in proc.stdout
    assert "openssl pkeyutl" in proc.stdout


def test_without_cryptography_a_resealed_trace_still_fails_on_its_head_hash(tmp_path):
    path, events = signed_trace(tmp_path)
    _resealed_after_signing(path, events)
    code, result = standalone_json(path, "--pubkey", KEY.public_hex, "--openssl")
    assert result["signature"]["status"] == "head_mismatch"
    assert code == 1


def test_without_cryptography_the_wrong_pinned_key_still_fails(tmp_path):
    path, _ = signed_trace(tmp_path)
    code, result = standalone_json(path, "--pubkey", OTHER.public_hex, "--openssl")
    assert result["signature"]["status"] == "key_mismatch"
    assert code == 1


def _openssl_3() -> bool:
    exe = shutil.which("openssl")
    if not exe:
        return False
    version = subprocess.run([exe, "version"], capture_output=True, text=True).stdout
    return version.startswith("OpenSSL 3")


needs_openssl_3 = pytest.mark.skipif(not _openssl_3(), reason="needs OpenSSL 3.0+ on PATH")


@needs_openssl_3
def test_the_printed_openssl_command_accepts_a_good_signature(tmp_path):
    path, _ = signed_trace(tmp_path)
    _, result = standalone_json(path, "--pubkey", KEY.public_hex, "--openssl")
    proc = subprocess.run(shlex.split(result["openssl_command"]), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "Signature Verified Successfully" in proc.stdout


@needs_openssl_3
def test_the_printed_openssl_command_rejects_a_forged_signature(tmp_path):
    path, events = signed_trace(tmp_path)
    _sidecar_edited(path, events)
    _, result = standalone_json(path, "--pubkey", KEY.public_hex, "--openssl")
    assert result["signature"]["status"] == "unavailable"
    proc = subprocess.run(shlex.split(result["openssl_command"]), capture_output=True, text=True)
    assert proc.returncode != 0


# -- dropped events, exit codes, independence -----------------------------------


def test_standalone_verifier_reports_dropped_events_in_the_same_words_as_audit(tmp_path, capsys):
    events = build_events(dropped=3)
    path = write(tmp_path / "run.jsonl", events)
    line = "3 dropped event(s) - the record admits gaps"

    proc = standalone(path)
    assert f"  -- {line}" in proc.stdout
    assert proc.returncode == 0

    main(["verify", str(path)])
    assert f"  -- {line}" in capsys.readouterr().out

    assert line in audit(_with_mandate(events)).summary()
    assert assert_agree(path, capsys)["dropped"] == 3


def _with_mandate(events):
    first = dict(events[0], body={**events[0]["body"], "mandate": {"max_steps": 100}})
    return [first, *events[1:]]


@pytest.mark.parametrize(
    "args",
    [["missing.jsonl"], ["run.jsonl", "--pubkey", "not-a-key"]],
    ids=["missing_file", "malformed_pubkey"],
)
def test_standalone_verifier_exits_2_where_depo_verify_does(tmp_path, monkeypatch, args):
    write(tmp_path / "run.jsonl", build_events())
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["verify", *args])
    assert exc.value.code == 2
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, cwd=tmp_path
    )
    assert proc.returncode == 2


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((alias.name.split(".")[0], node) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module.split(".")[0], node


def test_standalone_verifier_imports_nothing_outside_the_standard_library():
    tree = ast.parse(SCRIPT.read_text("utf-8"))
    guarded = {
        id(child)
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for child in ast.walk(node)
    }
    for module, node in _imports(tree):
        if module == "__future__":
            continue
        if module == "cryptography":
            assert id(node) in guarded, "cryptography must stay optional, inside a try"
            continue
        assert module in sys.stdlib_module_names, f"{module} is not in the standard library"


def test_standalone_verifier_does_not_import_deposition():
    tree = ast.parse(SCRIPT.read_text("utf-8"))
    assert "deposition" not in {module for module, _ in _imports(tree)}


def test_the_standalone_verifier_runs_with_site_packages_disabled():
    code, result = standalone_json(_demo.trace_path(), "--pubkey", _demo.public_key(), bare=True)
    assert code == 0
    assert result["chain_ok"] is True
