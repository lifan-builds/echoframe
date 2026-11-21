"""Experimental account-free Silk controller. Run: python3 -m scripts.instant_share --help.

Uses normal displayed-PIN pairing; no brute force, APK installation or exploit.
Pairing keys stay in a private file outside this repository, never in stdout.
"""
import argparse
import base64
import getpass
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import time
from urllib.parse import urlsplit
import uuid

from scripts.instant_share_crypto import Participant, decrypt_message, encode, encrypt_message, salted_key, timestamp

DEFAULT_STATE = Path.home() / ".local/share/alexa-photos/echo-show-silk.json"


def detect_lan_ip(peer_ip: str = "192.0.2.1") -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((peer_ip, 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pairing-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json_line(stream):
    line = stream.readline(32769)
    if not line.endswith(b"\n") or len(line) > 32768:
        raise ValueError("Incomplete or oversized protocol response")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def send_json_line(stream, payload):
    stream.write(json.dumps(payload).encode() + b"\n")
    stream.flush()


def rpc(host, port, payload):
    with socket.create_connection((host, port), timeout=8) as sock:
        with sock.makefile("rwb") as stream:
            send_json_line(stream, payload)
            return read_json_line(stream)


def launch(host, local_ip, dial_port=8009):
    with socket.socket() as listener:
        listener.bind((local_ip, 0))
        listener.listen(2)
        listener.settimeout(15)
        payload = {"clientAddress": local_ip, "clientPort": listener.getsockname()[1], "deviceName": "Local Photo Album"}
        connection = http.client.HTTPConnection(host, dial_port, timeout=5)
        try:
            # Native controller, not a browser-origin request. Do not spoof an Origin.
            connection.request("POST", "/apps/com.amazon.cloud9", json.dumps(payload), {"Content-Type": "text/plain"})
            response = connection.getresponse()
            response.read(4096)
            if response.status != 201:
                raise RuntimeError(f"Silk DIAL launch returned HTTP {response.status}")
        finally:
            connection.close()
        sock, address = listener.accept()
        with sock:
            if address[0] != host:
                raise ValueError("Callback did not come from the selected Echo")
            sock.settimeout(5)
            with sock.makefile("rb") as stream:
                port = read_json_line(stream).get("servicePort")
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("Invalid callback service port")
            return port


def authenticate(host, port, client_id, key):
    salt = secrets.token_bytes(24)
    payload = {"timestamp": timestamp(), "salt": encode(salt), "key": encode(salted_key(key, salt))}
    reply = rpc(host, port, {"operation": "authCheck", "clientId": client_id, **encrypt_message(key, payload)})
    if reply.get("status") != 200:
        raise RuntimeError(f"Authentication rejected: {reply.get('status')}")
    message = decrypt_message(key, reply)
    response_salt = base64.b64decode(message["salt"], validate=True)
    response_key = base64.b64decode(message["key"], validate=True)
    if not hmac.compare_digest(salted_key(key, response_salt), response_key):
        raise ValueError("Peer did not confirm the shared key")


def begin_pairing(host, local_ip, pending):
    port = launch(host, local_ip)
    client_id = "alexa-photos-" + uuid.uuid4().hex
    reply = rpc(host, port, {"operation": "startPairing", "clientId": client_id})
    if reply.get("status") != 200:
        raise RuntimeError(f"Pairing request rejected: {reply.get('status')}")
    pair_port = reply.get("jpakePort")
    if not isinstance(pair_port, int) or not 1 <= pair_port <= 65535:
        raise ValueError("Invalid pairing port")
    write_private(pending, {"host": host, "port": port, "clientId": client_id, "jpakePort": pair_port, "created": time.time()})
    print("Pairing requested. Read the four-digit code on the Kitchen Show; it expires in about five minutes.")


def complete_pairing(pending, state, pin):
    info = json.loads(pending.read_text())
    if time.time() - info["created"] > 290:
        raise ValueError("Pairing expired. Run begin again for a new on-screen code.")
    participant = Participant(info["clientId"], pin)
    with socket.create_connection((info["host"], info["jpakePort"]), timeout=5) as sock:
        with sock.makefile("rwb") as stream:
            send_json_line(stream, participant.round1())
            send_json_line(stream, participant.round2(read_json_line(stream)))
            send_json_line(stream, participant.round3(read_json_line(stream)))
            key = participant.finish(read_json_line(stream))
    # The peer stores its key immediately after mutual confirmation.
    time.sleep(0.3)
    authenticate(info["host"], info["port"], info["clientId"], key)
    write_private(state, {"host": info["host"], "clientId": info["clientId"], "key": encode(key)})
    pending.unlink()
    print("Pairing and mutual authentication succeeded. Private key saved outside the project.")


def open_url(state, local_ip, url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Supply an HTTP(S) album URL without credentials")
    if state.stat().st_mode & 0o077:
        raise ValueError("Pairing state must be private (chmod 600)")
    saved = json.loads(state.read_text())
    host = str(ipaddress.IPv4Address(saved["host"]))
    key = base64.b64decode(saved["key"], validate=True)
    if len(key) != 32:
        raise ValueError("Invalid saved key length")
    port = launch(host, local_ip)
    authenticate(host, port, saved["clientId"], key)
    reply = rpc(host, port, {"operation": "loadUrl", "clientId": saved["clientId"], **encrypt_message(key, {"timestamp": timestamp(), "url": url})})
    if reply.get("status") != 200:
        raise RuntimeError(f"URL request rejected: {reply.get('status')}")
    print("Echo accepted loadUrl. Verify a new album heartbeat and actual screen visibility; acceptance is not proof of display.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--local-ip", default=None, help="This computer's Wi-Fi IPv4 address (auto-detected if omitted)")
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("begin", help="Launch local sharing and request an on-screen pairing PIN")
    begin.add_argument("--host", required=True, help="Echo Show LAN IPv4 address")
    commands.add_parser("pair", help="Complete one pairing attempt; PIN is prompted, not a command-line argument")
    opener = commands.add_parser("open", help="Open a URL using a previously confirmed pairing")
    opener.add_argument("url")
    args = parser.parse_args()
    state = args.state.expanduser().resolve()
    pending = state.with_suffix(".pending.json")
    peer_hint = getattr(args, "host", None) or "8.8.8.8"
    local_ip = str(ipaddress.IPv4Address(args.local_ip or detect_lan_ip(peer_hint)))
    try:
        if args.command == "begin":
            if state.exists():
                raise ValueError("A pairing already exists; use open, or choose another --state path")
            begin_pairing(str(ipaddress.IPv4Address(args.host)), local_ip, pending)
        elif args.command == "pair":
            complete_pairing(pending, state, getpass.getpass("PIN displayed on the Echo: "))
        else:
            open_url(state, local_ip, args.url)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        parser.exit(1, f"Instant Share failed: {error}\n")


if __name__ == "__main__":
    main()
