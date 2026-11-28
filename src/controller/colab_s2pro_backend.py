"""
Dedicated Google Colab Fish Audio S2-Pro Server Client (Option B)
-----------------------------------------------------------------
Connects the local MomoTalk voice companion directly to a user-owned
open-source Fish Audio S2-Pro streaming server running on Google Colab
(A100 / L4 / H100 / T4) via Cloudflare Quick Tunnel (or localhost SSH tunnel).

Key capabilities:
  - Free / unlimited / zero-congestion genuine Fish Audio S2-Pro 44.1kHz TTS
    with native inline emotion tags (`[laughing]`, `[whisper]`, etc.).
  - Automatic studio reference voice registration for all 196 Blue Archive
    characters (JP & KR) using locally extracted 100%-transcript-locked
    Steam studio WAVs (`%LOCALAPPDATA%\\VoiceCompanionData\\models\\<ID>\\<LANG>\\fish_samples`),
    defaulting to the 38-48s extreme-quality multi-clip reference sweet spot (zero latency
    penalty with RadixAttention/KV prefix caching, maximum tonal & emotional fidelity).
  - Persistent HTTP/2 keep-alive connection pool + real-time streaming WAV
    header parser yielding raw PCM16 chunks immediately on arrival.
  - Strict SSRF-safe URL validation & zero-leak Bearer token redaction.
  - Optional endpoint auto-discovery from an explicit user-configured JSON file or env var.
"""

from __future__ import annotations

import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlparse

import httpx
import numpy as np
import soundfile as sf

from src.utils.app_paths import (
    get_app_root,
    get_user_models_dir,
    redact_secrets,
)


_SAFE_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")
_FORBIDDEN_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home.arpa")

_env_nb_id = os.environ.get("BA_COLAB_S2PRO_NOTEBOOK_ID", "").strip()
COLAB_S2PRO_NOTEBOOK_ID = _env_nb_id if _SAFE_DRIVE_ID_RE.match(_env_nb_id) else ""
COLAB_S2PRO_NOTEBOOK_URL = (
    f"https://colab.research.google.com/drive/{COLAB_S2PRO_NOTEBOOK_ID}"
    if COLAB_S2PRO_NOTEBOOK_ID
    else "https://colab.research.google.com/"
)
_env_folder_id = os.environ.get("BA_COLAB_S2PRO_DRIVE_FOLDER_ID", "").strip()
COLAB_S2PRO_DRIVE_FOLDER_ID = _env_folder_id if _SAFE_DRIVE_ID_RE.match(_env_folder_id) else ""


def build_colab_notebook_url(notebook_id: str | None = None) -> str:
    """Resolve the Colab notebook URL from an explicit user-config ID, env var, or generic Colab home."""
    raw = str(
        notebook_id
        if (notebook_id is not None and str(notebook_id).strip())
        else (os.environ.get("BA_COLAB_S2PRO_NOTEBOOK_ID", "").strip() or COLAB_S2PRO_NOTEBOOK_ID)
    ).strip()
    if raw and _SAFE_DRIVE_ID_RE.match(raw):
        return f"https://colab.research.google.com/drive/{raw}"
    return "https://colab.research.google.com/"


def validate_colab_notebook_url(raw_url: str) -> str:
    """
    Strictly validate that a notebook launch URL targets `https://colab.research.google.com/`
    (or `/drive/<SAFE_DRIVE_ID>`), blocking schemes, credentials, queries, fragments, and spoofing.
    """
    s = str(raw_url or "").strip()
    if not s or "\\" in s or any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        raise ValueError(f"허용되지 않는 Colab 노트북 주소입니다: {raw_url!r}")
    parsed = urlparse(s)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if (
        scheme != "https"
        or host != "colab.research.google.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Colab 공식 HTTPS 주소만 열 수 있습니다: {raw_url!r}")
    path = parsed.path or "/"
    if path == "/":
        return "https://colab.research.google.com/"
    if path.startswith("/drive/"):
        nb_id = path[len("/drive/") :].strip("/")
        if nb_id and "/" not in nb_id and _SAFE_DRIVE_ID_RE.match(nb_id):
            return f"https://colab.research.google.com/drive/{nb_id}"
    raise ValueError(f"허용되지 않는 Colab 노트북 경로입니다: {raw_url!r}")


_BUNDLE_URL_RE = re.compile(r"https?://[^\s'\"<>\]})]+", re.IGNORECASE)
_BUNDLE_KEY_KV_RE = re.compile(
    r"(?i)(?:인증\s*토큰(?:\s*\(Key\))?|api[_\-\s]?key|bearer|token|key)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{8,256})['\"]?"
)
_BUNDLE_BA_KEY_RE = re.compile(r"\b(ba_s2pro_[A-Za-z0-9_\-]{8,160})\b")


def parse_colab_connection_bundle(raw_text: str) -> tuple[str, str]:
    """
    Parse a 1-click Colab S2-Pro connection bundle from clipboard or CLI.
    Accepts:
      1. 1-line JSON bundle: `{"url":"https://xxxx.trycloudflare.com","api_key":"ba_s2pro_..."}`
         (also tolerated when embedded inside surrounding console text).
      2. Colab console lines containing URL + key (`서버 주소 (URL) : ...` / `인증 토큰 (Key) : ...`).
      3. A bare server URL (`https://xxxx.trycloudflare.com` or `http://127.0.0.1:8080`).

    Returns `(normalized_url, api_key)` (with `api_key=""` if omitted). Raises `ValueError` if invalid.
    """
    txt = str(raw_text or "").strip()
    if not txt:
        raise ValueError("클립보드에 붙여넣을 서버 연결 정보가 비어 있습니다.")

    url_cand = ""
    key_cand = ""

    # 1) Try JSON object directly, per-line `{...}`, or embedded `{...}` slice
    json_candidates: list[str] = []
    if txt.startswith("{") and txt.endswith("}"):
        json_candidates.append(txt)
    for line in txt.splitlines():
        sline = line.strip()
        lb = sline.find("{")
        rb = sline.rfind("}")
        if 0 <= lb < rb:
            cand_line = sline[lb : rb + 1]
            if cand_line not in json_candidates:
                json_candidates.append(cand_line)
    first_brace = txt.find("{")
    last_brace = txt.rfind("}")
    if 0 <= first_brace < last_brace:
        span = txt[first_brace : last_brace + 1]
        if span not in json_candidates:
            json_candidates.append(span)

    for cand in json_candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("url"):
            url_cand = str(obj.get("url") or "").strip()
            for kfield in ("api_key", "key", "token", "bearer_token"):
                if obj.get(kfield):
                    key_cand = str(obj.get(kfield) or "").strip()
                    break
            break

    # 2) Fallback: parse URL + Key from plain console text / multi-line banner
    if not url_cand:
        m_url = _BUNDLE_URL_RE.search(txt)
        if m_url:
            url_cand = m_url.group(0).rstrip(".,;)")
    if not key_cand:
        m_ba = _BUNDLE_BA_KEY_RE.search(txt)
        if m_ba:
            key_cand = m_ba.group(1).strip()
        else:
            m_kv = _BUNDLE_KEY_KV_RE.search(txt)
            if m_kv:
                key_cand = m_kv.group(1).strip()

    if not url_cand:
        raise ValueError(
            "유효한 S2-Pro 서버 주소(https://... 또는 로컬 터널 주소)를 찾지 못했습니다."
        )

    clean_url = validate_colab_server_url(url_cand, resolve_dns=False)
    clean_key = "".join(ch for ch in str(key_cand or "").strip() if ord(ch) >= 32 and ord(ch) != 127).strip()
    return clean_url, clean_key


def resolve_s2pro_streaming_params(speed_code: str, text_len: int = 0) -> tuple[int, int]:
    """
    Map MomoTalk streaming speed mode (`ultra` / `expressive` / `full_turn`) and utterance length
    to optimal Fish-Speech S2-Pro `(chunk_length, max_new_tokens)` parameters.
    Tighter `max_new_tokens` for short clauses (<=24 chars) guards against runaway EOS misses
    and minimizes SGLang KV-cache admission overhead without truncating expressive delivery.
    """
    code = str(speed_code or "expressive").strip().lower()
    n_chars = max(0, int(text_len or 0))
    if code == "ultra":
        if 0 < n_chars <= 24:
            return 120, 640
        return 120, (1280 if n_chars > 90 else 1024)
    if code == "full_turn":
        return 300, (2048 if n_chars > 60 else 1536)
    # "expressive" / "balanced" / default
    if 0 < n_chars <= 24:
        return 200, 640
    return 200, (1536 if n_chars > 90 else 1280)


_S2PRO_ANY_BRACKET_TAG_RE = re.compile(r"\[[^\]]{1,512}\]")
_S2PRO_JP_LONG_SCREAM_RE = re.compile(r"([あいうえおぁぃぅぇぉアイウエオァィゥェォー])\1{2,}")
_S2PRO_KO_LONG_SCREAM_RE = re.compile(r"([아어오우으악꺄꺅헉허])\1{2,}")
_S2PRO_EXCESS_PUNCT_RE = re.compile(r"([！？!?])\1{2,}")
_S2PRO_WRITTEN_SIGH_PREFIX_RE = re.compile(r"^(?:[…・\s])*(?:はあ|はぁ|ふぅ|ふわ|하아|후우|휴우|에휴)")
_S2PRO_WRITTEN_LAUGH_PREFIX_RE = re.compile(
    r"^(?:[…・\s])*(?:ふふ|フフ|あは|アハ|うは|ウハ|くす|うふふ|えへ|にひ|きひ|후훗|후후|아하|우하|하하|히히|헤헤|크크|키키)"
)

_S2PRO_SOFT_CUES = frozenset({
    "humid_pure_love",
    "humid_soft_baseline",
    "lover_coach_warm",
    "shy_soft",
    "ko_shy_soft",
    "char_lv3_whisper",
})
_S2PRO_SIGH_CUES = frozenset({"sigh_tired", "ko_sigh"})
_S2PRO_LAUGH_CUES = frozenset({"laugh_tease", "ko_laugh"})
_S2PRO_HIGH_ENERGY_CUES = frozenset({
    "fluster_panic",
    "ko_fluster",
    "excited_passion",
    "ko_excited",
    "scold_angry",
    "ko_scold",
    "serious_spike",
    "fanfare_spike",
    "meme_spicy_spike",
    "character_spike",
    "char_lv3_spike",
})


def sanitize_s2pro_open_weights_directing(
    tts_text: str,
    *,
    cue: str = "",
    auto_direct: bool = True,
    temperature: float = 0.72,
    top_p: float = 0.78,
    repetition_penalty: float = 1.12,
    forbidden_tags: set[str] | frozenset[str] | list[str] | None = None,
) -> tuple[str, str, float, float, float]:
    """
    Sanitize Fish Audio S2 directing text and sampling parameters for open-source
    `fishaudio/s2-pro` weights (Colab SGLang-Omni / fish-speech).

    Open-source S2-Pro checkpoints can misinterpret long Japanese prose tags
    (e.g. `[うわあああっと声が裏返るほど大慌てで]`) or high-intensity tags (`[excited]`, `[screaming]`)
    at `temperature >= 0.85` as harsh cracked shrieks before the sentence begins.
    This helper:
      1. Strips all prose/shriek bracket tags from TTS input text.
      2. Collapses excessive 3+ repeated scream vowels (`うわあああっ` -> `うわあっ`) in TTS text only.
      3. Emits ONLY listening-verified gentle English tags (`[soft tone]`, `[sigh]`, `[laughing]`)
         when genuinely appropriate and not already written into the opening text.
      4. Clamps `(temperature, top_p, repetition_penalty)` to the distortion-free sweet spot.

    Returns `(safe_tts_text, chosen_tag, safe_temp, safe_top_p, safe_rep_pen)`.
    """
    forb = {str(t).strip().lower() for t in (forbidden_tags or ()) if str(t).strip()}
    cue_key = str(cue or "").strip().lower()

    # 1) Strip all existing `[...]` direction tags so Japanese prose tags / [excited] never leak
    plain = _S2PRO_ANY_BRACKET_TAG_RE.sub("", str(tts_text or "")).strip()
    plain = re.sub(r"[ \t]{2,}", " ", plain).strip()

    # 2) Tame extreme scream vowel elongation & punctuation storms (TTS-only; subtitle stays untouched)
    if plain:
        plain = _S2PRO_JP_LONG_SCREAM_RE.sub(r"\1", plain)
        plain = _S2PRO_KO_LONG_SCREAM_RE.sub(r"\1", plain)
        plain = _S2PRO_EXCESS_PUNCT_RE.sub(r"\1\1", plain)

    # 3) Pick ONLY verified, distortion-free open-weights English tags
    chosen_tag = ""
    if auto_direct and plain:
        if cue_key in _S2PRO_SOFT_CUES:
            if "[soft tone]" not in forb and "[whisper]" not in forb:
                chosen_tag = "[soft tone]"
        elif cue_key in _S2PRO_SIGH_CUES:
            if "[sigh]" not in forb and not _S2PRO_WRITTEN_SIGH_PREFIX_RE.search(plain):
                chosen_tag = "[sigh]"
        elif cue_key in _S2PRO_LAUGH_CUES:
            if (
                "[laughing]" not in forb
                and "[laughs]" not in forb
                and not _S2PRO_WRITTEN_LAUGH_PREFIX_RE.search(plain)
            ):
                chosen_tag = "[laughing]"

    safe_tts = f"{chosen_tag} {plain}".strip() if (chosen_tag and plain) else plain

    # 4) Clamp sampling params to distortion-free open-weights range (prevents cracked VQ codes)
    try:
        raw_t = float(temperature)
    except (TypeError, ValueError):
        raw_t = 0.70
    if not math.isfinite(raw_t):
        raw_t = 0.70
    max_t = 0.72 if cue_key in _S2PRO_HIGH_ENERGY_CUES else 0.74
    # Monotonically compress high character temperatures above 0.72 (e.g. 0.72..0.94 -> 0.72..0.74)
    if raw_t > 0.72:
        raw_t = 0.72 + min(0.02, (raw_t - 0.72) * 0.10)
    safe_temp = round(max(0.56, min(max_t, raw_t)), 2)

    try:
        raw_p = float(top_p)
    except (TypeError, ValueError):
        raw_p = 0.76
    if not math.isfinite(raw_p):
        raw_p = 0.76
    safe_top_p = round(max(0.66, min(0.78, raw_p)), 2)

    try:
        raw_rp = float(repetition_penalty)
    except (TypeError, ValueError):
        raw_rp = 1.12
    if not math.isfinite(raw_rp):
        raw_rp = 1.12
    safe_rep_pen = round(max(1.08, min(1.18, raw_rp)), 2)

    return safe_tts, chosen_tag, safe_temp, safe_top_p, safe_rep_pen


def soft_limit_pcm_f32(
    audio_f32: np.ndarray,
    *,
    knee: float = 0.82,
    ceiling: float = 0.95,
) -> np.ndarray:
    """
    Transparent soft-knee peak limiter for float32 audio.
    Leaves signals with `|x| <= knee` bit-identical while smoothly compressing loud peaks
    toward `ceiling` via `tanh` so loud exclamations or >1.0x volume never hard-clip.
    """
    arr = np.asarray(audio_f32, dtype=np.float32)
    if arr.size == 0:
        return arr
    k = float(max(0.5, min(0.92, knee)))
    c = float(max(k + 0.02, min(0.985, ceiling)))
    abs_x = np.abs(arr)
    if float(np.max(abs_x)) <= k:
        return arr
    out = arr.copy()
    mask = abs_x > k
    headroom = c - k
    over = (abs_x[mask] - k) / headroom
    out[mask] = np.sign(arr[mask]) * (k + headroom * np.tanh(over))
    return out


_S2PRO_EST_ELLIPSIS_RE = re.compile(r"(?:…{1,}|\.{3,})")
_S2PRO_EST_END_PUNCT_RE = re.compile(r"[。！？!?]+")
_S2PRO_EST_COMMA_RE = re.compile(r"[、,；;]")
_S2PRO_EST_SMALL_KANA = frozenset("ぁぃぅぇぉっゃゅょァィゥェォッャュョ")
_S2PRO_EST_VOICE_FX_TAG_RE = re.compile(r"\[(?:sigh|laughing|laughs|chuckle|giggle)\]", re.IGNORECASE)

_S2PRO_OPEN_HARD_END_RE = re.compile(r"(?:[。！？!?…]|\.{3,}|(?<![0-9.])\.(?![0-9.]))+")
_S2PRO_OPEN_SOFT_SEP_RE = re.compile(r"(?:[、,；;]|——|──)")
_S2PRO_OPEN_JP_QUOTATIVE_RE = re.compile(r"^(?:ってば|って|と|なんて|など|とか|みたい|だなんて)")
_S2PRO_OPEN_JP_CONNECTIVE_RE = re.compile(
    r"(?:だけど|けれども|けれど|だから|なので|ですので|ますので|のに|だし|ですよ|ますよ|ですね|ますね|ですか|ますか|よね|わよ)"
)
_S2PRO_OPEN_KO_CLAUSE_END_RE = re.compile(
    r"(?:는데도|지만은|잖아요|거든요|는데|은데|지만|니까|해서|거든|네요|어요|아요|해요|세요|까요|군요|면서|라면|으면|다면)\s+"
)
_S2PRO_OPEN_KO_AUX_NEXT_RE = re.compile(r"^(?:싶|있|없|않|못|말|보|주|버리|두|놓|되)")
_S2PRO_OPEN_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_S2PRO_OPEN_BAD_NEXT_RE = re.compile(r"^[っッぁぃぅぇぉゃゅょァィゥェォャュョー、。！？!?….,;」』）)\"'”’\]]")


def estimate_s2pro_clause_audio_sec(text: str, *, speed: float = 1.0) -> float:
    """
    Estimate conservative (~85th-percentile expressive anime delivery) spoken duration in seconds
    for a Japanese, Korean, or mixed S2-Pro utterance so adaptive pre-buffering never underestimates.
    """
    raw = str(text or "").strip()
    if not raw:
        return 0.6

    fx_bonus = 0.40 if _S2PRO_EST_VOICE_FX_TAG_RE.search(raw) else 0.0
    plain = _S2PRO_ANY_BRACKET_TAG_RE.sub("", raw).strip()
    if not plain:
        return max(0.6, round(0.45 + fx_bonus, 3))

    dur = 0.42 + fx_bonus
    # Count punctuation pauses before scanning speech characters
    dur += 0.38 * len(_S2PRO_EST_ELLIPSIS_RE.findall(plain))
    no_ell = _S2PRO_EST_ELLIPSIS_RE.sub("", plain)
    dur += 0.24 * len(_S2PRO_EST_END_PUNCT_RE.findall(no_ell))
    dur += 0.18 * len(_S2PRO_EST_COMMA_RE.findall(no_ell))

    for ch in no_ell:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:
            dur += 0.26  # Kanji (~1.6-2.0 morae on average)
        elif 0x3040 <= code <= 0x30FF:
            dur += 0.08 if ch in _S2PRO_EST_SMALL_KANA else 0.15  # Hiragana / Katakana
        elif 0xAC00 <= code <= 0xD7A3:
            dur += 0.20  # Korean Hangul syllable
        elif ch.isalnum():
            dur += 0.08  # ASCII / digits
        elif ch.isspace():
            dur += 0.03

    try:
        spd = float(speed)
    except (TypeError, ValueError):
        spd = 1.0
    if not (0.5 <= spd <= 2.0):
        spd = 1.0
    eff_speed = max(0.80, min(1.25, spd))
    return round(max(0.6, min(20.0, dur / eff_speed)), 3)


def compute_s2pro_prebuffer_cushion_sec(
    est_audio_sec: float,
    ema_rtf: float,
    *,
    target_rtf: float = 0.92,
    safety_margin_sec: float = 0.35,
    burst_audio_sec: float = 1.15,
) -> float:
    """
    Compute the mathematically safe minimum pre-buffered audio duration (seconds) before starting
    playback when the server runs slower than real-time (`ema_rtf > target_rtf`).

    For discrete vocoder bursts of `burst_audio_sec` arriving at rate `1 / R`, avoiding underrun
    through the final burst requires:
        `B >= D * (R - target_rtf) / R + burst_audio_sec / R + safety_margin_sec`
    capped at `est_audio_sec` (stream EOF always starts playback immediately for shorter clauses).
    Returns `0.0` when `ema_rtf <= target_rtf` for instant zero-wait streaming on fast GPUs.
    """
    try:
        raw_est = float(est_audio_sec if est_audio_sec is not None else 0.6)
    except (TypeError, ValueError):
        raw_est = 0.6
    if not math.isfinite(raw_est):
        raw_est = 0.6
    est_d = max(0.6, raw_est)

    try:
        rtf = float(ema_rtf)
        tgt = float(target_rtf)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(tgt) or tgt <= 0.0:
        tgt = 0.92
    if math.isnan(rtf):
        return 0.0
    if not math.isfinite(rtf):
        return round(est_d, 3) if rtf > 0.0 else 0.0
    if rtf <= tgt:
        return 0.0

    try:
        burst = float(burst_audio_sec if burst_audio_sec is not None else 0.0)
    except (TypeError, ValueError):
        burst = 0.0
    if not math.isfinite(burst) or burst < 0.0:
        burst = 0.0

    try:
        margin = float(safety_margin_sec if safety_margin_sec is not None else 0.0)
    except (TypeError, ValueError):
        margin = 0.0
    if not math.isfinite(margin) or margin < 0.0:
        margin = 0.0

    cont = est_d * max(0.0, (rtf - tgt) / rtf)
    discrete = burst / rtf
    total = cont + discrete + margin
    if not math.isfinite(total):
        return round(est_d, 3)
    return round(min(est_d, total), 3)


_OPEN_TO_CLOSE_DELIM: dict[str, str] = {
    "「": "」",
    "『": "』",
    "（": "）",
    "(": ")",
    "“": "”",
    "‘": "’",
    "[": "]",
}
_CLOSE_DELIMS = frozenset(_OPEN_TO_CLOSE_DELIM.values())
_TRAILING_CLOSE_QUOTES = frozenset({"」", "』", "）", ")", "”", "’", '"', "'"})


def _quote_stack_at(s: str, pos: int) -> list[str]:
    """Return stack of expected closing quote/bracket delimiters still open at `s[:pos]`."""
    stack: list[str] = []
    limit = min(len(s), max(0, int(pos)))
    n = len(s)
    for i in range(limit):
        ch = s[i]
        expected_close = _OPEN_TO_CLOSE_DELIM.get(ch)
        if expected_close is not None:
            stack.append(expected_close)
        elif ch in _CLOSE_DELIMS:
            if stack and stack[-1] == ch:
                stack.pop()
            elif ch in stack:
                # Unwind back to the nearest matching open delimiter
                for j in range(len(stack) - 1, -1, -1):
                    if stack[j] == ch:
                        del stack[j:]
                        break
        elif ch == '"':
            if stack and stack[-1] == '"':
                stack.pop()
            else:
                stack.append('"')
        elif ch == "'":
            # Ignore English word-internal apostrophes (e.g. don't, I'm, it's)
            is_apostrophe = (
                0 < i < n - 1
                and s[i - 1].isascii()
                and s[i - 1].isalpha()
                and s[i + 1].isascii()
                and s[i + 1].isalpha()
            )
            if not is_apostrophe:
                if stack and stack[-1] == "'":
                    stack.pop()
                else:
                    stack.append("'")
    return stack


def _is_inside_unclosed_quote(s: str, pos: int) -> bool:
    return bool(_quote_stack_at(s, pos))


def split_s2pro_opening_clause(
    text: str,
    *,
    min_hard_chars: int = 6,
    min_soft_chars: int = 10,
    max_head_chars: int = 24,
    min_tail_chars: int = 8,
    hard_split_min_total: int = 18,
) -> tuple[str, str]:
    """
    Fast-split a long opening conversational clause for Colab S2-Pro (`RTF > 1.0`) into
    `(short_opening_head, remaining_tail)` so Clause #1a begins speaking in ~2.5-3.8s while
    Clause #1b generates concurrently on SGLang-Omni (`batch=2` ~27 tok/s).

    Guarantees natural JP/KR phrasing:
      - Tier 1 (hard sentence boundary `。！？!?……...`): triggers when `len >= hard_split_min_total`
        (default 18), outside quotes/brackets, never breaking Japanese `」って`/`」と` quotatives
        or swallowing the next sentence's opening quote.
      - Tier 2 (soft comma/dash boundary `、, ； ——`): only when `len > max_head_chars` (24) and
        `head >= min_soft_chars` (10), avoiding choppy 2-6 char filler cuts like `ふふっ、`.
      - Tier 3 (Korean space-delimited clause): multi-syllable connectives or word boundary
        avoiding auxiliary verb splits (`보고 싶다`, `하고 있다`).
      - Tier 4 (unpunctuated JP run-on >= 28 chars): unambiguous clause connectives only, never
        splitting sokuon `っ` or mid-word.
      - Returns `(s, "")` unchanged if no clean natural boundary exists.
    """
    s = str(text or "").strip()
    if not s or len(s) < hard_split_min_total:
        return s, ""

    # Tier 1: Hard sentence boundary (`。！？!?……...`)
    for m in _S2PRO_OPEN_HARD_END_RE.finditer(s):
        idx = m.end()
        stack = _quote_stack_at(s, idx)
        # Only attach trailing closing quotes that actually close an unclosed opener in the head
        while idx < len(s) and stack and s[idx] in _TRAILING_CLOSE_QUOTES and s[idx] == stack[-1]:
            stack.pop()
            idx += 1
        if stack:
            continue
        if idx < min_hard_chars:
            continue
        if idx > max_head_chars:
            break
        head = s[:idx].strip()
        tail = s[idx:].strip()
        if head and head[-1] in ("」", "』", '"', "'", "”", "’") and _S2PRO_OPEN_JP_QUOTATIVE_RE.match(tail):
            continue
        if len(head) >= min_hard_chars and len(tail) >= min_tail_chars:
            return head, tail

    if len(s) <= max_head_chars:
        return s, ""

    # Tier 2: Soft punctuation boundary (`、`, `,`, `；`, `——`)
    for m in _S2PRO_OPEN_SOFT_SEP_RE.finditer(s):
        idx = m.end()
        if idx < min_soft_chars:
            continue
        if idx > max_head_chars:
            break
        if _is_inside_unclosed_quote(s, idx):
            continue
        head = s[:idx].strip()
        tail = s[idx:].strip()
        if len(head) >= min_soft_chars and len(tail) >= min_tail_chars:
            return head, tail

    # Tier 3: Korean space-delimited clause boundary
    if _S2PRO_OPEN_HANGUL_RE.search(s) and " " in s:
        for m in _S2PRO_OPEN_KO_CLAUSE_END_RE.finditer(s):
            idx = m.end()
            head = s[:idx].strip()
            tail = s[idx:].strip()
            if len(head) < min_soft_chars:
                continue
            if len(head) > max_head_chars:
                break
            if len(tail) >= min_tail_chars and not _is_inside_unclosed_quote(s, idx):
                return head, tail
        # Fallback: word-boundary space in [12..max_head_chars] avoiding auxiliary verb cuts
        for m in re.finditer(r"\s+", s):
            idx = m.end()
            head = s[:idx].strip()
            tail = s[idx:].strip()
            if len(head) < max(12, min_soft_chars):
                continue
            if len(head) > max_head_chars:
                break
            if _S2PRO_OPEN_KO_AUX_NEXT_RE.match(tail):
                continue
            if len(tail) >= max(10, min_tail_chars) and not _is_inside_unclosed_quote(s, idx):
                return head, tail

    # Tier 4: Long unpunctuated Japanese run-on (>= 28 chars) at unambiguous clause connective
    if len(s) >= 28 and not _S2PRO_OPEN_HANGUL_RE.search(s):
        for m in _S2PRO_OPEN_JP_CONNECTIVE_RE.finditer(s):
            idx = m.end()
            if idx < max(11, min_soft_chars):
                continue
            if idx > max_head_chars:
                break
            if _is_inside_unclosed_quote(s, idx):
                continue
            head = s[:idx].strip()
            tail = s[idx:].strip()
            if (
                len(head) >= max(11, min_soft_chars)
                and len(tail) >= max(10, min_tail_chars)
                and not _S2PRO_OPEN_BAD_NEXT_RE.match(tail)
                and not head.endswith(("っ", "ッ"))
            ):
                return head, tail

    return s, ""

DEFAULT_S2PRO_SR = 44100
MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_ERROR_BYTES = 4096
MAX_WAV_HEADER_BYTES = 64 * 1024
# Extreme-quality studio reference defaults (~38-48s multi-clip verbatim transcript bundle):
# proven zero slowdown on S2-Pro while maximizing character timbre & emotional expressiveness.
TARGET_REF_MIN_SEC = 38.0
TARGET_REF_MAX_SEC = 48.0
REF_GAP_SEC = 0.32

_SAFE_REF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,79}$")
_NON_ID_CHARS_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _is_forbidden_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_localhost: bool = False,
) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if allow_localhost and ip.is_loopback:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


class ColabS2ProError(RuntimeError):
    """Base error for dedicated Colab S2-Pro server operations."""


class ColabS2ProAuthError(ColabS2ProError):
    """Raised when the Colab S2-Pro server rejects the Bearer token (HTTP 401/403)."""


class ColabS2ProUnavailableError(ColabS2ProError):
    """Raised when the Colab tunnel is offline, sleeping, or unreachable."""


def validate_colab_server_url(
    raw_url: str,
    *,
    resolve_dns: bool = True,
    fail_on_dns_error: bool = False,
) -> str:
    """
    Validate and normalize a Colab S2-Pro server base URL.
    Allows:
      - `https://<public-host>[:port]` (e.g. `https://xxxx.trycloudflare.com`),
        blocking raw non-global/private/CGNAT/loopback/link-local IP literals and DNS records
        resolving to non-global IPs to prevent LAN/CGNAT/metadata SSRF.
      - Loopback HTTP (`127.0.0.1` or `localhost` with optional port) for local SSH/GPU tunnels.
    """
    raw = str(raw_url or "").strip()
    if not raw or "\\" in raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError(f"유효하지 않은 서버 주소입니다: {raw_url!r}")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    raw_host = (parsed.hostname or "").lower()
    if raw_host.endswith(".."):
        raise ValueError(f"허용되지 않는 URL 형식입니다: {raw_url!r}")
    host = raw_host.rstrip(".")
    decoded_path = unquote(parsed.path or "")

    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or "?" in decoded_path
        or "#" in decoded_path
        or "\\" in decoded_path
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded_path)
        or ".." in decoded_path.split("/")
    ):
        raise ValueError(f"허용되지 않는 URL 형식입니다: {raw_url!r}")

    if scheme == "http":
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("HTTP는 로컬 터널(127.0.0.1 / localhost)만 허용됩니다. Colab 주소는 HTTPS를 사용해 주세요.")
    elif scheme == "https":
        if host == "localhost" or any(host.endswith(suf) for suf in _FORBIDDEN_HOST_SUFFIXES):
            raise ValueError(f"공개 도메인 형식이 아닙니다: {host}")

        # Block literal non-global/private/CGNAT/link-local/loopback IPs (including IPv4 shorthand like 127.1)
        ip_lit: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
        try:
            ip_lit = ipaddress.ip_address(host)
        except ValueError:
            ip_lit = None
        if ip_lit is None and ":" not in host:
            try:
                ip_lit = ipaddress.IPv4Address(socket.inet_aton(host))
            except OSError:
                ip_lit = None

        if ip_lit is not None:
            if _is_forbidden_ip(ip_lit):
                raise ValueError(f"내부 IP 주소는 허용되지 않습니다: {host}")
        else:
            if "." not in host or any(not lbl for lbl in host.split(".")):
                raise ValueError(f"공개 도메인 형식이 아닙니다: {host}")
            if resolve_dns:
                port = parsed.port or 443
                try:
                    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                except OSError as exc:
                    if fail_on_dns_error:
                        raise ValueError(f"서버 도메인을 해석할 수 없습니다 ({host}): {exc}") from exc
                    infos = []
                for _family, _type, _proto, _canon, sockaddr in infos:
                    ip_str = str(sockaddr[0]).split("%", 1)[0]
                    try:
                        resolved_ip = ipaddress.ip_address(ip_str)
                    except ValueError as exc:
                        raise ValueError(f"해석된 서버 IP 형식이 올바르지 않습니다: {host}") from exc
                    if _is_forbidden_ip(resolved_ip):
                        raise ValueError(f"내부 IP 주소로 해석되는 도메인은 허용되지 않습니다: {host}")
    else:
        raise ValueError(f"지원하지 않는 프로토콜입니다 ({scheme}): HTTPS URL을 입력해 주세요.")

    netloc_host = f"[{host}]" if ":" in host else host
    base = f"{scheme}://{netloc_host}"
    if parsed.port is not None:
        if not ((scheme == "https" and parsed.port == 443) or (scheme == "http" and parsed.port == 80)):
            base += f":{parsed.port}"
    clean_path = (parsed.path or "").rstrip("/")
    if clean_path:
        base += clean_path
    return base


def make_s2pro_reference_id(char_id: str, lang: str) -> str:
    """Build a strictly safe Fish-Speech reference ID like `ba_Aru_JP` or `ba_Hina_KR`."""
    raw_c = _NON_ID_CHARS_RE.sub("_", str(char_id or "default").strip()).strip("_") or "default"
    norm_lang = "KR" if str(lang or "ja").strip().lower() in ("ko", "kr") else "JP"
    ref_id = f"ba_{raw_c[:64]}_{norm_lang}"
    if not _SAFE_REF_ID_RE.match(ref_id):
        ref_id = "ba_voice_" + norm_lang
    return ref_id


def _load_mono_float32(wav_path: Path, target_sr: int = DEFAULT_S2PRO_SR) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim > 1:
        arr = np.mean(arr, axis=1, dtype=np.float32)
    if int(sr) != int(target_sr) and arr.size > 0:
        from math import gcd
        from scipy.signal import resample_poly

        g = gcd(int(sr), int(target_sr))
        arr = resample_poly(arr, int(target_sr) // g, int(sr) // g).astype(np.float32, copy=False)
        sr = target_sr
    return arr, int(sr)


def _clean_ref_transcript(text: str) -> str:
    """Clean game-script UI formatting (inner-monologue full-width parens, double spaces) from reference transcripts."""
    s = str(text or "").strip()
    if not s:
        return ""
    s = s.replace("（", "").replace("）", "").replace("(", "").replace(")", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _apply_short_edge_fade(arr: np.ndarray, sr: int, fade_ms: float = 5.0) -> np.ndarray:
    n = min(int(sr * (fade_ms / 1000.0)), arr.size // 4)
    if n <= 1:
        return arr
    out = arr.copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out[:n] *= ramp
    out[-n:] *= ramp[::-1]
    return out


def _find_character_model_dir(char_id: str, lang_tag: str) -> Path | None:
    """Locate `%LOCALAPPDATA%\\VoiceCompanionData\\models\\<Char>\\<LANG>` (case-insensitive fallback)."""
    roots = [get_user_models_dir(), get_app_root() / "models"]
    raw_cid = str(char_id or "").strip()
    raw_lang = str(lang_tag or "").strip()
    target_lower = raw_cid.lower()
    if (
        not raw_cid
        or not raw_lang
        or "\x00" in raw_cid
        or "\x00" in raw_lang
        or "/" in raw_cid
        or "\\" in raw_cid
        or "/" in raw_lang
        or "\\" in raw_lang
        or raw_cid in (".", "..")
        or raw_lang in (".", "..")
        or ".." in raw_cid
        or ".." in raw_lang
    ):
        return None

    for root in roots:
        try:
            if not root.is_dir():
                continue
            root_res = root.resolve()
        except OSError:
            continue

        direct_char = root / raw_cid
        direct = direct_char / raw_lang
        try:
            if (
                not direct_char.is_symlink()
                and not direct.is_symlink()
                and direct.is_dir()
                and direct.resolve().is_relative_to(root_res)
            ):
                return direct
        except OSError:
            pass

        # Case-insensitive folder scan (e.g. `aru` -> `Aru`)
        try:
            for child in root.iterdir():
                if child.is_symlink() or not child.is_dir():
                    continue
                if child.name.lower() == target_lower:
                    cand = child / raw_lang
                    if not cand.is_symlink() and cand.is_dir() and cand.resolve().is_relative_to(root_res):
                        return cand
        except OSError:
            continue
    return None


def build_optimal_reference_bundle(
    char_id: str,
    reply_lang: str,
    *,
    char_data: dict[str, Any] | None = None,
    steam_game_dir: str = "",
    allow_auto_extract: bool = True,
) -> tuple[bytes, str, dict[str, Any]]:
    """
    Build an extreme-quality 38-48s 44.1kHz mono PCM16 reference WAV + verbatim transcript
    for Fish Audio S2-Pro zero-shot voice cloning.

    Priority:
      1. Locally extracted `%LOCALAPPDATA%\\VoiceCompanionData\\models\\<Char>\\<LANG>\\fish_samples\\*.wav`
         paired with verbatim `transcripts.json` (selecting diverse studio clips up to ~42-48s).
      2. Stitched `<Char>_<LANG>_FishAudio_Reference.wav` (cleanly trimmed to <=48.0s).
      3. On-the-fly extraction from local Steam `.molru` packs via `build_character_fish_reference`.
      4. If KR requested and no KR pack exists, clean fallback to JP voice pack.
    """
    lang_tag = "KR" if str(reply_lang or "ja").strip().lower() in ("ko", "kr") else "JP"
    fallback_order = [lang_tag] + (["JP"] if lang_tag == "KR" else [])

    for try_lang in fallback_order:
        lang_dir = _find_character_model_dir(char_id, try_lang)
        if lang_dir is None and allow_auto_extract and char_data is not None:
            try:
                from src.extractor.build_fish_reference import build_character_fish_reference

                build_character_fish_reference(
                    char_data,
                    lang=try_lang,
                    game_dir=Path(steam_game_dir) if steam_game_dir else None,
                    quality_profile="standard45",
                )
                lang_dir = _find_character_model_dir(char_id, try_lang)
            except Exception:
                lang_dir = None

        if lang_dir is None:
            continue

        samples_dir = lang_dir / "fish_samples"
        if samples_dir.is_dir():
            wavs = sorted(
                p for p in samples_dir.glob("*.wav")
                if p.is_file() and not p.is_symlink()
            )
            tr_map: dict[str, str] = {}
            tr_path = samples_dir / "transcripts.json"
            if tr_path.is_file():
                try:
                    raw_tr = json.loads(tr_path.read_text(encoding="utf-8"))
                    if isinstance(raw_tr, dict):
                        tr_map = {str(k): str(v).strip() for k, v in raw_tr.items() if str(v).strip()}
                except Exception:
                    tr_map = {}

            # Prefer clips with exact verbatim transcripts first
            matched_wavs = [
                w for w in wavs if (tr_map.get(w.name) or tr_map.get(w.stem))
            ]
            ordered = matched_wavs if matched_wavs else wavs

            chosen_audio: list[np.ndarray] = []
            chosen_texts: list[str] = []
            total_sec = 0.0
            gap = np.zeros(int(DEFAULT_S2PRO_SR * REF_GAP_SEC), dtype=np.float32)

            for w in ordered:
                try:
                    seg, sr = _load_mono_float32(w, DEFAULT_S2PRO_SR)
                except Exception:
                    continue
                if seg.size < int(sr * 0.4):
                    continue
                dur = seg.size / float(sr)
                gap_cost = REF_GAP_SEC if chosen_audio else 0.0
                if not chosen_audio and dur > TARGET_REF_MAX_SEC:
                    max_samples = int(sr * TARGET_REF_MAX_SEC)
                    seg = _apply_short_edge_fade(seg[:max_samples], sr, fade_ms=15.0)
                    dur = seg.size / float(sr)
                elif chosen_audio and (total_sec + gap_cost + dur > TARGET_REF_MAX_SEC):
                    if total_sec >= TARGET_REF_MIN_SEC:
                        break
                    continue
                chosen_audio.append(_apply_short_edge_fade(seg, sr))
                txt = _clean_ref_transcript(tr_map.get(w.name) or tr_map.get(w.stem) or "")
                if txt:
                    chosen_texts.append(txt)
                total_sec += gap_cost + dur
                if total_sec >= TARGET_REF_MIN_SEC + 4.0:
                    break

            if chosen_audio:
                parts: list[np.ndarray] = []
                for idx, seg in enumerate(chosen_audio):
                    if idx > 0:
                        parts.append(gap)
                    parts.append(seg)
                merged = np.clip(np.concatenate(parts), -0.985, 0.985)
                buf = io.BytesIO()
                sf.write(buf, merged, DEFAULT_S2PRO_SR, format="WAV", subtype="PCM_16")
                joined_text = " ".join(chosen_texts).strip()
                if not joined_text:
                    joined_text = "こんにちは。よろしくお願いします。" if try_lang == "JP" else "안녕하세요. 잘 부탁드립니다."
                return (
                    buf.getvalue(),
                    joined_text,
                    {
                        "source": "fish_samples",
                        "lang": try_lang,
                        "clip_count": len(chosen_audio),
                        "duration_sec": round(merged.size / float(DEFAULT_S2PRO_SR), 2),
                        "transcript_matched": len(chosen_texts),
                    },
                )

        # Fallback: combined single reference WAV trimmed cleanly to <=48.0s
        comb_cands = sorted(
            p for p in lang_dir.glob("*_FishAudio_Reference.wav")
            if p.is_file() and not p.is_symlink()
        )
        for comb_wav in comb_cands:
            try:
                seg, sr = _load_mono_float32(comb_wav, DEFAULT_S2PRO_SR)
            except Exception:
                continue
            if seg.size == 0 or sr <= 0:
                continue
            max_samples = int(sr * TARGET_REF_MAX_SEC)
            if seg.size > max_samples:
                seg = _apply_short_edge_fade(seg[:max_samples], sr, fade_ms=15.0)
            buf = io.BytesIO()
            sf.write(buf, np.clip(seg, -0.985, 0.985), sr, format="WAV", subtype="PCM_16")
            fallback_text = "こんにちは。よろしくお願いします。" if try_lang == "JP" else "안녕하세요. 잘 부탁드립니다."
            return (
                buf.getvalue(),
                fallback_text,
                {
                    "source": "combined_wav",
                    "lang": try_lang,
                    "clip_count": 1,
                    "duration_sec": round(seg.size / float(sr), 2),
                    "transcript_matched": 0,
                },
            )

    raise ColabS2ProError(
        f"'{char_id}' ({lang_tag}) 캐릭터의 스튜디오 참조 음성을 찾을 수 없습니다. "
        "설정 -> 1단계에서 스팀 음성 추출을 먼저 실행해 주세요."
    )


_KSDATAFORMAT_SUBTYPE_PCM = b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
_TARGET_DRIVE_JSON_NAMES = ("live_endpoint.json", "server_status.json")


class StreamingWavHeaderStripper:
    """
    Incremental RIFF/WAVE header parser for streaming `/v1/tts` (`format='wav'`) responses.
    Extracts exact `sample_rate` and `channels` from the `fmt ` chunk, enforces a 64 KB
    header cap and 16-bit PCM validation (cleanly downmixing stereo to mono), and yields
    pure little-endian 16-bit mono PCM payload bytes starting immediately after `data`.
    """

    def __init__(self, default_sr: int = DEFAULT_S2PRO_SR, max_header_bytes: int = MAX_WAV_HEADER_BYTES) -> None:
        self.sample_rate: int = int(default_sr)
        self.channels: int = 1
        self.bits_per_sample: int = 16
        self.audio_format: int = 1
        self.header_parsed: bool = False
        self._max_header_bytes: int = max(256, int(max_header_bytes))
        self._buf = bytearray()
        self._mono_rem = bytearray()
        self._stereo_rem = bytearray()

    def _convert_pcm_to_mono(self, raw_pcm: bytes) -> bytes:
        if not raw_pcm:
            return b""
        if self.channels == 1:
            if self._mono_rem:
                buf = bytes(self._mono_rem) + raw_pcm
                self._mono_rem.clear()
            else:
                buf = raw_pcm
            aligned = len(buf) & ~1
            if aligned < len(buf):
                self._mono_rem.extend(buf[aligned:])
            return buf[:aligned] if aligned else b""
        if self.channels == 2:
            if self._stereo_rem:
                buf = bytes(self._stereo_rem) + raw_pcm
                self._stereo_rem.clear()
            else:
                buf = raw_pcm
            aligned = (len(buf) // 4) * 4
            if aligned < len(buf):
                self._stereo_rem.extend(buf[aligned:])
            if aligned == 0:
                return b""
            stereo = np.frombuffer(buf[:aligned], dtype="<i2").reshape(-1, 2)
            mono = ((stereo[:, 0].astype(np.int32) + stereo[:, 1].astype(np.int32)) // 2).astype("<i2")
            return mono.tobytes()
        raise ColabS2ProError(f"지원하지 않는 WAV 채널 수입니다 ({self.channels}ch).")

    def feed(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""
        if self.header_parsed:
            return self._convert_pcm_to_mono(chunk)

        self._buf.extend(chunk)
        # Need at least 12 bytes for RIFF....WAVE
        if len(self._buf) < 12:
            return b""

        # If server ever returned raw headerless PCM directly, pass through untouched
        if bytes(self._buf[:4]) != b"RIFF" or bytes(self._buf[8:12]) != b"WAVE":
            self.header_parsed = True
            out = bytes(self._buf)
            self._buf.clear()
            return self._convert_pcm_to_mono(out)

        pos = 12
        buf_len = len(self._buf)
        while pos + 8 <= buf_len:
            chunk_id = bytes(self._buf[pos : pos + 4])
            chunk_size = struct.unpack_from("<I", self._buf, pos + 4)[0]
            pos += 8
            if chunk_id == b"fmt ":
                if pos + chunk_size > self._max_header_bytes:
                    raise ColabS2ProError(f"Colab S2-Pro WAV 헤더 크기 한도({self._max_header_bytes}B)를 초과했습니다.")
                if pos + chunk_size > buf_len:
                    if buf_len > self._max_header_bytes:
                        raise ColabS2ProError(f"Colab S2-Pro WAV 헤더 크기 한도({self._max_header_bytes}B)를 초과했습니다.")
                    return b""  # Wait for complete fmt chunk
                if chunk_size < 16:
                    raise ColabS2ProError(
                        f"손상된 Colab S2-Pro WAV fmt 청크입니다 (size={chunk_size}B < 16B)."
                    )
                audio_fmt, ch, sr, _br, _ba, bps = struct.unpack_from("<HHIIHH", self._buf, pos)
                self.audio_format = int(audio_fmt)
                if audio_fmt not in (1, 0xFFFE) or bps != 16:
                    raise ColabS2ProError(
                        f"지원하지 않는 Colab S2-Pro WAV 포맷입니다 (format={audio_fmt}, bits={bps}). 16-bit PCM이 필요합니다."
                    )
                if audio_fmt == 0xFFFE:
                    if chunk_size < 40:
                        raise ColabS2ProError(
                            f"손상된 Colab S2-Pro WAV_EXTENSIBLE fmt 청크입니다 (size={chunk_size}B < 40B)."
                        )
                    subformat_guid = bytes(self._buf[pos + 24 : pos + 40])
                    if subformat_guid != _KSDATAFORMAT_SUBTYPE_PCM:
                        raise ColabS2ProError(
                            "지원하지 않는 Colab S2-Pro WAV_EXTENSIBLE SubFormat입니다. 16-bit PCM만 허용됩니다."
                        )
                if ch not in (1, 2):
                    raise ColabS2ProError(f"지원하지 않는 Colab S2-Pro WAV 채널 수입니다 ({ch}ch).")
                if 8000 <= sr <= 192000:
                    self.sample_rate = int(sr)
                self.channels = int(ch)
                self.bits_per_sample = int(bps)
                pos += chunk_size + (chunk_size & 1)
            elif chunk_id == b"data":
                self.header_parsed = True
                pcm = bytes(self._buf[pos:])
                self._buf.clear()
                return self._convert_pcm_to_mono(pcm)
            else:
                # Skip unknown metadata subchunk (LIST, JUNK, fact, etc.) with strict cap
                if pos + chunk_size > self._max_header_bytes:
                    raise ColabS2ProError(f"Colab S2-Pro WAV 헤더 크기 한도({self._max_header_bytes}B)를 초과했습니다.")
                if pos + chunk_size > buf_len:
                    return b""
                pos += chunk_size + (chunk_size & 1)
        if buf_len > self._max_header_bytes:
            raise ColabS2ProError(f"Colab S2-Pro WAV 헤더 크기 한도({self._max_header_bytes}B)를 초과했습니다.")
        return b""


def resolve_gdrive_bin() -> str | None:
    """
    Locate the `gdrive` CLI executable cleanly without depending on a shell-injected PATH.
    Checks optional `BA_GDRIVE_BIN` env override first, then `shutil.which("gdrive")`
    (preferring a sibling `.exe` over `.cmd`/`.bat` on Windows), and finally standard
    user-home CLI bin locations (`~/.local/bin`).
    """
    env_bin = str(os.environ.get("BA_GDRIVE_BIN") or "").strip()
    if env_bin:
        try:
            env_path = Path(env_bin)
            if env_path.is_file():
                return str(env_path)
        except OSError:
            pass
    found = shutil.which("gdrive")
    if found:
        if os.name == "nt" and Path(found).suffix.lower() in (".cmd", ".bat"):
            sibling_exe = Path(found).with_suffix(".exe")
            try:
                if sibling_exe.is_file():
                    return str(sibling_exe)
            except OSError:
                pass
        return found
    try:
        home = Path.home()
    except Exception:
        return None
    candidates = (
        home / ".local" / "bin" / "gdrive.exe",
        home / ".local" / "bin" / "gdrive.cmd",
        home / ".local" / "bin" / "gdrive",
    )
    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def _run_gdrive_subprocess(
    gdrive_bin: str,
    args: list[str],
    *,
    timeout_sec: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    """
    Invoke `gdrive` safely without `shell=True`.
    On Windows, `.cmd`/`.bat` shims break when paths contain `&` without spaces unless invoked
    via `cmd.exe /d /s /c` with explicit double-quoting around the script path and every argument.
    """
    cmd_parts = [str(gdrive_bin), *(str(a) for a in args)]
    if any('"' in p or "\r" in p or "\n" in p for p in cmd_parts):
        raise ValueError("Invalid control or quote character in gdrive command arguments")
    if os.name == "nt" and Path(gdrive_bin).suffix.lower() in (".cmd", ".bat"):
        sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
        cmd_exe = os.path.join(sysroot, "System32", "cmd.exe")
        inner = " ".join(f'"{p}"' for p in cmd_parts)
        cmdline = f'"{cmd_exe}" /d /s /c "{inner}"'
        return subprocess.run(
            cmdline,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    return subprocess.run(
        cmd_parts,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _resolve_drive_folder_id(drive_folder_id: str | None = None) -> str:
    """Resolve an explicitly configured Google Drive folder ID (argument or env var only)."""
    folder_arg = str(drive_folder_id).strip() if drive_folder_id is not None else ""
    folder_id = (
        folder_arg
        or os.environ.get("BA_COLAB_S2PRO_DRIVE_FOLDER_ID", "").strip()
        or COLAB_S2PRO_DRIVE_FOLDER_ID
    ).strip()
    if folder_id and _SAFE_DRIVE_ID_RE.match(folder_id):
        return folder_id
    return ""


def _parse_gdrive_ls_output(stdout: str) -> dict[str, str]:
    """
    Parse `gdrive readonly ls <folder_id> --json` (or legacy tabular fallback) matching ONLY exact
    filenames in `_TARGET_DRIVE_JSON_NAMES` (never loose substrings like `old_live_endpoint.json`).
    """
    found: dict[str, str] = {}
    text = str(stdout or "").strip()
    if not text:
        return found

    # 1) Structured JSON output (preferred)
    if text[0] in ("[", "{"):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        entries: list[Any] | None = None
        if isinstance(parsed, list):
            entries = parsed
        elif isinstance(parsed, dict):
            for key in ("files", "items", "entries", "data"):
                if isinstance(parsed.get(key), list):
                    entries = parsed[key]
                    break
        if entries is not None:
            best_mtime: dict[str, str] = {}
            for item in entries:
                if not isinstance(item, dict):
                    continue
                if item.get("trashed") or item.get("Trashed"):
                    continue
                name = str(item.get("name") or item.get("Name") or "").strip()
                if name not in _TARGET_DRIVE_JSON_NAMES:
                    continue
                fid = str(item.get("id") or item.get("Id") or item.get("fileId") or "").strip()
                if not _SAFE_DRIVE_ID_RE.match(fid):
                    continue
                mtime = str(
                    item.get("modifiedTime")
                    or item.get("modified_time")
                    or item.get("updated")
                    or item.get("createdTime")
                    or ""
                ).strip()
                if name not in found or (mtime and mtime > best_mtime.get(name, "")):
                    found[name] = fid
                    best_mtime[name] = mtime
            return found

    # 2) Legacy tabular output fallback: require exact whitespace-separated filename token match
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        cand_id = parts[0].strip()
        if not _SAFE_DRIVE_ID_RE.match(cand_id):
            continue
        name_tokens = parts[1:]
        for target_name in _TARGET_DRIVE_JSON_NAMES:
            if target_name in name_tokens and target_name not in found:
                found[target_name] = cand_id
    return found


def _gdrive_list_folder_file_ids(
    gdrive_bin: str,
    folder_id: str,
    timeout_sec: float = 10.0,
) -> dict[str, str]:
    """List known Colab S2-Pro runtime JSON file IDs in `folder_id` via `gdrive readonly ls --json`."""
    if not folder_id or not _SAFE_DRIVE_ID_RE.match(folder_id):
        return {}
    try:
        ls_res = _run_gdrive_subprocess(
            gdrive_bin,
            ["readonly", "ls", folder_id, "--json"],
            timeout_sec=timeout_sec,
        )
        if ls_res.returncode != 0:
            # Fallback for older gdrive versions that may not support `--json`
            ls_res = _run_gdrive_subprocess(
                gdrive_bin,
                ["readonly", "ls", folder_id],
                timeout_sec=timeout_sec,
            )
            if ls_res.returncode != 0:
                return {}
        return _parse_gdrive_ls_output(ls_res.stdout)
    except Exception:
        return {}


def _gdrive_download_json_by_id(
    gdrive_bin: str,
    file_id: str,
    filename: str,
    timeout_sec: float = 10.0,
) -> dict[str, Any] | None:
    """Download and parse a small JSON file from Google Drive by validated `file_id`."""
    clean_id = str(file_id or "").strip()
    if not clean_id or not _SAFE_DRIVE_ID_RE.match(clean_id):
        return None
    safe_name = Path(filename).name
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_json = Path(tmpdir) / safe_name
            dl_res = _run_gdrive_subprocess(
                gdrive_bin,
                ["readonly", "download", clean_id, "-o", str(tmp_json)],
                timeout_sec=timeout_sec,
            )
            if dl_res.returncode != 0 or not tmp_json.is_file():
                return None
            data = json.loads(tmp_json.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_server_status_from_drive(
    timeout_sec: float = 10.0,
    *,
    drive_folder_id: str | None = None,
    status_file: str | Path | None = None,
    cached_file_ids: dict[str, str] | None = None,
    force_refresh: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """
    Read `server_status.json` from an explicit local file or Google Drive folder via `gdrive readonly`.
    Caches discovered Drive file IDs (`server_status.json`, `live_endpoint.json`) in the returned dict
    so repeated polling uses 1 API call (`download`) instead of `ls` + `download`.
    Pass `force_refresh=True` to bypass cached file IDs and re-list the folder via `gdrive readonly ls`
    (protects against Drive FUSE `os.replace` file-ID churn where trashed old IDs still return HTTP 200).
    NEVER runs `gdrive` unless an explicit `drive_folder_id` or `BA_COLAB_S2PRO_DRIVE_FOLDER_ID` is set.
    """
    cache: dict[str, str] = {}
    if isinstance(cached_file_ids, dict):
        for k, v in cached_file_ids.items():
            ks = str(k or "").strip()
            vs = str(v or "").strip()
            if ks and vs and _SAFE_DRIVE_ID_RE.match(vs):
                cache[ks] = vs

    # 1) Optional explicit local status JSON file
    sf_arg = str(status_file).strip() if status_file is not None else ""
    if sf_arg:
        try:
            cand = Path(sf_arg)
            if cand.is_file() and (not cand.is_symlink()) and cand.suffix.lower() == ".json":
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data, cache
        except Exception:
            pass

    # 2) Optional `gdrive` CLI lookup ONLY when Drive folder ID is explicitly configured
    folder_id = _resolve_drive_folder_id(drive_folder_id)
    if not folder_id:
        return None, cache

    gdrive_bin = resolve_gdrive_bin()
    if not gdrive_bin:
        return None, cache

    if force_refresh:
        for target_name in _TARGET_DRIVE_JSON_NAMES:
            cache.pop(target_name, None)
    else:
        cached_status_id = cache.get("server_status.json", "")
        if cached_status_id:
            data = _gdrive_download_json_by_id(gdrive_bin, cached_status_id, "server_status.json", timeout_sec=timeout_sec)
            if data is not None:
                return data, cache
            # Cached file ID may have been replaced after Drive recreate; invalidate and refresh once
            cache.pop("server_status.json", None)

    listed = _gdrive_list_folder_file_ids(gdrive_bin, folder_id, timeout_sec=timeout_sec)
    if listed:
        cache.update(listed)
    status_id = cache.get("server_status.json", "")
    if not status_id:
        return None, cache

    data = _gdrive_download_json_by_id(gdrive_bin, status_id, "server_status.json", timeout_sec=timeout_sec)
    return data, cache


def discover_endpoint_from_drive(
    timeout_sec: float = 12.0,
    *,
    endpoint_file: str | Path | None = None,
    drive_folder_id: str | None = None,
    cached_file_id: str | None = None,
    cached_file_ids: dict[str, str] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """
    Optional endpoint auto-discovery using ONLY an explicit user-configured JSON file
    (`endpoint_file` or env var `BA_COLAB_S2PRO_ENDPOINT_FILE`) or an explicit Drive
    folder ID (`drive_folder_id` or env var `BA_COLAB_S2PRO_DRIVE_FOLDER_ID`) when
    `gdrive` CLI is installed. Returns `None` by default in clean public releases.

    When `cached_file_id` or `cached_file_ids["live_endpoint.json"]` is supplied (and
    `force_refresh=False`), downloads directly in 1 API call (falling back to `gdrive readonly ls`
    once if the cached ID fails). Pass `force_refresh=True` to bypass cached file IDs and re-list.
    """
    # 1) Explicit user-configured local endpoint JSON file (argument or env var only)
    ep_arg = str(endpoint_file).strip() if endpoint_file is not None else ""
    ep_raw = ep_arg or os.environ.get("BA_COLAB_S2PRO_ENDPOINT_FILE", "").strip()
    if ep_raw:
        try:
            cand = Path(ep_raw)
            if cand.is_file() and (not cand.is_symlink()) and cand.suffix.lower() == ".json":
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("status") == "ready" and data.get("url"):
                    data["url"] = validate_colab_server_url(str(data["url"]), resolve_dns=True)
                    return data
        except Exception:
            pass

    # 2) Optional `gdrive` CLI lookup ONLY when a Drive folder ID is explicitly configured
    folder_id = _resolve_drive_folder_id(drive_folder_id)
    if not folder_id:
        return None

    gdrive_bin = resolve_gdrive_bin()
    if not gdrive_bin:
        return None

    try:
        # Try cached Drive file ID first unless force_refresh is requested
        cand_file_id = ""
        if force_refresh:
            if isinstance(cached_file_ids, dict):
                cached_file_ids.pop("live_endpoint.json", None)
        else:
            if cached_file_id and _SAFE_DRIVE_ID_RE.match(str(cached_file_id).strip()):
                cand_file_id = str(cached_file_id).strip()
            elif isinstance(cached_file_ids, dict):
                cid_val = str(cached_file_ids.get("live_endpoint.json") or "").strip()
                if cid_val and _SAFE_DRIVE_ID_RE.match(cid_val):
                    cand_file_id = cid_val

        data: dict[str, Any] | None = None
        if cand_file_id:
            data = _gdrive_download_json_by_id(gdrive_bin, cand_file_id, "live_endpoint.json", timeout_sec=timeout_sec)
            if data is None and isinstance(cached_file_ids, dict):
                cached_file_ids.pop("live_endpoint.json", None)

        if data is None:
            listed = _gdrive_list_folder_file_ids(gdrive_bin, folder_id, timeout_sec=timeout_sec)
            if isinstance(cached_file_ids, dict) and listed:
                cached_file_ids.update(listed)
            file_id = listed.get("live_endpoint.json", "")
            if not file_id:
                return None
            data = _gdrive_download_json_by_id(gdrive_bin, file_id, "live_endpoint.json", timeout_sec=timeout_sec)

        if isinstance(data, dict) and data.get("status") == "ready" and data.get("url"):
            data["url"] = validate_colab_server_url(str(data["url"]), resolve_dns=True)
            return data
    except Exception:
        return None
    return None


class ColabS2ProBackend:
    """
    Thread-safe streaming client for a dedicated Google Colab Fish Audio S2-Pro server.
    Designed to plug directly into `AruFishBridgeApp` alongside `FishMcpBackend`.
    """

    def __init__(
        self,
        *,
        url_getter: Callable[[], str],
        api_key_getter: Callable[[], str],
        steam_dir_getter: Callable[[], str] | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._url_getter = url_getter
        self._api_key_getter = api_key_getter
        self._steam_dir_getter = steam_dir_getter
        self._log_cb = log_cb

        self._lock = threading.RLock()
        self._client: httpx.Client | None = None
        self._active_base_url: str = ""
        self._registered_ids: set[str] = set()
        self._listed_once_for_url: str = ""
        self._ref_upload_locks: dict[str, threading.Lock] = {}
        self._prewarm_inflight: set[tuple[str, str]] = set()
        # Conservative initial RTF & burst estimates (1.55 = Colab L4 BF16 class, 1.15s = tuned
        # stream_followup_stride=24 vocoder burst) so clause 1 never underruns; automatically
        # adapts downward on FP8 / H100 / B200 and upward on legacy stream_stride=40/45 servers.
        self._ema_rtf: float = 1.55
        self._ema_burst_sec: float = 1.15
        self._validated_url_cache: tuple[str, str, float] | None = None
        self._thread_local = threading.local()

    def should_buffer_full_clause(self, threshold_rtf: float = 0.92) -> bool:
        """
        Return True when the active Colab GPU generates audio slower than real-time
        (`ema_rtf > threshold_rtf`, e.g. Colab L4 at ~1.6x RTF), signaling the player to
        use pre-buffering protection so mid-sentence cutouts never occur.
        """
        with self._lock:
            return self._ema_rtf > float(threshold_rtf)

    def should_fast_split_opening_clause(self, threshold_rtf: float = 0.92) -> bool:
        """
        Return True when splitting a long opening clause into a short Clause #1a (~8-24 chars)
        plus parallel Clause #1b (`batch=2`) will noticeably reduce perceived TTFA.
        """
        with self._lock:
            return self._ema_rtf > float(threshold_rtf)

    def compute_prebuffer_cushion_sec(
        self,
        text: str,
        *,
        speed: float = 1.0,
        target_rtf: float = 0.92,
        safety_margin_sec: float = 0.35,
    ) -> float:
        """
        Compute the mathematically safe minimum audio cushion (seconds) to buffer before
        starting playback for `text` based on rolling `ema_rtf` and observed vocoder burst size.
        Returns `0.0` when `ema_rtf <= target_rtf` for instant streaming.
        """
        with self._lock:
            rtf = self._ema_rtf
            burst_sec = self._ema_burst_sec
        if rtf <= float(target_rtf):
            return 0.0
        est_sec = estimate_s2pro_clause_audio_sec(text, speed=speed)
        return compute_s2pro_prebuffer_cushion_sec(
            est_sec,
            rtf,
            target_rtf=target_rtf,
            safety_margin_sec=safety_margin_sec,
            burst_audio_sec=burst_sec,
        )

    def record_stream_observation(
        self,
        audio_dur_sec: float,
        wall_sec: float,
        *,
        burst_audio_sec: float | None = None,
    ) -> float:
        """Update rolling RTF (and optional vocoder burst duration) EMA from a completed stream."""
        with self._lock:
            if burst_audio_sec is not None:
                try:
                    b_val = float(burst_audio_sec)
                    if math.isfinite(b_val) and 0.35 <= b_val <= 4.0:
                        self._ema_burst_sec = round(0.60 * self._ema_burst_sec + 0.40 * b_val, 3)
                except (TypeError, ValueError):
                    pass
            try:
                dur_val = float(audio_dur_sec)
                wall_val = float(wall_sec)
            except (TypeError, ValueError):
                return self._ema_rtf
            if (not math.isfinite(dur_val)) or (not math.isfinite(wall_val)) or dur_val < 1.0 or wall_val <= 0.05:
                return self._ema_rtf
            obs_rtf = max(0.10, min(6.0, wall_val / dur_val))
            self._ema_rtf = round(0.55 * self._ema_rtf + 0.45 * obs_rtf, 3)
            return self._ema_rtf

    def get_observed_rtf(self) -> float:
        with self._lock:
            return self._ema_rtf

    def get_observed_burst_sec(self) -> float:
        with self._lock:
            return self._ema_burst_sec

    def get_last_tts_request_start(self) -> float:
        return float(getattr(self._thread_local, "last_tts_req_started", 0.0) or 0.0)

    def _log(self, msg: str) -> None:
        if self._log_cb is not None:
            try:
                self._log_cb(redact_secrets(msg, extra_secrets=(self._get_api_key(),)))
            except Exception:
                pass

    def _get_base_url(self) -> str:
        raw_url = str(self._url_getter() or "").strip()
        now_mono = time.monotonic()
        with self._lock:
            cached = self._validated_url_cache
            if cached is not None and cached[0] == raw_url and (now_mono - cached[2]) < 30.0:
                return cached[1]
        validated = validate_colab_server_url(raw_url, resolve_dns=True, fail_on_dns_error=True)
        with self._lock:
            self._validated_url_cache = (raw_url, validated, now_mono)
        return validated

    def _get_api_key(self) -> str:
        return str(self._api_key_getter() or "").strip()

    def is_configured(self) -> bool:
        try:
            url = str(self._url_getter() or "").strip()
            if not url:
                return False
            validate_colab_server_url(url, resolve_dns=False)
            return True
        except ValueError:
            return False

    def _get_client(self) -> httpx.Client:
        with self._lock:
            if self._client is not None and not self._client.is_closed:
                return self._client
            timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
            limits = httpx.Limits(max_connections=8, max_keepalive_connections=6, keepalive_expiry=120.0)
            try:
                self._client = httpx.Client(http2=True, timeout=timeout, limits=limits, follow_redirects=False)
            except ImportError:
                self._client = httpx.Client(timeout=timeout, limits=limits, follow_redirects=False)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
            self._registered_ids.clear()
            self._listed_once_for_url = ""
            self._validated_url_cache = None

    def reset_cache(self) -> None:
        with self._lock:
            self._registered_ids.clear()
            self._listed_once_for_url = ""
            self._validated_url_cache = None

    def _auth_headers(self, *, accept_json: bool = False) -> dict[str, str]:
        hdrs: dict[str, str] = {}
        tok = self._get_api_key()
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
        if accept_json:
            hdrs["Accept"] = "application/json"
        return hdrs

    def ping_health(
        self,
        *,
        override_url: str | None = None,
        override_key: str | None = None,
        timeout: float = 8.0,
    ) -> tuple[bool, float]:
        """Lightweight single-request `/v1/health` keepalive ping (does NOT list references)."""
        try:
            base_url = validate_colab_server_url(override_url if override_url is not None else self._url_getter())
        except ValueError:
            return False, 0.0

        tok = str(override_key if override_key is not None else self._get_api_key()).strip()
        hdrs = {"Accept": "application/json"}
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"

        client = self._get_client()
        t0 = time.perf_counter()
        try:
            resp = client.get(f"{base_url}/v1/health", headers=hdrs, timeout=max(0.5, float(timeout)))
        except httpx.HTTPError:
            return False, 0.0
        rtt_ms = (time.perf_counter() - t0) * 1000.0
        return resp.status_code == 200, rtt_ms

    def request_remote_shutdown(
        self,
        *,
        unassign: bool = True,
        override_url: str | None = None,
        override_key: str | None = None,
        timeout: float = 10.0,
    ) -> tuple[bool, str]:
        """
        Send an authenticated `POST /v1/admin/shutdown` request to gracefully stop the Colab S2-Pro
        server and optionally unassign (`google.colab.runtime.unassign()`) the Colab GPU runtime.
        Returns `(ok, beginner_friendly_korean_message)`.
        """
        raw_url = str(override_url if override_url is not None else self._url_getter() or "").strip()
        if not raw_url:
            return False, "연결된 Colab S2-Pro 서버 주소가 설정되어 있지 않습니다."
        try:
            base_url = validate_colab_server_url(raw_url, resolve_dns=True)
        except ValueError as exc:
            return False, str(exc)

        tok = str(override_key if override_key is not None else self._get_api_key()).strip()
        if not tok:
            return (
                False,
                "인증 토큰(Key)이 설정되어 있지 않아 원격 GPU 반납을 실행할 수 없습니다. "
                "Colab 노트북의 [2단계 셀(GPU 반납)]을 직접 실행해 주세요.",
            )

        hdrs = {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Connection": "close",
        }
        payload = {"unassign": bool(unassign)}
        req_timeout = max(2.0, float(timeout))
        client = self._get_client()

        resp: httpx.Response | None = None
        last_disconnect_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = client.post(
                    f"{base_url}/v1/admin/shutdown",
                    headers=hdrs,
                    json=payload,
                    timeout=req_timeout,
                )
                break
            except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
                last_disconnect_exc = exc
                if attempt == 0:
                    continue
                break
            except httpx.HTTPError as exc:
                if last_disconnect_exc is not None:
                    # Attempt 1 disconnected mid-request; retry failed to connect -> verify liveness below
                    break
                return (
                    False,
                    f"Colab 서버에 연결할 수 없습니다 ({type(exc).__name__}). "
                    "이미 종료되었거나 오프라인 상태일 수 있습니다.",
                )

        if resp is None or (last_disconnect_exc is not None and resp.status_code in (502, 503, 504)):
            # Only report shutdown success after verifying `/v1/health` is actually unreachable!
            alive, _rtt = self.ping_health(
                override_url=base_url,
                override_key=tok,
                timeout=min(4.0, req_timeout),
            )
            if alive:
                return (
                    False,
                    "원격 종료 요청 중 연결이 끊겼지만 Colab 서버가 아직 응답하고 있습니다. "
                    "잠시 후 다시 시도하거나 Colab [2단계 셀(GPU 반납)]을 직접 실행해 주세요.",
                )
            self.reset_cache()
            msg = (
                "Colab 서버가 종료되면서 연결이 해제되었습니다. 원격 GPU 반납이 정상 처리되었습니다!"
                if unassign
                else "Colab 서버가 종료되면서 연결이 해제되었습니다."
            )
            self._log(f"🛑 [Colab S2-Pro 원격 종료] {msg}")
            return True, msg

        if resp.status_code == 200:
            unassign_ack = bool(unassign)
            try:
                body = resp.json()
                if isinstance(body, dict) and "unassign" in body:
                    unassign_ack = bool(body.get("unassign"))
            except Exception:
                pass
            self.reset_cache()
            msg = (
                "Colab S2-Pro 서버 원격 종료 및 GPU 반납 요청을 완료했습니다! (약 5~10초 내 GPU가 자동 반납됩니다)"
                if unassign_ack
                else "Colab S2-Pro 서버 원격 종료 요청을 완료했습니다."
            )
            self._log(f"🛑 [Colab S2-Pro 원격 종료] {msg}")
            return True, msg

        if resp.status_code == 401:
            return (
                False,
                "인증 토큰이 일치하지 않아 원격 종료가 거부되었습니다 (HTTP 401). "
                "설정에서 인증 토큰을 확인하거나 Colab [2단계 셀(GPU 반납)]을 직접 실행해 주세요.",
            )
        if resp.status_code == 403:
            return (
                False,
                "Colab 서버에 인증 토큰이 설정되어 있지 않아 원격 GPU 반납이 차단되었습니다 (HTTP 403). "
                "Colab [2단계 셀(GPU 반납)]을 직접 실행해 주세요.",
            )
        if resp.status_code == 404:
            return (
                False,
                "현재 실행 중인 Colab 서버가 구버전이라 원격 GPU 반납을 지원하지 않습니다 (HTTP 404). "
                "Colab 브라우저 탭에서 [2단계 셀(GPU 반납)]을 직접 실행해 주세요.",
            )
        if resp.status_code in (502, 503, 504):
            return (
                False,
                f"Colab 서버 터널이 이미 닫혔거나 응답하지 않습니다 (HTTP {resp.status_code}). "
                "이미 종료되었을 수 있으며, 확인이 필요하면 Colab 탭에서 [2단계 셀]을 실행해 주세요.",
            )

        safe_err = redact_secrets(resp.text[:MAX_ERROR_BYTES], extra_secrets=(tok,))
        return False, f"원격 GPU 반납 요청 실패 (HTTP {resp.status_code}): {safe_err[:180]}"

    def check_health(
        self,
        *,
        override_url: str | None = None,
        override_key: str | None = None,
        timeout: float = 15.0,
    ) -> tuple[bool, str, float, list[str]]:
        """
        Verify Colab S2-Pro server liveness, auth token, RTT latency, and cached references.
        Returns `(ok, human_message, rtt_ms, registered_ref_ids)`.
        """
        try:
            base_url = validate_colab_server_url(override_url if override_url is not None else self._url_getter())
        except ValueError as exc:
            return False, str(exc), 0.0, []

        tok = str(override_key if override_key is not None else self._get_api_key()).strip()
        hdrs = {"Accept": "application/json"}
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"

        req_timeout = max(3.0, float(timeout))
        client = self._get_client()
        t0 = time.perf_counter()
        try:
            resp = client.get(f"{base_url}/v1/health", headers=hdrs, timeout=req_timeout)
        except httpx.HTTPError as exc:
            return False, f"서버에 연결할 수 없습니다 ({type(exc).__name__})", 0.0, []
        rtt_ms = (time.perf_counter() - t0) * 1000.0

        if resp.status_code in (401, 403):
            return False, f"인증 토큰이 일치하지 않습니다 (HTTP {resp.status_code})", rtt_ms, []
        if resp.status_code != 200:
            return False, f"서버 비정상 응답 (HTTP {resp.status_code})", rtt_ms, []

        ref_ids: list[str] = []
        try:
            r2 = client.get(f"{base_url}/v1/references/list", headers=hdrs, timeout=req_timeout)
            if r2.status_code == 200:
                j = r2.json()
                ref_ids = [str(x) for x in (j.get("reference_ids") or [])]
                if base_url == self._active_base_url or override_url is None:
                    with self._lock:
                        self._active_base_url = base_url
                        self._registered_ids = set(ref_ids)
                        self._listed_once_for_url = base_url
        except Exception:
            pass

        return (
            True,
            f"연결 성공! (핑 {rtt_ms:.0f}ms · 캐시된 캐릭터 {len(ref_ids)}명)",
            rtt_ms,
            ref_ids,
        )

    def _sync_remote_reference_list(
        self,
        base_url: str,
        *,
        api_key_override: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        with self._lock:
            if self._listed_once_for_url == base_url:
                return
        hdrs = {"Accept": "application/json"}
        tok = str(api_key_override if api_key_override is not None else self._get_api_key()).strip()
        if tok:
            hdrs["Authorization"] = f"Bearer {tok}"
        client = self._get_client()
        try:
            resp = client.get(
                f"{base_url}/v1/references/list",
                headers=hdrs,
                timeout=max(3.0, float(timeout)),
            )
            if resp.status_code in (401, 403):
                raise ColabS2ProAuthError(f"Colab S2-Pro 인증 토큰 오류 (HTTP {resp.status_code})")
            if resp.status_code == 200:
                try:
                    j = resp.json()
                except Exception as exc:
                    raise ColabS2ProError(f"Colab S2-Pro 참조 목록 JSON 파싱 오류 ({type(exc).__name__})") from exc
                if not isinstance(j, dict):
                    raise ColabS2ProError("Colab S2-Pro 참조 목록 응답 형식이 올바르지 않습니다.")
                raw_ids = j.get("reference_ids")
                ids = {str(x) for x in raw_ids if str(x).strip()} if isinstance(raw_ids, list) else set()
                with self._lock:
                    if self._active_base_url != base_url:
                        self._registered_ids.clear()
                        self._active_base_url = base_url
                    self._registered_ids.update(ids)
                    self._listed_once_for_url = base_url
        except ColabS2ProError:
            raise
        except httpx.HTTPError as exc:
            raise ColabS2ProUnavailableError(f"Colab S2-Pro 서버 미응답: {type(exc).__name__}") from exc

    def ensure_character_reference(
        self,
        char_id: str,
        reply_lang: str,
        *,
        char_data: dict[str, Any] | None = None,
        override_url: str | None = None,
        override_key: str | None = None,
        _validated_base_url: str | None = None,
    ) -> str:
        """
        Ensure `ba_<char_id>_<LANG>` is registered on the Colab S2-Pro server.
        Uploads the extreme-quality ~38-48s studio reference WAV + verbatim transcript ONCE per session.
        """
        if _validated_base_url is not None:
            base_url = _validated_base_url
        else:
            try:
                base_url = (
                    validate_colab_server_url(override_url, resolve_dns=True, fail_on_dns_error=True)
                    if override_url is not None
                    else self._get_base_url()
                )
            except ValueError as exc:
                if isinstance(exc.__cause__, OSError):
                    raise ColabS2ProUnavailableError(
                        f"Colab S2-Pro 서버가 꺼져 있거나 도메인에 연결할 수 없습니다 ({exc})"
                    ) from exc
                raise
        ref_id = make_s2pro_reference_id(char_id, reply_lang)

        self._sync_remote_reference_list(base_url, api_key_override=override_key)

        with self._lock:
            if self._active_base_url != base_url:
                self._registered_ids.clear()
                self._active_base_url = base_url
            if ref_id in self._registered_ids:
                return ref_id
            ref_lock = self._ref_upload_locks.setdefault(ref_id, threading.Lock())

        with ref_lock:
            with self._lock:
                if ref_id in self._registered_ids:
                    return ref_id

            steam_dir = self._steam_dir_getter() if self._steam_dir_getter else ""
            wav_bytes, ref_text, meta = build_optimal_reference_bundle(
                char_id,
                reply_lang,
                char_data=char_data,
                steam_game_dir=steam_dir,
            )

            hdrs = {"Accept": "application/json"}
            tok = str(override_key if override_key is not None else self._get_api_key()).strip()
            if tok:
                hdrs["Authorization"] = f"Bearer {tok}"

            t0 = time.perf_counter()
            client = self._get_client()
            try:
                resp = client.post(
                    f"{base_url}/v1/references/add",
                    headers=hdrs,
                    data={"id": ref_id, "text": ref_text},
                    files={"audio": (f"{ref_id}.wav", wav_bytes, "audio/wav")},
                    timeout=35.0,
                )
            except httpx.HTTPError as exc:
                raise ColabS2ProUnavailableError(f"참조 음성 업로드 실패 ({type(exc).__name__})") from exc

            if resp.status_code in (401, 403):
                raise ColabS2ProAuthError(f"Colab S2-Pro 인증 토큰 오류 (HTTP {resp.status_code})")
            if resp.status_code in (200, 409):
                with self._lock:
                    if self._active_base_url == base_url:
                        self._registered_ids.add(ref_id)
                if resp.status_code == 200:
                    elapsed = time.perf_counter() - t0
                    self._log(
                        f"☁️ [Colab S2-Pro 음색 등록] {ref_id} ({meta['duration_sec']}s / "
                        f"{meta['clip_count']}클립 · 대본 {meta['transcript_matched']}개 매칭, {elapsed:.2f}s)"
                    )
                return ref_id

            err_txt = redact_secrets(resp.text[:MAX_ERROR_BYTES], extra_secrets=(tok,))
            raise ColabS2ProError(f"참조 음성 등록 실패 (HTTP {resp.status_code}): {err_txt[:220]}")

    def prewarm_character_reference_async(
        self,
        char_id: str,
        reply_lang: str,
        *,
        char_data: dict[str, Any] | None = None,
        override_url: str | None = None,
        override_key: str | None = None,
    ) -> threading.Thread | None:
        """
        Asynchronously pre-register `ba_<char_id>_<LANG>` on the S2-Pro server so the first
        spoken clause avoids the ~1-3s WAV upload & VQ encoding stall. Deduplicates concurrent calls.
        """
        raw_url = str(override_url if override_url is not None else self._url_getter() or "").strip()
        if not raw_url:
            return None
        try:
            norm_url = validate_colab_server_url(raw_url, resolve_dns=False)
        except ValueError:
            return None

        ref_id = make_s2pro_reference_id(char_id, reply_lang)
        inflight_key = (norm_url, ref_id)
        with self._lock:
            if self._listed_once_for_url == norm_url and ref_id in self._registered_ids:
                return None
            if inflight_key in self._prewarm_inflight:
                return None
            self._prewarm_inflight.add(inflight_key)

        def _worker() -> None:
            try:
                self.ensure_character_reference(
                    char_id,
                    reply_lang,
                    char_data=char_data,
                    override_url=override_url,
                    override_key=override_key,
                )
            except Exception:
                # Best-effort background prewarm: keep UI and call startup silent on transient errors
                pass
            finally:
                with self._lock:
                    self._prewarm_inflight.discard(inflight_key)

        th = threading.Thread(target=_worker, daemon=True, name=f"s2pro-prewarm-{ref_id}")
        th.start()
        return th

    @staticmethod
    def _is_missing_reference_error(status_code: int, err_text: str) -> bool:
        if status_code == 404:
            return True
        if status_code in (400, 422):
            low = str(err_text or "").lower()
            if "reference" in low and any(tok in low for tok in ("not found", "missing", "unknown", "does not exist", "invalid")):
                return True
        return False

    def stream_tts_pcm(
        self,
        text: str,
        *,
        char_id: str,
        reply_lang: str,
        char_data: dict[str, Any] | None = None,
        temperature: float = 0.72,
        top_p: float = 0.78,
        repetition_penalty: float = 1.12,
        chunk_length: int = 200,
        max_new_tokens: int = 1280,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Iterator[tuple[bytes, int]]:
        """
        Stream raw 16-bit little-endian mono PCM chunks `(pcm_bytes, sample_rate)`
        directly from the dedicated Colab Fish Audio S2-Pro server.
        """
        clean_text = str(text or "").strip()
        if not clean_text:
            return

        if cancel_check is not None and cancel_check():
            return

        self._thread_local.last_tts_req_started = 0.0
        try:
            base_url = self._get_base_url()
        except ValueError as exc:
            if isinstance(exc.__cause__, OSError):
                raise ColabS2ProUnavailableError(
                    f"Colab S2-Pro 서버가 꺼져 있거나 도메인에 연결할 수 없습니다 ({exc})"
                ) from exc
            raise
        client = self._get_client()

        for attempt in range(2):
            ref_id = self.ensure_character_reference(
                char_id,
                reply_lang,
                char_data=char_data,
                _validated_base_url=base_url,
            )

            if cancel_check is not None and cancel_check():
                return

            try:
                safe_temp = float(temperature)
                if not math.isfinite(safe_temp):
                    safe_temp = 0.72
            except (TypeError, ValueError):
                safe_temp = 0.72
            try:
                safe_top_p = float(top_p)
                if not math.isfinite(safe_top_p):
                    safe_top_p = 0.78
            except (TypeError, ValueError):
                safe_top_p = 0.78
            try:
                safe_rep = float(repetition_penalty)
                if not math.isfinite(safe_rep):
                    safe_rep = 1.12
            except (TypeError, ValueError):
                safe_rep = 1.12
            try:
                safe_chunk_len = int(chunk_length)
            except (TypeError, ValueError):
                safe_chunk_len = 200
            try:
                safe_max_tokens = int(max_new_tokens)
            except (TypeError, ValueError):
                safe_max_tokens = 1280

            payload = {
                "text": clean_text,
                "reference_id": ref_id,
                "format": "wav",
                "streaming": True,
                "use_memory_cache": "on",
                "chunk_length": max(100, min(1000, safe_chunk_len)),
                "normalize": True,
                "temperature": round(max(0.1, min(1.0, safe_temp)), 3),
                "top_p": round(max(0.1, min(1.0, safe_top_p)), 3),
                "repetition_penalty": round(max(0.9, min(2.0, safe_rep)), 3),
                "max_new_tokens": max(256, min(4096, safe_max_tokens)),
            }
            headers = self._auth_headers()
            headers["Content-Type"] = "application/json"

            stripper = StreamingWavHeaderStripper(default_sr=DEFAULT_S2PRO_SR)
            total_bytes = 0
            should_retry_missing_ref = False

            try:
                self._thread_local.last_tts_req_started = time.perf_counter()
                with client.stream("POST", f"{base_url}/v1/tts", headers=headers, json=payload) as resp:
                    if resp.status_code in (401, 403):
                        raise ColabS2ProAuthError(f"Colab S2-Pro 인증 실패 (HTTP {resp.status_code})")
                    if resp.status_code in (502, 503, 504):
                        raise ColabS2ProUnavailableError(
                            f"Colab S2-Pro 터널 응답 없음 (HTTP {resp.status_code} — Colab 셀이 실행 중인지 확인해 주세요)"
                        )
                    if resp.status_code != 200:
                        body = b""
                        for b in resp.iter_bytes(chunk_size=1024):
                            body += b
                            if len(body) >= MAX_ERROR_BYTES:
                                break
                        raw_msg = body[:MAX_ERROR_BYTES].decode("utf-8", errors="replace")
                        if attempt == 0 and self._is_missing_reference_error(resp.status_code, raw_msg):
                            with self._lock:
                                self._registered_ids.discard(ref_id)
                                self._listed_once_for_url = ""
                            self._log(f"🔄 [Colab S2-Pro 참조 재등록] 서버 재시작으로 '{ref_id}'가 누락되어 자동 재등록 후 재시도합니다.")
                            should_retry_missing_ref = True
                        else:
                            msg = redact_secrets(raw_msg, extra_secrets=(self._get_api_key(),))
                            raise ColabS2ProError(f"Colab S2-Pro 합성 오류 (HTTP {resp.status_code}): {msg[:240]}")

                    if not should_retry_missing_ref:
                        for raw_chunk in resp.iter_bytes():
                            if cancel_check is not None and cancel_check():
                                break
                            if not raw_chunk:
                                continue
                            total_bytes += len(raw_chunk)
                            if total_bytes > MAX_STREAM_BYTES:
                                self._log(f"[Colab S2-Pro 보안 경고] 단일 스트림 크기 한도({MAX_STREAM_BYTES}B) 초과로 중단했습니다.")
                                break
                            pcm_bytes = stripper.feed(raw_chunk)
                            if pcm_bytes:
                                yield pcm_bytes, stripper.sample_rate
                        return
            except (ColabS2ProError, ValueError):
                raise
            except httpx.HTTPError as exc:
                raise ColabS2ProUnavailableError(
                    f"Colab S2-Pro 연결 오류 ({type(exc).__name__})"
                ) from exc
