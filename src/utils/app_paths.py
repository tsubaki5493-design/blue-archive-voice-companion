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
SECRET_CONFIG_KEYS: tuple[str, ...] = ("fish_api_key", "gemini_api_key")
MAX_CONFIG_FILE_BYTES = 4 * 1024 * 1024
MAX_DEFAULT_VOICES_BYTES = 2 * 1024 * 1024

_SID_RE = re.compile(r"^S-1-[0-9-]+$")
_ACL_LOCK = threading.Lock()
_HARDENED_ACL_PATHS: set[str] = set()
_CACHED_USER_SID: str | None = None
_DPAPI_INIT_LOCK = threading.Lock()
_DPAPI_CONFIGURED = False

# Secret redaction patterns (kept idempotent via negative lookahead on [REDACTED...])
_SECRET_JSON_KV_RE = re.compile(
    r"""(?i)(['"]?(?:fish_api_key|gemini_api_key)['"]?\s*[:=]\s*['"]?)(?!\[REDACTED\b)([^'"\s,}\]]+)"""
)
_DPAPI_BLOB_RE = re.compile(r"dpapi:[A-Za-z0-9+/=_\-]{4,}")
_AIZA_KEY_RE = re.compile(r"AIza[0-9A-Za-z\-_]{12,}")
_SK_KEY_RE = re.compile(r"\bsk-[0-9A-Za-z_\-]{10,}\b")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)(?!\[REDACTED\b)([^\s'\"`,;}\]]{6,})")
_GOOG_HDR_RE = re.compile(
    r"""(?i)(['"]?x-goog-api-key['"]?\s*[:=]\s*['"]?)(?!\[REDACTED\b)([^'"\s,;&}\]]+)"""
)
_QUERY_KEY_RE = re.compile(r"(?i)([?&](?:api_?)?(?:key)=)(?!\[REDACTED\b)([^&\s'\"<>}\]]+)")
_BARE_KEY_KV_RE = re.compile(r"(?i)((?:^|[\s,(])(?:api[_-]?)?(?:key)=)(?!\[REDACTED\b)([^&\s'\"<>}\],)]{6,})")


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
    """Configure ctypes signatures for Windows crypt32 DPAPI & kernel32.LocalFree."""
    global _DPAPI_CONFIGURED
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not _DPAPI_CONFIGURED:
        with _DPAPI_INIT_LOCK:
            if not _DPAPI_CONFIGURED:
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
                _DPAPI_CONFIGURED = True
    return crypt32, kernel32


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
    global _CACHED_USER_SID
    if _CACHED_USER_SID is not None:
        return _CACHED_USER_SID
    if os.name != "nt":
        _CACHED_USER_SID = ""
        return ""
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        TOKEN_QUERY = 0x0008
        TokenUser = 1

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
    """Read UTF-8 text from a regular non-symlink file with a strict byte-size ceiling."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Refusing to read non-regular or symlinked file: {path.name}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"File exceeds size limit ({max_bytes} bytes): {path.name}")
    with open(path, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"File exceeds size limit ({max_bytes} bytes): {path.name}")
    return raw.decode("utf-8")


def _prepare_config_for_disk(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of `cfg` with secret fields DPAPI-encrypted (never mutating caller's dict)."""
    disk_cfg = dict(cfg)
    for key in SECRET_CONFIG_KEYS:
        val = str(disk_cfg.get(key) or "").strip()
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
    "fish_reference_id": "",
    "fish_voice_title": "직접 입력 / 선택됨",
    "saved_voices": {},
    "saved_voices_kr": {},
    "steam_game_dir": "",
    "auto_voice_visibility": "private",  # "private" | "unlist" | "public"
    "auto_voice_quality_profile": "pro85",  # "pro85" | "standard45"
    "reply_language": "ja",  # "ja" (일본어 답변 · 기본) or "ko" (한국어 답변)
    "fish_model_header": "s2.1-pro-free",
    "stt_language": "auto",
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
    "call_bridge_mode": "mode1",  # "mode1" (PC Mic+Gemini) or "mode2" (VB-Cable)
    "gemini_backend": "browser",  # "browser" (Mode B) or "api" (Mode A)
    "gemini_browser_model": "hydrogen",  # auto / hydrogen / neon_4 / carbon_4 / web_dyn:*
    "gemini_thinking_level": "low",  # "low" (낮음) / "medium" (보통) / "high" (높음) / "xhigh" (최고)
    "gemini_api_key": "",
    "gemini_api_model": "gemini-3.5-flash-lite",
    "api_models": [],
    "web_models": [],  # Account-synced Gemini web [code, label] list
    "web_models_schema_version": 2,  # v2: Gemini 4 Neon / Auto / Hydrogen / Carbon lineup
    "web_account_tier": "",  # "Free" | "Pro" | "Ultra" | ""
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


def get_user_models_dir() -> Path:
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


def _normalize_loaded_config(raw_cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = deepcopy(DEFAULT_USER_CONFIG)
    prev_ver = int(raw_cfg.get("emotion_engine_version", 0) or 0)
    cfg.update(raw_cfg)
    for secret_key in SECRET_CONFIG_KEYS:
        raw_secret = str(cfg.get(secret_key, "") or "").strip()
        if raw_secret.startswith(DPAPI_PREFIX):
            cfg[secret_key] = unprotect_secret(raw_secret)
        else:
            cfg[secret_key] = raw_secret
    if not isinstance(cfg.get("saved_voices"), dict):
        cfg["saved_voices"] = {}
    if not isinstance(cfg.get("saved_voices_kr"), dict):
        cfg["saved_voices_kr"] = {}
    rlang = str(cfg.get("reply_language", "ja") or "ja").strip().lower()
    cfg["reply_language"] = "ko" if rlang == "ko" else "ja"
    cfg["steam_game_dir"] = str(cfg.get("steam_game_dir", "") or "").strip()
    auto_vis = str(cfg.get("auto_voice_visibility", "private") or "private").strip().lower()
    cfg["auto_voice_visibility"] = auto_vis if auto_vis in ("private", "unlist", "public") else "private"
    auto_prof = str(cfg.get("auto_voice_quality_profile", "pro85") or "pro85").strip().lower()
    cfg["auto_voice_quality_profile"] = auto_prof if auto_prof in ("pro85", "standard45") else "pro85"
    if prev_ver < 4:
        cfg["streaming_speed_mode"] = "expressive"
        cfg["emotion_engine_version"] = 4
    elif cfg.get("streaming_speed_mode") == "balanced":
        cfg["streaming_speed_mode"] = "expressive"
    if cfg.get("fish_reference_id") and "Aru" not in cfg["saved_voices"]:
        cfg["saved_voices"]["Aru"] = cfg["fish_reference_id"]

    # Clamp proactive range safely
    min_s = max(5.0, float(cfg.get("idle_ping_min_sec", 45.0) or 45.0))
    max_s = max(min_s, float(cfg.get("idle_ping_max_sec", 75.0) or 75.0))
    cfg["idle_ping_min_sec"] = min_s
    cfg["idle_ping_max_sec"] = max_s
    cfg["afk_max_consecutive_pings"] = max(1, int(cfg.get("afk_max_consecutive_pings", 2) or 2))

    # Migrate legacy combined or retired browser model / subtitle engine keys seamlessly
    valid_think_levels = ("low", "medium", "high", "xhigh")
    legacy_bmodel_map: dict[str, tuple[str, str | None]] = {
        "hydrogen_low": ("hydrogen", "low"),
        "hydrogen_medium": ("hydrogen", "medium"),
        "hydrogen_high": ("hydrogen", "high"),
        "flash_lite": ("auto", None),
        "pro_31": ("neon_4", None),
        "argon_4": ("neon_4", None),
    }
    raw_bmodel = str(cfg.get("gemini_browser_model", "hydrogen") or "hydrogen")
    if raw_bmodel in legacy_bmodel_map:
        base_m, inf_lvl = legacy_bmodel_map[raw_bmodel]
        cfg["gemini_browser_model"] = base_m
        if inf_lvl and "gemini_thinking_level" not in raw_cfg:
            cfg["gemini_thinking_level"] = inf_lvl
    if str(cfg.get("gemini_thinking_level", "low")) not in valid_think_levels:
        cfg["gemini_thinking_level"] = "low"

    legacy_sub_map: dict[str, tuple[str, str | None]] = {
        "auto_llm": ("auto_inline", None),
        "browser_hydrogen_low": ("web:hydrogen", "low"),
        "browser_hydrogen_high": ("web:hydrogen", "high"),
        "browser_flash_lite": ("web:auto", "low"),
        "web:flash_lite": ("web:auto", None),
        "web:pro_31": ("web:neon_4", None),
        "web:argon_4": ("web:neon_4", None),
    }
    raw_sub = str(cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
    if raw_sub in legacy_sub_map:
        new_sub, inf_lvl = legacy_sub_map[raw_sub]
        cfg["subtitle_engine"] = new_sub
        if inf_lvl and "subtitle_thinking_level" not in raw_cfg:
            cfg["subtitle_thinking_level"] = inf_lvl
    if str(cfg.get("subtitle_thinking_level", "low")) not in valid_think_levels:
        cfg["subtitle_thinking_level"] = "low"

    raw_api_models = cfg.get("api_models", [])
    if isinstance(raw_api_models, list):
        cfg["api_models"] = [str(m).strip() for m in raw_api_models if str(m).strip()]
    else:
        cfg["api_models"] = []

    raw_web_schema = int(raw_cfg.get("web_models_schema_version", 1) or 1)
    raw_web_models = cfg.get("web_models", [])
    clean_web_models: list[list[str]] = []
    if isinstance(raw_web_models, list):
        seen_wm: set[str] = set()
        for entry in raw_web_models:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                code = str(entry[0] or "").strip()
                label = str(entry[1] or "").strip()
                if code and label and code not in seen_wm:
                    seen_wm.add(code)
                    clean_web_models.append([code, label])
    retired_web_codes = {"flash_lite", "pro_31", "argon_4"}
    if raw_web_schema < 2 and any(code in retired_web_codes for code, _ in clean_web_models):
        # Clear stale pre-Nov-2026 cached web picker so updated Gemini 4 Neon defaults + auto-sync take over
        clean_web_models = []
    cfg["web_models"] = clean_web_models
    cfg["web_models_schema_version"] = 2
    tier_val = str(cfg.get("web_account_tier", "") or "").strip()
    cfg["web_account_tier"] = tier_val if tier_val in ("Free", "Pro", "Ultra") else ""

    raw_bm = raw_cfg.get("bridge_mode")
    raw_cbm = raw_cfg.get("call_bridge_mode")
    if raw_bm is None and raw_cbm is not None:
        cbm = "mode2" if str(raw_cbm).strip() == "mode2" else "mode1"
        cfg["call_bridge_mode"] = cbm
        cfg["bridge_mode"] = "gemini_live_audio" if cbm == "mode2" else "pc_mic"
    else:
        bm = (
            "gemini_live_audio"
            if str(cfg.get("bridge_mode", "pc_mic")).strip() == "gemini_live_audio"
            else "pc_mic"
        )
        cfg["bridge_mode"] = bm
        cfg["call_bridge_mode"] = "mode2" if bm == "gemini_live_audio" else "mode1"
    cfg["jp_beginner_lover_mode"] = bool(cfg.get("jp_beginner_lover_mode", False))
    cfg["temporary_call_mode"] = bool(cfg.get("temporary_call_mode", False))
    return cfg


def save_user_config(cfg: dict[str, Any]) -> Path:
    """
    Atomically persist user config inside %LOCALAPPDATA%\\VoiceCompanionData\\config.json.
    Encrypts API keys at rest with Windows DPAPI without mutating the caller's in-memory dict.
    """
    target = get_config_path()
    disk_cfg = _prepare_config_for_disk(cfg)
    payload = json.dumps(disk_cfg, ensure_ascii=False, indent=2)
    return _atomic_write_restricted_text(target, payload, prefix=".cfg_")


def load_user_config(app_root: Path | None = None) -> dict[str, Any]:
    """Load config from %LOCALAPPDATA%\\VoiceCompanionData\\config.json with defaults, DPAPI decrypt & presets."""
    cfg_path = get_config_path()
    loaded: dict[str, Any] = {}
    if cfg_path.exists() and not cfg_path.is_symlink():
        try:
            saved = json.loads(_read_bounded_text(cfg_path, MAX_CONFIG_FILE_BYTES))
            if isinstance(saved, dict):
                loaded = saved
        except Exception:
            loaded = {}

    # Best-effort transparent upgrade: encrypt any legacy plaintext API keys found on disk
    if loaded and os.name == "nt":
        has_legacy_plaintext_secret = any(
            bool(str(loaded.get(k) or "").strip()) and not str(loaded.get(k) or "").strip().startswith(DPAPI_PREFIX)
            for k in SECRET_CONFIG_KEYS
        )
        if has_legacy_plaintext_secret:
            try:
                migrated_disk = _prepare_config_for_disk(loaded)
                _atomic_write_restricted_text(
                    cfg_path,
                    json.dumps(migrated_disk, ensure_ascii=False, indent=2),
                    prefix=".cfg_",
                )
            except Exception:
                pass

    cfg = _normalize_loaded_config(loaded)
    bundled_jp, bundled_kr = load_bundled_default_voices(app_root)
    for cid, vid in bundled_jp.items():
        if not str(cfg["saved_voices"].get(cid) or "").strip():
            cfg["saved_voices"][cid] = vid
    for cid, vid in bundled_kr.items():
        if not str(cfg["saved_voices_kr"].get(cid) or "").strip():
            cfg["saved_voices_kr"][cid] = vid
    return cfg


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
    skip_dirs = {".venv", ".git", "__pycache__", "build", "dist", "vcclient"}
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
