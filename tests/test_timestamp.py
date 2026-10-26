"""RFC 3161 timestamps on the chain head (ADR 014).

No test here touches a real external timestamp authority. A local HTTP server
stands in for one - answering with a hand-built token, refusing, stalling, or
lying about the hash - and, where OpenSSL 3 is available, a throwaway CA and
TSA made with the openssl CLI issue real signed tokens, so `depo verify
--tsa-ca` is exercised end to end against tokens this project did not build.
"""

from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import deposition
from deposition import timestamp
from deposition.chain import ChainBuilder
from deposition.cli import main
from deposition.schema import canonical_json
from deposition.timestamp import _der_int, _der_oid, _tlv, build_request

SCRIPT = Path(__file__).parent.parent / "scripts" / "verify_standalone.py"


def _openssl_3() -> bool:
    exe = shutil.which("openssl")
    if not exe:
        return False
    out = subprocess.run([exe, "version"], capture_output=True, text=True).stdout
    return out.startswith("OpenSSL 3")


needs_openssl_3 = pytest.mark.skipif(not _openssl_3(), reason="needs OpenSSL 3.0+ on PATH")


# -- a hand-built token, the shape a TSA returns ---------------------------------


def parse_request(der: bytes) -> tuple[str, int | None]:
    items = timestamp._children(timestamp._only(der, 0x30, "TimeStampReq"))
    imprint = timestamp._children(items[1][1])
    hashed = imprint[1][1].hex()
    nonce = next((int.from_bytes(c, "big") for t, c in items[2:] if t == 0x02), None)
    return hashed, nonce


def fake_response(imprint_hex: str, nonce: int | None, *, status: int = 0) -> bytes:
    if status not in (0, 1):
        return _tlv(0x30, _tlv(0x30, _der_int(status)))
    algorithm = _tlv(0x30, _der_oid(timestamp.SHA256_OID) + b"\x05\x00")
    imprint = _tlv(0x30, algorithm + _tlv(0x04, bytes.fromhex(imprint_hex)))
    tst = _tlv(
        0x30,
        _der_int(1)
        + _der_oid("1.2.3.4.1")
        + imprint
        + _der_int(42)
        + _tlv(0x18, b"20261026090000Z")
        + (_der_int(nonce) if nonce is not None else b""),
    )
    encap = _tlv(0x30, _der_oid(timestamp._TST_INFO_OID) + _tlv(0xA0, _tlv(0x04, tst)))
    signed_data = _tlv(0x30, _der_int(3) + _tlv(0x31, b"") + encap + _tlv(0x31, b""))
    content_info = _tlv(0x30, _der_oid(timestamp._SIGNED_DATA_OID) + _tlv(0xA0, signed_data))
    return _tlv(0x30, _tlv(0x30, _der_int(status)) + content_info)


class FakeAuthority:
    """A local TSA. ``mode`` decides how it answers; every request is kept."""

    def __init__(self, mode="echo", *, delay=0.0, reply=None):
        self.mode, self.delay, self.reply = mode, delay, reply
        self.requests: list[bytes] = []
        authority = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                authority.requests.append(body)
                time.sleep(authority.delay)
                if authority.mode == "http_500":
                    self.send_response(500)
                    self.end_headers()
                    return
                hashed, nonce = parse_request(body)
                if authority.mode == "wrong_imprint":
                    payload = fake_response("ab" * 32, nonce)
                elif authority.mode == "refuse":
                    payload = fake_response(hashed, nonce, status=2)
                elif authority.mode == "openssl":
                    payload = authority.reply(body)
                else:
                    payload = fake_response(hashed, nonce)
                self.send_response(200)
                self.send_header("Content-Type", "application/timestamp-reply")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/tsa"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def authority():
    made = []

    def make(mode="echo", **kw):
        server = FakeAuthority(mode, **kw)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


def record_run(trace_dir, url, *, agent="timestamped-agent"):
    deposition.init("test-project", directory=trace_dir, timestamp_url=url, instrument=False)

    @deposition.record(name=agent)
    def work():
        deposition.annotate("did something")

    work()
    path = Path(deposition._recorder._runs[-1].path)
    deposition.shutdown()
    return path


def verify_json(path, capsys, *args):
    try:
        code = main(["verify", str(path), "--json", *map(str, args)])
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)[0]


def head_of(path):
    return json.loads(path.read_text("utf-8").splitlines()[-1])["hash"]


# -- the request -------------------------------------------------------------------


@needs_openssl_3
def test_the_request_is_byte_identical_to_openssls_apart_from_the_nonce(tmp_path):
    head = "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
    plain = tmp_path / "plain.tsq"
    subprocess.run(
        [
            "openssl",
            "ts",
            "-query",
            "-sha256",
            "-digest",
            head,
            "-cert",
            "-no_nonce",
            "-out",
            plain,
        ],
        check=True,
        capture_output=True,
    )
    assert build_request(head, nonce=None) == plain.read_bytes()

    with_nonce = tmp_path / "nonce.tsq"
    subprocess.run(
        ["openssl", "ts", "-query", "-sha256", "-digest", head, "-cert", "-out", with_nonce],
        check=True,
        capture_output=True,
    )
    openssl_bytes = with_nonce.read_bytes()
    _, nonce = parse_request(openssl_bytes)
    assert build_request(head, nonce=nonce) == openssl_bytes


def test_only_the_head_hash_leaves_the_machine(trace_dir, authority):
    tsa = authority()
    path = record_run(trace_dir, tsa.url, agent="secret-agent-name")
    [sent] = tsa.requests
    hashed, nonce = parse_request(sent)
    assert hashed == head_of(path)
    assert sent == build_request(hashed, nonce=nonce)
    assert b"secret-agent-name" not in sent
    assert path.stem.encode() not in sent


# -- recording -----------------------------------------------------------------------


def test_a_run_with_a_timestamp_url_writes_a_tsr_beside_the_trace(trace_dir, authority, capsys):
    path = record_run(trace_dir, authority().url)
    assert path.with_suffix(".tsr").exists()
    status = json.loads(path.with_suffix(".timestamp.json").read_text("utf-8"))
    assert status["status"] == "granted"
    assert status["head_hash"] == head_of(path)

    code, result = verify_json(path, capsys)
    assert code == 0
    assert result["timestamp"]["status"] == "unchecked"
    assert "openssl ts -verify -digest" in result["timestamp"]["message"]


def test_without_a_tsa_ca_a_timestamp_is_never_reported_anchored(trace_dir, authority, capsys):
    path = record_run(trace_dir, authority().url)
    main(["verify", str(path)])
    out = capsys.readouterr().out
    assert "OK timestamp" not in out
    assert "-- timestamp:" in out


def test_without_a_timestamp_url_nothing_is_requested_or_written(trace_dir, capsys):
    path = record_run(trace_dir, None)
    assert not path.with_suffix(".tsr").exists()
    assert not path.with_suffix(".timestamp.json").exists()
    _, result = verify_json(path, capsys)
    assert result["timestamp"]["status"] == "none"


def test_init_refuses_a_timestamp_url_that_is_not_http(trace_dir):
    with pytest.raises(ValueError, match="http"):
        deposition.init("p", directory=trace_dir, timestamp_url="ftp://tsa.example")


def test_the_run_does_not_wait_for_the_authority(trace_dir, authority):
    tsa = authority(delay=1.5)
    deposition.init("p", directory=trace_dir, timestamp_url=tsa.url, instrument=False)

    @deposition.record(name="quick")
    def work():
        deposition.annotate("x")

    started = time.monotonic()
    work()
    assert time.monotonic() - started < 1.0
    deposition.shutdown(timeout=5)


def test_a_timeout_records_unavailable_and_the_run_still_completes(
    trace_dir, authority, monkeypatch, capsys
):
    monkeypatch.setattr(timestamp, "TIMEOUT_S", 0.3)
    path = record_run(trace_dir, authority(delay=2.0).url)
    assert not path.with_suffix(".tsr").exists()
    status = json.loads(path.with_suffix(".timestamp.json").read_text("utf-8"))
    assert status["status"] == "unavailable"
    assert "timed out" in status["reason"].lower()

    code, result = verify_json(path, capsys)
    assert code == 0  # a missing timestamp is a gap, not evidence of tampering
    assert result["chain_ok"] is True
    assert result["timestamp"]["status"] == "unavailable"


@pytest.mark.parametrize("mode", ["refuse", "http_500"])
def test_an_authority_that_refuses_records_unavailable(trace_dir, authority, capsys, mode):
    path = record_run(trace_dir, authority(mode).url)
    assert not path.with_suffix(".tsr").exists()
    _, result = verify_json(path, capsys)
    assert result["timestamp"]["status"] == "unavailable"


def test_an_unreachable_authority_records_unavailable(trace_dir, capsys):
    path = record_run(trace_dir, "http://127.0.0.1:9/tsa")
    _, result = verify_json(path, capsys)
    assert result["timestamp"]["status"] == "unavailable"


def test_a_token_for_a_different_hash_is_never_stored(trace_dir, authority):
    path = record_run(trace_dir, authority("wrong_imprint").url)
    assert not path.with_suffix(".tsr").exists()
    status = json.loads(path.with_suffix(".timestamp.json").read_text("utf-8"))
    assert "different hash" in status["reason"]


def test_a_request_that_never_completed_is_reported_unavailable(tmp_path, capsys):
    path = write_trace(tmp_path / "run.jsonl")
    path.with_suffix(".timestamp.json").write_text(json.dumps({"status": "pending"}), "utf-8")
    _, result = verify_json(path, capsys)
    assert result["timestamp"]["status"] == "unavailable"
    assert "never completed" in result["timestamp"]["message"]


# -- verification ----------------------------------------------------------------------


def write_trace(path, *, note="original"):
    b = ChainBuilder("run_stamp01")
    events = [
        b.append("run_start", {"agent": "a"}),
        b.append("annotation", {"note": note}),
        b.append("run_end", {"status": "ok"}),
    ]
    path.write_text("".join(canonical_json(e) + "\n" for e in events), encoding="utf-8")
    return path


def stamp(path, head=None, nonce=7):
    path.with_suffix(".tsr").write_bytes(fake_response(head or head_of(path), nonce))
    return path


def test_a_mismatched_imprint_is_reported_as_a_mismatch_and_fails_verify(tmp_path, capsys):
    path = stamp(write_trace(tmp_path / "run.jsonl"))
    write_trace(path, note="rewritten after it was timestamped")
    code, result = verify_json(path, capsys)
    assert result["chain_ok"] is True
    assert result["timestamp"]["status"] == "imprint_mismatch"
    assert code == 1


def test_a_tsr_that_cannot_be_parsed_is_malformed_and_fails_verify(tmp_path, capsys):
    path = write_trace(tmp_path / "run.jsonl")
    path.with_suffix(".tsr").write_bytes(b"not DER at all")
    code, result = verify_json(path, capsys)
    assert result["timestamp"]["status"] == "malformed"
    assert code == 1


def test_a_token_whose_nonce_differs_from_the_recorded_request_is_malformed(tmp_path, capsys):
    path = stamp(write_trace(tmp_path / "run.jsonl"), nonce=7)
    path.with_suffix(".timestamp.json").write_text(json.dumps({"nonce": "8"}), "utf-8")
    code, result = verify_json(path, capsys)
    assert result["timestamp"]["status"] == "malformed"
    assert code == 1


STANDALONE_CASES = {
    "unchecked": lambda p: stamp(p),
    "imprint_mismatch": lambda p: stamp(p, head="cd" * 32),
    "malformed": lambda p: p.with_suffix(".tsr").write_bytes(b"\x30\x03garbage"),
    "unavailable": lambda p: p.with_suffix(".timestamp.json").write_text(
        json.dumps({"status": "unavailable", "reason": "timed out"}), "utf-8"
    ),
    "none": lambda p: None,
}


@pytest.mark.parametrize("case", sorted(STANDALONE_CASES))
def test_the_standalone_verifier_agrees_with_the_sdk_on_timestamps(tmp_path, capsys, case):
    path = write_trace(tmp_path / "run.jsonl")
    STANDALONE_CASES[case](path)
    code_d, d = verify_json(path, capsys)
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(path), "--json"], capture_output=True, text=True
    )
    s = json.loads(proc.stdout)[0]
    assert s["timestamp"] == d["timestamp"]
    assert d["timestamp"]["status"] == case
    assert proc.returncode == code_d


# -- real tokens from a throwaway OpenSSL TSA -------------------------------------------


@pytest.fixture
def openssl_tsa(tmp_path):
    """A CA and a TSA certificate made with the openssl CLI, and a function that
    answers a TimeStampReq with a real signed token."""
    d = tmp_path / "tsa"
    d.mkdir()

    def run(*args):
        subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True, cwd=d)

    ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    run("req", "-x509", *ec, "-keyout", "ca.key", "-out", "ca.crt", "-days", "2",
        "-subj", "/CN=Deposition Test CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign")  # fmt: skip
    run("req", *ec, "-keyout", "tsa.key", "-out", "tsa.csr", "-subj", "/CN=Deposition Test TSA")
    (d / "ext.cnf").write_text(
        "extendedKeyUsage=critical,timeStamping\nkeyUsage=critical,digitalSignature\n", "utf-8"
    )
    run("x509", "-req", "-in", "tsa.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
        "-CAcreateserial", "-out", "tsa.crt", "-days", "2", "-extfile", "ext.cnf")  # fmt: skip
    (d / "serial").write_text("01\n", "utf-8")
    (d / "tsa.cnf").write_text(
        f"""[ tsa ]
default_tsa = tsa_config1
[ tsa_config1 ]
dir = {d}
serial = $dir/serial
signer_cert = $dir/tsa.crt
certs = $dir/ca.crt
signer_key = $dir/tsa.key
signer_digest = sha256
default_policy = 1.2.3.4.1
digests = sha256
accuracy = secs:1
ordering = no
tsa_name = no
ess_cert_id_chain = no
ess_cert_id_alg = sha256
""",
        "utf-8",
    )

    def reply(request: bytes) -> bytes:
        (d / "req.tsq").write_bytes(request)
        run("ts", "-reply", "-config", "tsa.cnf", "-queryfile", "req.tsq", "-out", "resp.tsr")
        return (d / "resp.tsr").read_bytes()

    return d, reply


@needs_openssl_3
def test_a_real_token_is_anchored_only_against_a_ca_the_user_chose(
    trace_dir, authority, openssl_tsa, capsys
):
    d, reply = openssl_tsa
    path = record_run(trace_dir, authority("openssl", reply=reply).url)
    assert path.with_suffix(".tsr").exists()

    code, result = verify_json(path, capsys)
    assert result["timestamp"]["status"] == "unchecked"

    code, result = verify_json(path, capsys, "--tsa-ca", d / "ca.crt")
    assert result["timestamp"]["status"] == "anchored", result["timestamp"]["message"]
    assert code == 0


@needs_openssl_3
def test_a_real_token_checked_against_the_wrong_ca_fails(
    trace_dir, authority, openssl_tsa, tmp_path, capsys
):
    d, reply = openssl_tsa
    path = record_run(trace_dir, authority("openssl", reply=reply).url)
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", "o.key", "-out", "o.crt", "-days", "2", "-subj", "/CN=Other CA"],
        check=True, capture_output=True, cwd=other,
    )  # fmt: skip
    code, result = verify_json(path, capsys, "--tsa-ca", other / "o.crt")
    assert result["timestamp"]["status"] == "bad_timestamp"
    assert code == 1
