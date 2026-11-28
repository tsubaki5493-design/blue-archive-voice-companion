"""
Defensive Settings Sanitization, Backup & Safe Default Restoration
------------------------------------------------------------------
Pure stdlib + `src.utils.app_paths` helper module (zero imports from
`src.controller.*`, `src.rvc_live.*`, or `tkinter`) so it is safe to invoke
both during live UI operation and during early fatal startup recovery.

Exact Reset Contract ("fish audio api키, 제미니 로그인만 유지하고 다 기본값으로 변경"):
- STRICTLY PRESERVED:
  1. `config.json["fish_api_key"]` (salvaged from live dict, valid config.json,
     or regex+DPAPI fallback on corrupted config.json)
  2. `%LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile\\` (100% untouched —
     holds Edge/Chrome Gemini web login cookies & session)
  3. Non-settings user data (`memory\\`, `models\\`, `models_rvc\\`, `data\\`,
     `gamedata\\`, `logs\\`, `cache\\`, `tts_cache\\`, `rvc_test_outputs\\`,
     `fish_batch_manifest.json`, `saved_voices_pre_batch_backup.json`)
- AUTOMATICALLY BACKED UP (ACL-hardened, DPAPI-safe/redacted, pruned to 10)
  THEN RESET TO DEFAULTS:
  1. `config.json` -> `DEFAULT_USER_CONFIG` + bundled default voices + preserved
     `fish_api_key` (clears `gemini_api_key`, `claude_api_key`, `openai_api_key`,
     `fish_mcp_oauth_data`, restores `ai_provider="gemini"` &
     `gemini_backend="browser"`, resets all non-login Gemini/voice/audio/prompt/
     timer knobs, and strips extra/unknown keys)
  2. User override `%LOCALAPPDATA%\\VoiceCompanionData\\selectors.json` (deleted
     so `get_selectors_path()` cleanly falls back to bundled `config/selectors.json`)
  3. `%LOCALAPPDATA%\\VoiceCompanionData\\rvc_live_config.json` (deleted by path
     only, without importing `src.rvc_live`, so RVC Live resets to v3 defaults
     and clears its separate `gemini_api_key`)
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from src.utils import app_paths as ap

BACKUPS_DIR_NAME = "backups"
DEFAULT_MAX_BACKUPS = 10
MAX_SELECTORS_BACKUP_BYTES = 256 * 1024
MAX_CUSTOM_PERSONA_PROMPT_LEN = 16000
MAX_SINGLE_LINE_SECRET_LEN = 512

VALID_PERSONA_VARIANTS: tuple[str, ...] = (
    "official",
    "fanon_mainstream",
    "fanon_pure_love",
    "fanon_spicy_meme",
    "fanon_optimal_blend",
)
VALID_FISH_MODEL_HEADERS: tuple[str, ...] = ("s2.1-pro-free", "s2.1-pro", "s2-pro", "s1")
VALID_STT_LANGUAGES: tuple[str, ...] = ("auto", "ja", "ko")
VALID_MIC_STT_LANGUAGES: tuple[str, ...] = ("ko_auto", "ja_beginner_bi", "ja", "ko", "auto")
VALID_WHISPER_MODELS: tuple[str, ...] = ("auto", "large-v3-turbo", "medium", "small", "base", "tiny")
VALID_SPEED_MODES: tuple[str, ...] = ("expressive", "ultra", "full_turn")
VALID_LATENCY_MODES: tuple[str, ...] = ("balanced", "normal")
VALID_AI_PROVIDERS: tuple[str, ...] = ("gemini", "claude", "chatgpt")
VALID_GEMINI_BACKENDS: tuple[str, ...] = ("browser", "api")
VALID_THINKING_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
VALID_SUBTITLE_MODES: tuple[str, ...] = ("jp_only", "kr_only", "jp_kr")
VALID_AUTO_VOICE_VISIBILITIES: tuple[str, ...] = ("private", "unlist", "public")
VALID_AUTO_VOICE_PROFILES: tuple[str, ...] = ("plus200", "pro85", "standard45")
VALID_WEB_ACCOUNT_TIERS: tuple[str, ...] = ("", "Free", "Pro", "Ultra")
VALID_GEMINI_BROWSER_MODELS: tuple[str, ...] = ("auto", "hydrogen", "neon_4", "carbon_4")
VALID_CLAUDE_BROWSER_MODELS: tuple[str, ...] = ("default", "haiku", "sonnet", "opus", "fable")
VALID_CHATGPT_BROWSER_MODELS: tuple[str, ...] = ("default", "auto", "mini", "thinking", "pro")

_SAFE_REASON_RE = re.compile(r"[^A-Za-z0-9_-]+")
_FISH_KEY_SALVAGE_RE = re.compile(r'"fish_api_key"\s*:\s*"((?:\\.|[^"\\])*)"')
_MAX_OVERSIZED_SALVAGE_BYTES = 256 * 1024
_MAX_OVERSIZED_BACKUP_HEAD_BYTES = 64 * 1024
_VIRTUAL_LOOPBACK_MIC_TOKENS: tuple[str, ...] = (
    "cable",
    "vb-audio",
    "stereo mix",
    "스테레오 믹스",
)


_SAFE_DRIVE_ID_STRICT_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")
_DRIVE_FOLDER_PATH_RE = re.compile(r"^/drive/(?:(?:u/\d+|mobile)/)?folders/([A-Za-z0-9_\-]{8,128})/?$")
_DRIVE_FILE_PATH_RE = re.compile(r"^/file/d/([A-Za-z0-9_\-]{8,128})(?:/[^/]+)?/?$")
_COLAB_DRIVE_PATH_RE = re.compile(r"^/drive/([A-Za-z0-9_\-]{8,128})/?$")


def extract_google_drive_id(raw: Any, *, kind: Literal["any", "folder", "notebook"] = "any") -> str:
    """Extract and strictly validate a Google Drive folder or Colab notebook ID.

    Accepts either a raw ID (`^[A-Za-z0-9_\\-]{8,128}$`) or an official Google Drive /
    Google Colab URL (`https://drive.google.com/drive/folders/<id>`,
    `https://colab.research.google.com/drive/<id>`, `https://drive.google.com/file/d/<id>`,
    `https://drive.google.com/open?id=<id>`). Returns `""` if empty or invalid.
    """
    if raw is None or isinstance(raw, bool):
        return ""
    s = str(raw).strip()
    if not s or any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        return ""
    if _SAFE_DRIVE_ID_STRICT_RE.fullmatch(s):
        return s

    parsed = urlparse(s)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme != "https" or parsed.username is not None or parsed.password is not None:
        return ""
    path = parsed.path or ""

    cand = ""
    if kind in ("any", "folder") and host == "drive.google.com":
        m = _DRIVE_FOLDER_PATH_RE.match(path)
        if m:
            cand = m.group(1)
        elif path in ("/open", "/open/"):
            qs_ids = parse_qs(parsed.query).get("id") or []
            if qs_ids:
                cand = str(qs_ids[0] or "").strip()
    if not cand and kind in ("any", "notebook"):
        if host == "colab.research.google.com":
            m = _COLAB_DRIVE_PATH_RE.match(path)
            if m:
                cand = m.group(1)
        elif host == "drive.google.com":
            m = _DRIVE_FILE_PATH_RE.match(path)
            if m:
                cand = m.group(1)
            elif path in ("/open", "/open/"):
                qs_ids = parse_qs(parsed.query).get("id") or []
                if qs_ids:
                    cand = str(qs_ids[0] or "").strip()

    if cand and _SAFE_DRIVE_ID_STRICT_RE.fullmatch(cand):
        return cand
    return ""


def is_virtual_loopback_mic_label(label: Any) -> bool:
    """Return True iff `label` refers to a virtual audio cable or loopback device unsafe as Mode 1 PC mic."""
    low = str(label or "").strip().lower()
    if not low:
        return False
    return any(tok in low for tok in _VIRTUAL_LOOPBACK_MIC_TOKENS)


def _is_valid_web_dyn_code(code: str) -> bool:
    """Return True iff `code` is a safe dynamic account-synced `web_dyn:*` browser model code."""
    if not isinstance(code, str) or not code.startswith("web_dyn:"):
        return False
    tail = code[len("web_dyn:") :].strip()
    if not tail or len(code) > 128:
        return False
    if any(bad in code for bad in ("..", "/", "\\", "\x00")):
        return False
    return all(ord(ch) >= 32 and ord(ch) != 127 for ch in code)


def _is_valid_browser_model_code(code: str, provider: str) -> bool:
    """Validate a browser model code against known presets for `provider` or safe `web_dyn:*`."""
    if not isinstance(code, str):
        return False
    s = code.strip()
    if not s:
        return False
    if _is_valid_web_dyn_code(s):
        return True
    if provider == "claude":
        return s in VALID_CLAUDE_BROWSER_MODELS
    if provider == "chatgpt":
        return s in VALID_CHATGPT_BROWSER_MODELS
    return s in VALID_GEMINI_BROWSER_MODELS

_LEGACY_BROWSER_MODEL_MAP: dict[str, tuple[str, str | None]] = {
    "hydrogen_low": ("hydrogen", "low"),
    "hydrogen_medium": ("hydrogen", "medium"),
    "hydrogen_high": ("hydrogen", "high"),
    "flash_lite": ("auto", None),
    "pro_31": ("neon_4", None),
    "argon_4": ("neon_4", None),
}
_LEGACY_SUBTITLE_MAP: dict[str, tuple[str, str | None]] = {
    "auto_llm": ("auto_inline", None),
    "browser_hydrogen_low": ("web:hydrogen", "low"),
    "browser_hydrogen_high": ("web:hydrogen", "high"),
    "browser_flash_lite": ("web:auto", "low"),
    "web:flash_lite": ("web:auto", None),
    "web:pro_31": ("web:neon_4", None),
    "web:argon_4": ("web:neon_4", None),
}
_RETIRED_WEB_CODES: frozenset[str] = frozenset({"flash_lite", "pro_31", "argon_4"})
_SUBTITLE_API_PREFIXES: tuple[tuple[str, str], ...] = (
    ("api:", "gemini"),
    ("claude_api:", "claude"),
    ("openai_api:", "chatgpt"),
)
_SUBTITLE_WEB_PREFIXES: tuple[str, ...] = ("web:", "claude_web:", "chatgpt_web:")

ConfigLoadStatus = Literal["ok", "missing", "repaired_fields", "corrupt_json", "unreadable"]


@dataclass
class ConfigLoadReport:
    """Structured result from defensively loading `%LOCALAPPDATA%\\VoiceCompanionData\\config.json`."""

    config: dict[str, Any]
    status: ConfigLoadStatus
    repaired_fields: list[str] = field(default_factory=list)
    backup_dir: Path | None = None
    fish_api_key_preserved: bool = False
    fish_api_key_dpapi_failed: bool = False
    error_detail: str = ""

    @property
    def had_issues(self) -> bool:
        return (
            self.status in ("corrupt_json", "unreadable")
            or bool(self.repaired_fields)
            or self.fish_api_key_dpapi_failed
        )


@dataclass
class SettingsResetResult:
    """Structured result from restoring all user settings to defaults while preserving Fish API key & Gemini login."""

    config: dict[str, Any]
    backup_dir: Path | None
    fish_api_key_preserved: bool
    browser_profile_preserved: bool
    browser_profile_exists: bool
    reset_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _clean_single_line_secret(raw: Any) -> str:
    """Strip NUL/CRLF/control characters and bound length for API key strings."""
    if not isinstance(raw, str):
        return ""
    s = raw.replace("\x00", "")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    cleaned = "".join(ch for ch in first if ord(ch) >= 32 and ord(ch) != 127)
    return cleaned[:MAX_SINGLE_LINE_SECRET_LEN]


def _coerce_bool(raw: Any, default: bool, key: str, repaired: list[str], present: bool) -> bool:
    if not present:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        fval = float(raw)
        if math.isfinite(fval) and fval in (0.0, 1.0):
            res = bool(int(fval))
            repaired.append(f"{key} (number->{res})")
            return res
        repaired.append(f"{key} (invalid bool -> {default})")
        return default
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "y", "on"):
            repaired.append(f"{key} (str->True)")
            return True
        if low in ("false", "0", "no", "n", "off"):
            repaired.append(f"{key} (str->False)")
            return False
    repaired.append(f"{key} (invalid bool -> {default})")
    return default


def _coerce_float(
    raw: Any,
    default: float,
    min_val: float,
    max_val: float,
    key: str,
    repaired: list[str],
    present: bool,
) -> float:
    if not present:
        return default
    if raw is None or isinstance(raw, bool):
        repaired.append(f"{key} (invalid float -> {default})")
        return default
    val: float
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            repaired.append(f"{key} (empty float -> {default})")
            return default
        try:
            val = float(s)
        except ValueError:
            repaired.append(f"{key} (invalid float -> {default})")
            return default
    else:
        repaired.append(f"{key} (invalid float -> {default})")
        return default
    if not math.isfinite(val):
        repaired.append(f"{key} (non-finite -> {default})")
        return default
    clamped = max(min_val, min(max_val, val))
    if clamped != val:
        repaired.append(f"{key} (clamped {val} -> {clamped})")
    return clamped


def _coerce_int(
    raw: Any,
    default: int,
    min_val: int,
    max_val: int,
    key: str,
    repaired: list[str],
    present: bool,
) -> int:
    if not present:
        return default
    if raw is None or isinstance(raw, bool):
        repaired.append(f"{key} (invalid int -> {default})")
        return default
    val: int
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, float):
        if not math.isfinite(raw):
            repaired.append(f"{key} (non-finite -> {default})")
            return default
        val = int(round(raw))
        if float(val) != raw:
            repaired.append(f"{key} (float->int {val})")
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            repaired.append(f"{key} (empty int -> {default})")
            return default
        try:
            val = int(s)
        except ValueError:
            try:
                fval = float(s)
            except ValueError:
                repaired.append(f"{key} (invalid int -> {default})")
                return default
            if not math.isfinite(fval):
                repaired.append(f"{key} (non-finite -> {default})")
                return default
            val = int(round(fval))
            repaired.append(f"{key} (str->int {val})")
    else:
        repaired.append(f"{key} (invalid int -> {default})")
        return default
    clamped = max(min_val, min(max_val, val))
    if clamped != val:
        repaired.append(f"{key} (clamped {val} -> {clamped})")
    return clamped


def _coerce_enum(
    raw: Any,
    valid: tuple[str, ...],
    default: str,
    key: str,
    repaired: list[str],
    present: bool,
    *,
    lower: bool = True,
    aliases: dict[str, str] | None = None,
) -> str:
    if not present:
        return default
    if not isinstance(raw, str):
        repaired.append(f"{key} (invalid enum -> {default})")
        return default
    s = raw.strip()
    if lower:
        s = s.lower()
    if aliases and s in aliases:
        return aliases[s]
    if s in valid:
        return s
    repaired.append(f"{key} (invalid value -> {default})")
    return default


def _coerce_str(
    raw: Any,
    default: str,
    key: str,
    repaired: list[str],
    present: bool,
    *,
    max_len: int = 512,
    allow_empty: bool = True,
    allow_newlines: bool = False,
    reject_paren_placeholder: bool = False,
) -> str:
    if not present:
        return default
    if not isinstance(raw, str):
        repaired.append(f"{key} (invalid str -> default)")
        return default
    s = raw.replace("\x00", "")
    if not allow_newlines:
        s = "".join(ch for ch in s if ord(ch) >= 32 and ord(ch) != 127).strip()
    else:
        s = "".join(ch for ch in s if ch in ("\n", "\r", "\t") or (ord(ch) >= 32 and ord(ch) != 127))
    if reject_paren_placeholder and s.startswith("("):
        repaired.append(f"{key} (placeholder device -> '')")
        return ""
    if not allow_empty and not s:
        repaired.append(f"{key} (empty -> {default})")
        return default
    if len(s) > max_len:
        s = s[:max_len]
        repaired.append(f"{key} (truncated>{max_len})")
    return s


def _sanitize_voice_map(raw: Any, key: str, repaired: list[str], present: bool) -> dict[str, str]:
    if not present:
        return {}
    if not isinstance(raw, dict):
        repaired.append(f"{key} (non-dict -> {{}})")
        return {}
    clean: dict[str, str] = {}
    dropped = 0
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            dropped += 1
            continue
        ck = "".join(ch for ch in k.replace("\x00", "") if ord(ch) >= 32 and ord(ch) != 127).strip()
        cv = "".join(ch for ch in v.replace("\x00", "") if ord(ch) >= 32 and ord(ch) != 127).strip()
        if not ck or not cv or len(ck) > 128 or len(cv) > 256:
            dropped += 1
            continue
        clean[ck] = cv
    if dropped:
        repaired.append(f"{key} (dropped {dropped} invalid entries)")
    return clean


def _sanitize_web_model_pairs(raw: Any, key: str, repaired: list[str], present: bool) -> list[list[str]]:
    if not present:
        return []
    if not isinstance(raw, list):
        repaired.append(f"{key} (non-list -> [])")
        return []
    out: list[list[str]] = []
    seen: set[str] = set()
    dropped = 0
    for entry in raw:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and isinstance(entry[0], str) and isinstance(entry[1], str):
            code = "".join(ch for ch in entry[0].replace("\x00", "") if ord(ch) >= 32 and ord(ch) != 127).strip()
            label = "".join(ch for ch in entry[1].replace("\x00", "") if ord(ch) >= 32 and ord(ch) != 127).strip()
            if code and label and code not in seen:
                seen.add(code)
                out.append([code[:128], label[:200]])
                continue
        dropped += 1
    if dropped:
        repaired.append(f"{key} (dropped {dropped} invalid entries)")
    return out


def _decode_secret_field(raw_val: Any, secret_key: str, repaired: list[str], present: bool) -> tuple[str, bool]:
    """Decode a secret field from loaded config dict. Returns `(plaintext, dpapi_failed)`."""
    if not present or raw_val is None:
        if present and raw_val is None:
            repaired.append(f"{secret_key} (null -> '')")
        return "", False
    if isinstance(raw_val, bool):
        repaired.append(f"{secret_key} (invalid bool -> '')")
        return "", False
    if isinstance(raw_val, (dict, list)):
        if secret_key == "fish_mcp_oauth_data":
            try:
                return json.dumps(raw_val, ensure_ascii=False, separators=(",", ":"), allow_nan=False), False
            except Exception:
                repaired.append(f"{secret_key} (unserializable -> '')")
                return "", False
        repaired.append(f"{secret_key} (invalid container -> '')")
        return "", False
    if not isinstance(raw_val, str):
        repaired.append(f"{secret_key} (invalid type -> '')")
        return "", False

    raw_secret = raw_val.strip()
    if not raw_secret:
        return "", False
    if raw_secret.startswith(ap.DPAPI_PREFIX):
        dec = ap.unprotect_secret(raw_secret)
        if not dec:
            return "", True
        if secret_key == "fish_mcp_oauth_data":
            return dec, False
        return _clean_single_line_secret(dec), False
    if secret_key == "fish_mcp_oauth_data":
        return raw_secret.replace("\x00", ""), False
    return _clean_single_line_secret(raw_secret), False


def _sanitize_extra_json_value(val: Any, _depth: int = 0) -> tuple[Any, bool]:
    """Recursively ensure an unknown extra config value is strict JSON-safe (`allow_nan=False`)."""
    if _depth > 16:
        return None, True
    if val is None or isinstance(val, (bool, int)):
        return val, False
    if isinstance(val, float):
        if math.isfinite(val):
            return val, False
        return None, True
    if isinstance(val, str):
        if "\x00" in val:
            return val.replace("\x00", ""), True
        return val, False
    if isinstance(val, (list, tuple)):
        out_list: list[Any] = []
        changed = isinstance(val, tuple)
        for item in val:
            clean_item, mod = _sanitize_extra_json_value(item, _depth + 1)
            changed = changed or mod
            out_list.append(clean_item)
        return out_list, changed
    if isinstance(val, dict):
        out_dict: dict[str, Any] = {}
        changed = False
        for dk, dv in val.items():
            if not isinstance(dk, str):
                changed = True
                continue
            clean_dk = dk.replace("\x00", "")
            if clean_dk != dk:
                changed = True
            clean_dv, mod = _sanitize_extra_json_value(dv, _depth + 1)
            changed = changed or mod
            out_dict[clean_dk] = clean_dv
        return out_dict, changed
    return None, True


def _sanitize_user_config_internal(raw_cfg: Any) -> tuple[dict[str, Any], list[str], bool]:
    """Core sanitizer returning `(clean_cfg, repaired_fields, fish_api_key_dpapi_failed)`."""
    defaults = ap.DEFAULT_USER_CONFIG
    repaired: list[str] = []
    if not isinstance(raw_cfg, dict):
        return deepcopy(defaults), ["root (non-dict -> defaults)"], False

    cfg = deepcopy(defaults)
    known_keys = set(defaults.keys())
    # Preserve unknown strict-JSON-safe extra keys (e.g. selected_momotalk_entry_id) safely
    for k, v in raw_cfg.items():
        if isinstance(k, str) and k not in known_keys:
            if k == "selected_momotalk_entry_id":
                cfg[k] = _coerce_str(v, "", k, repaired, True, max_len=128)
            else:
                if isinstance(v, float) and not math.isfinite(v):
                    repaired.append(f"{k} (dropped non-finite extra float)")
                    continue
                clean_extra, modified = _sanitize_extra_json_value(v)
                if modified:
                    repaired.append(f"{k} (sanitized extra value)")
                if clean_extra is None and v is not None:
                    continue
                cfg[k] = clean_extra

    def _has(key: str) -> bool:
        return key in raw_cfg

    # 1. Secrets (DPAPI decrypt without ever crashing)
    fish_dpapi_failed = False
    for secret_key in ap.SECRET_CONFIG_KEYS:
        plain, dpapi_failed = _decode_secret_field(raw_cfg.get(secret_key), secret_key, repaired, _has(secret_key))
        cfg[secret_key] = plain
        if secret_key == "fish_api_key" and dpapi_failed:
            fish_dpapi_failed = True

    # 2. Version / character / prompt / language
    if _has("emotion_engine_version"):
        prev_ver = _coerce_int(
            raw_cfg.get("emotion_engine_version"), 4, 0, 999, "emotion_engine_version", repaired, True
        )
    else:
        prev_ver = 0
    cfg["emotion_engine_version"] = prev_ver if prev_ver >= 4 else 4

    sel_cid = _coerce_str(
        raw_cfg.get("selected_character_id"),
        "Aru",
        "selected_character_id",
        repaired,
        _has("selected_character_id"),
        max_len=64,
        allow_empty=False,
    )
    if any(sep in sel_cid for sep in ("/", "\\", "..", ":")):
        repaired.append("selected_character_id (unsafe chars -> Aru)")
        sel_cid = "Aru"
    cfg["selected_character_id"] = sel_cid

    cfg["persona_prompt_variant"] = _coerce_enum(
        raw_cfg.get("persona_prompt_variant"),
        VALID_PERSONA_VARIANTS,
        "official",
        "persona_prompt_variant",
        repaired,
        _has("persona_prompt_variant"),
    )
    cfg["reply_language"] = _coerce_enum(
        raw_cfg.get("reply_language"),
        ("ja", "ko"),
        "ja",
        "reply_language",
        repaired,
        _has("reply_language"),
    )
    cfg["use_custom_persona"] = _coerce_bool(
        raw_cfg.get("use_custom_persona"), False, "use_custom_persona", repaired, _has("use_custom_persona")
    )
    cfg["custom_persona_prompt"] = _coerce_str(
        raw_cfg.get("custom_persona_prompt"),
        str(defaults["custom_persona_prompt"]),
        "custom_persona_prompt",
        repaired,
        _has("custom_persona_prompt"),
        max_len=MAX_CUSTOM_PERSONA_PROMPT_LEN,
        allow_empty=True,
        allow_newlines=True,
    )

    # 3. Fish Audio / MCP / Voice settings
    cfg["fish_mcp_fallback_enabled"] = _coerce_bool(
        raw_cfg.get("fish_mcp_fallback_enabled"), True, "fish_mcp_fallback_enabled", repaired, _has("fish_mcp_fallback_enabled")
    )
    cfg["fish_mcp_primary_mode"] = _coerce_bool(
        raw_cfg.get("fish_mcp_primary_mode"), False, "fish_mcp_primary_mode", repaired, _has("fish_mcp_primary_mode")
    )
    cfg["fish_credit_saver_mode"] = _coerce_bool(
        raw_cfg.get("fish_credit_saver_mode"), True, "fish_credit_saver_mode", repaired, _has("fish_credit_saver_mode")
    )
    cfg["fish_tts_cache_enabled"] = _coerce_bool(
        raw_cfg.get("fish_tts_cache_enabled"), True, "fish_tts_cache_enabled", repaired, _has("fish_tts_cache_enabled")
    )
    cfg["colab_s2pro_enabled"] = _coerce_bool(
        raw_cfg.get("colab_s2pro_enabled"), False, "colab_s2pro_enabled", repaired, _has("colab_s2pro_enabled")
    )
    cfg["colab_s2pro_url"] = _coerce_str(
        raw_cfg.get("colab_s2pro_url"), "", "colab_s2pro_url", repaired, _has("colab_s2pro_url"), max_len=512
    )
    for _did_key, _did_kind in (
        ("colab_s2pro_drive_folder_id", "folder"),
        ("colab_s2pro_notebook_id", "notebook"),
    ):
        _did_raw = _coerce_str(
            raw_cfg.get(_did_key), "", _did_key, repaired, _has(_did_key), max_len=512
        )
        if _did_raw:
            _did_val = extract_google_drive_id(_did_raw, kind=_did_kind)  # type: ignore[arg-type]
            if not _did_val:
                repaired.append(f"{_did_key} (invalid drive id -> '')")
            elif _did_val != _did_raw:
                repaired.append(f"{_did_key} (url -> id)")
        else:
            _did_val = ""
        cfg[_did_key] = _did_val
    cfg["colab_s2pro_endpoint_file"] = _coerce_str(
        raw_cfg.get("colab_s2pro_endpoint_file"),
        "",
        "colab_s2pro_endpoint_file",
        repaired,
        _has("colab_s2pro_endpoint_file"),
        max_len=512,
    )
    cfg["fish_reference_id"] = _coerce_str(
        raw_cfg.get("fish_reference_id"), "", "fish_reference_id", repaired, _has("fish_reference_id"), max_len=128
    )
    cfg["fish_voice_title"] = _coerce_str(
        raw_cfg.get("fish_voice_title"),
        str(defaults["fish_voice_title"]),
        "fish_voice_title",
        repaired,
        _has("fish_voice_title"),
        max_len=200,
    )
    cfg["saved_voices"] = _sanitize_voice_map(
        raw_cfg.get("saved_voices"), "saved_voices", repaired, _has("saved_voices")
    )
    cfg["saved_voices_kr"] = _sanitize_voice_map(
        raw_cfg.get("saved_voices_kr"), "saved_voices_kr", repaired, _has("saved_voices_kr")
    )
    cfg["steam_game_dir"] = _coerce_str(
        raw_cfg.get("steam_game_dir"), "", "steam_game_dir", repaired, _has("steam_game_dir"), max_len=512
    )
    cfg["auto_voice_visibility"] = _coerce_enum(
        raw_cfg.get("auto_voice_visibility"),
        VALID_AUTO_VOICE_VISIBILITIES,
        "private",
        "auto_voice_visibility",
        repaired,
        _has("auto_voice_visibility"),
    )
    cfg["auto_voice_quality_profile"] = _coerce_enum(
        raw_cfg.get("auto_voice_quality_profile"),
        VALID_AUTO_VOICE_PROFILES,
        "plus200",
        "auto_voice_quality_profile",
        repaired,
        _has("auto_voice_quality_profile"),
    )
    cfg["fish_model_header"] = _coerce_enum(
        raw_cfg.get("fish_model_header"),
        VALID_FISH_MODEL_HEADERS,
        "s2.1-pro-free",
        "fish_model_header",
        repaired,
        _has("fish_model_header"),
        lower=False,
    )
    cfg["stt_language"] = _coerce_enum(
        raw_cfg.get("stt_language"),
        VALID_STT_LANGUAGES,
        "auto",
        "stt_language",
        repaired,
        _has("stt_language"),
    )
    cfg["whisper_model"] = _coerce_enum(
        raw_cfg.get("whisper_model"),
        VALID_WHISPER_MODELS,
        "auto",
        "whisper_model",
        repaired,
        _has("whisper_model"),
        aliases={"turbo": "large-v3-turbo"},
    )
    cfg["streaming_speed_mode"] = _coerce_enum(
        raw_cfg.get("streaming_speed_mode"),
        VALID_SPEED_MODES,
        "expressive",
        "streaming_speed_mode",
        repaired,
        _has("streaming_speed_mode"),
        aliases={"balanced": "expressive"},
    )
    if prev_ver < 4:
        cfg["streaming_speed_mode"] = "expressive"
        cfg["emotion_engine_version"] = 4

    # P1-4 guarded legacy single-character Aru voice backfill (never pollutes Aru with other chars or KR IDs)
    if (
        cfg["fish_reference_id"]
        and not cfg["saved_voices"]
        and cfg["selected_character_id"] == "Aru"
        and cfg["reply_language"] != "ko"
    ):
        cfg["saved_voices"]["Aru"] = cfg["fish_reference_id"]

    # 4. Audio / Directing / Proactive / VAD numeric settings
    cfg["auto_emotion_directing"] = _coerce_bool(
        raw_cfg.get("auto_emotion_directing"), True, "auto_emotion_directing", repaired, _has("auto_emotion_directing")
    )
    cfg["expression_boost_offset"] = _coerce_float(
        raw_cfg.get("expression_boost_offset"),
        0.0,
        -0.25,
        0.25,
        "expression_boost_offset",
        repaired,
        _has("expression_boost_offset"),
    )
    cfg["latency_mode"] = _coerce_enum(
        raw_cfg.get("latency_mode"),
        VALID_LATENCY_MODES,
        "balanced",
        "latency_mode",
        repaired,
        _has("latency_mode"),
    )
    cfg["pause_threshold_sec"] = _coerce_float(
        raw_cfg.get("pause_threshold_sec"),
        0.33,
        0.10,
        3.00,
        "pause_threshold_sec",
        repaired,
        _has("pause_threshold_sec"),
    )
    cfg["output_volume"] = _coerce_float(
        raw_cfg.get("output_volume"),
        1.0,
        0.10,
        2.00,
        "output_volume",
        repaired,
        _has("output_volume"),
    )
    cfg["proactive_prompt_append"] = _coerce_bool(
        raw_cfg.get("proactive_prompt_append"), True, "proactive_prompt_append", repaired, _has("proactive_prompt_append")
    )
    cfg["proactive_enable"] = _coerce_bool(
        raw_cfg.get("proactive_enable"), True, "proactive_enable", repaired, _has("proactive_enable")
    )
    cfg["proactive_timeout_sec"] = _coerce_float(
        raw_cfg.get("proactive_timeout_sec"),
        18.0,
        3.0,
        300.0,
        "proactive_timeout_sec",
        repaired,
        _has("proactive_timeout_sec"),
    )
    cfg["proactive_trigger_volume"] = _coerce_float(
        raw_cfg.get("proactive_trigger_volume"),
        1.0,
        0.10,
        2.00,
        "proactive_trigger_volume",
        repaired,
        _has("proactive_trigger_volume"),
    )
    cfg["proactive_trigger_device"] = _coerce_str(
        raw_cfg.get("proactive_trigger_device"),
        "",
        "proactive_trigger_device",
        repaired,
        _has("proactive_trigger_device"),
        max_len=256,
        reject_paren_placeholder=True,
    )
    cfg["primary_output_device"] = _coerce_str(
        raw_cfg.get("primary_output_device"),
        "",
        "primary_output_device",
        repaired,
        _has("primary_output_device"),
        max_len=256,
        reject_paren_placeholder=True,
    )
    mic_dev = _coerce_str(
        raw_cfg.get("mic_input_device"),
        "",
        "mic_input_device",
        repaired,
        _has("mic_input_device"),
        max_len=256,
        reject_paren_placeholder=True,
    )
    if mic_dev and is_virtual_loopback_mic_label(mic_dev):
        repaired.append("mic_input_device (virtual loopback -> '')")
        mic_dev = ""
    cfg["mic_input_device"] = mic_dev
    cfg["mic_vad_rms_threshold"] = _coerce_float(
        raw_cfg.get("mic_vad_rms_threshold"),
        0.010,
        0.001,
        0.25,
        "mic_vad_rms_threshold",
        repaired,
        _has("mic_vad_rms_threshold"),
    )
    cfg["mic_silence_end_sec"] = _coerce_float(
        raw_cfg.get("mic_silence_end_sec"),
        0.68,
        0.15,
        5.0,
        "mic_silence_end_sec",
        repaired,
        _has("mic_silence_end_sec"),
    )
    cfg["mic_stt_language"] = _coerce_enum(
        raw_cfg.get("mic_stt_language"),
        VALID_MIC_STT_LANGUAGES,
        "ko_auto",
        "mic_stt_language",
        repaired,
        _has("mic_stt_language"),
    )
    cfg["companion_proactive_enable"] = _coerce_bool(
        raw_cfg.get("companion_proactive_enable"),
        True,
        "companion_proactive_enable",
        repaired,
        _has("companion_proactive_enable"),
    )
    min_s = _coerce_float(
        raw_cfg.get("idle_ping_min_sec"),
        45.0,
        5.0,
        3600.0,
        "idle_ping_min_sec",
        repaired,
        _has("idle_ping_min_sec"),
    )
    max_s = _coerce_float(
        raw_cfg.get("idle_ping_max_sec"),
        max(min_s, 75.0),
        min_s,
        7200.0,
        "idle_ping_max_sec",
        repaired,
        _has("idle_ping_max_sec"),
    )
    cfg["idle_ping_min_sec"] = min_s
    cfg["idle_ping_max_sec"] = max_s
    cfg["afk_max_consecutive_pings"] = _coerce_int(
        raw_cfg.get("afk_max_consecutive_pings"),
        2,
        1,
        99,
        "afk_max_consecutive_pings",
        repaired,
        _has("afk_max_consecutive_pings"),
    )

    # 5. AI Provider, Backend, Models, Subtitles & Legacy Migrations
    cfg["ai_provider"] = _coerce_enum(
        raw_cfg.get("ai_provider"),
        VALID_AI_PROVIDERS,
        "gemini",
        "ai_provider",
        repaired,
        _has("ai_provider"),
    )
    cfg["gemini_backend"] = _coerce_enum(
        raw_cfg.get("gemini_backend"),
        VALID_GEMINI_BACKENDS,
        "browser",
        "gemini_backend",
        repaired,
        _has("gemini_backend"),
    )

    raw_bmodel = _coerce_str(
        raw_cfg.get("gemini_browser_model"),
        "hydrogen",
        "gemini_browser_model",
        repaired,
        _has("gemini_browser_model"),
        max_len=128,
        allow_empty=False,
    )
    inferred_chat_think: str | None = None
    if raw_bmodel in _LEGACY_BROWSER_MODEL_MAP:
        base_m, inf_lvl = _LEGACY_BROWSER_MODEL_MAP[raw_bmodel]
        cfg["gemini_browser_model"] = base_m
        if inf_lvl and not _has("gemini_thinking_level"):
            inferred_chat_think = inf_lvl
    elif _is_valid_browser_model_code(raw_bmodel, "gemini"):
        cfg["gemini_browser_model"] = raw_bmodel
    else:
        repaired.append("gemini_browser_model (invalid value -> hydrogen)")
        cfg["gemini_browser_model"] = "hydrogen"

    if inferred_chat_think is not None:
        cfg["gemini_thinking_level"] = inferred_chat_think
    else:
        cfg["gemini_thinking_level"] = _coerce_enum(
            raw_cfg.get("gemini_thinking_level"),
            VALID_THINKING_LEVELS,
            "low",
            "gemini_thinking_level",
            repaired,
            _has("gemini_thinking_level"),
        )

    cfg["subtitle_mode"] = _coerce_enum(
        raw_cfg.get("subtitle_mode"),
        VALID_SUBTITLE_MODES,
        "jp_kr",
        "subtitle_mode",
        repaired,
        _has("subtitle_mode"),
    )
    cfg["subtitle_font_size"] = _coerce_int(
        raw_cfg.get("subtitle_font_size"),
        11,
        9,
        18,
        "subtitle_font_size",
        repaired,
        _has("subtitle_font_size"),
    )
    raw_sub = _coerce_str(
        raw_cfg.get("subtitle_engine"),
        "auto_inline",
        "subtitle_engine",
        repaired,
        _has("subtitle_engine"),
        max_len=160,
        allow_empty=False,
    )
    inferred_sub_think: str | None = None
    if raw_sub in _LEGACY_SUBTITLE_MAP:
        new_sub, inf_lvl = _LEGACY_SUBTITLE_MAP[raw_sub]
        cfg["subtitle_engine"] = new_sub
        if inf_lvl and not _has("subtitle_thinking_level"):
            inferred_sub_think = inf_lvl
    elif raw_sub in ("auto_inline", "fast_nmt"):
        cfg["subtitle_engine"] = raw_sub
    elif raw_sub.startswith(_SUBTITLE_WEB_PREFIXES):
        prefix = next(p for p in _SUBTITLE_WEB_PREFIXES if raw_sub.startswith(p))
        tail = raw_sub[len(prefix) :].strip()
        web_prov = "claude" if prefix == "claude_web:" else ("chatgpt" if prefix == "chatgpt_web:" else "gemini")
        if tail and _is_valid_browser_model_code(tail, web_prov):
            cfg["subtitle_engine"] = f"{prefix}{tail}"
        elif not tail:
            repaired.append("subtitle_engine (empty web tail -> auto_inline)")
            cfg["subtitle_engine"] = "auto_inline"
        else:
            repaired.append("subtitle_engine (invalid web model -> auto_inline)")
            cfg["subtitle_engine"] = "auto_inline"
    else:
        matched_api_prefix = False
        for api_pref, api_prov in _SUBTITLE_API_PREFIXES:
            if raw_sub.startswith(api_pref):
                matched_api_prefix = True
                clean_tail = ap._sanitize_stored_api_model(raw_sub[len(api_pref) :], api_prov, default="")
                if clean_tail:
                    cfg["subtitle_engine"] = f"{api_pref}{clean_tail}"
                else:
                    repaired.append("subtitle_engine (invalid api model -> auto_inline)")
                    cfg["subtitle_engine"] = "auto_inline"
                break
        if not matched_api_prefix:
            repaired.append("subtitle_engine (unknown -> auto_inline)")
            cfg["subtitle_engine"] = "auto_inline"

    if inferred_sub_think is not None:
        cfg["subtitle_thinking_level"] = inferred_sub_think
    else:
        cfg["subtitle_thinking_level"] = _coerce_enum(
            raw_cfg.get("subtitle_thinking_level"),
            VALID_THINKING_LEVELS,
            "low",
            "subtitle_thinking_level",
            repaired,
            _has("subtitle_thinking_level"),
        )

    for api_key, prov, def_model in (
        ("gemini_api_model", "gemini", "gemini-3.5-flash-lite"),
        ("claude_api_model", "claude", "claude-sonnet-5-5"),
        ("openai_api_model", "chatgpt", "gpt-5.4-mini"),
    ):
        raw_m = raw_cfg.get(api_key)
        clean_m = ap._sanitize_stored_api_model(raw_m, prov, def_model)
        if _has(api_key) and (not isinstance(raw_m, str) or (raw_m.strip() and clean_m == def_model and raw_m.strip() != def_model and not raw_m.strip().lower().startswith(f"models/{def_model}"))):
            # Only flag genuine corruption (not valid extraction of a different model)
            if not isinstance(raw_m, str) or ap._sanitize_stored_api_model(raw_m, prov, default="") == "":
                if not (isinstance(raw_m, str) and not raw_m.strip()):
                    repaired.append(f"{api_key} (invalid model -> {def_model})")
        cfg[api_key] = clean_m

    for list_key, prov in (
        ("api_models", "gemini"),
        ("claude_api_models", "claude"),
        ("openai_api_models", "chatgpt"),
    ):
        if _has(list_key) and not isinstance(raw_cfg.get(list_key), list):
            repaired.append(f"{list_key} (non-list -> [])")
        cfg[list_key] = ap._sanitize_stored_api_model_list(raw_cfg.get(list_key, []), prov)

    raw_web_schema = _coerce_int(
        raw_cfg.get("web_models_schema_version") if _has("web_models_schema_version") else 1,
        1,
        1,
        999,
        "web_models_schema_version",
        repaired,
        _has("web_models_schema_version"),
    )
    clean_web_models = _sanitize_web_model_pairs(
        raw_cfg.get("web_models"), "web_models", repaired, _has("web_models")
    )
    if raw_web_schema < 2 and any(code in _RETIRED_WEB_CODES for code, _ in clean_web_models):
        clean_web_models = []
    cfg["web_models"] = clean_web_models
    cfg["web_models_schema_version"] = 2
    cfg["web_account_tier"] = _coerce_enum(
        raw_cfg.get("web_account_tier"),
        VALID_WEB_ACCOUNT_TIERS,
        "",
        "web_account_tier",
        repaired,
        _has("web_account_tier"),
        lower=False,
    )

    raw_claude_bm = _coerce_str(
        raw_cfg.get("claude_browser_model"),
        "default",
        "claude_browser_model",
        repaired,
        _has("claude_browser_model"),
        max_len=128,
        allow_empty=False,
    )
    if _is_valid_browser_model_code(raw_claude_bm, "claude"):
        cfg["claude_browser_model"] = raw_claude_bm
    else:
        repaired.append("claude_browser_model (invalid value -> default)")
        cfg["claude_browser_model"] = "default"

    raw_chatgpt_bm = _coerce_str(
        raw_cfg.get("chatgpt_browser_model"),
        "default",
        "chatgpt_browser_model",
        repaired,
        _has("chatgpt_browser_model"),
        max_len=128,
        allow_empty=False,
    )
    if _is_valid_browser_model_code(raw_chatgpt_bm, "chatgpt"):
        cfg["chatgpt_browser_model"] = raw_chatgpt_bm
    else:
        repaired.append("chatgpt_browser_model (invalid value -> default)")
        cfg["chatgpt_browser_model"] = "default"
    cfg["claude_web_models"] = _sanitize_web_model_pairs(
        raw_cfg.get("claude_web_models"), "claude_web_models", repaired, _has("claude_web_models")
    )
    cfg["chatgpt_web_models"] = _sanitize_web_model_pairs(
        raw_cfg.get("chatgpt_web_models"), "chatgpt_web_models", repaired, _has("chatgpt_web_models")
    )
    cfg["claude_web_account_tier"] = _coerce_str(
        raw_cfg.get("claude_web_account_tier"),
        "",
        "claude_web_account_tier",
        repaired,
        _has("claude_web_account_tier"),
        max_len=32,
    )
    cfg["chatgpt_web_account_tier"] = _coerce_str(
        raw_cfg.get("chatgpt_web_account_tier"),
        "",
        "chatgpt_web_account_tier",
        repaired,
        _has("chatgpt_web_account_tier"),
        max_len=32,
    )

    # 6. Bridge mode <-> Call bridge mode sync (preserving legacy precedence)
    raw_bm = raw_cfg.get("bridge_mode") if _has("bridge_mode") else None
    raw_cbm = raw_cfg.get("call_bridge_mode") if _has("call_bridge_mode") else None
    if raw_bm is None and raw_cbm is not None:
        cbm = _coerce_enum(raw_cbm, ("mode1", "mode2"), "mode1", "call_bridge_mode", repaired, True)
        cfg["call_bridge_mode"] = cbm
        cfg["bridge_mode"] = "gemini_live_audio" if cbm == "mode2" else "pc_mic"
    else:
        bm = _coerce_enum(
            raw_bm,
            ("pc_mic", "gemini_live_audio"),
            "pc_mic",
            "bridge_mode",
            repaired,
            _has("bridge_mode") and raw_bm is not None,
        )
        if _has("bridge_mode") and raw_bm is None:
            repaired.append("bridge_mode (null -> pc_mic)")
        cfg["bridge_mode"] = bm
        cfg["call_bridge_mode"] = "mode2" if bm == "gemini_live_audio" else "mode1"

    jp_coach = _coerce_bool(
        raw_cfg.get("jp_beginner_lover_mode"), False, "jp_beginner_lover_mode", repaired, _has("jp_beginner_lover_mode")
    )
    cfg["jp_beginner_lover_mode"] = False if cfg["reply_language"] == "ko" else jp_coach
    cfg["temporary_call_mode"] = _coerce_bool(
        raw_cfg.get("temporary_call_mode"), False, "temporary_call_mode", repaired, _has("temporary_call_mode")
    )

    return cfg, repaired, fish_dpapi_failed


def sanitize_user_config(raw_cfg: Any) -> tuple[dict[str, Any], list[str]]:
    """
    Validate, coerce, clamp, and migrate a loaded config dictionary without ever raising.
    Returns `(sanitized_config, repaired_fields)`.
    """
    cfg, repaired, _dpapi_failed = _sanitize_user_config_internal(raw_cfg)
    return cfg, repaired


def _extract_fish_key_from_value(raw_val: Any) -> tuple[str, bool]:
    """Extract and DPAPI-decrypt a Fish Audio API key value. Returns `(plain_key, dpapi_failed)`."""
    if not isinstance(raw_val, str):
        return "", False
    s = raw_val.strip()
    if not s:
        return "", False
    if s.startswith(ap.DPAPI_PREFIX):
        dec = ap.unprotect_secret(s)
        if not dec:
            return "", True
        return _clean_single_line_secret(dec), False
    return _clean_single_line_secret(s), False


def salvage_fish_api_key(
    current_cfg: dict[str, Any] | None = None,
    config_path: Path | None = None,
) -> tuple[str, bool]:
    """
    Recover the user's Fish Audio API key safely across live memory, valid JSON, or corrupted JSON.
    Priority:
      1. `current_cfg["fish_api_key"]` (if non-empty and decryptable)
      2. Parsed `config.json["fish_api_key"]`
      3. Regex salvage on raw UTF-8 text of corrupted `config.json` (unescaping JSON & DPAPI-decrypting)
    Returns `(plaintext_key, dpapi_decrypt_failed)`.
    """
    dpapi_failed = False

    if isinstance(current_cfg, dict) and "fish_api_key" in current_cfg:
        key_val, failed = _extract_fish_key_from_value(current_cfg.get("fish_api_key"))
        if key_val:
            return key_val, False
        dpapi_failed = dpapi_failed or failed

    target = Path(config_path) if config_path is not None else ap.get_config_path()
    if target.exists() and not target.is_symlink() and target.is_file():
        raw_text = ""
        is_oversized = False
        try:
            fsize = target.stat().st_size
            if fsize <= ap.MAX_CONFIG_FILE_BYTES:
                with open(target, "rb") as fh:
                    raw_bytes = fh.read(ap.MAX_CONFIG_FILE_BYTES + 1)
                if len(raw_bytes) <= ap.MAX_CONFIG_FILE_BYTES:
                    raw_text = raw_bytes.decode("utf-8-sig", errors="replace")
                else:
                    is_oversized = True
            else:
                is_oversized = True
            if is_oversized:
                # Bounded head read so an oversized/corrupted config can still salvage fish_api_key safely
                with open(target, "rb") as fh:
                    raw_text = fh.read(_MAX_OVERSIZED_SALVAGE_BYTES).decode("utf-8-sig", errors="replace")
        except OSError:
            raw_text = ""

        if raw_text:
            parsed = None
            if not is_oversized:
                try:
                    parsed = json.loads(raw_text)
                except Exception:
                    parsed = None
            if isinstance(parsed, dict) and "fish_api_key" in parsed:
                key_val, failed = _extract_fish_key_from_value(parsed.get("fish_api_key"))
                if key_val:
                    return key_val, False
                dpapi_failed = dpapi_failed or failed
            else:
                m = _FISH_KEY_SALVAGE_RE.search(raw_text)
                if m:
                    captured = m.group(1)
                    try:
                        unescaped = json.loads(f'"{captured}"')
                    except Exception:
                        unescaped = captured
                    key_val, failed = _extract_fish_key_from_value(unescaped)
                    if key_val:
                        return key_val, False
                    dpapi_failed = dpapi_failed or failed

    return "", dpapi_failed


def _safe_read_backup_text(path: Path, max_bytes: int) -> str | None:
    if not path.exists() or path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        return raw.decode("utf-8-sig", errors="replace")
    except OSError:
        return None


def _safe_read_oversized_head(path: Path, max_bytes: int = _MAX_OVERSIZED_BACKUP_HEAD_BYTES) -> str | None:
    if not path.exists() or path.is_symlink() or not path.is_file():
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes)
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return None


def _prune_old_backups(backups_root: Path, max_backups: int) -> None:
    keep = max(1, int(max_backups))
    try:
        entries = [
            p
            for p in backups_root.iterdir()
            if p.is_dir() and not p.is_symlink() and p.name.startswith("settings_backup_")
        ]
    except OSError:
        return
    def _sort_key(p: Path) -> tuple[int, str]:
        try:
            return (p.stat().st_mtime_ns, p.name)
        except OSError:
            return (0, p.name)

    entries.sort(key=_sort_key, reverse=True)
    for old_dir in entries[keep:]:
        try:
            resolved_old = old_dir.resolve()
            resolved_root = backups_root.resolve()
            if resolved_old.parent != resolved_root:
                continue
            shutil.rmtree(resolved_old, ignore_errors=True)
        except Exception:
            pass


def backup_settings_snapshot(
    reason: str = "manual_reset",
    *,
    max_backups: int = DEFAULT_MAX_BACKUPS,
    extra_meta: dict[str, Any] | None = None,
) -> Path | None:
    """
    Create an ACL-hardened, secret-safe snapshot of current user settings files under:
      `%LOCALAPPDATA%\\VoiceCompanionData\\backups\\settings_backup_<UTC>_<reason>\\`
    Covers `config.json`, `rvc_live_config.json`, and user override `selectors.json`.
    Valid JSON backups encrypt secrets via DPAPI; corrupt raw text backups scrub secrets.
    Returns `None` if none of the target settings files exist.
    """
    with ap._CONFIG_IO_LOCK:
        user_dir = ap.get_user_data_dir()
        cfg_path = user_dir / "config.json"
        rvc_path = user_dir / "rvc_live_config.json"
        sel_path = user_dir / "selectors.json"

        candidates: list[tuple[str, Path, int]] = [
            ("config.json", cfg_path, ap.MAX_CONFIG_FILE_BYTES),
            ("rvc_live_config.json", rvc_path, ap.MAX_CONFIG_FILE_BYTES),
            ("selectors.json", sel_path, MAX_SELECTORS_BACKUP_BYTES),
        ]
        existing = [
            (name, p, lim)
            for name, p, lim in candidates
            if p.exists() and not p.is_symlink() and p.is_file()
        ]
        if not existing:
            return None

        backups_root = user_dir / BACKUPS_DIR_NAME
        backups_root.mkdir(parents=True, exist_ok=True)
        ap.harden_user_data_acl(backups_root)

        safe_reason = _SAFE_REASON_RE.sub("_", str(reason or "reset")).strip("_")[:40] or "reset"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = backups_root / f"settings_backup_{stamp}_{safe_reason}"
        suffix = 1
        while backup_dir.exists():
            backup_dir = backups_root / f"settings_backup_{stamp}_{safe_reason}_{suffix:02d}"
            suffix += 1
        backup_dir.mkdir(parents=True, exist_ok=True)
        ap.harden_user_data_acl(backup_dir)

        backed_up: list[str] = []
        skipped: list[str] = []

        for name, src_path, limit in existing:
            raw_text = _safe_read_backup_text(src_path, limit)
            if raw_text is None:
                is_over = False
                try:
                    is_over = src_path.stat().st_size > limit
                except OSError:
                    is_over = False
                if is_over:
                    head_text = _safe_read_oversized_head(src_path)
                    if head_text is not None:
                        head_name = f"{name}.oversized_head.txt"
                        ap._atomic_write_restricted_text(
                            backup_dir / head_name,
                            ap.redact_secrets(head_text),
                            prefix=".bak_",
                        )
                        backed_up.append(head_name)
                    skipped.append(f"{name} (oversized>{limit} bytes; saved redacted head)")
                else:
                    skipped.append(f"{name} (oversized or unreadable)")
                continue

            if name == "config.json":
                try:
                    parsed = json.loads(raw_text)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    try:
                        disk_obj, _ = _sanitize_extra_json_value(ap._prepare_config_for_disk(parsed))
                        payload = json.dumps(disk_obj, ensure_ascii=False, indent=2, allow_nan=False)
                    except (ValueError, OSError):
                        payload = ap.redact_secrets(raw_text)
                else:
                    payload = ap.redact_secrets(raw_text)
            elif name == "rvc_live_config.json":
                try:
                    parsed = json.loads(raw_text)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    try:
                        disk_rvc = dict(parsed)
                        raw_gem_val = disk_rvc.get("gemini_api_key")
                        raw_gem = raw_gem_val.strip() if isinstance(raw_gem_val, str) else ""
                        disk_rvc["gemini_api_key"] = ap.protect_secret(raw_gem) if raw_gem else ""
                        clean_rvc, _ = _sanitize_extra_json_value(disk_rvc)
                        payload = json.dumps(clean_rvc, ensure_ascii=False, indent=2, allow_nan=False)
                    except (ValueError, OSError):
                        payload = ap.redact_secrets(raw_text)
                else:
                    payload = ap.redact_secrets(raw_text)
            else:
                payload = ap.redact_secrets(raw_text)

            ap._atomic_write_restricted_text(backup_dir / name, payload, prefix=".bak_")
            backed_up.append(name)

        meta: dict[str, Any] = {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "reason": safe_reason,
            "backed_up_files": backed_up,
            "skipped_files": skipped,
            "browser_profile_exists": (user_dir / "BrowserProfile").is_dir(),
        }
        if isinstance(extra_meta, dict):
            safe_extra: dict[str, Any] = {}
            for mk, mv in extra_meta.items():
                if not isinstance(mk, str):
                    continue
                if isinstance(mv, float):
                    safe_extra[mk] = mv if math.isfinite(mv) else str(mv)
                elif isinstance(mv, (bool, int)) or mv is None:
                    safe_extra[mk] = mv
                elif isinstance(mv, list):
                    safe_extra[mk] = [ap.redact_secrets(str(x)) for x in mv[:50]]
                else:
                    safe_extra[mk] = ap.redact_secrets(str(mv))
            meta["details"] = safe_extra

        ap._atomic_write_restricted_text(
            backup_dir / "backup_meta.json",
            json.dumps(meta, ensure_ascii=False, indent=2, allow_nan=False),
            prefix=".meta_",
        )
        _prune_old_backups(backups_root, max_backups=max_backups)
        return backup_dir


def _merge_bundled_default_voices(cfg: dict[str, Any], app_root: Path | None) -> None:
    bundled_jp, bundled_kr = ap.load_bundled_default_voices(app_root)
    for cid, vid in bundled_jp.items():
        if not str(cfg["saved_voices"].get(cid) or "").strip():
            cfg["saved_voices"][cid] = vid
    for cid, vid in bundled_kr.items():
        if not str(cfg["saved_voices_kr"].get(cid) or "").strip():
            cfg["saved_voices_kr"][cid] = vid


def load_user_config_with_report(
    app_root: Path | None = None,
    *,
    auto_backup_corrupt: bool = True,
    auto_rewrite_corrupt: bool = True,
) -> ConfigLoadReport:
    """
    Defensively load `%LOCALAPPDATA%\\VoiceCompanionData\\config.json`:
    - Sanitizes every field (never crashes on null, wrong type, NaN/Inf, out-of-range, invalid enum).
    - Auto-upgrades legacy plaintext secrets to Windows DPAPI on disk.
    - On corrupted JSON / non-dict root / unreadable file: salvages `fish_api_key` via regex+DPAPI,
      creates an automatic secret-safe backup, restores clean defaults, and persists recovery cleanly.
    """
    with ap._CONFIG_IO_LOCK:
        cfg_path = ap.get_config_path()

        if not cfg_path.exists():
            cfg = deepcopy(ap.DEFAULT_USER_CONFIG)
            _merge_bundled_default_voices(cfg, app_root)
            return ConfigLoadReport(config=cfg, status="missing")

        if cfg_path.is_symlink() or not cfg_path.is_file():
            cfg = deepcopy(ap.DEFAULT_USER_CONFIG)
            _merge_bundled_default_voices(cfg, app_root)
            return ConfigLoadReport(
                config=cfg,
                status="unreadable",
                error_detail="Refusing to read symlinked or non-regular config.json",
            )

        load_err: Exception | None = None
        loaded: dict[str, Any] | None = None
        is_corrupt_json = False
        is_oversized = False
        raw_text: str | None = None
        for read_attempt in range(3):
            try:
                raw_text = ap._read_bounded_text(cfg_path, ap.MAX_CONFIG_FILE_BYTES)
                load_err = None
                break
            except PermissionError as exc:
                load_err = exc
                if read_attempt < 2:
                    time.sleep(0.03 * (read_attempt + 1))
            except UnicodeDecodeError as exc:
                is_corrupt_json = True
                load_err = exc
                break
            except ValueError as exc:
                load_err = exc
                if "exceeds size limit" in str(exc).lower():
                    is_corrupt_json = True
                    is_oversized = True
                break
            except Exception as exc:
                load_err = exc
                break

        if raw_text is not None:
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, dict):
                    loaded = parsed
                else:
                    is_corrupt_json = True
                    load_err = ValueError(f"config.json root must be a JSON object (got {type(parsed).__name__})")
            except json.JSONDecodeError as exc:
                is_corrupt_json = True
                load_err = exc
            except Exception as exc:
                load_err = exc

        if loaded is None:
            salvaged_key, dpapi_failed = salvage_fish_api_key(config_path=cfg_path)
            backup_dir: Path | None = None
            err_msg = ap.redact_secrets(str(load_err or "Failed to load config.json"))
            if auto_backup_corrupt and is_corrupt_json:
                try:
                    backup_dir = backup_settings_snapshot(
                        reason="oversized_config_recovery" if is_oversized else "corrupt_config_recovery",
                        extra_meta={
                            "error": err_msg,
                            "oversized": is_oversized,
                            "fish_api_key_preserved": bool(salvaged_key),
                            "fish_api_key_dpapi_failed": dpapi_failed,
                        },
                    )
                except Exception:
                    backup_dir = None

            cfg = deepcopy(ap.DEFAULT_USER_CONFIG)
            if salvaged_key:
                cfg["fish_api_key"] = salvaged_key
            _merge_bundled_default_voices(cfg, app_root)

            # NEVER overwrite config.json on transient read errors ("unreadable" / PermissionError)
            if auto_rewrite_corrupt and is_corrupt_json and not cfg_path.is_symlink() and cfg_path.is_file():
                try:
                    ap.save_user_config(cfg)
                except Exception:
                    pass

            return ConfigLoadReport(
                config=cfg,
                status="corrupt_json" if is_corrupt_json else "unreadable",
                repaired_fields=[],
                backup_dir=backup_dir,
                fish_api_key_preserved=bool(salvaged_key),
                fish_api_key_dpapi_failed=dpapi_failed,
                error_detail=err_msg,
            )

        cfg, repaired, fish_dpapi_failed = _sanitize_user_config_internal(loaded)
        _merge_bundled_default_voices(cfg, app_root)

        repaired_backup_dir: Path | None = None
        if repaired:
            if auto_backup_corrupt:
                try:
                    repaired_backup_dir = backup_settings_snapshot(
                        reason="repaired_config_recovery",
                        extra_meta={
                            "repaired_fields": repaired[:50],
                            "fish_api_key_dpapi_failed": fish_dpapi_failed,
                        },
                    )
                except Exception:
                    repaired_backup_dir = None
            if auto_rewrite_corrupt and not cfg_path.is_symlink() and cfg_path.is_file():
                try:
                    ap.save_user_config(cfg)
                except Exception:
                    pass
        elif loaded and os.name == "nt":
            # Transparent legacy plaintext secret upgrade on Windows
            def _is_plaintext_secret(val: Any) -> bool:
                if isinstance(val, (dict, list)):
                    return bool(val)
                if not isinstance(val, str):
                    return False
                s_val = val.strip()
                return bool(s_val) and not s_val.startswith(ap.DPAPI_PREFIX)

            if any(_is_plaintext_secret(loaded.get(k)) for k in ap.SECRET_CONFIG_KEYS):
                try:
                    migrated_disk, _ = _sanitize_extra_json_value(ap._prepare_config_for_disk(loaded))
                    ap._atomic_write_restricted_text(
                        cfg_path,
                        json.dumps(migrated_disk, ensure_ascii=False, indent=2, allow_nan=False),
                        prefix=".cfg_",
                    )
                except Exception:
                    pass

        status: ConfigLoadStatus = "repaired_fields" if repaired else "ok"
        return ConfigLoadReport(
            config=cfg,
            status=status,
            repaired_fields=repaired,
            backup_dir=repaired_backup_dir,
            fish_api_key_preserved=bool(cfg.get("fish_api_key")),
            fish_api_key_dpapi_failed=fish_dpapi_failed,
            error_detail="",
        )


def reset_browser_profile(user_data_dir: Path | None = None) -> tuple[bool, str]:
    """
    Safely clear `%LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile` without touching
    `config.json`, Fish Audio API key, or `memory/`.
    Returns `(True, korean_msg)` when cleared or already absent, or `(False, korean_lock_hint)`
    when Windows holds open file locks (e.g. an AI Edge/Chrome window is still open).
    """
    with ap._CONFIG_IO_LOCK:
        try:
            base_raw = Path(user_data_dir).expanduser() if user_data_dir is not None else ap.get_user_data_dir()
            if "\x00" in str(base_raw):
                return False, "잘못된 사용자 데이터 경로(NUL 문자 포함)입니다."
            base_dir = base_raw.resolve()
        except Exception as exc:
            return False, f"사용자 데이터 폴더를 확인할 수 없습니다: {ap.redact_secrets(str(exc))}"

        profile_dir = base_dir / "BrowserProfile"
        if profile_dir.is_symlink():
            return False, "BrowserProfile이 심볼릭 링크로 설정되어 있어 안전을 위해 초기화를 건너뛰었습니다."
        if not profile_dir.exists():
            return (
                True,
                "초기화할 브라우저 프로필이 없어 이미 깨끗한 상태입니다.\n"
                "다음 통화 시작 시 새 브라우저 로그인 창이 열립니다.",
            )
        if not profile_dir.is_dir():
            return False, "BrowserProfile 경로가 정상 폴더가 아니어서 초기화하지 못했습니다."

        try:
            resolved_profile = profile_dir.resolve()
            if resolved_profile.parent != base_dir or resolved_profile.name != "BrowserProfile":
                return False, "BrowserProfile 경로 검증에 실패해 초기화를 중단했습니다."
        except OSError as exc:
            return False, f"BrowserProfile 경로 확인 중 오류가 발생했습니다: {ap.redact_secrets(str(exc))}"

        had_perm_error = False
        last_err: Exception | None = None

        def _handle_remove_err(func: Any, path_str: str, exc_info: Any) -> None:
            nonlocal had_perm_error, last_err
            try:
                mode = 0o700 if os.path.isdir(path_str) else 0o600
                os.chmod(path_str, mode)
                func(path_str)
                return
            except PermissionError as pexc:
                had_perm_error = True
                last_err = pexc
            except OSError as oexc:
                if getattr(oexc, "winerror", None) in (5, 32, 33):
                    had_perm_error = True
                last_err = oexc

        rmtree_err_kw: dict[str, Any] = (
            {"onexc": _handle_remove_err} if sys.version_info >= (3, 12) else {"onerror": _handle_remove_err}
        )
        for attempt in range(4):
            had_perm_error = False
            last_err = None
            try:
                shutil.rmtree(resolved_profile, **rmtree_err_kw)
            except PermissionError as exc:
                had_perm_error = True
                last_err = exc
            except OSError as exc:
                if getattr(exc, "winerror", None) in (5, 32, 33):
                    had_perm_error = True
                last_err = exc
            if not resolved_profile.exists():
                return (
                    True,
                    "AI 전용 브라우저 프로필(BrowserProfile)을 깨끗하게 초기화했습니다.\n"
                    "다음 통화 시작 시 새 브라우저 로그인 창이 열립니다.\n"
                    "(Fish Audio API 키 · 앱 설정 · 대화 기억은 그대로 유지됩니다.)",
                )
            if attempt < 3:
                time.sleep(0.08 * (attempt + 1))

        if had_perm_error or isinstance(last_err, PermissionError):
            return (
                False,
                "AI 전용 브라우저(Edge/Chrome) 창이 아직 실행 중이어서 BrowserProfile을 초기화할 수 없습니다.\n"
                "열려 있는 Gemini/Claude/ChatGPT 전용 브라우저 창(또는 작업 관리자의 관련 Edge/Chrome 프로세스)을 "
                "모두 닫은 뒤 다시 시도해 주세요.",
            )
        detail = ap.redact_secrets(str(last_err or "알 수 없는 파일 시스템 오류"))
        return False, f"브라우저 프로필 초기화 중 오류가 발생했습니다: {detail}"


def reset_settings_to_defaults(
    *,
    app_root: Path | None = None,
    current_cfg: dict[str, Any] | None = None,
    reason: str = "manual_reset",
    preserve_fish_api_key: bool = True,
    reset_rvc_config: bool = True,
    reset_user_selectors: bool = True,
    include_browser_profile: bool = False,
) -> SettingsResetResult:
    """
    Restore all user settings to clean defaults while strictly preserving:
      - Fish Audio API key (`fish_api_key`, DPAPI-encrypted at rest)
      - AI browser login profile (`%LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile\\`, untouched unless `include_browser_profile=True`)
      - Non-settings user data (`memory\\`, `models\\`, `logs\\`, `cache\\`, `tts_cache\\`, etc.)

    Automatically backs up existing `config.json`, `rvc_live_config.json`, and user `selectors.json`
    under `%LOCALAPPDATA%\\VoiceCompanionData\\backups\\` before resetting.
    """
    with ap._CONFIG_IO_LOCK:
        user_dir = ap.get_user_data_dir()
        cfg_path = user_dir / "config.json"
        browser_profile_exists = (user_dir / "BrowserProfile").is_dir()
        warnings: list[str] = []
        reset_files: list[str] = []

        # 1. Salvage Fish Audio API key BEFORE modifying any file on disk
        preserved_fish_key = ""
        dpapi_failed = False
        if preserve_fish_api_key:
            preserved_fish_key, dpapi_failed = salvage_fish_api_key(
                current_cfg=current_cfg,
                config_path=cfg_path,
            )
            if dpapi_failed and not preserved_fish_key:
                warnings.append(
                    "기존 Fish Audio API 키의 Windows 암호화(DPAPI)를 복호화할 수 없어 "
                    "키를 복원하지 못했습니다. 설정 1단계에서 다시 입력해 주세요."
                )

        # 2. Automatic secret-safe backup before resetting
        backup_dir: Path | None = None
        try:
            backup_dir = backup_settings_snapshot(
                reason=reason,
                extra_meta={
                    "fish_api_key_preserved": bool(preserved_fish_key),
                    "preserve_fish_api_key_requested": bool(preserve_fish_api_key),
                    "reset_rvc_config": bool(reset_rvc_config),
                    "reset_user_selectors": bool(reset_user_selectors),
                    "include_browser_profile": bool(include_browser_profile),
                },
            )
        except Exception as exc:
            warnings.append(f"기존 설정 백업 중 경고: {ap.redact_secrets(str(exc))}")

        # 3. Build pristine defaults + bundled default voices + preserved Fish API key ONLY
        new_cfg = deepcopy(ap.DEFAULT_USER_CONFIG)
        _merge_bundled_default_voices(new_cfg, app_root)
        new_cfg["fish_api_key"] = preserved_fish_key if preserve_fish_api_key else ""
        # Explicitly guarantee secondary credentials are cleared & Gemini web default mode is active
        new_cfg["gemini_api_key"] = ""
        new_cfg["claude_api_key"] = ""
        new_cfg["openai_api_key"] = ""
        new_cfg["fish_mcp_oauth_data"] = ""
        new_cfg["colab_s2pro_enabled"] = False
        new_cfg["fish_model_header"] = "s2.1-pro-free"
        new_cfg["colab_s2pro_url"] = ""
        new_cfg["colab_s2pro_api_key"] = ""
        new_cfg["colab_s2pro_drive_folder_id"] = ""
        new_cfg["colab_s2pro_notebook_id"] = ""
        new_cfg["colab_s2pro_endpoint_file"] = ""
        new_cfg["ai_provider"] = "gemini"
        new_cfg["gemini_backend"] = "browser"
        new_cfg["gemini_browser_model"] = "hydrogen"
        new_cfg["gemini_thinking_level"] = "low"
        new_cfg["whisper_model"] = "auto"

        # 4. Atomically write fresh config.json
        ap.save_user_config(new_cfg)
        reset_files.append("config.json")

        # 5. Remove user selectors.json override (falls back cleanly to bundled config/selectors.json)
        if reset_user_selectors:
            user_sel = user_dir / "selectors.json"
            if user_sel.exists():
                if user_sel.is_symlink() or not user_sel.is_file():
                    warnings.append("사용자 selectors.json 이 심볼릭 링크/비정규 파일이어서 건너뛰었습니다.")
                else:
                    try:
                        user_sel.unlink()
                        reset_files.append("selectors.json")
                    except OSError as exc:
                        warnings.append(f"사용자 selectors.json 초기화 실패: {ap.redact_secrets(str(exc))}")

        # 6. Remove rvc_live_config.json by path only (zero import of src.rvc_live)
        if reset_rvc_config:
            rvc_cfg = user_dir / "rvc_live_config.json"
            if rvc_cfg.exists():
                if rvc_cfg.is_symlink() or not rvc_cfg.is_file():
                    warnings.append("rvc_live_config.json 이 심볼릭 링크/비정규 파일이어서 건너뛰었습니다.")
                else:
                    try:
                        rvc_cfg.unlink()
                        reset_files.append("rvc_live_config.json")
                    except OSError as exc:
                        warnings.append(f"rvc_live_config.json 초기화 실패: {ap.redact_secrets(str(exc))}")

        # 7. Optional BrowserProfile reset (default False — preserves AI browser logins unless requested)
        browser_profile_preserved = True
        if include_browser_profile:
            bp_ok, bp_msg = reset_browser_profile(user_data_dir=user_dir)
            browser_profile_exists = (user_dir / "BrowserProfile").is_dir()
            if bp_ok:
                browser_profile_preserved = False
                reset_files.append("BrowserProfile")
            else:
                warnings.append(bp_msg)

        return SettingsResetResult(
            config=new_cfg,
            backup_dir=backup_dir,
            fish_api_key_preserved=bool(preserved_fish_key),
            browser_profile_preserved=browser_profile_preserved,
            browser_profile_exists=browser_profile_exists,
            reset_files=reset_files,
            warnings=warnings,
        )
