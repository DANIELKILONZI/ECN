"""
tests/test_crypto.py
--------------------
Unit tests for ecn.crypto.
"""

import pytest
from ecn.crypto import (
    generate_keypair,
    public_key_to_hex,
    public_key_from_hex,
    sign_result,
    verify_result,
    SignedResult,
)


class TestKeyGeneration:
    def test_generate_keypair_returns_two_objects(self):
        priv, pub = generate_keypair()
        assert priv is not None
        assert pub is not None

    def test_public_key_to_hex_is_64_chars(self):
        _, pub = generate_keypair()
        hex_str = public_key_to_hex(pub)
        assert isinstance(hex_str, str)
        assert len(hex_str) == 64  # 32 bytes → 64 hex chars

    def test_public_key_round_trip(self):
        _, pub = generate_keypair()
        hex_str = public_key_to_hex(pub)
        recovered = public_key_from_hex(hex_str)
        # Round-trip should produce an equivalent key
        assert public_key_to_hex(recovered) == hex_str

    def test_different_keypairs_differ(self):
        _, pub1 = generate_keypair()
        _, pub2 = generate_keypair()
        assert public_key_to_hex(pub1) != public_key_to_hex(pub2)


class TestSignVerify:
    def setup_method(self):
        self.priv, self.pub = generate_keypair()

    def test_valid_signature_verifies(self):
        sig = sign_result("Node-1", "abc123", self.priv)
        assert verify_result("Node-1", "abc123", sig, self.pub)

    def test_wrong_node_id_fails(self):
        sig = sign_result("Node-1", "abc123", self.priv)
        assert not verify_result("Node-2", "abc123", sig, self.pub)

    def test_wrong_hash_fails(self):
        sig = sign_result("Node-1", "abc123", self.priv)
        assert not verify_result("Node-1", "xyz999", sig, self.pub)

    def test_corrupted_signature_fails(self):
        sig = sign_result("Node-1", "abc123", self.priv)
        bad_sig = sig[:-8] + "00000000"
        assert not verify_result("Node-1", "abc123", bad_sig, self.pub)

    def test_wrong_key_fails(self):
        sig = sign_result("Node-1", "abc123", self.priv)
        _, other_pub = generate_keypair()
        assert not verify_result("Node-1", "abc123", sig, other_pub)

    def test_signature_is_hex_string(self):
        sig = sign_result("Node-1", "abc123", self.priv)
        assert isinstance(sig, str)
        # Ed25519 sig = 64 bytes = 128 hex chars
        assert len(sig) == 128
        int(sig, 16)  # should not raise

    def test_signing_is_deterministic_for_same_inputs(self):
        # Ed25519 is deterministic (RFC 8032)
        sig1 = sign_result("Node-1", "abc123", self.priv)
        sig2 = sign_result("Node-1", "abc123", self.priv)
        assert sig1 == sig2

    def test_different_inputs_produce_different_sigs(self):
        sig1 = sign_result("Node-1", "hash_A", self.priv)
        sig2 = sign_result("Node-1", "hash_B", self.priv)
        assert sig1 != sig2


class TestSignedResult:
    def setup_method(self):
        self.priv, self.pub = generate_keypair()
        sig = sign_result("Node-1", "abc123", self.priv)
        self.sr = SignedResult(node_id="Node-1", state_hash="abc123", signature=sig)

    def test_verify_valid(self):
        assert self.sr.verify(self.pub)

    def test_verify_invalid_key(self):
        _, other_pub = generate_keypair()
        assert not self.sr.verify(other_pub)

    def test_to_dict_keys(self):
        d = self.sr.to_dict()
        assert set(d.keys()) == {"node_id", "state_hash", "signature"}

    def test_round_trip_dict(self):
        d = self.sr.to_dict()
        recovered = SignedResult.from_dict(d)
        assert recovered.node_id == self.sr.node_id
        assert recovered.state_hash == self.sr.state_hash
        assert recovered.signature == self.sr.signature
        assert recovered.verify(self.pub)

    def test_repr(self):
        r = repr(self.sr)
        assert "Node-1" in r
        assert "abc123" in r
