# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from echoframe.instant_share import authenticate, read_json_line, write_private
from echoframe.instant_share_crypto import Participant, decrypt_message, encode, encrypt_message, java_positive, salted_key, timestamp
from echoframe.instant_share_group import P, Q, G


def exchange(pin_a="1234", pin_b="1234"):
    a, b = Participant("controller", pin_a), Participant("display", pin_b)
    a1, b1 = a.round1(), b.round1()
    a2, b2 = a.round2(b1), b.round2(a1)
    a3, b3 = a.round3(b2), b.round3(a2)
    return a, b, a3, b3


class PairingTests(unittest.TestCase):
    def test_group(self):
        self.assertEqual(P.bit_length(), 3072)
        self.assertEqual(Q.bit_length(), 256)
        self.assertEqual((P - 1) % Q, 0)
        self.assertEqual(pow(G, Q, P), 1)

    def test_correct_pin_mutual_confirmation(self):
        a, b, a3, b3 = exchange()
        self.assertEqual(a.finish(b3), b.finish(a3))
        with self.assertRaises(ValueError):
            a.finish(b3)

    def test_wrong_pin_rejected(self):
        a, b, a3, b3 = exchange(pin_b="4321")
        with self.assertRaises(ValueError):
            a.finish(b3)
        with self.assertRaises(ValueError):
            b.finish(a3)

    def test_round1_invalid_proof_and_identity(self):
        a, b = Participant("a", "1234"), Participant("b", "1234")
        a.round1()
        message = b.round1()
        bad = copy.deepcopy(message)
        bad["knowledgeProofForX1"][1] = str((int(bad["knowledgeProofForX1"][1]) + 1) % Q)
        with self.assertRaises(ValueError):
            a.round2(bad)
        message["participantId"] = "a"
        with self.assertRaises(ValueError):
            a.round2(message)

    def test_round2_wrong_peer_and_proof(self):
        a, b = Participant("a", "1234"), Participant("b", "1234")
        a1, b1 = a.round1(), b.round1()
        a.round2(b1)
        b2 = b.round2(a1)
        bad = dict(b2, participantId="unexpected")
        with self.assertRaises(ValueError):
            a.round3(bad)
        bad = dict(b2, a="0")
        with self.assertRaises(ValueError):
            a.round3(bad)

    def test_round3_tampering(self):
        a, _, _, b3 = exchange()
        b3["macTag"] = str(int(b3["macTag"]) ^ 1)
        with self.assertRaises(ValueError):
            a.finish(b3)

    def test_pin_validation(self):
        for pin in ["", "123", "12345", "abcd", "１２３４"]:
            with self.assertRaises(ValueError):
                Participant("a", pin)

    def test_java_integer_encoding(self):
        self.assertEqual(java_positive(0x80), b"\0\x80")
        self.assertEqual(java_positive(0x7f), b"\x7f")


class MessageTests(unittest.TestCase):
    def test_encryption_and_integrity(self):
        key = bytes(range(32))
        payload = {"timestamp": timestamp(), "url": "http://192.0.2.1:8765/"}
        encrypted = encrypt_message(key, payload)
        self.assertEqual(decrypt_message(key, encrypted), payload)
        data = bytearray(base64.b64decode(encrypted["data"]))
        data[0] ^= 1
        encrypted["data"] = encode(data)
        with self.assertRaises(ValueError):
            decrypt_message(key, encrypted)

    def test_stale_response(self):
        key = bytes(range(32))
        encrypted = encrypt_message(key, {"timestamp": "2000-01-01 00:00:00+0000"})
        with self.assertRaises(ValueError):
            decrypt_message(key, encrypted)

    def test_auth_check_mock_peer(self):
        key = bytes(range(32))
        def peer(host, port, request):
            self.assertEqual(request["operation"], "authCheck")
            data = decrypt_message(key, request)
            salt = base64.b64decode(data["salt"])
            self.assertEqual(base64.b64decode(data["key"]), salted_key(key, salt))
            return {"status": 200, **encrypt_message(key, data)}
        with patch("echoframe.instant_share.rpc", side_effect=peer):
            authenticate("192.0.2.1", 1234, "controller", key)

    def test_framing(self):
        self.assertEqual(read_json_line(io.BytesIO(b'{"status":200}\n')), {"status": 200})
        for raw in [b"", b"{}", b"[]\n", b"x" * 32769 + b"\n"]:
            with self.assertRaises(ValueError):
                read_json_line(io.BytesIO(raw))

    def test_private_file_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private" / "key.json"
            write_private(path, {"key": "example"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            write_private(path, {"key": "replacement"})
            self.assertEqual(json.loads(path.read_text()), {"key": "replacement"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
