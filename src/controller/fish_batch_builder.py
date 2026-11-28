"""
Full-Automation Blue Archive -> Fish Audio Batch Voice Builder & Account Sync Engine
------------------------------------------------------------------------------------
UI-independent core module (zero tkinter dependencies) powering both the CLI
(`tools/batch_create_fish_voices.py`) and the in-app MomoTalk batch dialog.

2-Stage Ready-Before-Payment Workflow:
  - Stage A (`extract_only`, $0 API cost — runnable BEFORE paying for Fish Audio):
      Extracts & masters studio v2 44.1kHz reference WAVs + official transcripts for all
      157 JP / 148 KR voiced Blue Archive characters directly from Steam `.molru` packs.
  - Stage B (`upload`, runnable right after Fish Audio subscription):
      1. Syncs existing models already on the user's Fish Audio account (`GET /model?self=true`)
         to guarantee ZERO duplicate model creation.
      2. Uploads only missing characters with studio WAVs + official transcripts + Steam
         portrait `cover_image`, persisting `saved_voices` / `saved_voices_kr` after EVERY
         single model for 100% crash-safe resume.
      3. Handles HTTP 429 (`Retry-After` + exponential backoff), transient 5xx retries, and
         graceful stop on HTTP 401/402/403 (auth or plan slot/credit limit reached).
"""

from __future__ import annotations

import copy
import datetime
from email.utils import parsedate_to_datetime
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse

import numpy as np
import requests
import soundfile as sf

from src.extractor.ba_molru_extractor import (
    DEFAULT_GAME_DIR,
    is_safe_entity_id,
    is_valid_ba_game_dir,
    resolve_ba_game_dir,
)
from src.extractor.ba_momotalk_assets import MomoTalkAssetManager
from src.extractor.build_fish_reference import (
    DEFAULT_QUALITY_PROFILE,
    INTER_CLIP_PAUSE_SEC,
    REFERENCE_QUALITY_PROFILES,
    STUDIO_PIPELINE_VERSION,
    TARGET_SR,
    apply_cosine_fade,
    build_alt_voice_char_data,
    build_character_fish_reference,
    character_has_steam_voice_pack,
    compute_clip_filter_hash,
    compute_extraction_config_hash,
    find_character_voice_molru_files,
    get_gamedata_signature,
    resample_audio,
    resolve_quality_profile,
    select_upload_samples_with_transcript_lock,
)
from src.utils.app_paths import (
    get_app_root,
    get_user_data_dir,
    get_user_models_dir,
    load_user_config,
    save_user_config,
)

FISH_MODEL_URL = "https://api.fish.audio/model"
FISH_MAX_UPLOAD_FILES = 20
VALID_VISIBILITIES: tuple[str, ...] = ("private", "unlist", "public")

# Allow nested parentheses inside Korean/JP character names (e.g. "텐도 케이 (Key) / KR ...")
_TITLE_IDENTITY_RE = re.compile(
    r"^Blue\s+Archive\s*-\s*([A-Za-z0-9_]+)\s*\(.*?/\s*(JP|KR)\b",
    re.IGNORECASE,
)
_FISH_HEX_STRICT_RE = re.compile(r"\A[0-9a-fA-F]{32}\Z")
_FISH_MODEL_PATH_RE = re.compile(r"\A/(?:[a-zA-Z_-]{2,10}/)?m/([0-9a-fA-F]{32})/?\Z")
_ALLOWED_FISH_HOSTS = frozenset({"fish.audio", "www.fish.audio", "api.fish.audio"})
_MIN_FREE_DISK_BYTES = 500 * 1024 * 1024  # 500 MB hard floor
_WARN_FREE_DISK_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB warning threshold for full 305-char pro85 run
MAX_FISH_JSON_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MB cap on Fish Audio JSON responses
MAX_CHAR_CONFIG_BYTES = 5 * 1024 * 1024         # 5 MB cap per character JSON
MAX_MANIFEST_JSON_BYTES = 8 * 1024 * 1024       # 8 MB cap on fish_batch_manifest.json

_BATCH_PERSIST_LOCK = threading.RLock()


def _as_dict(val: Any) -> dict[str, Any]:
    return val if isinstance(val, dict) else {}


def _ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    val = parent.get(key)
    if isinstance(val, dict):
        return val
    d: dict[str, Any] = {}
    parent[key] = d
    return d


def _safe_int(val: Any, default: int = 0) -> int:
    if isinstance(val, bool) or val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if isinstance(val, bool) or val is None:
        return default
    try:
        v = float(val)
        return v if math.isfinite(v) else default
    except (ValueError, TypeError):
        return default

_FALLBACK_REDACT_PATS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"\bsk-[0-9A-Za-z_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)(Bearer\s+)[^\s\"',;}\]]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:x-goog-api-key|x-api-key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}\]]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:api_)?key=)[^&\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r'("(?:fish_api_key|gemini_api_key|claude_api_key|openai_api_key)"\s*:\s*")[^"]*(")'), r"\1[REDACTED]\2"),
    (re.compile(r"dpapi:[A-Za-z0-9+/=]{16,}"), "dpapi:[REDACTED]"),
)


def _redact_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Scrub API keys, Bearer tokens, and DPAPI blobs using `app_paths.redact_secrets` with inline fallback."""
    s = str(text or "")
    if not s:
        return s
    try:
        from src.utils import app_paths as _ap

        fn = getattr(_ap, "redact_secrets", None)
        if callable(fn):
            return str(fn(s, extra_secrets=extra_secrets))
    except Exception:
        pass
    out = s
    for sec in extra_secrets:
        tok = str(sec or "").strip()
        if len(tok) >= 8:
            out = out.replace(tok, "[REDACTED]")
    for pat, repl in _FALLBACK_REDACT_PATS:
        out = pat.sub(repl, out)
    return out


def _validate_fish_api_url(url: str) -> str:
    """Strictly enforce HTTPS-only requests to `api.fish.audio` (prevents SSRF / scheme downgrade / traversal)."""
    raw = str(url or "").strip()
    if not raw or "\\" in raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError(f"Blocked malformed or control-char URL: {url!r}")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or host != "api.fish.audio"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or "\\" in decoded_path
        or ".." in decoded_path.split("/")
    ):
        raise ValueError(f"Blocked non-Fish-Audio or insecure URL: {url!r}")
    return raw


def is_valid_fish_model_id(raw: str) -> bool:
    """Return True iff `raw` is a strictly valid 32-char hexadecimal Fish Audio model ID."""
    return bool(_FISH_HEX_STRICT_RE.match(str(raw or "").strip()))


def build_fish_model_item_url(model_id: str) -> str:
    """Build a strictly validated `https://api.fish.audio/model/<32-hex>` URL (prevents SSRF/path injection)."""
    clean_id = _clean_model_id(model_id)
    if not clean_id:
        raise ValueError(f"Invalid Fish Audio model ID: {model_id!r}")
    return _validate_fish_api_url(f"{FISH_MODEL_URL}/{clean_id}")


def _is_real_requests(sess: Any) -> bool:
    return sess is requests or isinstance(sess, requests.Session)


def _read_bounded_response(resp: Any, max_bytes: int = MAX_FISH_JSON_RESPONSE_BYTES) -> tuple[int, str, Any, Any]:
    """Read a requests or mock HTTP response safely with strict OOM-DoS byte cap."""
    code = int(getattr(resp, "status_code", 0) or 0)
    resp_headers = getattr(resp, "headers", None) or {}
    if hasattr(resp, "iter_content"):
        try:
            cl = resp_headers.get("Content-Length") or resp_headers.get("content-length")
            if cl is not None and str(cl).strip().isdigit() and int(str(cl).strip()) > max_bytes:
                raise ValueError(f"Fish Audio response Content-Length exceeds limit ({cl} > {max_bytes})")
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise ValueError(f"Fish Audio response body exceeds limit ({max_bytes} bytes)")
            text = buf.decode("utf-8", errors="replace")
            try:
                body = json.loads(text) if text else {}
            except Exception:
                body = None
            return code, text, body, resp_headers
        finally:
            try:
                resp.close()
            except Exception:
                pass
    raw_text = str(getattr(resp, "text", "") or "")
    if len(raw_text.encode("utf-8", errors="ignore")) > max_bytes:
        raise ValueError(f"Fish Audio response text exceeds limit ({max_bytes} bytes)")
    try:
        body = resp.json()
    except Exception:
        body = None
    return code, raw_text, body, resp_headers


def build_fish_model_title(char_id: str, name_tag: str, lang: str, cv_name: str = "") -> str:
    """Build a clean canonical Fish Audio title guaranteed to round-trip via `_TITLE_IDENTITY_RE`."""
    lang_up = (lang or "JP").strip().upper()
    clean_name = re.sub(r"[\r\n]+", " ", str(name_tag or char_id)).strip()
    clean_cv = re.sub(r"[\r\n]+", " ", str(cv_name or "")).strip()
    if clean_cv and clean_cv.lower() not in (f"{lang_up.lower()} voice", "unknown", "미정", "없음"):
        suffix = f"{lang_up} {clean_cv}"
    else:
        suffix = lang_up
    return f"Blue Archive - {char_id} ({clean_name} / {suffix})"


def _clean_model_id(raw: str) -> str:
    """
    Strictly extract and validate a Fish Audio 32-char hex model ID from either a raw ID
    or an official `https://fish.audio/.../m/<32-hex>` URL. Rejects traversal/SSRF inputs with `""`.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if _FISH_HEX_STRICT_RE.match(s):
        return s.lower()
    candidate = s
    if "://" not in candidate and candidate.lower().startswith(("fish.audio/", "www.fish.audio/", "api.fish.audio/")):
        candidate = "https://" + candidate
    if "://" in candidate:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme == "https"
            and host in _ALLOWED_FISH_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        ):
            m = _FISH_MODEL_PATH_RE.match(parsed.path)
            if m:
                return m.group(1).lower()
    return ""


def _atomic_write_json(target: Path, data: Any) -> Path:
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite symlink file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=".fbatch_", suffix=".tmp", dir=str(target.parent))
    fd_closed = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
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


def get_pre_batch_backup_path() -> Path:
    return get_user_data_dir() / "saved_voices_pre_batch_backup.json"


def backup_saved_voices_once(cfg: dict[str, Any], log_cb: Callable[[str], None] | None = None) -> Path | None:
    """
    Create a one-time safety backup of pre-existing `saved_voices` / `saved_voices_kr` before
    replacing any external/community Fish Audio links with newly created self-owned models.
    Never overwrites an already-created backup file.
    """
    backup_path = get_pre_batch_backup_path()
    with _BATCH_PERSIST_LOCK:
        if backup_path.is_file():
            return backup_path
        saved_jp = dict(_as_dict(cfg.get("saved_voices")))
        saved_kr = dict(_as_dict(cfg.get("saved_voices_kr")))
        if not saved_jp and not saved_kr:
            return None
        snapshot = {
            "backed_up_at": time.time(),
            "saved_voices": saved_jp,
            "saved_voices_kr": saved_kr,
            "fish_reference_id": str(cfg.get("fish_reference_id") or ""),
        }
        _atomic_write_json(backup_path, snapshot)
    if callable(log_cb):
        log_cb(
            f"💾 [안전 백업] 기존 연결 보이스 (JP {len(saved_jp)}개 · KR {len(saved_kr)}개)를 "
            f"'{backup_path.name}'에 안전 백업했습니다."
        )
    return backup_path


def _sync_active_reference_id(cfg: dict[str, Any], char_id: str, lang_up: str, model_id: str) -> None:
    """Keep `fish_reference_id` synchronized when the currently selected student/language voice updates."""
    sel_cid = str(cfg.get("selected_character_id") or "").strip()
    rlang = "ko" if str(cfg.get("reply_language") or "ja").strip().lower() == "ko" else "ja"
    target_rlang = "ko" if lang_up.upper() == "KR" else "ja"
    if sel_cid and sel_cid.lower() == char_id.lower() and rlang == target_rlang:
        cfg["fish_reference_id"] = model_id


def _update_manifest_entry_locked(
    entry_key: str,
    patch: dict[str, Any],
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Thread-safe read-merge-write of a single manifest entry without losing concurrent updates."""
    with _BATCH_PERSIST_LOCK:
        disk_m = load_batch_manifest()
        disk_entries = _ensure_dict(disk_m, "entries")
        if manifest is not None:
            mem_entries = _ensure_dict(manifest, "entries")
            for dk, dv in disk_entries.items():
                if dk not in mem_entries and isinstance(dv, dict):
                    mem_entries[dk] = dv
            target_m = manifest
            entries = mem_entries
        else:
            target_m = disk_m
            entries = disk_entries
        prev_ent = _as_dict(entries.get(entry_key))
        entries[entry_key] = {**prev_ent, **patch}
        save_batch_manifest(target_m)
        return target_m


def record_self_owned_model(
    char_id: str,
    name_kr: str,
    lang: str,
    model_id: str,
    visibility: str = "private",
    quality_profile: str = DEFAULT_QUALITY_PROFILE,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a model created on the user's own Fish Audio account in `fish_batch_manifest.json`."""
    lang_up = lang.upper()
    key = f"{char_id}::{lang_up}"
    return _update_manifest_entry_locked(
        key,
        {
            "char_id": char_id,
            "name_kr": name_kr,
            "lang": lang_up,
            "quality_profile": quality_profile,
            "extracted": True,
            "uploaded": True,
            "owned_by_self": True,
            "model_id": _clean_model_id(model_id),
            "visibility": visibility,
            "error": "",
            "updated_at": time.time(),
        },
        manifest=manifest,
    )


def get_batch_manifest_path() -> Path:
    return get_user_data_dir() / "fish_batch_manifest.json"


def load_batch_manifest() -> dict[str, Any]:
    path = get_batch_manifest_path()
    default: dict[str, Any] = {"schema_version": 1, "updated_at": 0.0, "entries": {}}
    with _BATCH_PERSIST_LOCK:
        if path.is_symlink() or not path.is_file():
            return default
        try:
            if path.stat().st_size > MAX_MANIFEST_JSON_BYTES:
                return default
            with open(path, "rb") as fh:
                raw_bytes = fh.read(MAX_MANIFEST_JSON_BYTES + 1)
            if len(raw_bytes) > MAX_MANIFEST_JSON_BYTES:
                return default
            raw = json.loads(raw_bytes.decode("utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                default.update(raw)
                default["entries"] = {
                    str(k): v for k, v in raw["entries"].items() if isinstance(v, dict)
                }
        except Exception:
            pass
    return default


def save_batch_manifest(manifest: dict[str, Any]) -> Path:
    with _BATCH_PERSIST_LOCK:
        _ensure_dict(manifest, "entries")
        manifest["schema_version"] = 1
        manifest["updated_at"] = time.time()
        return _atomic_write_json(get_batch_manifest_path(), manifest)


def load_all_character_configs(config_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    cdir = config_dir or (get_app_root() / "config" / "characters")
    chars: dict[str, dict[str, Any]] = {}
    if not cdir.is_dir():
        return chars
    cdir_res = cdir.resolve()
    for fp in sorted(cdir.glob("*.json")):
        try:
            if fp.is_symlink() or not fp.resolve().is_relative_to(cdir_res):
                continue
            if fp.stat().st_size > MAX_CHAR_CONFIG_BYTES:
                continue
            data = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            cid = str(data.get("id") or "").strip()
            if cid and is_safe_entity_id(cid):
                chars[cid] = data
        except Exception:
            continue
    return chars


@dataclass
class BatchVoiceJob:
    char_id: str
    name_kr: str
    name_jp: str
    lang: str  # "JP" or "KR"
    reply_lang: str  # "ja" or "ko"
    molru_files: list[Path]
    already_extracted: bool
    existing_model_id: str
    char_data: dict[str, Any]
    owned_by_self: bool = False

    @property
    def key(self) -> str:
        return f"{self.char_id}::{self.lang}"


@dataclass
class BatchProgress:
    phase: str  # "scan" | "sync" | "extract" | "upload" | "done" | "stopped" | "error"
    current: int
    total: int
    char_id: str = ""
    name_kr: str = ""
    lang: str = ""
    status: str = ""
    message: str = ""
    counts: dict[str, int] = field(default_factory=dict)


def _char_ref_dir(char_id: str, lang: str) -> tuple[Path, Path]:
    cid = str(char_id or "").strip()
    if not is_safe_entity_id(cid):
        raise ValueError(f"Invalid or unsafe character_id: {char_id!r}")
    lang_up = str(lang or "JP").strip().upper()
    if lang_up not in ("JP", "KR"):
        raise ValueError(f"Invalid voice language code: {lang!r}")
    models_root = get_user_models_dir().resolve()
    out_dir = (models_root / cid / lang_up).resolve()
    if not out_dir.is_relative_to(models_root):
        raise ValueError(f"Path traversal blocked for character_id={cid!r}")
    samples_dir = (out_dir / "fish_samples").resolve()
    return out_dir / f"{cid}_{lang_up}_FishAudio_Reference.wav", samples_dir


def is_studio_v2_extracted(
    char_id: str,
    lang: str,
    quality_profile: str | None = None,
    char_data: dict[str, Any] | None = None,
) -> bool:
    comb_wav, samples_dir = _char_ref_dir(char_id, lang)
    if not samples_dir.is_dir():
        return False
    meta_file = samples_dir / "studio_v2_meta.json"
    if not meta_file.is_file():
        return False
    wavs = list(samples_dir.glob("*.wav"))
    if not (wavs and comb_wav.is_file()):
        return False
    try:
        # Ensure WAV files are non-empty (> 44-byte RIFF header)
        if comb_wav.stat().st_size <= 44:
            return False
        if any(w.stat().st_size <= 44 for w in wavs):
            return False
    except OSError:
        return False
    if meta_file.is_symlink():
        return False
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(meta, dict):
        return False
    pipe_ver = _safe_int(meta.get("pipeline_version"), 0)
    if pipe_ver < STUDIO_PIPELINE_VERSION:
        return False
    if quality_profile:
        want_prof, _ = resolve_quality_profile(quality_profile)
        saved_prof = str(meta.get("quality_profile") or "").strip().lower()
        if saved_prof != want_prof:
            return False
    if isinstance(char_data, dict):
        want_flt_hash = compute_clip_filter_hash(char_data)
        saved_flt_hash = str(meta.get("clip_filter_hash") or "")
        if saved_flt_hash != want_flt_hash:
            return False
        if "extraction_config_hash" in meta:
            want_cfg_hash = compute_extraction_config_hash(char_data, lang=lang)
            if str(meta.get("extraction_config_hash") or "") != want_cfg_hash:
                return False
    cur_gd_sig = get_gamedata_signature(lang=lang)
    if cur_gd_sig:
        if "gamedata_sig" in meta and str(meta.get("gamedata_sig") or "") != cur_gd_sig:
            return False
        if "gamedata_available" in meta and not bool(meta.get("gamedata_available")):
            return False
    return True


def _normalize_char_filter(character_filter: Iterable[str] | None) -> set[str]:
    out: set[str] = set()
    for item in character_filter or []:
        for tok in str(item or "").split(","):
            t = tok.strip().lower()
            if t:
                out.add(t)
    return out


def _matches_char_filter(char_data: dict[str, Any], flt: set[str]) -> bool:
    if not flt:
        return True
    cid = str(char_data.get("id") or "").strip().lower()
    parent_cid = str(char_data.get("parent_char_id") or "").strip().lower()
    alt_key = str(char_data.get("alt_voice_key") or "").strip().lower()
    name_kr = str(char_data.get("name_kr") or "").strip().lower()
    name_jp = str(char_data.get("name_jp") or "").strip().lower()
    kr_parts = [p for p in re.split(r"[\s()（）]+", name_kr) if p]
    if cid in flt or (parent_cid and parent_cid in flt) or (alt_key and alt_key in flt) or name_kr in flt or name_jp in flt:
        return True
    if any(p in flt for p in kr_parts):
        return True
    return False


def _iter_voice_targets(characters: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any], bool]]:
    """
    Return ordered `(voice_slot_id, char_data, is_alt_voice)` targets across all base characters
    plus any declared `alt_voice_personas` (e.g. Kisaki -> `Kisaki_Kiki`).
    """
    targets: list[tuple[str, dict[str, Any], bool]] = []
    for cid in sorted(characters.keys(), key=lambda k: k.lower()):
        ch = characters[cid]
        targets.append((cid, ch, False))
        alts = ch.get("alt_voice_personas")
        if isinstance(alts, dict):
            for alt_key in sorted(alts.keys()):
                alt_ch = build_alt_voice_char_data(ch, alt_key)
                if alt_ch is not None:
                    targets.append((str(alt_ch["id"]), alt_ch, True))
    return targets


def scan_voiced_roster(
    characters: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    langs: tuple[str, ...] = ("JP", "KR"),
    game_dir: Path | None = None,
    character_filter: Iterable[str] | None = None,
    quality_profile: str = DEFAULT_QUALITY_PROFILE,
    manifest: dict[str, Any] | None = None,
    self_owned_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Scan all Blue Archive characters (including `alt_voice_personas` such as `Kisaki_Kiki`)
    for available Steam `.molru` packs across requested languages plus local extraction,
    self-owned Fish Audio status, and pre-existing external link status.
    """
    game_dir = resolve_ba_game_dir(game_dir, cfg)
    prof_key, _ = resolve_quality_profile(quality_profile)
    norm_langs = tuple(l.upper() for l in langs if l.upper() in ("JP", "KR")) or ("JP", "KR")
    flt = _normalize_char_filter(character_filter)
    saved_jp = _as_dict(cfg.get("saved_voices"))
    saved_kr = _as_dict(cfg.get("saved_voices_kr"))
    man = manifest if manifest is not None else load_batch_manifest()
    man_entries = _as_dict(man.get("entries")) if isinstance(man, dict) else {}
    owned_remote_set = {(str(c).lower(), str(l).upper()) for c, l in (self_owned_keys or set())}

    jobs: list[BatchVoiceJob] = []
    unvoiced: list[dict[str, str]] = []
    summary = {
        "total_chars": 0,
        "alt_voice_slots": 0,
        "jp_voiced": 0,
        "kr_voiced": 0,
        "jp_linked": 0,
        "kr_linked": 0,
        "jp_self_owned": 0,
        "kr_self_owned": 0,
        "jp_external_linked": 0,
        "kr_external_linked": 0,
        "jp_extracted": 0,
        "kr_extracted": 0,
        "quality_profile": prof_key,
    }

    for cid, ch, is_alt in _iter_voice_targets(characters):
        if not _matches_char_filter(ch, flt):
            continue
        if is_alt:
            summary["alt_voice_slots"] += 1
        else:
            summary["total_chars"] += 1
        any_voice = False
        for lang_up in norm_langs:
            molrus = find_character_voice_molru_files(ch, lang=lang_up, game_dir=game_dir)
            extracted = is_studio_v2_extracted(cid, lang_up, quality_profile=prof_key, char_data=ch)
            if not molrus and not extracted and not _has_any_local_wavs(cid, lang_up):
                continue
            any_voice = True
            rlang = "ko" if lang_up == "KR" else "ja"
            existing_id = _clean_model_id(str((saved_kr if lang_up == "KR" else saved_jp).get(cid) or ""))

            entry = _as_dict(man_entries.get(f"{cid}::{lang_up}"))
            man_mid = _clean_model_id(str(entry.get("model_id") or ""))
            man_owned = bool(entry.get("owned_by_self") or entry.get("uploaded")) and bool(man_mid)
            owned_by_self = (cid.lower(), lang_up) in owned_remote_set or (
                man_owned and (not existing_id or existing_id == man_mid)
            )
            external_linked = bool(existing_id) and (not owned_by_self)

            if lang_up == "JP":
                summary["jp_voiced"] += 1
                if existing_id:
                    summary["jp_linked"] += 1
                if owned_by_self:
                    summary["jp_self_owned"] += 1
                if external_linked:
                    summary["jp_external_linked"] += 1
                if extracted:
                    summary["jp_extracted"] += 1
            else:
                summary["kr_voiced"] += 1
                if existing_id:
                    summary["kr_linked"] += 1
                if owned_by_self:
                    summary["kr_self_owned"] += 1
                if external_linked:
                    summary["kr_external_linked"] += 1
                if extracted:
                    summary["kr_extracted"] += 1
            jobs.append(
                BatchVoiceJob(
                    char_id=cid,
                    name_kr=str(ch.get("name_kr") or cid),
                    name_jp=str(ch.get("name_jp") or cid),
                    lang=lang_up,
                    reply_lang=rlang,
                    molru_files=molrus,
                    already_extracted=extracted,
                    existing_model_id=existing_id,
                    char_data=ch,
                    owned_by_self=owned_by_self,
                )
            )
        if (not any_voice) and (not is_alt):
            unvoiced.append(
                {
                    "id": cid,
                    "name_kr": str(ch.get("name_kr") or cid),
                    "category": str(ch.get("category") or ""),
                }
            )

    return {"jobs": jobs, "unvoiced": unvoiced, "summary": summary}


_NAME_NORM_STRIP_RE = re.compile(r"[\s_\-·•*＊()（）\[\]{}/／:,.'\"`~!@#$%^&+=|\\]+")
_TITLE_PREFIX_STRIP_RE = re.compile(
    r"^(?:(?:blue\s*archive|블루\s*아카이브|블루아카|블아|ブルーアーカイブ|ブルアカ)\s*[-–—:|/\s]+|\d{1,3}[_.\-\s]+)+",
    re.IGNORECASE,
)
_TITLE_REF_SUFFIX_STRIP_RE = re.compile(r"(?:[_\-\s]+fishaudio[_\-\s]*reference)?(?:\.wav)?$", re.IGNORECASE)
_TITLE_LANG_SUFFIX_RE = re.compile(
    r"""(?ix)
    (?:
        [\s_\-–—/|,:]*[\(\[（【]\s*(?P<br_lang>jp|ja|jpn|japanese|일음|일본어|일본|일|kr|ko|kor|korean|한음|한국어|한국|더빙|한)\s*[\)\]）】]
        |
        (?:[\s_\-–—/|,:]+)(?P<sp_lang>jp|ja|jpn|japanese|일음|일본어|일본|kr|ko|kor|korean|한음|한국어|한국|더빙)
    )\s*$
    """
)
_EXPLICIT_CHAR_ALIASES: dict[str, tuple[str, ...]] = {
    "Shiroko_Terror": ("시로코테러", "시로코*테러", "시로코 테러", "시로코(테러)", "쿠로코", "シロコテラー", "クロコ", "kuroko", "shirokoterror"),
    "Shun_Small": ("어린슌", "어린 슌", "유녀슌", "슌유녀", "슌(유녀)", "슈에링", "어린이슌", "꼬마슌", "シュエリン", "幼女シュン", "shunsmall", "shunling"),
    "Kisaki_Kiki": ("키키", "키사키키키", "키사키(키키)", "류우게키사키키키", "キキ", "kisakikiki", "kiki"),
    "Kei": ("케이", "텐도케이", "케이(key)", "케이key", "key", "ケイ"),
    "GSC_President": ("총학생회장", "連邦生徒会長", "gscpresident"),
    "Hatsune_Miku": ("하츠네미쿠", "미쿠", "初音ミク", "ミク", "miku"),
    "Misaka_Mikoto": ("미사카미코토", "미사카", "미코토", "御坂美琴", "美琴", "mikoto"),
    "Shokuhou_Misaki": ("쇼쿠호미사키", "쇼쿠호", "食蜂操祈", "shokuhou"),
    "Saten_Ruiko": ("사텐루이코", "사텐", "루이코", "佐天涙子", "saten", "ruiko"),
}
_RECOMMENDED_SHORT_TITLE_OVERRIDES: dict[str, str] = {
    "Shiroko_Terror": "시로코*테러",
    "Shun_Small": "어린 슌",
    "Kisaki_Kiki": "키키",
    "Kei": "케이",
    "Misaki": "이마시노 미사키",
    "Shokuhou_Misaki": "쇼쿠호 미사키",
    "Hatsune_Miku": "하츠네 미쿠",
    "Misaka_Mikoto": "미사카 미코토",
    "Saten_Ruiko": "사텐 루이코",
    "GSC_President": "총학생회장",
}
_DEFAULT_CHARACTER_NAME_INDEX_CACHE: dict[str, str] | None = None


def build_recommended_short_title(char_id: str, name_kr: str, lang: str = "JP") -> str:
    """
    Build the crisp, collision-free short title recommended for manual Fish Audio web uploads.
    Guaranteed to round-trip 100% cleanly via `parse_fish_model_identity` for all JP & KR slots.
    """
    cid = str(char_id or "").strip()
    lang_up = "KR" if str(lang or "JP").strip().upper() == "KR" else "JP"
    base = _RECOMMENDED_SHORT_TITLE_OVERRIDES.get(cid, "")
    if not base:
        clean_kr = re.sub(r"\s*[(\（][^)\）]*[)\）]\s*", " ", str(name_kr or cid)).strip()
        parts = [p for p in clean_kr.split() if p]
        base = parts[-1] if len(parts) >= 2 else (clean_kr or cid)
    return f"{base} KR" if lang_up == "KR" else base


def _norm_identity_key(raw: str) -> str:
    return _NAME_NORM_STRIP_RE.sub("", str(raw or "").strip()).lower()


def _lang_code_from_token(tok: str) -> str:
    low = str(tok or "").strip().lower()
    if low in ("kr", "ko", "kor", "korean", "한음", "한국어", "한국", "더빙", "한"):
        return "KR"
    if low in ("jp", "ja", "jpn", "japanese", "일음", "일본어", "일본", "일"):
        return "JP"
    return ""


def _lang_from_model_metadata(model_obj: dict[str, Any]) -> str:
    langs = model_obj.get("languages")
    if isinstance(langs, list):
        normed = {str(x or "").strip().lower() for x in langs if str(x or "").strip()}
        has_ko = bool(normed & {"ko", "kr", "kor", "korean"})
        has_ja = bool(normed & {"ja", "jp", "jpn", "japanese"})
        if has_ko and not has_ja:
            return "KR"
        if has_ja and not has_ko:
            return "JP"
    tags = model_obj.get("tags")
    if isinstance(tags, list):
        tset = {str(x or "").strip().lower() for x in tags}
        has_ko = bool(tset & {"kr", "ko", "korean", "한국어", "한음"})
        has_ja = bool(tset & {"jp", "ja", "japanese", "일본어", "일음"})
        if has_ko and not has_ja:
            return "KR"
        if has_ja and not has_ko:
            return "JP"
    return ""


def build_character_name_index(characters: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    """
    Build a collision-safe normalized name/alias -> canonical `char_id` lookup table so
    users can upload models with just the character's name (e.g. `유우카`, `하야세 유우카`,
    `유우카 KR`, `Yuuka`, `早瀬ユウカ`, `시로코*테러`, `어린 슌`, `키키`) and still auto-sync.
    """
    global _DEFAULT_CHARACTER_NAME_INDEX_CACHE
    if characters is None:
        if _DEFAULT_CHARACTER_NAME_INDEX_CACHE is not None:
            return _DEFAULT_CHARACTER_NAME_INDEX_CACHE
        characters = load_all_character_configs()
        built = build_character_name_index(characters)
        _DEFAULT_CHARACTER_NAME_INDEX_CACHE = built
        return built

    primary: dict[str, str] = {}
    secondary_counts: dict[str, list[str]] = {}
    targets = _iter_voice_targets(characters)
    valid_cids = {cid for cid, _, _ in targets}

    def _add_primary(alias: str, cid: str) -> None:
        k = _norm_identity_key(alias)
        if k:
            primary.setdefault(k, cid)

    def _add_secondary(alias: str, cid: str) -> None:
        k = _norm_identity_key(alias)
        if k and k not in primary:
            lst = secondary_counts.setdefault(k, [])
            if cid not in lst:
                lst.append(cid)

    # Pass 1: exact canonical IDs, full KR/JP names, and curated unambiguous aliases
    for cid, ch, is_alt in targets:
        _add_primary(cid, cid)
        name_kr = str(ch.get("name_kr") or "").strip()
        name_jp = str(ch.get("name_jp") or "").strip()
        if name_kr:
            _add_primary(name_kr, cid)
        if name_jp:
            _add_primary(name_jp, cid)
        # Base character owns the parenthetical-stripped full name (prevents alt variants colliding with base)
        if not is_alt and cid not in ("Shiroko_Terror", "Shun_Small"):
            kr_no_paren = re.sub(r"\s*[(\（][^)\）]*[)\）]\s*", " ", name_kr).strip()
            jp_no_paren = re.sub(r"\s*[(\（][^)\）]*[)\）]\s*", "", name_jp).strip()
            if kr_no_paren:
                _add_primary(kr_no_paren, cid)
            if jp_no_paren:
                _add_primary(jp_no_paren, cid)
        for alias in _EXPLICIT_CHAR_ALIASES.get(cid, ()):
            _add_primary(alias, cid)

    # Preferred default owners for the 3 shared short Korean given names when no variant suffix is typed
    for short_alias, owner_cid in (("시로코", "Shiroko"), ("슌", "Shun"), ("미사키", "Misaki")):
        if owner_cid in valid_cids:
            _add_primary(short_alias, owner_cid)

    # Pass 2: short given names (last token of Korean name, JP given-name katakana/kana tail) when unambiguous
    for cid, ch, is_alt in targets:
        if is_alt or cid in ("Shiroko_Terror", "Shun_Small", "Shokuhou_Misaki"):
            continue
        name_kr = str(ch.get("name_kr") or "").strip()
        kr_no_paren = re.sub(r"\s*[(\（][^)\）]*[)\）]\s*", " ", name_kr).strip()
        parts = [p for p in kr_no_paren.split() if p]
        if len(parts) >= 2:
            _add_secondary(parts[-1], cid)
        # Extract trailing kana given name from JP full name (e.g. 早瀬ユウカ -> ユウカ)
        name_jp = str(ch.get("name_jp") or "").strip()
        jp_no_paren = re.sub(r"\s*[(\（][^)\）]*[)\）]\s*", "", name_jp).strip()
        m_jp_tail = re.search(r"([ァ-ヶー]{2,}|[ぁ-ん]{2,})$", jp_no_paren)
        if m_jp_tail and m_jp_tail.group(1) != jp_no_paren:
            _add_secondary(m_jp_tail.group(1), cid)

    merged = dict(primary)
    for k, cids in secondary_counts.items():
        if len(cids) == 1:
            merged[k] = cids[0]
    return merged


def parse_fish_model_identity(
    model_obj: dict[str, Any],
    *,
    characters: dict[str, dict[str, Any]] | None = None,
    name_index: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """
    Extract `(char_id, lang)` from a Fish Audio model object:
      1. Structured tags (`ba-char-<id>` / `ba-lang-<jp|kr>` or legacy `ba_char:` / `ba_lang:`)
      2. Canonical title pattern (`Blue Archive - <ID> (... / JP|KR ...)`)
      3. Short manual title matching just the character name (+ optional JP/KR suffix or model `languages`),
         e.g. `유우카`, `하야세 유우카`, `유우카 KR`, `Yuuka`, `早瀬ユウカ`, `시로코*테러`, `어린 슌`, `키키`.
    """
    if not isinstance(model_obj, dict):
        return None
    tags = model_obj.get("tags") or []
    tag_cid = ""
    tag_lang = ""
    if isinstance(tags, list):
        for t in tags:
            ts = str(t or "").strip()
            low = ts.lower()
            if low.startswith("ba-char-"):
                tag_cid = ts[len("ba-char-") :].strip()
            elif low.startswith("ba_char:"):
                tag_cid = ts[len("ba_char:") :].strip()
            elif low.startswith("ba-lang-"):
                lv = ts[len("ba-lang-") :].strip().upper()
                if lv in ("JP", "KR"):
                    tag_lang = lv
            elif low.startswith("ba_lang:"):
                lv = ts[len("ba_lang:") :].strip().upper()
                if lv in ("JP", "KR"):
                    tag_lang = lv
    if tag_cid and tag_lang and is_safe_entity_id(tag_cid):
        return tag_cid, tag_lang

    title = str(model_obj.get("title") or "").strip()
    m = _TITLE_IDENTITY_RE.match(title)
    if m:
        parsed_cid = m.group(1).strip()
        if is_safe_entity_id(parsed_cid):
            return parsed_cid, m.group(2).strip().upper()

    # 3. Short/manual character-name matching (e.g. "유우카", "하야세 유우카 KR", "Yuuka", "早瀬ユウカ JP")
    if not title:
        return None
    idx = name_index if name_index is not None else build_character_name_index(characters)
    if not idx:
        return None

    cleaned = _TITLE_REF_SUFFIX_STRIP_RE.sub("", title).strip()
    cleaned = _TITLE_PREFIX_STRIP_RE.sub("", cleaned).strip()

    explicit_lang = tag_lang
    while True:
        lm = _TITLE_LANG_SUFFIX_RE.search(cleaned)
        if not lm:
            break
        tok = lm.group("br_lang") or lm.group("sp_lang") or ""
        parsed_lang = _lang_code_from_token(tok)
        if parsed_lang and not explicit_lang:
            explicit_lang = parsed_lang
        cleaned = cleaned[: lm.start()].strip()

    # Also allow leading language tag like "[KR] 유우카" or "KR 유우카"
    m_lead_lang = re.match(
        r"^(?:[\(\[（【]\s*(jp|ja|jpn|일음|일본어|일|kr|ko|kor|한음|한국어|한)\s*[\)\]）】]\s*|(jp|ja|kr|ko|일음|한음|일본어|한국어)[\s_\-–—:|/]+)(.+)$",
        cleaned,
        re.IGNORECASE,
    )
    if m_lead_lang:
        lead_tok = m_lead_lang.group(1) or m_lead_lang.group(2) or ""
        parsed_lang = _lang_code_from_token(lead_tok)
        if parsed_lang and not explicit_lang:
            explicit_lang = parsed_lang
        cleaned = m_lead_lang.group(3).strip()

    # Support folder/filename pastes like "001_Airi_쿠리무라_아이리_JP" or "134_Shiroko_Terror_스나오오카미_시로코_(시로코_테러)_JP".
    # Try longest underscore-joined prefix first so multi-word IDs (Shiroko_Terror, Kisaki_Kiki,
    # Shun_Small, Hatsune_Miku, Misaka_Mikoto, Shokuhou_Misaki, Saten_Ruiko, GSC_President)
    # ALWAYS resolve before single-token base IDs (Shiroko, Kisaki, Shun).
    candidates_to_try: list[str] = [cleaned]
    if "_" in cleaned:
        parts = [p.strip() for p in cleaned.split("_") if p.strip()]
        if len(parts) >= 2:
            max_prefix = min(3, len(parts) - 1)
            for k in range(max_prefix, 0, -1):
                candidates_to_try.append("_".join(parts[:k]))
            for k in range(1, max_prefix + 1):
                candidates_to_try.append("_".join(parts[k:]))

    matched_cid = ""
    for cand in candidates_to_try:
        key = _norm_identity_key(cand)
        if key and key in idx:
            matched_cid = idx[key]
            break

    if not matched_cid or not is_safe_entity_id(matched_cid):
        return None

    resolved_lang = explicit_lang or _lang_from_model_metadata(model_obj) or "JP"
    return matched_cid, resolved_lang


def sync_existing_fish_models(
    api_key: str,
    cfg: dict[str, Any],
    characters: dict[str, dict[str, Any]] | None = None,
    overwrite: bool = False,
    http_session: Any = None,
    log_cb: Callable[[str], None] | None = None,
    manifest: dict[str, Any] | None = None,
    stop_event: threading.Event | None = None,
    max_retries: int = 3,
    persist_voice_link_cb: Callable[[str, str, str], None] | None = None,
    timeout: float = 30.0,
    prefer_new_self_owned: bool = False,
) -> dict[str, Any]:
    """
    Query `GET https://api.fish.audio/model?self=true` across all pages and link any existing
    Blue Archive voice models created on the user's own Fish Audio account into `cfg` & manifest.
    Prevents duplicate model creation across re-runs or new PCs.
    """
    clean_key = (api_key or "").strip()
    if not clean_key:
        return {
            "ok": False,
            "fatal_code": "AUTH_ERROR",
            "error": "Fish Audio API Key가 비어 있습니다.",
            "synced": 0,
            "total_remote": 0,
            "matched_keys": [],
            "matched_models": {},
            "synced_models": {},
            "self_owned_ids": [],
        }

    sess = http_session or requests
    headers = {"Authorization": f"Bearer {clean_key}"}
    eff_chars = characters if characters is not None else load_all_character_configs()
    name_index = build_character_name_index(eff_chars)
    cid_ci: dict[str, str] = {}
    name_by_cid: dict[str, str] = {}
    for target_cid, target_ch, _ in _iter_voice_targets(eff_chars):
        cid_ci[target_cid.lower()] = target_cid
        name_by_cid[target_cid] = str(target_ch.get("name_kr") or target_cid)

    page = 1
    page_size = 100
    total_remote = 0
    synced = 0
    seen_remote_ids: set[str] = set()
    matched_models: dict[tuple[str, str], str] = {}
    retry_limit = max(0, int(max_retries))
    eff_timeout = max(1.0, float(_safe_float(timeout, 30.0) or 30.0))

    def _stopped_result() -> dict[str, Any]:
        return {
            "ok": False,
            "fatal_code": "STOPPED",
            "error": "사용자에 의해 안전 중지되었습니다.",
            "synced": synced,
            "total_remote": total_remote,
            "matched_keys": list(matched_models.keys()),
            "matched_models": {f"{c}::{l}": m for (c, l), m in matched_models.items()},
            "synced_models": {},
            "self_owned_ids": sorted(seen_remote_ids),
        }

    while page <= 50:
        if stop_event is not None and stop_event.is_set():
            return _stopped_result()

        resp_code = 0
        resp_text = ""
        body: Any = None
        resp_headers: Any = {}

        for attempt in range(retry_limit + 1):
            if stop_event is not None and stop_event.is_set():
                return _stopped_result()
            try:
                url = _validate_fish_api_url(FISH_MODEL_URL)
                if _is_real_requests(sess):
                    resp = sess.get(
                        url,
                        headers=headers,
                        params={"self": "true", "page_size": page_size, "page_number": page},
                        timeout=eff_timeout,
                        verify=True,
                        allow_redirects=False,
                        stream=True,
                    )
                else:
                    resp = sess.get(
                        url,
                        headers=headers,
                        params={"self": "true", "page_size": page_size, "page_number": page},
                        timeout=eff_timeout,
                    )
                resp_code, resp_text, body, resp_headers = _read_bounded_response(resp, MAX_FISH_JSON_RESPONSE_BYTES)
            except Exception as exc:
                err_txt = _redact_secrets(f"계정 모델 조회 네트워크 오류: {exc}", extra_secrets=(clean_key,))
                if attempt < retry_limit:
                    wait_s = min(30.0, 2.0 * (2**attempt))
                    if callable(log_cb):
                        log_cb(f"⏳ [계정 동기화] {err_txt} → {wait_s:.0f}초 후 재시도 ({attempt + 1}/{retry_limit})")
                    if _interruptible_sleep(wait_s, stop_event):
                        return _stopped_result()
                    continue
                return {
                    "ok": False,
                    "fatal_code": "",
                    "error": err_txt,
                    "synced": synced,
                    "total_remote": total_remote,
                    "matched_keys": list(matched_models.keys()),
                    "matched_models": {f"{c}::{l}": m for (c, l), m in matched_models.items()},
                    "synced_models": {},
                    "self_owned_ids": sorted(seen_remote_ids),
                }

            if resp_code == 401:
                return {
                    "ok": False,
                    "fatal_code": "AUTH_ERROR",
                    "error": "Fish Audio API Key가 유효하지 않습니다 (HTTP 401).",
                    "synced": 0,
                    "total_remote": 0,
                    "matched_keys": [],
                    "matched_models": {},
                    "synced_models": {},
                    "self_owned_ids": [],
                }
            if resp_code == 429:
                wait_s = _parse_retry_after(resp_headers, default_sec=min(60.0, 3.0 * (2**attempt)))
                if attempt < retry_limit:
                    if callable(log_cb):
                        log_cb(f"⏳ [계정 동기화] 속도 제한(429) → {wait_s:.0f}초 후 재시도 ({attempt + 1}/{retry_limit})")
                    if _interruptible_sleep(wait_s, stop_event):
                        return _stopped_result()
                    continue
                safe_txt = _redact_secrets(resp_text[:200], extra_secrets=(clean_key,))
                return {
                    "ok": False,
                    "fatal_code": "",
                    "error": f"계정 모델 목록 조회 속도 제한 (HTTP 429): {safe_txt}",
                    "synced": synced,
                    "total_remote": total_remote,
                    "matched_keys": list(matched_models.keys()),
                    "matched_models": {f"{c}::{l}": m for (c, l), m in matched_models.items()},
                    "synced_models": {},
                    "self_owned_ids": sorted(seen_remote_ids),
                }
            if resp_code >= 500:
                wait_s = min(30.0, 2.0 * (2**attempt))
                if attempt < retry_limit:
                    if callable(log_cb):
                        log_cb(f"⏳ [계정 동기화] 서버 일시 오류({resp_code}) → {wait_s:.0f}초 후 재시도 ({attempt + 1}/{retry_limit})")
                    if _interruptible_sleep(wait_s, stop_event):
                        return _stopped_result()
                    continue
                safe_txt = _redact_secrets(resp_text[:200], extra_secrets=(clean_key,))
                return {
                    "ok": False,
                    "fatal_code": "",
                    "error": f"계정 모델 목록 조회 서버 오류 (HTTP {resp_code}): {safe_txt}",
                    "synced": synced,
                    "total_remote": total_remote,
                    "matched_keys": list(matched_models.keys()),
                    "matched_models": {f"{c}::{l}": m for (c, l), m in matched_models.items()},
                    "synced_models": {},
                    "self_owned_ids": sorted(seen_remote_ids),
                }
            if resp_code != 200:
                safe_txt = _redact_secrets(resp_text[:200], extra_secrets=(clean_key,))
                return {
                    "ok": False,
                    "fatal_code": "",
                    "error": f"계정 모델 목록 조회 실패 (HTTP {resp_code}): {safe_txt}",
                    "synced": synced,
                    "total_remote": total_remote,
                    "matched_keys": list(matched_models.keys()),
                    "matched_models": {f"{c}::{l}": m for (c, l), m in matched_models.items()},
                    "synced_models": {},
                    "self_owned_ids": sorted(seen_remote_ids),
                }
            break

        if body is None:
            break

        items = body.get("items") if isinstance(body, dict) else (body if isinstance(body, list) else [])
        if not isinstance(items, list) or not items:
            break

        new_on_page = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = _clean_model_id(str(item.get("_id") or item.get("id") or ""))
            if not mid or mid in seen_remote_ids:
                continue
            seen_remote_ids.add(mid)
            new_on_page += 1
            total_remote += 1
            if str(item.get("state") or "").strip().lower() == "failed":
                continue
            ident = parse_fish_model_identity(item, name_index=name_index)
            if not ident:
                continue
            raw_cid, lang_up = ident
            canonical_cid = cid_ci.get(raw_cid.lower(), raw_cid)
            matched_models.setdefault((canonical_cid, lang_up), mid)

        if new_on_page == 0:
            break

        if isinstance(body, dict) and "has_more" in body:
            if not bool(body.get("has_more")):
                break
        else:
            total_reported = _safe_int(body.get("total"), 0) if isinstance(body, dict) else 0
            if len(items) < page_size or (total_reported and total_remote >= total_reported):
                break
        page += 1

    synced_models: dict[str, str] = {}
    persisted_links: list[tuple[str, str, str]] = []
    with _BATCH_PERSIST_LOCK:
        saved_jp = _ensure_dict(cfg, "saved_voices")
        saved_kr = _ensure_dict(cfg, "saved_voices_kr")
        disk_man = load_batch_manifest()
        disk_entries = _ensure_dict(disk_man, "entries")
        if manifest is not None:
            entries = _ensure_dict(manifest, "entries")
            for dk, dv in disk_entries.items():
                if dk not in entries and isinstance(dv, dict):
                    entries[dk] = dv
            man = manifest
        else:
            man = disk_man
            entries = disk_entries
        manifest_dirty = False

        for (cid, lang_up), mid in matched_models.items():
            bucket = saved_kr if lang_up == "KR" else saved_jp
            prev_id = _clean_model_id(str(bucket.get(cid) or ""))
            entry_key = f"{cid}::{lang_up}"
            old_ent = _as_dict(entries.get(entry_key))
            prev_owned_mid = _clean_model_id(str(old_ent.get("model_id") or "")) if old_ent.get("owned_by_self") else ""
            is_newly_discovered_self = bool(prefer_new_self_owned) and (prev_owned_mid != mid)
            if overwrite or not prev_id or is_newly_discovered_self:
                if prev_id and prev_id != mid:
                    backup_saved_voices_once(cfg, log_cb=log_cb)
                if prev_id != mid:
                    bucket[cid] = mid
                    _sync_active_reference_id(cfg, cid, lang_up, mid)
                    synced += 1
                    synced_models[entry_key] = mid
                    persisted_links.append((cid, lang_up, mid))

            if not old_ent.get("owned_by_self") or _clean_model_id(str(old_ent.get("model_id") or "")) != mid:
                ch_name = str(name_by_cid.get(cid) or old_ent.get("name_kr") or cid)
                entries[entry_key] = {
                    **old_ent,
                    "char_id": cid,
                    "name_kr": ch_name,
                    "lang": lang_up,
                    "uploaded": True,
                    "owned_by_self": True,
                    "model_id": mid,
                    "error": "",
                    "updated_at": time.time(),
                }
                manifest_dirty = True

        if synced > 0 and not callable(persist_voice_link_cb):
            save_user_config(copy.deepcopy(cfg))
        if manifest_dirty:
            save_batch_manifest(man)

    if callable(persist_voice_link_cb) and persisted_links:
        for pcid, plang, pmid in persisted_links:
            persist_voice_link_cb(pcid, plang, pmid)

    if callable(log_cb):
        log_cb(f"🔄 [계정 동기화] 내 Fish Audio 모델 {total_remote}개 스캔 → 블루아카 매칭 {len(matched_models)}개 · 새로 연결 {synced}개")

    return {
        "ok": True,
        "fatal_code": "",
        "synced": synced,
        "matched": len(matched_models),
        "total_remote": total_remote,
        "matched_keys": list(matched_models.keys()),
        "matched_models": {f"{c}::{l}": m for (c, l), m in matched_models.items()},
        "synced_models": synced_models,
        "self_owned_ids": sorted(seen_remote_ids),
    }


def extract_job_reference(
    job: BatchVoiceJob,
    force: bool = False,
    game_dir: Path | None = None,
    quality_profile: str = DEFAULT_QUALITY_PROFILE,
) -> dict[str, Any]:
    """Extract & master studio reference WAV clips + verbatim official transcripts for one `(char_id, lang)` job."""
    game_dir = resolve_ba_game_dir(game_dir)
    prof_key, _ = resolve_quality_profile(quality_profile)
    comb_wav, samples_dir = _char_ref_dir(job.char_id, job.lang)
    if (not force) and is_studio_v2_extracted(job.char_id, job.lang, quality_profile=prof_key, char_data=job.char_data):
        meta_path = samples_dir / "studio_v2_meta.json"
        meta: dict[str, Any] = {}
        try:
            loaded_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded_meta, dict):
                meta = loaded_meta
        except Exception:
            meta = {}
        wav_files = sorted(samples_dir.glob("*.wav"))
        return {
            "skipped": True,
            "character_id": job.char_id,
            "lang": job.lang,
            "pipeline_version": _safe_int(meta.get("pipeline_version"), STUDIO_PIPELINE_VERSION),
            "quality_profile": str(meta.get("quality_profile") or prof_key),
            "clip_count": _safe_int(meta.get("clip_count"), len(wav_files)),
            "memoriallobby_clips": _safe_int(meta.get("memoriallobby_clips"), 0),
            "total_duration_sec": _safe_float(meta.get("total_duration_sec"), 0.0),
            "transcript_matched": _safe_int(meta.get("transcript_matched"), 0),
            "transcript_coverage": _safe_float(meta.get("transcript_coverage"), 0.0),
            "transcript_locked": bool(meta.get("transcript_locked", False)),
            "combined_wav": comb_wav,
            "samples_dir": samples_dir,
            "sample_files": wav_files,
        }

    res = build_character_fish_reference(
        job.char_data,
        lang=job.lang,
        game_dir=game_dir,
        quality_profile=prof_key,
    )
    return {"skipped": False, **res}


def _parse_retry_after(headers: Any, default_sec: float) -> float:
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is not None:
            s = str(raw).strip()
            if not s:
                return default_sec
            try:
                val = float(s)
                if math.isfinite(val) and val >= 0.5:
                    return min(180.0, val)
                return default_sec
            except (ValueError, TypeError):
                pass
            try:
                dt = parsedate_to_datetime(s)
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                    if math.isfinite(delta) and delta >= 0.5:
                        return min(180.0, delta)
            except Exception:
                pass
    except Exception:
        pass
    return default_sec


def _interruptible_sleep(sec: float, stop_event: threading.Event | None) -> bool:
    """Sleep for `sec` seconds; returns True if interrupted early by `stop_event`."""
    end = time.monotonic() + max(0.0, float(sec))
    while time.monotonic() < end:
        if stop_event is not None and stop_event.is_set():
            return True
        time.sleep(min(0.2, max(0.01, end - time.monotonic())))
    return bool(stop_event is not None and stop_event.is_set())


def run_preflight_checks(
    mode: str,
    langs: tuple[str, ...],
    api_key: str,
    game_dir: Path | None = None,
    log_cb: Callable[[str], None] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Validate environment readiness (API key, Steam .molru directories, free disk space) BEFORE
    starting a long batch run so errors are caught immediately with clear guidance.
    """
    game_dir = resolve_ba_game_dir(game_dir, cfg)
    if mode in ("sync_only", "upload") and not (api_key or "").strip():
        return {"ok": False, "fatal_code": "AUTH_ERROR", "error": "Fish Audio API Key가 비어 있습니다. 설정 1단계에 키를 입력해 주세요."}

    if mode in ("extract_only", "upload"):
        # Check free disk space on models target drive
        try:
            usage = shutil.disk_usage(get_user_models_dir())
            free_gb = usage.free / (1024**3)
            if usage.free < _MIN_FREE_DISK_BYTES:
                return {
                    "ok": False,
                    "fatal_code": "DISK_FULL",
                    "error": f"저장 공간이 부족합니다 (남은 공간 {free_gb:.2f} GB). 최소 1 GB 이상 확보한 뒤 다시 시도해 주세요.",
                }
            if usage.free < _WARN_FREE_DISK_BYTES and callable(log_cb):
                log_cb(f"⚠️ [공간 안내] 현재 남은 디스크 공간은 {free_gb:.1f} GB입니다 (전 캐릭터 307건 고품질 추출 시 약 5~8 GB 필요).")
        except Exception:
            pass

        # Verify Steam voice folder exists for at least one requested language
        audio_root = (
            game_dir
            / "BlueArchive_Data"
            / "StreamingAssets"
            / "PUB"
            / "Resource"
            / "GameData"
            / "MediaResources"
            / "Audio"
        )
        present_langs = [l for l in langs if (audio_root / f"VOC_{l.upper()}").is_dir()]
        if not present_langs and mode == "extract_only":
            return {
                "ok": False,
                "fatal_code": "STEAM_MISSING",
                "error": (
                    f"스팀 블루 아카이브 음성 폴더를 찾을 수 없습니다: {audio_root}\n"
                    "스팀 클라이언트가 설치된 경로를 확인하거나 `--game-dir`로 경로를 지정해 주세요."
                ),
            }
    return {"ok": True, "fatal_code": "", "error": ""}


def _probe_wav_info(path: Path) -> tuple[int, int, float] | None:
    """Return `(frames, samplerate, duration_sec)` for a readable WAV file, or None."""
    try:
        info = sf.info(str(path))
        frames = int(getattr(info, "frames", 0) or 0)
        sr = int(getattr(info, "samplerate", 0) or 0)
        if frames <= 0 or sr <= 0:
            return None
        return frames, sr, frames / float(sr)
    except Exception:
        return None


def _estimate_resampled_samples(frames: int, orig_sr: int, target_sr: int) -> int:
    """Exact output sample count produced by `scipy.signal.resample_poly(y, up, down)`."""
    if frames <= 0 or orig_sr <= 0 or target_sr <= 0:
        return 0
    if orig_sr == target_sr:
        return frames
    g = math.gcd(int(orig_sr), int(target_sr))
    up = int(target_sr) // g
    down = int(orig_sr) // g
    return (frames * up + down - 1) // down


def _sum_wav_durations(wavs: list[Path], durations_by_path: dict[str, float] | None = None) -> float:
    total = 0.0
    cache = durations_by_path if durations_by_path is not None else {}
    for w in wavs:
        key = str(w)
        if key in cache:
            total += cache[key]
            continue
        info = _probe_wav_info(w)
        if info is not None:
            cache[key] = info[2]
            total += info[2]
    return round(total, 2)


def pack_upload_samples_to_max_files(
    wav_paths: list[Path],
    texts: list[str],
    *,
    max_files: int = FISH_MAX_UPLOAD_FILES,
    max_total_sec: float = 200.0,
    pause_sec: float = INTER_CLIP_PAUSE_SEC,
    target_sr: int = TARGET_SR,
    temp_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Pack ordered transcript-matched reference clips into `<= max_files` (default 20) WAV files
    strictly within `max_total_sec` (default 200.0s, safely under Fish Audio Plus 210s cap),
    including all inserted inter-clip silence pauses (`pause_sec`, default 0.45s).

    - If `len(kept) <= max_files`: returns the original WAV paths untouched (zero re-encoding).
    - If `len(kept) > max_files`: performs balanced shortest-adjacent merging preserving natural
      clip order so every bucket stays concise:
        * Single-clip buckets reuse their original WAV path directly (zero copy/re-encode).
        * Multi-clip buckets concatenate mono float32 @ `target_sr` with `pause_sec` zero silence,
          resampling cleanly via `resample_poly` if needed, applying a gentle edge fade only when
          a clip lacks quiet fade margins, and writing `PCM_16` WAVs (`pack_01.wav`, ...) into `temp_dir`.
        * Bucket transcripts are joined verbatim with `" "` for 100% 1:1 alignment.
    """
    eff_max_files = max(1, int(max_files))
    eff_sr = max(8000, int(target_sr))
    raw_pause = _safe_float(pause_sec, INTER_CLIP_PAUSE_SEC)
    eff_pause_sec = max(0.0, raw_pause)
    pause_samples = int(round(eff_sr * eff_pause_sec)) if eff_pause_sec > 0.0 else 0
    raw_max_sec = _safe_float(max_total_sec, 200.0)
    eff_max_total_sec = raw_max_sec if raw_max_sec > 0.0 else 200.0
    max_total_samples = int(math.floor(eff_max_total_sec * eff_sr + 1e-6))

    in_wavs = list(wav_paths or [])
    input_count = len(in_wavs)
    if not in_wavs:
        return {
            "wavs": [],
            "texts": [],
            "source_wavs": [],
            "source_texts": [],
            "packed": False,
            "trimmed": False,
            "input_clip_count": 0,
            "source_clip_count": 0,
            "packed_file_count": 0,
            "raw_duration_sec": 0.0,
            "packed_duration_sec": 0.0,
            "merged_buckets": 0,
            "reused_files": 0,
            "bucket_sizes": [],
            "durations_by_path": {},
        }

    raw_texts = [str(t or "").strip() for t in (texts or [])]
    has_texts = bool(raw_texts) and len(raw_texts) == input_count and all(bool(t) for t in raw_texts)

    # Probe clip headers without decoding full audio: (path, text, raw_dur_sec, est_target_samples, orig_sr)
    probed: list[tuple[Path, str, float, int, int]] = []
    for idx, w_path in enumerate(in_wavs):
        txt = raw_texts[idx] if has_texts else ""
        info = _probe_wav_info(w_path)
        if info is None:
            continue
        frames, orig_sr, raw_dur = info
        est_samples = _estimate_resampled_samples(frames, orig_sr, eff_sr)
        if est_samples <= 0:
            continue
        probed.append((w_path, txt, raw_dur, est_samples, orig_sr))

    if not probed:
        fallback_wavs = in_wavs[:eff_max_files]
        fallback_texts = raw_texts[: len(fallback_wavs)] if has_texts else []
        return {
            "wavs": fallback_wavs,
            "texts": fallback_texts,
            "source_wavs": list(fallback_wavs),
            "source_texts": list(fallback_texts),
            "packed": False,
            "trimmed": len(fallback_wavs) < input_count,
            "input_clip_count": input_count,
            "source_clip_count": len(fallback_wavs),
            "packed_file_count": len(fallback_wavs),
            "raw_duration_sec": 0.0,
            "packed_duration_sec": 0.0,
            "merged_buckets": 0,
            "reused_files": len(fallback_wavs),
            "bucket_sizes": [1] * len(fallback_wavs),
            "durations_by_path": {},
        }

    # Strictly enforce `max_total_sec` including internal pauses via exact integer sample-count math:
    # For K clips packed into min(K, max_files) buckets, total internal pauses is always max(0, K - max_files).
    kept: list[tuple[Path, str, float, int, int]] = []
    kept_est_samples = 0
    for item in probed:
        est_s = item[3]
        k_new = len(kept) + 1
        merges_new = max(0, k_new - eff_max_files)
        projected_samples = kept_est_samples + est_s + merges_new * pause_samples
        if projected_samples <= max_total_samples:
            kept.append(item)
            kept_est_samples += est_s

    if not kept:
        kept.append(min(probed, key=lambda x: x[2]))

    source_wavs = [x[0] for x in kept]
    source_texts = [x[1] for x in kept] if has_texts else []
    raw_dur_total = sum(x[2] for x in kept)
    durations_by_path: dict[str, float] = {str(x[0]): x[2] for x in kept}
    trimmed = len(kept) < input_count

    # Zero-copy fast path when <= max_files clips remain within budget
    if len(kept) <= eff_max_files:
        return {
            "wavs": list(source_wavs),
            "texts": list(source_texts),
            "source_wavs": source_wavs,
            "source_texts": source_texts,
            "packed": False,
            "trimmed": trimmed,
            "input_clip_count": input_count,
            "source_clip_count": len(kept),
            "packed_file_count": len(kept),
            "raw_duration_sec": round(raw_dur_total, 2),
            "packed_duration_sec": round(raw_dur_total, 2),
            "merged_buckets": 0,
            "reused_files": len(kept),
            "bucket_sizes": [1] * len(kept),
            "durations_by_path": durations_by_path,
        }

    if temp_dir is None:
        raise ValueError("temp_dir is required when packing more than max_files clips")
    out_dir = Path(temp_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Balanced order-preserving shortest-adjacent merging into `eff_max_files` ordered buckets
    buckets: list[list[int]] = [[i] for i in range(len(kept))]
    bucket_samples: list[int] = [kept[i][3] for i in range(len(kept))]
    while len(buckets) > eff_max_files:
        best_j = 0
        best_key = (
            bucket_samples[0] + pause_samples + bucket_samples[1],
            len(buckets[0]) + len(buckets[1]),
            0,
        )
        for j in range(1, len(buckets) - 1):
            cand_key = (
                bucket_samples[j] + pause_samples + bucket_samples[j + 1],
                len(buckets[j]) + len(buckets[j + 1]),
                j,
            )
            if cand_key < best_key:
                best_key = cand_key
                best_j = j
        buckets[best_j] = buckets[best_j] + buckets[best_j + 1]
        bucket_samples[best_j] = bucket_samples[best_j] + pause_samples + bucket_samples[best_j + 1]
        del buckets[best_j + 1]
        del bucket_samples[best_j + 1]

    pause_arr = np.zeros(pause_samples, dtype=np.float32) if pause_samples > 0 else None
    out_wavs: list[Path] = []
    out_texts: list[str] = []
    actual_total_dur = 0.0
    merged_buckets = 0
    reused_files = 0
    edge_check_samples = max(1, int(round(eff_sr * 0.002)))

    for b_idx, idxs in enumerate(buckets, 1):
        if has_texts:
            joined_text = " ".join(kept[i][1].strip() for i in idxs if kept[i][1].strip())
            out_texts.append(joined_text)

        if len(idxs) == 1:
            w_path = kept[idxs[0]][0]
            clip_dur = kept[idxs[0]][2]
            out_wavs.append(w_path)
            actual_total_dur += clip_dur
            durations_by_path[str(w_path)] = clip_dur
            reused_files += 1
            continue

        parts: list[np.ndarray] = []
        for pos, clip_i in enumerate(idxs):
            src_path = kept[clip_i][0]
            y, sr = sf.read(str(src_path), dtype="float32", always_2d=False)
            y_arr = np.asarray(y, dtype=np.float32)
            if y_arr.ndim > 1:
                y_arr = np.mean(y_arr, axis=1, dtype=np.float32)
            y_arr = np.nan_to_num(y_arr, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
            if int(sr) != eff_sr and len(y_arr) > 0:
                y_arr = resample_audio(y_arr, int(sr), eff_sr)
            ec = min(len(y_arr), edge_check_samples)
            if len(y_arr) >= 8 and (
                float(np.max(np.abs(y_arr[:ec]))) > 1e-3
                or float(np.max(np.abs(y_arr[-ec:]))) > 1e-3
            ):
                y_arr = apply_cosine_fade(y_arr, eff_sr, fade_ms=5.0)
            parts.append(y_arr)
            if pos + 1 < len(idxs) and pause_arr is not None:
                parts.append(pause_arr)

        merged = np.clip(np.concatenate(parts), -1.0, 1.0).astype(np.float32, copy=False)
        out_path = out_dir / f"pack_{b_idx:02d}.wav"
        sf.write(str(out_path), merged, eff_sr, subtype="PCM_16")
        merged_dur = len(merged) / float(eff_sr)
        out_wavs.append(out_path)
        actual_total_dur += merged_dur
        durations_by_path[str(out_path)] = merged_dur
        merged_buckets += 1

    return {
        "wavs": out_wavs,
        "texts": out_texts if has_texts else [],
        "source_wavs": source_wavs,
        "source_texts": source_texts,
        "packed": merged_buckets > 0,
        "trimmed": trimmed,
        "input_clip_count": input_count,
        "source_clip_count": len(kept),
        "packed_file_count": len(out_wavs),
        "raw_duration_sec": round(raw_dur_total, 2),
        "packed_duration_sec": round(actual_total_dur, 2),
        "merged_buckets": merged_buckets,
        "reused_files": reused_files,
        "bucket_sizes": [len(b) for b in buckets],
        "durations_by_path": durations_by_path,
    }


def _build_upload_fallback_plans(
    selected_wavs: list[Path],
    texts_list: list[str],
    transcript_attached: bool,
    comb_wav: Path,
    vis: str,
    has_cover: bool,
    *,
    unpacked_wavs: list[Path] | None = None,
    unpacked_texts: list[str] | None = None,
    packed_from_clips: int | None = None,
    packed_duration_sec: float | None = None,
    durations_by_path: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """
    Build deduplicated progressive fallback payload plans for HTTP 400/413/422 self-healing.
    Plan 0 is always the optimal full-quality payload (full-packed <=20 WAVs + 100%-locked
    verbatim transcripts + clean tags + cover when >20 clips are available; or direct clips otherwise),
    falling back progressively to unpacked top-20 individual WAVs, no-tags, compact-8, and single WAV.
    """
    plans: list[dict[str, Any]] = []
    seen_sigs: set[tuple[Any, ...]] = set()
    dur_cache: dict[str, float] = dict(durations_by_path or {})

    def _add(
        name: str,
        wavs: list[Path],
        texts: list[str],
        use_tags: bool,
        use_cover: bool,
        *,
        source_clips: int | None = None,
        dur_sec: float | None = None,
    ) -> None:
        if not wavs:
            return
        use_texts = list(texts) if (texts and len(texts) == len(wavs)) else []
        eff_cover = bool(use_cover and has_cover) or (vis == "public" and has_cover)
        sig = (tuple(str(w) for w in wavs), len(use_texts), bool(use_tags), eff_cover)
        if sig in seen_sigs:
            return
        seen_sigs.add(sig)
        eff_dur = (
            round(float(dur_sec), 2)
            if dur_sec is not None
            else _sum_wav_durations(wavs, dur_cache)
        )
        plans.append(
            {
                "name": name,
                "wavs": list(wavs),
                "texts": use_texts,
                "use_tags": bool(use_tags),
                "use_cover": eff_cover,
                "packed_from_clips": int(source_clips) if source_clips is not None else len(wavs),
                "duration_sec": eff_dur,
            }
        )

    init_texts = texts_list if (transcript_attached and len(texts_list) == len(selected_wavs)) else []
    eff_unpacked_wavs = list(unpacked_wavs) if unpacked_wavs else list(selected_wavs)
    eff_unpacked_texts = (
        list(unpacked_texts)
        if (transcript_attached and unpacked_texts and len(unpacked_texts) == len(eff_unpacked_wavs))
        else ([] if unpacked_wavs else init_texts)
    )
    is_packed = bool(
        unpacked_wavs
        and (
            tuple(str(w) for w in selected_wavs) != tuple(str(w) for w in eff_unpacked_wavs)
            or (packed_from_clips is not None and int(packed_from_clips) > len(selected_wavs))
        )
    )

    if is_packed:
        _add(
            "packed_optimal",
            selected_wavs,
            init_texts,
            True,
            True,
            source_clips=packed_from_clips if packed_from_clips is not None else len(selected_wavs),
            dur_sec=packed_duration_sec,
        )
        _add("unpacked_top20", eff_unpacked_wavs, eff_unpacked_texts, True, True)
        _add("no_tags", eff_unpacked_wavs, eff_unpacked_texts, False, True)
    else:
        _add("optimal", selected_wavs, init_texts, True, True, dur_sec=packed_duration_sec)
        _add("no_tags", selected_wavs, init_texts, False, True, dur_sec=packed_duration_sec)

    base_wavs = eff_unpacked_wavs
    base_texts = eff_unpacked_texts
    if len(base_wavs) > 8:
        _add("compact_8_clips", base_wavs[:8], base_texts[:8] if base_texts else [], False, True)
    if base_texts:
        _add("auto_asr_no_texts", base_wavs[:8] if len(base_wavs) > 8 else base_wavs, [], False, True)
    if vis != "public" and has_cover:
        _add("no_cover", base_wavs[:8] if len(base_wavs) > 8 else base_wavs, [], False, False)
    if comb_wav.is_file():
        _add("single_combined_wav", [comb_wav], [], False, vis == "public")
    elif base_wavs:
        _add("single_first_clip", [base_wavs[0]], [], False, vis == "public")
    return plans


def upload_job_to_fish_audio(
    job: BatchVoiceJob,
    api_key: str,
    visibility: str = "private",
    include_cover: bool = True,
    quality_profile: str = DEFAULT_QUALITY_PROFILE,
    game_dir: Path | None = None,
    asset_mgr: MomoTalkAssetManager | None = None,
    http_session: Any = None,
    max_retries: int = 4,
    stop_event: threading.Event | None = None,
    log_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Upload one student's studio reference samples to Fish Audio `POST /model`
    with <=20-file full-duration paragraph packing (up to profile `max_clips` / `max_dur_sec`),
    exponential backoff, 100%-aligned official transcripts, clean tags, portrait cover,
    and automatic HTTP 400/413/422 self-healing fallback.
    """
    game_dir = resolve_ba_game_dir(game_dir)
    clean_key = (api_key or "").strip()
    if not clean_key:
        return {"ok": False, "fatal_code": "AUTH_ERROR", "error": "Fish Audio API Key가 비어 있습니다."}

    prof_key, prof_cfg = resolve_quality_profile(quality_profile)
    # Select ALL curated clips up to profile `max_clips` (e.g. 30 for plus200) before packing into <=20 files
    profile_max_clips = max(1, int(prof_cfg["max_clips"]))
    profile_max_dur = float(prof_cfg["max_dur_sec"])
    vis = visibility if visibility in VALID_VISIBILITIES else "private"
    comb_wav, samples_dir = _char_ref_dir(job.char_id, job.lang)
    if not is_studio_v2_extracted(job.char_id, job.lang, quality_profile=prof_key, char_data=job.char_data):
        try:
            extract_job_reference(job, force=False, game_dir=game_dir, quality_profile=prof_key)
        except Exception as exc:
            if not (samples_dir.is_dir() and list(samples_dir.glob("*.wav"))):
                return {"ok": False, "fatal_code": "", "error": f"스팀 음성 추출 실패: {exc}"}

    sample_wavs = sorted(samples_dir.glob("*.wav")) if samples_dir.is_dir() else []
    if not sample_wavs and comb_wav.is_file():
        sample_wavs = [comb_wav]
    if not sample_wavs:
        return {"ok": False, "fatal_code": "", "error": "업로드할 WAV 파일이 없습니다."}

    transcript_map: dict[str, str] = {}
    tr_json = samples_dir / "transcripts.json"
    if tr_json.is_file() and not tr_json.is_symlink():
        try:
            loaded_tr = json.loads(tr_json.read_text(encoding="utf-8"))
            if isinstance(loaded_tr, dict):
                transcript_map = {
                    str(k): str(v)
                    for k, v in loaded_tr.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
        except Exception:
            transcript_map = {}

    selected_wavs, texts_list, transcript_attached = select_upload_samples_with_transcript_lock(
        sample_wavs,
        transcript_map,
        max_clips=profile_max_clips,
        min_paired_clips=4,
    )

    cover_bytes: bytes | None = None
    if include_cover or vis == "public":
        try:
            mgr = asset_mgr or MomoTalkAssetManager(ba_root_hint=game_dir)
            cover_bytes = mgr.get_portrait_png(job.char_data, size=256, bg_rgb=(255, 255, 255), ring_rgb=(251, 115, 144))
        except Exception:
            cover_bytes = None

    ch = job.char_data if isinstance(job.char_data, dict) else {}
    cv_name = str(_as_dict(_as_dict(ch.get("voice_variants")).get(job.lang)).get("voice_actor") or "")
    name_tag = job.name_kr if job.lang == "KR" else job.name_jp
    title = build_fish_model_title(job.char_id, name_tag, job.lang, cv_name=cv_name)
    desc = f"Blue Archive {job.name_jp} ({job.name_kr}) {job.lang} studio voice"

    pack_tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        pack_tmp_dir: Path | None = None
        if len(selected_wavs) > FISH_MAX_UPLOAD_FILES:
            preferred_tmp_root = samples_dir.parent
            if preferred_tmp_root.is_dir() and not preferred_tmp_root.is_symlink():
                try:
                    pack_tmp = tempfile.TemporaryDirectory(
                        prefix="ba_fish_pack_",
                        dir=str(preferred_tmp_root),
                    )
                except OSError:
                    pack_tmp = None
            if pack_tmp is None:
                pack_tmp = tempfile.TemporaryDirectory(prefix="ba_fish_pack_")
            pack_tmp_dir = Path(pack_tmp.name)

        pack_res = pack_upload_samples_to_max_files(
            selected_wavs,
            texts_list if transcript_attached else [],
            max_files=FISH_MAX_UPLOAD_FILES,
            max_total_sec=profile_max_dur,
            pause_sec=INTER_CLIP_PAUSE_SEC,
            target_sr=TARGET_SR,
            temp_dir=pack_tmp_dir,
        )
        packed_wavs: list[Path] = list(pack_res["wavs"])
        packed_texts: list[str] = list(pack_res["texts"])
        packed_tr_attached = bool(transcript_attached and packed_texts and len(packed_texts) == len(packed_wavs))

        unpacked_wavs: list[Path] | None = None
        unpacked_texts: list[str] | None = None
        if pack_res.get("packed"):
            src_wavs: list[Path] = list(pack_res.get("source_wavs") or selected_wavs)
            src_texts: list[str] = list(pack_res.get("source_texts") or (texts_list if transcript_attached else []))
            unpacked_wavs = src_wavs[:FISH_MAX_UPLOAD_FILES]
            unpacked_texts = (
                src_texts[: len(unpacked_wavs)]
                if (packed_tr_attached and len(src_texts) >= len(unpacked_wavs))
                else []
            )
            if callable(log_cb):
                tr_lock_txt = " · 공식 대본 100% 정렬" if packed_tr_attached else ""
                log_cb(
                    f"📦 [{job.name_kr} {job.lang}] {int(round(profile_max_dur))}초 풀패킹 적용: "
                    f"{pack_res['source_clip_count']}클립({pack_res['raw_duration_sec']:.1f}초) → "
                    f"{pack_res['packed_file_count']}파일({pack_res['packed_duration_sec']:.1f}초"
                    f"{tr_lock_txt})"
                )

        plans = _build_upload_fallback_plans(
            selected_wavs=packed_wavs,
            texts_list=packed_texts,
            transcript_attached=packed_tr_attached,
            comb_wav=comb_wav,
            vis=vis,
            has_cover=bool(cover_bytes),
            unpacked_wavs=unpacked_wavs,
            unpacked_texts=unpacked_texts,
            packed_from_clips=int(pack_res.get("source_clip_count", len(packed_wavs))),
            packed_duration_sec=float(pack_res.get("packed_duration_sec", 0.0)),
            durations_by_path=pack_res.get("durations_by_path"),
        )
        sess = http_session or requests
        last_err = ""
        plan_idx = 0

        while plan_idx < len(plans):
            plan = plans[plan_idx]
            plan_wavs: list[Path] = plan["wavs"]
            plan_texts: list[str] = plan["texts"]
            use_tags: bool = bool(plan["use_tags"])
            use_cover: bool = bool(plan["use_cover"])

            data_fields: list[tuple[str, str]] = [
                ("type", "tts"),
                ("title", title),
                ("description", desc),
                ("visibility", vis),
                ("train_mode", "fast"),
                ("enhance_audio_quality", "false"),
            ]
            if use_tags:
                data_fields.extend(
                    [
                        ("tags", "blue-archive"),
                        ("tags", f"ba-char-{job.char_id.lower()}"),
                        ("tags", f"ba-lang-{job.lang.lower()}"),
                    ]
                )
            for t_item in plan_texts:
                data_fields.append(("texts", t_item))

            escalate_plan = False
            for attempt in range(max(1, int(max_retries) + 1)):
                if stop_event is not None and stop_event.is_set():
                    return {"ok": False, "fatal_code": "STOPPED", "error": "사용자에 의해 안전 중지되었습니다."}

                handles = []
                try:
                    files: list[tuple[str, Any]] = []
                    for w in plan_wavs:
                        fh = open(w, "rb")
                        handles.append(fh)
                        files.append(("voices", (w.name, fh, "audio/wav")))
                    if use_cover and cover_bytes:
                        files.append(("cover_image", (f"{job.char_id}.png", cover_bytes, "image/png")))

                    post_url = _validate_fish_api_url(FISH_MODEL_URL)
                    if _is_real_requests(sess):
                        resp = sess.post(
                            post_url,
                            headers={"Authorization": f"Bearer {clean_key}"},
                            data=data_fields,
                            files=files,
                            timeout=120,
                            verify=True,
                            allow_redirects=False,
                            stream=True,
                        )
                    else:
                        resp = sess.post(
                            post_url,
                            headers={"Authorization": f"Bearer {clean_key}"},
                            data=data_fields,
                            files=files,
                            timeout=120,
                        )
                    code, resp_text, resp_body, resp_headers = _read_bounded_response(resp, MAX_FISH_JSON_RESPONSE_BYTES)
                except Exception as exc:
                    last_err = _redact_secrets(f"네트워크 오류: {exc}", extra_secrets=(clean_key,))
                    if attempt < max_retries:
                        wait_s = min(60.0, 4.0 * (2**attempt))
                        if callable(log_cb):
                            log_cb(f"⏳ [{job.name_kr} {job.lang}] {last_err} → {wait_s:.0f}초 후 재시도 ({attempt + 1}/{max_retries})")
                        if _interruptible_sleep(wait_s, stop_event):
                            return {"ok": False, "fatal_code": "STOPPED", "error": "사용자에 의해 안전 중지되었습니다."}
                        continue
                    return {"ok": False, "fatal_code": "", "error": last_err}
                finally:
                    for fh in handles:
                        try:
                            fh.close()
                        except Exception:
                            pass

                if code in (200, 201):
                    body = resp_body if isinstance(resp_body, dict) else {}
                    mid = _clean_model_id(str(body.get("_id") or body.get("id") or ""))
                    if not mid:
                        safe_body = _redact_secrets(str(body)[:180], extra_secrets=(clean_key,))
                        return {"ok": False, "fatal_code": "", "error": f"응답에 모델 ID가 없습니다: {safe_body}"}
                    actual_vis = str(body.get("visibility") or vis).strip().lower()
                    if actual_vis != vis and callable(log_cb):
                        log_cb(
                            f"ℹ️ [{job.name_kr} {job.lang}] 요청 공개범위는 '{vis}'였으나 Fish Audio가 '{actual_vis}'로 저장했습니다."
                        )
                    return {
                        "ok": True,
                        "fatal_code": "",
                        "model_id": mid,
                        "visibility": actual_vis,
                        "requested_visibility": vis,
                        "quality_profile": prof_key,
                        "clip_count": len(plan_wavs),
                        "packed_from_clips": int(plan.get("packed_from_clips", len(plan_wavs))),
                        "packed_duration_sec": round(float(plan.get("duration_sec", 0.0)), 2),
                        "transcript_attached": bool(plan_texts),
                        "upload_plan": plan["name"],
                    }

                err_txt = _redact_secrets(resp_text.strip(), extra_secrets=(clean_key,))
                if code == 401:
                    return {"ok": False, "fatal_code": "AUTH_ERROR", "error": "Fish Audio API Key 인증 실패 (HTTP 401). 키를 확인해 주세요."}

                if code in (402, 403):
                    low = err_txt.lower()
                    if "limit" in low or "quota" in low or "credit" in low or "slot" in low or "private" in low or code == 402:
                        hint = (
                            "Fish Audio 요금제 슬롯 또는 크레딧 한도에 도달했습니다 "
                            "(Plus: 비공개 10개 / 공개 무제한 · Pro: 비공개 무제한). "
                            "지금까지 성공한 모델은 모두 자동 저장되었으며, 요금제 업그레이드 또는 공개 범위 변경 후 이어서 실행할 수 있습니다."
                        )
                        return {"ok": False, "fatal_code": "QUOTA_LIMIT", "error": f"{hint} [HTTP {code}: {err_txt[:180]}]"}
                    return {"ok": False, "fatal_code": "QUOTA_LIMIT", "error": f"Fish Audio 권한/한도 오류 (HTTP {code}): {err_txt[:180]}"}

                if code == 429:
                    wait_s = _parse_retry_after(resp_headers, default_sec=min(90.0, 5.0 * (2**attempt)))
                    last_err = f"요청 속도 제한 (HTTP 429): {err_txt[:140]}"
                    if attempt < max_retries:
                        if callable(log_cb):
                            log_cb(f"⏳ [{job.name_kr} {job.lang}] 속도 제한(429) → {wait_s:.0f}초 대기 후 재시도 ({attempt + 1}/{max_retries})")
                        if _interruptible_sleep(wait_s, stop_event):
                            return {"ok": False, "fatal_code": "STOPPED", "error": "사용자에 의해 안전 중지되었습니다."}
                        continue
                    return {"ok": False, "fatal_code": "", "error": last_err}

                if code >= 500:
                    wait_s = min(60.0, 4.0 * (2**attempt))
                    last_err = f"Fish Audio 서버 일시 오류 (HTTP {code}): {err_txt[:140]}"
                    if attempt < max_retries:
                        if callable(log_cb):
                            log_cb(f"⏳ [{job.name_kr} {job.lang}] 서버 오류({code}) → {wait_s:.0f}초 후 재시도 ({attempt + 1}/{max_retries})")
                        if _interruptible_sleep(wait_s, stop_event):
                            return {"ok": False, "fatal_code": "STOPPED", "error": "사용자에 의해 안전 중지되었습니다."}
                        continue
                    return {"ok": False, "fatal_code": "", "error": last_err}

                if code in (400, 413, 422):
                    last_err = f"업로드 요청 검증 오류 (HTTP {code}): {err_txt[:200]}"
                    if plan_idx + 1 < len(plans):
                        next_plan = plans[plan_idx + 1]
                        if callable(log_cb):
                            log_cb(
                                f"🛠️ [{job.name_kr} {job.lang}] HTTP {code} 응답 → "
                                f"자동 호환 폴백({next_plan['name']})으로 재시도합니다..."
                            )
                        escalate_plan = True
                        break
                    return {"ok": False, "fatal_code": "", "error": last_err}

                return {"ok": False, "fatal_code": "", "error": f"업로드 실패 (HTTP {code}): {err_txt[:200]}"}

            if escalate_plan:
                plan_idx += 1
                continue
            break

        return {"ok": False, "fatal_code": "", "error": last_err or "알 수 없는 업로드 오류"}
    finally:
        if pack_tmp is not None:
            try:
                pack_tmp.cleanup()
            except Exception:
                shutil.rmtree(pack_tmp.name, ignore_errors=True)


def run_fish_batch_pipeline(
    mode: str = "extract_only",  # "extract_only" | "sync_only" | "upload" | "dry_run"
    langs: tuple[str, ...] = ("JP", "KR"),
    visibility: str = "private",
    quality_profile: str = DEFAULT_QUALITY_PROFILE,
    api_key: str = "",
    game_dir: Path | None = None,
    characters: dict[str, dict[str, Any]] | None = None,
    cfg: dict[str, Any] | None = None,
    character_filter: Iterable[str] | None = None,
    limit: int | None = None,
    delay_sec: float = 1.5,
    include_cover: bool = True,
    force_extract: bool = False,
    sync_before_upload: bool = True,
    replace_external: bool = True,
    force_recreate: bool = False,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[BatchProgress], None] | None = None,
    on_model_linked: Callable[[str, str, str], None] | None = None,
    log_cb: Callable[[str], None] | None = None,
    http_session: Any = None,
    persist_voice_link_cb: Callable[[str, str, str], None] | None = None,
) -> dict[str, Any]:
    """
    Full-automation resumable pipeline for extracting and/or uploading all voiced Blue Archive
    characters (JP & KR) to Fish Audio and linking them straight into MomoTalk config.json.

    When `replace_external=True` (default), students who currently have an external/community
    link in `config.json` are backed up once to `saved_voices_pre_batch_backup.json` and rebuilt
    as the user's OWN high-quality Fish Audio models, while models already created on the user's
    own account (`owned_by_self=True`) are safely skipped on resume so zero duplicates are created.
    """
    prof_key, _ = resolve_quality_profile(quality_profile)
    all_chars = characters if characters is not None else load_all_character_configs()
    if cfg is not None:
        user_cfg = copy.deepcopy(cfg) if callable(persist_voice_link_cb) else cfg
    else:
        user_cfg = load_user_config()
    game_dir = resolve_ba_game_dir(game_dir, user_cfg)
    eff_key = (api_key or str(user_cfg.get("fish_api_key") or "")).strip()
    manifest = load_batch_manifest()
    _ensure_dict(manifest, "entries")

    counts = {
        "total_jobs": 0,
        "extracted": 0,
        "uploaded": 0,
        "replaced_external": 0,
        "synced": 0,
        "skipped": 0,
        "failed": 0,
    }

    def _emit(
        phase: str,
        cur: int,
        tot: int,
        job: BatchVoiceJob | None = None,
        status: str = "",
        msg: str = "",
    ) -> None:
        safe_msg = _redact_secrets(msg, extra_secrets=(eff_key,)) if msg else ""
        if callable(log_cb) and safe_msg:
            log_cb(safe_msg)
        if callable(on_progress):
            on_progress(
                BatchProgress(
                    phase=phase,
                    current=cur,
                    total=tot,
                    char_id=job.char_id if job else "",
                    name_kr=job.name_kr if job else "",
                    lang=job.lang if job else "",
                    status=status,
                    message=safe_msg,
                    counts=dict(counts),
                )
            )

    # Preflight environment validation (API key, Steam dirs, disk space)
    pre = run_preflight_checks(mode=mode, langs=langs, api_key=eff_key, game_dir=game_dir, log_cb=log_cb)
    if not pre.get("ok"):
        err = _redact_secrets(str(pre.get("error") or "사전 점검 실패"), extra_secrets=(eff_key,))
        _emit("error", 0, 1, status="error", msg=f"❌ {err}")
        return {"ok": False, "fatal_code": pre.get("fatal_code", ""), "error": err, "counts": counts}

    owns_session = http_session is None and mode in ("sync_only", "upload")
    if owns_session:
        sess = requests.Session()
        sess.verify = True
        sess.max_redirects = 0
    else:
        sess = http_session
    self_owned_keys: set[tuple[str, str]] = set()

    try:
        # Optional pre-upload account sync so models already on the user's account are linked & never duplicated
        if mode in ("sync_only", "upload") and (mode == "sync_only" or sync_before_upload):
            _emit("sync", 0, 1, status="syncing", msg="🔄 내 Fish Audio 계정의 기존 보이스 모델을 동기화하는 중...")
            sync_res = sync_existing_fish_models(
                api_key=eff_key,
                cfg=user_cfg,
                characters=all_chars,
                overwrite=bool(replace_external),
                http_session=sess,
                log_cb=log_cb,
                manifest=manifest,
                stop_event=stop_event,
                persist_voice_link_cb=persist_voice_link_cb,
            )
            counts["synced"] = _safe_int(sync_res.get("synced"), 0)
            if not sync_res.get("ok"):
                err = str(sync_res.get("error") or "계정 동기화 실패")
                fcode = str(sync_res.get("fatal_code") or "")
                if fcode == "STOPPED":
                    _emit("stopped", 0, 1, status="stopped", msg=f"⏹ {err}")
                    return {"ok": True, "fatal_code": "STOPPED", "error": err, "counts": counts}
                _emit("error", 0, 1, status="error", msg=f"❌ {err}")
                return {"ok": False, "fatal_code": fcode, "error": err, "counts": counts}
            for pair in sync_res.get("matched_keys") or []:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    self_owned_keys.add((str(pair[0]), str(pair[1]).upper()))
            if callable(on_model_linked):
                for key_str, mid in (sync_res.get("synced_models") or {}).items():
                    if "::" in key_str:
                        scid, slang = key_str.split("::", 1)
                        try:
                            on_model_linked(scid, slang, str(mid))
                        except Exception:
                            pass
            if mode == "sync_only":
                _emit("done", 1, 1, status="done", msg=f"✅ 계정 모델 동기화 완료 (새로 연결 {counts['synced']}개)")
                return {"ok": True, "fatal_code": "", "counts": counts, "sync": sync_res}

        scan = scan_voiced_roster(
            all_chars,
            user_cfg,
            langs=langs,
            game_dir=game_dir,
            character_filter=character_filter,
            quality_profile=prof_key,
            manifest=manifest,
            self_owned_keys=self_owned_keys,
        )
        jobs: list[BatchVoiceJob] = scan["jobs"]
        counts["total_jobs"] = len(jobs)
        summary = scan["summary"]
        ext_total = summary["jp_external_linked"] + summary["kr_external_linked"]
        owned_total = summary["jp_self_owned"] + summary["kr_self_owned"]
        _emit(
            "scan",
            0,
            len(jobs),
            status="scanned",
            msg=(
                f"📋 스캔 완료: 🇯🇵 JP {summary['jp_voiced']}명 (내 모델 {summary['jp_self_owned']} · 외부링크 {summary['jp_external_linked']} · 추출 {summary['jp_extracted']}) / "
                f"🇰🇷 KR {summary['kr_voiced']}명 (내 모델 {summary['kr_self_owned']} · 외부링크 {summary['kr_external_linked']} · 추출 {summary['kr_extracted']}) — 총 {len(jobs)}건"
            ),
        )

        def _should_skip_upload(j: BatchVoiceJob) -> bool:
            if force_recreate:
                return False
            if j.owned_by_self:
                return True
            if (not replace_external) and bool(j.existing_model_id):
                return True
            return False

        if mode == "dry_run":
            todo_upload = [j for j in jobs if not _should_skip_upload(j)]
            todo_extract = [j for j in jobs if not j.already_extracted]
            replace_cnt = sum(1 for j in todo_upload if j.existing_model_id and not j.owned_by_self)
            policy_txt = (
                f"외부링크 {replace_cnt}건 포함 자체 생성(자동 백업 후 교체)"
                if replace_external
                else "기존 링크 유지"
            )
            _emit(
                "done",
                len(jobs),
                len(jobs),
                status="dry_run",
                msg=(
                    f"🧪 드라이런 완료: 추출 대기 {len(todo_extract)}건 · 업로드 대기 {len(todo_upload)}건 "
                    f"(내 모델 {owned_total}건 완료 · {policy_txt} · 실제 API 호출 없음)"
                ),
            )
            return {
                "ok": True,
                "fatal_code": "",
                "counts": counts,
                "summary": summary,
                "todo_upload": len(todo_upload),
                "todo_extract": len(todo_extract),
                "external_to_replace": replace_cnt,
            }

        asset_mgr = MomoTalkAssetManager(ba_root_hint=game_dir) if (mode == "upload" and (include_cover or visibility == "public")) else None
        uploads_done_this_run = 0
        limit_reached = False
        fatal_code = ""
        fatal_err = ""

        for idx, job in enumerate(jobs, 1):
            if stop_event is not None and stop_event.is_set():
                fatal_code = "STOPPED"
                fatal_err = "사용자에 의해 안전 중지되었습니다 (진행 상태 저장 완료)."
                _emit("stopped", idx - 1, len(jobs), job=job, status="stopped", msg=f"⏹ {fatal_err}")
                break

            if mode == "extract_only":
                try:
                    res = extract_job_reference(job, force=force_extract, game_dir=game_dir, quality_profile=prof_key)
                    if res.get("skipped"):
                        counts["skipped"] += 1
                        _emit("extract", idx, len(jobs), job=job, status="skipped", msg=f"[{idx}/{len(jobs)}] ⏭ {job.name_kr} ({job.lang}) 이미 추출됨 ({prof_key})")
                    else:
                        counts["extracted"] += 1
                        _update_manifest_entry_locked(
                            job.key,
                            {
                                "char_id": job.char_id,
                                "name_kr": job.name_kr,
                                "lang": job.lang,
                                "pipeline_version": _safe_int(res.get("pipeline_version"), STUDIO_PIPELINE_VERSION),
                                "quality_profile": prof_key,
                                "extracted": True,
                                "clip_count": _safe_int(res.get("clip_count"), 0),
                                "memoriallobby_clips": _safe_int(res.get("memoriallobby_clips"), 0),
                                "duration_sec": _safe_float(res.get("total_duration_sec"), 0.0),
                                "transcript_matched": _safe_int(res.get("transcript_matched"), 0),
                                "transcript_coverage": _safe_float(res.get("transcript_coverage"), 0.0),
                                "transcript_locked": bool(res.get("transcript_locked", False)),
                                "gamedata_available": bool(res.get("gamedata_available", False)),
                                "updated_at": time.time(),
                            },
                            manifest=manifest,
                        )
                        lock_tag = " · 대본100%" if res.get("transcript_locked") else ""
                        _emit(
                            "extract",
                            idx,
                            len(jobs),
                            job=job,
                            status="extracted",
                            msg=(
                                f"[{idx}/{len(jobs)}] ✅ {job.name_kr} ({job.lang}) 추출 완료 "
                                f"({res.get('clip_count', 0)}클립 · {res.get('total_duration_sec', 0)}초 · "
                                f"대본 {res.get('transcript_matched', 0)}/{res.get('clip_count', 0)}{lock_tag})"
                            ),
                        )
                except Exception as exc:
                    counts["failed"] += 1
                    safe_exc = _redact_secrets(str(exc), extra_secrets=(eff_key,))
                    _update_manifest_entry_locked(
                        job.key,
                        {
                            "char_id": job.char_id,
                            "name_kr": job.name_kr,
                            "lang": job.lang,
                            "error": safe_exc,
                            "updated_at": time.time(),
                        },
                        manifest=manifest,
                    )
                    _emit("extract", idx, len(jobs), job=job, status="failed", msg=f"[{idx}/{len(jobs)}] ⚠️ {job.name_kr} ({job.lang}) 추출 실패: {safe_exc}")
                continue

            # mode == "upload"
            if _should_skip_upload(job):
                counts["skipped"] += 1
                skip_reason = "내 계정 모델 완료" if job.owned_by_self else "기존 링크 유지"
                short_id = f" ({job.existing_model_id[:8]}…)" if job.existing_model_id else ""
                _emit("upload", idx, len(jobs), job=job, status="skipped", msg=f"[{idx}/{len(jobs)}] 💗 {job.name_kr} ({job.lang}) {skip_reason}{short_id}")
                continue

            if limit is not None and limit > 0 and uploads_done_this_run >= limit:
                limit_reached = True
                _emit("done", idx - 1, len(jobs), job=job, status="limit_reached", msg=f"⏸ 설정한 업로드 상한({limit}개)에 도달해 정상 종료했습니다.")
                break

            if not job.already_extracted:
                try:
                    ext_res = extract_job_reference(job, force=False, game_dir=game_dir, quality_profile=prof_key)
                    if not ext_res.get("skipped"):
                        counts["extracted"] += 1
                except Exception as exc:
                    safe_exc = _redact_secrets(str(exc), extra_secrets=(eff_key,))
                    if _has_any_local_wavs(job.char_id, job.lang):
                        _emit(
                            "upload",
                            idx,
                            len(jobs),
                            job=job,
                            status="extract_fallback",
                            msg=f"[{idx}/{len(jobs)}] ⚠️ {job.name_kr} ({job.lang}) 재추출 실패({safe_exc}) → 기존 로컬 WAV로 업로드를 계속합니다.",
                        )
                    else:
                        counts["failed"] += 1
                        _update_manifest_entry_locked(
                            job.key,
                            {
                                "char_id": job.char_id,
                                "name_kr": job.name_kr,
                                "lang": job.lang,
                                "error": safe_exc,
                                "updated_at": time.time(),
                            },
                            manifest=manifest,
                        )
                        _emit("upload", idx, len(jobs), job=job, status="failed", msg=f"[{idx}/{len(jobs)}] ⚠️ {job.name_kr} ({job.lang}) 추출 실패: {safe_exc}")
                        continue

            replacing_ext = bool(job.existing_model_id) and (not job.owned_by_self)
            rep_note = " · 기존 외부링크 교체" if replacing_ext else ""
            _emit("upload", idx, len(jobs), job=job, status="uploading", msg=f"[{idx}/{len(jobs)}] 🚀 {job.name_kr} ({job.lang}) Fish Audio 모델 업로드 중 ({visibility} · {prof_key}{rep_note})...")
            up_res = upload_job_to_fish_audio(
                job=job,
                api_key=eff_key,
                visibility=visibility,
                include_cover=include_cover,
                quality_profile=prof_key,
                game_dir=game_dir,
                asset_mgr=asset_mgr,
                http_session=sess,
                stop_event=stop_event,
                log_cb=log_cb,
            )

            if up_res.get("ok"):
                mid = str(up_res["model_id"])
                actual_vis = str(up_res.get("visibility") or visibility)
                bucket_name = "saved_voices_kr" if job.lang == "KR" else "saved_voices"
                with _BATCH_PERSIST_LOCK:
                    bucket = _ensure_dict(user_cfg, bucket_name)
                    prev_id = _clean_model_id(str(bucket.get(job.char_id) or ""))
                    if prev_id and prev_id != mid:
                        backup_saved_voices_once(user_cfg, log_cb=log_cb)
                    bucket[job.char_id] = mid
                    _sync_active_reference_id(user_cfg, job.char_id, job.lang, mid)
                    if not callable(persist_voice_link_cb):
                        save_user_config(copy.deepcopy(user_cfg))
                if callable(persist_voice_link_cb):
                    persist_voice_link_cb(job.char_id, job.lang, mid)

                uploads_done_this_run += 1
                counts["uploaded"] += 1
                if replacing_ext:
                    counts["replaced_external"] += 1
                record_self_owned_model(
                    char_id=job.char_id,
                    name_kr=job.name_kr,
                    lang=job.lang,
                    model_id=mid,
                    visibility=actual_vis,
                    quality_profile=prof_key,
                    manifest=manifest,
                )

                if callable(on_model_linked):
                    try:
                        on_model_linked(job.char_id, job.lang, mid)
                    except Exception:
                        pass

                plan_name = str(up_res.get("upload_plan") or "")
                packed_from = _safe_int(up_res.get("packed_from_clips"), 0)
                clip_cnt = _safe_int(up_res.get("clip_count"), 0)
                packed_dur = _safe_float(up_res.get("packed_duration_sec"), 0.0)
                if plan_name == "packed_optimal" and packed_from > clip_cnt:
                    plan_note = f" [풀패킹 {packed_from}클립→{clip_cnt}파일 · {packed_dur:.1f}초]"
                elif plan_name not in ("", "optimal"):
                    plan_note = f" [{plan_name}]"
                else:
                    plan_note = ""
                rep_done = " (기존 외부링크 → 내 모델 교체)" if replacing_ext else ""
                _emit(
                    "upload",
                    idx,
                    len(jobs),
                    job=job,
                    status="uploaded",
                    msg=f"[{idx}/{len(jobs)}] 💗 {job.name_kr} ({job.lang}) 업로드 & 자동 연결 완료!{rep_done}{plan_note} ({mid})",
                )
                if delay_sec > 0 and idx < len(jobs):
                    if _interruptible_sleep(delay_sec, stop_event):
                        fatal_code = "STOPPED"
                        fatal_err = "사용자에 의해 안전 중지되었습니다 (진행 상태 저장 완료)."
                        _emit("stopped", idx, len(jobs), job=job, status="stopped", msg=f"⏹ {fatal_err}")
                        break
            else:
                fcode = str(up_res.get("fatal_code") or "")
                err_msg = _redact_secrets(str(up_res.get("error") or "업로드 실패"), extra_secrets=(eff_key,))
                if fcode == "STOPPED":
                    fatal_code = "STOPPED"
                    fatal_err = err_msg
                    _emit("stopped", idx - 1, len(jobs), job=job, status="stopped", msg=f"⏹ {err_msg}")
                    break
                counts["failed"] += 1
                _update_manifest_entry_locked(
                    job.key,
                    {
                        "char_id": job.char_id,
                        "name_kr": job.name_kr,
                        "lang": job.lang,
                        "error": err_msg,
                        "updated_at": time.time(),
                    },
                    manifest=manifest,
                )
                if fcode in ("AUTH_ERROR", "QUOTA_LIMIT"):
                    fatal_code = fcode
                    fatal_err = err_msg
                    _emit("error", idx, len(jobs), job=job, status="error", msg=f"🛑 {err_msg}")
                    break
                _emit("upload", idx, len(jobs), job=job, status="failed", msg=f"[{idx}/{len(jobs)}] ⚠️ {job.name_kr} ({job.lang}) 실패: {err_msg}")

        if not fatal_code and not limit_reached:
            rep_str = f" (외부링크 교체 {counts['replaced_external']}건 포함)" if counts["replaced_external"] else ""
            _emit(
                "done",
                len(jobs),
                len(jobs),
                status="done",
                msg=(
                    f"🎉 일괄 작업 완료! 추출 {counts['extracted']} · 업로드 {counts['uploaded']}{rep_str} · "
                    f"계정동기화 {counts['synced']} · 건너뜀 {counts['skipped']} · 실패 {counts['failed']}"
                ),
            )

        return {
            "ok": fatal_code in ("", "STOPPED"),
            "fatal_code": fatal_code,
            "error": fatal_err,
            "counts": counts,
            "summary": summary,
            "manifest_path": str(get_batch_manifest_path()),
        }
    finally:
        if owns_session and sess is not None:
            try:
                sess.close()
            except Exception:
                pass


def _has_any_local_wavs(char_id: str, lang: str) -> bool:
    comb_wav, samples_dir = _char_ref_dir(char_id, lang)
    try:
        if comb_wav.is_file() and comb_wav.stat().st_size > 44:
            return True
        if samples_dir.is_dir() and any(w.stat().st_size > 44 for w in samples_dir.glob("*.wav")):
            return True
    except OSError:
        pass
    return False


def get_student_voice_availability(
    char_data: dict[str, Any],
    lang: str = "JP",
    game_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """
    Classify whether `char_data` can be auto-generated from local Steam `.molru` packs for `lang`:
      - `"steam_ready"`       : Steam `.molru` or pre-extracted WAVs exist for `lang`
      - `"fallback_other_lang"`: No `.molru` for `lang`, but the other language (e.g. JP for collab/new) exists
      - `"unvoiced_npc"`      : Character has no official voice pack in either language
      - `"steam_unknown"`     : Steam Blue Archive isn't detected on this PC yet, but character is voiced
    """
    cid = str(char_data.get("id") or "").strip()
    lang_up = "KR" if str(lang or "JP").strip().upper() == "KR" else "JP"
    other_lang = "JP" if lang_up == "KR" else "KR"

    if cid and is_safe_entity_id(cid) and _has_any_local_wavs(cid, lang_up):
        return "steam_ready"

    eff_game_dir = resolve_ba_game_dir(game_dir, cfg)
    has_cur = character_has_steam_voice_pack(char_data, lang=lang_up, game_dir=eff_game_dir)
    if has_cur is True:
        return "steam_ready"
    if has_cur is False:
        has_other = character_has_steam_voice_pack(char_data, lang=other_lang, game_dir=eff_game_dir)
        if has_other is True:
            return "fallback_other_lang"
        return "unvoiced_npc"

    # Steam not detected yet — inspect character JSON metadata cleanly
    packs = char_data.get("local_voice_packs") if isinstance(char_data.get("local_voice_packs"), dict) else {}
    cur_packs = packs.get(lang_up.lower()) or []
    other_packs = packs.get(other_lang.lower()) or []
    if cur_packs:
        return "steam_unknown"
    if other_packs:
        return "fallback_other_lang"
    category = str(char_data.get("category") or "").strip().lower()
    molru_prefix = str(char_data.get("molru_prefix") or "").strip()
    if category == "npc" and not molru_prefix:
        return "unvoiced_npc"
    return "steam_unknown"


def auto_generate_and_link_student_voice(
    char_data: dict[str, Any],
    lang: str = "JP",
    api_key: str = "",
    cfg: dict[str, Any] | None = None,
    characters: dict[str, dict[str, Any]] | None = None,
    visibility: str | None = None,
    quality_profile: str | None = None,
    game_dir: Path | None = None,
    include_cover: bool = True,
    check_account_first: bool = True,
    include_alt_voices: bool = True,
    force_recreate: bool = False,
    asset_mgr: MomoTalkAssetManager | None = None,
    http_session: Any = None,
    stop_event: threading.Event | None = None,
    on_stage: Callable[[str, str], None] | None = None,
    on_model_linked: Callable[[str, str, str], None] | None = None,
    log_cb: Callable[[str], None] | None = None,
    persist_voice_link_cb: Callable[[str, str, str], None] | None = None,
) -> dict[str, Any]:
    """
    On-demand 1-click voice creation & auto-linking engine designed for distributed end-users.

    Flow for a single student `(char_data, lang)`:
      1. Validate Fish Audio API key.
      2. Optionally check the user's own Fish Audio account (`GET /model?self=true`) first so
         models already generated earlier (or on another PC) are linked instantly with ZERO duplicates.
      3. Auto-discover Steam Blue Archive (or use saved `cfg['steam_game_dir']` / existing local WAVs),
         extract & master clean studio WAVs (+ official transcripts if available).
      4. Upload to Fish Audio (`POST /model`) with portrait cover, clean tags, 429/5xx retry, and
         HTTP 400/413/422 progressive self-healing fallback.
      5. Save the returned Model ID into `saved_voices` / `saved_voices_kr` + `fish_reference_id`,
         update `fish_batch_manifest.json`, and invoke `on_model_linked(char_id, lang, model_id)`.
      6. If `include_alt_voices=True` and the student declares `alt_voice_personas` (e.g. Kisaki -> Kiki),
         extract/upload/link any missing alt voice slot too (non-fatal if an extra slot hits quota).
    """
    if cfg is not None:
        user_cfg = copy.deepcopy(cfg) if callable(persist_voice_link_cb) else cfg
    else:
        user_cfg = load_user_config()
    eff_key = (api_key or str(user_cfg.get("fish_api_key") or "")).strip()
    cid = str(char_data.get("id") or "").strip()
    name_kr = str(char_data.get("name_kr") or cid)
    name_jp = str(char_data.get("name_jp") or cid)
    lang_up = "KR" if str(lang or "JP").strip().upper() == "KR" else "JP"
    rlang = "ko" if lang_up == "KR" else "ja"
    bucket_name = "saved_voices_kr" if lang_up == "KR" else "saved_voices"

    raw_vis = str(visibility or user_cfg.get("auto_voice_visibility") or "private").strip().lower()
    eff_vis = raw_vis if raw_vis in VALID_VISIBILITIES else "private"
    prof_key, _ = resolve_quality_profile(quality_profile or str(user_cfg.get("auto_voice_quality_profile") or DEFAULT_QUALITY_PROFILE))

    def _stage(stage: str, msg: str) -> None:
        safe_msg = _redact_secrets(msg, extra_secrets=(eff_key,)) if msg else ""
        if callable(log_cb) and safe_msg:
            log_cb(safe_msg)
        if callable(on_stage):
            try:
                on_stage(stage, safe_msg)
            except Exception:
                pass

    if not cid or not is_safe_entity_id(cid):
        return {"ok": False, "fatal_code": "INVALID_CHAR", "error": "유효한 캐릭터 정보가 없습니다."}

    if not eff_key:
        err = "Fish Audio API Key가 비어 있습니다. 설정 1단계에 키를 입력해 주세요."
        _stage("error", f"❌ {err}")
        return {"ok": False, "fatal_code": "AUTH_ERROR", "error": err, "char_id": cid, "lang": lang_up}

    all_chars = characters if characters is not None else {cid: char_data}
    if cid not in all_chars:
        all_chars = {**all_chars, cid: char_data}

    manifest = load_batch_manifest()
    eff_game_dir = resolve_ba_game_dir(game_dir, user_cfg)
    owns_session = http_session is None
    if owns_session:
        sess = requests.Session()
        sess.verify = True
        sess.max_redirects = 0
    else:
        sess = http_session

    def _persist_link(target_cid: str, target_name_kr: str, model_id: str, actual_vis: str) -> None:
        with _BATCH_PERSIST_LOCK:
            bucket = _ensure_dict(user_cfg, bucket_name)
            prev_id = _clean_model_id(str(bucket.get(target_cid) or ""))
            if prev_id and prev_id != model_id:
                backup_saved_voices_once(user_cfg, log_cb=log_cb)
            bucket[target_cid] = model_id
            _sync_active_reference_id(user_cfg, target_cid, lang_up, model_id)
            if not callable(persist_voice_link_cb):
                save_user_config(copy.deepcopy(user_cfg))
            record_self_owned_model(
                char_id=target_cid,
                name_kr=target_name_kr,
                lang=lang_up,
                model_id=model_id,
                visibility=actual_vis,
                quality_profile=prof_key,
                manifest=manifest,
            )
        if callable(persist_voice_link_cb):
            persist_voice_link_cb(target_cid, lang_up, model_id)
        if callable(on_model_linked):
            try:
                on_model_linked(target_cid, lang_up, model_id)
            except Exception:
                pass

    def _build_and_link_alt_voices(matched_remote: dict[str, str]) -> list[dict[str, Any]]:
        alt_results: list[dict[str, Any]] = []
        if not include_alt_voices or char_data.get("alt_voice_key"):
            return alt_results
        alts = char_data.get("alt_voice_personas")
        if not isinstance(alts, dict):
            return alt_results

        for ak in sorted(alts.keys()):
            if stop_event is not None and stop_event.is_set():
                break
            alt_ch = build_alt_voice_char_data(char_data, ak)
            if alt_ch is None:
                continue
            alt_cid = str(alt_ch["id"])
            alt_name = str(alt_ch.get("name_kr") or alt_cid)
            alt_existing = _clean_model_id(str(_as_dict(user_cfg.get(bucket_name)).get(alt_cid) or ""))
            remote_alt_id = matched_remote.get(f"{alt_cid}::{lang_up}", "")
            if (not force_recreate) and remote_alt_id:
                _persist_link(alt_cid, alt_name, remote_alt_id, eff_vis)
                alt_results.append({"ok": True, "char_id": alt_cid, "model_id": remote_alt_id, "source": "account_sync"})
                continue
            if (not force_recreate) and alt_existing:
                alt_results.append({"ok": True, "char_id": alt_cid, "model_id": alt_existing, "source": "existing"})
                continue

            alt_already = is_studio_v2_extracted(alt_cid, lang_up, quality_profile=prof_key, char_data=alt_ch)
            alt_molrus = (
                find_character_voice_molru_files(alt_ch, lang=lang_up, game_dir=eff_game_dir)
                if is_valid_ba_game_dir(eff_game_dir)
                else []
            )
            if not alt_already and not alt_molrus and not _has_any_local_wavs(alt_cid, lang_up):
                continue

            _stage("alt", f"🎭 보조 보이스 [{alt_name} ({lang_up})] 자동 생성 중...")
            alt_job = BatchVoiceJob(
                char_id=alt_cid,
                name_kr=alt_name,
                name_jp=str(alt_ch.get("name_jp") or alt_cid),
                lang=lang_up,
                reply_lang=rlang,
                molru_files=alt_molrus,
                already_extracted=alt_already,
                existing_model_id=alt_existing,
                char_data=alt_ch,
            )
            try:
                if not alt_job.already_extracted and alt_molrus:
                    extract_job_reference(alt_job, force=False, game_dir=eff_game_dir, quality_profile=prof_key)
                alt_up = upload_job_to_fish_audio(
                    job=alt_job,
                    api_key=eff_key,
                    visibility=eff_vis,
                    include_cover=include_cover,
                    quality_profile=prof_key,
                    game_dir=eff_game_dir,
                    asset_mgr=asset_mgr,
                    http_session=sess,
                    stop_event=stop_event,
                    log_cb=log_cb,
                )
            except Exception as exc:
                alt_up = {"ok": False, "fatal_code": "", "error": _redact_secrets(str(exc), extra_secrets=(eff_key,))}

            if alt_up.get("ok"):
                alt_mid = str(alt_up["model_id"])
                _persist_link(alt_cid, alt_name, alt_mid, str(alt_up.get("visibility") or eff_vis))
                alt_results.append({"ok": True, "char_id": alt_cid, "model_id": alt_mid, "source": "created"})
            else:
                alt_results.append(
                    {
                        "ok": False,
                        "char_id": alt_cid,
                        "fatal_code": str(alt_up.get("fatal_code") or ""),
                        "error": str(alt_up.get("error") or ""),
                    }
                )
                if str(alt_up.get("fatal_code") or "") == "STOPPED":
                    break
        return alt_results

    try:
        matched_remote: dict[str, str] = {}
        # 1. Check user's own Fish Audio account first to reuse existing models with ZERO duplicates
        if check_account_first and not force_recreate:
            _stage("sync", f"🔍 내 Fish Audio 계정에 기존 [{name_kr} ({lang_up})] 모델이 있는지 확인 중...")
            sync_res = sync_existing_fish_models(
                api_key=eff_key,
                cfg=user_cfg,
                characters=all_chars,
                overwrite=False,
                http_session=sess,
                log_cb=log_cb,
                manifest=manifest,
                stop_event=stop_event,
                persist_voice_link_cb=persist_voice_link_cb,
            )
            if not sync_res.get("ok") and sync_res.get("fatal_code") in ("AUTH_ERROR", "STOPPED"):
                fcode = str(sync_res.get("fatal_code") or "")
                err = str(sync_res.get("error") or "Fish Audio API Key 인증 실패")
                _stage("stopped" if fcode == "STOPPED" else "error", f"{'⏹' if fcode == 'STOPPED' else '❌'} {err}")
                return {"ok": False, "fatal_code": fcode, "error": err, "char_id": cid, "lang": lang_up}

            if sync_res.get("ok"):
                matched_remote = dict(sync_res.get("matched_models") or {})
                existing_remote_id = _clean_model_id(str(matched_remote.get(f"{cid}::{lang_up}") or ""))
                # Notify UI ONLY for models newly linked during account sync (do not overwrite pinned voices)
                if callable(on_model_linked):
                    for key_str, rmid in (sync_res.get("synced_models") or {}).items():
                        if existing_remote_id and key_str == f"{cid}::{lang_up}":
                            continue
                        if "::" in key_str:
                            scid, slang = key_str.split("::", 1)
                            try:
                                on_model_linked(scid, slang, str(rmid))
                            except Exception:
                                pass
                if existing_remote_id:
                    _stage("link", f"🔄 내 계정의 기존 [{name_kr} ({lang_up})] 모델을 자동 연결합니다...")
                    _persist_link(cid, name_kr, existing_remote_id, eff_vis)
                    alt_results = _build_and_link_alt_voices(matched_remote)
                    _stage("done", f"💗 [{name_kr} ({lang_up})] 내 계정 기존 보이스 연결 완료! ({existing_remote_id})")
                    return {
                        "ok": True,
                        "source": "account_sync",
                        "char_id": cid,
                        "name_kr": name_kr,
                        "lang": lang_up,
                        "model_id": existing_remote_id,
                        "visibility": eff_vis,
                        "quality_profile": prof_key,
                        "clip_count": 0,
                        "transcript_attached": False,
                        "alt_results": alt_results,
                        "fatal_code": "",
                        "error": "",
                    }

        # 2. Prepare studio reference WAVs (use existing local cache or auto-extract from Steam)
        already_extracted = is_studio_v2_extracted(cid, lang_up, quality_profile=prof_key, char_data=char_data)
        has_local_wavs = _has_any_local_wavs(cid, lang_up)
        steam_valid = is_valid_ba_game_dir(eff_game_dir)
        molru_files: list[Path] = []

        if not already_extracted:
            if not steam_valid and not has_local_wavs:
                err = (
                    f"스팀 블루 아카이브 설치 폴더를 찾지 못했습니다 (확인 경로: {eff_game_dir}).\n"
                    "스팀 블루 아카이브 폴더를 한 번만 지정하면 모든 학생 목소리를 원클릭으로 자동 생성할 수 있습니다."
                )
                _stage("error", f"⚠️ {err}")
                return {
                    "ok": False,
                    "fatal_code": "STEAM_MISSING",
                    "error": err,
                    "char_id": cid,
                    "name_kr": name_kr,
                    "lang": lang_up,
                    "game_dir": str(eff_game_dir),
                }

            if steam_valid:
                molru_files = find_character_voice_molru_files(char_data, lang=lang_up, game_dir=eff_game_dir)
                if not molru_files and not has_local_wavs:
                    other_lang = "JP" if lang_up == "KR" else "KR"
                    other_molrus = find_character_voice_molru_files(char_data, lang=other_lang, game_dir=eff_game_dir)
                    fb_lang = other_lang if other_molrus else ""
                    err = (
                        f"스팀 클라이언트에서 '{name_kr}' ({lang_up}) 음성 팩(.molru)을 찾을 수 없습니다."
                        + (
                            f" ({other_lang} 원음은 보유 중입니다.)"
                            if fb_lang
                            else " (보이스 미실장 NPC일 수 있습니다.)"
                        )
                    )
                    _stage("error", f"⚠️ {err}")
                    return {
                        "ok": False,
                        "fatal_code": "NO_VOICE_PACK",
                        "fallback_lang": fb_lang,
                        "error": err,
                        "char_id": cid,
                        "name_kr": name_kr,
                        "lang": lang_up,
                    }

            # Check free disk space before writing studio WAVs
            if molru_files:
                try:
                    usage = shutil.disk_usage(get_user_data_dir())
                    if usage.free < _MIN_FREE_DISK_BYTES:
                        free_gb = usage.free / (1024**3)
                        err = f"저장 공간이 부족합니다 (남은 공간 {free_gb:.2f} GB). 최소 500 MB 이상 확보해 주세요."
                        _stage("error", f"❌ {err}")
                        return {"ok": False, "fatal_code": "DISK_FULL", "error": err, "char_id": cid, "lang": lang_up}
                except Exception:
                    pass

        job = BatchVoiceJob(
            char_id=cid,
            name_kr=name_kr,
            name_jp=name_jp,
            lang=lang_up,
            reply_lang=rlang,
            molru_files=molru_files,
            already_extracted=already_extracted,
            existing_model_id=_clean_model_id(str(_as_dict(user_cfg.get(bucket_name)).get(cid) or "")),
            char_data=char_data,
        )

        if not job.already_extracted and molru_files:
            prof_tag = {
                "plus200": "Plus 200초 최고품질",
                "pro85": "고품질 85초",
                "standard45": "표준 45초",
            }.get(prof_key, prof_key)
            _stage("extract", f"🎧 [{name_kr} ({lang_up})] 스팀 원음 추출 중 ({prof_tag})...")
            try:
                extract_job_reference(job, force=False, game_dir=eff_game_dir, quality_profile=prof_key)
            except Exception as exc:
                if not _has_any_local_wavs(cid, lang_up):
                    err = _redact_secrets(f"스팀 원음 추출 실패: {exc}", extra_secrets=(eff_key,))
                    _stage("error", f"❌ {err}")
                    return {"ok": False, "fatal_code": "", "error": err, "char_id": cid, "lang": lang_up}

        # 3. Upload to Fish Audio with progressive fallback & retries
        _stage("upload", f"🚀 [{name_kr} ({lang_up})] Fish Audio 보이스 모델 생성 중 ({eff_vis})...")
        up_res = upload_job_to_fish_audio(
            job=job,
            api_key=eff_key,
            visibility=eff_vis,
            include_cover=include_cover,
            quality_profile=prof_key,
            game_dir=eff_game_dir,
            asset_mgr=asset_mgr,
            http_session=sess,
            stop_event=stop_event,
            log_cb=log_cb,
        )
        if not up_res.get("ok"):
            fcode = str(up_res.get("fatal_code") or "")
            err = str(up_res.get("error") or "Fish Audio 보이스 생성 실패")
            _stage("error", f"⚠️ [{name_kr} ({lang_up})] {err}")
            comb_wav, samples_dir = _char_ref_dir(cid, lang_up)
            return {
                "ok": False,
                "fatal_code": fcode,
                "error": err,
                "char_id": cid,
                "name_kr": name_kr,
                "lang": lang_up,
                "requested_visibility": eff_vis,
                "combined_wav": str(comb_wav),
                "samples_dir": str(samples_dir),
            }

        model_id = str(up_res["model_id"])
        actual_vis = str(up_res.get("visibility") or eff_vis)
        _stage("link", f"🔗 [{name_kr} ({lang_up})] 모모톡 목소리 링크 자동 연결 중...")
        _persist_link(cid, name_kr, model_id, actual_vis)
        alt_results = _build_and_link_alt_voices(matched_remote)

        packed_from = _safe_int(up_res.get("packed_from_clips"), 0)
        clip_cnt = _safe_int(up_res.get("clip_count"), 0)
        packed_dur = _safe_float(up_res.get("packed_duration_sec"), 0.0)
        clip_desc = (
            f"{packed_from}클립→{clip_cnt}파일 풀패킹 · {packed_dur:.1f}초"
            if packed_from > clip_cnt
            else f"{clip_cnt}클립"
        )
        tr_note = " · 공식대본 100%" if up_res.get("transcript_attached") else ""
        _stage(
            "done",
            f"💗 [{name_kr} ({lang_up})] 목소리 자동 생성 & 연결 완료! ({clip_desc}{tr_note} · ID: {model_id})",
        )
        return {
            "ok": True,
            "source": "created",
            "char_id": cid,
            "name_kr": name_kr,
            "lang": lang_up,
            "model_id": model_id,
            "visibility": actual_vis,
            "requested_visibility": eff_vis,
            "quality_profile": prof_key,
            "clip_count": clip_cnt,
            "packed_from_clips": packed_from if packed_from > 0 else clip_cnt,
            "packed_duration_sec": round(packed_dur, 2),
            "transcript_attached": bool(up_res.get("transcript_attached", False)),
            "upload_plan": str(up_res.get("upload_plan") or "optimal"),
            "alt_results": alt_results,
            "fatal_code": "",
            "error": "",
        }
    finally:
        if owns_session and sess is not None:
            try:
                sess.close()
            except Exception:
                pass
