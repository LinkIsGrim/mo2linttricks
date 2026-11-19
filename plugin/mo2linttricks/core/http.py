"""Minimal HTTPS transport on urllib, with CA-bundle fallbacks for Wine/Proton.

Under Wine, Python's ssl loads roots from Wine's crypt32 store, which normally
mirrors the host. If that fails we fall back to the Linux CA bundle through
Wine's Z: drive (also present inside Steam's pressure-vessel container).
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

_CA_FALLBACKS = [
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/Arch, Steam runtime
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora/Nobara
    "/etc/ssl/cert.pem",
]

USER_AGENT = "mo2linttricks"


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> object:
        return json.loads(self.body or b"null")


def _wine_path(p: str) -> str:
    return "Z:" + p.replace("/", "\\") if os.name == "nt" else p


def _contexts():
    yield ssl.create_default_context()
    for p in _CA_FALLBACKS:
        path = _wine_path(p)
        if os.path.exists(path):
            yield ssl.create_default_context(cafile=path)


class Transport:
    """Callable: (method, url, headers, body) -> Response. Replaceable in tests."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._ctx: ssl.SSLContext | None = None

    def __call__(self, method: str, url: str, headers: dict[str, str] | None = None, body: bytes | None = None) -> Response:
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        contexts = [self._ctx] if self._ctx else list(_contexts())
        last_err: Exception | None = None
        for ctx in contexts:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                    self._ctx = ctx
                    return Response(r.status, r.read(), dict(r.headers))
            except urllib.error.HTTPError as e:
                self._ctx = ctx
                return Response(e.code, e.read(), dict(e.headers or {}))
            except urllib.error.URLError as e:
                if isinstance(e.reason, ssl.SSLError):
                    last_err = e
                    continue
                raise
        raise last_err or RuntimeError("no usable TLS context")
