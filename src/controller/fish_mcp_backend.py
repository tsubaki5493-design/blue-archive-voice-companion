"""
Official Fish Audio MCP Backend (`FishMcpBackend`)
--------------------------------------------------
Connects to Fish Audio's official Streamable HTTP MCP endpoint:
  https://api.fish.audio/mcp

Features:
- Standard OAuth 2.1 + PKCE (`S256`) + RFC 7591 Dynamic Client Registration (DCR)
  discovered via `/.well-known/oauth-protected-resource/mcp` and
  `/.well-known/oauth-authorization-server`.
- Honest client identity (`BlueArchiveVoiceCompanion`) with loopback-only
  ephemeral callback (`http://127.0.0.1:<port>/callback`).
- Pluggable DPAPI-compatible token storage (`load_tokens_cb` / `save_tokens_cb`)
  with automatic `refresh_token` renewal.
- Streamable HTTP MCP JSON-RPC 2.0 client supporting `Mcp-Session-Id` plus both
  `application/json` and `text/event-stream` (SSE) responses.
- Schema-adaptive tool invocation via `tools/list` (`get_credit_balance`,
  `text_to_speech`, `get_speech` polling for long generations).
- UTF-8 byte-safe text chunking (`split_text_by_utf8_bytes`) that never splits
  mid-character or mid-`[acting tag]`.
- SSRF-hardened HTTPS `audio_url` downloader (rejects loopback/private/link-local
  IPs on every hop, enforces <= 32 MB limit) + native MP3/WAV/OGG decoding to
  `(mono_float32, sample_rate)` using `soundfile` / `libsndfile`.
- Built-in CLI (`--check-discovery` and `--smoke-test`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx
import numpy as np
import soundfile as sf

# Allow running directly via `python src/controller/fish_mcp_backend.py --check-discovery`
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.controller.tts_cache import decode_audio_bytes_to_pcm

FISH_MCP_URL = "https://api.fish.audio/mcp"
FISH_PRM_WELL_KNOWN_URL = "https://api.fish.audio/.well-known/oauth-protected-resource/mcp"
FISH_ASM_WELL_KNOWN_URL = "https://api.fish.audio/.well-known/oauth-authorization-server"
FISH_OAUTH_CLIENT_NAME = "BlueArchiveVoiceCompanion"
FISH_MCP_CLIENT_VERSION = "1.0.0"
FISH_MCP_PROTOCOL_VERSION = "2025-03-26"

DEFAULT_MAX_TEXT_BYTES = 500
DEFAULT_SAFE_SPLIT_BYTES = 480
MAX_AUDIO_DOWNLOAD_BYTES = 32 * 1024 * 1024  # 32 MB
MAX_RPC_RESPONSE_BYTES = 4 * 1024 * 1024     # 4 MB
MAX_METADATA_BYTES = 256 * 1024              # 256 KB
MAX_AUDIO_REDIRECTS = 3
TOKEN_REFRESH_SKEW_SEC = 60.0
BALANCE_CACHE_TTL_SEC = 60.0

_ALLOWED_FISH_API_HOSTS = frozenset({"api.fish.audio"})
_ALLOWED_FISH_AUTH_HOSTS = frozenset({"api.fish.audio", "fish.audio", "www.fish.audio"})
_FORBIDDEN_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa", ".localdomain")

_BRACKET_TAG_RE = re.compile(r"\[[^\[\]\r\n]+\]")
_HTTPS_URL_RE = re.compile(r"https://[^\s\"'<>()\[\]{}]+")
_RESOURCE_META_RE = re.compile(r'resource_metadata="([^"]+)"', re.IGNORECASE)
_SENTENCE_END_CHARS = frozenset("。！？!?.…~〜\n")
_CLAUSE_BREAK_CHARS = frozenset("、,;；:：·—")
_CLOSING_QUOTE_CHARS = frozenset("」』）)]\"'”’")

_QUOTA_ERROR_HINTS = (
    "insufficient credit",
    "insufficient balance",
    "not enough credit",
    "out of credit",
    "quota exceeded",
    "credit limit",
    "rate limit",
    "too many requests",
    "payment required",
    "balance is too low",
    "credits exhausted",
    "upgrade your plan",
)
_AUTH_ERROR_HINTS = (
    "unauthorized",
    "invalid_token",
    "invalid_grant",
    "expired token",
    "token expired",
    "authentication required",
    "not authenticated",
    "access denied",
)


class FishMcpError(RuntimeError):
    """Base error raised by `FishMcpBackend`."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        rpc_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.rpc_code = rpc_code


class FishMcpAuthError(FishMcpError):
    """Raised when OAuth credentials are missing, expired, rejected, or cancelled."""


class FishMcpQuotaError(FishMcpError):
    """Raised when Fish Audio web credits/quota are exhausted or rate-limited."""


def _redact_text(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Scrub OAuth tokens and API keys from logs and exception messages."""
    s = str(text or "")
    if not s:
        return ""
    try:
        from src.utils.app_paths import redact_secrets

        s = redact_secrets(s, extra_secrets=extra_secrets)
    except Exception:
        for sec in sorted({str(x).strip() for x in extra_secrets if x and len(str(x).strip()) >= 4}, key=len, reverse=True):
            s = s.replace(sec, "[REDACTED]")
        s = re.sub(r"(?i)\b(Bearer\s+)([^\s'\"`,;}\]]{6,})", r"\1[REDACTED]", s)
    # Also redact OAuth token JSON keys & presigned download query credentials explicitly
    s = re.sub(
        r"""(?i)(['"]?(?:access_token|refresh_token|client_secret|code_verifier|id_token)['"]?\s*[:=]\s*['"]?)(?!\[REDACTED\b)([^'"\s,}\]]+)""",
        r"\1[REDACTED]",
        s,
    )
    s = re.sub(
        r"""(?i)([?&](?:token|access_token|sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token)=)(?!\[REDACTED\b)([^&\s'"<>}\]]+)""",
        r"\1[REDACTED]",
        s,
    )
    return s


def _safe_finite_float(value: Any, default: float = 0.0) -> float:
    """Safely convert `value` to a finite float, returning `default` on bad types/strings/NaN/Inf."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) else default


def _validate_https_host_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Enforce HTTPS-only URL pinned to `allowed_hosts` without userinfo, traversal, or control chars."""
    raw = str(url or "").strip()
    if not raw or "\\" in raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("Blocked malformed or control-character URL")
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    decoded_path = urllib.parse.unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or host not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or "\\" in decoded_path
        or ".." in decoded_path.split("/")
    ):
        raise ValueError(f"Blocked untrusted or non-HTTPS Fish Audio URL: {host or raw!r}")
    return raw


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if an IP address is loopback, private, link-local, reserved, multicast, or unspecified."""
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        if _is_forbidden_ip(ip_obj.ipv4_mapped):
            return True
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or not ip_obj.is_global
    )


def _validate_audio_download_url(url: str, *, resolve_dns: bool = True) -> str:
    """
    Validate an MCP-returned `audio_url` strictly against SSRF:
    - HTTPS scheme only, standard port 443, no userinfo/fragments/control chars
    - Rejects localhost, `.local`/`.internal`, private/loopback/link-local IP literals
    - Resolves DNS and rejects any host mapping to non-global IP addresses
    """
    raw = str(url or "").strip()
    if not raw or "\\" in raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("Blocked malformed audio download URL")
    parsed = urllib.parse.urlparse(raw)
    raw_host = (parsed.hostname or "").strip().lower()
    if not raw_host or raw_host.startswith(".") or ".." in raw_host:
        raise ValueError("Blocked non-HTTPS or malformed audio download URL")
    host = raw_host.rstrip(".")
    decoded_path = urllib.parse.unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or "\\" in decoded_path
        or ".." in decoded_path.split("/")
    ):
        raise ValueError("Blocked non-HTTPS or malformed audio download URL")

    if host == "localhost" or any(host.endswith(suf) for suf in _FORBIDDEN_HOST_SUFFIXES):
        raise ValueError(f"Blocked local/internal audio host: {host}")

    # Direct IP literal check (also blocks shorthand/decimal/octal/hex IPv4 literals via inet_aton)
    try:
        ip_lit = ipaddress.ip_address(host)
    except ValueError:
        ip_lit = None
    if ip_lit is None and ":" not in host:
        try:
            packed_v4 = socket.inet_aton(host)
            ip_lit = ipaddress.IPv4Address(packed_v4)
        except OSError:
            ip_lit = None
    if ip_lit is not None:
        if _is_forbidden_ip(ip_lit):
            raise ValueError(f"Blocked non-public audio IP address: {host}")
        return raw

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise FishMcpError(f"Failed to resolve audio download host {host!r}: {exc}") from exc
        if not infos:
            raise FishMcpError(f"Audio download host resolved to no addresses: {host!r}")
        for _family, _type, _proto, _canon, sockaddr in infos:
            ip_str = str(sockaddr[0]).split("%", 1)[0]
            try:
                resolved_ip = ipaddress.ip_address(ip_str)
            except ValueError as exc:
                raise ValueError(f"Blocked unparseable resolved IP for {host!r}") from exc
            if _is_forbidden_ip(resolved_ip):
                raise ValueError(f"Blocked SSRF audio download target resolving to private/loopback IP ({host})")

    return raw


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _tokenize_preserving_tags(text: str) -> list[tuple[str, bool]]:
    """
    Split text into atomic units `(token, is_tag)`:
    - Every `[acting tag]` is kept whole (`is_tag=True`) so we never split inside `[...]`.
    - Characters outside tags are emitted individually (`is_tag=False`) so byte budgets are exact.
    """
    units: list[tuple[str, bool]] = []
    pos = 0
    for m in _BRACKET_TAG_RE.finditer(text):
        start, end = m.span()
        if start > pos:
            for ch in text[pos:start]:
                units.append((ch, False))
        units.append((m.group(0), True))
        pos = end
    if pos < len(text):
        for ch in text[pos:]:
            units.append((ch, False))
    return units


def split_text_by_utf8_bytes(text: str, max_bytes: int = DEFAULT_SAFE_SPLIT_BYTES) -> list[str]:
    """
    Split `text` into chunks strictly satisfying `len(chunk.encode("utf-8")) <= max_bytes`.

    Guarantees:
    - Never splits mid-UTF-8 character (works on Unicode codepoints, accounting for exact UTF-8 byte sizes).
    - Never splits inside `[bracket]` acting tags (unless a single pathological tag alone exceeds `max_bytes`).
    - Avoids leaving a dangling `[tag]` alone at the tail of a chunk when followed by speech.
    - Prefers paragraph -> sentence punctuation -> clause punctuation -> whitespace -> clean char boundary.
    """
    budget = int(max_bytes)
    if budget < 4:
        raise ValueError(f"max_bytes must be >= 4 (to hold any UTF-8 codepoint), got {max_bytes}")

    raw = str(text or "").strip()
    if not raw:
        return []
    if _utf8_len(raw) <= budget:
        return [raw]

    units = _tokenize_preserving_tags(raw)
    chunks: list[str] = []
    idx = 0
    total = len(units)

    while idx < total:
        # Skip leading whitespace between chunks
        while idx < total and not units[idx][1] and units[idx][0].isspace():
            idx += 1
        if idx >= total:
            break

        # Find the maximum slice `units[idx:end]` that fits within `budget` UTF-8 bytes
        end = idx
        cur_bytes = 0
        while end < total:
            tok, _is_tag = units[end]
            tok_bytes = _utf8_len(tok)
            if cur_bytes + tok_bytes > budget:
                break
            cur_bytes += tok_bytes
            end += 1

        # Edge case: single atomic `[tag]` alone exceeds `budget` -> split safely by codepoint
        if end == idx:
            oversized_tok, _ = units[idx]
            piece_chars: list[str] = []
            piece_bytes = 0
            for ch in oversized_tok:
                cb = _utf8_len(ch)
                if piece_bytes + cb > budget:
                    if piece_chars:
                        chunks.append("".join(piece_chars).strip())
                    piece_chars = [ch]
                    piece_bytes = cb
                else:
                    piece_chars.append(ch)
                    piece_bytes += cb
            if piece_chars:
                rem = "".join(piece_chars).strip()
                if rem:
                    chunks.append(rem)
            idx += 1
            continue

        # If everything remaining fits, emit it cleanly and finish
        if end >= total:
            tail_str = "".join(t for t, _ in units[idx:total]).strip()
            if tail_str:
                chunks.append(tail_str)
            break

        # Choose the best natural split point strictly inside `(idx, end]`
        split_at: int | None = None
        min_natural = idx + max(1, (end - idx) // 4)

        # Priority 1: Newline boundary
        for pos in range(end, min_natural - 1, -1):
            prev_tok, prev_is_tag = units[pos - 1]
            if not prev_is_tag and prev_tok == "\n":
                split_at = pos
                break

        # Priority 2: Sentence-ending punctuation (including trailing closing quotes)
        if split_at is None:
            for pos in range(end, min_natural - 1, -1):
                prev_tok, prev_is_tag = units[pos - 1]
                if prev_is_tag:
                    continue
                if prev_tok in _SENTENCE_END_CHARS:
                    split_at = pos
                    break
                if prev_tok in _CLOSING_QUOTE_CHARS and pos - 2 >= idx:
                    before_tok, before_is_tag = units[pos - 2]
                    if not before_is_tag and before_tok in _SENTENCE_END_CHARS:
                        split_at = pos
                        break

        # Priority 3: Clause punctuation
        if split_at is None:
            for pos in range(end, min_natural - 1, -1):
                prev_tok, prev_is_tag = units[pos - 1]
                if not prev_is_tag and prev_tok in _CLAUSE_BREAK_CHARS:
                    split_at = pos
                    break

        # Priority 4: Whitespace
        if split_at is None:
            for pos in range(end, min_natural - 1, -1):
                prev_tok, prev_is_tag = units[pos - 1]
                if not prev_is_tag and prev_tok.isspace():
                    split_at = pos
                    break

        # Priority 5: Exact unit boundary at `end` (already respects UTF-8 & `[tag]` atomicity)
        if split_at is None:
            split_at = end

        # Avoid stranding trailing `[tag]` token(s) at the end of this chunk when speech precedes them
        trim_pos = split_at
        while trim_pos > idx and not units[trim_pos - 1][1] and units[trim_pos - 1][0].isspace():
            trim_pos -= 1
        back_pos = trim_pos
        while back_pos > idx and units[back_pos - 1][1]:
            back_pos -= 1
        # Only push trailing tags to next chunk if meaningful non-whitespace content remains before them
        if back_pos < trim_pos and any(not u[1] and not u[0].isspace() for u in units[idx:back_pos]):
            split_at = back_pos

        chunk_str = "".join(t for t, _ in units[idx:split_at]).strip()
        if chunk_str:
            chunks.append(chunk_str)
        idx = split_at

    return [c for c in chunks if c]


def _pkce_pair() -> tuple[str, str]:
    """Generate RFC 7636 PKCE `(code_verifier, code_challenge)` using `S256`."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _render_oauth_callback_html(*, success: bool, detail: str = "") -> bytes:
    """Render a clean Korean/English HTML confirmation page for the loopback OAuth callback."""
    if success:
        title = "Fish Audio 연결 완료 / Connected"
        heading = "Fish Audio 웹 크레딧 연결 완료!"
        body_ko = "공식 Fish Audio MCP 로그인이 완료되었습니다. 이 브라우저 창을 닫고 앱으로 돌아가 주세요."
        body_en = "Official Fish Audio MCP sign-in succeeded. You may close this tab and return to the app."
        badge_bg = "#16a34a"
    else:
        title = "Fish Audio 연결 실패 / Sign-in Failed"
        heading = "Fish Audio 로그인에 실패했습니다"
        safe_detail = html.escape(detail or "Unknown OAuth error")
        body_ko = f"인증 처리 중 문제가 발생했습니다 ({safe_detail}). 이 창을 닫고 앱에서 다시 시도해 주세요."
        body_en = f"Authentication did not complete ({safe_detail}). Please close this tab and retry from the app."
        badge_bg = "#dc2626"

    page = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Malgun Gothic", sans-serif;
      background: #0f172a;
      color: #f8fafc;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 16px;
      padding: 28px 32px;
      max-width: 480px;
      width: 100%;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.45);
    }}
    .badge {{
      display: inline-block;
      background: {badge_bg};
      color: #ffffff;
      font-weight: 700;
      font-size: 12px;
      padding: 4px 10px;
      border-radius: 999px;
      margin-bottom: 12px;
    }}
    h1 {{ font-size: 20px; margin: 0 0 12px 0; }}
    p {{ font-size: 14px; line-height: 1.6; color: #cbd5e1; margin: 0 0 10px 0; }}
    .en {{ font-size: 13px; color: #94a3b8; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">BlueArchiveVoiceCompanion · Fish Audio MCP</div>
    <h1>{heading}</h1>
    <p>{body_ko}</p>
    <p class="en">{body_en}</p>
  </div>
</body>
</html>
"""
    return page.encode("utf-8")


class _ExclusiveLoopbackHTTPServer(ThreadingHTTPServer):
    """
    Loopback-only threaded OAuth callback server that:
    - refuses address reuse on Windows (`allow_reuse_address = False`),
    - dispatches each TCP connection on a daemon worker thread so browser
      speculative preconnect / keep-alive sockets can NEVER block `/callback`
      or deadlock `server.shutdown()`, and
    - closes immediately without blocking on idle client threads (`block_on_close = False`).
    """

    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16


def _stop_oauth_loopback_server(
    server: _ExclusiveLoopbackHTTPServer | None,
    thread: threading.Thread | None = None,
) -> None:
    """Idempotently shut down and close an OAuth loopback server without blocking."""
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        try:
            thread.join(timeout=0.5)
        except Exception:
            pass


def _parse_loopback_callback_port(redirect_uri: str) -> int | None:
    """Extract port from a strictly validated `http://127.0.0.1:<port>/callback` URI."""
    raw = str(redirect_uri or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
        if (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.path.rstrip("/") == "/callback"
            and parsed.port is not None
            and 1024 <= int(parsed.port) <= 65535
        ):
            return int(parsed.port)
    except Exception:
        return None
    return None


def _start_oauth_loopback_server(
    expected_state: str,
    expected_issuer: str | None = None,
    preferred_port: int | None = None,
) -> tuple[_ExclusiveLoopbackHTTPServer, int, threading.Event, dict[str, Any], threading.Thread]:
    """
    Bind an OAuth callback server strictly on `127.0.0.1` (reusing `preferred_port`
    if provided and available, otherwise falling back to an ephemeral port `0`).
    Returns `(server, port, done_event, result_dict, server_thread)`.
    """
    done_event = threading.Event()
    result_lock = threading.Lock()
    result: dict[str, Any] = {}

    class _CallbackHandler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            try:
                # Prevent idle Chromium speculative preconnect sockets from lingering forever
                self.connection.settimeout(3.0)
            except Exception:
                pass

        def handle(self) -> None:
            try:
                super().handle()
            except (TimeoutError, socket.timeout, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                return

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Never log callback query parameters (contain OAuth authorization `code` & `state`)
            return

        def _send_html(self, status: int, body: bytes) -> None:
            self.close_connection = True
            try:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout, OSError):
                pass

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            norm_path = parsed.path.rstrip("/") or "/"
            if norm_path == "/favicon.ico":
                self.close_connection = True
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if norm_path != "/callback":
                self.close_connection = True
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return

            with result_lock:
                if done_event.is_set():
                    already_ok = "code" in result and "error" not in result
                    prev_err = str(result.get("error") or "")
                    self._send_html(
                        200 if already_ok else 400,
                        _render_oauth_callback_html(success=already_ok, detail=prev_err),
                    )
                    return

                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                err = (params.get("error") or [""])[0].strip()
                err_desc = (params.get("error_description") or [""])[0].strip()
                code = (params.get("code") or [""])[0].strip()
                got_state = (params.get("state") or [""])[0].strip()
                got_iss = (params.get("iss") or [""])[0].strip()

                if err:
                    msg = f"{err}: {err_desc}" if err_desc else err
                    result["error"] = msg
                    result["oauth_error_code"] = err.lower()
                    try:
                        self._send_html(400, _render_oauth_callback_html(success=False, detail=msg))
                    finally:
                        done_event.set()
                    return

                if not got_state or not secrets.compare_digest(got_state, expected_state):
                    msg = "Invalid OAuth state parameter"
                    result["error"] = msg
                    try:
                        self._send_html(400, _render_oauth_callback_html(success=False, detail=msg))
                    finally:
                        done_event.set()
                    return

                if expected_issuer and got_iss:
                    if got_iss.rstrip("/") != expected_issuer.rstrip("/"):
                        msg = "OAuth issuer mismatch"
                        result["error"] = msg
                        try:
                            self._send_html(400, _render_oauth_callback_html(success=False, detail=msg))
                        finally:
                            done_event.set()
                        return

                if not code:
                    msg = "Missing authorization code"
                    result["error"] = msg
                    try:
                        self._send_html(400, _render_oauth_callback_html(success=False, detail=msg))
                    finally:
                        done_event.set()
                    return

                result["code"] = code
                try:
                    self._send_html(200, _render_oauth_callback_html(success=True))
                finally:
                    done_event.set()

    server: _ExclusiveLoopbackHTTPServer | None = None
    if preferred_port is not None and 1024 <= int(preferred_port) <= 65535:
        try:
            server = _ExclusiveLoopbackHTTPServer(("127.0.0.1", int(preferred_port)), _CallbackHandler)
        except OSError:
            server = None
    if server is None:
        server = _ExclusiveLoopbackHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
        name=f"fish-mcp-oauth-cb-{port}",
    )
    thread.start()
    return server, port, done_event, result, thread


def _get_header_ci(headers: Any, name: str) -> str:
    """Case-insensitive HTTP header lookup supporting both `httpx.Headers` and plain `dict`."""
    if not headers:
        return ""
    val = headers.get(name)
    if val is not None:
        return str(val).strip()
    target = name.lower()
    val_low = headers.get(target)
    if val_low is not None:
        return str(val_low).strip()
    try:
        for k, v in headers.items():
            if str(k).lower() == target and v is not None:
                return str(v).strip()
    except Exception:
        pass
    return ""


def _parse_sse_json_messages(raw_text: str) -> list[dict[str, Any]]:
    """Parse `text/event-stream` payload into a list of decoded JSON-RPC dict objects."""
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def _flush() -> None:
        if not data_lines:
            return
        payload = "\n".join(data_lines).strip()
        data_lines.clear()
        if not payload or payload == "[DONE]":
            return
        try:
            obj = json.loads(payload)
            if isinstance(obj, dict):
                messages.append(obj)
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        messages.append(item)
        except json.JSONDecodeError:
            return

    for line in raw_text.splitlines():
        if line == "":
            _flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            val = line[5:]
            if val.startswith(" "):
                val = val[1:]
            data_lines.append(val)
    _flush()
    return messages


def _extract_first_int(mapping: Any, candidate_keys: tuple[str, ...]) -> int | None:
    """Recursively search dict(s) for the first parseable integer under any key in `candidate_keys`."""
    if not isinstance(mapping, dict):
        return None
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in candidate_keys:
        if key.lower() in lowered:
            val = lowered[key.lower()]
            if isinstance(val, bool):
                continue
            if isinstance(val, int) or (isinstance(val, float) and math.isfinite(val)):
                return int(val)
            if isinstance(val, str):
                cleaned = val.replace(",", "").strip()
                m = re.search(r"-?\d+", cleaned)
                if m:
                    return int(m.group(0))
    for sub in mapping.values():
        if isinstance(sub, dict):
            found = _extract_first_int(sub, candidate_keys)
            if found is not None:
                return found
    return None


def _extract_first_str(mapping: Any, candidate_keys: tuple[str, ...]) -> str | None:
    """Recursively search dict(s) for the first non-empty string under any key in `candidate_keys`."""
    if not isinstance(mapping, dict):
        return None
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in candidate_keys:
        if key.lower() in lowered:
            val = lowered[key.lower()]
            if isinstance(val, str) and val.strip():
                return val.strip()
    for sub in mapping.values():
        if isinstance(sub, dict):
            found = _extract_first_str(sub, candidate_keys)
            if found is not None:
                return found
    return None


def _classify_error_message(msg: str, *, status_code: int | None = None, rpc_code: int | None = None) -> FishMcpError:
    """Map HTTP/JSON-RPC/tool errors into typed `FishMcpAuthError`, `FishMcpQuotaError`, or `FishMcpError`."""
    low = msg.lower()
    if status_code in (401, 403) or any(h in low for h in _AUTH_ERROR_HINTS):
        return FishMcpAuthError(msg, status_code=status_code, rpc_code=rpc_code)
    if status_code in (402, 429) or any(h in low for h in _QUOTA_ERROR_HINTS):
        return FishMcpQuotaError(msg, status_code=status_code, rpc_code=rpc_code)
    return FishMcpError(msg, status_code=status_code, rpc_code=rpc_code)


def _resample_mono_f32(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Resample 1D float32 PCM from `src_sr` to `dst_sr` when concatenating mismatched chunks."""
    if src_sr == dst_sr or audio.size == 0:
        return np.ascontiguousarray(audio, dtype=np.float32)
    try:
        from scipy.signal import resample_poly

        g = math.gcd(src_sr, dst_sr)
        out = resample_poly(audio, dst_sr // g, src_sr // g).astype(np.float32, copy=False)
        return np.ascontiguousarray(out, dtype=np.float32)
    except Exception:
        new_len = max(1, int(round(audio.size * (dst_sr / float(src_sr)))))
        x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32, copy=False)


def _encode_pcm_for_cache(mono_f32: np.ndarray, sr: int) -> bytes:
    """Encode concatenated multi-chunk PCM into MP3 (or WAV fallback) for raw-byte caching."""
    clipped = np.clip(np.asarray(mono_f32, dtype=np.float32), -1.0, 1.0)
    buf = io.BytesIO()
    try:
        sf.write(buf, clipped, int(sr), format="MP3")
        return buf.getvalue()
    except Exception:
        buf = io.BytesIO()
        sf.write(buf, clipped, int(sr), format="WAV", subtype="PCM_16")
        return buf.getvalue()


class FishMcpBackend:
    """
    Official Fish Audio Streamable HTTP MCP client with OAuth 2.1 + PKCE + RFC 7591 DCR.

    High-level methods for `aru_fish_bridge.py` / `settings_dialog.py`:
      - `is_authenticated() -> bool`
      - `login_interactive(timeout_sec=180.0, status_cb=None, cancel_check=None) -> dict`
      - `cancel_login() -> None`
      - `logout() -> None`
      - `get_credit_balance(force_refresh=False) -> dict`
      - `get_cached_balance() -> dict | None`
      - `list_tools(force_refresh=False) -> list[dict]`
      - `synthesize_to_pcm(text, voice_id=None, language=None, cancel_check=None) -> tuple[np.ndarray, int, bytes]`
      - `discover_oauth_metadata(force_refresh=False) -> dict`
      - `close() -> None`
    """

    def __init__(
        self,
        *,
        load_tokens_cb: Callable[[], dict[str, Any] | str | None] | None = None,
        save_tokens_cb: Callable[[dict[str, Any]], None] | None = None,
        mcp_url: str = FISH_MCP_URL,
        client_name: str = FISH_OAUTH_CLIENT_NAME,
        http_client: httpx.Client | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        self.mcp_url = _validate_https_host_url(mcp_url, _ALLOWED_FISH_API_HOSTS)
        self.client_name = str(client_name or FISH_OAUTH_CLIENT_NAME).strip() or FISH_OAUTH_CLIENT_NAME
        self._load_tokens_cb = load_tokens_cb
        self._save_tokens_cb = save_tokens_cb
        self._log_cb = log_cb

        self._owns_client = http_client is None
        self._client = (
            http_client
            if http_client is not None
            else httpx.Client(
                verify=True,
                follow_redirects=False,
                timeout=httpx.Timeout(connect=12.0, read=65.0, write=20.0, pool=12.0),
                headers={"User-Agent": f"{self.client_name}/{FISH_MCP_CLIENT_VERSION}"},
            )
        )

        self._token_lock = threading.RLock()
        self._session_lock = threading.RLock()
        self._login_lock = threading.Lock()
        self._login_cancel_event = threading.Event()
        self._active_login_cancel: threading.Event | None = None
        self._active_login_server: _ExclusiveLoopbackHTTPServer | None = None
        self._active_login_server_thread: threading.Thread | None = None
        self._login_in_progress: bool = False
        self._rpc_id = 0

        self._tokens: dict[str, Any] = {}
        self._oauth_meta: dict[str, Any] | None = None
        self._session_id: str | None = None
        self._protocol_version: str = FISH_MCP_PROTOCOL_VERSION
        self._initialized: bool = False
        self._tools_by_name: dict[str, dict[str, Any]] = {}
        self._cached_balance: dict[str, Any] | None = None
        self._cached_balance_at: float = 0.0
        self._max_text_bytes: int | None = None

        self._load_persisted_tokens()

    def _log(self, msg: str) -> None:
        if self._log_cb is None:
            return
        try:
            self._log_cb(_redact_text(msg, extra_secrets=self._active_secrets()))
        except Exception:
            pass

    def _active_secrets(self) -> tuple[str, ...]:
        secs: list[str] = []
        with self._token_lock:
            for k in ("access_token", "refresh_token", "client_secret"):
                v = str(self._tokens.get(k) or "").strip()
                if v:
                    secs.append(v)
        return tuple(secs)

    def _load_persisted_tokens(self) -> None:
        if self._load_tokens_cb is None:
            return
        try:
            loaded = self._load_tokens_cb()
            if isinstance(loaded, str) and loaded.strip():
                loaded = json.loads(loaded)
            if isinstance(loaded, dict):
                with self._token_lock:
                    self._tokens = dict(loaded)
        except Exception:
            with self._token_lock:
                self._tokens = {}

    def _persist_tokens_unlocked(self) -> None:
        if self._save_tokens_cb is None:
            return
        payload = dict(self._tokens)
        self._save_tokens_cb(payload)

    def close(self) -> None:
        """Close the internally managed `httpx.Client` and reset local MCP session state."""
        self.cancel_login()
        with self._session_lock:
            self._session_id = None
            self._initialized = False
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass

    def is_login_in_progress(self) -> bool:
        """Return True if an interactive OAuth login flow is currently active."""
        with self._login_lock:
            return bool(self._login_in_progress)

    def cancel_login(self) -> None:
        """Signal any in-progress interactive OAuth browser login to abort cleanly and close its loopback listener."""
        self._login_cancel_event.set()
        srv: _ExclusiveLoopbackHTTPServer | None = None
        srv_thread: threading.Thread | None = None
        with self._login_lock:
            if self._active_login_cancel is not None:
                self._active_login_cancel.set()
            srv = self._active_login_server
            srv_thread = self._active_login_server_thread
            self._active_login_server = None
            self._active_login_server_thread = None
        if srv is not None:
            _stop_oauth_loopback_server(srv, srv_thread)

    def verify_or_refresh_session(self, *, force_refresh_token: bool = False) -> dict[str, Any]:
        """
        Verify an existing OAuth session (refreshing the access token via `refresh_token`
        when expiring or when `force_refresh_token=True`) and immediately sync credit balance
        without opening a browser. Raises `FishMcpAuthError` if interactive re-login is needed.
        """
        self._get_valid_access_token(force_refresh=force_refresh_token)
        return self.get_credit_balance(force_refresh=True)

    def is_authenticated(self) -> bool:
        """
        Return True if valid OAuth credentials are present (an unexpired `access_token`
        or a `refresh_token` paired with `client_id` + `token_endpoint`).
        """
        with self._token_lock:
            access = str(self._tokens.get("access_token") or "").strip()
            refresh = str(self._tokens.get("refresh_token") or "").strip()
            client_id = str(self._tokens.get("client_id") or "").strip()
            token_ep = str(self._tokens.get("token_endpoint") or "").strip()
            expires_at = _safe_finite_float(self._tokens.get("expires_at"), 0.0)

            if refresh and client_id and token_ep:
                return True
            if access:
                if expires_at <= 0.0:
                    return True
                return time.time() < (expires_at - 5.0)
            return False

    def logout(self) -> None:
        """Clear in-memory & persisted OAuth tokens and reset MCP session state."""
        self.cancel_login()
        with self._token_lock:
            self._tokens = {}
            try:
                self._persist_tokens_unlocked()
            except Exception:
                pass
        with self._session_lock:
            self._session_id = None
            self._initialized = False
            self._tools_by_name = {}
            self._cached_balance = None
            self._cached_balance_at = 0.0
            self._max_text_bytes = None

    def get_cached_balance(self) -> dict[str, Any] | None:
        """Return the last fetched credit balance dict without performing network I/O."""
        with self._session_lock:
            return dict(self._cached_balance) if self._cached_balance is not None else None

    # ------------------------------------------------------------------
    # OAuth 2.1 Discovery, RFC 7591 DCR, PKCE Login & Token Refresh
    # ------------------------------------------------------------------

    def _read_bounded_json(self, resp: httpx.Response, max_bytes: int, context: str) -> Any:
        raw = resp.content if getattr(resp, "content", None) is not None else resp.read()
        if len(raw) > max_bytes:
            raise FishMcpError(f"{context} response exceeded limit ({max_bytes} bytes)")
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            snippet = _redact_text(raw[:240].decode("utf-8", errors="replace"), self._active_secrets())
            raise FishMcpError(f"{context} returned invalid JSON: {snippet}") from exc

    def discover_oauth_metadata(self, force_refresh: bool = False) -> dict[str, Any]:
        """
        Discover Fish Audio's OAuth 2.1 Protected Resource + Authorization Server metadata.
        Non-mutating GET-only discovery against `https://api.fish.audio/.well-known/*`.
        """
        if not force_refresh:
            with self._session_lock:
                if self._oauth_meta is not None:
                    return dict(self._oauth_meta)

        prm_url = _validate_https_host_url(FISH_PRM_WELL_KNOWN_URL, _ALLOWED_FISH_API_HOSTS)
        prm_data: dict[str, Any] | None = None

        try:
            r_prm = self._client.get(prm_url, headers={"Accept": "application/json"})
            if r_prm.status_code == 200:
                parsed = self._read_bounded_json(r_prm, MAX_METADATA_BYTES, "OAuth protected-resource metadata")
                if isinstance(parsed, dict):
                    prm_data = parsed
        except FishMcpError:
            raise
        except Exception as exc:
            raise FishMcpError(f"Failed to fetch OAuth protected-resource metadata: {_redact_text(str(exc))}") from exc

        # RFC 9728 fallback: inspect WWW-Authenticate challenge from unauthenticated MCP probe
        if prm_data is None:
            try:
                probe = self._client.post(
                    self.mcp_url,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
                )
                www_auth = _get_header_ci(probe.headers, "www-authenticate")
                m_meta = _RESOURCE_META_RE.search(www_auth)
                if m_meta:
                    alt_prm_url = _validate_https_host_url(m_meta.group(1), _ALLOWED_FISH_API_HOSTS)
                    r_alt = self._client.get(alt_prm_url, headers={"Accept": "application/json"})
                    if r_alt.status_code == 200:
                        parsed = self._read_bounded_json(r_alt, MAX_METADATA_BYTES, "OAuth protected-resource metadata")
                        if isinstance(parsed, dict):
                            prm_data = parsed
            except (FishMcpError, ValueError):
                raise
            except Exception as exc:
                raise FishMcpError(
                    f"Failed to probe OAuth protected-resource metadata: {_redact_text(str(exc), self._active_secrets())}"
                ) from exc

        if not isinstance(prm_data, dict):
            raise FishMcpError("Fish Audio OAuth protected-resource discovery failed")

        resource = str(prm_data.get("resource") or "").strip()
        if resource and resource.rstrip("/") != self.mcp_url.rstrip("/"):
            raise FishMcpError(f"OAuth resource mismatch: expected {self.mcp_url}, got {resource}")

        auth_servers = prm_data.get("authorization_servers")
        if isinstance(auth_servers, list) and auth_servers:
            auth_server_base = _validate_https_host_url(str(auth_servers[0]), _ALLOWED_FISH_API_HOSTS).rstrip("/")
            asm_url = _validate_https_host_url(
                f"{auth_server_base}/.well-known/oauth-authorization-server",
                _ALLOWED_FISH_API_HOSTS,
            )
        else:
            asm_url = _validate_https_host_url(FISH_ASM_WELL_KNOWN_URL, _ALLOWED_FISH_API_HOSTS)

        try:
            r_asm = self._client.get(asm_url, headers={"Accept": "application/json"})
        except Exception as exc:
            raise FishMcpError(f"Failed to fetch OAuth authorization-server metadata: {_redact_text(str(exc))}") from exc

        if r_asm.status_code != 200:
            raise FishMcpError(
                f"OAuth authorization-server discovery failed (HTTP {r_asm.status_code})",
                status_code=r_asm.status_code,
            )

        asm_data = self._read_bounded_json(r_asm, MAX_METADATA_BYTES, "OAuth authorization-server metadata")
        if not isinstance(asm_data, dict):
            raise FishMcpError("Invalid OAuth authorization-server metadata object")

        auth_endpoint = _validate_https_host_url(
            str(asm_data.get("authorization_endpoint") or ""),
            _ALLOWED_FISH_AUTH_HOSTS,
        )
        token_endpoint = _validate_https_host_url(
            str(asm_data.get("token_endpoint") or ""),
            _ALLOWED_FISH_API_HOSTS,
        )
        reg_raw = str(asm_data.get("registration_endpoint") or "").strip()
        registration_endpoint = (
            _validate_https_host_url(reg_raw, _ALLOWED_FISH_API_HOSTS) if reg_raw else ""
        )
        issuer = _validate_https_host_url(
            str(asm_data.get("issuer") or "https://api.fish.audio"),
            _ALLOWED_FISH_API_HOSTS,
        )

        pkce_raw = asm_data.get("code_challenge_methods_supported")
        pkce_methods = [str(m) for m in pkce_raw] if isinstance(pkce_raw, list) else []
        if "S256" not in pkce_methods:
            raise FishMcpError("Fish Audio OAuth server does not advertise PKCE S256 support")

        prm_scopes = prm_data.get("scopes_supported")
        asm_scopes = asm_data.get("scopes_supported")
        if isinstance(prm_scopes, list) and prm_scopes:
            scopes_raw: list[Any] = prm_scopes
        elif isinstance(asm_scopes, list) and asm_scopes:
            scopes_raw = asm_scopes
        else:
            scopes_raw = ["mcp"]
        scopes = [str(s).strip() for s in scopes_raw if str(s).strip()] or ["mcp"]

        grant_types_raw = asm_data.get("grant_types_supported")
        auth_methods_raw = asm_data.get("token_endpoint_auth_methods_supported")

        meta = {
            "resource": resource or self.mcp_url,
            "resource_name": str(prm_data.get("resource_name") or "Fish Audio"),
            "issuer": issuer,
            "authorization_endpoint": auth_endpoint,
            "token_endpoint": token_endpoint,
            "registration_endpoint": registration_endpoint,
            "scopes_supported": scopes,
            "code_challenge_methods_supported": pkce_methods,
            "grant_types_supported": list(grant_types_raw) if isinstance(grant_types_raw, list) else [],
            "token_endpoint_auth_methods_supported": (
                list(auth_methods_raw) if isinstance(auth_methods_raw, list) else []
            ),
            "authorization_response_iss_parameter_supported": bool(
                asm_data.get("authorization_response_iss_parameter_supported", False)
            ),
        }
        with self._session_lock:
            self._oauth_meta = meta
        return dict(meta)

    def _register_dynamic_client(self, registration_endpoint: str, redirect_uri: str, scope: str) -> dict[str, Any]:
        """Perform RFC 7591 Dynamic Client Registration as an honest public PKCE native client."""
        reg_url = _validate_https_host_url(registration_endpoint, _ALLOWED_FISH_API_HOSTS)
        payload = {
            "client_name": self.client_name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": scope,
        }
        try:
            resp = self._client.post(
                reg_url,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json=payload,
            )
        except Exception as exc:
            raise FishMcpAuthError(f"Dynamic client registration request failed: {_redact_text(str(exc))}") from exc

        if resp.status_code not in (200, 201):
            err_txt = _redact_text(resp.text[:240], self._active_secrets())
            raise FishMcpAuthError(
                f"Dynamic client registration rejected (HTTP {resp.status_code}): {err_txt}",
                status_code=resp.status_code,
            )

        data = self._read_bounded_json(resp, MAX_METADATA_BYTES, "OAuth DCR")
        if not isinstance(data, dict) or not str(data.get("client_id") or "").strip():
            raise FishMcpAuthError("OAuth dynamic client registration did not return a client_id")
        return data

    def _clear_cached_dcr_metadata(self) -> None:
        """Drop cached DCR redirect URI / port so the next login registers a fresh client."""
        with self._token_lock:
            changed = False
            for k in ("registered_redirect_uri", "registered_port"):
                if k in self._tokens:
                    self._tokens.pop(k, None)
                    changed = True
            if changed and not str(self._tokens.get("access_token") or "").strip() and not str(self._tokens.get("refresh_token") or "").strip():
                self._tokens.pop("client_id", None)
                self._tokens.pop("client_secret", None)
            if changed:
                try:
                    self._persist_tokens_unlocked()
                except Exception:
                    pass

    def login_interactive(
        self,
        timeout_sec: float = 180.0,
        status_cb: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        force_new_client: bool = False,
    ) -> dict[str, Any]:
        """
        Run standard OAuth 2.1 + PKCE (`S256`) + RFC 7591 DCR browser sign-in flow.
        Binds strictly to `http://127.0.0.1:<port>/callback` (reusing the previously
        registered loopback port + `client_id` when available, or cleanly re-registering
        via DCR if the port changed or no cached client exists), opens the user's
        default browser, exchanges the authorization code for tokens, and persists them.
        """
        # Cleanly abort any lingering prior login attempt before starting a new one
        prev_srv: _ExclusiveLoopbackHTTPServer | None = None
        prev_srv_thread: threading.Thread | None = None
        with self._login_lock:
            if self._active_login_cancel is not None:
                self._active_login_cancel.set()
            prev_srv = self._active_login_server
            prev_srv_thread = self._active_login_server_thread
            self._active_login_server = None
            self._active_login_server_thread = None
        if prev_srv is not None:
            _stop_oauth_loopback_server(prev_srv, prev_srv_thread)

        attempt_cancel = threading.Event()
        with self._login_lock:
            self._login_cancel_event.clear()
            self._active_login_cancel = attempt_cancel
            self._login_in_progress = True

        def _notify(msg: str) -> None:
            if status_cb is not None:
                try:
                    status_cb(msg)
                except Exception:
                    pass

        def _is_cancelled() -> bool:
            if attempt_cancel.is_set() or self._login_cancel_event.is_set():
                return True
            if cancel_check is not None:
                try:
                    return bool(cancel_check())
                except Exception:
                    return False
            return False

        cur_server: _ExclusiveLoopbackHTTPServer | None = None
        cur_srv_thread: threading.Thread | None = None

        def _cleanup_attempt_server() -> None:
            nonlocal cur_server, cur_srv_thread
            srv_to_stop = cur_server
            thr_to_stop = cur_srv_thread
            cur_server = None
            cur_srv_thread = None
            with self._login_lock:
                if self._active_login_server is srv_to_stop:
                    self._active_login_server = None
                    self._active_login_server_thread = None
            if srv_to_stop is not None:
                _stop_oauth_loopback_server(srv_to_stop, thr_to_stop)

        try:
            if _is_cancelled():
                raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")

            _notify("Fish Audio OAuth 메타데이터를 확인하는 중...")
            meta = self.discover_oauth_metadata(force_refresh=True)
            reg_ep = str(meta.get("registration_endpoint") or "").strip()
            if not reg_ep:
                raise FishMcpAuthError("Fish Audio OAuth server does not provide a registration_endpoint")

            scope = " ".join(meta.get("scopes_supported") or ["mcp"])
            expected_iss = meta["issuer"] if meta.get("authorization_response_iss_parameter_supported") else None

            use_fresh_client = bool(force_new_client)
            for dcr_attempt in range(2):
                if _is_cancelled():
                    raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")

                cached_client_id = ""
                cached_client_secret = ""
                cached_reg_ep = ""
                cached_redirect_uri = ""
                preferred_port: int | None = None
                if not use_fresh_client:
                    with self._token_lock:
                        cached_client_id = str(self._tokens.get("client_id") or "").strip()
                        cached_client_secret = str(self._tokens.get("client_secret") or "").strip()
                        cached_reg_ep = str(self._tokens.get("registration_endpoint") or "").strip()
                        cached_redirect_uri = str(self._tokens.get("registered_redirect_uri") or "").strip()
                    if cached_client_id and cached_reg_ep == reg_ep and cached_redirect_uri:
                        preferred_port = _parse_loopback_callback_port(cached_redirect_uri)

                state = secrets.token_urlsafe(32)
                code_verifier, code_challenge = _pkce_pair()

                server, port, done_event, cb_result, srv_thread = _start_oauth_loopback_server(
                    expected_state=state,
                    expected_issuer=expected_iss,
                    preferred_port=preferred_port,
                )
                cur_server = server
                cur_srv_thread = srv_thread
                with self._login_lock:
                    if attempt_cancel.is_set() or self._login_cancel_event.is_set():
                        # Never call `_cleanup_attempt_server()` under `self._login_lock` (self-deadlock):
                        # raising here releases the lock and lets `finally:` close `cur_server` cleanly.
                        raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")
                    if self._active_login_cancel is attempt_cancel:
                        self._active_login_server = server
                        self._active_login_server_thread = srv_thread

                redirect_uri = f"http://127.0.0.1:{port}/callback"
                reused_cached_client = bool(
                    not use_fresh_client
                    and cached_client_id
                    and cached_reg_ep == reg_ep
                    and cached_redirect_uri
                    and redirect_uri == cached_redirect_uri
                )

                if _is_cancelled():
                    raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")

                if reused_cached_client:
                    client_id = cached_client_id
                    client_secret = cached_client_secret
                else:
                    _notify("Fish Audio MCP 클라이언트 등록 중...")
                    dcr_data = self._register_dynamic_client(reg_ep, redirect_uri, scope)
                    client_id = str(dcr_data["client_id"]).strip()
                    client_secret = str(dcr_data.get("client_secret") or "").strip()

                if _is_cancelled():
                    raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")

                auth_params = {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "scope": scope,
                    "state": state,
                    "code_challenge": code_challenge,
                    "code_challenge_method": "S256",
                    "resource": meta["resource"],
                }
                auth_url = f"{meta['authorization_endpoint']}?{urllib.parse.urlencode(auth_params)}"

                _notify("브라우저에서 Fish Audio 로그인 대기 중...")
                opened = webbrowser.open(auth_url, new=1)
                if not opened:
                    self._log("기본 브라우저를 자동으로 열지 못했습니다. 표시된 로그인 링크를 직접 열어 주세요.")

                deadline = time.monotonic() + max(5.0, float(timeout_sec))
                while not done_event.is_set():
                    if _is_cancelled():
                        raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")
                    if time.monotonic() >= deadline:
                        raise FishMcpAuthError("Fish Audio 로그인 대기 시간이 초과되었습니다.")
                    done_event.wait(timeout=0.1)

                # Stop the loopback callback server immediately (non-blocking) BEFORE token exchange
                _cleanup_attempt_server()

                if _is_cancelled():
                    raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")

                if "error" in cb_result:
                    err_code = str(cb_result.get("oauth_error_code") or "").strip().lower()
                    if (
                        reused_cached_client
                        and dcr_attempt == 0
                        and err_code in ("invalid_client", "unauthorized_client", "invalid_redirect_uri", "redirect_uri_mismatch")
                        and not _is_cancelled()
                    ):
                        self._clear_cached_dcr_metadata()
                        use_fresh_client = True
                        _notify("등록된 클라이언트 정보가 만료되어 재등록하는 중...")
                        continue
                    raise FishMcpAuthError(f"Fish Audio OAuth 인증 실패: {_redact_text(str(cb_result['error']))}")

                auth_code = str(cb_result.get("code") or "").strip()
                if not auth_code:
                    raise FishMcpAuthError("Fish Audio OAuth 콜백에서 인증 코드를 받지 못했습니다.")

                if _is_cancelled():
                    raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")

                _notify("OAuth 액세스 토큰을 교환하는 중...")
                token_url = _validate_https_host_url(meta["token_endpoint"], _ALLOWED_FISH_API_HOSTS)
                form_data = {
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": code_verifier,
                    "resource": meta["resource"],
                }
                if client_secret:
                    form_data["client_secret"] = client_secret

                try:
                    tok_resp = self._client.post(
                        token_url,
                        headers={"Accept": "application/json"},
                        data=form_data,
                    )
                except Exception as exc:
                    raise FishMcpAuthError(
                        f"OAuth token exchange failed: {_redact_text(str(exc), (code_verifier, auth_code, client_secret))}"
                    ) from exc

                if tok_resp.status_code != 200:
                    err_body = _redact_text(
                        tok_resp.text[:240],
                        (code_verifier, auth_code, client_secret),
                    )
                    if reused_cached_client and tok_resp.status_code in (400, 401):
                        low_err = err_body.lower()
                        if any(k in low_err for k in ("invalid_client", "unauthorized_client", "redirect_uri")):
                            self._clear_cached_dcr_metadata()
                    raise FishMcpAuthError(
                        f"OAuth 토큰 발급 실패 (HTTP {tok_resp.status_code}): {err_body}",
                        status_code=tok_resp.status_code,
                    )

                tok_json = self._read_bounded_json(tok_resp, MAX_METADATA_BYTES, "OAuth token exchange")
                if not isinstance(tok_json, dict) or not str(tok_json.get("access_token") or "").strip():
                    raise FishMcpAuthError("OAuth 토큰 응답에 access_token이 없습니다.")

                if _is_cancelled():
                    raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")

                expires_in = _safe_finite_float(tok_json.get("expires_in"), 3600.0)
                if expires_in <= 0.0:
                    expires_in = 3600.0
                new_tokens: dict[str, Any] = {
                    "client_id": client_id,
                    "access_token": str(tok_json["access_token"]).strip(),
                    "refresh_token": str(tok_json.get("refresh_token") or "").strip(),
                    "expires_at": time.time() + max(30.0, expires_in),
                    "token_endpoint": token_url,
                    "registration_endpoint": reg_ep,
                    "registered_redirect_uri": redirect_uri,
                    "registered_port": port,
                    "issuer": meta["issuer"],
                    "scope": str(tok_json.get("scope") or scope).strip(),
                }
                if client_secret:
                    new_tokens["client_secret"] = client_secret

                with self._token_lock:
                    if _is_cancelled():
                        raise FishMcpAuthError("OAuth 로그인이 취소되었습니다.")
                    self._tokens = new_tokens
                    self._persist_tokens_unlocked()
                with self._session_lock:
                    self._session_id = None
                    self._initialized = False
                    self._tools_by_name = {}
                    self._cached_balance = None
                    self._cached_balance_at = 0.0

                _notify("Fish Audio 웹 크레딧(MCP) 연결이 완료되었습니다!")
                return {
                    "authenticated": True,
                    "scope": new_tokens["scope"],
                    "expires_at": new_tokens["expires_at"],
                    "has_refresh_token": bool(new_tokens["refresh_token"]),
                    "reused_client": reused_cached_client,
                }

            raise FishMcpAuthError("Fish Audio OAuth 클라이언트 등록에 실패했습니다.")
        finally:
            _cleanup_attempt_server()
            with self._login_lock:
                if self._active_login_cancel is attempt_cancel:
                    self._active_login_cancel = None
                    self._active_login_server = None
                    self._active_login_server_thread = None
                    self._login_in_progress = False

    def _refresh_access_token_unlocked(self) -> str:
        """Refresh the OAuth access token using `refresh_token`. Must be called under `self._token_lock`."""
        refresh_tok = str(self._tokens.get("refresh_token") or "").strip()
        client_id = str(self._tokens.get("client_id") or "").strip()
        token_ep = str(self._tokens.get("token_endpoint") or "").strip()
        client_secret = str(self._tokens.get("client_secret") or "").strip()

        if not refresh_tok or not client_id:
            raise FishMcpAuthError("Fish Audio OAuth 세션이 만료되었습니다. 다시 로그인해 주세요.")

        if not token_ep:
            meta = self.discover_oauth_metadata()
            token_ep = meta["token_endpoint"]
        token_url = _validate_https_host_url(token_ep, _ALLOWED_FISH_API_HOSTS)

        form_data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": client_id,
            "resource": self.mcp_url,
        }
        if client_secret:
            form_data["client_secret"] = client_secret

        try:
            resp = self._client.post(
                token_url,
                headers={"Accept": "application/json"},
                data=form_data,
            )
        except Exception as exc:
            raise FishMcpAuthError(
                f"OAuth 토큰 갱신 요청 실패: {_redact_text(str(exc), self._active_secrets())}"
            ) from exc

        if resp.status_code != 200:
            err_body = _redact_text(resp.text[:240], self._active_secrets())
            if resp.status_code in (400, 401):
                # Token or DCR client revoked/invalidated: clear credentials so UI accurately reflects disconnected state
                self._tokens["access_token"] = ""
                self._tokens["expires_at"] = 0.0
                low_err = err_body.lower()
                if (
                    resp.status_code == 401
                    or "invalid_grant" in low_err
                    or "invalid_client" in low_err
                    or "unauthorized_client" in low_err
                ):
                    self._tokens["refresh_token"] = ""
                if "invalid_client" in low_err or "unauthorized_client" in low_err:
                    self._tokens.pop("registered_redirect_uri", None)
                    self._tokens.pop("registered_port", None)
                try:
                    self._persist_tokens_unlocked()
                except Exception:
                    pass
            raise FishMcpAuthError(
                f"Fish Audio OAuth 토큰 갱신 실패 (HTTP {resp.status_code}): {err_body}",
                status_code=resp.status_code,
            )

        tok_json = self._read_bounded_json(resp, MAX_METADATA_BYTES, "OAuth token refresh")
        if not isinstance(tok_json, dict) or not str(tok_json.get("access_token") or "").strip():
            raise FishMcpAuthError("OAuth 토큰 갱신 응답에 access_token이 없습니다.")

        new_access = str(tok_json["access_token"]).strip()
        new_refresh = str(tok_json.get("refresh_token") or refresh_tok).strip()
        expires_in = _safe_finite_float(tok_json.get("expires_in"), 3600.0)
        if expires_in <= 0.0:
            expires_in = 3600.0

        self._tokens["access_token"] = new_access
        self._tokens["refresh_token"] = new_refresh
        self._tokens["expires_at"] = time.time() + max(30.0, expires_in)
        self._tokens["token_endpoint"] = token_url
        if tok_json.get("scope"):
            self._tokens["scope"] = str(tok_json["scope"]).strip()
        self._persist_tokens_unlocked()
        return new_access

    def _get_valid_access_token(
        self,
        *,
        force_refresh: bool = False,
        failed_token: str | None = None,
    ) -> str:
        with self._token_lock:
            access = str(self._tokens.get("access_token") or "").strip()
            expires_at = _safe_finite_float(self._tokens.get("expires_at"), 0.0)
            has_refresh = bool(str(self._tokens.get("refresh_token") or "").strip())

            # Coalesce concurrent HTTP 401 refreshes: if another worker already replaced
            # `failed_token` with a fresh non-expired token while we waited on `_token_lock`,
            # reuse it without triggering a duplicate `/oauth/token` refresh call.
            if (
                force_refresh
                and failed_token
                and access
                and access != failed_token
                and (expires_at <= 0.0 or time.time() < expires_at - TOKEN_REFRESH_SKEW_SEC)
            ):
                return access

            needs_refresh = force_refresh or not access or (expires_at > 0.0 and time.time() >= expires_at - TOKEN_REFRESH_SKEW_SEC)
            if needs_refresh:
                if has_refresh:
                    return self._refresh_access_token_unlocked()
                if access and not force_refresh and (expires_at <= 0.0 or time.time() < expires_at):
                    return access
                raise FishMcpAuthError("Fish Audio MCP 로그인이 필요합니다.")
            return access

    # ------------------------------------------------------------------
    # Streamable HTTP MCP JSON-RPC 2.0 Transport
    # ------------------------------------------------------------------

    def _next_rpc_id(self) -> int:
        with self._session_lock:
            self._rpc_id += 1
            return self._rpc_id

    def _send_mcp_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        is_notification: bool = False,
        include_session_id: bool = True,
        allow_auth_retry: bool = True,
        allow_session_retry: bool = True,
    ) -> dict[str, Any] | None:
        """
        Send a JSON-RPC 2.0 request/notification to `https://api.fish.audio/mcp`.
        Supports `Mcp-Session-Id`, `application/json`, and `text/event-stream` SSE.
        """
        token = self._get_valid_access_token(force_refresh=False)
        req_id = None if is_notification else self._next_rpc_id()
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if req_id is not None:
            body["id"] = req_id
        if params is not None:
            body["params"] = params

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        sent_sid: str | None = None
        with self._session_lock:
            if include_session_id and self._session_id:
                sent_sid = self._session_id
                headers["Mcp-Session-Id"] = sent_sid
            if include_session_id and self._initialized and self._protocol_version:
                headers["MCP-Protocol-Version"] = self._protocol_version

        try:
            resp = self._client.post(self.mcp_url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise FishMcpError(f"Fish Audio MCP 요청 시간 초과 ({method}): {_redact_text(str(exc))}") from exc
        except Exception as exc:
            raise FishMcpError(f"Fish Audio MCP 네트워크 오류 ({method}): {_redact_text(str(exc), self._active_secrets())}") from exc

        # Capture/update Mcp-Session-Id header if server assigns one
        new_sid = _get_header_ci(resp.headers, "mcp-session-id")
        if new_sid:
            with self._session_lock:
                self._session_id = new_sid

        # Handle expired access token (HTTP 401): coalesce refresh once & retry
        if resp.status_code == 401 and allow_auth_retry:
            self._get_valid_access_token(force_refresh=True, failed_token=token)
            return self._send_mcp_rpc(
                method,
                params,
                is_notification=is_notification,
                include_session_id=include_session_id,
                allow_auth_retry=False,
                allow_session_retry=allow_session_retry,
            )

        # Handle expired MCP session (HTTP 404 with active session): coalesce re-initialize atomically under RLock
        if (
            resp.status_code == 404
            and include_session_id
            and allow_session_retry
            and method != "initialize"
        ):
            with self._session_lock:
                if (not self._initialized) or (self._session_id == sent_sid):
                    self._session_id = None
                    self._initialized = False
                    self._tools_by_name = {}
                    self._ensure_initialized(force=True)
            return self._send_mcp_rpc(
                method,
                params,
                is_notification=is_notification,
                include_session_id=True,
                allow_auth_retry=allow_auth_retry,
                allow_session_retry=False,
            )

        if is_notification and resp.status_code in (200, 202, 204):
            return None

        if resp.status_code not in (200, 201, 202):
            raw_err = resp.content[:1024].decode("utf-8", errors="replace") if resp.content else ""
            clean_err = _redact_text(raw_err[:280], self._active_secrets())
            raise _classify_error_message(
                f"Fish Audio MCP HTTP {resp.status_code} ({method}): {clean_err}",
                status_code=resp.status_code,
            )

        raw_bytes = resp.content if resp.content is not None else b""
        if len(raw_bytes) > MAX_RPC_RESPONSE_BYTES:
            raise FishMcpError(f"Fish Audio MCP response exceeded limit ({MAX_RPC_RESPONSE_BYTES} bytes)")
        if not raw_bytes.strip():
            if is_notification:
                return None
            raise FishMcpError(f"Fish Audio MCP returned empty response for {method}")

        content_type = _get_header_ci(resp.headers, "content-type").lower()
        decoded_text = raw_bytes.decode("utf-8", errors="replace")

        rpc_msg: dict[str, Any] | None = None
        if "text/event-stream" in content_type or decoded_text.lstrip().startswith(("event:", "data:", ":")):
            events = _parse_sse_json_messages(decoded_text)
            for ev in events:
                if req_id is not None and ev.get("id") == req_id:
                    rpc_msg = ev
                    break
            if rpc_msg is None:
                for ev in reversed(events):
                    if "result" in ev or "error" in ev:
                        rpc_msg = ev
                        break
        else:
            try:
                parsed = json.loads(decoded_text)
            except json.JSONDecodeError as exc:
                raise FishMcpError(
                    f"Fish Audio MCP invalid JSON ({method}): {_redact_text(decoded_text[:200], self._active_secrets())}"
                ) from exc
            if isinstance(parsed, dict):
                rpc_msg = parsed
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and (req_id is None or item.get("id") == req_id):
                        rpc_msg = item
                        break

        if rpc_msg is None:
            raise FishMcpError(f"Fish Audio MCP response missing JSON-RPC payload for {method}")

        if "error" in rpc_msg and rpc_msg["error"] is not None:
            err_obj = rpc_msg["error"]
            if isinstance(err_obj, dict):
                rpc_code = err_obj.get("code")
                err_msg = _redact_text(str(err_obj.get("message") or err_obj), self._active_secrets())
            else:
                rpc_code = None
                err_msg = _redact_text(str(err_obj), self._active_secrets())
            raise _classify_error_message(
                f"Fish Audio MCP RPC error ({method}): {err_msg}",
                rpc_code=int(rpc_code) if isinstance(rpc_code, int) else None,
            )

        result = rpc_msg.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def _ensure_initialized(self, *, force: bool = False) -> None:
        with self._session_lock:
            if self._initialized and not force:
                return
            init_res = self._send_mcp_rpc(
                "initialize",
                {
                    "protocolVersion": FISH_MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self.client_name,
                        "version": FISH_MCP_CLIENT_VERSION,
                    },
                },
                include_session_id=False,
                allow_session_retry=False,
            )
            if isinstance(init_res, dict) and init_res.get("protocolVersion"):
                self._protocol_version = str(init_res["protocolVersion"]).strip()
            self._initialized = True
            # Send MCP initialized notification (best-effort; some servers return 202 empty)
            try:
                self._send_mcp_rpc(
                    "notifications/initialized",
                    {},
                    is_notification=True,
                    include_session_id=True,
                    allow_session_retry=False,
                )
            except Exception:
                pass

    def list_tools(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Call MCP `tools/list` once per session and cache tool schemas for adaptive invocation."""
        self._ensure_initialized()
        with self._session_lock:
            if self._tools_by_name and not force_refresh:
                return list(self._tools_by_name.values())

        tools_map: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        for _page in range(10):
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            res = self._send_mcp_rpc("tools/list", params)
            if not isinstance(res, dict):
                break
            items = res.get("tools")
            if isinstance(items, list):
                for tool in items:
                    if isinstance(tool, dict) and tool.get("name"):
                        tools_map[str(tool["name"])] = tool
            next_cursor = res.get("nextCursor")
            if isinstance(next_cursor, str) and next_cursor.strip():
                cursor = next_cursor.strip()
            else:
                break

        with self._session_lock:
            self._tools_by_name = tools_map
            return list(self._tools_by_name.values())

    def _find_tool_schema(self, candidates: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
        """Resolve exact MCP tool name and schema from cached `tools/list`."""
        with self._session_lock:
            has_tools = bool(self._tools_by_name)
        if not has_tools:
            self.list_tools()
        with self._session_lock:
            for name in candidates:
                if name in self._tools_by_name:
                    return name, self._tools_by_name[name]
            norm_map = {k.lower().replace("-", "_"): (k, v) for k, v in self._tools_by_name.items()}
            for name in candidates:
                norm = name.lower().replace("-", "_")
                if norm in norm_map:
                    return norm_map[norm]
        return candidates[0], {}

    def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        """
        Invoke MCP `tools/call` and unpack `structuredContent` + text/audio `content` blocks.
        Returns `(parsed_dict, combined_text, raw_content_items)`.
        """
        self._ensure_initialized()
        res = self._send_mcp_rpc("tools/call", {"name": tool_name, "arguments": arguments})
        if not isinstance(res, dict):
            raise FishMcpError(f"Unexpected MCP tools/call result for {tool_name}")

        parsed: dict[str, Any] = {}
        structured = res.get("structuredContent")
        if isinstance(structured, dict):
            parsed.update(structured)

        content_items = res.get("content")
        items: list[dict[str, Any]] = [x for x in content_items if isinstance(x, dict)] if isinstance(content_items, list) else []
        text_parts: list[str] = []

        for item in items:
            itype = str(item.get("type") or "").lower()
            if itype == "text":
                txt = str(item.get("text") or "")
                if txt:
                    text_parts.append(txt)
                    stripped = txt.strip()
                    # Unwrap fenced JSON if present
                    if stripped.startswith("```"):
                        m_fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
                        if m_fence:
                            stripped = m_fence.group(1).strip()
                    if stripped.startswith("{") and stripped.endswith("}"):
                        try:
                            obj = json.loads(stripped)
                            if isinstance(obj, dict):
                                parsed.update(obj)
                        except json.JSONDecodeError:
                            pass
                    elif "{" in stripped and "}" in stripped:
                        first_b = stripped.find("{")
                        last_b = stripped.rfind("}")
                        if 0 <= first_b < last_b:
                            try:
                                obj = json.loads(stripped[first_b : last_b + 1])
                                if isinstance(obj, dict):
                                    parsed.update(obj)
                            except json.JSONDecodeError:
                                pass

        combined_text = "\n".join(text_parts).strip()

        if bool(res.get("isError", False)):
            err_detail = _redact_text(combined_text or json.dumps(parsed, ensure_ascii=False)[:240], self._active_secrets())
            raise _classify_error_message(f"Fish Audio MCP tool '{tool_name}' error: {err_detail}")

        return parsed, combined_text, items

    # ------------------------------------------------------------------
    # High-Level Credit Balance & TTS Synthesis API
    # ------------------------------------------------------------------

    def get_credit_balance(self, force_refresh: bool = False) -> dict[str, Any]:
        """
        Query Fish Audio MCP `get_credit_balance` (0 credits) and return:
          `{"plan": str, "remaining_credits": int | None, "max_text_bytes": int, ...}`

        Supports both Fish Audio's live MCP schema (`package_balance`, `package_extra_balance`,
        `api_credit_usd`, `package_cycle_ends_at`, `tts_max_text_bytes_per_call`) and
        legacy/generic credit balance schemas (`remaining_credits`, `credit_balance`, etc.).
        """
        now = time.time()
        with self._session_lock:
            if (
                not force_refresh
                and self._cached_balance is not None
                and (now - self._cached_balance_at) < BALANCE_CACHE_TTL_SEC
            ):
                return dict(self._cached_balance)

        tool_name, _schema = self._find_tool_schema(
            ("get_credit_balance", "credit_balance", "get_balance", "get_credits")
        )
        parsed, text, _items = self._call_tool(tool_name, {})

        plan = _extract_first_str(parsed, ("plan", "tier", "plan_name", "subscription", "package_name"))

        # Live Fish Audio MCP returns `package_balance` (+ optional `package_extra_balance`)
        pkg_balance = _extract_first_int(parsed, ("package_balance", "package_credits"))
        pkg_extra = _extract_first_int(parsed, ("package_extra_balance", "extra_package_balance", "extra_credits"))
        if pkg_balance is not None or pkg_extra is not None:
            remaining: int | None = max(0, pkg_balance or 0) + max(0, pkg_extra or 0)
        else:
            remaining = _extract_first_int(
                parsed,
                (
                    "remaining_credits",
                    "credits_remaining",
                    "available_credits",
                    "credit_balance",
                    "web_credits",
                    "credits",
                    "balance",
                ),
            )

        max_bytes = _extract_first_int(
            parsed,
            (
                "tts_max_text_bytes_per_call",
                "max_text_bytes_per_call",
                "max_text_bytes",
                "tts_max_bytes",
                "max_tts_bytes",
            ),
        )
        api_credit_usd = _extract_first_str(parsed, ("api_credit_usd",))
        raw_api_usd = parsed.get("api_credit_usd")
        if api_credit_usd is None and not isinstance(raw_api_usd, bool) and isinstance(raw_api_usd, (int, float)):
            api_credit_usd = str(raw_api_usd)
        cycle_ends_at = _extract_first_str(parsed, ("package_cycle_ends_at", "cycle_ends_at", "reset_at"))
        team_name = _extract_first_str(parsed, ("team_name", "workspace_name"))

        # Fallback regex parsing if server returns prose or partially formatted JSON text
        if plan is None and text:
            m_plan = re.search(r"""(?i)["']?\bplan\b["']?\s*[:=]\s*["']?([A-Za-z0-9_\- ]+)["']?""", text)
            if m_plan:
                plan = m_plan.group(1).strip()
        if remaining is None and text:
            m_pkg = re.search(r"""(?i)["']?package_balance["']?\s*[:=]\s*["']?([\d,]+)["']?""", text)
            m_ext = re.search(r"""(?i)["']?package_extra_balance["']?\s*[:=]\s*["']?([\d,]+)["']?""", text)
            if m_pkg or m_ext:
                pkg_balance = int(m_pkg.group(1).replace(",", "")) if m_pkg else 0
                pkg_extra = int(m_ext.group(1).replace(",", "")) if m_ext else 0
                remaining = max(0, pkg_balance) + max(0, pkg_extra)
            else:
                m_rem = re.search(
                    r"""(?i)["']?(?:remaining[_\s]?credits?|credits?[_\s]?remaining|available[_\s]?credits?|credits?[_\s]?balance|credit_balance|balance)["']?\s*[:=]\s*["']?([\d,]+)["']?""",
                    text,
                )
                if m_rem:
                    remaining = int(m_rem.group(1).replace(",", ""))
        if max_bytes is None and text:
            m_max = re.search(
                r"""(?i)["']?(?:tts_max_text_bytes_per_call|max_text_bytes_per_call|max_text_bytes)["']?\s*[:=]\s*["']?([\d,]+)["']?""",
                text,
            )
            if m_max:
                max_bytes = int(m_max.group(1).replace(",", ""))

        effective_max_bytes = max(64, int(max_bytes)) if max_bytes and max_bytes > 0 else DEFAULT_MAX_TEXT_BYTES
        result = {
            "plan": plan or "unknown",
            "remaining_credits": remaining,
            "package_balance": pkg_balance,
            "package_extra_balance": pkg_extra,
            "api_credit_usd": api_credit_usd,
            "package_cycle_ends_at": cycle_ends_at,
            "team_name": team_name,
            "max_text_bytes": effective_max_bytes,
            "raw": parsed if parsed else text,
        }
        with self._session_lock:
            self._max_text_bytes = effective_max_bytes
            self._cached_balance = dict(result)
            self._cached_balance_at = time.time()
        return result

    def _build_tts_arguments(
        self,
        text: str,
        voice_id: str | None,
        language: str | None,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        props = {}
        input_schema = schema.get("inputSchema")
        if isinstance(input_schema, dict) and isinstance(input_schema.get("properties"), dict):
            props = input_schema["properties"]

        def _pick_param(candidates: tuple[str, ...], default: str | None) -> str | None:
            if props:
                for cand in candidates:
                    if cand in props:
                        return cand
                low_map = {str(k).lower(): str(k) for k in props}
                for cand in candidates:
                    if cand.lower() in low_map:
                        return low_map[cand.lower()]
            return default

        text_key = _pick_param(("text", "input", "prompt", "content", "script"), "text") or "text"
        args: dict[str, Any] = {text_key: text}

        clean_voice = str(voice_id or "").strip()
        if clean_voice:
            voice_key = _pick_param(("voice_id", "reference_id", "voice", "model_id"), "voice_id")
            if voice_key:
                args[voice_key] = clean_voice

        clean_lang = str(language or "").strip().lower()
        if clean_lang:
            lang_key = _pick_param(("language", "lang", "language_code"), None if props else "language")
            if lang_key:
                args[lang_key] = clean_lang

        return args

    def _build_get_speech_arguments(self, task_id: str, schema: dict[str, Any]) -> dict[str, Any]:
        props = {}
        input_schema = schema.get("inputSchema")
        if isinstance(input_schema, dict) and isinstance(input_schema.get("properties"), dict):
            props = input_schema["properties"]
        for cand in ("task_id", "id", "speech_id", "job_id"):
            if cand in props:
                return {cand: task_id}
        return {"task_id": task_id}

    def _extract_audio_or_task(
        self,
        parsed: dict[str, Any],
        text: str,
        items: list[dict[str, Any]],
    ) -> tuple[str | None, str | None, bytes | None, str | None]:
        """
        Inspect MCP tool output and extract `(audio_url, task_id, inline_audio_bytes, status)`.
        """
        # 1. Check inline MCP audio content item (`type == "audio"` with base64 `data`)
        max_b64_chars = ((MAX_AUDIO_DOWNLOAD_BYTES * 4) // 3) + 16
        for item in items:
            if str(item.get("type") or "").lower() == "audio" and item.get("data"):
                try:
                    raw_b64 = str(item["data"]).strip()
                    if raw_b64.lower().startswith("data:") and ";base64," in raw_b64[:96].lower():
                        raw_b64 = raw_b64.split(",", 1)[1]
                    raw_b64 = re.sub(r"\s+", "", raw_b64)
                    if not raw_b64 or len(raw_b64) > max_b64_chars:
                        continue
                    raw = base64.b64decode(raw_b64, validate=True)
                    if raw and len(raw) <= MAX_AUDIO_DOWNLOAD_BYTES:
                        return None, None, raw, "completed"
                except Exception:
                    pass

        status = _extract_first_str(parsed, ("status", "state"))
        audio_url = _extract_first_str(
            parsed,
            ("audio_url", "mp3_url", "download_url", "file_url", "output_url", "url", "audio"),
        )
        if audio_url and "://" not in audio_url:
            audio_url = None

        if audio_url is None and text:
            m_url = _HTTPS_URL_RE.search(text)
            if m_url:
                audio_url = m_url.group(0).rstrip(".,;:")

        task_id = _extract_first_str(parsed, ("task_id", "speech_id", "job_id", "id"))
        if task_id is None and text and audio_url is None:
            m_task = re.search(r"(?i)(?:task_id|speech_id|job_id)\s*[:=]\s*([A-Za-z0-9_\-]+)", text)
            if m_task:
                task_id = m_task.group(1).strip()

        return audio_url, task_id, None, status

    def _poll_get_speech(
        self,
        task_id: str,
        *,
        cancel_check: Callable[[], bool] | None = None,
        timeout_sec: float = 90.0,
    ) -> tuple[str | None, bytes | None]:
        """Poll Fish Audio MCP `get_speech` when `text_to_speech` returns an async `task_id`."""
        tool_name, schema = self._find_tool_schema(("get_speech", "get_tts_status", "speech_status"))
        args = self._build_get_speech_arguments(task_id, schema)
        deadline = time.monotonic() + max(5.0, float(timeout_sec))
        poll_interval = 1.0

        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise FishMcpError("TTS synthesis cancelled while polling get_speech")

            parsed, text, items = self._call_tool(tool_name, args)
            audio_url, _tid, inline_bytes, status = self._extract_audio_or_task(parsed, text, items)
            if audio_url or inline_bytes:
                return audio_url, inline_bytes

            low_status = str(status or "").lower()
            if low_status in ("failed", "error", "cancelled", "canceled", "rejected"):
                err_msg = _extract_first_str(parsed, ("error", "message", "detail")) or text or low_status
                raise _classify_error_message(f"Fish Audio MCP get_speech failed: {_redact_text(err_msg)}")

            # Interruptible sleep so barge-in responds immediately
            sleep_end = min(deadline, time.monotonic() + poll_interval)
            while time.monotonic() < sleep_end:
                if cancel_check is not None and cancel_check():
                    raise FishMcpError("TTS synthesis cancelled while polling get_speech")
                time.sleep(min(0.1, max(0.01, sleep_end - time.monotonic())))
            poll_interval = min(2.5, poll_interval * 1.25)

        raise FishMcpError(f"Fish Audio MCP get_speech timed out for task_id={task_id}")

    def _download_audio_bytes(
        self,
        audio_url: str,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> bytes:
        """
        Securely download synthesized audio from `audio_url`:
        - Enforces `https://` + `verify=True` + DNS/IP SSRF guard on EVERY redirect hop
        - Enforces `<= MAX_AUDIO_DOWNLOAD_BYTES` (32 MB) while streaming
        - Honors `cancel_check` between chunks
        """
        current_url = _validate_audio_download_url(audio_url, resolve_dns=True)

        for _hop in range(MAX_AUDIO_REDIRECTS + 1):
            if cancel_check is not None and cancel_check():
                raise FishMcpError("Audio download cancelled")

            try:
                with self._client.stream("GET", current_url, headers={"Accept": "audio/*,*/*;q=0.8"}) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = _get_header_ci(resp.headers, "location")
                        if not location:
                            raise FishMcpError(
                                f"Audio download redirect missing Location header (HTTP {resp.status_code})"
                            )
                        next_url = urllib.parse.urljoin(current_url, location)
                        current_url = _validate_audio_download_url(next_url, resolve_dns=True)
                        continue

                    if resp.status_code != 200:
                        raise _classify_error_message(
                            f"Audio download failed (HTTP {resp.status_code})",
                            status_code=resp.status_code,
                        )

                    cl_hdr = _get_header_ci(resp.headers, "content-length")
                    if cl_hdr:
                        try:
                            if int(cl_hdr) > MAX_AUDIO_DOWNLOAD_BYTES:
                                raise FishMcpError(
                                    f"Audio file exceeds safety size limit ({MAX_AUDIO_DOWNLOAD_BYTES} bytes)"
                                )
                        except ValueError:
                            pass

                    buf = bytearray()
                    for chunk in resp.iter_bytes(chunk_size=16384):
                        if cancel_check is not None and cancel_check():
                            raise FishMcpError("Audio download cancelled")
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) > MAX_AUDIO_DOWNLOAD_BYTES:
                            raise FishMcpError(
                                f"Audio download exceeded safety size limit ({MAX_AUDIO_DOWNLOAD_BYTES} bytes)"
                            )
                    if not buf:
                        raise FishMcpError("Downloaded audio payload is empty")
                    return bytes(buf)
            except (FishMcpError, ValueError):
                raise
            except httpx.TimeoutException as exc:
                raise FishMcpError(
                    f"Audio download timed out: {_redact_text(str(exc), self._active_secrets())}"
                ) from exc
            except Exception as exc:
                raise FishMcpError(
                    f"Audio download network error: {_redact_text(str(exc), self._active_secrets())}"
                ) from exc

        raise FishMcpError(f"Audio download exceeded max redirects ({MAX_AUDIO_REDIRECTS})")

    def _synthesize_single_chunk(
        self,
        chunk_text: str,
        *,
        voice_id: str | None,
        language: str | None,
        cancel_check: Callable[[], bool] | None,
    ) -> tuple[np.ndarray, int, bytes]:
        if cancel_check is not None and cancel_check():
            raise FishMcpError("TTS synthesis cancelled")

        tts_tool_name, tts_schema = self._find_tool_schema(("text_to_speech", "tts", "synthesize_speech"))
        args = self._build_tts_arguments(chunk_text, voice_id, language, tts_schema)

        # Fish Audio MCP allows 1 free identical retry within 5 min on transient timeout/transport failures
        last_exc: Exception | None = None
        parsed: dict[str, Any] = {}
        text_out = ""
        items: list[dict[str, Any]] = []
        for attempt in range(2):
            if cancel_check is not None and cancel_check():
                raise FishMcpError("TTS synthesis cancelled")
            try:
                parsed, text_out, items = self._call_tool(tts_tool_name, args)
                last_exc = None
                break
            except (FishMcpAuthError, FishMcpQuotaError):
                raise
            except FishMcpError as exc:
                last_exc = exc
                is_transient = (
                    exc.status_code in (500, 502, 503, 504)
                    or "시간 초과" in str(exc)
                    or "네트워크 오류" in str(exc)
                    or "timeout" in str(exc).lower()
                )
                if attempt == 0 and is_transient:
                    continue
                raise
        if last_exc is not None:
            raise last_exc

        audio_url, task_id, inline_bytes, _status = self._extract_audio_or_task(parsed, text_out, items)

        if inline_bytes is None and audio_url is None and task_id:
            audio_url, inline_bytes = self._poll_get_speech(task_id, cancel_check=cancel_check)

        if inline_bytes is not None:
            raw_bytes = inline_bytes
        elif audio_url is not None:
            raw_bytes = self._download_audio_bytes(audio_url, cancel_check=cancel_check)
        else:
            snippet = _redact_text(text_out[:200] or json.dumps(parsed, ensure_ascii=False)[:200], self._active_secrets())
            raise FishMcpError(f"Fish Audio MCP text_to_speech returned neither audio_url nor task_id: {snippet}")

        try:
            pcm_f32, sr = decode_audio_bytes_to_pcm(raw_bytes)
        except ValueError as exc:
            raise FishMcpError(f"Failed to decode synthesized MCP audio: {exc}") from exc

        # Optimistically decrement cached remaining credit estimate (1 credit = 1 UTF-8 byte)
        spent = _utf8_len(chunk_text)
        with self._session_lock:
            if self._cached_balance is not None and isinstance(self._cached_balance.get("remaining_credits"), int):
                self._cached_balance["remaining_credits"] = max(0, self._cached_balance["remaining_credits"] - spent)

        return pcm_f32, sr, raw_bytes

    def synthesize_to_pcm(
        self,
        text: str,
        voice_id: str | None = None,
        language: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[np.ndarray, int, bytes]:
        """
        Synthesize `text` via Official Fish Audio MCP into `(mono_float32, sample_rate, raw_audio_bytes)`.

        - Automatically splits text exceeding `max_text_bytes` using `split_text_by_utf8_bytes`
          (default safe ceiling 480 bytes, or server-reported `tts_max_text_bytes_per_call`).
        - Downloads MP3 securely and decodes to mono `float32` PCM via `soundfile`.
        - Raises `FishMcpAuthError`, `FishMcpQuotaError`, or `FishMcpError`.
        """
        if cancel_check is not None and cancel_check():
            raise FishMcpError("TTS synthesis cancelled")

        with self._session_lock:
            max_b = self._max_text_bytes
        limit = max_b if (max_b and max_b >= 64) else DEFAULT_SAFE_SPLIT_BYTES
        chunks = split_text_by_utf8_bytes(text, max_bytes=limit)
        if not chunks:
            raise ValueError("Synthesis text is empty")

        if len(chunks) == 1:
            return self._synthesize_single_chunk(
                chunks[0],
                voice_id=voice_id,
                language=language,
                cancel_check=cancel_check,
            )

        pcm_parts: list[np.ndarray] = []
        target_sr: int | None = None
        for ch in chunks:
            part_pcm, part_sr, _part_raw = self._synthesize_single_chunk(
                ch,
                voice_id=voice_id,
                language=language,
                cancel_check=cancel_check,
            )
            if target_sr is None:
                target_sr = part_sr
            elif part_sr != target_sr:
                part_pcm = _resample_mono_f32(part_pcm, part_sr, target_sr)
            pcm_parts.append(part_pcm)

        assert target_sr is not None
        combined_pcm = np.ascontiguousarray(np.concatenate(pcm_parts), dtype=np.float32)
        combined_raw = _encode_pcm_for_cache(combined_pcm, target_sr)
        return combined_pcm, target_sr, combined_raw


# ----------------------------------------------------------------------
# CLI Smoke-Test & Discovery Self-Check
# ----------------------------------------------------------------------


def _run_mp3_decoder_self_test() -> dict[str, Any]:
    """Verify native MP3 encode+decode roundtrip using `soundfile` / `libsndfile`."""
    sr = 44100
    t = np.linspace(0.0, 0.1, int(sr * 0.1), dtype=np.float32)
    wave = (0.25 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, wave, sr, format="MP3")
    mp3_bytes = buf.getvalue()
    pcm, out_sr = decode_audio_bytes_to_pcm(mp3_bytes)
    return {
        "soundfile_version": getattr(sf, "__version__", "unknown"),
        "libsndfile_version": getattr(sf, "__libsndfile_version__", "unknown"),
        "mp3_supported": "MP3" in sf.available_formats(),
        "encoded_bytes": len(mp3_bytes),
        "decoded_samples": int(pcm.size),
        "decoded_dtype": str(pcm.dtype),
        "decoded_sr": int(out_sr),
    }


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Official Fish Audio MCP OAuth discovery check & interactive smoke test."
    )
    parser.add_argument(
        "--check-discovery",
        action="store_true",
        help="Non-mutating live check of Fish Audio OAuth well-known metadata + local MP3 decoder self-test.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Interactive OAuth login -> print live tools/list schemas & credit balance.",
    )
    parser.add_argument(
        "--speak",
        type=str,
        default="",
        help="Optional text to synthesize & decode during --smoke-test (consumes UTF-8 byte credits).",
    )
    parser.add_argument(
        "--voice-id",
        type=str,
        default="",
        help="Optional Fish Audio public voice_id to pass with --speak.",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="",
        help="Optional language code (e.g. ko, ja) to pass with --speak.",
    )
    args = parser.parse_args(argv)

    if not args.check_discovery and not args.smoke_test:
        parser.print_help()
        return 2

    backend = FishMcpBackend()
    try:
        if args.check_discovery:
            print("[1/2] Checking Fish Audio OAuth well-known discovery metadata...")
            meta = backend.discover_oauth_metadata(force_refresh=True)
            print(json.dumps(meta, ensure_ascii=False, indent=2))

            print("\n[2/2] Running local MP3 decoder self-test (soundfile / libsndfile)...")
            mp3_info = _run_mp3_decoder_self_test()
            print(json.dumps(mp3_info, ensure_ascii=False, indent=2))
            if not mp3_info["mp3_supported"] or mp3_info["decoded_samples"] <= 0:
                print("ERROR: MP3 decoder self-test failed.", file=sys.stderr)
                return 1
            print("\nOK: Discovery metadata & native MP3 decoder verified.")

        if args.smoke_test:
            print("\nStarting interactive Fish Audio MCP OAuth login...")
            login_info = backend.login_interactive(status_cb=lambda m: print(f"  [OAuth] {m}"))
            print(f"Login succeeded: {json.dumps(login_info, ensure_ascii=False)}")

            print("\nFetching live MCP tools/list schemas...")
            tools = backend.list_tools(force_refresh=True)
            target_tools = {"text_to_speech", "get_speech", "get_credit_balance"}
            selected = [t for t in tools if str(t.get("name") or "") in target_tools]
            print(f"Total tools discovered: {len(tools)}")
            print(json.dumps(selected, ensure_ascii=False, indent=2))

            print("\nFetching credit balance (0 credits)...")
            bal = backend.get_credit_balance(force_refresh=True)
            print(json.dumps(bal, ensure_ascii=False, indent=2))

            if args.speak.strip():
                print(f"\nSynthesizing test text ({len(args.speak.strip().encode('utf-8'))} UTF-8 bytes)...")
                pcm, sr, raw_bytes = backend.synthesize_to_pcm(
                    args.speak.strip(),
                    voice_id=args.voice_id.strip() or None,
                    language=args.language.strip() or None,
                )
                dur_sec = pcm.size / float(sr) if sr else 0.0
                print(
                    json.dumps(
                        {
                            "raw_audio_bytes": len(raw_bytes),
                            "sample_rate": sr,
                            "samples": int(pcm.size),
                            "duration_sec": round(dur_sec, 3),
                            "peak_abs": round(float(np.max(np.abs(pcm))), 4) if pcm.size else 0.0,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        return 0
    except Exception as exc:
        print(f"ERROR: {_redact_text(str(exc), backend._active_secrets())}", file=sys.stderr)
        return 1
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(_cli_main())
