"""
Local Microphone RMS/VAD Detector & Zero-Cost Faster-Whisper STT Engine
-----------------------------------------------------------------------
Captures user microphone speech on 25ms frames, resets the proactive silence timer
immediately on speech onset, and transcribes completed utterances locally using
the shared faster-whisper engine (CUDA INT8 or CPU INT8 fallback, $0 STT cost).
"""

from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from math import gcd
from typing import Any, Callable

import numpy as np
import sounddevice as sd

from src.controller.proactive_lead_engine import _extract_dev_base_name, _norm_dev_key

_RESAMPLE_POLY_FN: Any = None


def _resample_1d(audio: np.ndarray, up: int, down: int) -> np.ndarray:
    """Lazy-import `scipy.signal.resample_poly` on first non-16kHz mic utterance."""
    global _RESAMPLE_POLY_FN
    if _RESAMPLE_POLY_FN is None:
        from scipy.signal import resample_poly as _sp_resample_poly

        _RESAMPLE_POLY_FN = _sp_resample_poly
    return _RESAMPLE_POLY_FN(audio, up, down).astype(np.float32)

WHISPER_SR = 16000

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


def list_microphone_labels(inputs: list[tuple[int, str, int]]) -> list[str]:
    """
    Return deduplicated physical microphone input labels (MME -> DirectSound -> WASAPI),
    excluding VB-Cable, Sound Mapper, WDM-KS, and Stereo Mix when real microphones exist.
    """
    best_by_key: dict[str, tuple[tuple[int, int, int, int], str]] = {}
    for idx, label, _ in inputs:
        low = label.lower()
        if any(
            k in low
            for k in (
                "cable",
                "vb-audio",
                "sound mapper",
                "주 사운드",
                "주 음성",
                "stereo mix",
                "스테레오 믹스",
                "wdm-ks",
            )
        ):
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

    # Fallback on PCs with no physical mic plugged in: list non-mapper inputs cleanly
    fallback = [
        lbl
        for _, lbl, _ in inputs
        if not any(k in lbl.lower() for k in ("sound mapper", "주 사운드", "주 음성", "wdm-ks"))
    ]
    return fallback


_PAREN_SUFFIX_RE = re.compile(r"\s*[(（][^()（）]*[)）]")
_NORM_HINT_ECHO_RE = re.compile(r"[\s,，、.。!！?？]+")
_WS_MULTI_RE = re.compile(r"\s+")
_JP_WS_SPLIT_RE = re.compile(r"[\s　]+")


def _norm_hint_echo(text: str) -> str:
    return _NORM_HINT_ECHO_RE.sub(" ", (text or "").strip()).strip().lower()


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
    school_kr = str(char_data.get("school") or "").strip()
    user_title_kr = str(char_data.get("user_title") or "선생님").strip()

    raw_name_jp = _PAREN_SUFFIX_RE.sub("", str(char_data.get("name_jp") or "")).strip()
    name_jp_compact = _WS_MULTI_RE.sub("", raw_name_jp)
    jp_parts = [p for p in _JP_WS_SPLIT_RE.split(raw_name_jp) if p]
    given_jp = jp_parts[-1] if (len(jp_parts) >= 2 and len(jp_parts[-1]) >= 2) else ""
    school_jp = str(char_data.get("school_jp") or "").strip()
    user_title_jp = str(char_data.get("user_title_jp") or "先生").strip()

    ko_terms = _dedupe([user_title_kr, "샬레", raw_name_kr, given_kr, school_kr])
    ja_terms = _dedupe([user_title_jp, "シャーレ", name_jp_compact, given_jp, school_jp])

    ko_hint = ", ".join(ko_terms)
    ja_hint = "、".join(ja_terms)

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

        self.running = False
        self.last_utterance_during_playback = False
        self._rms_threshold = 0.010
        self._silence_end_sec = 0.68
        self._stt_language = "ko_auto"
        self._hint_lock = threading.Lock()
        self._ko_prompt_hint = ""
        self._ja_prompt_hint = ""
        self._hint_echo_phrases: set[str] = set()
        self._hint_token_cache: dict[tuple[str, str], list[int]] = {}
        self._audio_queue: queue.Queue[tuple[np.ndarray, int, bool]] = queue.Queue(maxsize=8)
        self._threads: list[threading.Thread] = []

    def _emit_duck(self, active: bool) -> None:
        if self._on_barge_in_duck is not None:
            try:
                self._on_barge_in_duck(active)
            except Exception:
                pass

    def _update_echo_phrases(self, ko_hint: str, ja_hint: str, extra_echo: set[str] | None = None) -> None:
        echoes: set[str] = set(extra_echo or ())
        for raw_h in (ko_hint, ja_hint):
            norm = _norm_hint_echo(raw_h)
            if norm:
                echoes.add(norm)
        self._hint_echo_phrases = echoes

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
        with self._hint_lock:
            return bool(norm and norm in self._hint_echo_phrases)

    def configure(
        self,
        rms_threshold: float | None = None,
        silence_end_sec: float | None = None,
        stt_language: str | None = None,
        ko_prompt_hint: str | None = None,
        ja_prompt_hint: str | None = None,
    ) -> None:
        if rms_threshold is not None:
            self._rms_threshold = max(0.002, min(0.15, float(rms_threshold)))
        if silence_end_sec is not None:
            self._silence_end_sec = max(0.35, min(2.5, float(silence_end_sec)))
        if stt_language is not None:
            self._stt_language = str(stt_language).strip() or "ko_auto"
        if ko_prompt_hint is not None or ja_prompt_hint is not None:
            with self._hint_lock:
                if ko_prompt_hint is not None:
                    self._ko_prompt_hint = str(ko_prompt_hint).strip()
                if ja_prompt_hint is not None:
                    self._ja_prompt_hint = str(ja_prompt_hint).strip()
                self._hint_token_cache.clear()
                self._update_echo_phrases(self._ko_prompt_hint, self._ja_prompt_hint)

    def start(
        self,
        dev_idx: int | None,
        dev_sr: int,
        dev_label: str,
        fallback_candidates: list[tuple[int | None, int, str]] | None = None,
    ) -> None:
        if self.running:
            return
        self.running = True
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

        candidates: list[tuple[int | None, int, str]] = []
        seen_idx: set[int | None] = set()
        for raw_idx, raw_sr, raw_lbl in [(dev_idx, int(dev_sr or 44100), dev_label)] + list(fallback_candidates or []):
            if raw_idx in seen_idx:
                continue
            seen_idx.add(raw_idx)
            candidates.append((raw_idx, int(raw_sr or 44100), str(raw_lbl)))

        t_cap = threading.Thread(
            target=self._capture_loop,
            args=(candidates,),
            daemon=True,
        )
        t_stt = threading.Thread(target=self._stt_worker, daemon=True)
        self._threads = [t_cap, t_stt]
        t_cap.start()
        t_stt.start()

    def stop(self) -> None:
        self.running = False
        self._emit_duck(False)
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def _capture_loop(self, candidates: list[tuple[int | None, int, str]]) -> None:
        block_dur = 0.025
        pre_roll: deque[np.ndarray] = deque(maxlen=6)
        speech_frames: list[np.ndarray] = []
        speaking = False
        onset_hits = 0
        voiced_sec = 0.0
        silence_sec = 0.0
        cb_count = 0
        utterance_had_pb = False
        ducked = False

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
                self._emit_duck(False)

        def _callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            nonlocal speaking, onset_hits, voiced_sec, silence_sec, cb_count, utterance_had_pb, ducked

            if not self.running:
                return

            if indata.ndim > 1:
                mono = indata[:, 0] if indata.shape[1] == 1 else np.mean(indata, axis=1, dtype=np.float32)
            else:
                mono = indata
            if mono.size:
                rms = float(np.sqrt(float(np.dot(mono, mono)) / mono.size))
                peak = float(np.max(np.abs(mono)))
            else:
                rms = 0.0
                peak = 0.0

            cb_count += 1
            if cb_count % 4 == 0:
                try:
                    self._on_meter(rms, peak, speaking)
                except Exception:
                    pass

            if not self._is_listening_allowed():
                if speaking:
                    _reset_buffers(clear_duck=True)
                pre_roll.clear()
                onset_hits = 0
                return

            pb_active = bool(self._is_playback_active()) if self._is_playback_active is not None else False
            if pb_active and not speaking:
                eff_thresh = max(self._rms_threshold * 1.35, self._rms_threshold + 0.004)
                req_onset = 3
            else:
                eff_thresh = self._rms_threshold
                req_onset = 2

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
                        voiced_sec = len(speech_frames) * block_dur
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
                    self._emit_duck(True)
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
                if voiced_sec >= 0.22 and speech_frames:
                    full = np.concatenate(speech_frames)
                    item = (full, active_sr, utterance_had_pb)
                    if self._audio_queue.full():
                        try:
                            self._audio_queue.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        self._audio_queue.put_nowait(item)
                    except queue.Full:
                        pass
                    _reset_buffers(clear_duck=False)
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

        for cand_idx, cand_sr, cand_lbl in candidates:
            if not self.running:
                break
            sr_candidates = list(dict.fromkeys([int(cand_sr or 44100), 44100, 48000, 16000]))
            for sr_cand in sr_candidates:
                active_sr = int(sr_cand)
                block_n = max(256, int(active_sr * block_dur))
                for ch in (1, 2):
                    try:
                        with sd.InputStream(
                            samplerate=active_sr,
                            channels=ch,
                            dtype="float32",
                            device=cand_idx,
                            blocksize=block_n,
                            callback=_callback,
                        ):
                            opened = True
                            self._log(f"🎙️ [마이크 VAD 연결] '{cand_lbl}' @ {active_sr}Hz ({ch}ch)")
                            while self.running:
                                time.sleep(0.08)
                        break
                    except Exception as exc:
                        last_exc = exc
                if opened:
                    break
            if opened:
                break

        if not opened and self.running:
            self.running = False
            self._on_error(f"마이크 열기 실패 ({primary_lbl}): {last_exc}")

    def _stt_worker(self) -> None:
        while self.running:
            try:
                raw_item = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if len(raw_item) == 3:
                audio, src_sr, had_pb = raw_item
            else:
                audio, src_sr = raw_item
                had_pb = False

            model = self._get_whisper()
            if model is None:
                self._log("[마이크 STT 대기] Whisper 모델 로딩 완료를 기다리는 중입니다...")
                self._emit_duck(False)
                self._on_empty()
                continue

            try:
                if src_sr != WHISPER_SR:
                    g = gcd(src_sr, WHISPER_SR)
                    audio_16k = _resample_1d(audio, WHISPER_SR // g, src_sr // g)
                else:
                    audio_16k = audio.astype(np.float32)

                peak = float(np.max(np.abs(audio_16k))) if audio_16k.size else 0.0
                if peak < 0.008:
                    self._emit_duck(False)
                    self._on_empty()
                    continue
                raw_peak = peak
                if peak < 0.30:
                    audio_16k = np.clip(audio_16k * (0.38 / peak), -1.0, 1.0).astype(np.float32)

                t0 = time.time()
                need_sec = len(audio_16k) / float(WHISPER_SR)
                tokenizers = self._get_tokenizers() or {}
                lang_setting = str(self._stt_language or "ko_auto").strip()

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
                    cl = next((b for b in (4, 6, 8, 12) if b >= need_sec), 12)
                    pad_samples = max(len(audio_16k), WHISPER_SR * min(cl, 4))
                    padded = (
                        np.pad(audio_16k, (0, pad_samples - len(audio_16k)))
                        if len(audio_16k) < pad_samples
                        else audio_16k
                    )
                    feats = model.feature_extractor(padded, chunk_length=cl)
                    n_f = cl * 100
                    if feats.shape[-1] < n_f:
                        feats = np.pad(feats, ((0, 0), (0, n_f - feats.shape[-1])))
                    else:
                        feats = feats[:, :n_f]

                    enc = model.encode(feats)
                    if lang_setting in ("ko", "ja"):
                        det_lang = lang_setting
                    else:
                        det_list = model.model.detect_language(enc)[0]
                        probs = dict(det_list)
                        p_ja = probs.get("<|ja|>", 0.0)
                        p_ko = probs.get("<|ko|>", 0.0)
                        if lang_setting == "ja_beginner_bi":
                            det_lang = "ja" if (p_ja >= 0.32 and p_ja >= p_ko - 0.10) else "ko"
                        else:
                            det_lang = "ja" if (p_ja >= 0.55 and p_ja > p_ko + 0.10) else "ko"

                    tok = tokenizers[det_lang]
                    prev_tokens = self._get_prompt_hint_tokens(det_lang, tok) if raw_peak >= 0.012 else []
                    prompt_ids = model.get_prompt(tok, prev_tokens, without_timestamps=True)
                    supp_ids = (
                        list(model.get_suppressed_tokens(tok, [-1]))
                        if hasattr(model, "get_suppressed_tokens")
                        else [-1]
                    )
                    gen_res = model.model.generate(
                        enc,
                        [prompt_ids],
                        beam_size=1,
                        max_length=96,
                        suppress_blank=True,
                        suppress_tokens=supp_ids,
                        no_repeat_ngram_size=3,
                        repetition_penalty=1.08,
                        return_scores=True,
                        return_no_speech_prob=True,
                    )[0]
                    mean_nsp = float(getattr(gen_res, "no_speech_prob", 0.0))
                    tokens = [t for t in gen_res.sequences_ids[0] if t < tok.eot]
                    text = _WS_MULTI_RE.sub(" ", tok.decode(tokens)).strip()
                else:
                    if lang_setting in ("ko", "ja"):
                        lang_opt = lang_setting
                    elif lang_setting == "ja_beginner_bi":
                        lang_opt = None
                    elif lang_setting == "ko_auto":
                        lang_opt = "ko"
                    else:
                        lang_opt = None
                    with self._hint_lock:
                        hint_opt = (
                            (self._ja_prompt_hint if lang_opt == "ja" else self._ko_prompt_hint)
                            if raw_peak >= 0.012
                            else ""
                        )
                    transcribe_kwargs: dict[str, Any] = {
                        "language": lang_opt,
                        "beam_size": 1,
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
                    self._emit_duck(False)
                    self._on_empty()
                    continue
                if len(text) <= 1 and text in {".", "!", "?", "음", "어", "아", "ん", "あ"}:
                    self._emit_duck(False)
                    self._on_empty()
                    continue

                self._log(f"🗣️ [사용자 마이크 인식 ({det_lang}, {dt:.2f}s)] {text}")
                self.last_utterance_during_playback = bool(had_pb)
                self._emit_duck(False)
                self._on_transcribed(text)
            except Exception as exc:
                self._emit_duck(False)
                self._log(f"[마이크 STT 오류] {exc}")
                self._on_empty()
