#!/usr/bin/env python3
"""
Hermes Tencent Relay

A minimal HTTP relay/proxy: every incoming request (any method, any path) is
forwarded verbatim to a single upstream URL.  Optional Cloudflare Access
service token headers are attached when configured, and the upstream response
(status + headers + body) is relayed back unchanged.

Stdlib only: http.server + urllib.request + os.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urllib_error
from urllib import request as urllib_request

# ---------------------------------------------------------------------------
# Configuration (env vars, with defaults)
# ---------------------------------------------------------------------------

UPSTREAM_URL = os.environ.get("TENCENT_RELAY_UPSTREAM_URL", "").strip()
CF_CLIENT_ID = os.environ.get("TENCENT_RELAY_CF_CLIENT_ID", "")
CF_CLIENT_SECRET = os.environ.get("TENCENT_RELAY_CF_CLIENT_SECRET", "")
LISTEN_HOST = os.environ.get("TENCENT_RELAY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("TENCENT_RELAY_LISTEN_PORT", "8420"))
UPSTREAM_TIMEOUT = 30  # seconds

# Hop-by-hop headers: they describe a single connection leg and must be
# rebuilt per hop, never copied verbatim. Host/Content-Length are set by
# urllib for the upstream leg.
_HOP_BY_HOP = frozenset({
    "host",
    "content-length",
    "connection",
    "proxy-connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
})

# send_response() already emits Date/Server locally; skip upstream copies so
# the client never sees duplicate headers.
_RESPONSE_ARTIFACTS = frozenset({"date", "server"})


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    """Never follow redirects — relay 3xx responses back to the client."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib_request.build_opener(_NoRedirect())


class RelayHandler(BaseHTTPRequestHandler):
    """Forward every request to UPSTREAM_URL and relay the response back."""

    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def log_message(self, fmt, *args):
        # Suppress the stdlib access log; _log() emits our own line.
        pass

    def _log(self, status):
        sys.stderr.write("→ [%s] %s → %d\n" % (self.command, self.path, status))
        sys.stderr.flush()

    def _read_body(self):
        """Read the request body (Content-Length or chunked transfer-encoding)."""
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            chunks = []
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    break
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    break
                if size == 0:
                    # Drain the trailing CRLF and any trailer headers.
                    while True:
                        tail = self.rfile.readline(65537)
                        if tail in (b"", b"\r\n", b"\n"):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # CRLF after the chunk data
            return b"".join(chunks)

        content_length = self.headers.get("Content-Length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                length = 0
            return self.rfile.read(length) if length > 0 else b""
        return None

    def _send_json(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self._log(status)

    # ------------------------------------------------------------------
    # the relay itself
    # ------------------------------------------------------------------

    def _relay(self):
        if not UPSTREAM_URL:
            self._send_json(
                503,
                {"error": "upstream not configured (set TENCENT_RELAY_UPSTREAM_URL)"},
            )
            return

        try:
            body = self._read_body()
        except OSError:
            body = None

        url = UPSTREAM_URL.rstrip("/") + self.path

        req = urllib_request.Request(url, data=body, method=self.command)
        for name, value in self.headers.items():
            if name.lower() not in _HOP_BY_HOP:
                req.add_header(name, value)
        if CF_CLIENT_ID:
            req.add_header("CF-Access-Client-Id", CF_CLIENT_ID)
        if CF_CLIENT_SECRET:
            req.add_header("CF-Access-Client-Secret", CF_CLIENT_SECRET)

        try:
            with _OPENER.open(req, timeout=UPSTREAM_TIMEOUT) as resp:
                status = resp.status
                payload = resp.read()
                resp_headers = resp.headers
        except urllib_error.HTTPError as exc:
            # Upstream answered with an error/redirect status: relay it as-is.
            status = exc.code
            payload = exc.read()
            resp_headers = exc.headers
        except Exception as exc:
            sys.stderr.write(
                "[relay] upstream error for %s %s: %r\n"
                % (self.command, self.path, exc)
            )
            self._send_json(502, {"error": "upstream unreachable"})
            return

        self.send_response(status)
        for name, value in resp_headers.items():
            lowered = name.lower()
            if lowered not in _HOP_BY_HOP and lowered not in _RESPONSE_ARTIFACTS:
                self.send_header(name, value)
        if status != 204:  # 204 must not carry a Content-Length
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD" and payload:
            self.wfile.write(payload)
        self._log(status)

    # Common methods; anything else is caught by __getattr__ below.
    do_GET = _relay
    do_POST = _relay
    do_PUT = _relay
    do_PATCH = _relay
    do_DELETE = _relay
    do_HEAD = _relay
    do_OPTIONS = _relay

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._relay
        raise AttributeError(name)


def main():
    if not UPSTREAM_URL:
        sys.stderr.write(
            "error: TENCENT_RELAY_UPSTREAM_URL is not set\n"
            "Run: hermes tencent-relay setup\n"
        )
        sys.stderr.flush()
        raise SystemExit(1)

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), RelayHandler)
    sys.stderr.write(
        "hermes-tencent-relay listening on http://%s:%d → %s\n"
        % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_URL)
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
