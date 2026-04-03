"""
crypto.py
---------
Cryptographic trust layer for the Executable Consensus Network.

Provides:
- Ed25519 key-pair generation per node
- Signing of node results (node_id + state_hash)
- Verification of signatures before consensus
- SignedResult message format:
    {
        "node_id":    "node_1",
        "state_hash": "abc123...",
        "signature":  "<hex-encoded Ed25519 signature>"
    }

Uses the `cryptography` library (Apache-licensed, widely available).
No secrets are hard-coded; every node generates ephemeral keys on startup.
"""

import hashlib
from typing import Dict

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
)
from cryptography.exceptions import InvalidSignature


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """
    Generate a fresh Ed25519 key pair.

    Returns
    -------
    (private_key, public_key)
        Both are cryptography library objects.
    """
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


def public_key_to_hex(public_key: Ed25519PublicKey) -> str:
    """Serialize a public key to a hex string (raw 32-byte Ed25519 public key)."""
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return raw.hex()


def public_key_from_hex(hex_str: str) -> Ed25519PublicKey:
    """Deserialize a public key from a hex string."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    raw = bytes.fromhex(hex_str)
    return Ed25519PublicKey.from_public_bytes(raw)


# ---------------------------------------------------------------------------
# Signing & verification
# ---------------------------------------------------------------------------

def _build_message(node_id: str, state_hash: str) -> bytes:
    """
    Build the canonical byte string that is signed / verified.

    We hash the concatenation so the signature covers a fixed-length
    input regardless of node_id length.
    """
    payload = f"{node_id}:{state_hash}".encode("utf-8")
    return hashlib.sha256(payload).digest()


def sign_result(
    node_id: str,
    state_hash: str,
    private_key: Ed25519PrivateKey,
) -> str:
    """
    Sign (node_id, state_hash) with *private_key*.

    Returns
    -------
    str
        Hex-encoded 64-byte Ed25519 signature.
    """
    message = _build_message(node_id, state_hash)
    raw_sig = private_key.sign(message)
    return raw_sig.hex()


def verify_result(
    node_id: str,
    state_hash: str,
    signature_hex: str,
    public_key: Ed25519PublicKey,
) -> bool:
    """
    Verify that *signature_hex* is a valid Ed25519 signature of
    (node_id, state_hash) under *public_key*.

    Returns
    -------
    bool
        ``True`` if valid, ``False`` if the signature is forged / corrupted.
    """
    try:
        message = _build_message(node_id, state_hash)
        raw_sig = bytes.fromhex(signature_hex)
        public_key.verify(raw_sig, message)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------------------
# SignedResult — wire message format
# ---------------------------------------------------------------------------

class SignedResult:
    """
    A node's execution result with an attached cryptographic signature.

    This is the message format exchanged over the real network.  Receivers
    must call ``verify()`` before trusting the contents.

    Attributes
    ----------
    node_id : str
    state_hash : str
    signature : str
        Hex-encoded Ed25519 signature over SHA-256(node_id + ":" + state_hash).
    """

    __slots__ = ("node_id", "state_hash", "signature")

    def __init__(self, node_id: str, state_hash: str, signature: str) -> None:
        self.node_id = node_id
        self.state_hash = state_hash
        self.signature = signature

    # ------------------------------------------------------------------
    # Serialisation helpers (for network transport)
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, str]:
        return {
            "node_id": self.node_id,
            "state_hash": self.state_hash,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, str]) -> "SignedResult":
        return cls(
            node_id=d["node_id"],
            state_hash=d["state_hash"],
            signature=d["signature"],
        )

    def verify(self, public_key: Ed25519PublicKey) -> bool:
        """Return True if this result's signature is valid under *public_key*."""
        return verify_result(
            self.node_id, self.state_hash, self.signature, public_key
        )

    def __repr__(self) -> str:
        return (
            f"SignedResult(node_id={self.node_id!r}, "
            f"hash={self.state_hash[:16]}..., "
            f"sig={self.signature[:16]}...)"
        )
