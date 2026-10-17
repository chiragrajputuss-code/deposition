"""The bundled demo trace and `deposition demo` (zero setup, zero arguments).

Two different things under test. First, the trace itself: what
``scripts/generate_demo_trace.py`` produced is a fixed file, shipped in the
package, and these tests pin its actual story - if someone regenerates it and
the story breaks (the violation disappears, the signature stops matching), a
test fails here rather than a new user's first `deposition demo` being the
first place anyone notices. Second, the CLI command: `cmd_demo` is checked
against a mocked `serve` rather than a real server, matching how `cmd_view`
is - unlike a trace file, a running viewer is not something this suite starts.
"""

from __future__ import annotations

import json

from deposition.chain import verify_file
from deposition.cli import main
from deposition.mandate import audit

# -- the bundled trace itself -------------------------------------------------


def test_the_bundled_trace_resolves_to_a_real_file():
    from deposition._demo import trace_path

    path = trace_path()
    assert path.exists()
    assert path.name == "run_demo.jsonl"


def test_the_bundled_trace_verifies():
    from deposition._demo import trace_path

    result = verify_file(trace_path())
    assert result.ok, result.summary()


def test_the_bundled_public_key_actually_verifies_the_bundled_signature():
    from deposition._demo import public_key, trace_path
    from deposition.signing import read_sidecar, verify_sidecar

    path = trace_path()
    result = verify_file(path)
    signature = verify_sidecar(
        read_sidecar(path),
        head_hash=result.head_hash,
        run_id=result.run_id,
        expected_public_key=public_key(),
    )
    assert signature.ok, signature.message


def test_the_bundled_trace_tells_the_story_it_is_shipped_for():
    """Pins the actual content, not just that the file parses - a demo whose
    whole job is to show the product working has to keep working."""
    from deposition._demo import public_key, trace_path
    from deposition.viewer import load_trace

    loaded = load_trace(trace_path(), public_key=public_key())

    assert loaded["integrity"]["ok"] is True
    assert loaded["signature"]["status"] == "verified"
    assert loaded["completeness"]["status"] == "complete"
    assert loaded["authority"]["status"] == "violations"

    moment_kinds = sorted(m["kind"] for m in loaded["digest"]["moments"])
    assert moment_kinds == ["error", "mutation", "violation"]
    assert len(loaded["digest"]["episodes"]) == 3


def test_the_violation_names_the_forbidden_tool_and_its_cause():
    from deposition._demo import trace_path

    events = [json.loads(line) for line in trace_path().read_text("utf-8").splitlines()]
    result = audit(events)
    codes = {v.code for v in result.violations}
    assert "forbidden_tool" in codes
    assert "tainted_external" in codes
    email_call = next(e for e in events if e.get("body", {}).get("tool") == "send_email")
    assert email_call["caused_by"], "the violation must trace back to the turn that requested it"


def test_the_mutation_and_the_failure_are_in_separate_episodes_from_the_violation():
    from deposition._demo import public_key, trace_path
    from deposition.viewer import load_trace

    moments = {
        m["seq"]: m["kind"]
        for m in load_trace(trace_path(), public_key=public_key())["digest"]["moments"]
    }
    seqs = sorted(moments)
    # three distinct seqs, not the same event wearing three labels
    assert len(set(seqs)) == 3


# -- the CLI command, serve() mocked ------------------------------------------


def test_demo_passes_the_bundled_trace_and_pinned_key_to_serve(monkeypatch):
    from deposition import _demo, viewer

    calls = []

    def fake_serve(path, *, host, port, open_browser, public_key):
        calls.append(
            {
                "path": path,
                "host": host,
                "port": port,
                "open_browser": open_browser,
                "public_key": public_key,
            }
        )
        return 0

    monkeypatch.setattr(viewer, "serve", fake_serve)
    assert main(["demo", "--no-browser"]) == 0

    assert len(calls) == 1
    call = calls[0]
    assert call["path"] == _demo.trace_path()
    assert call["public_key"] == _demo.public_key()
    assert call["open_browser"] is False
    assert call["host"] == "127.0.0.1"


def test_demo_honours_an_explicit_port(monkeypatch):
    from deposition import viewer

    seen = {}
    monkeypatch.setattr(
        viewer,
        "serve",
        lambda path, **kw: seen.update(kw) or 0,
    )
    main(["demo", "--no-browser", "--port", "9999"])
    assert seen["port"] == 9999


def test_demo_prints_what_the_run_is_before_opening_it(monkeypatch, capsys):
    from deposition import viewer

    monkeypatch.setattr(viewer, "serve", lambda path, **kw: 0)
    main(["demo", "--no-browser"])
    err = capsys.readouterr().err
    assert "no API key" in err
    assert "prompt-injected" in err
