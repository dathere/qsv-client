"""Just enough HTTP/1.1 over asyncio streams for qsv-clientd (stdlib only).

One request per connection (``Connection: close``), ``Content-Length`` bodies only. Anything
else (chunked bodies, oversized heads, malformed lines) is refused and the connection closed.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

MAX_HEAD = 16 * 1024
_REASONS = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    411: "Length Required",
    413: "Content Too Large",
    421: "Misdirected Request",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


class HttpError(Exception):
    """A request the daemon refuses; becomes a JSON ``{"daemon_error": ...}`` response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes = b""

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError(400, "bad_json", f"request body is not JSON: {exc}") from None
        if not isinstance(data, dict):
            raise HttpError(400, "bad_json", "request body must be a JSON object")
        return data


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, data: Any, status: int = 200) -> Response:
        return cls(status, json.dumps(data, separators=(",", ":")).encode())

    @classmethod
    def error(cls, exc: HttpError) -> Response:
        return cls.json({"daemon_error": {"code": exc.code, "message": exc.message}}, exc.status)


async def read_head(reader: asyncio.StreamReader) -> Request:
    """Read the request line and headers (the body is read separately, once its limit is known)."""
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise HttpError(413, "head_too_large", "request head too large") from None
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            raise ConnectionResetError from None
        raise HttpError(400, "bad_request", "incomplete request head") from None
    if len(raw) > MAX_HEAD:
        raise HttpError(413, "head_too_large", "request head too large")
    try:
        text = raw.decode("latin-1")
        request_line, *header_lines = text[:-4].split("\r\n")
        method, target, version = request_line.split(" ")
    except ValueError:
        raise HttpError(400, "bad_request", "malformed request line") from None
    if version not in ("HTTP/1.1", "HTTP/1.0") or not method.isalpha():
        raise HttpError(400, "bad_request", "unsupported HTTP version or method")
    headers: dict[str, str] = {}
    for line in header_lines:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise HttpError(400, "bad_request", "malformed header")
        key = name.lower()
        if key in headers:
            raise HttpError(400, "bad_request", f"duplicate header: {name}")
        headers[key] = value.strip()
    if "transfer-encoding" in headers:
        raise HttpError(501, "unsupported", "Transfer-Encoding is not supported")
    url = urlsplit(target)
    if url.scheme or url.netloc or not url.path.startswith("/"):
        raise HttpError(400, "bad_request", "request target must be a path")
    return Request(method, unquote(url.path), dict(parse_qsl(url.query)), headers)


async def read_body(reader: asyncio.StreamReader, request: Request, limit: int) -> None:
    length_header = request.headers.get("content-length")
    if length_header is None:
        if request.method in ("POST", "PUT"):
            raise HttpError(411, "length_required", "Content-Length is required")
        return
    if not length_header.isdigit():
        raise HttpError(400, "bad_request", "invalid Content-Length")
    length = int(length_header)
    if length > limit:
        raise HttpError(413, "body_too_large", f"request body over {limit} bytes")
    try:
        request.body = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise HttpError(400, "bad_request", "request body shorter than Content-Length") from None


async def write_response(writer: asyncio.StreamWriter, response: Response) -> None:
    reason = _REASONS.get(response.status, "Unknown")
    lines = [f"HTTP/1.1 {response.status} {reason}"]
    headers = {
        "Content-Type": response.content_type,
        "Content-Length": str(len(response.body)),
        "Connection": "close",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        **response.headers,
    }
    lines += [f"{k}: {v}" for k, v in headers.items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + response.body)
    await writer.drain()
