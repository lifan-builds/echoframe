"""Experimental Silk Instant Share wire compatibility, not a general crypto library.

J-PAKE SHA-256/NIST-3072 as observed in Silk's bundled Bouncy Castle classes.
Preserves proof validation and mutual key confirmation. Never guesses PINs.
"""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import struct

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from echoframe.instant_share_group import P, Q, G


def unsigned(n):
    return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")


def java_positive(n):
    value = unsigned(n)
    return b"\0" + value if value[0] & 128 else value


def proof_hash(base, commitment, public, identity):
    parts = [unsigned(base), unsigned(commitment), unsigned(public), identity.encode()]
    digest = hashlib.sha256(b"".join(struct.pack("!I", len(p)) + p for p in parts)).digest()
    return int.from_bytes(digest, "big", signed=True)


def proof(base, public, secret, identity):
    nonce = secrets.randbelow(Q)
    commitment = pow(base, nonce, P)
    response = (nonce - secret * proof_hash(base, commitment, public, identity)) % Q
    return [str(commitment), str(response)]


def integer(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("Invalid protocol integer")
    return int(value)


def validate_proof(base, public, supplied, identity):
    if not isinstance(supplied, list) or len(supplied) != 2:
        raise ValueError("Invalid proof shape")
    commitment, response = map(integer, supplied)
    if not (0 < public < P and pow(public, Q, P) == 1 and 0 < commitment < P and 0 <= response < Q):
        raise ValueError("Invalid group element or proof range")
    challenge = proof_hash(base, commitment, public, identity)
    if pow(base, response, P) * pow(public, challenge, P) % P != commitment:
        raise ValueError("J-PAKE proof validation failed")


class Participant:
    def __init__(self, identity, pin):
        if len(pin) != 4 or not pin.isascii() or not pin.isdigit():
            raise ValueError("Expected the four-digit PIN displayed by this device")
        self.identity = identity
        self.password = int.from_bytes(pin.encode(), "big") % Q
        self.state = 0

    def round1(self):
        if self.state != 0:
            raise ValueError("Round 1 already started")
        self.x1, self.x2 = secrets.randbelow(Q), secrets.randbelow(Q - 1) + 1
        self.gx1, self.gx2 = pow(G, self.x1, P), pow(G, self.x2, P)
        self.state = 1
        return {"round": 1, "participantId": self.identity, "gx1": str(self.gx1), "gx2": str(self.gx2),
                "knowledgeProofForX1": proof(G, self.gx1, self.x1, self.identity),
                "knowledgeProofForX2": proof(G, self.gx2, self.x2, self.identity)}

    def round2(self, peer):
        if self.state != 1 or peer.get("round") != 1:
            raise ValueError("Unexpected round 1 response")
        self.peer = peer["participantId"]
        if not isinstance(self.peer, str) or not 1 <= len(self.peer) <= 256 or self.peer == self.identity:
            raise ValueError("Invalid peer identity")
        self.gx3, self.gx4 = integer(peer["gx1"]), integer(peer["gx2"])
        if self.gx4 == 1:
            raise ValueError("Invalid second public value")
        validate_proof(G, self.gx3, peer["knowledgeProofForX1"], self.peer)
        validate_proof(G, self.gx4, peer["knowledgeProofForX2"], self.peer)
        base = self.gx1 * self.gx3 * self.gx4 % P
        if base == 1:
            raise ValueError("Degenerate round 2 base")
        exponent = self.x2 * self.password % Q
        value = pow(base, exponent, P)
        self.state = 2
        return {"round": 2, "participantId": self.identity, "a": str(value),
                "knowledgeProofForX2": proof(base, value, exponent, self.identity)}

    def tag(self, peer_first=False):
        key = hashlib.sha256(unsigned(self.material) + b"JPAKE_KC").digest()
        ids = (self.peer, self.identity) if peer_first else (self.identity, self.peer)
        elements = (self.gx3, self.gx4, self.gx1, self.gx2) if peer_first else (self.gx1, self.gx2, self.gx3, self.gx4)
        message = b"KC_1_U" + ids[0].encode() + ids[1].encode() + b"".join(map(unsigned, elements))
        return int.from_bytes(hmac.digest(key, message, "sha256"), "big", signed=True)

    def round3(self, peer):
        if self.state != 2 or peer.get("round") != 2 or peer.get("participantId") != self.peer:
            raise ValueError("Unexpected round 2 identity or response")
        value = integer(peer["a"])
        base = self.gx3 * self.gx1 * self.gx2 % P
        if base == 1:
            raise ValueError("Degenerate peer base")
        validate_proof(base, value, peer["knowledgeProofForX2"], self.peer)
        self.material = pow(pow(self.gx4, (-self.x2 * self.password) % Q, P) * value % P, self.x2, P)
        self.password = self.x1 = self.x2 = None
        self.state = 3
        return {"round": 3, "participantId": self.identity, "macTag": str(self.tag())}

    def finish(self, peer):
        if self.state != 3 or peer.get("round") != 3 or peer.get("participantId") != self.peer:
            raise ValueError("Unexpected round 3 identity or response")
        # Java BigInteger MAC values are signed decimal strings on the wire.
        actual = integer(peer["macTag"])
        if not -(1 << 255) <= actual < (1 << 255):
            raise ValueError("Invalid MAC range")
        if not hmac.compare_digest(actual.to_bytes(32, "big", signed=True), self.tag(True).to_bytes(32, "big", signed=True)):
            raise ValueError("Pairing confirmation failed: PIN or peer transcript mismatch")
        key = java_positive(self.material)[:32].ljust(32, b"\0")
        self.material = None
        self.state = 4
        return key


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")


def encode(value):
    return base64.b64encode(value).decode("ascii")


def encrypt_message(key, payload):
    iv = secrets.token_bytes(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(json.dumps(payload).encode()) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    data = cipher.update(padded) + cipher.finalize()
    return {"data": encode(data), "iv": encode(iv), "signature": encode(hmac.digest(key, data, "sha256"))}


def decrypt_message(key, message):
    data, iv, signature = [base64.b64decode(message[k], validate=True) for k in ("data", "iv", "signature")]
    if not hmac.compare_digest(signature, hmac.digest(key, data, "sha256")):
        raise ValueError("Response signature mismatch")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    payload = json.loads(unpadder.update(padded) + unpadder.finalize())
    age = (datetime.now(timezone.utc) - datetime.strptime(payload["timestamp"], "%Y-%m-%d %H:%M:%S%z")).total_seconds()
    if not -60 <= age <= 300:
        raise ValueError("Response timestamp is stale or in the future")
    return payload


def salted_key(key, salt):
    return hashlib.pbkdf2_hmac("sha1", base64.b64encode(key), salt, 1000, dklen=20)
