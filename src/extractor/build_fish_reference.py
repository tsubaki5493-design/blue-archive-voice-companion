"""
Curate the cleanest, most expressive conversational Blue Archive character voice clips
(from data/characters/<ID>/<LANG>/<ID>_<LANG>_rvc_dataset.zip OR directly from local Steam .molru containers)
for Fish Audio voice cloning:
1. Excludes battle screams / damage grunts / clipped SFX clips that degrade conversational TTS.
2. Prioritizes Lobby, MemorialLobby, Cafe, Relationship, Login, and Event conversational lines.
3. Exports:
   - models/<ID>/<LANG>/<ID>_<LANG>_FishAudio_Reference.wav (single ~45s 44.1kHz 16-bit WAV for instant web drag & drop)
   - models/<ID>/<LANG>/fish_samples/*.wav (top individual clean clips for multi-sample API upload)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from math import gcd
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.extractor.ba_molru_extractor import (
    DEFAULT_GAME_DIR,
    MAX_MOLRU_FILE_BYTES,
    MAX_OGG_STREAM_BYTES,
    _assert_not_inside_game_dir,
    _ensure_path_within,
    extract_ogg_streams_from_molru,
    find_character_molru_files,
    is_safe_entity_id,
    is_valid_ba_game_dir,
    load_or_build_catalog,
    resolve_ba_game_dir,
    sanitize_extract_filename,
)
from src.utils.app_paths import get_app_root, get_cache_dir, get_user_data_dir, get_user_models_dir

TARGET_SR = 44100
TARGET_RMS_DBFS = -18.0
PEAK_CEILING_DBFS = -1.5
HIGHPASS_HZ = 75.0
MAX_ZIP_MEMBER_BYTES = 32 * 1024 * 1024      # 32 MB per WAV inside dataset ZIP
MAX_ZIP_TOTAL_BYTES = 256 * 1024 * 1024      # 256 MB total uncompressed read per dataset ZIP
MAX_ZIP_ENTRIES = 4096                       # Max entries inspected per dataset ZIP
MAX_GAMEDATA_JSON_BYTES = 64 * 1024 * 1024   # 64 MB cap on optional transcript JSON files
_ENV_GAMEDATA_DIR = os.environ.get("BA_GAMEDATA_DIR", "").strip()
OPTIONAL_GAMEDATA_DIR: Optional[Path] = Path(_ENV_GAMEDATA_DIR) if _ENV_GAMEDATA_DIR else None
_TRANSCRIPT_CACHE: Dict[str, Dict[str, str]] = {}

DEFAULT_QUALITY_PROFILE = "pro85"

REFERENCE_QUALITY_PROFILES: Dict[str, Dict[str, object]] = {
    "standard45": {
        "label": "⚡ 표준 45초 (무료·일반 즉시 복제 · 최대 8클립)",
        "target_dur_sec": 44.0,
        "max_dur_sec": 52.0,
        "min_clips": 6,
        "target_clips": 7,
        "max_clips": 8,
        "min_matched_dur_sec": 24.0,
    },
    "pro85": {
        "label": "💎 고품질 85초 (유료 90초 최적화 · 최대 14클립 · 대본 100% 매칭 · 추천)",
        "target_dur_sec": 76.0,
        "max_dur_sec": 83.5,
        "min_clips": 8,
        "target_clips": 12,
        "max_clips": 14,
        "min_matched_dur_sec": 36.0,
    },
}

_WIN_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WIN_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_HARD_SCREAM_KEYWORDS = ("damage", "retire", "defeat", "shout", "groggy", "panic")


def sanitize_filename_stem(raw_stem: str, fallback: str = "clip") -> str:
    """Make an audio clip stem 100% safe for Windows/macOS/Linux filesystems (blocks traversal & device names)."""
    cleaned = _WIN_UNSAFE_FILENAME_RE.sub("_", str(raw_stem or "")).strip(" .")
    cleaned = re.sub(r"\.{2,}", "_", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in _WIN_RESERVED_STEMS:
        cleaned = f"_{cleaned}"
    return cleaned[:96]


def _is_safe_zip_member_name(name: str) -> bool:
    """Reject Zip-Slip member paths containing `..`, absolute roots, Windows drive letters, or NUL."""
    if not name or "\x00" in name:
        return False
    if name.startswith(("/", "\\")):
        return False
    norm = name.replace("\\", "/")
    parts = norm.split("/")
    if not parts:
        return False
    if ":" in parts[0]:
        return False
    return all(part not in ("..", "") for part in parts)


def _atomic_write_text(target: Path, content: str) -> None:
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite symlink file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".ref_", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        last_err: Exception | None = None
        for attempt in range(6):
            try:
                os.replace(tmp_name, target)
                last_err = None
                break
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.04 * (attempt + 1))
        if last_err is not None:
            raise last_err
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def resolve_quality_profile(profile: str | None = None) -> tuple[str, Dict[str, object]]:
    key = (profile or DEFAULT_QUALITY_PROFILE).strip().lower()
    if key not in REFERENCE_QUALITY_PROFILES:
        key = DEFAULT_QUALITY_PROFILE
    return key, REFERENCE_QUALITY_PROFILES[key]


def select_upload_samples_with_transcript_lock(
    sample_wavs: List[Path],
    transcript_map: Dict[str, str],
    max_clips: int = 14,
    min_paired_clips: int = 4,
) -> tuple[List[Path], List[str], bool]:
    """
    Guarantee 100% 1:1 audio-to-transcript alignment when uploading multi-sample references to Fish Audio.
    If at least `min_paired_clips` samples (or all available samples when fewer than `min_paired_clips`)
    have verbatim official transcripts, returns ONLY the transcript-matched subset so 1 unmatched clip
    never drops transcripts for the whole model.
    """
    cap = max(1, int(max_clips))
    paired_wavs: List[Path] = []
    paired_texts: List[str] = []
    for w in sample_wavs:
        txt = str(transcript_map.get(w.name) or transcript_map.get(w.stem) or "").strip()
        if txt:
            paired_wavs.append(w)
            paired_texts.append(txt)
            if len(paired_wavs) >= cap:
                break

    if paired_wavs and (len(paired_wavs) >= min(min_paired_clips, len(sample_wavs))):
        return paired_wavs, paired_texts, True

    fallback = sample_wavs[:cap]
    return fallback, [], False

EXCLUDE_KEYWORDS = [
    "damage",
    "retire",
    "defeat",
    "shout",
    "attack",
    "tactic",
    "cover",
    "move",
    "reload",
    "heal",
    "buff",
    "exskill",
    "battle_",
    "commonskill",
    "formation_",
    "summon",
    "minigame",
    "cardgame",
    "_radio",
    "groggy",
    "panic",
]

PRIORITY_KEYWORDS_CALM = [
    "memoriallobby",
    "lobby",
    "cafe",
    "relationship",
    "login",
    "title",
    "season",
    "event",
    "gacha",
]

PRIORITY_KEYWORDS_EXPRESSIVE = [
    "lobby",
    "relationship",
    "gacha",
    "event",
    "cafe",
    "login",
    "season",
    "title",
    "memoriallobby",
]

CALM_ARCHETYPES = {"cool_downer", "kuudere_deadpan", "lazy_my_pace"}


def score_clip(
    name: str,
    dur: float,
    peak: float,
    rms: float,
    archetype: str = "gentle_earnest_passion",
    y: Optional[np.ndarray] = None,
    sr: int = 44100,
) -> float:
    low = name.lower()
    if any(bad in low for bad in EXCLUDE_KEYWORDS):
        return -1000.0
    if dur < 2.8 or dur > 11.5:
        return -500.0
    if rms < 0.018 or peak > 1.55:
        return -200.0
    if y is not None and len(y) > 0:
        clipped_ratio = float(np.mean(np.abs(y) >= 0.99))
        if clipped_ratio > 0.008:
            return -250.0

    kw_list = PRIORITY_KEYWORDS_CALM if archetype in CALM_ARCHETYPES else PRIORITY_KEYWORDS_EXPRESSIVE
    score = 0.0
    for idx, kw in enumerate(kw_list):
        if kw in low:
            score += 50.0 - idx * 3.0
            break
    # Prefer rich 4.0s - 8.8s conversational lines
    if 4.0 <= dur <= 8.8:
        score += 20.0
    score += min(rms * 100.0, 15.0)

    # Reward natural syllable/prosodic contour variance for expressive archetypes (avoids ASMR whisper-only reference sets)
    if y is not None and len(y) >= sr:
        hop = max(256, sr // 20)  # 50ms envelope frames
        n_frames = len(y) // hop
        if n_frames >= 6:
            env = np.sqrt(np.mean(y[: n_frames * hop].reshape(n_frames, hop) ** 2, axis=1))
            voiced_env = env[env > 0.015]
            if len(voiced_env) >= 4:
                dyn_ratio = float(np.std(voiced_env) / (np.mean(voiced_env) + 1e-5))
                if archetype in CALM_ARCHETYPES:
                    # Prefer steady, controlled delivery
                    score += max(0.0, 8.0 - abs(dyn_ratio - 0.42) * 14.0)
                else:
                    # Prefer expressive prosodic modulation
                    score += min(dyn_ratio * 16.0, 14.0)
    return score


def resample_audio(y: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    if orig_sr == target_sr:
        return y
    from scipy.signal import resample_poly

    g = gcd(orig_sr, target_sr)
    return resample_poly(y, target_sr // g, orig_sr // g).astype(np.float32)


def normalize_peak(y: np.ndarray, target_dbfs: float = PEAK_CEILING_DBFS) -> np.ndarray:
    peak = float(np.max(np.abs(y)))
    if peak < 1e-6:
        return y.astype(np.float32)
    target_lin = 10.0 ** (target_dbfs / 20.0)
    return (y * (target_lin / peak)).astype(np.float32)


def highpass_filter(y: np.ndarray, sr: int, cutoff_hz: float = HIGHPASS_HZ) -> np.ndarray:
    """2nd-order zero-phase Butterworth high-pass filter to strip sub-bass rumble/pops."""
    if len(y) < 64 or sr <= 200 or cutoff_hz <= 0.0:
        return y.astype(np.float32)
    from scipy.signal import butter, sosfiltfilt

    sos = butter(2, cutoff_hz / (sr * 0.5), btype="highpass", output="sos")
    return sosfiltfilt(sos, y).astype(np.float32)


def normalize_active_rms(
    y: np.ndarray,
    target_rms_dbfs: float = TARGET_RMS_DBFS,
    peak_ceiling_dbfs: float = PEAK_CEILING_DBFS,
) -> np.ndarray:
    """Match active-speech RMS loudness across clips while enforcing a clean peak ceiling."""
    active = y[np.abs(y) > 0.012]
    ref = active if len(active) >= 256 else y
    rms = float(np.sqrt(np.mean(ref**2))) if len(ref) else 0.0
    if rms < 1e-6:
        return y.astype(np.float32)
    target_rms = 10.0 ** (target_rms_dbfs / 20.0)
    peak_ceil = 10.0 ** (peak_ceiling_dbfs / 20.0)
    gain = target_rms / rms
    peak = float(np.max(np.abs(y)))
    if peak * gain > peak_ceil:
        gain = peak_ceil / max(peak, 1e-6)
    return np.clip(y * gain, -peak_ceil, peak_ceil).astype(np.float32)


def apply_cosine_fade(y: np.ndarray, sr: int, fade_ms: float = 15.0) -> np.ndarray:
    """Apply smooth raised-cosine edge fade-in/out to prevent clicks between stitched samples."""
    n_fade = min(int(sr * (fade_ms / 1000.0)), len(y) // 4)
    if n_fade <= 1:
        return y.astype(np.float32)
    out = y.astype(np.float32, copy=True)
    ramp = (0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, n_fade, dtype=np.float32)))).astype(np.float32)
    out[:n_fade] *= ramp
    out[-n_fade:] *= ramp[::-1]
    return out


def master_studio_clip(y: np.ndarray, orig_sr: int, target_sr: int = TARGET_SR) -> np.ndarray:
    """Full studio mastering chain for Fish Audio reference WAVs (DC -> 75Hz HPF -> 44.1kHz -> RMS -18dBFS -> 15ms fade)."""
    y32 = y.astype(np.float32, copy=False)
    if len(y32) == 0:
        return y32
    y32 = y32 - float(np.mean(y32))
    y32 = highpass_filter(y32, orig_sr, cutoff_hz=HIGHPASS_HZ)
    y_resamp = resample_audio(y32, orig_sr, target_sr)
    y_norm = normalize_active_rms(y_resamp, TARGET_RMS_DBFS, PEAK_CEILING_DBFS)
    return apply_cosine_fade(y_norm, target_sr, fade_ms=15.0)


def _clean_official_transcript(raw_text: str, lang: str) -> str:
    """Clean official game transcript for verbatim Fish Audio cloning/Zero-Shot text alignment."""
    if not raw_text:
        return ""
    if "[USERNAME]" in raw_text or "[NICKNAME]" in raw_text:
        return ""
    s = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if (s.startswith("(") and s.endswith(")")) or (s.startswith("<") and s.endswith(">")):
        inner = s[1:-1].strip()
        if "(" not in inner and ")" not in inner and "<" not in inner and ">" not in inner:
            s = inner
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 4:
        return ""
    if lang.upper() == "KR" and not re.search(r"[가-힣]", s):
        return ""
    if lang.upper() == "JP" and not re.search(r"[ぁ-んァ-ヶ一-龥]", s):
        return ""
    return s


def load_optional_official_transcripts(lang: str = "JP", gamedata_dir: Optional[Path] = None) -> Dict[str, str]:
    """
    Optionally load lowercase audio stem -> verbatim official script from local SchaleDB/Blue Archive gamedata.
    Returns an empty dict cleanly if gamedata is not present on this machine.
    """
    lang_up = lang.upper()
    if gamedata_dir is None:
        eff_dir: Optional[Path] = None
        env_gd = os.environ.get("BA_GAMEDATA_DIR", "").strip()
        candidates: list[Path] = [
            get_user_data_dir() / "gamedata",
            get_app_root() / "config" / "gamedata",
        ]
        if env_gd:
            candidates.append(Path(env_gd))
        if OPTIONAL_GAMEDATA_DIR is not None:
            candidates.append(OPTIONAL_GAMEDATA_DIR)
        for cand in candidates:
            if cand.is_dir():
                eff_dir = cand
                break
    else:
        eff_dir = gamedata_dir
    cache_key = f"{lang_up}::{eff_dir}"
    if cache_key in _TRANSCRIPT_CACHE:
        return _TRANSCRIPT_CACHE[cache_key]

    stem_to_text: Dict[str, str] = {}
    if eff_dir is None or not eff_dir.exists():
        _TRANSCRIPT_CACHE[cache_key] = stem_to_text
        return stem_to_text
    gamedata_dir = eff_dir

    try:
        sv_file = gamedata_dir / ("schale_kr_voice.json" if lang_up == "KR" else "schale_jp_voice.json")
        if sv_file.exists() and not sv_file.is_symlink() and sv_file.stat().st_size <= MAX_GAMEDATA_JSON_BYTES:
            sv = json.loads(sv_file.read_text(encoding="utf-8"))
            for v in (sv.values() if isinstance(sv, dict) else []):
                if not isinstance(v, dict):
                    continue
                for items in v.values():
                    if not isinstance(items, list):
                        continue
                    for it in items:
                        clip = str(it.get("AudioClip") or "").strip()
                        tr = _clean_official_transcript(str(it.get("Transcription") or ""), lang_up)
                        if clip and tr:
                            stem_to_text[Path(clip).stem.lower()] = tr

        cd_file = gamedata_dir / "CharacterDialogExcel.json"
        sk_file = gamedata_dir / ("schale_kr_students.json" if (gamedata_dir / "schale_kr_students.json").exists() else "schale_jp_students.json")
        if (
            cd_file.exists()
            and sk_file.exists()
            and not cd_file.is_symlink()
            and not sk_file.is_symlink()
            and cd_file.stat().st_size <= MAX_GAMEDATA_JSON_BYTES
            and sk_file.stat().st_size <= MAX_GAMEDATA_JSON_BYTES
        ):
            raw_sk = json.loads(sk_file.read_text(encoding="utf-8"))
            sk_map = {int(s["Id"]): s for s in (raw_sk.values() if isinstance(raw_sk, dict) else raw_sk)}
            raw_cd = json.loads(cd_file.read_text(encoding="utf-8"))
            cd_rows = next((v for v in raw_cd.values() if isinstance(v, list)), [])
            loc_key = "localize_kr" if lang_up == "KR" else "localize_jp"
            by_cid_grp: Dict[Tuple[int, int], List[str]] = defaultdict(list)
            for r in cd_rows:
                if int(r.get("dialog_category") or -1) == 8:
                    cid = int(r.get("character_id") or 0)
                    txt = _clean_official_transcript(str(r.get(loc_key) or ""), lang_up)
                    by_cid_grp[(cid, int(r.get("group_id") or 0))].append(txt)
            for (cid, grp), texts in by_cid_grp.items():
                s = sk_map.get(cid) or {}
                dev = str(s.get("DevName") or "").strip()
                if dev.lower().endswith("_default"):
                    dev = dev[:-8]
                path_nm = str(s.get("PathName") or "").strip()
                prefixes = [p.lower() for p in (dev, path_nm) if p]
                for idx, txt in enumerate(texts, 1):
                    if not txt:
                        continue
                    suffix = f"memoriallobby_{grp}" if len(texts) == 1 else f"memoriallobby_{grp}_{idx}"
                    for pref in prefixes:
                        stem_to_text.setdefault(f"{pref}_{suffix}", txt)
    except Exception:
        pass

    _TRANSCRIPT_CACHE[cache_key] = stem_to_text
    return stem_to_text


def compute_clip_filter_hash(char_data: dict) -> str:
    """Return a short deterministic hash of `voice_clip_filter` so filter changes invalidate stale extracts."""
    flt = char_data.get("voice_clip_filter")
    if not isinstance(flt, dict) or not flt:
        return ""
    canonical = json.dumps(flt, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]


def build_alt_voice_char_data(base_char_data: dict, alt_key: str) -> Optional[dict]:
    """
    Build a virtual character config dict for an entry under `base_char_data['alt_voice_personas'][alt_key]`
    (e.g. Kisaki -> `kiki` -> `Kisaki_Kiki`) so extraction, batch upload, account sync, and runtime TTS
    emotion directing all share the exact same clean character data contract.
    """
    alts = base_char_data.get("alt_voice_personas")
    if not isinstance(alts, dict):
        return None
    spec = alts.get(alt_key)
    if not isinstance(spec, dict):
        return None
    base_id = str(base_char_data.get("id") or "").strip()
    slot_id = str(spec.get("voice_slot_id") or f"{base_id}_{alt_key.capitalize()}").strip()
    if not is_safe_entity_id(base_id) or not is_safe_entity_id(slot_id):
        return None
    base_persona = dict(base_char_data.get("persona") or {})
    if isinstance(spec.get("voice_acting_profile"), dict):
        merged_vap = dict(base_persona.get("voice_acting_profile") or {})
        merged_vap.update(spec["voice_acting_profile"])
        base_persona["voice_acting_profile"] = merged_vap
    if spec.get("sample_tts_line_jp"):
        base_persona["sample_tts_line_jp"] = str(spec["sample_tts_line_jp"])
    if spec.get("sample_tts_line_kr"):
        base_persona["sample_tts_line_kr"] = str(spec["sample_tts_line_kr"])

    return {
        "id": slot_id,
        "parent_char_id": base_id,
        "alt_voice_key": alt_key,
        "category": str(base_char_data.get("category") or "playable"),
        "character_id": base_char_data.get("character_id"),
        "name_kr": str(spec.get("name_kr") or f"{base_char_data.get('name_kr', base_id)} ({alt_key})"),
        "name_jp": str(spec.get("name_jp") or f"{base_char_data.get('name_jp', base_id)} ({alt_key})"),
        "school": str(base_char_data.get("school") or ""),
        "club": str(base_char_data.get("club") or ""),
        "molru_prefix": str(spec.get("molru_prefix") or base_char_data.get("molru_prefix") or base_id),
        "local_voice_packs": spec.get("local_voice_packs") or base_char_data.get("local_voice_packs") or {},
        "voice_clip_filter": spec.get("voice_clip_filter") or {},
        "voice_variants": base_char_data.get("voice_variants") or {},
        "persona": base_persona,
    }


def _passes_clip_filter(raw_stem_low: str, clip_filter: dict) -> tuple[bool, bool]:
    """
    Check if `raw_stem_low` satisfies `voice_clip_filter`.
    Returns `(allowed, is_explicitly_included)`.
    """
    if not clip_filter:
        return True, False
    inc_stems = {str(x).strip().lower() for x in (clip_filter.get("include_stems") or []) if str(x).strip()}
    exc_stems = {str(x).strip().lower() for x in (clip_filter.get("exclude_stems") or []) if str(x).strip()}
    inc_prefs = tuple(str(x).strip().lower() for x in (clip_filter.get("include_prefixes") or []) if str(x).strip())
    exc_prefs = tuple(str(x).strip().lower() for x in (clip_filter.get("exclude_prefixes") or []) if str(x).strip())

    if raw_stem_low in exc_stems:
        return False, False
    if exc_prefs and any(raw_stem_low.startswith(p) for p in exc_prefs):
        return False, False
    if inc_stems:
        return (raw_stem_low in inc_stems), (raw_stem_low in inc_stems)
    if inc_prefs:
        ok = any(raw_stem_low.startswith(p) for p in inc_prefs)
        return ok, ok
    return True, False


_MOLRU_AVAIL_CACHE: Dict[Tuple[str, str, str], Tuple[float, bool]] = {}


def clear_steam_voice_availability_cache() -> None:
    """Clear cached Steam .molru availability checks (e.g. after user picks a new Steam directory)."""
    _MOLRU_AVAIL_CACHE.clear()


def find_character_voice_molru_files(
    char_data: dict,
    lang: str = "JP",
    game_dir: Optional[Path] = None,
) -> List[Path]:
    """
    Resolve all Steam .molru voice packs for a character JSON entry, searching explicit packs,
    `molru_prefix`, canonical `id` (recovers base packs like JP_Ako/JP_Azusa/JP_erika when
    molru_prefix is an internal CH code), and romanization aliases (e.g. Reijo -> reizyo).
    When `voice_clip_filter` specifies `include_stems` or `include_prefixes` alongside explicit packs,
    only the explicit packs are searched so alt personas never pull unintended packs.
    """
    eff_game_dir = resolve_ba_game_dir(game_dir)
    char_id = str(char_data.get("id") or "").strip()
    molru_prefix = str(char_data.get("molru_prefix") or char_id).strip()
    lang_up = lang.upper()
    explicit_packs = (char_data.get("local_voice_packs") or {}).get(lang_up.lower()) or []
    clip_flt = char_data.get("voice_clip_filter") if isinstance(char_data.get("voice_clip_filter"), dict) else {}
    strict_explicit = bool(explicit_packs and (clip_flt.get("include_stems") or clip_flt.get("include_prefixes")))
    extra_prefixes = (
        []
        if strict_explicit
        else ([char_id] if (char_id and char_id.lower() != molru_prefix.lower()) else [])
    )
    return find_character_molru_files(
        eff_game_dir,
        molru_prefix,
        lang_up,
        explicit_packs=explicit_packs,
        extra_prefixes=extra_prefixes,
    )


def character_has_steam_voice_pack(
    char_data: dict,
    lang: str = "JP",
    game_dir: Optional[Path] = None,
) -> Optional[bool]:
    """
    Fast cached check of whether `char_data` has at least one `.molru` voice pack in Steam for `lang`.
    Returns:
      - `True` / `False` when a valid Steam Blue Archive audio directory is present
      - `None` when Steam Blue Archive is not installed / not detected at `game_dir`
    """
    eff_game_dir = resolve_ba_game_dir(game_dir)
    lang_up = (lang or "JP").strip().upper()
    voc_dir = (
        eff_game_dir
        / "BlueArchive_Data"
        / "StreamingAssets"
        / "PUB"
        / "Resource"
        / "GameData"
        / "MediaResources"
        / "Audio"
        / f"VOC_{lang_up}"
    )
    if not voc_dir.is_dir():
        return None
    cid = str(char_data.get("id") or "").strip()
    cache_key = (str(eff_game_dir).lower(), cid.lower(), lang_up)
    now = time.monotonic()
    hit = _MOLRU_AVAIL_CACHE.get(cache_key)
    if hit is not None and (now - hit[0]) < 60.0:
        return hit[1]
    has_pack = bool(find_character_voice_molru_files(char_data, lang=lang_up, game_dir=eff_game_dir))
    _MOLRU_AVAIL_CACHE[cache_key] = (now, has_pack)
    return has_pack


def build_character_fish_reference(
    char_data: dict,
    lang: str = "JP",
    game_dir: Optional[Path] = None,
    quality_profile: str = DEFAULT_QUALITY_PROFILE,
) -> dict:
    """
    Build clean 44.1kHz Fish Audio reference WAVs for ANY Blue Archive character.
    Uses existing RVC dataset zip if present; otherwise reads OGG streams directly from Steam .molru packs!
    Supports `quality_profile="pro85"` (78~88s, up to 14 clips, 100% official transcript lock)
    and `quality_profile="standard45"` (44~52s, up to 8 clips).
    """
    game_dir = resolve_ba_game_dir(game_dir)
    prof_key, prof_cfg = resolve_quality_profile(quality_profile)
    target_dur_sec = float(prof_cfg["target_dur_sec"])
    max_dur_sec = float(prof_cfg["max_dur_sec"])
    min_clips = int(prof_cfg["min_clips"])
    target_clips = int(prof_cfg["target_clips"])
    max_clips = int(prof_cfg["max_clips"])
    min_matched_dur_sec = float(prof_cfg["min_matched_dur_sec"])

    char_id = str(char_data.get("id") or "Aru").strip()
    if not is_safe_entity_id(char_id):
        raise ValueError(f"Invalid or unsafe character_id: {char_id!r}")
    archetype = (((char_data.get("persona") or {}).get("voice_acting_profile")) or {}).get(
        "archetype", "gentle_earnest_passion"
    )
    lang_up = str(lang or "JP").strip().upper()
    if lang_up not in ("JP", "KR"):
        raise ValueError(f"Invalid voice language code: {lang!r}")
    user_data_root = get_user_data_dir()
    user_dataset_zip = _ensure_path_within(
        user_data_root, user_data_root / "data" / "characters" / char_id / lang_up / f"{char_id}_{lang_up}_rvc_dataset.zip"
    )
    legacy_data_root = ROOT_DIR / "data"
    legacy_dataset_zip = _ensure_path_within(
        legacy_data_root, legacy_data_root / "characters" / char_id / lang_up / f"{char_id}_{lang_up}_rvc_dataset.zip"
    )
    dataset_zip = user_dataset_zip if user_dataset_zip.exists() else legacy_dataset_zip

    models_root = get_user_models_dir()
    out_dir = _ensure_path_within(models_root, models_root / char_id / lang_up)
    _assert_not_inside_game_dir(out_dir, game_dir)
    samples_dir = _ensure_path_within(out_dir, out_dir / "fish_samples")
    combined_wav = _ensure_path_within(out_dir, out_dir / f"{char_id}_{lang_up}_FishAudio_Reference.wav")

    candidates: List[Tuple[float, str, str, np.ndarray, int, float]] = []
    relaxed_candidates: List[Tuple[float, str, str, np.ndarray, int, float]] = []

    clip_filter = char_data.get("voice_clip_filter") if isinstance(char_data.get("voice_clip_filter"), dict) else {}
    clip_filter_hash = compute_clip_filter_hash(char_data)
    relax_dur_min = float(clip_filter.get("relax_duration_min", 2.0) or 2.0)

    transcript_lookup = load_optional_official_transcripts(lang=lang_up)

    if dataset_zip.exists() and not dataset_zip.is_symlink():
        total_zip_bytes = 0
        with zipfile.ZipFile(dataset_zip, "r") as zf:
            infos = sorted(zf.infolist(), key=lambda zi: zi.filename)[:MAX_ZIP_ENTRIES]
            for info in infos:
                name = info.filename
                if info.is_dir() or not name.lower().endswith(".wav"):
                    continue
                if not _is_safe_zip_member_name(name):
                    continue
                if info.file_size > MAX_ZIP_MEMBER_BYTES:
                    continue
                raw_stem = Path(name.replace("\\", "/")).stem
                raw_stem_low = raw_stem.lower()
                allowed_by_flt, explicit_inc = _passes_clip_filter(raw_stem_low, clip_filter)
                if not allowed_by_flt:
                    continue
                with zf.open(info, "r") as member_fh:
                    raw = member_fh.read(MAX_ZIP_MEMBER_BYTES + 1)
                if len(raw) > MAX_ZIP_MEMBER_BYTES:
                    continue
                total_zip_bytes += len(raw)
                if total_zip_bytes > MAX_ZIP_TOTAL_BYTES:
                    break
                try:
                    y, sr = sf.read(io.BytesIO(raw), dtype="float32")
                except Exception:
                    continue
                if y.ndim > 1:
                    y = np.mean(y, axis=1)
                dur = len(y) / float(sr)
                peak = float(np.max(np.abs(y))) if len(y) else 0.0
                rms = float(np.sqrt(np.mean(y**2))) if len(y) else 0.0
                safe_fname = f"{sanitize_filename_stem(raw_stem)}.wav"
                sc = score_clip(raw_stem, dur, peak, rms, archetype=archetype, y=y, sr=sr)
                if explicit_inc and sc <= -150.0 and relax_dur_min <= dur <= 12.5 and rms >= 0.012:
                    sc = 25.0 + min(dur * 3.0, 24.0) + min(rms * 80.0, 12.0)
                if sc > -150.0:
                    if raw_stem_low in transcript_lookup:
                        sc += 6.0
                    candidates.append((sc, safe_fname, raw_stem_low, y, sr, dur))
                elif (
                    1.2 <= dur <= 14.0
                    and rms >= 0.008
                    and not any(bad in raw_stem_low for bad in _HARD_SCREAM_KEYWORDS)
                ):
                    rel_sc = min(dur, 9.0) + min(rms * 50.0, 10.0) + (6.0 if raw_stem_low in transcript_lookup else 0.0)
                    relaxed_candidates.append((rel_sc, safe_fname, raw_stem_low, y, sr, dur))
    else:
        molru_files = find_character_voice_molru_files(char_data, lang=lang_up, game_dir=game_dir)
        if not molru_files:
            raise FileNotFoundError(
                f"스팀 클라이언트에서 '{char_data.get('name_kr', char_id)}' ({lang_up}) 음성 팩(.molru)을 찾을 수 없습니다. (미실장 또는 보이스 없는 NPC일 수 있습니다.)"
            )

        user_catalog = get_cache_dir() / "catalog_cache.json"
        catalog = load_or_build_catalog(game_dir, user_catalog)
        game_dir_res = game_dir.resolve()
        for molru_path in molru_files:
            if molru_path.is_symlink() or not molru_path.resolve().is_relative_to(game_dir_res):
                continue
            if molru_path.stat().st_size > MAX_MOLRU_FILE_BYTES:
                continue
            rel_from_game = molru_path.relative_to(game_dir).as_posix()
            mxmc_key = rel_from_game[:-6] if rel_from_game.endswith(".molru") else rel_from_game
            name_queue = list(catalog.get(mxmc_key, []))
            streams = extract_ogg_streams_from_molru(
                molru_path.read_bytes(), max_stream_bytes=MAX_OGG_STREAM_BYTES
            )

            for idx, (_, _, ogg_bytes) in enumerate(streams):
                fallback_stem = f"{molru_path.stem}_{idx:04d}"
                raw_orig = (
                    str((name_queue[idx] or {}).get("external_file") or "")
                    if idx < len(name_queue)
                    else f"{fallback_stem}.ogg"
                )
                orig_name = sanitize_extract_filename(raw_orig, default_ext=".ogg", fallback_stem=fallback_stem)
                raw_stem = Path(orig_name).stem
                raw_stem_low = raw_stem.lower()
                allowed_by_flt, explicit_inc = _passes_clip_filter(raw_stem_low, clip_filter)
                if not allowed_by_flt:
                    continue
                safe_fname = f"{sanitize_filename_stem(raw_stem, fallback=f'{molru_path.stem}_{idx:04d}')}.wav"
                try:
                    y, sr = sf.read(io.BytesIO(ogg_bytes), dtype="float32")
                except Exception:
                    continue
                if y.ndim > 1:
                    y = np.mean(y, axis=1)
                non_silent = np.where(np.abs(y) > 0.0056)[0]
                if len(non_silent) == 0:
                    continue
                pad = int(sr * 0.08)
                y = y[max(0, int(non_silent[0]) - pad) : min(len(y), int(non_silent[-1]) + pad)]
                dur = len(y) / float(sr)
                peak = float(np.max(np.abs(y))) if len(y) else 0.0
                rms = float(np.sqrt(np.mean(y**2))) if len(y) else 0.0
                sc = score_clip(raw_stem, dur, peak, rms, archetype=archetype, y=y, sr=sr)
                if explicit_inc and sc <= -150.0 and relax_dur_min <= dur <= 12.5 and rms >= 0.012:
                    sc = 25.0 + min(dur * 3.0, 24.0) + min(rms * 80.0, 12.0)
                if sc > -150.0:  # Allow fallback conversational lines if a pack lacks 'MemorialLobby' naming
                    if raw_stem_low in transcript_lookup:
                        sc += 6.0
                    candidates.append((sc, safe_fname, raw_stem_low, y, sr, dur))
                elif (
                    1.2 <= dur <= 14.0
                    and rms >= 0.008
                    and not any(bad in raw_stem_low for bad in _HARD_SCREAM_KEYWORDS)
                ):
                    rel_sc = min(dur, 9.0) + min(rms * 50.0, 10.0) + (6.0 if raw_stem_low in transcript_lookup else 0.0)
                    relaxed_candidates.append((rel_sc, safe_fname, raw_stem_low, y, sr, dur))

    if not candidates and relaxed_candidates:
        candidates = relaxed_candidates

    candidates.sort(key=lambda x: x[0], reverse=True)
    if not candidates:
        raise RuntimeError(f"'{char_data.get('name_kr', char_id)}' 음성 팩에서 적합한 대화형 보이스 클립을 찾지 못했습니다.")

    # Separate verbatim-transcript-matched candidates & deduplicate identical spoken lines for max phonetic diversity
    matched_candidates: List[Tuple[float, str, str, np.ndarray, int, float]] = []
    unmatched_candidates: List[Tuple[float, str, str, np.ndarray, int, float]] = []
    seen_texts: set[str] = set()
    for cand in candidates:
        raw_stem_low = cand[2]
        tr_txt = transcript_lookup.get(raw_stem_low, "")
        if tr_txt:
            norm_tr = re.sub(r"[\s.,!?~…「」『』()（）]+", "", tr_txt).lower()
            if norm_tr and norm_tr in seen_texts:
                continue
            if norm_tr:
                seen_texts.add(norm_tr)
            matched_candidates.append(cand)
        else:
            unmatched_candidates.append(cand)

    # 100% official transcript lock: when transcript-matched clips provide rich enough duration,
    # curate strictly from them so ZERO unmatched clips can break Fish Audio `texts[]` alignment.
    matched_dur_avail = sum(c[5] for c in matched_candidates[:max_clips])
    use_matched_only = len(matched_candidates) >= 4 and matched_dur_avail >= min_matched_dur_sec
    pool = matched_candidates if use_matched_only else (matched_candidates + unmatched_candidates)

    out_dir.mkdir(parents=True, exist_ok=True)
    samples_dir.mkdir(parents=True, exist_ok=True)
    studio_meta_json = samples_dir / "studio_v2_meta.json"
    # Remove previous commit marker first so a mid-extraction crash never leaves stale metadata behind
    if studio_meta_json.exists():
        try:
            studio_meta_json.unlink()
        except OSError:
            pass
    for old in samples_dir.glob("*.wav"):
        try:
            old.unlink()
        except OSError:
            pass

    selected = []
    total_dur = 0.0
    for sc, fname, raw_stem_low, y, sr, dur in pool:
        if len(selected) >= max_clips:
            break
        if total_dur + dur > max_dur_sec and len(selected) >= min_clips:
            continue
        if total_dur + dur > (max_dur_sec + 1.5) and len(selected) >= 4:
            break
        y_44k = master_studio_clip(y, sr, TARGET_SR)
        selected.append((fname, raw_stem_low, y_44k, dur, sc))
        total_dur += dur
        if total_dur >= target_dur_sec and len(selected) >= target_clips:
            break

    # Safety: if pool had items but every item exceeded max_dur_sec+1.5 after 4 clips, selected still has >=1
    if not selected and pool:
        sc, fname, raw_stem_low, y, sr, dur = pool[0]
        selected.append((fname, raw_stem_low, master_studio_clip(y, sr, TARGET_SR), dur, sc))

    pause = np.zeros(int(TARGET_SR * 0.35), dtype=np.float32)
    stitched_parts = []
    saved_samples: List[Path] = []
    transcripts_map: Dict[str, str] = {}
    transcript_matched = 0
    for idx, (fname, raw_stem_low, y_44k, dur, sc) in enumerate(selected, 1):
        out_name = sanitize_extract_filename(f"{idx:02d}_{fname}", default_ext=".wav", fallback_stem=f"{idx:02d}_clip")
        out_sample = _ensure_path_within(samples_dir, samples_dir / out_name)
        sf.write(out_sample, y_44k, TARGET_SR, subtype="PCM_16")
        saved_samples.append(out_sample)
        tr_text = transcript_lookup.get(raw_stem_low, "")
        if tr_text:
            transcripts_map[out_name] = tr_text
            transcripts_map[out_sample.stem] = tr_text
            transcript_matched += 1
        stitched_parts.append(y_44k)
        if idx < len(selected):
            stitched_parts.append(pause)

    transcripts_json = samples_dir / "transcripts.json"
    if transcripts_map:
        _atomic_write_text(transcripts_json, json.dumps(transcripts_map, ensure_ascii=False, indent=2))
    elif transcripts_json.exists():
        try:
            transcripts_json.unlink()
        except OSError:
            pass

    combined = np.concatenate(stitched_parts)
    total_dur_rounded = round(len(combined) / float(TARGET_SR), 1)
    tr_cov = round(transcript_matched / max(1, len(saved_samples)), 3)

    # Write combined_wav BEFORE studio_v2_meta.json so studio_v2_meta.json acts as an atomic commit marker
    sf.write(combined_wav, combined, TARGET_SR, subtype="PCM_16")

    _atomic_write_text(
        studio_meta_json,
        json.dumps(
            {
                "pipeline_version": 3,
                "quality_profile": prof_key,
                "clip_filter_hash": clip_filter_hash,
                "max_upload_clips": max_clips,
                "target_sr": TARGET_SR,
                "rms_dbfs": TARGET_RMS_DBFS,
                "peak_ceiling_dbfs": PEAK_CEILING_DBFS,
                "hpf_hz": HIGHPASS_HZ,
                "clip_count": len(saved_samples),
                "total_duration_sec": total_dur_rounded,
                "transcript_matched": transcript_matched,
                "transcript_coverage": tr_cov,
                "transcript_locked": bool(saved_samples and transcript_matched == len(saved_samples)),
            },
            ensure_ascii=False,
            indent=2,
        ),
    )

    return {
        "character_id": char_id,
        "lang": lang_up,
        "quality_profile": prof_key,
        "clip_count": len(saved_samples),
        "total_duration_sec": total_dur_rounded,
        "combined_wav": combined_wav,
        "samples_dir": samples_dir,
        "sample_files": saved_samples,
        "transcripts_json": transcripts_json if transcripts_map else None,
        "transcript_matched": transcript_matched,
        "transcript_coverage": tr_cov,
        "transcript_locked": bool(saved_samples and transcript_matched == len(saved_samples)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build clean Fish Audio reference WAVs for any Blue Archive character")
    parser.add_argument("--character", type=str, default="Aru", help="Character ID (e.g. Aru, Yuuka, Mika, Hoshino)")
    parser.add_argument("--lang", type=str, default="JP", choices=["JP", "KR"], help="Language pack (JP or KR)")
    parser.add_argument(
        "--profile",
        type=str,
        default=DEFAULT_QUALITY_PROFILE,
        choices=list(REFERENCE_QUALITY_PROFILES.keys()),
        help="Quality profile: pro85 (78-88s, up to 14 clips, recommended) or standard45 (44-52s, up to 8 clips)",
    )
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR, help="Path to Steam BlueArchive directory")
    args = parser.parse_args()

    if not is_safe_entity_id(args.character):
        raise ValueError(f"Invalid character identifier: {args.character!r}")
    chars_dir = ROOT_DIR / "config" / "characters"
    cfg_file = _ensure_path_within(chars_dir, chars_dir / f"{args.character.lower()}.json")
    if cfg_file.exists():
        char_data = json.loads(cfg_file.read_text(encoding="utf-8"))
    else:
        char_data = {"id": args.character, "molru_prefix": args.character}

    res = build_character_fish_reference(char_data, lang=args.lang, game_dir=args.game_dir, quality_profile=args.profile)
    print(
        f"[OK] Saved {res['clip_count']} studio clips ({res['total_duration_sec']}s · profile={res['quality_profile']} · "
        f"transcripts={res['transcript_matched']}/{res['clip_count']}) -> {res['combined_wav']}"
    )


if __name__ == "__main__":
    main()
