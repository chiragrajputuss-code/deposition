"""Ed25519 signatures over a trace's head hash (ADR 005, layer 1).

The hash chain proves internal consistency and nothing else: whoever holds the
file can edit event 12, re-seal 12 onward with the same public algorithm, and
produce a trace that ``deposition verify`` calls clean. A signature is what
makes that rewrite detectable, because re-sealing changes the head hash and the
attacker cannot produce a new signature over it without the private key.

What this layer honestly buys, and what it does not:

* It binds a trace's content to *a key*. If the verifier knows which key to
  expect, a rewritten trace fails.
* It does **not** say whose key it is. A sidecar carries its own public key, and
  an attacker who rewrites the trace can sign it with a key of their own and
  overwrite that field. Verification without a pinned key therefore reports
  ``signed by an unverified key`` - never a clean bill of health.
* It does **not** prove time. ``signed_at`` is asserted by the signer.
* A key sitting on the same disk as the trace protects against very little.
  Layer 2 (countersigning at ingest, ADR 005) is the real answer.

Optional by design: this module is the only place in the SDK that imports a
third-party package, behind the ``signing`` extra, so the zero-dependency rule
in ADR 003 still holds for everyone who does not ask for signatures.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import canonical_bytes, canonical_json, now_ts

__all__ = [
    "SIGNATURE_VERSION",
    "SignatureError",
    "SignatureResult",
    "SigningKey",
    "generate_key",
    "load_key",
    "load_public_key",
    "sidecar_path",
    "sign_head",
    "read_sidecar",
    "write_sidecar",
    "verify_sidecar",
]

#: Sidecar format version. Bumped independently of the trace schema - a
#: signature is metadata beside the trace, never part of it.
SIGNATURE_VERSION = "0.1"

ALGORITHM = "ed25519"

_INSTALL_HINT = (
    "Ed25519 signing needs its extra dependency.\n"
    "  Install it with:  pip install 'deposition[signing]'"
)


class SignatureError(Exception):
    """A signing key or sidecar could not be loaded, written or parsed."""


def _ed25519() -> Any:
    """Import the backend, turning a missing extra into a readable error."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SignatureError(_INSTALL_HINT) from exc
    return ed25519


# -- keys -------------------------------------------------------------------


@dataclass(frozen=True)
class SigningKey:
    """An Ed25519 private key, with its public half as 64 hex characters."""

    _private: Any = field(repr=False)
    public_hex: str

    def sign(self, message: bytes) -> str:
        return self._private.sign(message).hex()

    def write(self, path: str | os.PathLike[str]) -> Path:
        """Write the private key as unencrypted PKCS#8 PEM, readable only by its owner."""
        from cryptography.hazmat.primitives import serialization

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        pem = self._private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        # Create with 0600 from the start: a private key must never exist, even
        # for an instant, at the umask's default permissions.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        target.with_suffix(target.suffix + ".pub").write_text(self.public_hex + "\n", "utf-8")
        return target


def _public_hex(private: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def generate_key() -> SigningKey:
    """A new random Ed25519 keypair."""
    private = _ed25519().Ed25519PrivateKey.generate()
    return SigningKey(private, _public_hex(private))


def load_key(source: str | os.PathLike[str] | bytes | SigningKey) -> SigningKey:
    """Load a signing key from a PEM file, PEM/raw bytes, or pass one through.

    Accepts a 32-byte raw seed as well, so a key held in an environment variable
    (``bytes.fromhex(os.environ[...])``) works without a file on disk.
    """
    if isinstance(source, SigningKey):
        return source

    ed25519 = _ed25519()
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if not path.exists():
            raise SignatureError(f"no such signing key: {path}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SignatureError(f"cannot read signing key {path}: {exc}") from exc
    else:
        data = bytes(source)

    if len(data) == 32:
        private = ed25519.Ed25519PrivateKey.from_private_bytes(data)
        return SigningKey(private, _public_hex(private))

    from cryptography.hazmat.primitives import serialization

    try:
        private = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise SignatureError(
            "signing key is not an unencrypted Ed25519 PKCS#8 PEM or a 32-byte raw key"
        ) from exc
    if not isinstance(private, ed25519.Ed25519PrivateKey):
        raise SignatureError(f"signing key is {type(private).__name__}, not Ed25519")
    return SigningKey(private, _public_hex(private))


def load_public_key(source: str | os.PathLike[str]) -> str:
    """Normalise a pinned public key given as 64 hex characters or a path to them."""
    text = str(source).strip()
    candidate = text
    path = Path(text)
    if len(text) != 64 and path.exists():
        try:
            candidate = path.read_text("utf-8").strip()
        except OSError as exc:
            raise SignatureError(f"cannot read public key {path}: {exc}") from exc
    candidate = candidate.strip().lower()
    if len(candidate) != 64:
        raise SignatureError(
            f"public key must be 64 hex characters (32 bytes), got {len(candidate)}"
        )
    try:
        bytes.fromhex(candidate)
    except ValueError as exc:
        raise SignatureError("public key is not valid hex") from exc
    return candidate


# -- signing ----------------------------------------------------------------


def sidecar_path(trace_path: str | os.PathLike[str]) -> Path:
    """``run_x.jsonl`` -> ``run_x.sig``, the file ``verify`` looks for."""
    return Path(trace_path).with_suffix(".sig")


def _signable(claims: dict[str, Any]) -> bytes:
    """The bytes actually signed: every claim except the signature itself.

    Signing the head hash alone would let a valid signature be lifted onto a
    different run's sidecar, so run_id, seq and the asserted time are bound in
    too.
    """
    return canonical_bytes({k: v for k, v in claims.items() if k != "signature"})


def sign_head(
    key: SigningKey,
    *,
    run_id: str,
    head_hash: str,
    seq: int,
    events: int,
    signed_at: str | None = None,
) -> dict[str, Any]:
    """Build a signed sidecar for a finished chain."""
    claims = {
        "v": SIGNATURE_VERSION,
        "alg": ALGORITHM,
        "run_id": run_id,
        "head_hash": head_hash,
        "seq": seq,
        "events": events,
        "public_key": key.public_hex,
        "signed_at": signed_at or now_ts(),
    }
    claims["signature"] = key.sign(_signable(claims))
    return claims


def write_sidecar(sidecar: dict[str, Any], trace_path: str | os.PathLike[str]) -> Path:
    """Write ``run_x.sig`` beside its trace, atomically."""
    target = sidecar_path(trace_path)
    tmp = target.with_suffix(".sig.tmp")
    tmp.write_text(canonical_json(sidecar) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def read_sidecar(trace_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Read the sidecar beside a trace, or ``None`` if there is none."""
    target = sidecar_path(trace_path)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SignatureError(f"cannot read {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise SignatureError(f"{target} does not contain a signature object")
    return data


# -- verification -----------------------------------------------------------

#: Ordered worst to best; ``ok`` is true only for :data:`VERIFIED`.
UNSIGNED = "unsigned"
UNAVAILABLE = "unavailable"
MALFORMED = "malformed"
BAD_SIGNATURE = "bad_signature"
HEAD_MISMATCH = "head_mismatch"
KEY_MISMATCH = "key_mismatch"
UNPINNED = "unpinned"
VERIFIED = "verified"


@dataclass
class SignatureResult:
    """What a sidecar does and does not establish about a trace."""

    status: str
    message: str
    public_key: str | None = None
    signed_at: str | None = None

    @property
    def ok(self) -> bool:
        """True only when the signature checks out against a key the caller pinned."""
        return self.status == VERIFIED

    @property
    def trusted(self) -> bool:
        """True when nothing is wrong, even if no key was pinned to check against."""
        return self.status in (VERIFIED, UNPINNED)

    def __str__(self) -> str:
        return self.message


def verify_sidecar(
    sidecar: dict[str, Any] | None,
    *,
    head_hash: str | None,
    run_id: str | None = None,
    expected_public_key: str | None = None,
) -> SignatureResult:
    """Check a sidecar against the chain that was just verified.

    Never raises: like chain verification, a bad signature is a result the
    caller decides about, not an exception.
    """
    if sidecar is None:
        return SignatureResult(UNSIGNED, "unsigned (no .sig file beside this trace)")

    claims = {k: sidecar.get(k) for k in sidecar}
    signature = claims.get("signature")
    public_key = claims.get("public_key")
    for name, value in (
        ("signature", signature),
        ("public_key", public_key),
        ("head_hash", claims.get("head_hash")),
    ):
        if not isinstance(value, str) or not value:
            return SignatureResult(MALFORMED, f"signature file has no usable {name}")
    if claims.get("alg") != ALGORITHM:
        return SignatureResult(MALFORMED, f"unsupported signature algorithm {claims.get('alg')!r}")

    signed_at = claims.get("signed_at") if isinstance(claims.get("signed_at"), str) else None

    try:
        ed25519 = _ed25519()
    except SignatureError as exc:
        return SignatureResult(UNAVAILABLE, str(exc), public_key=public_key, signed_at=signed_at)

    try:
        key = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        key.verify(bytes.fromhex(signature), _signable(claims))
    except ValueError:
        return SignatureResult(
            MALFORMED,
            "signature or public key is not valid hex",
            public_key=public_key,
            signed_at=signed_at,
        )
    except Exception:  # noqa: BLE001 - InvalidSignature lives in a lazily imported module
        return SignatureResult(
            BAD_SIGNATURE,
            "signature does not match this signature file - it was altered or forged",
            public_key=public_key,
            signed_at=signed_at,
        )

    # The signature is authentic; now ask whether it is about *this* trace.
    if head_hash is not None and claims["head_hash"] != head_hash:
        return SignatureResult(
            HEAD_MISMATCH,
            f"signature covers head {claims['head_hash'][:12]}... but this trace ends at "
            f"{head_hash[:12]}... - the trace was rewritten after it was signed",
            public_key=public_key,
            signed_at=signed_at,
        )
    if run_id is not None and claims.get("run_id") not in (None, run_id):
        return SignatureResult(
            HEAD_MISMATCH,
            f"signature belongs to run {claims.get('run_id')!r}, not {run_id!r}",
            public_key=public_key,
            signed_at=signed_at,
        )

    if expected_public_key is None:
        return SignatureResult(
            UNPINNED,
            f"signed by an unverified key {public_key[:12]}... - pass --pubkey to check it "
            "against the key you expect",
            public_key=public_key,
            signed_at=signed_at,
        )
    if expected_public_key.lower() != public_key.lower():
        return SignatureResult(
            KEY_MISMATCH,
            f"signed by {public_key[:12]}..., not the expected key {expected_public_key[:12]}...",
            public_key=public_key,
            signed_at=signed_at,
        )
    return SignatureResult(
        VERIFIED,
        f"signature verified against {public_key[:12]}...",
        public_key=public_key,
        signed_at=signed_at,
    )
