"""
Curate the cleanest, most expressive conversational Blue Archive character voice clips
(directly from native 44.1kHz Steam .molru containers, with fallback to RVC dataset ZIPs)
for Fish Audio S2 / S2.1 Instant Voice Clone:
1. Excludes battle screams / damage grunts / BossOperator / MultiFloorRaid / radio / SFX / clipped clips.
2. Prioritizes natural in-character conversational lines (Lobby, Relationship, Event, Cafe, Gacha, Login, Season)
   while fixing `"lobby"` shadowing `"memoriallobby"` and capping whispery MemorialLobby clips on expressive archetypes.
3. Applies studio 44.1kHz mono PCM_16 mastering:
   - -45 dBFS 10ms-frame silence trim + mandatory pre-filter 60ms lead / 80ms tail zero-pad (0% onset attenuation)
   - DC removal -> 65 Hz 2nd-order zero-phase Butterworth HPF -> 2-pass relative-gated -18.0 dBFS active RMS
   - Transparent -3.0 dBFS soft-knee limiter with -1.2 dBFS peak ceiling (eliminates cross-clip loudness drops)
   - 10 ms raised-cosine outer-pad edge fade + 0.45 s inter-clip pauses counted strictly inside the <=85.0s budget
4. Loads 100%-verified verbatim JP & KR official transcripts (5-tier gamedata + gamedata_jp pipeline,
   VoiceSpineExcel-verified MemorialLobby alignment with 0 misaligned clips, and full 7-NPC Spine recovery).
5. Exports atomically:
   - models/<ID>/<LANG>/<ID>_<LANG>_FishAudio_Reference.wav (single <=85s 44.1kHz 16-bit WAV for web drag & drop)
   - models/<ID>/<LANG>/fish_samples/*.wav (top individual clean clips + transcripts.json + studio_v2_meta.json)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict
from math import gcd
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    lookup_catalog_entries,
    resolve_ba_game_dir,
    sanitize_extract_filename,
)
from src.utils.app_paths import get_app_root, get_cache_dir, get_user_data_dir, get_user_models_dir

STUDIO_PIPELINE_VERSION = 4

TARGET_SR = 44100
TARGET_RMS_DBFS = -18.0
PEAK_CEILING_DBFS = -1.2
SOFT_LIMITER_KNEE_DBFS = -3.0
SOFT_LIMITER_MAX_GR_DB = 4.5
HIGHPASS_HZ = 65.0
TRIM_THRESHOLD_DBFS = -45.0
LEAD_PAD_MS = 60.0
TAIL_PAD_MS = 80.0
EDGE_FADE_MS = 10.0
INTER_CLIP_PAUSE_SEC = 0.45

MAX_ZIP_MEMBER_BYTES = 32 * 1024 * 1024      # 32 MB per WAV inside dataset ZIP
MAX_ZIP_TOTAL_BYTES = 256 * 1024 * 1024      # 256 MB total uncompressed read per dataset ZIP
MAX_ZIP_ENTRIES = 4096                       # Max entries inspected per dataset ZIP
MAX_GAMEDATA_JSON_BYTES = 64 * 1024 * 1024   # 64 MB cap on optional transcript JSON files

_ENV_GAMEDATA_DIR = os.environ.get("BA_GAMEDATA_DIR", "").strip()
OPTIONAL_GAMEDATA_DIR: Optional[Path] = Path(_ENV_GAMEDATA_DIR) if _ENV_GAMEDATA_DIR else None
_SIBLING_GAMEDATA_REL_CANDIDATES: Tuple[Tuple[str, ...], ...] = (
    ("gamedata",),
)

_TRANSCRIPT_CACHE: Dict[str, Tuple[Dict[str, str], frozenset[str], str]] = {}
_TRANSCRIPT_CACHE_LOCK = threading.RLock()

DEFAULT_QUALITY_PROFILE = "plus200"

REFERENCE_QUALITY_PROFILES: Dict[str, Dict[str, object]] = {
    "standard45": {
        "label": "⚡ 표준 45초 (빠른 복제·Zero-Shot · 36~48초 · 최대 8클립)",
        "target_dur_sec": 40.0,
        "max_dur_sec": 48.0,
        "early_stop_dur_sec": 28.0,
        "min_clips": 4,
        "target_clips": 6,
        "max_clips": 8,
        "min_matched_dur_sec": 22.0,
    },
    "pro85": {
        "label": "💎 고품질 85초 (무료 90초 한도 최적화 · 최대 14클립 · 대본 100% 검증)",
        "target_dur_sec": 74.0,
        "max_dur_sec": 85.0,
        "early_stop_dur_sec": 36.0,
        "min_clips": 6,
        "target_clips": 10,
        "max_clips": 14,
        "min_matched_dur_sec": 30.0,
    },
    "plus200": {
        "label": "👑 Plus 최고품질 200초 (Plus 210초 한도 최적화 · 최대 30클립 · 품질 게이트 · 추천)",
        "target_dur_sec": 185.0,
        "max_dur_sec": 200.0,
        "early_stop_dur_sec": 48.0,
        "min_clips": 6,
        "target_clips": 22,
        "max_clips": 30,
        "min_matched_dur_sec": 30.0,
    },
}

_WIN_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WIN_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)

_HARD_SCREAM_KEYWORDS = (
    "damage",
    "retire",
    "defeat",
    "shout",
    "groggy",
    "panic",
    "scream",
    "exskill",
    "attack",
    "battle_",
    "formation_",
    "commonskill",
    "commontsaskill",
    "_radio",
    "radio_",
    "bossoperator",
    "multifloorraid",
    "worldraid",
    "_sfx",
    "default_tts",
    "etc_fue",
    "work_sleep",
    "memoriallobby_0",
    "megaphone",
)

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
    "commontsaskill",
    "formation_",
    "summon",
    "minigame",
    "cardgame",
    "_radio",
    "radio_",
    "groggy",
    "panic",
    "scream",
    "megaphone",
    "bossoperator",
    "multifloorraid",
    "worldraid",
    "versus",
    "_sfx",
    "default_tts",
    "etc_fue",
    "work_sleep",
    "_tile",
    "zoneopen",
    "memoriallobby_0",
]

PRIORITY_KEYWORDS_CALM = [
    "lobby",
    "memoriallobby",
    "cafe",
    "relationship",
    "event",
    "login",
    "season",
    "title",
    "gacha",
]

PRIORITY_KEYWORDS_EXPRESSIVE = [
    "lobby",
    "relationship",
    "event",
    "cafe",
    "gacha",
    "login",
    "season",
    "title",
    "memoriallobby",
]

_CATEGORY_SCORES_CALM: Dict[str, float] = {
    "lobby": 50.0,
    "memoriallobby": 47.0,
    "cafe": 44.0,
    "relationship": 41.0,
    "event": 38.0,
    "login": 35.0,
    "season": 32.0,
    "title": 29.0,
    "gacha": 26.0,
}

_CATEGORY_SCORES_EXPRESSIVE: Dict[str, float] = {
    "lobby": 50.0,
    "relationship": 47.0,
    "event": 44.0,
    "cafe": 41.0,
    "gacha": 38.0,
    "login": 35.0,
    "season": 32.0,
    "title": 29.0,
    "memoriallobby": 18.0,
}

CALM_ARCHETYPES = {"cool_downer", "kuudere_deadpan", "lazy_my_pace"}

_SCREAM_OFFTONE_JP_RE = re.compile(
    r"(?:ぎゃあ|ぎゃっ|うわああ|うわぁぁ|きゃああ|きゃぁぁ|きゃひゃ|きゃはは|くははは|"
    r"ぐああ|ぐわあ|ああああ|ぁぁぁぁ|うううう|うぅぅぅ|ははははは|"
    r"はぁ[、,…・\s]+はぁ|はっ[、,…・\s]+はっ|ぜぇ[、,…・\s]+ぜぇ|"
    r"うっ[、,…・\s]+うっ|ひっ[、,…・\s]+ひっ|ぬぬぬ|くしゅん|へっくしゅ|"
    r"ごほっ|げほっ|むにゃ|すーすー|すぅすぅ|死にたい死にたい)"
)

_SCREAM_OFFTONE_KR_RE = re.compile(
    r"(?:갸아악|갸아아|으아악|으아아|우와아아|꺄아악|꺄아아|크아악|아아아악|"
    r"으으으으|우으으으|우으읏|하하하하하|꺄하하|캬하하|키히히|크하하|흐흐흐흐|"
    r"하아[,\s…\.]+하아|헉[,\s…\.]+헉|흐읍[,\s…\.]+흐읍|훌쩍훌쩍|훌쩍[…\.\s]*$|"
    r"에취|콜록|음냐|쿨쿨|드르렁|죽고\s*싶어\s*죽고\s*싶어)"
)

_EXCESSIVE_BANG_RE = re.compile(r"[!！]{3,}")
_ALNUM_SPEECH_RE = re.compile(r"[가-힣ぁ-んァ-ヶ一-龥A-Za-z0-9]")

_MEMORIAL_LOBBY_STEM_RE = re.compile(r"^(.+?)_memoriallobby_(\d+)(?:_(\d+))?(.*)$")

_STUDENT_PREFIX_ALIASES: Dict[str, str] = {
    "reijo": "reizyo",
    "aris": "alice",
    "hifumi": "hihumi",
    "hifumi_swimsuit": "hihumiswimsuit",
    "junko": "zunko",
}

_AOI_ANIM_TO_STEM: Dict[str, str] = {
    "Start01_Idle_01": "np0032_minieventlogin_1",
    "Start01_Idle_02": "np0032_minieventlogin_2",
    "Start01_Idle_Event_01": "np0032_minieventlogin_season_1",
    "Start01_Idle_Event_02": "np0032_minieventlogin_season_2",
    "Start01_Idle_Event_03": "np0032_minieventlogin_season_end",
    "Start01_Idle_Shop_01": "np0032_minieventshop_end",
    "Talk_01_M": "np0032_minieventlobby_1",
    "Talk_02_M": "np0032_minieventlobby_2",
    "Talk_03_M": "np0032_minieventlobby_3",
    "Talk_04_M": "np0032_minieventlobby_4",
    "Talk_05_M": "np0032_minieventlobby_5",
    "ShopBuy_01_M": "np0032_minieventshop_buy_1",
    "ShopBuy_02_M": "np0032_minieventshop_buy_2",
    "ShopBuy_03_M": "np0032_minieventshop_buy_3",
    "ShopBuy_04_M": "np0032_minieventshop_buy_4",
    "BoxGachaBuy_01_M": "np0032_minieventboxshop_spbuy_1",
    "BoxGachaBuy_02_M": "np0032_minieventboxshop_spbuy_2",
    "BoxGachaBuy_03_M": "np0032_minieventboxshop_spbuy_3",
    "BoxGachaBuy_300_M": "np0032_minieventboxshop_buy_300",
    "CardShop_01_M": "np0032_minieventcardpickup_1",
    "CardShop_02_M": "np0032_minieventcardpickup_2",
    "CardShop_03_M": "np0032_minieventcardpickup_3",
    "CardShop_04_M": "np0032_minieventcardpickup_4",
    "MissionGet_01_M": "np0032_minieventmission_get_1",
    "MissionGet_02_M": "np0032_minieventmission_get_2",
    "MissionComplete_01_M": "np0032_minieventmission_day_end",
    "FindTreasure_01_M": "np0032_minievent_findtreasure_1",
    "FindTreasure_02_M": "np0032_minievent_findtreasure_2",
    "GetTreasure_01_M": "np0032_minievent_gettreasure_1",
    "GetTreasure_02_M": "np0032_minievent_gettreasure_2",
    "RoundRenewal_01_M": "np0032_minievent_roundrenewal",
}


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


def _atomic_replace_path(src: Path, dst: Path) -> None:
    if dst.is_symlink():
        raise ValueError(f"Refusing to overwrite symlink path: {dst}")
    last_err: Exception | None = None
    for attempt in range(6):
        try:
            os.replace(src, dst)
            last_err = None
            break
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.04 * (attempt + 1))
    if last_err is not None:
        raise last_err


def _unlink_with_retries(target: Path) -> None:
    if not target.exists() and not target.is_symlink():
        return
    last_err: Exception | None = None
    for attempt in range(6):
        try:
            target.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.04 * (attempt + 1))
    if last_err is not None:
        raise last_err


def _atomic_write_text(target: Path, content: str) -> None:
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite symlink file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".ref_", suffix=".tmp", dir=str(target.parent))
    fd_closed = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd_closed = True
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        _atomic_replace_path(Path(tmp_name), target)
    except Exception:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_wav(target: Path, data: np.ndarray, sr: int, subtype: str = "PCM_16") -> None:
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite symlink file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".wav_", suffix=".wav", dir=str(target.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        sf.write(tmp_path, data, sr, subtype=subtype)
        _atomic_replace_path(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
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


def classify_clip_category(name: str) -> str:
    """
    Classify clip category without substring shadowing (`memoriallobby` is NEVER matched as `lobby`).
    Returns canonical category name or "" if uncategorized.
    """
    low = str(name or "").lower()
    if "memoriallobby" in low:
        return "memoriallobby"
    if "lobby" in low:
        return "lobby"
    for kw in ("relationship", "event", "cafe", "gacha", "login", "season", "title"):
        if kw in low:
            return kw
    return ""


def _count_alnum_speech_chars(text: str) -> int:
    return len(_ALNUM_SPEECH_RE.findall(str(text or "")))


def is_scream_or_offtone_transcript(text: str, lang: str = "JP") -> bool:
    """Detect screaming, panting, crying, coughing/sneezing, manic laugh, or sleep-talk lines."""
    s = str(text or "").strip()
    if not s:
        return False
    if _EXCESSIVE_BANG_RE.search(s):
        return True
    if str(lang or "JP").strip().upper() == "KR":
        return bool(_SCREAM_OFFTONE_KR_RE.search(s))
    return bool(_SCREAM_OFFTONE_JP_RE.search(s))


def _is_bandpass_radio_speech(y: np.ndarray, sr: int) -> bool:
    """
    Detect telephone/radio bandpass-filtered voice clips (`>4kHz` energy stripped & `300-3400Hz` > 96%),
    while requiring broadband formant energy above 600 Hz so pure single-tone test sines are never rejected.
    """
    if sr < 16000 or len(y) < sr // 2:
        return False
    n_fft = min(len(y), 65536)
    seg = y[:n_fft].astype(np.float64)
    seg = seg - float(np.mean(seg))
    spec = np.abs(np.fft.rfft(seg)) ** 2
    freqs = np.fft.rfftfreq(len(seg), d=1.0 / float(sr))
    mask_hp = freqs >= 75.0
    total_e = float(np.sum(spec[mask_hp]))
    if total_e <= 1e-12:
        return False
    e_mid = float(np.sum(spec[(freqs >= 300.0) & (freqs <= 3400.0)])) / total_e
    e_hi = float(np.sum(spec[freqs > 4000.0])) / total_e
    e_formant = float(np.sum(spec[(freqs >= 600.0) & (freqs <= 3400.0)])) / total_e
    return e_hi < 0.0025 and e_mid > 0.96 and e_formant > 0.20


def score_clip(
    name: str,
    dur: float,
    peak: float,
    rms: float,
    archetype: str = "gentle_earnest_passion",
    y: Optional[np.ndarray] = None,
    sr: int = 44100,
    *,
    transcript: str = "",
    lang: str = "JP",
    voiced_ratio: Optional[float] = None,
) -> float:
    low = str(name or "").lower()
    if any(bad in low for bad in EXCLUDE_KEYWORDS):
        return -1000.0
    if not (np.isfinite(dur) and np.isfinite(peak) and np.isfinite(rms)):
        return -500.0
    if dur < 2.8 or dur > 13.5:
        return -500.0
    if rms < 0.018 or peak > 1.55:
        return -200.0
    if y is not None and len(y) > 0:
        clipped_ratio = float(np.mean(np.abs(y) >= 0.99))
        if clipped_ratio > 0.008:
            return -250.0
        if _is_bandpass_radio_speech(y, sr):
            return -260.0

    # Transcript-level quality gates (screams/panting/crying/sleep-talk and sparse semantic lines)
    if transcript:
        if is_scream_or_offtone_transcript(transcript, lang=lang):
            return -250.0
        if dur >= 3.0 and _count_alnum_speech_chars(transcript) < 6:
            return -220.0

    cat = classify_clip_category(low)
    score_table = _CATEGORY_SCORES_CALM if archetype in CALM_ARCHETYPES else _CATEGORY_SCORES_EXPRESSIVE
    score = float(score_table.get(cat, 0.0))

    # Prefer rich 4.5s - 11.0s paragraph-like conversational lines
    if 4.5 <= dur <= 11.0:
        score += 20.0
    elif (3.0 <= dur < 4.5) or (11.0 < dur <= 13.5):
        score += 10.0
    else:
        score += 2.0

    score += min(rms * 100.0, 15.0)

    # Minor crest penalty for extreme spikes (handled cleanly by soft-knee limiter, never hard-rejected)
    crest_db = 20.0 * float(np.log10((peak + 1e-6) / (rms + 1e-6)))
    if crest_db > 19.5:
        score -= 8.0

    # Voiced-frame ratio gate & band-pass prosody contour evaluation
    eff_voiced_ratio = voiced_ratio
    if y is not None and sr > 0 and len(y) >= sr // 2:
        hop = max(256, sr // 20)  # 50ms envelope frames
        n_frames = len(y) // hop
        if n_frames >= 6:
            env = np.sqrt(np.mean(y[: n_frames * hop].reshape(n_frames, hop) ** 2, axis=1))
            voiced_mask = env > 0.015
            if eff_voiced_ratio is None:
                eff_voiced_ratio = float(np.mean(voiced_mask))
            voiced_env = env[voiced_mask]
            if len(voiced_env) >= 4:
                dyn_ratio = float(np.std(voiced_env) / (np.mean(voiced_env) + 1e-5))
                if archetype in CALM_ARCHETYPES:
                    if 0.34 <= dyn_ratio <= 0.52:
                        score += 10.0
                    elif dyn_ratio < 0.20:
                        score -= 6.0
                    elif dyn_ratio > 0.78:
                        score -= 10.0
                    else:
                        score += max(0.0, 8.0 - abs(dyn_ratio - 0.43) * 18.0)
                else:
                    if 0.42 <= dyn_ratio <= 0.68:
                        score += 12.0
                    elif dyn_ratio < 0.25:
                        score -= 8.0
                    elif dyn_ratio > 0.85:
                        score -= 10.0
                    else:
                        score += max(0.0, 10.0 - abs(dyn_ratio - 0.55) * 18.0)

    if eff_voiced_ratio is not None:
        if eff_voiced_ratio < 0.35:
            return -250.0
        if eff_voiced_ratio < 0.38:
            return -200.0
        if eff_voiced_ratio < 0.45:
            score -= 16.0
        elif eff_voiced_ratio >= 0.60:
            score += 4.0

    return score


def _trim_and_pad_speech_with_metrics(
    y: np.ndarray,
    sr: int,
    threshold_dbfs: float = TRIM_THRESHOLD_DBFS,
    lead_pad_ms: float = LEAD_PAD_MS,
    tail_pad_ms: float = TAIL_PAD_MS,
) -> Tuple[np.ndarray, float, float]:
    """
    Trim outer silence using 10ms frame RMS at `threshold_dbfs` (-45 dBFS), keep natural source margin,
    and explicitly zero-pad whenever raw audio starts/ends closer than `lead_pad_ms` / `tail_pad_ms`
    so HPF and edge fades NEVER attenuate initial consonants or release tails.

    Returns `(padded_audio, raw_speech_span_sec, raw_speech_voiced_ratio)`.
    """
    y32 = np.nan_to_num(np.asarray(y, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    if sr <= 0 or y32.size == 0:
        return np.zeros(0, dtype=np.float32), 0.0, 0.0

    thresh_lin = float(10.0 ** (threshold_dbfs / 20.0))
    frame_len = max(1, int(round(sr * 0.010)))  # 10ms frames ignore isolated single-sample ticks
    n_frames = len(y32) // frame_len

    speech_start = -1
    speech_end = -1
    if n_frames >= 1:
        frames = y32[: n_frames * frame_len].reshape(n_frames, frame_len)
        frame_rms = np.sqrt(np.mean(frames**2, axis=1))
        active_idx = np.where(frame_rms > thresh_lin)[0]
        if len(active_idx) > 0:
            speech_start = int(active_idx[0]) * frame_len
            speech_end = min(len(y32), (int(active_idx[-1]) + 1) * frame_len)
            # Check trailing partial frame if any
            rem = y32[n_frames * frame_len :]
            if len(rem) > 0 and float(np.sqrt(np.mean(rem**2))) > thresh_lin:
                speech_end = len(y32)

    if speech_start < 0:
        non_silent = np.where(np.abs(y32) > thresh_lin)[0]
        if len(non_silent) == 0:
            return np.zeros(0, dtype=np.float32), 0.0, 0.0
        speech_start = int(non_silent[0])
        speech_end = int(non_silent[-1]) + 1

    speech_span_sec = max(0.0, (speech_end - speech_start) / float(sr))

    # Measure voiced-frame ratio strictly on the unpadded speech span (before adding 140ms silence margins)
    core = y32[speech_start:speech_end]
    hop50 = max(256, sr // 20)
    n50 = len(core) // hop50
    if n50 >= 2:
        env50 = np.sqrt(np.mean(core[: n50 * hop50].reshape(n50, hop50) ** 2, axis=1))
        voiced_ratio = float(np.mean(env50 > 0.015))
    else:
        voiced_ratio = 1.0 if float(np.sqrt(np.mean(core**2))) > 0.015 else 0.0

    lead_samples = max(0, int(round(sr * (lead_pad_ms / 1000.0))))
    tail_samples = max(0, int(round(sr * (tail_pad_ms / 1000.0))))

    slice_start = max(0, speech_start - lead_samples)
    slice_end = min(len(y32), speech_end + tail_samples)
    avail_lead = speech_start - slice_start
    avail_tail = slice_end - speech_end
    need_zero_lead = max(0, lead_samples - avail_lead)
    need_zero_tail = max(0, tail_samples - avail_tail)

    seg = y32[slice_start:slice_end].astype(np.float32, copy=True)
    if seg.size == 0:
        return np.zeros(0, dtype=np.float32), 0.0, 0.0

    # Remove DC offset on raw segment BEFORE concatenating zero-pads so no DC step is created at boundaries
    seg = seg - float(np.mean(seg))

    # Tiny 1.5ms click-guard taper strictly when zero-pad meets a non-zero raw boundary sample
    taper_n = min(max(1, int(round(sr * 0.0015))), len(seg) // 8)
    if need_zero_lead > 0 and taper_n > 1 and abs(float(seg[0])) > 0.005:
        ramp = (0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, taper_n, dtype=np.float32)))).astype(np.float32)
        seg[:taper_n] *= ramp
    if need_zero_tail > 0 and taper_n > 1 and abs(float(seg[-1])) > 0.005:
        ramp = (0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, taper_n, dtype=np.float32)))).astype(np.float32)
        seg[-taper_n:] *= ramp[::-1]

    if need_zero_lead > 0 or need_zero_tail > 0:
        parts = []
        if need_zero_lead > 0:
            parts.append(np.zeros(need_zero_lead, dtype=np.float32))
        parts.append(seg)
        if need_zero_tail > 0:
            parts.append(np.zeros(need_zero_tail, dtype=np.float32))
        padded = np.concatenate(parts)
    else:
        padded = seg

    return padded.astype(np.float32, copy=False), speech_span_sec, voiced_ratio


def trim_and_pad_speech(
    y: np.ndarray,
    sr: int,
    threshold_dbfs: float = TRIM_THRESHOLD_DBFS,
    lead_pad_ms: float = LEAD_PAD_MS,
    tail_pad_ms: float = TAIL_PAD_MS,
) -> np.ndarray:
    """Trim outer silence (-45 dBFS, 10ms RMS) and guarantee >=60ms lead / >=80ms tail pre-HPF zero-padding."""
    padded, _, _ = _trim_and_pad_speech_with_metrics(
        y, sr, threshold_dbfs=threshold_dbfs, lead_pad_ms=lead_pad_ms, tail_pad_ms=tail_pad_ms
    )
    return padded


def resample_audio(y: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    y32 = np.nan_to_num(np.asarray(y, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    if orig_sr <= 0 or target_sr <= 0 or orig_sr == target_sr or len(y32) == 0:
        return y32
    from scipy.signal import resample_poly

    g = gcd(int(orig_sr), int(target_sr))
    out = resample_poly(y32, int(target_sr) // g, int(orig_sr) // g).astype(np.float32)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def normalize_peak(y: np.ndarray, target_dbfs: float = PEAK_CEILING_DBFS) -> np.ndarray:
    y32 = np.nan_to_num(np.asarray(y, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    if len(y32) == 0:
        return y32
    peak = float(np.max(np.abs(y32)))
    if peak < 1e-6:
        return y32
    target_lin = 10.0 ** (target_dbfs / 20.0)
    return (y32 * (target_lin / peak)).astype(np.float32)


def highpass_filter(y: np.ndarray, sr: int, cutoff_hz: float = HIGHPASS_HZ) -> np.ndarray:
    """2nd-order zero-phase Butterworth high-pass filter (65 Hz) to strip DC/hum/sub-bass rumble."""
    y32 = np.nan_to_num(np.asarray(y, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    if len(y32) < 64 or sr <= 200 or cutoff_hz <= 0.0 or cutoff_hz >= sr * 0.45:
        return y32
    from scipy.signal import butter, sosfiltfilt

    sos = butter(2, cutoff_hz / (sr * 0.5), btype="highpass", output="sos")
    out = sosfiltfilt(sos, y32).astype(np.float32)
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def _measure_relative_active_rms(y32: np.ndarray, sr: int = TARGET_SR) -> float:
    """Gain-invariant 20ms-frame active-speech RMS using a relative gate (-20 dB below 95th percentile)."""
    if len(y32) == 0:
        return 0.0
    frame_len = max(64, int(round(max(8000, int(sr)) * 0.020)))
    n_frames = len(y32) // frame_len
    if n_frames >= 2:
        frames = y32[: n_frames * frame_len].reshape(n_frames, frame_len)
        frame_rms = np.sqrt(np.mean(frames**2, axis=1))
        p95 = float(np.percentile(frame_rms, 95))
        gate = max(p95 * 0.1, 1e-4)
        active = frame_rms[frame_rms >= gate]
        if len(active) >= 1:
            return float(np.sqrt(np.mean(active**2)))
    return float(np.sqrt(np.mean(y32**2)))


def normalize_active_rms(
    y: np.ndarray,
    target_rms_dbfs: float = TARGET_RMS_DBFS,
    peak_ceiling_dbfs: float = PEAK_CEILING_DBFS,
    *,
    sr: int = TARGET_SR,
    soft_knee_dbfs: float = SOFT_LIMITER_KNEE_DBFS,
) -> np.ndarray:
    """
    2-pass relative-gated active-speech RMS loudness normalizer (-18.0 dBFS) with a transparent
    C1-smooth soft-knee peak limiter (-3.0 dBFS knee -> -1.2 dBFS ceiling, 4.5 dB max GR cap).
    Samples below -3.0 dBFS remain 100% linear and untouched.
    """
    y32 = np.nan_to_num(np.asarray(y, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    if len(y32) == 0:
        return y32
    rms = _measure_relative_active_rms(y32, sr=sr)
    if rms < 1e-6 or not np.isfinite(rms):
        return y32

    target_rms = float(10.0 ** (target_rms_dbfs / 20.0))
    peak_ceil = float(10.0 ** (peak_ceiling_dbfs / 20.0))
    knee_lin = float(10.0 ** (min(soft_knee_dbfs, peak_ceiling_dbfs - 0.5) / 20.0))

    scaled = y32 * (target_rms / rms)
    peak_scaled = float(np.max(np.abs(scaled)))

    if peak_scaled > knee_lin:
        if peak_scaled > peak_ceil:
            over_ceil_db = 20.0 * float(np.log10(peak_scaled / peak_ceil))
            if over_ceil_db > SOFT_LIMITER_MAX_GR_DB:
                backoff = float(10.0 ** (-(over_ceil_db - SOFT_LIMITER_MAX_GR_DB) / 20.0))
                scaled = scaled * backoff

        headroom = max(peak_ceil - knee_lin, 1e-5)
        abs_s = np.abs(scaled)
        over_mask = abs_s > knee_lin
        if np.any(over_mask):
            excess = abs_s[over_mask] - knee_lin
            limited_mag = knee_lin + headroom * np.tanh(excess / headroom)
            scaled = scaled.copy()
            scaled[over_mask] = np.sign(scaled[over_mask]) * limited_mag

    return np.clip(scaled, -peak_ceil, peak_ceil).astype(np.float32)


def apply_cosine_fade(y: np.ndarray, sr: int, fade_ms: float = EDGE_FADE_MS) -> np.ndarray:
    """Apply smooth raised-cosine edge fade-in/out (10 ms) inside the outer margin of padded clips."""
    if sr <= 0 or len(y) == 0:
        return np.asarray(y, dtype=np.float32)
    n_fade = min(int(round(sr * (fade_ms / 1000.0))), len(y) // 4)
    if n_fade <= 1:
        return y.astype(np.float32)
    out = y.astype(np.float32, copy=True)
    ramp = (0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, n_fade, dtype=np.float32)))).astype(np.float32)
    out[:n_fade] *= ramp
    out[-n_fade:] *= ramp[::-1]
    return out


def master_studio_clip(y: np.ndarray, orig_sr: int, target_sr: int = TARGET_SR) -> np.ndarray:
    """
    Full studio mastering chain for Fish Audio S2/S2.1 reference WAVs
    (DC -> 65 Hz HPF -> 44.1 kHz -> 2-pass relative active RMS -18 dBFS + -3/-1.2 dBFS soft-knee limiter -> 10 ms fade).
    Preserves exact resampled clip length (call `trim_and_pad_speech` beforehand for pre-HPF zero-padding).
    """
    y32 = np.nan_to_num(np.asarray(y, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    if len(y32) == 0 or orig_sr <= 0:
        return y32
    y32 = y32 - float(np.mean(y32))
    y32 = highpass_filter(y32, orig_sr, cutoff_hz=HIGHPASS_HZ)
    y_resamp = resample_audio(y32, orig_sr, target_sr)
    y_norm = normalize_active_rms(y_resamp, TARGET_RMS_DBFS, PEAK_CEILING_DBFS, sr=target_sr)
    return apply_cosine_fade(y_norm, target_sr, fade_ms=EDGE_FADE_MS)


def _clean_official_transcript(raw_text: str, lang: str) -> str:
    """
    Clean official Blue Archive transcript for 100%-verbatim Fish Audio S2/S2.1 alignment:
    - Rejects `[USERNAME]`/`[NICKNAME]` placeholder lines.
    - Strips NGUI/HTML markup while UNWRAPPING spoken `[word]` / `<title>` quotes (0 stray brackets for S2).
    - Strips JP ruby furigana / KR Hanja glosses & explicit SFX/eating stage directions.
    - Removes inner-monologue paren delimiters `（`/`）`/`(`/`)` and decorative music/star symbols.
    - Joins JP line wraps with `""` (preserving English spaces) and removes stray CJK mid-word spaces;
      joins KR line wraps with `" "` preserving Korean word spacing.
    """
    if not raw_text:
        return ""
    if "[USERNAME]" in raw_text or "[NICKNAME]" in raw_text:
        return ""
    lang_up = str(lang or "JP").strip().upper()
    s = raw_text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("\u3000", " ")

    # 1. Strip NGUI color/style tags & HTML tags ONLY
    s = re.sub(r"\[(?:/?[cbiu]|-|[0-9a-fA-F]{6,8})\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"</?(?:color|size|b|i|u|ruby|br)[^>]*>", "", s, flags=re.IGNORECASE)

    # 2. Unwrap spoken [word] and <title> quotes without deleting their spoken content
    s = re.sub(r"\[([^\[\]]+)\]", r"\1", s)
    s = re.sub(r"<([^<>]+)>", r"\1", s)
    s = s.replace("[", "").replace("]", "").replace("<", "").replace(">", "")

    # 3. Strip ruby / Hanja reading glosses attached to Kanji/Hangul
    if lang_up == "JP":
        s = re.sub(r"(?<=[一-龥])[（(][ぁ-んァ-ヶー]+[）)]", "", s)
    elif lang_up == "KR":
        s = re.sub(r"(?<=[가-힣])[（(][一-龥]+[）)]", "", s)

    # 4. Strip explicit unspoken SFX / eating / muttering stage-direction parens
    s = re.sub(
        r"[（(]\s*(?:コクン|ぶつぶつ|パリッ|もぐもぐ|むしゃむしゃ|"
        r"꿀꺽[,\s꿀꺽]*|냠냠[\s쩝쩝]*|와구와구|와작|우물우물|꾸벅|중얼중얼)\s*[）)]",
        "",
        s,
    )

    # 5. Remove remaining inner-monologue paren delimiters (handles whole-line, multi-box, and mixed parens)
    s = re.sub(r"[（()）]", "", s)

    # 6. Strip decorative non-spoken symbols
    s = re.sub(r"[♪♬♫♡♥☆★※]+", "", s)

    # 7. Language-specific line-break & punctuation normalization
    if lang_up == "JP":
        s = re.sub(r"[ \t]*\n[ \t]*", "\n", s)
        s = re.sub(r"(?<=[A-Za-z0-9])\n(?=[A-Za-z0-9])", " ", s)
        s = s.replace("\n", "")
        s = re.sub(
            r"(?<=[\u2014\u2015\u2026\u3000-\u30ff\u4e00-\u9fff\uff01-\uff60])[ \t]+"
            r"(?=[\u2014\u2015\u2026\u3000-\u30ff\u4e00-\u9fff\uff01-\uff60])",
            "",
            s,
        )
        s = re.sub(r"\.{3,}", "……", s)
        s = re.sub(r"[~～〜]{2,}", "～", s)
    else:
        s = re.sub(r"[ \t]*\n[ \t]*", " ", s)
        s = re.sub(r"\.{4,}", "...", s)
        s = re.sub(r"[~～〜]{2,}", "~", s)

    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 4:
        return ""
    if lang_up == "KR" and not re.search(r"[가-힣]", s):
        return ""
    if lang_up == "JP" and not re.search(r"[ぁ-んァ-ヶ一-龥]", s):
        return ""
    return s


def resolve_optional_gamedata_dir(gamedata_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Resolve optional Blue Archive gamedata directory safely (returns None cleanly when absent).
    Precedence:
      1. Explicit `gamedata_dir` argument
      2. `BA_GAMEDATA_DIR` environment variable (set to `"none"`/`"off"`/`"disabled"` to force off)
      3. `get_user_data_dir() / "gamedata"`
      4. `get_app_root() / "config" / "gamedata"`
      5. `OPTIONAL_GAMEDATA_DIR` module override
      6. Sibling workspace fallback (`get_app_root().parent / ...`, only if `.is_dir()`)
    """
    if gamedata_dir is not None:
        return gamedata_dir if gamedata_dir.is_dir() else None

    env_gd = os.environ.get("BA_GAMEDATA_DIR", "").strip()
    if env_gd.lower() in ("none", "off", "disabled", "0"):
        return None

    app_root = get_app_root()
    candidates: List[Path] = []
    if env_gd:
        candidates.append(Path(env_gd))
    candidates.append(get_user_data_dir() / "gamedata")
    candidates.append(app_root / "config" / "gamedata")
    if OPTIONAL_GAMEDATA_DIR is not None:
        candidates.append(OPTIONAL_GAMEDATA_DIR)
    for rel_parts in _SIBLING_GAMEDATA_REL_CANDIDATES:
        candidates.append(app_root.parent.joinpath(*rel_parts))

    for cand in candidates:
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return None


def get_gamedata_signature(lang: str = "JP", gamedata_dir: Optional[Path] = None) -> str:
    """Fast deterministic signature of available gamedata sources for cache invalidation ('' if absent)."""
    lang_up = str(lang or "JP").strip().upper()
    eff_dir = resolve_optional_gamedata_dir(gamedata_dir)
    if eff_dir is None:
        return ""
    jp_dir = eff_dir.parent / "gamedata_jp"
    has_jp_dir = lang_up == "JP" and jp_dir.is_dir()
    key_files = (
        "schale_jp_voice.json",
        "schale_kr_voice.json",
        "VoiceExcel.json",
        "VoiceSpineExcel.json",
        "CharacterDialogExcel.json",
        "CharacterDialogEventExcel.json",
        "CharacterDialogBattlePassExcel.json",
        "CharacterVoiceExcel.json",
        "CharacterVoiceSubtitleExcel.json",
    )
    def _file_entries(base_dir: Path) -> List[List[Any]]:
        entries: List[List[Any]] = []
        for fn in key_files:
            fp = base_dir / fn
            try:
                if fp.is_file():
                    st = fp.stat()
                    entries.append([fn, int(st.st_size), int(st.st_mtime_ns)])
            except OSError:
                continue
        return entries

    present_eff = _file_entries(eff_dir)
    present_jp = _file_entries(jp_dir) if has_jp_dir else []
    if not present_eff and not present_jp:
        return ""
    payload = json.dumps(
        {
            "lang": lang_up,
            "eff": str(eff_dir.resolve()),
            "jp": str(jp_dir.resolve()) if has_jp_dir else "",
            "eff_files": present_eff,
            "jp_files": present_jp,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def clear_official_transcript_cache() -> None:
    """Clear in-memory cached transcript tables (useful in tests or after gamedata changes)."""
    with _TRANSCRIPT_CACHE_LOCK:
        _TRANSCRIPT_CACHE.clear()


def _read_gamedata_json(path: Path) -> Any:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        if path.stat().st_size > MAX_GAMEDATA_JSON_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_excel_rows(path: Path) -> List[dict]:
    raw = _read_gamedata_json(path)
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        rows = next((v for v in raw.values() if isinstance(v, list)), [])
        return [r for r in rows if isinstance(r, dict)]
    return []


def _extract_lang_stems_from_paths(paths: Any, lang_up: str) -> List[str]:
    if isinstance(paths, str):
        path_list = [paths]
    elif isinstance(paths, list):
        path_list = [str(p or "") for p in paths]
    else:
        return []
    want_low = f"voc_{lang_up.lower()}/"
    want_mid = f"/{want_low}"
    stems: List[str] = []
    for p in path_list:
        p_norm = p.replace("\\", "/").strip()
        if not p_norm:
            continue
        p_low = p_norm.lower()
        if want_mid in p_low or p_low.startswith(want_low):
            st = Path(p_norm).stem.lower()
            if st:
                stems.append(st)
    return stems


def _parse_voice_ids(val: Any) -> List[int]:
    if val is None:
        return []
    if isinstance(val, int) and not isinstance(val, bool):
        return [val] if val != 0 else []
    if isinstance(val, list):
        out: List[int] = []
        for x in val:
            if isinstance(x, bool) or x is None:
                continue
            try:
                iv = int(x)
                if iv != 0:
                    out.append(iv)
            except (TypeError, ValueError):
                continue
        return out
    return []


def _index_dialog_table_with_voice_ids(
    rows: List[dict],
    group_key_fn: Callable[[dict], Optional[Tuple[Any, ...]]],
    loc_key: str,
    vid_to_stems: Dict[int, set[str]],
    lang_up: str,
    stem_to_text: Dict[str, str],
) -> None:
    """
    Tier 2 multi-box continuation segmentation:
    Walks rows sorted by `display_order`, joining continuation subtitle boxes (`voice_id == []`)
    onto the active clip's `voice_id`, deduplicating repeated identical `voice_id` rows,
    and skipping spliced multi-voice (`len(voice_id) > 1`) `[USERNAME]` rows.
    """
    grouped: Dict[Tuple[Any, ...], List[dict]] = defaultdict(list)
    for r in rows:
        if r.get("teen_mode") is True:
            continue
        gk = group_key_fn(r)
        if gk is not None:
            grouped[gk].append(r)

    for g_rows in grouped.values():
        g_rows.sort(key=lambda x: int(x.get("display_order") or 0))
        cur_vid: Optional[int] = None
        cur_rows: List[dict] = []

        def _flush() -> None:
            nonlocal cur_vid, cur_rows
            if cur_vid is not None and cur_rows:
                raws = [str(x.get(loc_key) or "") for x in cur_rows]
                if not any(("[USERNAME]" in t or "[NICKNAME]" in t) for t in raws):
                    joined = "\n".join(t for t in raws if t.strip())
                    cleaned = _clean_official_transcript(joined, lang_up)
                    if cleaned:
                        for st in vid_to_stems.get(cur_vid, ()):
                            stem_to_text.setdefault(st, cleaned)
            cur_vid = None
            cur_rows = []

        for r in g_rows:
            vids = _parse_voice_ids(r.get("voice_id"))
            if len(vids) == 1:
                if vids[0] == cur_vid:
                    continue  # skip duplicate repeated rows (e.g. ch0250_minigame_*)
                _flush()
                cur_vid = vids[0]
                cur_rows = [r]
            elif len(vids) == 0:
                if cur_vid is not None:
                    cur_rows.append(r)  # continuation box spoken in same audio clip
            else:
                _flush()  # skip spliced multi-voice rows

        _flush()


def _load_npc_spine_transcripts(
    cd_rows: List[dict],
    cde_rows: List[dict],
    loc_key: str,
    lang_up: str,
    stem_to_text: Dict[str, str],
) -> None:
    """Tier 4: Verified Spine `.skel` animation_name -> stem recovery for Sora, Momoka, Rin, Ayumu, Aoi."""

    def _assign_group(stem: str, g_rows: List[dict]) -> None:
        raws = [str(x.get(loc_key) or "") for x in g_rows]
        if any(("[USERNAME]" in t or "[NICKNAME]" in t) for t in raws):
            return
        cleaned = _clean_official_transcript("\n".join(t for t in raws if t.strip()), lang_up)
        if cleaned:
            stem_to_text.setdefault(stem, cleaned)

    # Sora (9009001, cat=9), Momoka (9009002, cat=14), Rin (9009003, cat=17), Ayumu (9009004, cat=40)
    npc_cd_groups: Dict[Tuple[int, int, int, int], List[dict]] = defaultdict(list)
    for r in cd_rows:
        if r.get("teen_mode") is True:
            continue
        try:
            cid = int(r.get("character_id") or 0)
            cat = int(r.get("dialog_category") if r.get("dialog_category") is not None else -1)
            cond = int(r.get("dialog_condition") or 0)
            grp = int(r.get("group_id") or 0)
        except (TypeError, ValueError):
            continue
        if (cid, cat) in ((9009001, 9), (9009002, 14), (9009003, 17), (9009004, 40)):
            npc_cd_groups[(cid, cat, cond, grp)].append(r)

    for (cid, cat, cond, grp), g_rows in npc_cd_groups.items():
        g_rows.sort(key=lambda x: int(x.get("display_order") or 0))
        anim = str(g_rows[0].get("animation_name") or "").strip()
        stem = ""
        if cid == 9009001 and cat == 9:  # Sora
            if cond == 2 and grp in (1, 2):
                stem = f"sora_shop_out_{grp}"
            elif anim == "Start02_Idle_01":
                stem = "sora_shop_in_1"
            elif anim == "Start01_Idle_01":
                stem = "sora_shop_in_2"
            else:
                m = re.fullmatch(r"Talk_0([1-7])_M", anim)
                if m:
                    stem = f"sora_shop_talk_{m.group(1)}"
        elif cid == 9009002 and cat == 14:  # Momoka
            if anim == "Start01_Idle_01":
                stem = "momoka_weekdungeon_in_1"
            elif anim == "Start02_Idle_01":
                stem = "momoka_weekdungeon_in_2"
            else:
                m = re.fullmatch(r"Talk_0([1-7])_M", anim)
                if m:
                    stem = f"momoka_weekdungeon_talk_{m.group(1)}"
        elif cid == 9009003 and cat == 17:  # Rin
            if anim == "Start01_Idle_01":
                stem = "rin_raidlobby_in_1"
            elif anim == "Start02_Idle_01":
                stem = "rin_raidlobby_in_2"
            else:
                m = re.fullmatch(r"Talk_0([1-6])_M", anim)
                if m:
                    stem = f"rin_raidlobby_talk_{m.group(1)}"
        elif cid == 9009004 and cat == 40:  # Ayumu
            m_start = re.fullmatch(r"Start01_Idle_0([12])", anim)
            m_talk = re.fullmatch(r"Talk_0([1-5])_M", anim)
            m_get = re.fullmatch(r"Get_0([1-4])_M", anim)
            m_comp = re.fullmatch(r"Complete_0([12])_M", anim)
            if m_start:
                stem = f"np0013_mission_login_{m_start.group(1)}"
            elif m_talk:
                stem = f"np0013_mission_{m_talk.group(1)}"
            elif m_get:
                stem = f"np0013_mission_get_{m_get.group(1)}"
            elif m_comp:
                stem = f"np0013_mission_daycomplete_{m_comp.group(1)}"

        if stem:
            _assign_group(stem, g_rows)

    # Aoi (CharacterDialogEventExcel, costume_unique_id == 1900914001)
    aoi_groups: Dict[Tuple[int, int, int, int, int], List[dict]] = defaultdict(list)
    for r in cde_rows:
        if r.get("teen_mode") is True:
            continue
        try:
            costume_id = int(r.get("costume_unique_id") or 0)
            if costume_id != 1900914001:
                continue
            key = (
                int(r.get("event_id") or 0),
                int(r.get("dialog_category") or 0),
                int(r.get("dialog_condition") or 0),
                int(r.get("dialog_condition_detail") or 0),
                int(r.get("group_id") or 0),
            )
        except (TypeError, ValueError):
            continue
        aoi_groups[key].append(r)

    for key in sorted(aoi_groups.keys()):
        g_rows = aoi_groups[key]
        g_rows.sort(key=lambda x: int(x.get("display_order") or 0))
        anim = str(g_rows[0].get("animation_name") or "").strip()
        stem = _AOI_ANIM_TO_STEM.get(anim, "")
        if stem:
            _assign_group(stem, g_rows)


def _load_official_transcript_bundle(
    lang: str = "JP",
    gamedata_dir: Optional[Path] = None,
) -> Tuple[Dict[str, str], frozenset[str], str]:
    """
    Load verified 5-tier official transcripts plus `ml_multi_stems` (multi-OGG N:N MemorialLobby stems
    subject to speaking-rate verification) and a deterministic `gamedata_sig`.
    """
    lang_up = str(lang or "JP").strip().upper()
    eff_dir = resolve_optional_gamedata_dir(gamedata_dir)
    gd_sig = get_gamedata_signature(lang_up, gamedata_dir=gamedata_dir)
    cache_key = f"{lang_up}::{eff_dir}::{gd_sig}"
    with _TRANSCRIPT_CACHE_LOCK:
        cached = _TRANSCRIPT_CACHE.get(cache_key)
        if cached is not None:
            return cached

    stem_to_text: Dict[str, str] = {}
    ml_multi_stems: set[str] = set()
    if eff_dir is None or not eff_dir.is_dir():
        result = (stem_to_text, frozenset(ml_multi_stems), "")
        with _TRANSCRIPT_CACHE_LOCK:
            _TRANSCRIPT_CACHE[cache_key] = result
        return result

    jp_dir = eff_dir.parent / "gamedata_jp"
    has_jp_dir = lang_up == "JP" and jp_dir.is_dir()
    excel_dirs: List[Path] = [jp_dir, eff_dir] if has_jp_dir else [eff_dir]
    loc_key = "localize_kr" if lang_up == "KR" else "localize_jp"

    # ------------------------------------------------------------------
    # Tier 1: schale_*_voice.json (explicit AudioClip -> Transcription)
    # ------------------------------------------------------------------
    try:
        sv_file = eff_dir / ("schale_kr_voice.json" if lang_up == "KR" else "schale_jp_voice.json")
        sv = _read_gamedata_json(sv_file)
        if isinstance(sv, dict):
            for v in sv.values():
                if not isinstance(v, dict):
                    continue
                for items in v.values():
                    if not isinstance(items, list):
                        continue
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        clip = str(it.get("AudioClip") or "").strip()
                        tr = _clean_official_transcript(str(it.get("Transcription") or ""), lang_up)
                        if clip and tr:
                            stem_to_text[Path(clip).stem.lower()] = tr
    except Exception:
        pass

    # Pre-read per-dir dialog tables once so Tiers 2, 4, and 5 don't re-parse large JSONs repeatedly
    cd_rows_by_dir: Dict[Path, List[dict]] = {}
    cde_rows_by_dir: Dict[Path, List[dict]] = {}
    cdbp_rows_by_dir: Dict[Path, List[dict]] = {}
    for d in excel_dirs:
        cd_rows_by_dir[d] = _read_excel_rows(d / "CharacterDialogExcel.json")
        cde_rows_by_dir[d] = _read_excel_rows(d / "CharacterDialogEventExcel.json")
        cdbp_rows_by_dir[d] = _read_excel_rows(d / "CharacterDialogBattlePassExcel.json")

    # ------------------------------------------------------------------
    # Tier 2: VoiceExcel.json x (CD [cat != 8] + CDE + CDBP) with multi-box joining
    # ------------------------------------------------------------------
    try:
        vid_to_stems: Dict[int, set[str]] = defaultdict(set)
        for d in excel_dirs:
            for r in _read_excel_rows(d / "VoiceExcel.json"):
                try:
                    vid = int(r.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if vid == 0:
                    continue
                for st in _extract_lang_stems_from_paths(r.get("path"), lang_up):
                    vid_to_stems[vid].add(st)

        if vid_to_stems:
            def _cd_non_ml_key(r: dict) -> Optional[Tuple[Any, ...]]:
                try:
                    cat = int(r.get("dialog_category") if r.get("dialog_category") is not None else -1)
                    if cat == 8:
                        return None
                    return (
                        int(r.get("character_id") or 0),
                        cat,
                        int(r.get("dialog_condition") or 0),
                        int(r.get("group_id") or 0),
                    )
                except (TypeError, ValueError):
                    return None

            def _cde_key(r: dict) -> Optional[Tuple[Any, ...]]:
                try:
                    return (
                        int(r.get("costume_unique_id") or 0),
                        int(r.get("event_id") or 0),
                        int(r.get("dialog_category") or 0),
                        int(r.get("dialog_condition") or 0),
                        int(r.get("dialog_condition_detail") or 0),
                        int(r.get("group_id") or 0),
                    )
                except (TypeError, ValueError):
                    return None

            def _cdbp_key(r: dict) -> Optional[Tuple[Any, ...]]:
                try:
                    return (
                        int(r.get("costume_unique_id") or 0),
                        int(r.get("battle_pass_id") or 0),
                        int(r.get("dialog_category") or 0),
                        int(r.get("dialog_condition") or 0),
                        int(r.get("group_id") or 0),
                    )
                except (TypeError, ValueError):
                    return None

            for d in excel_dirs:
                _index_dialog_table_with_voice_ids(
                    cd_rows_by_dir[d], _cd_non_ml_key, loc_key, vid_to_stems, lang_up, stem_to_text
                )
                _index_dialog_table_with_voice_ids(
                    cde_rows_by_dir[d], _cde_key, loc_key, vid_to_stems, lang_up, stem_to_text
                )
                _index_dialog_table_with_voice_ids(
                    cdbp_rows_by_dir[d], _cdbp_key, loc_key, vid_to_stems, lang_up, stem_to_text
                )
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Tier 3: CharacterVoiceExcel.json x CharacterVoiceSubtitleExcel.json
    # ------------------------------------------------------------------
    try:
        for d in excel_dirs:
            sub_rows = _read_excel_rows(d / "CharacterVoiceSubtitleExcel.json")
            cv_rows = _read_excel_rows(d / "CharacterVoiceExcel.json")
            if not sub_rows or not cv_rows:
                continue
            sub_map: Dict[Tuple[int, str], List[str]] = defaultdict(list)
            for sr_row in sub_rows:
                try:
                    gid = int(sr_row.get("character_voice_group_id") or 0)
                except (TypeError, ValueError):
                    continue
                lcv = str(sr_row.get("localize_cv_group") or "").strip()
                txt_raw = str(sr_row.get(loc_key) or "")
                if gid and lcv and txt_raw.strip():
                    sub_map[(gid, lcv)].append(txt_raw)
            for cv_row in cv_rows:
                try:
                    gid = int(cv_row.get("character_voice_group_id") or 0)
                except (TypeError, ValueError):
                    continue
                lcv = str(cv_row.get("localize_cv_group") or "").strip()
                raws = sub_map.get((gid, lcv))
                if not raws:
                    continue
                if any(("[USERNAME]" in t or "[NICKNAME]" in t) for t in raws):
                    continue
                cleaned = _clean_official_transcript("\n".join(raws), lang_up)
                if not cleaned:
                    continue
                for st in _extract_lang_stems_from_paths(cv_row.get("path"), lang_up):
                    stem_to_text.setdefault(st, cleaned)
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Tier 4: 7 Zero-Transcript NPCs (Sora, Momoka, Rin, Ayumu, Aoi via verified Spine bindings)
    # ------------------------------------------------------------------
    try:
        for d in excel_dirs:
            _load_npc_spine_transcripts(
                cd_rows_by_dir[d], cde_rows_by_dir[d], loc_key, lang_up, stem_to_text
            )
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Tier 5: Strictly Verified MemorialLobby (CharacterDialogExcel cat == 8 x VoiceSpineExcel)
    # ------------------------------------------------------------------
    try:
        vs_by_pg_sets: Dict[Tuple[str, int], set[Tuple[int, str]]] = defaultdict(set)
        for d in excel_dirs:
            for r in _read_excel_rows(d / "VoiceSpineExcel.json"):
                for st in _extract_lang_stems_from_paths(r.get("path"), lang_up):
                    m = _MEMORIAL_LOBBY_STEM_RE.match(st)
                    if not m:
                        continue
                    pref_low, grp_s, sub_s, tail = m.group(1), m.group(2), m.group(3), m.group(4)
                    if tail:
                        continue  # excludes *_sfx etc.
                    grp_i = int(grp_s)
                    if grp_i == 0:
                        continue  # memoriallobby_0 has 0 dialog rows
                    sub_i = int(sub_s) if sub_s is not None else 0
                    vs_by_pg_sets[(pref_low, grp_i)].add((sub_i, st))

        vs_by_pg: Dict[Tuple[str, int], List[Tuple[int, str]]] = {
            k: sorted(v, key=lambda x: (x[0], x[1])) for k, v in vs_by_pg_sets.items()
        }

        sk_file = eff_dir / (
            "schale_kr_students.json" if (eff_dir / "schale_kr_students.json").exists() else "schale_jp_students.json"
        )
        raw_sk = _read_gamedata_json(sk_file)
        if vs_by_pg and raw_sk is not None:
            sk_iter = raw_sk.values() if isinstance(raw_sk, dict) else (raw_sk if isinstance(raw_sk, list) else [])
            sk_map: Dict[int, dict] = {}
            for s in sk_iter:
                if not isinstance(s, dict):
                    continue
                try:
                    sid = int(s["Id"])
                except (KeyError, TypeError, ValueError):
                    continue
                sk_map[sid] = s

            handled_ml_groups: set[Tuple[int, int]] = set()
            for d in excel_dirs:
                is_global_jp_fallback = lang_up == "JP" and (d == eff_dir)
                by_cid_grp: Dict[Tuple[int, int], List[dict]] = defaultdict(list)
                for r in cd_rows_by_dir[d]:
                    if r.get("teen_mode") is True:
                        continue
                    try:
                        cat = int(r.get("dialog_category") if r.get("dialog_category") is not None else -1)
                        if cat != 8:
                            continue
                        if int(r.get("dialog_type") or 0) == 2:
                            continue  # drops unvoiced camera UI prompts (e.g. ch0173 grp 3 & 6)
                        cid = int(r.get("character_id") or 0)
                        grp_id = int(r.get("group_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if grp_id == 0:
                        continue
                    by_cid_grp[(cid, grp_id)].append(r)

                for (cid, grp), rows in by_cid_grp.items():
                    if (cid, grp) in handled_ml_groups:
                        continue
                    handled_ml_groups.add((cid, grp))
                    s = sk_map.get(cid)
                    if not isinstance(s, dict):
                        continue
                    rows.sort(key=lambda x: int(x.get("display_order") or 0))

                    # Build candidate stem prefixes + aliases
                    dev = str(s.get("DevName") or "").strip()
                    if dev.lower().endswith("_default"):
                        dev = dev[:-8]
                    path_nm = str(s.get("PathName") or "").strip()
                    raw_cands = [dev, path_nm, path_nm.replace("_", "")]
                    pref_cands: List[str] = []
                    for c in raw_cands:
                        cl = c.strip().lower()
                        if cl and cl not in pref_cands:
                            pref_cands.append(cl)
                        alias = _STUDENT_PREFIX_ALIASES.get(cl)
                        if alias and alias not in pref_cands:
                            pref_cands.append(alias)

                    oggs: Optional[List[Tuple[int, str]]] = None
                    for pref in pref_cands:
                        hit = vs_by_pg.get((pref, grp))
                        if hit:
                            oggs = hit
                            break
                    if not oggs:
                        continue

                    subs = [sub_i for sub_i, _ in oggs]

                    # Global-only JP shift guard (protects against Nexon Global Utaha/Kotori/Haruka/Yoshimi shifts
                    # when sibling `gamedata_jp` is absent)
                    def _is_global_jp_shifted(row: dict) -> bool:
                        if not is_global_jp_fallback:
                            return False
                        jp_clean = _clean_official_transcript(str(row.get("localize_jp") or ""), "JP")
                        kr_clean = _clean_official_transcript(str(row.get("localize_kr") or ""), "KR")
                        jp_n = _count_alnum_speech_chars(jp_clean)
                        kr_n = _count_alnum_speech_chars(kr_clean)
                        hi = max(jp_n, kr_n)
                        lo = min(jp_n, kr_n)
                        return hi >= 12 and (hi / float(max(1, lo))) >= 2.8

                    # Case A: Single OGG (subs in ([0], [1])) -> ALWAYS alignable, join all rows
                    if len(oggs) == 1 and subs in ([0], [1]):
                        if any(_is_global_jp_shifted(r) for r in rows):
                            continue
                        raws = [str(r.get(loc_key) or "") for r in rows]
                        if any(("[USERNAME]" in t or "[NICKNAME]" in t) for t in raws):
                            continue
                        cleaned = _clean_official_transcript("\n".join(t for t in raws if t.strip()), lang_up)
                        if cleaned:
                            stem_to_text.setdefault(oggs[0][1], cleaned)
                    # Case B: Exact N:N with contiguous 1..N sub-indices -> alignable with speaking-rate guard
                    elif len(oggs) >= 2 and len(oggs) == len(rows) and subs == list(range(1, len(oggs) + 1)):
                        if any(_is_global_jp_shifted(r) for r in rows):
                            continue
                        for row, (_sub_i, st) in zip(rows, oggs):
                            cleaned = _clean_official_transcript(str(row.get(loc_key) or ""), lang_up)
                            if cleaned:
                                if st not in stem_to_text:
                                    stem_to_text[st] = cleaned
                                    ml_multi_stems.add(st)
                    # Case C: Multi-OGG mismatch or non-1..N subs -> STRICTLY EXCLUDED (0 poisoned transcripts!)
                    else:
                        continue
    except Exception:
        pass

    result = (stem_to_text, frozenset(ml_multi_stems), gd_sig)
    with _TRANSCRIPT_CACHE_LOCK:
        _TRANSCRIPT_CACHE[cache_key] = result
    return result


def load_optional_official_transcripts(lang: str = "JP", gamedata_dir: Optional[Path] = None) -> Dict[str, str]:
    """
    Optionally load lowercase audio stem -> verbatim official script from local Blue Archive gamedata.
    Auto-detects `BA_GAMEDATA_DIR`, `%LOCALAPPDATA%\\VoiceCompanionData\\gamedata`, `config/gamedata`,
    and local dev gamedata (plus sibling `gamedata_jp` for JP). Returns `{}` cleanly when absent.
    """
    stem_to_text, _, _ = _load_official_transcript_bundle(lang=lang, gamedata_dir=gamedata_dir)
    return stem_to_text


def compute_clip_filter_hash(char_data: dict) -> str:
    """Return a short deterministic hash of `voice_clip_filter` so filter changes invalidate stale extracts."""
    flt = char_data.get("voice_clip_filter")
    if not isinstance(flt, dict) or not flt:
        return ""
    canonical = json.dumps(flt, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]


def compute_extraction_config_hash(char_data: dict, lang: str = "JP") -> str:
    """
    Deterministic hash of all character extraction inputs (`voice_clip_filter`, `archetype`,
    `local_voice_packs[lang]`, `molru_prefix`) so any config change invalidates stale extracts cleanly.
    """
    lang_low = str(lang or "JP").strip().lower()
    flt = char_data.get("voice_clip_filter") if isinstance(char_data.get("voice_clip_filter"), dict) else {}
    archetype = str(
        (((char_data.get("persona") or {}).get("voice_acting_profile")) or {}).get(
            "archetype", "gentle_earnest_passion"
        )
    )
    lvp = char_data.get("local_voice_packs") if isinstance(char_data.get("local_voice_packs"), dict) else {}
    lang_packs = sorted(str(p).strip() for p in (lvp.get(lang_low) or []) if str(p).strip())
    molru_prefix = str(char_data.get("molru_prefix") or char_data.get("id") or "").strip().lower()
    payload = json.dumps(
        {
            "filter": flt,
            "archetype": archetype,
            "packs": lang_packs,
            "molru_prefix": molru_prefix,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


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
_MOLRU_AVAIL_CACHE_LOCK = threading.RLock()


def clear_steam_voice_availability_cache() -> None:
    """Clear cached Steam .molru availability checks (e.g. after user picks a new Steam directory)."""
    with _MOLRU_AVAIL_CACHE_LOCK:
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
        "" if strict_explicit else molru_prefix,
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
    with _MOLRU_AVAIL_CACHE_LOCK:
        hit = _MOLRU_AVAIL_CACHE.get(cache_key)
    if hit is not None and (now - hit[0]) < 60.0:
        return hit[1]
    has_pack = bool(find_character_voice_molru_files(char_data, lang=lang_up, game_dir=eff_game_dir))
    with _MOLRU_AVAIL_CACHE_LOCK:
        _MOLRU_AVAIL_CACHE[cache_key] = (now, has_pack)
    return has_pack


def _passes_ml_speaking_rate_guard(
    transcript: str,
    speech_span_sec: float,
    lang_up: str,
) -> bool:
    """
    Enforce speaking-rate sanity on N:N multi-OGG MemorialLobby pairs so rare intra-pair timing shifts
    (e.g. 19 chars on a 1.7s clip) never pollute `transcripts.json`.
    """
    alnum = _count_alnum_speech_chars(transcript)
    span = max(0.35, float(speech_span_sec))
    cps = alnum / span
    if lang_up == "KR":
        return 1.15 <= cps <= 9.5
    return 1.35 <= cps <= 10.5


def _commit_staged_samples_dir(staging_dir: Path, samples_dir: Path) -> None:
    """
    Atomically replace `samples_dir` with `staging_dir` on Windows/POSIX.
    Falls back to strict per-file atomic replacement if `samples_dir` itself is open in another process,
    refusing to leave stale WAVs behind and writing `studio_v2_meta.json` LAST as the commit marker.
    """
    old_backup = samples_dir.parent / f".fish_samples_old_{os.getpid()}_{time.time_ns()}"
    swapped = False
    moved_old = False
    try:
        if samples_dir.exists():
            if samples_dir.is_symlink():
                raise ValueError(f"Refusing to overwrite symlink directory: {samples_dir}")
            try:
                os.replace(samples_dir, old_backup)
                moved_old = True
            except OSError:
                moved_old = False

        if not samples_dir.exists():
            os.replace(staging_dir, samples_dir)
            swapped = True
            if moved_old and old_backup.exists():
                shutil.rmtree(old_backup, ignore_errors=True)
            return
    except Exception:
        if moved_old and not swapped and old_backup.exists() and not samples_dir.exists():
            try:
                os.replace(old_backup, samples_dir)
            except OSError:
                pass
        raise

    # Fallback path when `samples_dir` directory handle is held open: replace contents strictly in order
    samples_dir.mkdir(parents=True, exist_ok=True)
    meta_dst = samples_dir / "studio_v2_meta.json"
    _unlink_with_retries(meta_dst)
    for old_wav in list(samples_dir.glob("*.wav")):
        _unlink_with_retries(old_wav)
    old_tr = samples_dir / "transcripts.json"
    if not (staging_dir / "transcripts.json").exists():
        _unlink_with_retries(old_tr)

    staged_files = sorted(staging_dir.iterdir(), key=lambda p: (p.name == "studio_v2_meta.json", p.name))
    for src_f in staged_files:
        dst_f = _ensure_path_within(samples_dir, samples_dir / src_f.name)
        _atomic_replace_path(src_f, dst_f)
    shutil.rmtree(staging_dir, ignore_errors=True)


def build_character_fish_reference(
    char_data: dict,
    lang: str = "JP",
    game_dir: Optional[Path] = None,
    quality_profile: str = DEFAULT_QUALITY_PROFILE,
    gamedata_dir: Optional[Path] = None,
) -> dict:
    """
    Build clean 44.1kHz mono PCM_16 Fish Audio S2/S2.1 reference WAVs for ANY Blue Archive character.
    Reads native 44.1kHz OGG streams directly from Steam `.molru` packs FIRST (avoiding 40kHz RVC ZIP
    resampling), falling back to an RVC dataset ZIP only when Steam `.molru` packs are unavailable.
    Supports `quality_profile="pro85"` (<=85.0s incl. pauses, up to 14 clips, 100% verified transcript lock)
    and `quality_profile="standard45"` (<=48.0s incl. pauses, up to 8 clips).
    """
    game_dir = resolve_ba_game_dir(game_dir)
    prof_key, prof_cfg = resolve_quality_profile(quality_profile)
    target_dur_sec = float(prof_cfg["target_dur_sec"])
    max_dur_sec = float(prof_cfg["max_dur_sec"])
    early_stop_dur_sec = float(prof_cfg.get("early_stop_dur_sec", target_dur_sec * 0.5))
    min_clips = int(prof_cfg["min_clips"])
    target_clips = int(prof_cfg["target_clips"])
    max_clips = int(prof_cfg["max_clips"])
    min_matched_dur_sec = float(prof_cfg["min_matched_dur_sec"])

    char_id = str(char_data.get("id") or "Aru").strip()
    if not is_safe_entity_id(char_id):
        raise ValueError(f"Invalid or unsafe character_id: {char_id!r}")
    archetype = str(
        (((char_data.get("persona") or {}).get("voice_acting_profile")) or {}).get(
            "archetype", "gentle_earnest_passion"
        )
    )
    lang_up = str(lang or "JP").strip().upper()
    if lang_up not in ("JP", "KR"):
        raise ValueError(f"Invalid voice language code: {lang!r}")

    user_data_root = get_user_data_dir()
    user_dataset_zip = _ensure_path_within(
        user_data_root, user_data_root / "data" / "characters" / char_id / lang_up / f"{char_id}_{lang_up}_rvc_dataset.zip"
    )
    legacy_data_root = get_app_root() / "data"
    legacy_dataset_zip = _ensure_path_within(
        legacy_data_root, legacy_data_root / "characters" / char_id / lang_up / f"{char_id}_{lang_up}_rvc_dataset.zip"
    )
    dataset_zip = user_dataset_zip if user_dataset_zip.exists() else legacy_dataset_zip

    models_root = get_user_models_dir()
    out_dir = _ensure_path_within(models_root, models_root / char_id / lang_up)
    _assert_not_inside_game_dir(out_dir, game_dir)
    samples_dir = _ensure_path_within(out_dir, out_dir / "fish_samples")
    combined_wav = _ensure_path_within(out_dir, out_dir / f"{char_id}_{lang_up}_FishAudio_Reference.wav")

    # Tuple layout: (score, safe_fname, raw_stem_low, y_padded, sr, dur_padded, verified_transcript)
    candidates: List[Tuple[float, str, str, np.ndarray, int, float, str]] = []
    relaxed_candidates: List[Tuple[float, str, str, np.ndarray, int, float, str]] = []

    clip_filter = char_data.get("voice_clip_filter") if isinstance(char_data.get("voice_clip_filter"), dict) else {}
    clip_filter_hash = compute_clip_filter_hash(char_data)
    extraction_config_hash = compute_extraction_config_hash(char_data, lang=lang_up)
    relax_dur_min = float(clip_filter.get("relax_duration_min", 2.0) or 2.0)

    transcript_lookup, ml_multi_stems, gamedata_sig = _load_official_transcript_bundle(
        lang=lang_up, gamedata_dir=gamedata_dir
    )

    def _ingest_raw_audio(
        raw_stem: str,
        safe_fname: str,
        y_raw: np.ndarray,
        sr: int,
        explicit_inc: bool,
    ) -> None:
        if sr <= 0 or y_raw.size == 0:
            return
        if y_raw.ndim > 1:
            y_raw = np.mean(y_raw, axis=1)
        if not np.all(np.isfinite(y_raw)):
            y_raw = np.nan_to_num(y_raw, nan=0.0, posinf=0.0, neginf=0.0)

        y_pad, speech_span_sec, voiced_ratio = _trim_and_pad_speech_with_metrics(y_raw, sr)
        if y_pad.size == 0:
            return

        dur = len(y_pad) / float(sr)
        peak = float(np.max(np.abs(y_pad)))
        rms = float(np.sqrt(np.mean(y_pad**2)))
        raw_stem_low = raw_stem.lower()

        tr_txt = transcript_lookup.get(raw_stem_low, "")
        ml_rate_rejected = False
        if tr_txt and raw_stem_low in ml_multi_stems:
            if not _passes_ml_speaking_rate_guard(tr_txt, speech_span_sec, lang_up):
                tr_txt = ""
                ml_rate_rejected = True

        sc = score_clip(
            raw_stem,
            dur,
            peak,
            rms,
            archetype=archetype,
            y=y_pad,
            sr=sr,
            transcript=tr_txt,
            lang=lang_up,
            voiced_ratio=voiced_ratio,
        )
        if explicit_inc and sc <= -150.0 and relax_dur_min <= dur <= 13.5 and rms >= 0.012:
            if float(np.mean(np.abs(y_pad) >= 0.99)) <= 0.008 and not ml_rate_rejected:
                sc = 25.0 + min(dur * 3.0, 24.0) + min(rms * 80.0, 12.0)

        if sc > -150.0 and not ml_rate_rejected:
            if tr_txt:
                sc += 6.0
            candidates.append((sc, safe_fname, raw_stem_low, y_pad, sr, dur, tr_txt))
        elif (
            1.4 <= dur <= 14.5
            and rms >= 0.008
            and voiced_ratio >= 0.35
            and not ml_rate_rejected
            and not any(bad in raw_stem_low for bad in _HARD_SCREAM_KEYWORDS)
            and float(np.mean(np.abs(y_pad) >= 0.99)) <= 0.008
        ):
            scream_pen = -15.0 if (tr_txt and is_scream_or_offtone_transcript(tr_txt, lang=lang_up)) else 0.0
            rel_sc = min(dur, 9.0) + min(rms * 50.0, 10.0) + (6.0 if tr_txt else 0.0) + scream_pen
            relaxed_candidates.append((rel_sc, safe_fname, raw_stem_low, y_pad, sr, dur, tr_txt))

    # 1. Primary source: Native 44.1kHz Steam `.molru` packs (zero resampling, exact catalog stems)
    molru_files = find_character_voice_molru_files(char_data, lang=lang_up, game_dir=game_dir)
    if molru_files:
        user_catalog = get_cache_dir() / "catalog_cache.json"
        catalog = load_or_build_catalog(game_dir, user_catalog)
        game_dir_res = game_dir.resolve()
        for molru_path in molru_files:
            if molru_path.is_symlink() or not molru_path.resolve().is_relative_to(game_dir_res):
                continue
            try:
                if molru_path.stat().st_size > MAX_MOLRU_FILE_BYTES:
                    continue
                rel_from_game = molru_path.relative_to(game_dir).as_posix()
                mxmc_key = rel_from_game[:-6] if rel_from_game.endswith(".molru") else rel_from_game
                name_queue = lookup_catalog_entries(catalog, mxmc_key)
                streams = extract_ogg_streams_from_molru(
                    molru_path.read_bytes(), max_stream_bytes=MAX_OGG_STREAM_BYTES
                )
            except (OSError, ValueError):
                continue

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
                safe_fname = f"{sanitize_filename_stem(raw_stem, fallback=fallback_stem)}.wav"
                try:
                    y, sr = sf.read(io.BytesIO(ogg_bytes), dtype="float32")
                except Exception:
                    continue
                _ingest_raw_audio(raw_stem, safe_fname, y, sr, explicit_inc)

    # 2. Fallback source: RVC dataset ZIP (only when Steam `.molru` packs aren't available or yielded 0 clips)
    if not candidates and not relaxed_candidates and dataset_zip.exists() and not dataset_zip.is_symlink():
        try:
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
                    safe_fname = f"{sanitize_filename_stem(raw_stem)}.wav"
                    _ingest_raw_audio(raw_stem, safe_fname, y, sr, explicit_inc)
        except (zipfile.BadZipFile, OSError, EOFError):
            candidates.clear()
            relaxed_candidates.clear()

    if not candidates and not relaxed_candidates:
        if not molru_files and not dataset_zip.exists():
            raise FileNotFoundError(
                f"스팀 클라이언트에서 '{char_data.get('name_kr', char_id)}' ({lang_up}) 음성 팩(.molru)을 찾을 수 없습니다. (미실장 또는 보이스 없는 NPC일 수 있습니다.)"
            )
        raise RuntimeError(f"'{char_data.get('name_kr', char_id)}' 음성 팩에서 적합한 대화형 보이스 클립을 찾지 못했습니다.")

    if not candidates and relaxed_candidates:
        candidates = relaxed_candidates

    candidates.sort(key=lambda x: x[0], reverse=True)

    # Separate verbatim-transcript-matched candidates & deduplicate identical spoken lines for max phonetic diversity
    matched_candidates: List[Tuple[float, str, str, np.ndarray, int, float, str]] = []
    unmatched_candidates: List[Tuple[float, str, str, np.ndarray, int, float, str]] = []
    seen_texts: set[str] = set()
    for cand in candidates:
        tr_txt = cand[6]
        if tr_txt:
            norm_tr = re.sub(r"[\s.,!?！？~～…「」『』()（）]+", "", tr_txt).lower()
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

    # Quality-gated duration budget strictly counting `(N - 1) * INTER_CLIP_PAUSE_SEC` inside `max_dur_sec`
    is_expressive = archetype not in CALM_ARCHETYPES
    expressive_ml_cap = 2
    selected: List[Tuple[str, str, np.ndarray, float, float, str]] = []
    deferred_ml: List[Tuple[float, str, str, np.ndarray, int, float, str]] = []
    total_combined_dur = 0.0
    total_speech_dur = 0.0
    ml_selected_count = 0

    def _try_add_candidate(
        cand: Tuple[float, str, str, np.ndarray, int, float, str],
        allow_early_stop: bool = True,
    ) -> str:
        """Returns `'added'`, `'skip'`, or `'stop'`."""
        nonlocal total_combined_dur, total_speech_dur, ml_selected_count
        sc, fname, raw_stem_low, y_pad, sr, _est_dur, tr_txt = cand
        if len(selected) >= max_clips:
            return "stop"
        if allow_early_stop and total_combined_dur >= early_stop_dur_sec and len(selected) >= min_clips and sc < 20.0:
            return "stop"
        y_44k = master_studio_clip(y_pad, sr, TARGET_SR)
        if len(y_44k) == 0:
            return "skip"
        clip_dur = len(y_44k) / float(TARGET_SR)
        added_pause = INTER_CLIP_PAUSE_SEC if selected else 0.0
        projected = total_combined_dur + added_pause + clip_dur
        if projected > max_dur_sec and selected:
            return "skip"
        selected.append((fname, raw_stem_low, y_44k, clip_dur, sc, tr_txt))
        total_combined_dur = projected
        total_speech_dur += clip_dur
        if classify_clip_category(raw_stem_low) == "memoriallobby":
            ml_selected_count += 1
        if total_combined_dur >= target_dur_sec and len(selected) >= target_clips:
            return "stop"
        return "added"

    for cand in pool:
        if len(selected) >= max_clips:
            break
        raw_stem_low = cand[2]
        if is_expressive and classify_clip_category(raw_stem_low) == "memoriallobby" and ml_selected_count >= expressive_ml_cap:
            deferred_ml.append(cand)
            continue
        action = _try_add_candidate(cand, allow_early_stop=True)
        if action == "stop":
            break

    # Only pull deferred MemorialLobby clips if non-ML clips weren't enough to hit minimum clean coverage
    if deferred_ml and (total_combined_dur < early_stop_dur_sec or len(selected) < min_clips):
        for cand in deferred_ml:
            if len(selected) >= max_clips:
                break
            if total_combined_dur >= early_stop_dur_sec and len(selected) >= min_clips:
                break
            action = _try_add_candidate(cand, allow_early_stop=False)
            if action == "stop":
                break

    if not selected and pool:
        _try_add_candidate(pool[0], allow_early_stop=False)

    if not selected:
        raise RuntimeError(f"'{char_data.get('name_kr', char_id)}' 음성 클립 마스터링 결과가 비어 있습니다.")

    pause = np.zeros(int(round(TARGET_SR * INTER_CLIP_PAUSE_SEC)), dtype=np.float32)
    stitched_parts: List[np.ndarray] = []
    for idx, (_fname, _raw_stem_low, y_44k, _dur, _sc, _tr) in enumerate(selected, 1):
        stitched_parts.append(y_44k)
        if idx < len(selected):
            stitched_parts.append(pause)
    combined = np.concatenate(stitched_parts)

    out_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=".fish_stage_", dir=str(out_dir)))
    saved_sample_names: List[str] = []
    transcripts_map: Dict[str, str] = {}
    transcript_matched = 0

    try:
        for idx, (fname, _raw_stem_low, y_44k, _dur, _sc, tr_text) in enumerate(selected, 1):
            out_name = sanitize_extract_filename(f"{idx:02d}_{fname}", default_ext=".wav", fallback_stem=f"{idx:02d}_clip")
            staged_sample = _ensure_path_within(staging_dir, staging_dir / out_name)
            sf.write(staged_sample, y_44k, TARGET_SR, subtype="PCM_16")
            saved_sample_names.append(out_name)
            if tr_text:
                transcripts_map[out_name] = tr_text
                transcripts_map[staged_sample.stem] = tr_text
                transcript_matched += 1

        if transcripts_map:
            _atomic_write_text(
                staging_dir / "transcripts.json",
                json.dumps(transcripts_map, ensure_ascii=False, indent=2),
            )

        total_dur_rounded = round(len(combined) / float(TARGET_SR), 1)
        speech_dur_rounded = round(total_speech_dur, 1)
        tr_cov = round(transcript_matched / max(1, len(saved_sample_names)), 3)
        tr_locked = bool(saved_sample_names and transcript_matched == len(saved_sample_names))

        meta_payload = {
            "pipeline_version": STUDIO_PIPELINE_VERSION,
            "quality_profile": prof_key,
            "clip_filter_hash": clip_filter_hash,
            "extraction_config_hash": extraction_config_hash,
            "gamedata_sig": gamedata_sig,
            "gamedata_available": bool(transcript_lookup),
            "max_upload_clips": max_clips,
            "target_sr": TARGET_SR,
            "rms_dbfs": TARGET_RMS_DBFS,
            "peak_ceiling_dbfs": PEAK_CEILING_DBFS,
            "soft_limiter_knee_dbfs": SOFT_LIMITER_KNEE_DBFS,
            "hpf_hz": HIGHPASS_HZ,
            "lead_pad_ms": LEAD_PAD_MS,
            "tail_pad_ms": TAIL_PAD_MS,
            "edge_fade_ms": EDGE_FADE_MS,
            "inter_clip_pause_sec": INTER_CLIP_PAUSE_SEC,
            "clip_count": len(saved_sample_names),
            "memoriallobby_clips": ml_selected_count,
            "speech_duration_sec": speech_dur_rounded,
            "total_duration_sec": total_dur_rounded,
            "transcript_matched": transcript_matched,
            "transcript_coverage": tr_cov,
            "transcript_locked": tr_locked,
        }
        _atomic_write_text(
            staging_dir / "studio_v2_meta.json",
            json.dumps(meta_payload, ensure_ascii=False, indent=2),
        )

        # Atomic commit: combined WAV first, then staged fish_samples/ (with studio_v2_meta.json commit marker)
        _atomic_write_wav(combined_wav, combined, TARGET_SR, subtype="PCM_16")
        _commit_staged_samples_dir(staging_dir, samples_dir)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    saved_samples = [_ensure_path_within(samples_dir, samples_dir / nm) for nm in saved_sample_names]
    transcripts_json = samples_dir / "transcripts.json"

    return {
        "character_id": char_id,
        "lang": lang_up,
        "pipeline_version": STUDIO_PIPELINE_VERSION,
        "quality_profile": prof_key,
        "clip_count": len(saved_samples),
        "memoriallobby_clips": ml_selected_count,
        "speech_duration_sec": speech_dur_rounded,
        "total_duration_sec": total_dur_rounded,
        "combined_wav": combined_wav,
        "samples_dir": samples_dir,
        "sample_files": saved_samples,
        "transcripts_json": transcripts_json if transcripts_map else None,
        "transcript_matched": transcript_matched,
        "transcript_coverage": tr_cov,
        "transcript_locked": tr_locked,
        "gamedata_available": bool(transcript_lookup),
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
        help="Quality profile: plus200 (<=200s, up to 30 clips, recommended for Plus 210s), pro85 (<=85s, up to 14 clips), or standard45 (<=48s, up to 8 clips)",
    )
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR, help="Path to Steam BlueArchive directory")
    args = parser.parse_args()

    if not is_safe_entity_id(args.character):
        raise ValueError(f"Invalid character identifier: {args.character!r}")
    chars_dir = get_app_root() / "config" / "characters"
    cfg_file = _ensure_path_within(chars_dir, chars_dir / f"{args.character.lower()}.json")
    if cfg_file.exists():
        char_data = json.loads(cfg_file.read_text(encoding="utf-8"))
    else:
        char_data = {"id": args.character, "molru_prefix": args.character}

    res = build_character_fish_reference(char_data, lang=args.lang, game_dir=args.game_dir, quality_profile=args.profile)
    print(
        f"[OK] Saved {res['clip_count']} studio clips ({res['total_duration_sec']}s · profile={res['quality_profile']} · "
        f"v{res['pipeline_version']} · transcripts={res['transcript_matched']}/{res['clip_count']}) -> {res['combined_wav']}"
    )


if __name__ == "__main__":
    main()
