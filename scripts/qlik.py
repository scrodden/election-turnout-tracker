"""Minimal stdlib client for a public Qlik Cloud app (QIX engine over a
WebSocket). Used by ga_update.py for the Georgia SoS Election Data Hub.

The hub is a Qlik mashup whose page (sos.ga.gov) sits behind a Cloudflare
browser challenge; the Qlik tenant itself does not. The mashup obtains an
anonymous access token for every visitor from a public token endpoint, then
talks JSON-RPC to the engine at wss://<tenant>/app/<appId>. We do the same,
at most a couple of times a day.

    s = QlikSession(token_url, tenant_host, app_id)
    rows = s.cube(["County"], ["Sum(x)"])      # -> [[text, number, ...], ...]
    s.close()
"""
import base64
import json
import os
import socket
import ssl
import struct
import urllib.request

import common as C


class _WebSocket:
    def __init__(self, host, path, headers, timeout=60):
        raw = socket.create_connection((host, 443), timeout=timeout)
        self.s = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = ["GET %s HTTP/1.1" % path, "Host: %s" % host, "Upgrade: websocket",
                 "Connection: Upgrade", "Sec-WebSocket-Key: %s" % key,
                 "Sec-WebSocket-Version: 13", "User-Agent: %s" % C.USER_AGENT]
        lines += ["%s: %s" % kv for kv in headers.items()]
        self.s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.s.recv(4096)
            if not chunk:
                break
            resp += chunk
        head, _, self.buf = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("websocket handshake failed: %r" % head[:300])

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise RuntimeError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode, data):
        hdr = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        self.s.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, text):
        self._frame(0x1, text.encode())

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if b2 & 0x80:
                self._read(4)
            payload = self._read(n)
            if op == 0x8:
                raise RuntimeError("websocket closed by server: %r" % payload[:200])
            if op == 0x9:
                self._frame(0xA, payload)   # pong
                continue
            if op == 0xA:
                continue
            msg += payload
            if b1 & 0x80:
                return msg.decode("utf-8", "replace")

    def close(self):
        try:
            self._frame(0x8, b"")
            self.s.close()
        except OSError:
            pass


class QlikSession:
    def __init__(self, token_url, host, app_id):
        with urllib.request.urlopen(urllib.request.Request(token_url, headers={"User-Agent": C.USER_AGENT}),
                                    timeout=30, context=C._SSL_CTX) as r:
            token = json.loads(r.read())["access_token"]
        self.ws = _WebSocket(host, "/app/%s" % app_id, {"Authorization": "Bearer %s" % token})
        self.id = 0
        self.doc = self.call(-1, "OpenDoc", {"qDocName": app_id})["qReturn"]["qHandle"]

    def call(self, handle, method, params):
        self.id += 1
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": self.id, "handle": handle,
                                 "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.id:
                if "error" in m:
                    raise RuntimeError("%s: %s" % (method, m["error"]))
                return m["result"]

    def cube(self, dims, measures, max_rows=2000):
        """Evaluate a session hypercube. Returns rows of [dim text..., measure number...]
        (a measure's number is None when Qlik returns NaN/'-')."""
        width = len(dims) + len(measures)
        d = {"qInfo": {"qType": "turnout"}, "qHyperCubeDef": {
            "qDimensions": [{"qDef": {"qFieldDefs": [f]}} for f in dims],
            "qMeasures": [{"qDef": {"qDef": e}} for e in measures],
            "qSuppressZero": False,
            "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qWidth": width, "qHeight": max(1, min(max_rows, 10000 // width))}]}}
        h = self.call(self.doc, "CreateSessionObject", [d])["qReturn"]["qHandle"]
        hc = self.call(h, "GetLayout", {})["qLayout"]["qHyperCube"]
        out = []
        for r in (hc["qDataPages"][0]["qMatrix"] if hc["qDataPages"] else []):
            dim = [c.get("qText") for c in r[:len(dims)]]
            num = [c.get("qNum") if isinstance(c.get("qNum"), (int, float)) else None for c in r[len(dims):]]
            out.append(dim + num)
        return out

    def values(self, field):
        """All values of a field regardless of selections."""
        lo = {"qInfo": {"qType": "lo"}, "qListObjectDef": {"qDef": {"qFieldDefs": [field]}, "qShowAlternatives": True,
              "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qWidth": 1, "qHeight": 5000}]}}
        h = self.call(self.doc, "CreateSessionObject", [lo])["qReturn"]["qHandle"]
        lay = self.call(h, "GetLayout", {})["qLayout"]["qListObject"]
        return [r[0]["qText"] for r in (lay["qDataPages"][0]["qMatrix"] if lay["qDataPages"] else [])]

    def close(self):
        self.ws.close()
