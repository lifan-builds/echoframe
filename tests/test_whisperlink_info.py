# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. EchoFrame; Section 7 attribution terms in ATTRIBUTION.md.
import struct
import unittest
from unittest.mock import patch

from echoframe.whisperlink_info import Reader, query, summarize_device


def text(value):
    encoded = value.encode()
    return struct.pack("!i", len(encoded)) + encoded


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def recv(self, size):
        return next(self.chunks, b"")

    def sendall(self, data):
        self.sent.append(data)


class WhisperLinkTests(unittest.TestCase):
    def test_nested_struct_and_map(self):
        # struct field 1 = map<string,string>, field 4 = i32.
        payload = b"\x0d\x00\x01\x0b\x0b" + struct.pack("!i", 1)
        payload += text("key") + text("value") + b"\x08\x00\x04" + struct.pack("!i", 7) + b"\0"
        reader = Reader(payload)
        self.assertEqual(reader.value(12), {"1": [["key", "value"]], "4": 7})
        self.assertEqual(reader.offset, len(payload))

    def test_invalid_and_incomplete_lengths(self):
        with self.assertRaises(ValueError):
            Reader(struct.pack("!i", -1)).string()
        with self.assertRaises(EOFError):
            Reader(struct.pack("!i", 5) + b"a").string()
        with self.assertRaises(ValueError):
            Reader(b"\x0b\x0b" + struct.pack("!i", 100000)).value(13)

    def test_identity_output_excludes_account_and_validation_data(self):
        device = {"1": "Test Show", "2": "private-uuid", "6": "private-account", "4": {
            "3": "Amazon", "7": {"2": [["dev.display.res.w", "1024"], ["cachedValidationData", "private-key-data"]]}
        }}
        result = summarize_device(device)
        self.assertEqual(result["capabilities"], {"dev.display.res.w": "1024"})
        self.assertNotIn("private", str(result))

    def test_query_handles_fragmentation(self):
        reply = struct.pack("!I", 0x80010002) + text("getFullDeviceInfo") + struct.pack("!i", 1)
        reply += b"\x0c\x00\x00\x0b\x00\x01" + text("Test Show") + b"\0\0"
        sock = FakeSocket([b"HTTP/1.0 200 ", b"OK\r\n\r\n", reply[:12], reply[12:]])
        with patch("echoframe.whisperlink_info.socket.create_connection", return_value=sock):
            self.assertEqual(query("192.0.2.1", 1234, "test", "getFullDeviceInfo"), {"0": {"1": "Test Show"}})
        self.assertEqual(len(sock.sent), 2)
        self.assertIn(b"getFullDeviceInfo", sock.sent[1])

    def test_rejected_handshake_sends_no_rpc(self):
        sock = FakeSocket([b"HTTP/1.0 401 Fail\r\n\r\n"])
        with patch("echoframe.whisperlink_info.socket.create_connection", return_value=sock):
            with self.assertRaises(ConnectionError):
                query("192.0.2.1", 1234, "test", "getFullDeviceInfo")
        self.assertEqual(len(sock.sent), 1)

    def test_mutating_method_rejected_before_connect(self):
        with patch("echoframe.whisperlink_info.socket.create_connection") as connect:
            with self.assertRaises(ValueError):
                query("192.0.2.1", 1234, "test", "setMediaSource")
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
