"""
OS-standard user data isolation and path resolution utilities.
--------------------------------------------------------------
Ensures ZERO personal data (API keys, config.json, browser profiles, cache, logs)
is ever created or stored inside the executable or project workspace directory.
All mutable per-user data resides exclusively under:
  %LOCALAPPDATA%\\VoiceCompanionData\\
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

if os.name == "nt":
    import ctypes.wintypes

USER_DATA_FOLDER_NAME = "VoiceCompanionData"
DPAPI_PREFIX = "dpapi:"
CRYPTPROTECT_UI_FORBIDDEN = 0x1
SECRET_CONFIG_KEYS: tuple[str, ...] = (
    "fish_api_key",
    "gemini_api_key",
    "claude_api_key",
    "openai_api_key",
    "fish_mcp_oauth_data",
    "colab_s2pro_api_key",
)
MAX_CONFIG_FILE_BYTES = 4 * 1024 * 1024
MAX_DEFAULT_VOICES_BYTES = 2 * 1024 * 1024

_SID_RE = re.compile(r"^S-1-[0-9-]+$")
_ACL_LOCK = threading.Lock()
_CONFIG_IO_LOCK = threading.RLock()
_HARDENED_ACL_PATHS: set[str] = set()
_CACHED_USER_SID: str | None = None
_SID_INIT_LOCK = threading.Lock()
_DPAPI_INIT_LOCK = threading.Lock()
_DPAPI_CRYPT32: Any = None
_DPAPI_KERNEL32: Any = None
_SID_ADVAPI32: Any = None
_SID_KERNEL32: Any = None

# Secret redaction patterns (kept idempotent via negative lookahead on [REDACTED...])
_SECRET_JSON_KV_RE = re.compile(
    r"""(?i)((?:\\?['"])?(?:fish_api_key|gemini_api_key|claude_api_key|openai_api_key|fish_mcp_oauth_data|colab_s2pro_api_key|access_token|refresh_token|client_secret|id_token)(?:\\?['"])?\s*[:=]\s*(?:\\?['"])?)(?!\[REDACTED\b)([^'"\\\s,}\]]+)"""
)
_DPAPI_BLOB_RE = re.compile(r"dpapi:[A-Za-z0-9+/=_\-]{4,}")
_AIZA_KEY_RE = re.compile(r"AIza[0-9A-Za-z\-_]{12,}")
_SK_KEY_RE = re.compile(r"\bsk-[0-9A-Za-z_\-]{10,}\b")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)(?!\[REDACTED\b)([^\s'\"`,;}\]]{6,})")
_GOOG_HDR_RE = re.compile(
    r"""(?i)(['"]?(?:x-goog-api-key|x-api-key)['"]?\s*[:=]\s*['"]?)(?!\[REDACTED\b)([^'"\s,;&}\]]+)"""
)
_QUERY_KEY_RE = re.compile(r"(?i)([?&](?:api_?)?(?:key)=)(?!\[REDACTED\b)([^&\s'\"<>}\]]+)")
_BARE_KEY_KV_RE = re.compile(r"(?i)((?:^|[\s,(])(?:api[_-]?)?(?:key)=)(?!\[REDACTED\b)([^&\s'\"<>}\],)]{6,})")

# Zero-circular-import API model ID sanitization patterns (mirrors gemini_connector.py)
_SAFE_API_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UNSAFE_API_MODEL_CHARS_RE = re.compile(r"(?:\.\.|[/\\?#&=;<>\x00-\x1f\x7f])")
_PROVIDER_API_MODEL_TOKEN_RES: dict[str, re.Pattern[str]] = {
    "gemini": re.compile(r"\b(gemini-[A-Za-z0-9][A-Za-z0-9._-]{1,120})\b"),
    "claude": re.compile(r"\b(claude-[A-Za-z0-9][A-Za-z0-9._-]{1,120})\b"),
    "chatgpt": re.compile(r"\b((?:gpt-[A-Za-z0-9]|o[1-9])[A-Za-z0-9._-]{1,120})\b"),
}


if os.name == "nt":
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Sid", ctypes.c_void_p),
            ("Attributes", ctypes.wintypes.DWORD),
        ]

    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [
            ("User", _SID_AND_ATTRIBUTES),
        ]


def _configure_dpapi() -> tuple[Any, Any]:
    """Configure private ctypes WinDLL handles for Windows crypt32 DPAPI & kernel32.LocalFree."""
    global _DPAPI_CRYPT32, _DPAPI_KERNEL32
    if _DPAPI_CRYPT32 is not None and _DPAPI_KERNEL32 is not None:
        return _DPAPI_CRYPT32, _DPAPI_KERNEL32
    with _DPAPI_INIT_LOCK:
        if _DPAPI_CRYPT32 is None or _DPAPI_KERNEL32 is None:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),
                ctypes.c_wchar_p,
                ctypes.POINTER(_DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.wintypes.DWORD,
                ctypes.POINTER(_DATA_BLOB),
            ]
            crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL

            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),
                ctypes.POINTER(ctypes.c_wchar_p),
                ctypes.POINTER(_DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.wintypes.DWORD,
                ctypes.POINTER(_DATA_BLOB),
            ]
            crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL

            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            _DPAPI_CRYPT32 = crypt32
            _DPAPI_KERNEL32 = kernel32
    return _DPAPI_CRYPT32, _DPAPI_KERNEL32


def protect_secret(plaintext: str) -> str:
    """
    Encrypt a secret string using Windows Current-User DPAPI (`CryptProtectData`).
    Returns `"dpapi:<base64>"` on Windows, `""` for empty strings, and is idempotent
    if given an already `"dpapi:..."` value.
    """
    if not isinstance(plaintext, str) or not plaintext:
        return ""
    if plaintext.startswith(DPAPI_PREFIX):
        return plaintext
    if os.name != "nt":
        return plaintext

    crypt32, kernel32 = _configure_dpapi()
    raw = plaintext.encode("utf-8")
    buf = ctypes.create_string_buffer(raw, len(raw))
    in_blob = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    try:
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "VoiceCompanionSecret",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok or not out_blob.pbData or out_blob.cbData == 0:
            raise OSError("Windows DPAPI CryptProtectData failed")
        ciphertext = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return DPAPI_PREFIX + base64.b64encode(ciphertext).decode("ascii")
    finally:
        if out_blob.pbData:
            if out_blob.cbData:
                ctypes.memset(out_blob.pbData, 0, out_blob.cbData)
            kernel32.LocalFree(out_blob.pbData)
        ctypes.memset(ctypes.addressof(buf), 0, len(raw))


def unprotect_secret(value: str) -> str:
    """
    Decrypt a `"dpapi:<base64>"` secret using Windows Current-User DPAPI.
    Transparently passes through legacy plaintext values that do not start with `"dpapi:"`.
    Returns `""` safely if a DPAPI blob is corrupted or belongs to another Windows account.
    """
    if not isinstance(value, str) or not value:
        return ""
    if not value.startswith(DPAPI_PREFIX):
        return value
    b64_part = value[len(DPAPI_PREFIX) :].strip()
    if not b64_part or os.name != "nt":
        return ""
    try:
        encrypted = base64.b64decode(b64_part, validate=True)
    except Exception:
        return ""
    if not encrypted:
        return ""

    crypt32, kernel32 = _configure_dpapi()
    buf = ctypes.create_string_buffer(encrypted, len(encrypted))
    in_blob = _DATA_BLOB(len(encrypted), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    try:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            return ""
        if not out_blob.pbData or out_blob.cbData == 0:
            return ""
        raw_plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        try:
            return raw_plain.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    except Exception:
        return ""
    finally:
        if out_blob.pbData:
            if out_blob.cbData:
                ctypes.memset(out_blob.pbData, 0, out_blob.cbData)
            kernel32.LocalFree(out_blob.pbData)
        ctypes.memset(ctypes.addressof(buf), 0, len(encrypted))


def redact_secrets(text: str, extra_secrets: tuple[str, ...] | Iterable[str] = ()) -> str:
    """
    Scrub API keys, authorization tokens, header/query secrets, and DPAPI blobs
    from log lines, exception messages, and UI error dialogs.
    """
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if not s:
        return ""

    # 1. Scrub caller-supplied runtime secrets first (longest-first to avoid partial leaks)
    explicit: set[str] = set()
    for sec in extra_secrets or ():
        if not sec:
            continue
        sec_s = str(sec).strip()
        if len(sec_s) >= 4:
            explicit.add(sec_s)
            quoted = urllib.parse.quote(sec_s, safe="")
            if quoted != sec_s and len(quoted) >= 6:
                explicit.add(quoted)
    for sec_s in sorted(explicit, key=len, reverse=True):
        if sec_s in s:
            s = s.replace(sec_s, "[REDACTED]")

    # 2. Scrub structured config/header/query/token patterns
    s = _SECRET_JSON_KV_RE.sub(r"\1[REDACTED]", s)
    s = _DPAPI_BLOB_RE.sub("[REDACTED_DPAPI]", s)
    s = _AIZA_KEY_RE.sub("[REDACTED]", s)
    s = _SK_KEY_RE.sub("[REDACTED]", s)
    s = _BEARER_RE.sub(r"\1[REDACTED]", s)
    s = _GOOG_HDR_RE.sub(r"\1[REDACTED]", s)
    s = _QUERY_KEY_RE.sub(r"\1[REDACTED]", s)
    s = _BARE_KEY_KV_RE.sub(r"\1[REDACTED]", s)
    return s


def _get_current_user_sid() -> str:
    """Return the current Windows user's SID string (e.g. `S-1-5-21-...`), or `""` on failure."""
    global _CACHED_USER_SID, _SID_ADVAPI32, _SID_KERNEL32
    if _CACHED_USER_SID is not None:
        return _CACHED_USER_SID
    with _SID_INIT_LOCK:
        if _CACHED_USER_SID is not None:
            return _CACHED_USER_SID
        if os.name != "nt":
            _CACHED_USER_SID = ""
            return ""
        try:
            if _SID_ADVAPI32 is None or _SID_KERNEL32 is None:
                advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.GetCurrentProcess.argtypes = []
                kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
                advapi32.OpenProcessToken.argtypes = [
                    ctypes.wintypes.HANDLE,
                    ctypes.wintypes.DWORD,
                    ctypes.POINTER(ctypes.wintypes.HANDLE),
                ]
                advapi32.OpenProcessToken.restype = ctypes.wintypes.BOOL
                advapi32.GetTokenInformation.argtypes = [
                    ctypes.wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    ctypes.wintypes.DWORD,
                    ctypes.POINTER(ctypes.wintypes.DWORD),
                ]
                advapi32.GetTokenInformation.restype = ctypes.wintypes.BOOL
                advapi32.ConvertSidToStringSidW.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                ]
                advapi32.ConvertSidToStringSidW.restype = ctypes.wintypes.BOOL
                kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
                kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
                kernel32.LocalFree.argtypes = [ctypes.c_void_p]
                kernel32.LocalFree.restype = ctypes.c_void_p
                _SID_ADVAPI32 = advapi32
                _SID_KERNEL32 = kernel32
            else:
                advapi32 = _SID_ADVAPI32
                kernel32 = _SID_KERNEL32

            TOKEN_QUERY = 0x0008
            TokenUser = 1

            h_tok = ctypes.wintypes.HANDLE()
            if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(h_tok)):
                _CACHED_USER_SID = ""
                return ""
            try:
                needed = ctypes.wintypes.DWORD(0)
                advapi32.GetTokenInformation(h_tok, TokenUser, None, 0, ctypes.byref(needed))
                if needed.value == 0:
                    _CACHED_USER_SID = ""
                    return ""
                buf = ctypes.create_string_buffer(needed.value)
                if not advapi32.GetTokenInformation(h_tok, TokenUser, buf, needed.value, ctypes.byref(needed)):
                    _CACHED_USER_SID = ""
                    return ""
                tu = ctypes.cast(buf, ctypes.POINTER(_TOKEN_USER)).contents
                p_str = ctypes.c_void_p()
                if not advapi32.ConvertSidToStringSidW(tu.User.Sid, ctypes.byref(p_str)):
                    _CACHED_USER_SID = ""
                    return ""
                try:
                    sid_str = ctypes.wstring_at(p_str.value)
                finally:
                    if p_str.value:
                        kernel32.LocalFree(p_str.value)
            finally:
                kernel32.CloseHandle(h_tok)

            _CACHED_USER_SID = sid_str if _SID_RE.match(sid_str or "") else ""
            return _CACHED_USER_SID
        except Exception:
            _CACHED_USER_SID = ""
            return ""


def harden_user_data_acl(path: Path) -> None:
    """
    Best-effort restrict a user data directory or file to the current user (+ SYSTEM on Windows).
    Throttled once per resolved path per process so startup and temp-dir tests stay fast.
    """
    try:
        raw_path = Path(path)
        if "\x00" in str(raw_path) or raw_path.is_symlink():
            return
        target = raw_path.resolve()
        if not target.exists():
            return

        cache_key = os.path.normcase(str(target))
        with _ACL_LOCK:
            if cache_key in _HARDENED_ACL_PATHS:
                return
            _HARDENED_ACL_PATHS.add(cache_key)

        if os.name != "nt":
            os.chmod(target, 0o700 if target.is_dir() else 0o600)
            return

        sid = _get_current_user_sid()
        if not sid or not _SID_RE.match(sid):
            return
        sys_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows")
        icacls_exe = sys_root / "System32" / "icacls.exe"
        if not icacls_exe.is_file():
            return

        perm = "(OI)(CI)F" if target.is_dir() else "F"
        argv = [
            str(icacls_exe),
            str(target),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:{perm}",
            "/grant:r",
            f"*S-1-5-18:{perm}",
        ]
        subprocess.run(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            timeout=5,
            check=False,
        )
    except Exception:
        pass


def _read_bounded_text(path: Path, max_bytes: int) -> str:
    """Read UTF-8 (with optional BOM) text from a regular non-symlink file with a strict byte-size ceiling."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Refusing to read non-regular or symlinked file: {path.name}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"File exceeds size limit ({max_bytes} bytes): {path.name}")
    with open(path, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"File exceeds size limit ({max_bytes} bytes): {path.name}")
    return raw.decode("utf-8-sig")


def _prepare_config_for_disk(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a deep snapshot of `cfg` with secret fields DPAPI-encrypted (never mutating caller's dict)."""
    disk_cfg = deepcopy(dict(cfg))
    for key in SECRET_CONFIG_KEYS:
        raw_val = disk_cfg.get(key)
        if raw_val is None or isinstance(raw_val, bool):
            val = ""
        elif isinstance(raw_val, (dict, list)):
            val = json.dumps(raw_val, ensure_ascii=False, separators=(",", ":"))
        else:
            val = str(raw_val).strip()
        disk_cfg[key] = protect_secret(val) if val else ""
    return disk_cfg


def _atomic_write_restricted_text(target: Path, payload: str, *, prefix: str = ".cfg_") -> Path:
    """Atomically write `payload` into `target` with restrictive permissions and guaranteed `.tmp` cleanup."""
    if "\x00" in str(target):
        raise ValueError("Invalid path containing NUL byte")
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite symlinked file: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    harden_user_data_acl(target.parent)

    fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(target.parent))
    fd_closed = False
    try:
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd_closed = True
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        last_perm_err: Exception | None = None
        for attempt in range(6):
            try:
                os.replace(tmp_name, target)
                last_perm_err = None
                break
            except PermissionError as exc:
                last_perm_err = exc
                time.sleep(0.04 * (attempt + 1))
        if last_perm_err is not None:
            raise last_perm_err
    except Exception:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return target

DEFAULT_USER_CONFIG: dict[str, Any] = {
    "emotion_engine_version": 4,
    "selected_character_id": "Aru",
    "persona_prompt_variant": "official",
    "fish_api_key": "",
    "fish_mcp_oauth_data": "",
    "fish_mcp_fallback_enabled": True,
    "fish_mcp_primary_mode": False,
    "fish_credit_saver_mode": True,
    "fish_tts_cache_enabled": True,
    "colab_s2pro_enabled": False,
    "colab_s2pro_url": "",
    "colab_s2pro_api_key": "",
    "colab_s2pro_drive_folder_id": "",
    "colab_s2pro_notebook_id": "",
    "colab_s2pro_endpoint_file": "",
    "fish_reference_id": "",
    "fish_voice_title": "직접 입력 / 선택됨",
    "saved_voices": {},
    "saved_voices_kr": {},
    "steam_game_dir": "",
    "auto_voice_visibility": "private",  # "private" | "unlist" | "public"
    "auto_voice_quality_profile": "plus200",  # "plus200" | "pro85" | "standard45"
    "reply_language": "ja",  # "ja" (일본어 답변 · 기본) or "ko" (한국어 답변)
    "fish_model_header": "s2.1-pro-free",
    "stt_language": "auto",
    "whisper_model": "auto",
    "streaming_speed_mode": "expressive",
    "auto_emotion_directing": True,
    "expression_boost_offset": 0.0,
    "latency_mode": "balanced",
    "pause_threshold_sec": 0.33,
    "output_volume": 1.0,
    "proactive_prompt_append": True,
    "proactive_enable": True,
    "proactive_timeout_sec": 18.0,
    "proactive_trigger_volume": 1.0,
    "proactive_trigger_device": "",
    "primary_output_device": "",
    # --- Mode C & Distributable PC Mic Companion Settings ---
    "bridge_mode": "pc_mic",  # "pc_mic" (Mode 1) or "gemini_live_audio" (Mode 2)
    "call_bridge_mode": "mode1",  # "mode1" (PC Mic+AI) or "mode2" (VB-Cable)
    "ai_provider": "gemini",  # "gemini" | "claude" | "chatgpt"
    "gemini_backend": "browser",  # "browser" (웹 로그인) or "api" (공식 API)
    "gemini_browser_model": "hydrogen",  # auto / hydrogen / neon_4 / carbon_4 / web_dyn:*
    "gemini_thinking_level": "low",  # "low" (낮음) / "medium" (보통) / "high" (높음) / "xhigh" (최고)
    "gemini_api_key": "",
    "gemini_api_model": "gemini-3.5-flash-lite",
    "api_models": [],
    "web_models": [],  # Account-synced Gemini web [code, label] list
    "web_models_schema_version": 2,  # v2: Gemini 4 Neon / Auto / Hydrogen / Carbon lineup
    "web_account_tier": "",  # "Free" | "Pro" | "Ultra" | ""
    # Claude (Anthropic) settings
    "claude_api_key": "",
    "claude_api_model": "claude-sonnet-5-5",
    "claude_browser_model": "default",
    "claude_api_models": [],
    "claude_web_models": [],
    "claude_web_account_tier": "",
    # ChatGPT (OpenAI) settings
    "openai_api_key": "",
    "openai_api_model": "gpt-5.4-mini",
    "chatgpt_browser_model": "default",
    "openai_api_models": [],
    "chatgpt_web_models": [],
    "chatgpt_web_account_tier": "",
    "use_custom_persona": False,
    "custom_persona_prompt": (
        "너는 다정하고 재치 있는 AI 보이스 메이트야. "
        "친구처럼 편하게 대화하고, 실제로 옆에서 말하는 것처럼 짧고 생동감 있는 구어체로 대답해 줘."
    ),
    "mic_input_device": "",
    "mic_vad_rms_threshold": 0.010,
    "mic_silence_end_sec": 0.68,
    "mic_stt_language": "ko_auto",
    "companion_proactive_enable": True,
    "idle_ping_min_sec": 45.0,
    "idle_ping_max_sec": 75.0,
    "afk_max_consecutive_pings": 2,
    "subtitle_mode": "jp_kr",
    "subtitle_font_size": 11,
    "subtitle_engine": "auto_inline",  # "auto_inline", "web:<model>", "api:<model>", "fast_nmt"
    "subtitle_thinking_level": "low",  # "low" (낮음) / "medium" (보통) / "high" (높음) / "xhigh" (최고)
    "jp_beginner_lover_mode": False,
    "temporary_call_mode": False,
}


def get_app_root() -> Path:
    """Return read-only application bundle/project root directory."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parents[2]


def get_exe_dir() -> Path:
    """Return directory containing the running executable or script entry root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return get_app_root()


def get_user_data_dir() -> Path:
    """
    Return the isolated per-user data directory under %LOCALAPPDATA%\\VoiceCompanionData.
    Honors VOICE_COMPANION_DATA_DIR only when explicitly set (for isolated tests).
    """
    override = os.environ.get("VOICE_COMPANION_DATA_DIR", "").strip()
    if override:
        base = Path(override).expanduser().resolve()
    else:
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            base = Path(local_app_data) / USER_DATA_FOLDER_NAME
        else:
            base = Path.home() / "AppData" / "Local" / USER_DATA_FOLDER_NAME
    base.mkdir(parents=True, exist_ok=True)
    harden_user_data_acl(base)
    return base


def get_config_path() -> Path:
    return get_user_data_dir() / "config.json"


def get_browser_profile_dir() -> Path:
    p = get_user_data_dir() / "BrowserProfile"
    p.mkdir(parents=True, exist_ok=True)
    harden_user_data_acl(p)
    return p


def get_logs_dir() -> Path:
    p = get_user_data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_cache_dir() -> Path:
    p = get_user_data_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_trigger_cache_dir() -> Path:
    p = get_cache_dir() / "proactive_triggers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_tts_cache_dir() -> Path:
    p = get_cache_dir() / "tts_audio"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_user_models_dir() -> Path:
    """
    Return the directory storing extracted studio voice WAVs/transcripts.
    Precedence:
      1. Explicit `VOICE_COMPANION_MODELS_DIR` env override
      2. Isolated test override (`VOICE_COMPANION_DATA_DIR/models`)
      3. Local workspace `data/models` when present (keeps large audio packs off C: drive)
      4. Standard per-user `%LOCALAPPDATA%\\VoiceCompanionData\\models`
    """
    models_override = os.environ.get("VOICE_COMPANION_MODELS_DIR", "").strip()
    if models_override:
        p = Path(models_override).expanduser().resolve()
    elif os.environ.get("VOICE_COMPANION_DATA_DIR", "").strip():
        p = get_user_data_dir() / "models"
    else:
        exe_models = get_exe_dir() / "data" / "models"
        local_models = get_app_root() / "data" / "models"
        if exe_models.is_dir() and not exe_models.is_symlink():
            p = exe_models
        elif local_models != exe_models and local_models.is_dir() and not local_models.is_symlink():
            p = local_models
        else:
            p = get_user_data_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_memory_dir() -> Path:
    """Return isolated persistent MomoTalk conversation history & memory directory."""
    p = get_user_data_dir() / "memory"
    p.mkdir(parents=True, exist_ok=True)
    harden_user_data_acl(p)
    return p


def get_selectors_path(app_root: Path | None = None) -> Path:
    """Prefer user override in %LOCALAPPDATA%\\VoiceCompanionData\\selectors.json, else bundled config/selectors.json."""
    user_sel = get_user_data_dir() / "selectors.json"
    if user_sel.is_file() and not user_sel.is_symlink():
        return user_sel
    root = app_root or get_app_root()
    return root / "config" / "selectors.json"


def ensure_user_selectors_copy(app_root: Path | None = None) -> Path:
    """Copy bundled config/selectors.json into user data directory if not already copied."""
    user_dir = get_user_data_dir().resolve()
    user_sel = user_dir / "selectors.json"
    if user_sel.is_symlink():
        raise ValueError("Refusing to use symlinked selectors.json")
    if not user_sel.exists():
        root = app_root or get_app_root()
        bundled = root / "config" / "selectors.json"
        if bundled.is_file() and not bundled.is_symlink():
            shutil.copy2(bundled, user_sel)
    resolved = user_sel.resolve()
    if resolved.parent != user_dir or resolved.name.lower() != "selectors.json":
        raise ValueError("Invalid selectors.json target path")
    return resolved


def get_bundled_default_voices_path(app_root: Path | None = None) -> Path:
    """Return path to optional bundled public Fish Audio voice presets (`config/default_voices.json`)."""
    root = app_root or get_app_root()
    return root / "config" / "default_voices.json"


def load_bundled_default_voices(app_root: Path | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Load optional bundled `(saved_voices, saved_voices_kr)` presets shipped with distribution builds."""
    preset_path = get_bundled_default_voices_path(app_root)
    if not preset_path.is_file() or preset_path.is_symlink():
        return {}, {}
    try:
        raw = json.loads(_read_bounded_text(preset_path, MAX_DEFAULT_VOICES_BYTES))
        if not isinstance(raw, dict):
            return {}, {}
        jp_raw = raw.get("saved_voices")
        kr_raw = raw.get("saved_voices_kr")
        jp = (
            {
                str(k).strip(): str(v).strip()
                for k, v in jp_raw.items()
                if str(k).strip() and str(v).strip()
            }
            if isinstance(jp_raw, dict)
            else {}
        )
        kr = (
            {
                str(k).strip(): str(v).strip()
                for k, v in kr_raw.items()
                if str(k).strip() and str(v).strip()
            }
            if isinstance(kr_raw, dict)
            else {}
        )
        return jp, kr
    except Exception:
        return {}, {}


def _normalize_provider_key(provider: Any) -> str:
    raw = str(provider or "gemini").strip().lower()
    if raw in ("claude", "anthropic"):
        return "claude"
    if raw in ("chatgpt", "openai", "gpt"):
        return "chatgpt"
    return "gemini"


def _sanitize_stored_api_model(raw: Any, provider: str, default: str) -> str:
    """
    Validate or self-heal a persisted API model identifier without importing `gemini_connector`.
    Accepts clean IDs, strips a safe `models/` prefix, or extracts a clean provider-prefixed ID
    from old/new UI display labels only when free of dangerous URL/path characters.
    """
    if not isinstance(raw, str):
        return default
    s = raw.strip()
    if not s:
        return default
    if s.lower().startswith("models/"):
        tail = s[7:].strip()
        if tail and ".." not in tail and _SAFE_API_MODEL_ID_RE.fullmatch(tail):
            return tail
        return default
    if ".." not in s and _SAFE_API_MODEL_ID_RE.fullmatch(s):
        return s
    if _UNSAFE_API_MODEL_CHARS_RE.search(s):
        return default
    p = _normalize_provider_key(provider)
    pat = _PROVIDER_API_MODEL_TOKEN_RES.get(p)
    if pat is not None:
        m = pat.search(s)
        if m:
            cand = m.group(1).strip()
            if cand and ".." not in cand and _SAFE_API_MODEL_ID_RE.fullmatch(cand):
                return cand
    return default


def _sanitize_stored_api_model_list(raw_list: Any, provider: str) -> list[str]:
    """Sanitize and deduplicate a stored list of API model IDs while preserving order."""
    if not isinstance(raw_list, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_list:
        clean = _sanitize_stored_api_model(item, provider, default="")
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _normalize_loaded_config(raw_cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate, coerce, clamp, decrypt, and migrate loaded config via `settings_recovery.sanitize_user_config`."""
    from src.utils.settings_recovery import sanitize_user_config

    cfg, _repaired = sanitize_user_config(raw_cfg)
    return cfg


def save_user_config(cfg: dict[str, Any]) -> Path:
    """
    Atomically persist user config inside %LOCALAPPDATA%\\VoiceCompanionData\\config.json.
    Thread-safe via `_CONFIG_IO_LOCK`; encrypts API keys at rest with Windows DPAPI
    on a deep snapshot without mutating the caller's in-memory dict.
    """
    with _CONFIG_IO_LOCK:
        target = get_config_path()
        disk_cfg = _prepare_config_for_disk(cfg)
        payload = json.dumps(disk_cfg, ensure_ascii=False, indent=2, allow_nan=False)
        return _atomic_write_restricted_text(target, payload, prefix=".cfg_")


def load_user_config(app_root: Path | None = None) -> dict[str, Any]:
    """Load config from %LOCALAPPDATA%\\VoiceCompanionData\\config.json with defaults, DPAPI decrypt & presets."""
    from src.utils.settings_recovery import load_user_config_with_report

    return load_user_config_with_report(app_root=app_root).config


def migrate_legacy_workspace_data(app_root: Path | None = None) -> dict[str, Any]:
    """
    One-time migration of legacy workspace-local config/logs into %LOCALAPPDATA%\\VoiceCompanionData\\.
    Removes legacy secret-bearing files from the project/exe directory so distribution builds stay clean.
    """
    root = app_root or get_app_root()
    user_dir = get_user_data_dir()
    logs_dir = get_logs_dir()
    migrated: dict[str, Any] = {"migrated_config": False, "moved_logs": []}

    legacy_cfg = root / "config" / "fish_bridge_config.json"
    if legacy_cfg.exists() and not legacy_cfg.is_symlink():
        try:
            raw_text = _read_bounded_text(legacy_cfg, MAX_CONFIG_FILE_BYTES)
            legacy_data = json.loads(raw_text)
            backup_path = user_dir / "legacy_fish_bridge_config.backup.json"
            if not backup_path.exists():
                if isinstance(legacy_data, dict):
                    backup_payload = json.dumps(_prepare_config_for_disk(legacy_data), ensure_ascii=False, indent=2)
                else:
                    backup_payload = redact_secrets(raw_text)
                _atomic_write_restricted_text(backup_path, backup_payload, prefix=".bak_")
            if isinstance(legacy_data, dict) and not get_config_path().exists():
                normalized = _normalize_loaded_config(legacy_data)
                save_user_config(normalized)
                migrated["migrated_config"] = True
            legacy_cfg.unlink()
        except Exception:
            pass

    legacy_data_dir = root / "data"
    if legacy_data_dir.exists():
        for log_name in ("bridge_runtime.log", "bridge_crash.log", "studio_crash.log"):
            src_log = legacy_data_dir / log_name
            if src_log.exists() and not src_log.is_symlink():
                try:
                    dst_log = logs_dir / log_name
                    content = redact_secrets(src_log.read_text(encoding="utf-8", errors="replace"))
                    if dst_log.exists():
                        with open(dst_log, "a", encoding="utf-8") as fh:
                            fh.write("\n" + content)
                    else:
                        _atomic_write_restricted_text(dst_log, content, prefix=".log_")
                    src_log.unlink()
                    migrated["moved_logs"].append(log_name)
                except Exception:
                    pass

        fw_status = legacy_data_dir / "firewall_fix_status.json"
        if fw_status.exists() and not fw_status.is_symlink():
            try:
                dst_fw = get_cache_dir() / "firewall_fix_status.json"
                if not dst_fw.exists():
                    shutil.copy2(fw_status, dst_fw)
                fw_status.unlink()
            except Exception:
                pass

    return migrated


def scan_forbidden_personal_artifacts(root: Path) -> list[str]:
    """
    Scan an application/release root for personal configs, browser profiles, or runtime logs
    that must NEVER be packaged or shipped to third parties.
    """
    findings: list[str] = []
    skip_dirs = {".venv", ".venv_rvc", ".git", "__pycache__", "build", "dist", "vcclient"}
    forbidden_dir_names = {"EBWebView", "User Data", "BrowserProfile", "VoiceCompanionData"}

    legacy_cfg = root / "config" / "fish_bridge_config.json"
    if legacy_cfg.exists():
        findings.append(str(legacy_cfg.relative_to(root)))

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        # Prune skipped directories & .tmp.* directories
        dirnames[:] = [
            d for d in dirnames
            if d not in skip_dirs and not d.startswith(".tmp.")
        ]
        for d in list(dirnames):
            if d in forbidden_dir_names:
                findings.append(str((rel_dir / d).as_posix()))
        for fn in filenames:
            low = fn.lower()
            if low.endswith(".log") or low == ".env" or low.startswith(".env."):
                findings.append(str((rel_dir / fn).as_posix()))
            if rel_dir.as_posix().startswith("data") and low in ("firewall_fix_status.json",):
                findings.append(str((rel_dir / fn).as_posix()))
    return sorted(set(findings))
