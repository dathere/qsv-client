"""Pairing a Data Schematic (a browser page) with the daemon.

The page asks to pair and shows a short code; the user approves it from a shell with
``qsv-clientd approve <code>`` (the daemon is headless and never prompts). The page then
gets a bearer token that is good for one dataset, for a limited time. Tokens are not tied
to the page's Origin: a ``file://`` page and a sandboxed iframe on any site both send
``Origin: null``, so the code the user approves is the only real proof.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

CODE_TTL = 300.0
MAX_PENDING = 10
MAX_REQUESTS_PER_MINUTE = 5
# no 0/O, 1/I/L, so a code survives being read aloud or retyped
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(6))
    return f"{raw[:3]}-{raw[3:]}"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class PendingPair:
    request_id: str
    code: str
    dataset_id: str
    origin: str
    created: float
    token: str | None = None
    """Set once approved; handed to the page on its next poll, then forgotten."""
    denied: bool = False


@dataclass
class Grant:
    dataset_id: str
    expires: float
    origin: str


@dataclass
class Pairing:
    token_ttl: float
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    pending: dict[str, PendingPair] = field(default_factory=dict)
    # keyed by sha256(token), so a memory dump or log line never holds a usable token
    grants: dict[str, Grant] = field(default_factory=dict)
    _recent: list[float] = field(default_factory=list)

    def _now(self) -> float:
        return self.clock()

    def _expire(self) -> None:
        now = self._now()
        for rid, p in list(self.pending.items()):
            if now - p.created > CODE_TTL:
                del self.pending[rid]
        for key, g in list(self.grants.items()):
            if g.expires <= now:
                del self.grants[key]
        self._recent = [t for t in self._recent if now - t < 60.0]

    def request(self, dataset_id: str, origin: str) -> PendingPair | None:
        """Start a pairing; ``None`` when rate-limited."""
        self._expire()
        if len(self.pending) >= MAX_PENDING or len(self._recent) >= MAX_REQUESTS_PER_MINUTE:
            return None
        now = self._now()
        self._recent.append(now)
        codes = {p.code for p in self.pending.values()}
        code = _code()
        while code in codes:
            code = _code()
        pending = PendingPair(secrets.token_urlsafe(16), code, dataset_id, origin, now)
        self.pending[pending.request_id] = pending
        return pending

    def find_code(self, code: str) -> PendingPair | None:
        self._expire()
        wanted = code.strip().upper().replace(" ", "")
        if len(wanted) == 6:
            wanted = f"{wanted[:3]}-{wanted[3:]}"
        return next((p for p in self.pending.values() if p.code == wanted), None)

    def approve(self, code: str) -> PendingPair | None:
        pending = self.find_code(code)
        if pending is None or pending.token is not None or pending.denied:
            return None
        token = secrets.token_urlsafe(32)
        pending.token = token
        self.grants[token_digest(token)] = Grant(
            pending.dataset_id, self._now() + self.token_ttl, pending.origin
        )
        return pending

    def deny(self, code: str) -> bool:
        pending = self.find_code(code)
        if pending is None:
            return False
        pending.denied = True
        return True

    def poll(self, request_id: str) -> tuple[str, str | None]:
        """``("pending" | "approved" | "denied" | "unknown", token)``; the token is given once."""
        self._expire()
        pending = self.pending.get(request_id)
        if pending is None:
            return "unknown", None
        if pending.denied:
            del self.pending[request_id]
            return "denied", None
        if pending.token is None:
            return "pending", None
        del self.pending[request_id]
        return "approved", pending.token

    def check(self, token: str) -> Grant | None:
        self._expire()
        return self.grants.get(token_digest(token))

    def live_digests(self) -> set[str]:
        """The digests of every unexpired token."""
        self._expire()
        return set(self.grants)

    def revoke_all(self, dataset_id: str | None = None) -> int:
        doomed = [
            k for k, g in self.grants.items() if dataset_id is None or g.dataset_id == dataset_id
        ]
        for key in doomed:
            del self.grants[key]
        return len(doomed)
