"""Read-only WhisperLink discovery for an explicitly selected LAN device.

Uses the advertised amzn.dmgr service, never starts media or registers services.
Protocol references: ConnectSDK's WhisperPlay.jar and Sor3nt/Amazon-Fire-TV-Stream.
Only allowlisted identity/capability fields are printed; raw replies are not saved.
"""
import argparse
import json
import socket
import struct
import uuid


class Reader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, size):
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise EOFError("Incomplete Thrift response")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def number(self, fmt):
        return struct.unpack("!" + fmt, self.take(struct.calcsize("!" + fmt)))[0]

    def string(self):
        size = self.number("i")
        if not 0 <= size <= 262144:
            raise ValueError("Invalid string size")
        return self.take(size).decode("utf-8", errors="replace")

    def value(self, kind, depth=0):
        if depth > 24:
            raise ValueError("Nesting limit exceeded")
        formats = {2: "b", 3: "b", 4: "d", 6: "h", 8: "i", 10: "q"}
        if kind in formats:
            return self.number(formats[kind])
        if kind == 11:
            return self.string()
        if kind == 12:
            result = {}
            while True:
                field_type = self.number("b")
                if field_type == 0:
                    return result
                field_id = self.number("h")
                result[str(field_id)] = self.value(field_type, depth + 1)
        if kind in (13, 14, 15):
            item_type = self.number("b")
            value_type = self.number("b") if kind == 13 else None
            count = self.number("i")
            if not 0 <= count <= 4096:
                raise ValueError("Invalid collection size")
            if kind == 13:
                return [[self.value(item_type, depth + 1), self.value(value_type, depth + 1)] for _ in range(count)]
            return [self.value(item_type, depth + 1) for _ in range(count)]
        raise ValueError(f"Unsupported Thrift type {kind}")


def query(host, port, service_hash, method):
    if method not in ("getFullDeviceInfo", "getDeviceServices"):
        raise ValueError("Only discovery queries are supported")
    headers = {
        "x-amzn-dev-uuid": uuid.uuid4().hex.upper(),
        "x-amzn-dev-name": "alexa-photos-research",
        "x-amzn-dev-type": "0",
        "x-amzn-dev-version": "0",
        "x-amzn-svc-uuid": "amzn.dmgr",
        "x-amzn-svc-version": "2",
        "x-amzn-channel": "inet",
        "x-amzn-connection-id": "6917",
        "x-amzn-connection-version": "0",
        "x-amzn-connection-metadata": '{"unsecurePort":0,"securePort":0}',
        "x-amzn-svc-hash": service_hash,
    }
    request = "POST /whisperlink HTTP/1.0\r\n" + "".join(f"{k}:{v}\r\n" for k, v in headers.items()) + "\r\n"
    with socket.create_connection((host, port), timeout=4) as sock:
        sock.sendall(request.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Connection closed during handshake")
            buf += chunk
            if len(buf) > 16384:
                raise ValueError("Handshake too large")
        head, buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if status not in (b"HTTP/1.0 200 OK", b"HTTP/1.1 200 OK"):
            raise ConnectionError(status.decode(errors="replace"))
        encoded = method.encode()
        sock.sendall(struct.pack("!II", 0x80010001, len(encoded)) + encoded + struct.pack("!I", 1) + b"\0")
        while len(buf) <= 262144:
            try:
                reader = Reader(buf)
                version = reader.number("I")
                response_method = reader.string()
                sequence = reader.number("i")
                result = reader.value(12)
                if version != 0x80010002 or response_method != method or sequence != 1:
                    raise ValueError("Unexpected RPC reply or exception")
                return result
            except EOFError:
                chunk = sock.recv(16384)
                if not chunk:
                    raise ConnectionError("Connection closed before complete reply")
                buf += chunk
        raise ValueError("Response too large")


def summarize_device(device):
    info = device.get("4", {})
    capabilities = info.get("7", {}).get("2", [])
    allowed = {"dev.display.type", "dev.display.res.H", "dev.display.res.w", "dev.display.ppi", "dev.audio.support", "dev.video.support", "dev.amazon.device.type", "dev.cds.support", "dev.cds.image.protocols", "dev.cds.video.protocols", "dev.dial.server"}
    return {
        "name": device.get("1"),
        "device_type": device.get("3"),
        "manufacturer": info.get("3"),
        "model_reported": info.get("4"),
        "os_major_reported": info.get("5"),
        "os_minor_reported": info.get("6"),
        "capabilities": {k: v for k, v in capabilities if k in allowed},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("service_hash", help="Hash from this device's amzn.dmgr mDNS advertisement")
    parser.add_argument("--services", action="store_true")
    args = parser.parse_args()
    method = "getDeviceServices" if args.services else "getFullDeviceInfo"
    result = query(args.host, args.port, args.service_hash, method)
    if args.services:
        discovery = result.get("0", {})
        output = {
            "device": summarize_device(discovery.get("1", {})),
            "services": [{"sid": s.get("1"), "name": s.get("2"), "access_level": s.get("3"), "security": s.get("6"), "version": s.get("4")} for s in discovery.get("2", [])],
        }
    else:
        output = summarize_device(result.get("0", {}))
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
