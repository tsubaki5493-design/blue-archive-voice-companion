"""
Proactive Conversation Lead & Silence Watchdog Engine (UI-Independent)
----------------------------------------------------------------------
Shared core engine for:
  - Stage 1 (Current): PC Earphones (Realtek) + Monitor Speaker Acoustic Trigger (25G64) + iPhone AirPlay
  - Stage 2-A (Future): Headless PC Chrome Gemini Live + Virtual Mic (Cable-A) + iPhone Remote PWA

Zero tkinter dependencies so this module can be imported directly by both the
desktop Tkinter controller and a future FastAPI/WebSocket server.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

PROACTIVE_PROMPT_MARKER = "【最重要・会話主導（リード）＆逆質問プロトコル】"

TRIGGER_PHRASES: dict[str, str] = {
    "nudge_1": "자동 신호, 선생님께 먼저 말 걸어줘",
    "nudge_2": "자동 신호, 새로운 화제로 질문해줘",
    "nudge_manual": "자동 신호, 대화를 리드해줘",
}

_TRIGGER_ECHO_RE = re.compile(
    r"(자동\s*신호|自動\s*信号|선생님께\s*먼저\s*말\s*걸어줘|새로운\s*화제로\s*질문해줘|대화를\s*리드해줘)"
)


def contains_trigger_echo(text: str) -> bool:
    """Return True if STT accidentally picked up a raw trigger echo phrase."""
    return bool(_TRIGGER_ECHO_RE.search(text or ""))


def build_proactive_lead_prompt(char_data: dict[str, Any], base_prompt: str) -> str:
    """
    Dynamically append character-tailored conversational lead & silence-signal rules
    to any character voice prompt without mutating the 196 source JSON files.
    """
    clean_base = (base_prompt or "").strip()
    if PROACTIVE_PROMPT_MARKER in clean_base:
        return clean_base

    persona = char_data.get("persona") or {}
    sm = persona.get("speech_mechanics_jp") or {}
    name_jp = str(char_data.get("name_jp") or char_data.get("name_kr") or "キャラクター").strip()
    school_jp = str(char_data.get("school_jp") or char_data.get("school") or "キヴォトス").strip()
    club_jp = str(char_data.get("club_jp") or char_data.get("club") or "部活").strip()
    first_person = str(sm.get("first_person") or "私").strip()
    sensei_addr = str(sm.get("sensei_address") or char_data.get("user_title_jp") or "先生").strip()
    peers = str(sm.get("peer_address_examples") or "仲間たち").strip()

    lead_block = (
        f"\n\n{PROACTIVE_PROMPT_MARKER}\n"
        f"・受け身の一問一答AI（ユーザーの質問に答えるだけで会話を終える態度）は【絶対禁止】です。\n"
        f"・{name_jp}（一人称：{first_person}／相手への呼称：{sensei_addr}）として、返答の終わりには必ず"
        f"「{sensei_addr}への逆質問」「二択の提案」「次の話題やミニイベントの誘い」を自然につなげ、あなた側から会話を主導（リード）してください。\n"
        f"・{sensei_addr}の返事が短くても（「うん」「そうだね」「なるほど」等）、"
        f"自分の所属（{school_jp}・{club_jp}）での最近の出来事・{peers}とのエピソード・{sensei_addr}に聞いてみたいことなどを自ら切り出して会話を膨らませてください。\n"
        f"\n"
        f"【無言タイマー合図（\"자동 신호...\"）受信時の先制発話ルール】\n"
        f"・マイクから韓国語で「자동 신호, 선생님께 먼저 말 걸어줘」「자동 신호, 새로운 화제로 질문해줘」"
        f"「자동 신호, 대화를 리드해줘」（または「자동 신호」「자동신호」「먼저 말 걸어줘」「새로운 화제로 질문해줘」を含む合図）が聞こえた場合、"
        f"それは{sensei_addr}が喋った言葉ではなく、**「今、{sensei_addr}がしばらく無言であなたを見つめている／黙っている状態」を知らせる透明なシステム合図**です。\n"
        f"・合図そのものへの返事（「分かりました」「信号確認」「자동 신호」の復唱、合図に言及するメタ発言）は【100％禁止】です。\n"
        f"・1回目の合図（먼저 말 걸어줘）を受けたら、急に黙り込んだ{sensei_addr}の様子をキャラクター固有の口調で気にかけたり、"
        f"ちょっと拗ねたり照れたりからかったりしながら、直前の文脈に絡めた質問で先に話しかけてください。\n"
        f"・2回目または手動合図（새로운 화제로 질문해줘 / 대화를 리드해줘）を受けたら、空気を変える新しい話題・日常の相談・クイズや"
        f"二人だけの予定の提案などを自ら切り出して会話を積極的にリードしてください。\n"
        f"・返答は必ず100％自然な日本語のセリフのみを発話してください。"
    )
    return f"{clean_base}{lead_block}".strip()


def _synth_sapi_wav_via_powershell(phrase: str, out_wav: Path) -> bool:
    """Synthesize a clean WAV file using Windows built-in System.Speech (0 API cost)."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    safe_phrase = phrase.replace("'", "''")
    safe_path = str(out_wav.resolve()).replace("'", "''")
    ps_script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$voices = $s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }; "
        "if ($voices -contains 'Microsoft Heami Desktop') { $s.SelectVoice('Microsoft Heami Desktop') } "
        "elseif ($voices -contains 'Microsoft Haruka Desktop') { $s.SelectVoice('Microsoft Haruka Desktop') } "
        "$s.Rate = 2; "
        "$s.Volume = 100; "
        f"$s.SetOutputToWaveFile('{safe_path}'); "
        f"$s.Speak('{safe_phrase}'); "
        "$s.Dispose();"
    )
    res = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return res.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 1000


def _trim_fade_normalize(audio: np.ndarray, sr: int) -> np.ndarray:
    """Trim leading/trailing silence (>0.008), boost vocal loudness cleanly, and add 25ms fade in/out."""
    mono = np.mean(audio, axis=1) if audio.ndim > 1 else audio
    mono = mono.astype(np.float32)
    idx = np.where(np.abs(mono) > 0.008)[0]
    if idx.size > 0:
        pad = int(sr * 0.03)
        start = max(0, int(idx[0]) - pad)
        end = min(len(mono), int(idx[-1]) + pad + 1)
        mono = mono[start:end]

    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    if peak > 1e-6:
        mono = mono / peak
        # Gentle tanh loudness leveling makes distant monitor-speaker playback clearer for iPhone mic
        mono = (np.tanh(mono * 1.45) / np.tanh(1.45)) * 0.92

    fade_n = min(int(sr * 0.025), len(mono) // 4)
    if fade_n > 1:
        ramp = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
        mono[:fade_n] *= ramp
        mono[-fade_n:] *= ramp[::-1]
    return mono.astype(np.float32)


def ensure_trigger_wavs_cached(cache_dir: Path) -> dict[str, tuple[np.ndarray, int]]:
    """
    Ensure local trigger WAV files exist in `cache_dir` (synthesizing missing ones via SAPI)
    and return trimmed/normalized `(float32_mono_array, sample_rate)` dict in memory.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    loaded: dict[str, tuple[np.ndarray, int]] = {}
    for key, phrase in TRIGGER_PHRASES.items():
        wav_path = cache_dir / f"{key}.wav"
        if not wav_path.exists() or wav_path.stat().st_size <= 1000:
            ok = _synth_sapi_wav_via_powershell(phrase, wav_path)
            if not ok:
                # Fallback short Japanese wake cue if Korean SAPI fails
                fallback_phrase = "自動信号、先生に話しかけて"
                if not _synth_sapi_wav_via_powershell(fallback_phrase, wav_path):
                    raise RuntimeError(f"SAPI 트리거 음성 합성 실패: {wav_path}")

        raw_audio, sr = sf.read(str(wav_path), dtype="float32")
        clean_audio = _trim_fade_normalize(raw_audio, int(sr))
        loaded[key] = (clean_audio, int(sr))
    return loaded


_DEV_IDX_PREFIX_RE = re.compile(r"^\[\d+\]\s*")
_DEV_HOSTAPI_SUFFIX_RE = re.compile(r"\s*\((?:MME|Windows DirectSound|Windows WASAPI|DirectSound|WASAPI)\)\s*$", re.I)
_WS_MULTI_RE = re.compile(r"\s+")


def _extract_dev_base_name(label: str) -> str:
    """Extract device name from `'[idx] Device Name (HostAPI)'` label."""
    s = _DEV_IDX_PREFIX_RE.sub("", label.strip())
    s = _DEV_HOSTAPI_SUFFIX_RE.sub("", s)
    return s.strip()


def _extract_host_api_tag(label: str) -> str:
    """Return normalized host API family tag (`wasapi`, `mme`, `directsound`, or ``)."""
    low = label.lower()
    if "wasapi" in low:
        return "wasapi"
    if "mme" in low:
        return "mme"
    if "directsound" in low:
        return "directsound"
    return ""


def _norm_dev_key(label: str) -> str:
    base = _extract_dev_base_name(label)
    return _WS_MULTI_RE.sub(" ", base.lower())[:26]


def match_device_label_by_name(
    saved_label: str,
    candidate_labels: list[str],
    require_same_api: bool = False,
) -> str | None:
    """
    Resolve a persisted `[idx] Device Name (HostAPI)` label against current PortAudio candidates
    even when USB/HDMI hotplug shifts numeric `[idx]` prefixes.
    """
    if not saved_label or not candidate_labels:
        return None
    low_saved = saved_label.lower()
    # Skip virtual mappers when matching against physical device lists
    cand_has_non_mapper = any(
        "sound mapper" not in c.lower() and "주 사운드" not in c.lower() for c in candidate_labels
    )
    if cand_has_non_mapper and ("sound mapper" in low_saved or "주 사운드" in low_saved):
        return None

    if saved_label in candidate_labels:
        return saved_label

    saved_key = _norm_dev_key(saved_label)
    if not saved_key:
        return None
    saved_api = _extract_host_api_tag(saved_label)

    for cand in candidate_labels:
        if _norm_dev_key(cand) != saved_key:
            continue
        if require_same_api and saved_api and _extract_host_api_tag(cand) != saved_api:
            continue
        return cand
    return None


def _deduplicate_physical_outputs(
    outputs: list[tuple[int, str, int]],
    exclude_25g64: bool = True,
) -> list[str]:
    """Collapse MME/DirectSound/WASAPI duplicates into one label per physical device (WASAPI first)."""

    def _collect(skip_25g64: bool) -> dict[str, tuple[tuple[int, int, int], str]]:
        best_by_key: dict[str, tuple[tuple[int, int, int], str]] = {}
        for idx, label, _ in outputs:
            low = label.lower()
            if any(k in low for k in ("cable", "vb-audio", "sound mapper", "주 사운드")):
                continue
            if skip_25g64 and "25g64" in low:
                continue

            base = _extract_dev_base_name(label)
            norm_key = _norm_dev_key(label)
            if not norm_key:
                continue

            api_score = 3 if "wasapi" in low else (2 if "mme" in low else 1)
            score = (api_score, len(base), -idx)
            prev = best_by_key.get(norm_key)
            if prev is None or score > prev[0]:
                best_by_key[norm_key] = (score, label)
        return best_by_key

    best = _collect(exclude_25g64)
    if not best and exclude_25g64:
        best = _collect(False)
    return [v[1] for v in best.values()]


def list_primary_output_labels(outputs: list[tuple[int, str, int]]) -> list[str]:
    """
    Return clean, deduplicated physical playback speakers for the primary character voice output.
    Excludes VB-Cable, Sound Mapper, and speakerless `25G64`, sorting Realtek/headphones first
    and monitor speakers like `ANMITE` last.
    """
    unique = _deduplicate_physical_outputs(outputs, exclude_25g64=True)

    def _sort_key(lbl: str) -> tuple[int, str]:
        low = lbl.lower()
        if any(k in low for k in ("realtek", "headphone", "headset", "이어폰", "헤드폰", "헤드셋")):
            tier = 0
        elif not any(k in low for k in ("anmite", "nvidia", "hdmi", "display")):
            tier = 1
        else:
            tier = 2
        return (tier, low)

    return sorted(unique, key=_sort_key)


def list_trigger_output_labels(outputs: list[tuple[int, str, int]]) -> list[str]:
    """
    Return clean, deduplicated physical playback speakers for the Trigger Speaker dropdown.
    Excludes virtual cables (`VB-Audio`/`CABLE`), Windows virtual mappers (`Sound Mapper`/`주 사운드`),
    and speakerless `25G64`, collapsing MME/DirectSound/WASAPI duplicates (WASAPI first).
    """
    unique = _deduplicate_physical_outputs(outputs, exclude_25g64=True)

    def _sort_key(lbl: str) -> tuple[int, int, str]:
        low = lbl.lower()
        tier = 0 if "anmite" in low else (1 if any(k in low for k in ("nvidia", "hdmi", "display")) else 2)
        is_realtek = 1 if "realtek" in low else 0
        return (tier, is_realtek, low)

    return sorted(unique, key=_sort_key)


def pick_default_trigger_output(
    outputs: list[tuple[int, str, int]],
    primary_out_idx: int | None = None,
) -> int | None:
    """
    Auto-select monitor built-in speaker (`ANMITE` first, then other monitor speakers, then `25G64`),
    excluding VB-Cable and Sound Mapper, preferring WASAPI -> MME -> DirectSound.
    """
    scored: list[tuple[int, int, int, int]] = []
    for idx, label, _ in outputs:
        low = label.lower()
        if "cable" in low or "vb-audio" in low or "sound mapper" in low or "주 사운드" in low:
            continue
        if primary_out_idx is not None and idx == primary_out_idx:
            continue

        if "anmite" in low:
            dev_tier = 3
        elif any(k in low for k in ("nvidia", "hdmi", "display")) and "25g64" not in low:
            dev_tier = 2
        elif "25g64" in low:
            dev_tier = 1
        else:
            dev_tier = 0

        # Prefer WASAPI (full device name, background-safe, native 48kHz) -> MME -> DirectSound
        api_bonus = 3 if "wasapi" in low else (2 if "mme" in low else 1)
        not_realtek = 1 if "realtek" not in low else 0
        scored.append((dev_tier, not_realtek, api_bonus, idx))

    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][3]


@dataclass
class WatchdogStatus:
    state: str
    elapsed_sec: float
    timeout_sec: float
    consecutive_nudges: int
    max_nudges: int
    has_started_call: bool


class ProactiveWatchdog:
    """
    Deterministic state machine that monitors Gemini Live silence without a PC mic.
    Distinguishes Gemini's reply to our own proactive trigger (`AWAITING_GEMINI_REPLY`)
    from Gemini's reply to actual user speech, preventing Fish Audio credit burn when
    the user steps away (`max_nudges` cap -> `AUTO_PAUSED`).
    """

    DISARMED = "DISARMED"
    MONITORING = "MONITORING"
    AWAITING_GEMINI_REPLY = "AWAITING_GEMINI_REPLY"
    AUTO_PAUSED = "AUTO_PAUSED"

    def __init__(
        self,
        enabled: bool = True,
        silence_timeout_sec: float = 18.0,
        max_nudges: int = 2,
        reply_wait_timeout_sec: float = 14.0,
    ) -> None:
        self._lock = threading.Lock()
        self.enabled = enabled
        self.silence_timeout_sec = max(5.0, float(silence_timeout_sec))
        self.max_nudges = max(1, int(max_nudges))
        self.reply_wait_timeout_sec = max(5.0, float(reply_wait_timeout_sec))

        self.state = self.DISARMED
        self.has_started_call = False
        self.consecutive_nudges = 0
        now = time.monotonic()
        self.last_activity_ts = now
        self.awaiting_since_ts = now

    def reset(self, enabled: bool | None = None, silence_timeout_sec: float | None = None) -> None:
        with self._lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if silence_timeout_sec is not None:
                self.silence_timeout_sec = max(5.0, float(silence_timeout_sec))
            self.state = self.DISARMED
            self.has_started_call = False
            self.consecutive_nudges = 0
            now = time.monotonic()
            self.last_activity_ts = now
            self.awaiting_since_ts = now

    def configure(self, enabled: bool, silence_timeout_sec: float) -> None:
        with self._lock:
            self.enabled = bool(enabled)
            self.silence_timeout_sec = max(5.0, float(silence_timeout_sec))

    def on_vbcable_speech_detected(self, now: float | None = None, is_new_turn: bool = True) -> bool:
        """
        Called when VB-Cable captures confirmed Gemini speech text.
        Returns True if this speech indicates a real user-triggered turn (resetting nudge counter),
        or False if it is a continuation clause or Gemini responding to our proactive trigger.
        """
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            self.has_started_call = True
            self.last_activity_ts = ts
            if self.state == self.AWAITING_GEMINI_REPLY:
                self.state = self.MONITORING
                return False
            if self.state == self.DISARMED:
                self.state = self.MONITORING
                return True
            if not is_new_turn:
                return False
            # Real user spoke (new turn started while MONITORING or AUTO_PAUSED) -> reset counter & resume!
            self.consecutive_nudges = 0
            self.state = self.MONITORING
            return True

    def on_playback_active(self, now: float | None = None) -> None:
        """Keep activity timestamp fresh while Fish Audio character speech is playing."""
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            if ts > self.last_activity_ts:
                self.last_activity_ts = ts

    def record_manual_nudge(self, now: float | None = None) -> None:
        """Arm/transition immediately when user clicks manual nudge button."""
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            self.has_started_call = True
            self.state = self.AWAITING_GEMINI_REPLY
            self.awaiting_since_ts = ts
            self.last_activity_ts = ts

    def tick(self, now: float | None = None, is_pipeline_busy: bool = False) -> str | None:
        """
        Advance watchdog state. Returns `'nudge_1'` or `'nudge_2'` when a proactive
        trigger should be emitted, or `None` otherwise.
        """
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            if not self.enabled or not self.has_started_call:
                return None

            if self.state == self.AWAITING_GEMINI_REPLY:
                if is_pipeline_busy:
                    # Gemini already started speaking or TTS is playing; keep awaiting timestamp fresh
                    self.last_activity_ts = ts
                    return None
                if (ts - self.awaiting_since_ts) >= self.reply_wait_timeout_sec:
                    # Gemini didn't answer the trigger within timeout
                    self.last_activity_ts = ts
                    if self.consecutive_nudges >= self.max_nudges:
                        self.state = self.AUTO_PAUSED
                    else:
                        self.state = self.MONITORING
                return None

            if self.state != self.MONITORING:
                return None

            if is_pipeline_busy:
                self.last_activity_ts = ts
                return None

            elapsed = ts - self.last_activity_ts
            if elapsed < self.silence_timeout_sec:
                return None

            if self.consecutive_nudges >= self.max_nudges:
                self.state = self.AUTO_PAUSED
                return None

            nudge_key = "nudge_1" if self.consecutive_nudges == 0 else "nudge_2"
            self.consecutive_nudges += 1
            self.state = self.AWAITING_GEMINI_REPLY
            self.awaiting_since_ts = ts
            self.last_activity_ts = ts
            return nudge_key

    def get_status(self, now: float | None = None, is_pipeline_busy: bool = False) -> WatchdogStatus:
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            elapsed = 0.0 if (is_pipeline_busy or self.state != self.MONITORING) else max(0.0, ts - self.last_activity_ts)
            return WatchdogStatus(
                state=self.state,
                elapsed_sec=elapsed,
                timeout_sec=self.silence_timeout_sec,
                consecutive_nudges=self.consecutive_nudges,
                max_nudges=self.max_nudges,
                has_started_call=self.has_started_call,
            )
