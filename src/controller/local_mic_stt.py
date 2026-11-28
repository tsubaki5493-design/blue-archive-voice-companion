"""
Local Microphone RMS/VAD Detector & Zero-Cost Faster-Whisper STT Engine
-----------------------------------------------------------------------
Captures user microphone speech on 25ms frames, resets the proactive silence timer
immediately on speech onset, and transcribes completed utterances locally using
the shared faster-whisper engine (CUDA INT8 or CPU INT8 fallback, $0 STT cost).
"""

from __future__ import annotations

import math
import queue
import re
import threading
import time
import zlib
from collections import deque
from math import gcd
from typing import Any, Callable


def _safe_finite_float(val: Any, default: float, lo: float, hi: float) -> float:
    try:
        if isinstance(val, bool) or val is None:
            return default
        f = float(val)
        if not math.isfinite(f):
            return default
        return max(lo, min(hi, f))
    except Exception:
        return default


def _safe_int_clamp(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        if isinstance(val, bool) or val is None:
            return default
        f = float(val)
        if not math.isfinite(f):
            return default
        return max(lo, min(hi, int(round(f))))
    except Exception:
        return default


import numpy as np
import sounddevice as sd

from src.controller.proactive_lead_engine import _extract_dev_base_name, _norm_dev_key

_RESAMPLE_POLY_FN: Any = None
_HPF_SOS_CACHE: dict[int, Any] = {}
_HPF_SOSFILT_FN: Any = None
_SILERO_VAD_LOCK = threading.Lock()
_SILERO_VAD_FN: Any = None
_SILERO_VAD_OPTS_CLS: Any = None
_SILERO_VAD_UNAVAILABLE = False
_AC_STD_EPS = 1e-5
_MIN_MIC_SR = 8000
_MAX_MIC_SR = 192000
_MAX_UTTERANCE_SAMPLES = _MAX_MIC_SR * 25
_MAX_PROMPT_HINT_CHARS = 512
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")

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


def _clamp_sample_rate(sr: Any, default: int = 44100) -> int:
    try:
        v = int(sr or default)
    except Exception:
        v = default
    return max(_MIN_MIC_SR, min(_MAX_MIC_SR, v))


def _sanitize_prompt_hint(raw: Any) -> str:
    s = _CTRL_CHARS_RE.sub(" ", str(raw or "")).strip()
    return s[:_MAX_PROMPT_HINT_CHARS]


def _resample_1d(audio: np.ndarray, up: int, down: int) -> np.ndarray:
    """Lazy-import `scipy.signal.resample_poly` on first non-16kHz mic utterance, with numpy linear fallback."""
    global _RESAMPLE_POLY_FN
    if _RESAMPLE_POLY_FN is None:
        try:
            from scipy.signal import resample_poly as _sp_resample_poly

            _RESAMPLE_POLY_FN = _sp_resample_poly
        except Exception:
            if audio.size == 0 or up <= 0 or down <= 0 or up == down:
                return audio.astype(np.float32, copy=False)
            out_len = max(1, int(round(audio.size * (float(up) / float(down)))))
            src_x = np.linspace(0.0, 1.0, audio.size, endpoint=False, dtype=np.float64)
            dst_x = np.linspace(0.0, 1.0, out_len, endpoint=False, dtype=np.float64)
            return np.interp(dst_x, src_x, audio.astype(np.float64)).astype(np.float32)
    return _RESAMPLE_POLY_FN(audio, up, down).astype(np.float32)


WHISPER_SR = 16000


def _apply_dc_and_hpf(audio: np.ndarray, sr: int = WHISPER_SR, cutoff_hz: float = 80.0) -> np.ndarray:
    """Remove DC offset and apply an 80Hz high-pass filter on real AC signals only."""
    if audio.size == 0 or float(np.std(audio)) <= _AC_STD_EPS:
        return audio.astype(np.float32, copy=False)

    centered = (audio - float(np.mean(audio))).astype(np.float32, copy=False)
    if centered.size < 4 or sr <= int(cutoff_hz * 2.5):
        return centered

    global _HPF_SOSFILT_FN
    try:
        sos = _HPF_SOS_CACHE.get(sr)
        if sos is None or _HPF_SOSFILT_FN is None:
            from scipy.signal import butter as _sp_butter, sosfilt as _sp_sosfilt

            nyq = 0.5 * float(sr)
            wn = max(1e-4, min(0.95, cutoff_hz / nyq))
            sos = _sp_butter(2, wn, btype="highpass", output="sos")
            _HPF_SOS_CACHE[sr] = sos
            _HPF_SOSFILT_FN = _sp_sosfilt
        filtered = _HPF_SOSFILT_FN(sos, centered).astype(np.float32, copy=False)
    except Exception:
        # Lightweight 1-pole IIR high-pass fallback if scipy is unavailable
        rc = 1.0 / (2.0 * math.pi * cutoff_hz)
        dt = 1.0 / float(sr)
        alpha = rc / (rc + dt)
        filtered = np.empty_like(centered, dtype=np.float32)
        y_prev = 0.0
        x_prev = float(centered[0])
        filtered[0] = 0.0
        for idx in range(1, centered.size):
            x_cur = float(centered[idx])
            y_prev = alpha * (y_prev + x_cur - x_prev)
            filtered[idx] = y_prev
            x_prev = x_cur

    if filtered.size and not np.all(np.isfinite(filtered)):
        filtered = np.nan_to_num(filtered, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
    return filtered


def _normalize_loudness(
    audio: np.ndarray,
    sr: int = WHISPER_SR,
    target_rms: float = 0.08,
    max_gain: float = 12.0,
    limiter_knee: float = 0.92,
) -> np.ndarray:
    """RMS-aware active-speech loudness normalization with smooth soft peak limiting."""
    if audio.size == 0:
        return audio.astype(np.float32, copy=False)

    arr = audio.astype(np.float32, copy=False)
    frame_n = max(1, int(sr * 0.02))
    n_frames = arr.size // frame_n
    if n_frames >= 2:
        framed = arr[: n_frames * frame_n].reshape(n_frames, frame_n)
        frame_rms = np.sqrt(np.mean(framed * framed, axis=1))
        ref_frm = float(np.percentile(frame_rms, 85)) if frame_rms.size else 0.0
        active_mask = frame_rms >= max(0.004, 0.25 * ref_frm)
        if np.any(active_mask):
            active_rms = float(np.median(frame_rms[active_mask]))
        else:
            active_rms = float(np.sqrt(np.mean(arr * arr)))
    else:
        active_rms = float(np.sqrt(np.mean(arr * arr)))

    if not math.isfinite(active_rms) or active_rms <= 1e-6:
        gain = 1.0
    elif active_rms < target_rms:
        gain = min(target_rms / active_rms, max_gain)
    elif active_rms > 0.22:
        gain = max(0.16 / active_rms, 0.35)
    else:
        gain = 1.0

    scaled = (arr * np.float32(gain)) if gain != 1.0 else arr
    ceiling = 0.99
    headroom = max(1e-4, ceiling - limiter_knee)
    abs_x = np.abs(scaled)
    over = abs_x > limiter_knee
    if np.any(over):
        excess = abs_x[over] - limiter_knee
        limited = limiter_knee + headroom * np.tanh(excess / headroom)
        scaled = scaled.copy()
        scaled[over] = np.sign(scaled[over]) * limited

    return np.clip(scaled, -1.0, 1.0).astype(np.float32, copy=False)


def _run_silero_vad_trim(audio_16k: np.ndarray, pad_ms: int = 250) -> tuple[np.ndarray | None, bool]:
    """
    Confirm real speech and trim excessive leading/trailing silence using bundled Silero VAD.
    Returns `(trimmed_audio_or_None, vad_was_applied)`.
    Falls back gracefully to `(audio_16k, False)` if Silero VAD is unavailable.
    """
    global _SILERO_VAD_FN, _SILERO_VAD_OPTS_CLS, _SILERO_VAD_UNAVAILABLE
    if _SILERO_VAD_UNAVAILABLE or audio_16k.size == 0:
        return audio_16k, False

    if _SILERO_VAD_FN is None or _SILERO_VAD_OPTS_CLS is None:
        with _SILERO_VAD_LOCK:
            if _SILERO_VAD_UNAVAILABLE:
                return audio_16k, False
            if _SILERO_VAD_FN is None or _SILERO_VAD_OPTS_CLS is None:
                try:
                    from faster_whisper.vad import VadOptions as _fw_vad_opts
                    from faster_whisper.vad import get_speech_timestamps as _fw_get_speech_ts

                    _SILERO_VAD_FN = _fw_get_speech_ts
                    _SILERO_VAD_OPTS_CLS = _fw_vad_opts
                except Exception:
                    _SILERO_VAD_UNAVAILABLE = True
                    return audio_16k, False

    try:
        safe_pad_ms = max(0, min(1000, int(pad_ms)))
        vad_in = audio_16k.astype(np.float32, copy=False)
        if not np.all(np.isfinite(vad_in)):
            vad_in = np.nan_to_num(vad_in, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
        peak = float(np.max(np.abs(vad_in))) if vad_in.size else 0.0
        if 0.0 < peak < 0.15:
            vad_gain = min(10.0, 0.30 / peak)
            vad_in = np.clip(vad_in * np.float32(vad_gain), -1.0, 1.0).astype(np.float32, copy=False)

        vad_opts = _SILERO_VAD_OPTS_CLS(
            threshold=0.32,
            neg_threshold=0.18,
            min_speech_duration_ms=90,
            min_silence_duration_ms=300,
            speech_pad_ms=safe_pad_ms,
        )
        with _SILERO_VAD_LOCK:
            speeches = _SILERO_VAD_FN(vad_in, vad_options=vad_opts, sampling_rate=WHISPER_SR)
        if not speeches:
            return None, True
        start = max(0, int(speeches[0].get("start", 0)))
        end = min(len(audio_16k), int(speeches[-1].get("end", len(audio_16k))))

        # Active-speech envelope guard: once Silero confirms real speech exists,
        # check 20ms frame RMS outside [start, end] so expressive/high-pitched
        # speech onsets or offsets are never chopped off while isolated clicks
        # and steady background noise stay excluded.
        frame_n = max(1, int(WHISPER_SR * 0.02))
        n_frames = vad_in.size // frame_n
        if n_frames >= 6 and end > start:
            framed = vad_in[: n_frames * frame_n].reshape(n_frames, frame_n)
            frame_rms = np.sqrt(np.mean(framed * framed, axis=1))
            s_idx = max(0, min(n_frames - 1, start // frame_n))
            e_idx = max(s_idx + 1, min(n_frames, (end + frame_n - 1) // frame_n))
            in_rms = frame_rms[s_idx:e_idx]
            speech_ref = float(np.percentile(in_rms, 75)) if in_rms.size else 0.0
            noise_floor = float(np.percentile(frame_rms, 15))
            if math.isfinite(speech_ref) and math.isfinite(noise_floor) and speech_ref > 0.0:
                expand_thresh = max(0.012, speech_ref * 0.30, noise_floor * 2.5)
                active = frame_rms >= expand_thresh
                # Require >= 3 consecutive 20ms frames (60ms) so short clicks/bumps never expand bounds
                sustained = active[:-2] & active[1:-1] & active[2:]
                hit_idx = np.flatnonzero(sustained)
                if hit_idx.size:
                    pad_samples = int(WHISPER_SR * (safe_pad_ms / 1000.0))
                    first_s = max(0, int(hit_idx[0]) * frame_n - pad_samples)
                    last_e = min(len(audio_16k), int(hit_idx[-1] + 3) * frame_n + pad_samples)
                    if first_s < start:
                        start = first_s
                    if last_e > end:
                        end = last_e

        if end - start >= int(WHISPER_SR * 0.08):
            return audio_16k[start:end], True
        return audio_16k, True
    except Exception:
        return audio_16k, False


def _resolve_suppressed_tokens(model: Any, tok: Any) -> list[int]:
    """Resolve suppressed token IDs across mock Whisper models and faster-whisper 1.2.x."""
    if hasattr(model, "get_suppressed_tokens"):
        try:
            res = model.get_suppressed_tokens(tok, [-1])
            if res is not None:
                return [int(t) for t in res]
        except Exception:
            pass
    try:
        from faster_whisper.transcribe import get_suppressed_tokens as _fw_get_suppressed_tokens

        res = _fw_get_suppressed_tokens(tok, [-1])
        if res is not None:
            return [int(t) for t in res]
    except Exception:
        pass
    try:
        non_speech = getattr(tok, "non_speech_tokens", ())
        if non_speech:
            return [int(t) for t in non_speech]
    except Exception:
        pass
    return []


def _is_128mel_whisper_model(model: Any, model_name: str = "") -> bool:
    """
    Detect 128-mel Whisper v3 / turbo models (`large-v3-turbo`, `large-v3`).
    These models must use standard 30s mel windows with timestamp tokens enabled
    to avoid short-mel `<|endoftext|>` collapse and repetition loops.
    """
    inner = getattr(model, "model", None)
    fe = getattr(model, "feature_extractor", None)
    for raw_n in (
        getattr(inner, "n_mels", None),
        getattr(model, "n_mels", None),
        getattr(fe, "feature_size", None),
        getattr(fe, "n_mels", None),
    ):
        if isinstance(raw_n, int) and not isinstance(raw_n, bool) and raw_n >= 128:
            return True
    mel_filters = getattr(fe, "mel_filters", None)
    shape = getattr(mel_filters, "shape", None)
    if isinstance(shape, tuple) and shape and isinstance(shape[0], int) and not isinstance(shape[0], bool):
        if shape[0] >= 128:
            return True
    low_name = str(model_name or "").strip().lower()
    if low_name and ("large-v3" in low_name or "turbo" in low_name or "v3" in low_name):
        return True
    return False


def _text_compression_ratio(text: str) -> float:
    """Compute Whisper-style UTF-8 zlib compression ratio to catch runaway repetition loops."""
    raw = (text or "").encode("utf-8")
    if len(raw) < 24:
        return 0.0
    comp = zlib.compress(raw)
    return float(len(raw)) / float(max(1, len(comp)))


MIC_HALLUCINATIONS = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "チャンネル登録よろしくお願いします",
    "字幕",
    "시청해 주셔서 감사합니다",
    "시청해 주셔서 감사합니다.",
    "시청해주셔서 감사합니다.",
    "구독과 좋아요 부탁드립니다",
    "구독과 좋아요 부탁드립니다.",
    "감사합니다.",
    "MBC 뉴스 김수근입니다.",
    "자막 제공",
    "you",
    "You",
    "you.",
    "Thank you.",
    "Thank you",
    "...",
    ".",
}


_VIRTUAL_LOOPBACK_MIC_KEYWORDS: tuple[str, ...] = (
    "cable",
    "vb-audio",
    "stereo mix",
    "스테레오 믹스",
)
_EXCLUDED_SYSTEM_MIC_KEYWORDS: tuple[str, ...] = (
    "sound mapper",
    "사운드 매퍼",
    "주 사운드",
    "주 음성",
    "wdm-ks",
)


def is_virtual_loopback_mic_label(label: str) -> bool:
    """Return True if an audio input label represents a virtual cable or loopback mixer."""
    low = str(label or "").lower()
    return any(k in low for k in _VIRTUAL_LOOPBACK_MIC_KEYWORDS)


def list_microphone_labels(inputs: list[tuple[int, str, int]]) -> list[str]:
    """
    Return deduplicated physical microphone input labels (MME -> DirectSound -> WASAPI),
    excluding VB-Cable, Sound Mapper, WDM-KS, and Stereo Mix (even in fallback).
    """
    excluded_all = _VIRTUAL_LOOPBACK_MIC_KEYWORDS + _EXCLUDED_SYSTEM_MIC_KEYWORDS
    best_by_key: dict[str, tuple[tuple[int, int, int, int], str]] = {}
    for idx, label, _ in inputs:
        low = label.lower()
        if any(k in low for k in excluded_all):
            continue
        base = _extract_dev_base_name(label)
        norm_key = _norm_dev_key(label)
        if not norm_key:
            continue
        is_mic = 1 if any(k in low for k in ("mic", "마이크", "headset", "이어폰", "헤드셋", "webcam", "usb", "array")) else 0
        # MME & DirectSound are far more reliable than Windows WASAPI for USB mics in PortAudio
        api_score = 3 if "mme" in low else (2 if "directsound" in low else 1)
        score = (is_mic, api_score, len(base), -idx)
        prev = best_by_key.get(norm_key)
        if prev is None or score > prev[0]:
            best_by_key[norm_key] = (score, label)

    if best_by_key:
        ranked = sorted(best_by_key.values(), key=lambda x: x[0], reverse=True)
        return [lbl for _, lbl in ranked]

    # Fallback when no named physical mic matched: still NEVER promote VB-Cable / Stereo Mix / mappers
    fallback = [
        lbl
        for _, lbl, _ in inputs
        if not any(k in lbl.lower() for k in excluded_all)
    ]
    return fallback


_PAREN_SUFFIX_RE = re.compile(r"\s*[(（][^()（）]*[)）]")
_NORM_HINT_ECHO_RE = re.compile(r"[\s,，、.。!！?？/／·・]+")
_HINT_TERM_SPLIT_RE = re.compile(r"[,，、]+")
_WS_MULTI_RE = re.compile(r"\s+")
_JP_WS_SPLIT_RE = re.compile(r"[\s　]+")
_JP_KANJI_KANA_GIVEN_RE = re.compile(r"[\u4e00-\u9fff]([\u30a0-\u30ffー]{2,}|[\u3040-\u309fー]{2,})$")


def _norm_hint_echo(text: str) -> str:
    return _NORM_HINT_ECHO_RE.sub(" ", (text or "").strip()).strip().lower()


def _clean_proper_noun(raw: Any, max_len: int = 24) -> str:
    s = _PAREN_SUFFIX_RE.sub("", str(raw or "")).strip()
    if "/" in s or "／" in s:
        s = re.split(r"[/／]", s, maxsplit=1)[0].strip()
    s = _WS_MULTI_RE.sub(" ", s).strip()
    if not s or len(s) > max_len:
        return ""
    return s


def _split_hint_terms(hint_str: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in _HINT_TERM_SPLIT_RE.split(hint_str or ""):
        norm = _norm_hint_echo(part)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _is_cjk_or_kana(ch: str) -> bool:
    code = ord(ch)
    return (0x3040 <= code <= 0x30FF) or (0x4E00 <= code <= 0x9FFF)


def _is_composed_of_hint_terms(norm: str, terms: set[str]) -> bool:
    """
    Return True iff `norm` can be completely segmented into >= 2 distinct hint terms
    separated by punctuation/whitespace (or directly adjacent CJK/Kana characters).
    Single hint terms (e.g. "히카리", "타치바나 히카리") and repeated single terms
    (e.g. "히카리, 히카리") return False.
    """
    if not norm or not terms or norm in terms:
        return False

    n = len(norm)
    # dp[i] holds:
    # - None: unreachable
    # - "MULTI": reachable via >= 2 distinct terms
    # - set[str]: reachable via 1 distinct term (each element is that single term, or "" at i=0)
    dp: list[str | set[str] | None] = [None] * (n + 1)
    dp[0] = {""}
    term_list = sorted(terms, key=len, reverse=True)

    for i in range(n):
        cur = dp[i]
        if cur is None:
            continue
        for term in term_list:
            tlen = len(term)
            j = i + tlen
            if j > n or not norm.startswith(term, i):
                continue
            if j == n:
                next_i = n
            elif norm[j] == " ":
                next_i = j + 1
            elif tlen >= 2 and _is_cjk_or_kana(norm[j - 1]) and _is_cjk_or_kana(norm[j]):
                next_i = j
            else:
                continue

            if cur == "MULTI":
                dp[next_i] = "MULTI"
            else:
                # cur is a set of single-term strings (or {""} at start)
                target = dp[next_i]
                if target == "MULTI":
                    continue
                is_multi = False
                new_singles: set[str] = set(target) if isinstance(target, set) else set()
                for prev_term in cur:
                    if prev_term == "" or prev_term == term:
                        new_singles.add(term)
                    else:
                        is_multi = True
                        break
                dp[next_i] = "MULTI" if is_multi else new_singles

    return dp[n] == "MULTI"


def build_character_mic_hints(char_data: dict[str, Any] | None) -> tuple[str, str, set[str]]:
    """
    Build compact Korean/Japanese proper-noun STT prompt hints and exact multi-term glossary
    echo filters from a character config without blocking legitimate short utterances.
    """
    if not isinstance(char_data, dict):
        return "", "", set()

    def _dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            s = _WS_MULTI_RE.sub(" ", str(item or "")).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    raw_name_kr = _PAREN_SUFFIX_RE.sub("", str(char_data.get("name_kr") or "")).strip()
    kr_parts = [p for p in raw_name_kr.split() if p]
    given_kr = kr_parts[-1] if (len(kr_parts) >= 2 and len(kr_parts[-1]) >= 2) else ""
    school_kr = _clean_proper_noun(char_data.get("school"))
    club_kr = _clean_proper_noun(char_data.get("club"))
    user_title_kr = str(char_data.get("user_title") or "선생님").strip()

    raw_name_jp = _PAREN_SUFFIX_RE.sub("", str(char_data.get("name_jp") or "")).strip()
    name_jp_compact = _WS_MULTI_RE.sub("", raw_name_jp)
    jp_parts = [p for p in _JP_WS_SPLIT_RE.split(raw_name_jp) if p]
    given_jp = jp_parts[-1] if (len(jp_parts) >= 2 and len(jp_parts[-1]) >= 2) else ""
    if not given_jp and name_jp_compact:
        m_given = _JP_KANJI_KANA_GIVEN_RE.search(name_jp_compact)
        if m_given:
            given_jp = m_given.group(1)
    school_jp = _clean_proper_noun(char_data.get("school_jp"))
    club_jp = _clean_proper_noun(char_data.get("club_jp"))
    user_title_jp = str(char_data.get("user_title_jp") or "先生").strip()

    ko_terms = _dedupe([user_title_kr, "샬레", raw_name_kr, given_kr, school_kr, club_kr, "키보토스"])
    ja_terms = _dedupe([user_title_jp, "シャーレ", name_jp_compact, given_jp, school_jp, club_jp, "キヴォトス"])

    ko_hint = _sanitize_prompt_hint(", ".join(ko_terms))
    ja_hint = _sanitize_prompt_hint("、".join(ja_terms))

    echo_phrases: set[str] = set()
    for sep, terms in ((", ", ko_terms), ("、", ja_terms)):
        if len(terms) >= 2:
            for k in range(2, len(terms) + 1):
                phrase = sep.join(terms[:k])
                norm = _norm_hint_echo(phrase)
                if norm:
                    echo_phrases.add(norm)

    return ko_hint, ja_hint, echo_phrases


class LocalMicSttEngine:
    """Real-time 25ms RMS/VAD capture + zero-cost faster-whisper transcription worker."""

    def __init__(
        self,
        get_whisper_model: Callable[[], Any],
        get_tokenizers: Callable[[], dict[str, Any]],
        is_listening_allowed: Callable[[], bool],
        on_speech_onset: Callable[[], None],
        on_transcribed: Callable[[str], None],
        on_empty: Callable[[], None],
        on_meter: Callable[[float, float, bool], None],
        on_error: Callable[[str], None],
        log_cb: Callable[[str], None],
        is_playback_active: Callable[[], bool] | None = None,
        on_barge_in_duck: Callable[[bool], None] | None = None,
        on_stream_open: Callable[[], None] | None = None,
        on_stream_close: Callable[[], None] | None = None,
    ) -> None:
        self._get_whisper = get_whisper_model
        self._get_tokenizers = get_tokenizers
        self._is_listening_allowed = is_listening_allowed
        self._on_speech_onset = on_speech_onset
        self._on_transcribed = on_transcribed
        self._on_empty = on_empty
        self._on_meter = on_meter
        self._on_error = on_error
        self._log = log_cb
        self._is_playback_active = is_playback_active
        self._on_barge_in_duck = on_barge_in_duck
        self._on_stream_open = on_stream_open
        self._on_stream_close = on_stream_close

        self.running = False
        self.last_utterance_during_playback = False
        self._rms_threshold = 0.010
        self._silence_end_sec = 0.68
        self._stt_language = "ko_auto"
        self._beam_size: int | None = None
        self._use_silero_vad: bool = True
        self._hint_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cap_thread: threading.Thread | None = None
        self._ko_prompt_hint = ""
        self._ja_prompt_hint = ""
        self._hint_echo_phrases: set[str] = set()
        self._hint_terms: set[str] = set()
        self._hint_token_cache: dict[tuple[str, str], list[int]] = {}
        self._audio_queue: queue.Queue[tuple[np.ndarray, int, bool]] = queue.Queue(maxsize=8)
        self._threads: list[threading.Thread] = []

    def _emit_duck(self, active: bool, *, ev: threading.Event | None = None) -> None:
        if ev is not None and self._stop_event is not ev:
            return
        if self._on_barge_in_duck is not None:
            try:
                self._on_barge_in_duck(active)
            except Exception:
                pass

    def _update_echo_phrases(self, ko_hint: str, ja_hint: str, extra_echo: set[str] | None = None) -> None:
        echoes: set[str] = set(extra_echo or ())
        terms: set[str] = set()
        for raw_h in (ko_hint, ja_hint):
            split_terms = _split_hint_terms(raw_h)
            terms.update(split_terms)
            if len(split_terms) >= 2:
                norm = _norm_hint_echo(raw_h)
                if norm:
                    echoes.add(norm)
        self._hint_echo_phrases = echoes
        self._hint_terms = terms

    def set_character_context(self, char_data: dict[str, Any] | None) -> None:
        """Update Korean/Japanese proper-noun STT hints for the active character."""
        ko_hint, ja_hint, echoes = build_character_mic_hints(char_data)
        with self._hint_lock:
            if ko_hint != self._ko_prompt_hint or ja_hint != self._ja_prompt_hint:
                self._ko_prompt_hint = ko_hint
                self._ja_prompt_hint = ja_hint
                self._hint_token_cache.clear()
            self._update_echo_phrases(ko_hint, ja_hint, echoes)

    def _get_prompt_hint_tokens(self, lang: str, tok: Any) -> list[int]:
        with self._hint_lock:
            hint_str = self._ja_prompt_hint if lang == "ja" else self._ko_prompt_hint
            if not hint_str:
                return []
            key = (lang, hint_str)
            cached = self._hint_token_cache.get(key)
            if cached is not None:
                return list(cached)
        try:
            toks = list(tok.encode(" " + hint_str))[:40]
        except Exception:
            toks = []
        with self._hint_lock:
            if (lang == "ja" and self._ja_prompt_hint == hint_str) or (lang != "ja" and self._ko_prompt_hint == hint_str):
                self._hint_token_cache[key] = toks
        return list(toks)

    def _is_glossary_echo(self, text: str) -> bool:
        stripped = (text or "").strip(" .,!?。、")
        if not stripped:
            return False
        norm = _norm_hint_echo(stripped)
        if not norm:
            return False
        with self._hint_lock:
            if norm in self._hint_terms:
                return False
            if norm in self._hint_echo_phrases:
                return True
            return _is_composed_of_hint_terms(norm, self._hint_terms)

    def _resolve_beam_size(self, model: Any) -> int:
        if self._beam_size is not None:
            return max(1, min(5, int(self._beam_size)))
        inner = getattr(model, "model", None)
        dev = str(getattr(inner, "device", "") or "").lower()
        if "cuda" in dev:
            return 3
        return 1

    def configure(
        self,
        rms_threshold: float | None = None,
        silence_end_sec: float | None = None,
        stt_language: str | None = None,
        ko_prompt_hint: str | None = None,
        ja_prompt_hint: str | None = None,
        beam_size: int | None = None,
        use_silero_vad: bool | None = None,
    ) -> None:
        if rms_threshold is not None:
            self._rms_threshold = _safe_finite_float(rms_threshold, self._rms_threshold, 0.002, 0.15)
        if silence_end_sec is not None:
            self._silence_end_sec = _safe_finite_float(silence_end_sec, self._silence_end_sec, 0.35, 2.5)
        if stt_language is not None:
            self._stt_language = str(stt_language).strip() or "ko_auto"
        if beam_size is not None:
            parsed_beam = _safe_int_clamp(beam_size, -1, 1, 5)
            if parsed_beam >= 1:
                self._beam_size = parsed_beam
        if use_silero_vad is not None:
            self._use_silero_vad = bool(use_silero_vad)
        if ko_prompt_hint is not None or ja_prompt_hint is not None:
            with self._hint_lock:
                if ko_prompt_hint is not None:
                    self._ko_prompt_hint = _sanitize_prompt_hint(ko_prompt_hint)
                if ja_prompt_hint is not None:
                    self._ja_prompt_hint = _sanitize_prompt_hint(ja_prompt_hint)
                self._hint_token_cache.clear()
                self._update_echo_phrases(self._ko_prompt_hint, self._ja_prompt_hint)

    def start(
        self,
        dev_idx: int | None,
        dev_sr: int,
        dev_label: str,
        fallback_candidates: list[tuple[int | None, int, str]] | None = None,
    ) -> None:
        with self._state_lock:
            if self.running:
                return
            stop_ev = threading.Event()
            audio_q: queue.Queue[tuple[np.ndarray, int, bool]] = queue.Queue(maxsize=8)
            self._stop_event = stop_ev
            self._audio_queue = audio_q
            self.running = True

            candidates: list[tuple[int | None, int, str]] = []
            seen_idx: set[int | None] = set()
            for raw_idx, raw_sr, raw_lbl in [(dev_idx, _clamp_sample_rate(dev_sr, 44100), dev_label)] + list(fallback_candidates or []):
                if raw_idx in seen_idx:
                    continue
                seen_idx.add(raw_idx)
                candidates.append((raw_idx, _clamp_sample_rate(raw_sr, 44100), str(raw_lbl)))

            t_cap = threading.Thread(
                target=self._capture_loop,
                args=(candidates, stop_ev, audio_q),
                daemon=True,
                name="local-mic-capture",
            )
            t_stt = threading.Thread(
                target=self._stt_worker,
                args=(stop_ev, audio_q),
                daemon=True,
                name="local-mic-stt",
            )
            self._cap_thread = t_cap
            self._threads = [t_cap, t_stt]
        t_cap.start()
        t_stt.start()

    def stop(self) -> None:
        with self._state_lock:
            self.running = False
            stop_ev = self._stop_event
            old_q = self._audio_queue
            stop_ev.set()
            cap_t = self._cap_thread
            self._cap_thread = None
        self._emit_duck(False, ev=stop_ev)
        while not old_q.empty():
            try:
                old_q.get_nowait()
            except queue.Empty:
                break
        if cap_t is not None and cap_t.is_alive() and cap_t is not threading.current_thread():
            try:
                cap_t.join(timeout=0.6)
            except Exception:
                pass

    def _capture_loop(
        self,
        candidates: list[tuple[int | None, int, str]],
        stop_ev: threading.Event | None = None,
        audio_q: queue.Queue[tuple[np.ndarray, int, bool]] | None = None,
    ) -> None:
        ev = stop_ev if stop_ev is not None else self._stop_event
        q = audio_q if audio_q is not None else self._audio_queue
        block_dur = 0.025
        pre_roll: deque[np.ndarray] = deque(maxlen=14)
        speech_frames: list[np.ndarray] = []
        speaking = False
        onset_hits = 0
        voiced_sec = 0.0
        silence_sec = 0.0
        cb_count = 0
        utterance_had_pb = False
        ducked = False
        noise_floor = 0.0
        noise_frames = 0

        def _reset_buffers(clear_duck: bool = True) -> None:
            nonlocal speaking, onset_hits, voiced_sec, silence_sec, utterance_had_pb, ducked
            speech_frames.clear()
            speaking = False
            onset_hits = 0
            voiced_sec = 0.0
            silence_sec = 0.0
            utterance_had_pb = False
            if clear_duck and ducked:
                ducked = False
                self._emit_duck(False, ev=ev)

        def _callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            nonlocal speaking, onset_hits, voiced_sec, silence_sec, cb_count, utterance_had_pb, ducked
            nonlocal noise_floor, noise_frames

            if ev.is_set() or not self.running:
                return

            if indata.ndim > 1:
                mono = indata[:, 0] if indata.shape[1] == 1 else np.mean(indata, axis=1, dtype=np.float32)
            else:
                mono = indata
            if mono.size and not np.all(np.isfinite(mono)):
                mono = np.nan_to_num(mono, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
            if mono.size:
                if float(np.std(mono)) > _AC_STD_EPS:
                    mono_ac = mono - float(np.mean(mono))
                else:
                    mono_ac = mono
                rms = float(np.sqrt(float(np.dot(mono_ac, mono_ac)) / mono_ac.size))
                peak = float(np.max(np.abs(mono_ac)))
                if not math.isfinite(rms):
                    rms = 0.0
                if not math.isfinite(peak):
                    peak = 0.0
            else:
                rms = 0.0
                peak = 0.0

            cb_count += 1
            if cb_count % 4 == 0:
                try:
                    self._on_meter(rms, peak, speaking)
                except Exception:
                    pass

            try:
                listening_allowed = bool(self._is_listening_allowed())
            except Exception:
                listening_allowed = False
            if not listening_allowed:
                if speaking:
                    _reset_buffers(clear_duck=True)
                    try:
                        self._on_empty()
                    except Exception:
                        pass
                pre_roll.clear()
                onset_hits = 0
                return

            try:
                pb_active = bool(self._is_playback_active()) if self._is_playback_active is not None else False
            except Exception:
                pb_active = False
            if not speaking and not pb_active and onset_hits == 0 and rms < self._rms_threshold:
                if noise_frames == 0:
                    noise_floor = rms
                else:
                    alpha = 0.12 if noise_frames < 20 else (0.04 if rms < noise_floor else 0.015)
                    noise_floor = (1.0 - alpha) * noise_floor + alpha * rms
                noise_frames = min(noise_frames + 1, 1000)

            base_thresh = self._rms_threshold
            if noise_frames >= 20 and noise_floor > 0.0025:
                base_thresh = max(self._rms_threshold, min(self._rms_threshold * 1.35, noise_floor * 1.35))

            if pb_active and not speaking:
                onset_thresh = max(base_thresh * 1.35, base_thresh + 0.004)
                req_onset = 3
            else:
                onset_thresh = base_thresh
                req_onset = 2
            if speaking:
                sustain_thresh = onset_thresh * 0.60
                if noise_frames >= 20 and noise_floor > 0.0:
                    sustain_thresh = max(sustain_thresh, min(onset_thresh * 0.90, noise_floor * 1.15))
                eff_thresh = sustain_thresh
            else:
                eff_thresh = onset_thresh

            if rms >= eff_thresh:
                silence_sec = 0.0
                if not speaking:
                    onset_hits += 1
                    pre_roll.append(mono.copy())
                    if onset_hits >= req_onset:
                        speaking = True
                        utterance_had_pb = pb_active
                        ducked = False
                        speech_frames.extend(pre_roll)
                        pre_roll.clear()
                        voiced_sec = onset_hits * block_dur
                        try:
                            self._on_speech_onset()
                        except Exception:
                            pass
                    return
                speech_frames.append(mono.copy())
                voiced_sec += block_dur
                utterance_had_pb = utterance_had_pb or pb_active
                if utterance_had_pb and voiced_sec >= 0.25 and not ducked:
                    ducked = True
                    self._emit_duck(True, ev=ev)
            else:
                onset_hits = 0
                if not speaking:
                    pre_roll.append(mono.copy())
                    return
                speech_frames.append(mono.copy())
                silence_sec += block_dur
                utterance_had_pb = utterance_had_pb or pb_active

            total_dur = len(speech_frames) * block_dur
            if speaking and (silence_sec >= self._silence_end_sec or total_dur >= 18.0):
                if voiced_sec >= 0.16 and speech_frames:
                    full = np.concatenate(speech_frames)
                    item = (full, active_sr, utterance_had_pb)
                    if q.full():
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                    enqueued = False
                    try:
                        q.put_nowait(item)
                        enqueued = True
                    except queue.Full:
                        pass
                    if enqueued:
                        _reset_buffers(clear_duck=False)
                    else:
                        _reset_buffers(clear_duck=True)
                        try:
                            self._on_empty()
                        except Exception:
                            pass
                else:
                    _reset_buffers(clear_duck=True)
                    try:
                        self._on_empty()
                    except Exception:
                        pass

        opened = False
        last_exc: Exception | None = None
        active_sr = 44100
        primary_lbl = candidates[0][2] if candidates else "기본 마이크"

        def _run_candidate_stream(
            cand_idx: int | None,
            cand_sr: int,
            cand_lbl: str,
        ) -> tuple[bool, bool, Exception | None]:
            """
            Try opening and holding `sd.InputStream` for one device candidate.
            Returns `(opened_ok, stopped_cleanly, error)`.
            """
            nonlocal active_sr
            sr_candidates = list(dict.fromkeys([_clamp_sample_rate(cand_sr, 44100), 44100, 48000, 16000]))
            cand_last_exc: Exception | None = None
            for sr_cand in sr_candidates:
                if ev.is_set() or not self.running:
                    return False, True, None
                active_sr = _clamp_sample_rate(sr_cand, 44100)
                block_n = max(256, int(active_sr * block_dur))
                for ch in (1, 2):
                    if ev.is_set() or not self.running:
                        return False, True, None
                    stream_counted = False
                    stream_opened = False
                    try:
                        if self._on_stream_open is not None:
                            try:
                                self._on_stream_open()
                                stream_counted = True
                            except Exception:
                                pass
                        with sd.InputStream(
                            samplerate=active_sr,
                            channels=ch,
                            dtype="float32",
                            device=cand_idx,
                            blocksize=block_n,
                            callback=_callback,
                        ) as stream:
                            stream_opened = True
                            self._log(f"🎙️ [마이크 VAD 연결] '{cand_lbl}' @ {active_sr}Hz ({ch}ch)")
                            while self.running and not ev.is_set():
                                if not getattr(stream, "active", True):
                                    raise RuntimeError("마이크 입력 스트림이 중단되었습니다 (장치 연결 해제)")
                                ev.wait(0.08)
                        return True, True, None
                    except Exception as exc:
                        cand_last_exc = exc
                        if stream_opened:
                            _reset_buffers(clear_duck=True)
                            if ev.is_set() or not self.running:
                                return True, True, None
                            self._log(
                                _redact_secrets(f"⚠️ [마이크 스트림 중단] '{cand_lbl}' 연결 끊김: {exc}")
                            )
                            return True, False, exc
                    finally:
                        if stream_counted and self._on_stream_close is not None:
                            try:
                                self._on_stream_close()
                            except Exception:
                                pass
            return False, False, cand_last_exc

        mid_call_err: Exception | None = None
        for cand_idx, cand_sr, cand_lbl in candidates:
            if ev.is_set() or not self.running:
                break
            cand_opened, stopped_clean, cand_err = _run_candidate_stream(cand_idx, cand_sr, cand_lbl)
            if cand_opened:
                opened = True
                if stopped_clean:
                    return
                mid_call_err = cand_err
                break
            if cand_err is not None:
                last_exc = cand_err

        # One-shot mid-call USB mic unplug / PortAudio recovery: refresh devices and reconnect OS default mic
        if mid_call_err is not None and not ev.is_set() and self.running:
            ev.wait(0.25)
            if not ev.is_set() and self.running:
                default_sr = 44100
                try:
                    sd.query_devices()
                    def_info = sd.query_devices(kind="input")
                    if isinstance(def_info, dict) and def_info.get("default_samplerate"):
                        default_sr = _clamp_sample_rate(def_info.get("default_samplerate"), 44100)
                except Exception:
                    pass
                self._log("🔄 [마이크 자동 재연결] Windows 기본 마이크(device=None)로 1회 복구를 시도합니다...")
                rec_opened, rec_clean, rec_err = _run_candidate_stream(None, default_sr, "Windows 기본 마이크")
                if rec_opened and rec_clean:
                    return
                final_err = rec_err or mid_call_err
                if not ev.is_set() and self.running:
                    with self._state_lock:
                        if self._stop_event is ev:
                            self.running = False
                    self._on_error(
                        _redact_secrets(f"마이크 연결 끊김 및 자동 재연결 실패 ({primary_lbl}): {final_err}")
                    )
                return

        if not opened and not ev.is_set() and self.running:
            with self._state_lock:
                if self._stop_event is ev:
                    self.running = False
            self._on_error(_redact_secrets(f"마이크 열기 실패 ({primary_lbl}): {last_exc}"))

    def _stt_worker(
        self,
        stop_ev: threading.Event | None = None,
        audio_q: queue.Queue[tuple[np.ndarray, int, bool]] | None = None,
    ) -> None:
        ev = stop_ev if stop_ev is not None else self._stop_event
        q = audio_q if audio_q is not None else self._audio_queue

        def _safe_on_empty() -> None:
            if self._stop_event is ev and not ev.is_set():
                try:
                    self._on_empty()
                except Exception as cb_exc:
                    self._log(_redact_secrets(f"[마이크 STT 콜백 오류] {cb_exc}"))

        while self.running and not ev.is_set():
            try:
                raw_item = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if ev.is_set() or not self.running:
                self._emit_duck(False, ev=ev)
                break

            if len(raw_item) == 3:
                audio, src_sr, had_pb = raw_item
            else:
                audio, src_sr = raw_item
                had_pb = False

            try:
                model = self._get_whisper()
                if model is None:
                    self._log("[마이크 STT 대기] Whisper 모델 로딩 완료를 기다리는 중입니다...")
                    self._emit_duck(False, ev=ev)
                    _safe_on_empty()
                    continue

                safe_sr = _clamp_sample_rate(src_sr, WHISPER_SR)
                if audio.size > _MAX_UTTERANCE_SAMPLES:
                    audio = audio[:_MAX_UTTERANCE_SAMPLES]
                if audio.size and not np.all(np.isfinite(audio)):
                    audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
                if safe_sr != WHISPER_SR:
                    g = gcd(safe_sr, WHISPER_SR)
                    audio_16k = _resample_1d(audio, WHISPER_SR // g, safe_sr // g)
                else:
                    audio_16k = audio.astype(np.float32)

                if audio_16k.size and not np.all(np.isfinite(audio_16k)):
                    audio_16k = np.nan_to_num(audio_16k, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)

                is_ac = bool(audio_16k.size > 0 and float(np.std(audio_16k)) > _AC_STD_EPS)
                if is_ac:
                    audio_16k = _apply_dc_and_hpf(audio_16k, WHISPER_SR)

                peak = float(np.max(np.abs(audio_16k))) if audio_16k.size else 0.0
                if (not math.isfinite(peak)) or peak < 0.008:
                    self._emit_duck(False, ev=ev)
                    _safe_on_empty()
                    continue
                raw_peak = peak

                if self._use_silero_vad and is_ac:
                    trimmed, _vad_ran = _run_silero_vad_trim(audio_16k, pad_ms=250)
                    if trimmed is None:
                        self._emit_duck(False, ev=ev)
                        _safe_on_empty()
                        continue
                    audio_16k = trimmed

                if is_ac:
                    audio_16k = _normalize_loudness(audio_16k, WHISPER_SR)
                elif peak < 0.30:
                    audio_16k = np.clip(audio_16k * (0.38 / peak), -1.0, 1.0).astype(np.float32)

                t0 = time.time()
                tail_pad_samples = int(WHISPER_SR * 0.15)
                need_sec = (len(audio_16k) + tail_pad_samples) / float(WHISPER_SR)
                tokenizers = self._get_tokenizers() or {}
                lang_setting = str(self._stt_language or "ko_auto").strip()
                eff_beam = self._resolve_beam_size(model)

                text = ""
                det_lang = lang_setting
                mean_nsp = 0.0

                if (
                    need_sec <= 12.0
                    and tokenizers.get("ko")
                    and tokenizers.get("ja")
                    and hasattr(model, "encode")
                    and hasattr(model, "feature_extractor")
                ):
                    use_std_30s = _is_128mel_whisper_model(model)
                    if use_std_30s:
                        cl = 30
                    else:
                        cl = next(
                            (b for b in (4, 6, 8, 12) if b * WHISPER_SR >= len(audio_16k) + tail_pad_samples),
                            12,
                        )
                    pad_samples = max(len(audio_16k), cl * WHISPER_SR)
                    padded = (
                        np.pad(
                            audio_16k,
                            (0, pad_samples - len(audio_16k)),
                            mode="constant",
                            constant_values=0.0,
                        ).astype(np.float32, copy=False)
                        if len(audio_16k) < pad_samples
                        else audio_16k.astype(np.float32, copy=False)
                    )
                    feats = model.feature_extractor(padded, chunk_length=cl)
                    n_f = cl * 100
                    if feats.shape[-1] < n_f:
                        pad_val = float(np.min(feats)) if feats.size else -1.0
                        feats = np.pad(
                            feats,
                            ((0, 0), (0, n_f - feats.shape[-1])),
                            mode="constant",
                            constant_values=pad_val,
                        )
                    else:
                        feats = feats[:, :n_f]

                    enc = model.encode(feats)
                    if lang_setting in ("ko", "ja"):
                        det_lang = lang_setting
                    else:
                        det_raw = model.model.detect_language(enc)
                        det_list = det_raw[0] if det_raw else []
                        probs = dict(det_list)
                        p_ja = probs.get("<|ja|>", 0.0)
                        p_ko = probs.get("<|ko|>", 0.0)
                        if lang_setting == "ja_beginner_bi":
                            det_lang = "ja" if (p_ja >= 0.32 and p_ja >= p_ko - 0.10) else "ko"
                        else:
                            det_lang = "ja" if (p_ja >= 0.45 and p_ja >= p_ko + 0.05) else "ko"

                    tok = tokenizers[det_lang]
                    prev_tokens = self._get_prompt_hint_tokens(det_lang, tok) if raw_peak >= 0.012 else []
                    prompt_ids = model.get_prompt(tok, prev_tokens, without_timestamps=(not use_std_30s))
                    supp_ids = _resolve_suppressed_tokens(model, tok)
                    gen_list = model.model.generate(
                        enc,
                        [prompt_ids],
                        beam_size=eff_beam,
                        max_length=224 if use_std_30s else 96,
                        suppress_blank=True,
                        suppress_tokens=supp_ids,
                        no_repeat_ngram_size=0 if use_std_30s else 3,
                        repetition_penalty=1.0 if use_std_30s else 1.08,
                        return_scores=True,
                        return_no_speech_prob=True,
                    )
                    if gen_list:
                        gen_res = gen_list[0]
                        mean_nsp = float(getattr(gen_res, "no_speech_prob", 0.0))
                        seq_ids = getattr(gen_res, "sequences_ids", None) or []
                        first_seq = seq_ids[0] if seq_ids else []
                        ts_begin = getattr(tok, "timestamp_begin", None)
                        if not isinstance(ts_begin, int) or isinstance(ts_begin, bool):
                            ts_begin = None
                        tokens = [t for t in first_seq if t < tok.eot and (ts_begin is None or t < ts_begin)]
                        text = _WS_MULTI_RE.sub(" ", tok.decode(tokens)).strip()
                        orig_cr = _text_compression_ratio(text) if use_std_30s else 0.0
                        if use_std_30s and orig_cr > 2.4:
                            retry_list = model.model.generate(
                                enc,
                                [prompt_ids],
                                beam_size=eff_beam,
                                max_length=224,
                                suppress_blank=True,
                                suppress_tokens=supp_ids,
                                no_repeat_ngram_size=3,
                                repetition_penalty=1.08,
                                return_scores=True,
                                return_no_speech_prob=True,
                            )
                            if retry_list:
                                retry_res = retry_list[0]
                                retry_seq_ids = getattr(retry_res, "sequences_ids", None) or []
                                retry_first = retry_seq_ids[0] if retry_seq_ids else []
                                retry_tokens = [
                                    t for t in retry_first if t < tok.eot and (ts_begin is None or t < ts_begin)
                                ]
                                retry_text = _WS_MULTI_RE.sub(" ", tok.decode(retry_tokens)).strip()
                                if retry_text and _text_compression_ratio(retry_text) < orig_cr:
                                    text = retry_text
                                    mean_nsp = float(getattr(retry_res, "no_speech_prob", mean_nsp))
                    else:
                        mean_nsp = 1.0
                        text = ""
                else:
                    if lang_setting in ("ko", "ja"):
                        lang_opt = lang_setting
                    else:
                        lang_opt = None
                    with self._hint_lock:
                        if raw_peak >= 0.012:
                            if lang_opt == "ja":
                                hint_opt = self._ja_prompt_hint
                            elif lang_opt == "ko":
                                hint_opt = self._ko_prompt_hint
                            else:
                                hint_opt = ""
                        else:
                            hint_opt = ""
                    transcribe_kwargs: dict[str, Any] = {
                        "language": lang_opt,
                        "beam_size": eff_beam,
                        "vad_filter": False,
                        "no_speech_threshold": 0.75,
                        "condition_on_previous_text": False,
                    }
                    if hint_opt:
                        transcribe_kwargs["initial_prompt"] = hint_opt
                    segments, info = model.transcribe(audio_16k, **transcribe_kwargs)
                    parts = []
                    avg_no_speech = 0.0
                    seg_count = 0
                    for seg in segments:
                        txt = (seg.text or "").strip()
                        if txt:
                            parts.append(txt)
                        avg_no_speech += float(getattr(seg, "no_speech_prob", 0.0))
                        seg_count += 1
                    text = _WS_MULTI_RE.sub(" ", " ".join(parts)).strip()
                    mean_nsp = (avg_no_speech / seg_count) if seg_count else 1.0
                    det_lang = getattr(info, "language", lang_opt or "auto")

                dt = time.time() - t0
                if not text or text in MIC_HALLUCINATIONS or self._is_glossary_echo(text) or mean_nsp > 0.75:
                    self._emit_duck(False, ev=ev)
                    _safe_on_empty()
                    continue
                if len(text) <= 1 and text in {".", "!", "?", "음", "어", "아", "ん", "あ"}:
                    self._emit_duck(False, ev=ev)
                    _safe_on_empty()
                    continue

                if ev.is_set() or not self.running or self._stop_event is not ev:
                    self._emit_duck(False, ev=ev)
                    continue
                self._log(f"🗣️ [사용자 마이크 인식 ({det_lang}, {dt:.2f}s)] {text}")
                self.last_utterance_during_playback = bool(had_pb)
                self._emit_duck(False, ev=ev)
                try:
                    self._on_transcribed(text)
                except Exception as cb_exc:
                    self._log(_redact_secrets(f"[마이크 STT 전달 콜백 오류] {cb_exc}"))
            except Exception as exc:
                if not ev.is_set() and self._stop_event is ev:
                    self._emit_duck(False, ev=ev)
                    self._log(_redact_secrets(f"[마이크 STT 오류] {exc}"))
                    _safe_on_empty()
