"""
Blue Archive All-Characters (196 Personas) — Real-Time Gemini × Fish Audio Bridge
--------------------------------------------------------------------------------
Architecture (PC Direct Execution):
  - Mode 1 (Default): PC Microphone (Continuous VAD + faster-whisper STT)
    -> Gemini Browser / API (uses user's Google Gemini Subscription or API Key)
    -> Fish Audio Streaming TTS ('s2.1-pro' / 's2.1-pro-free', 44.1kHz 16-bit PCM)
    -> Real PC Headphones / Speakers + Real-time JP/KR Subtitles
  - Mode 2: PC VB-Cable ('CABLE Output') -> Local CUDA faster-whisper -> Fish Audio Streaming TTS
Supports all 196 Blue Archive characters (151 Playable + 45 Named NPCs) with 100% Japanese Persona Prompts,
per-character Fish Audio voice memory, and 1-click Steam .molru studio reference WAV extraction!
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import datetime
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from math import ceil, gcd
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

_FALLBACK_REDACT_PATS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"\bsk-[0-9A-Za-z_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)(Bearer\s+)[^\s\"',;}\]]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(x-goog-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}\]]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:api_)?key=)[^&\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r'("(?:fish_api_key|gemini_api_key)"\s*:\s*")[^"]*(")'), r"\1[REDACTED]\2"),
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


def _ensure_project_venv_or_relaunch() -> None:
    """If double-clicked directly in Explorer (invoking global Python without .venv packages),
    transparently re-spawn using the project's .venv\\Scripts\\pythonw.exe and exit."""
    if os.environ.get("BA_VENV_BOOTSTRAPPED") == "1":
        return
    venv_dir = ROOT_DIR / ".venv"
    venv_pythonw = venv_dir / "Scripts" / "pythonw.exe"
    venv_python = venv_dir / "Scripts" / "python.exe"
    if not venv_dir.exists() or venv_dir.is_symlink():
        return
    try:
        venv_res = venv_dir.resolve()
        in_venv = Path(sys.prefix).resolve() == venv_res
    except Exception:
        return
    if not in_venv:
        target_exe = venv_pythonw if venv_pythonw.exists() else venv_python
        try:
            script_res = Path(__file__).resolve()
            exe_res = target_exe.resolve()
            if (
                not target_exe.is_file()
                or target_exe.is_symlink()
                or not exe_res.is_relative_to(venv_res)
                or not script_res.is_relative_to(ROOT_DIR.resolve())
            ):
                return
        except OSError:
            return
        env = os.environ.copy()
        for bad_var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"):
            env.pop(bad_var, None)
        env["PYTHONNOUSERSITE"] = "1"
        env["BA_VENV_BOOTSTRAPPED"] = "1"
        si = None
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 1  # SW_SHOWNORMAL
        subprocess.Popen(
            [str(exe_res), str(script_res), *sys.argv[1:]],
            cwd=str(ROOT_DIR),
            env=env,
            startupinfo=si,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        raise SystemExit(0)


def _report_fatal_startup_error(title: str, err_text: str) -> None:
    safe_title = _redact_secrets(title)
    safe_err = _redact_secrets(err_text)
    try:
        from src.utils.app_paths import get_logs_dir

        crash_log = get_logs_dir() / "bridge_crash.log"
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        if not crash_log.is_symlink():
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {safe_title}\n{safe_err}\n")
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(0, safe_err[:1800], safe_title, 0x10)
    except Exception:
        pass


_ensure_project_venv_or_relaunch()

_NVIDIA_SITE = ROOT_DIR / ".venv" / "Lib" / "site-packages" / "nvidia"
_CUDA_DLL_LOCK = threading.Lock()
_CUDA_DLL_READY = False
_RESAMPLE_POLY_FN: Any = None


def _ensure_cuda_dll_paths() -> None:
    """Inject verified project-venv CUDA (cuBLAS / cuDNN) DLL directories lazily right before loading faster_whisper."""
    global _CUDA_DLL_READY
    if _CUDA_DLL_READY:
        return
    with _CUDA_DLL_LOCK:
        if _CUDA_DLL_READY:
            return
        try:
            venv_res = (ROOT_DIR / ".venv").resolve()
            nv_res = _NVIDIA_SITE.resolve()
            if _NVIDIA_SITE.is_dir() and not _NVIDIA_SITE.is_symlink() and nv_res.is_relative_to(venv_res):
                for _bin_dir in _NVIDIA_SITE.glob("*/bin"):
                    if not _bin_dir.is_dir() or _bin_dir.is_symlink():
                        continue
                    _bin_res = _bin_dir.resolve()
                    if not _bin_res.is_relative_to(nv_res):
                        continue
                    _p = str(_bin_res)
                    os.environ["PATH"] = _p + ";" + os.environ.get("PATH", "")
                    if hasattr(os, "add_dll_directory"):
                        try:
                            os.add_dll_directory(_p)
                        except Exception:
                            pass
        except OSError:
            pass
        _CUDA_DLL_READY = True


def _resample_pcm_1d(audio: np.ndarray, up: int, down: int) -> np.ndarray:
    """Lazy-import `scipy.signal.resample_poly` on first audio resampling call (saves ~0.8-6.0s startup)."""
    global _RESAMPLE_POLY_FN
    if _RESAMPLE_POLY_FN is None:
        from scipy.signal import resample_poly as _sp_resample_poly

        _RESAMPLE_POLY_FN = _sp_resample_poly
    return _RESAMPLE_POLY_FN(audio, up, down).astype(np.float32)


class StreamingPolyphaseResampler:
    """
    Phase-continuous overlap-save polyphase FIR streaming resampler (e.g. 44.1kHz -> 48kHz WASAPI).
    Eliminates 43Hz metallic/buzzing chunk-boundary ringing caused by stateless per-chunk resample_poly
    while keeping lookahead latency at only ~3.3ms (147 samples at 44.1kHz).
    """

    def __init__(self, sr_from: int, sr_to: int) -> None:
        self.sr_from = int(sr_from)
        self.sr_to = int(sr_to)
        if self.sr_from == self.sr_to or self.sr_from <= 0 or self.sr_to <= 0:
            self.up = 1
            self.down = 1
            self.margin_in = 0
        else:
            g = gcd(self.sr_from, self.sr_to)
            self.up = self.sr_to // g
            self.down = self.sr_from // g
            min_ctx = max(32, int(ceil(20.0 * max(1.0, self.down / self.up))))
            self.margin_in = int(ceil(min_ctx / self.down)) * self.down
        self._prev_tail = np.empty(0, dtype=np.float32)
        self._pending = np.empty(0, dtype=np.float32)

    def process(self, chunk: np.ndarray) -> np.ndarray:
        if chunk.size == 0:
            return np.empty(0, dtype=np.float32)
        c32 = chunk.astype(np.float32, copy=False)
        if self.up == self.down:
            return c32
        self._pending = np.concatenate([self._pending, c32]) if self._pending.size else c32
        avail = self._pending.size - self.margin_in
        n_emit_in = (avail // self.down) * self.down
        if n_emit_in <= 0:
            return np.empty(0, dtype=np.float32)
        left_len = self._prev_tail.size
        right_slice = self._pending[: n_emit_in + self.margin_in]
        win = np.concatenate([self._prev_tail, right_slice]) if left_len else right_slice
        y = _resample_pcm_1d(win, self.up, self.down)
        left_out = (left_len // self.down) * self.up
        emit_out = (n_emit_in // self.down) * self.up
        out = y[left_out : left_out + emit_out]
        emit_end_in = left_len + n_emit_in
        self._prev_tail = win[max(0, emit_end_in - self.margin_in) : emit_end_in].copy()
        self._pending = self._pending[n_emit_in:].copy()
        return out

    def flush(self) -> np.ndarray:
        if self.up == self.down or self._pending.size == 0:
            self._pending = np.empty(0, dtype=np.float32)
            return np.empty(0, dtype=np.float32)
        left_len = self._prev_tail.size
        win = np.concatenate([self._prev_tail, self._pending]) if left_len else self._pending
        y = _resample_pcm_1d(win, self.up, self.down)
        left_out = (left_len // self.down) * self.up
        self._prev_tail = np.empty(0, dtype=np.float32)
        self._pending = np.empty(0, dtype=np.float32)
        return y[left_out:]


def _apply_micro_fade_1d(audio: np.ndarray, sr: int, fade_in: bool = False, fade_out: bool = False, fade_ms: float = 4.0) -> np.ndarray:
    """Apply smooth cosine micro-fade at clause start/end to prevent click/pop artifacts."""
    if audio.size <= 4 or (not fade_in and not fade_out):
        return audio
    n_fade = min(max(2, int(sr * (fade_ms / 1000.0))), audio.size // 2)
    out = audio.copy()
    ramp = (0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, n_fade, dtype=np.float32)))).astype(np.float32)
    if fade_in:
        out[:n_fade] *= ramp
    if fade_out:
        out[-n_fade:] *= ramp[::-1]
    return out


try:
    import httpx
    import msgpack
    import numpy as np
    import pyperclip
    import requests
    import sounddevice as sd

    from src.controller.companion_state_machine import (
        CompanionState,
        CompanionStateMachine,
    )
    from src.controller.gemini_connector import (
        GEMINI_API_MODEL_LABELS,
        GEMINI_API_MODELS,
        GEMINI_BROWSER_MODEL_LABELS,
        GEMINI_BROWSER_MODELS,
        GEMINI_THINKING_LEVEL_LABELS,
        GEMINI_THINKING_LEVELS,
        IDLE_PING_TOKEN,
        KOREAN_VOICE_OUTPUT_LOCK,
        GeminiSessionManager,
        has_korean_script,
        sanitize_japanese_tts_text,
        extract_voice_tag_prefix,
        sanitize_korean_tts_text,
        split_legacy_browser_model,
        strip_voice_persona_tags,
    )
    from src.controller.jp_conversation_coach import build_jp_beginner_lover_coach_prompt
    from src.controller.local_mic_stt import (
        LocalMicSttEngine,
        list_microphone_labels,
    )
    from src.controller.memory_dialog import MemoryNoteDialog
    from src.controller.memory_manager import (
        MEMORY_TRACK_LABELS,
        MemoryManager,
        parse_memory_extraction_json,
        persona_variant_to_memory_track,
    )
    from src.controller.proactive_lead_engine import (
        ProactiveWatchdog,
        _norm_dev_key,
        build_proactive_lead_prompt,
        contains_trigger_echo,
        ensure_trigger_wavs_cached,
        list_primary_output_labels,
        list_trigger_output_labels,
        match_device_label_by_name,
        pick_default_trigger_output,
    )
    from src.controller.settings_dialog import (
        BOOST_LABEL_TO_VAL,
        BOOST_PRESET_LABELS,
        ENABLE_ON_DEMAND_VOICE_UI,
        PERSONA_LABEL_TO_CODE,
        PERSONA_VARIANT_LABELS,
        PROACTIVE_TIMEOUT_LABELS,
        PROACTIVE_TIMEOUT_VALS,
        PROACTIVE_VOL_LABELS,
        PROACTIVE_VOL_VALS,
        SPEED_LABEL_TO_CODE,
        SPEED_MODE_LABELS,
        STT_LABEL_TO_CODE,
        STT_LANG_LABELS,
        SettingsDialog,
    )
    from src.extractor.ba_molru_extractor import (
        is_valid_ba_game_dir,
        resolve_ba_game_dir,
    )
    from src.extractor.ba_momotalk_assets import (
        MomoTalkAssetManager,
        MomoTalkCallAudioFX,
        RingbackStreamState,
    )
    from src.controller.subtitle_translator import (
        SUBTITLE_ENGINE_LABELS,
        SUBTITLE_ENGINES,
        SUBTITLE_MODES,
        SUBTITLE_MODE_LABELS,
        RealtimeSubtitleTranslator,
        SubtitleEntry,
        build_character_ko_style_guide,
        contains_japanese,
        get_subtitle_engine_choices,
        resolve_subtitle_engine_label,
    )
    from src.extractor.build_fish_reference import (
        DEFAULT_QUALITY_PROFILE,
        build_alt_voice_char_data,
        build_character_fish_reference,
        clear_steam_voice_availability_cache,
        select_upload_samples_with_transcript_lock,
    )
    from src.utils.app_paths import (
        get_browser_profile_dir,
        get_cache_dir,
        get_config_path,
        get_logs_dir,
        get_trigger_cache_dir,
        get_user_models_dir,
        load_user_config,
        migrate_legacy_workspace_data,
        save_user_config,
    )
except Exception:
    _report_fatal_startup_error("보이스 브릿지 모듈 로드 오류", traceback.format_exc())
    raise

migrate_legacy_workspace_data(ROOT_DIR)
CONFIG_PATH = get_config_path()
CHARACTERS_DIR = ROOT_DIR / "config" / "characters"
MOMOTALK_PROFILES_PATH = ROOT_DIR / "config" / "momotalk_profiles.json"
RUNTIME_LOG_PATH = get_logs_dir() / "bridge_runtime.log"
TRIGGER_CACHE_DIR = get_trigger_cache_dir()

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_MODEL_URL = "https://api.fish.audio/model"
FISH_PCM_SR = 44100
WHISPER_SR = 16000

# Runtime security & DoS guardrails
MAX_TTS_STREAM_BYTES = 32 * 1024 * 1024           # 32 MB (~6 min 44.1kHz PCM) per TTS stream
MAX_HTTP_ERROR_BYTES = 4096                       # 4 KB max read for HTTP error diagnostics
MAX_FISH_SEARCH_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MB max JSON body for Fish model search
MAX_ZEROSHOT_WAV_BYTES = 25 * 1024 * 1024         # 25 MB max local reference WAV read
MAX_CHAR_CONFIG_BYTES = 5 * 1024 * 1024           # 5 MB max character JSON file size
_ALLOWED_WHISPER_MODELS = frozenset({"tiny", "base", "small", "medium"})
_SAFE_ENTITY_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,79}\Z")
_WIN_RESERVED_IDS = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_FISH_HEX_STRICT_RE = re.compile(r"\A[0-9a-fA-F]{32}\Z")
_FISH_MODEL_PATH_RE = re.compile(r"\A/(?:[a-zA-Z_-]{2,10}/)?m/([0-9a-fA-F]{32})/?\Z")
_ALLOWED_FISH_HOSTS = frozenset({"fish.audio", "www.fish.audio", "api.fish.audio"})


def is_safe_entity_id(value: str) -> bool:
    """Return True iff `value` is a strictly safe flat character/entry ID (zero path traversal or reserved names)."""
    s = str(value or "").strip()
    if not s or not _SAFE_ENTITY_ID_RE.match(s):
        return False
    return s.upper() not in _WIN_RESERVED_IDS


def is_valid_fish_model_id(raw: str) -> bool:
    """Return True iff `raw` is a strictly valid 32-char hexadecimal Fish Audio model ID."""
    return bool(_FISH_HEX_STRICT_RE.match(str(raw or "").strip()))


def _validate_fish_api_url(url: str) -> str:
    """Enforce HTTPS-only requests strictly pinned to `api.fish.audio` (blocks SSRF / scheme downgrade / traversal)."""
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


def build_fish_model_url(model_id: str) -> str:
    """Validate `model_id` strictly before interpolating into `https://api.fish.audio/model/{id}`."""
    clean_id = extract_model_id(model_id)
    if not is_valid_fish_model_id(clean_id):
        raise ValueError(f"Invalid Fish Audio model ID: {model_id!r}")
    return _validate_fish_api_url(f"{FISH_MODEL_URL}/{clean_id}")


def _safe_explorer_exe() -> Path:
    """Return absolute path to Windows Explorer in %SystemRoot% to prevent CWD/PATH binary hijacking."""
    sys_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return sys_root / "explorer.exe"


def _reveal_file_in_explorer(target_file: Path, allowed_roots: tuple[Path, ...]) -> bool:
    """Reveal a verified regular file inside `allowed_roots` in Windows Explorer without PATH hijack."""
    if os.name != "nt":
        return False
    try:
        if target_file.is_symlink() or not target_file.is_file():
            return False
        res = target_file.resolve()
        if not any(res.is_relative_to(r.resolve()) for r in allowed_roots):
            return False
        s_path = str(res)
        if '"' in s_path or any(ord(ch) < 32 for ch in s_path):
            return False
        explorer = _safe_explorer_exe()
        if not explorer.is_file():
            return False
        subprocess.Popen([str(explorer), f'/select,"{s_path}"'])
        return True
    except Exception:
        return False


def _open_directory_in_explorer(target_dir: Path, allowed_roots: tuple[Path, ...]) -> bool:
    """Open a verified directory inside `allowed_roots` in Windows Explorer (avoids shell-verb `os.startfile`)."""
    if os.name != "nt":
        return False
    try:
        if target_dir.is_symlink() or not target_dir.is_dir():
            return False
        res = target_dir.resolve()
        if not any(res == r.resolve() or res.is_relative_to(r.resolve()) for r in allowed_roots):
            return False
        explorer = _safe_explorer_exe()
        if not explorer.is_file():
            return False
        subprocess.Popen([str(explorer), str(res)])
        return True
    except Exception:
        return False


def _read_bounded_httpx_error(resp: Any, max_bytes: int = MAX_HTTP_ERROR_BYTES) -> str:
    """Read at most `max_bytes` from a streaming httpx error response to prevent OOM on hostile bodies."""
    buf = bytearray()
    try:
        for chunk in resp.iter_bytes(chunk_size=1024):
            if not chunk:
                continue
            need = max_bytes - len(buf)
            if need <= 0:
                break
            buf.extend(chunk[:need])
            if len(buf) >= max_bytes:
                break
    except Exception:
        pass
    return buf.decode("utf-8", errors="replace")

# Common Whisper silence/chime hallucinations to ignore (JP, KO, EN)
HALLUCINATION_PHRASES = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございました。",
    "チャンネル登録お願いします",
    "チャンネル登録よろしくお願いします",
    "字幕",
    "お疲れ様でした",
    "ありがとうございました。",
    "시청해 주셔서 감사합니다",
    "시청해 주셔서 감사합니다.",
    "시청해주셔서 감사합니다.",
    "구독과 좋아요 부탁드립니다",
    "구독과 좋아요 부탁드립니다.",
    "감사합니다.",
    "자막 제공",
    "you",
    "You",
    "you.",
    "You.",
    "Thank you.",
    "Thank you",
}

PATCH15_FILTER_LABEL = "⭐ 정밀패치 학생 (추천)"


def is_patch15_character(c: dict) -> bool:
    """Return True when the character includes the 15-person persona & spike/whisper directing patch."""
    persona = c.get("persona") or {}
    vap = persona.get("voice_acting_profile") or {}
    return bool(persona.get("patch_kit_v15") or vap.get("spike_directing") or vap.get("whisper_directing"))


def _mono_to_stereo(mono: np.ndarray) -> np.ndarray:
    """Duplicate 1-D float32 mono samples into contiguous 2-ch stereo without column_stack overhead."""
    out = np.empty((mono.shape[0], 2), dtype=np.float32)
    out[:, 0] = mono
    out[:, 1] = mono
    return out


def resolve_voice_persona_prompt(char_data: dict, variant_code: str) -> tuple[str, str]:
    """Return (japanese_voice_prompt, human_label) keeping Official Prompt 1 strictly separate from 2차창작 4종."""
    persona = char_data.get("persona") or {}
    fanon = persona.get("fanon_dc_ba_gallery") or {}
    if variant_code == "fanon_mainstream":
        p = fanon.get("voice_prompt_mainstream_jp") or persona.get("gemini_live_voice_prompt", "")
        return p, PERSONA_VARIANT_LABELS["fanon_mainstream"]
    if variant_code == "fanon_pure_love":
        p = fanon.get("voice_prompt_pure_love_jp") or persona.get("gemini_live_voice_prompt", "")
        return p, PERSONA_VARIANT_LABELS["fanon_pure_love"]
    if variant_code == "fanon_spicy_meme":
        p = fanon.get("voice_prompt_spicy_meme_jp") or persona.get("gemini_live_voice_prompt", "")
        return p, PERSONA_VARIANT_LABELS["fanon_spicy_meme"]
    if variant_code == "fanon_optimal_blend":
        p = (
            fanon.get("voice_prompt_optimal_blend_jp")
            or fanon.get("voice_prompt_mainstream_jp")
            or persona.get("gemini_live_voice_prompt", "")
        )
        return p, PERSONA_VARIANT_LABELS["fanon_optimal_blend"]
    return persona.get("gemini_live_voice_prompt", ""), PERSONA_VARIANT_LABELS["official"]

_STAGE_DIR_RE = re.compile(r"\*[^*]{1,40}\*")
_OPEN_SIGH_RE = re.compile(r"^(はあ|はぁ|ふぅ|ふーむ|まったく|ったく|やれやれ)(?![…、。！？!?\s])")
_OPEN_HESIT_RE = re.compile(r"^(えっと|あの|その|うーん|うぅ|えーっと)(?![…、。！？!?\s])")
_OPEN_NN_RE = re.compile(r"^ん(?=[私先生お前あなただいじょうぶそう良いいえ違今今日])")
_OPEN_GASP_RE = re.compile(r"^(なっ|ちょっ|ひゃっ|ひぃっ|うわっ|えぇっ|ええっ)(?![…、。！？!?とてたちゃちでだ])")
_END_PUNCT_RE = re.compile(r"[。！？!?…♪〜~、]$")
_QUESTION_END_RE = re.compile(
    r"(ですか|ますか|でしょうか|かしら|かな|だよね|でしょ|なの|ですかね|ますかね|かい|のか|ないか|じゃない|だっけ)$"
)
_EXCLAIM_END_RE = re.compile(
    r"(なさい|わよ|だぞ|のだ|ですの|もん|死刑|くださいっ|ませんっ|からね|ますよ|だよっ|だもん|いくよ|ますっ|ですっ)$"
)
_TRAIL_END_RE = re.compile(r"(けど|からね|かなあ|だなあ|だねえ)$")

_CUE_FLUSTER_RE = re.compile(
    r"(！？|\?!|なっ……|ちょ、|べ、別に|い、いえ|ち、違|あ、あわわ|だ、誰が|わ、わわ|ひゃっ|ひぃっ|えぇっ|ええっ|うえぇ|うわあ|うそっ|待って！？|そんなんじゃ|死刑|エッチ|ふざけないで|かわ…いい|ずるいです|顔が赤)"
)
_CUE_SIGH_RE = re.compile(r"(^はあ|^はぁ|^ふぅ|……はあ|……はぁ|まったく|やれやれ|めんどくさ|眠い|ふわぁ|疲れた)")
_CUE_WRITTEN_SIGH_START_RE = re.compile(r"^(はあ|はぁ|ふぅ|ふわぁ)")
_CUE_LAUGH_RE = re.compile(r"(ふふっ|フフッ|あはは|アハハ|くすっ|うふふ|えへへ|にひひ|あはっ|きひひ|わっぴー|ざぁこ)")
_CUE_WRITTEN_LAUGH_START_RE = re.compile(r"^(ふふ|フフ|あはは|アハハ|くす|うふふ|えへへ|にひひ|あはっ|きひひ)")
_CUE_SCOLD_RE = re.compile(r"(ダメです|いけません|許しません|反省して|いい加減に|怒ります|帰れるとは|減給|没収|遅刻|サボり)")
_CUE_SHY_SOFT_RE = re.compile(
    r"(ありがと……|ありがと…|内緒|ここだけ|そばにいて|嫌いじゃありません|嫌いではありません|悪い気はしない|ほ、本当ですか|あ、あの|可愛い以外も|バカ[、…\.]|馬鹿[、…\.]|特別に……|膝の上|耳かき)"
)
_CUE_POUT_TEASE_RE = re.compile(
    r"(ぶー|むーっ|むー[、…]|ほっぺ|つまんな|くすん|ひどい|ぷんぷん|パヒャヒャ|ぱひゃひゃ|めっ！|メーッ|ダメですよ〜)"
)
_CUE_SERIOUS_GUARD_RE = re.compile(r"(手出し|指一本|許さない|退いて|退きな|後輩に|本気出す|守るから)")
_CUE_FANFARE_RE = re.compile(r"(パンパカパーン|ぱんぱかぱーん|レベルアップ|勇者|光よ)")
_CUE_HUMID_LOVE_RE = re.compile(
    r"(二人きり|帰さない|帰しません|他の生徒|他の子|私だけ|あたしだけ|独占|離さない|離しません|ずっとそば|ぎゅっ|撫でて|なでなで|正妻|泊まって|心臓の音|ドキドキ|好き……|大好き……)"
)
_CUE_EXCITED_RE = re.compile(r"(！.*！|最高|すごい|やった|大好き|行きましょう！|任せてください！|完璧|大勝利)")
_CUE_LOVER_COACH_WARM_RE = re.compile(
    r"(ゆっくりでいい|大丈夫だよ|大丈夫よ|無理しなくて|気にしなくていい|ちゃんと伝わ|わかったよ|分かったよ|えらい|偉い|よしよし|そばにいる)"
)
_SPIKE_EXCITED_RE = re.compile(r"(元気|輝|パンパカ|ハイテンション)")
_SPIKE_SERIOUS_RE = re.compile(r"(真剣|鋭く|窘め|メーッ|厳しく)")
_TSUN_DEFLECT_RE = re.compile(r"(別に|嫌い|検討|勘違い|何なんですか)")
_PLAYFUL_TAG_RE = re.compile(r"(弾ん|悪戯|明る|嬉し)")
_FISH_URL_RE = re.compile(r"fish\.audio/(?:[a-zA-Z_-]+/)?m/([a-zA-Z0-9_-]+)")
_FISH_HEX_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")
_JP_PAREN_SPLIT_RE = re.compile(r"[（(]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_CUSTOM_MD_STRIP_RE = re.compile(r"[*#_~>`]+")
_ECHO_NORM_RE = re.compile(r"[\s。、．，.!！?？「」『』()（）\[\]…・~〜ー\-]+")
_HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]")
_KO_FLUSTER_RE = re.compile(r"(！？|\?!|으아악|어억|자,\s*잠깐|무,\s*무슨|아,\s*아니|에엣|허억|헉|말도 안|부끄러)")
_KO_LAUGH_RE = re.compile(r"(후훗|후후|우하하|아하하|하하|히히|헤헤|크크|키키킥|키키킷|키키키)")
_ALT_MONARCH_BLOCK_RE = re.compile(r"(妾|其方|のじゃ|じゃぞ|じゃろう|거라|꾸나|노라|문주|門主|아니한가|하거라)")
_ALT_POLITE_CUE_RE = re.compile(r"(です|ます|ください|ですよ|ますよ|예요|에요|어요|아요|해요|세요|까요|죠|선생님|先生|！|!)")


def resolve_sentence_voice_persona(char_data: dict[str, Any], raw_sentence: str) -> tuple[str, str]:
    """
    Resolve sentence-level dual-voice switching (e.g. Kisaki <-> Kiki via `[VOICE:kiki]` or smart fallback).
    Always returns `(alt_voice_key_or_empty, clean_text_without_voice_tags)`.
    """
    explicit_key, clean_text = extract_voice_tag_prefix(raw_sentence or "")
    alts = char_data.get("alt_voice_personas") if isinstance(char_data, dict) else None
    if not isinstance(alts, dict) or not alts:
        return "", clean_text

    if explicit_key in ("base", "default", "main", "normal"):
        return "", clean_text
    if explicit_key:
        for k in alts.keys():
            if k.lower() == explicit_key.lower():
                return k, clean_text

    # Smart fallback: if Gemini (or Mode 2 Whisper STT) speaks as Kiki without emitting `[VOICE:kiki]`,
    # detect Kiki's cheerful self-reference + polite speech while blocking solemn monarch lines.
    if clean_text and not _ALT_MONARCH_BLOCK_RE.search(clean_text):
        for alt_key, spec in alts.items():
            if not isinstance(spec, dict):
                continue
            keywords = [str(w) for w in (spec.get("auto_detect_keywords") or []) if str(w)]
            if not keywords:
                continue
            probe = clean_text
            for exc_pat in (spec.get("auto_detect_exclude_patterns") or []):
                if exc_pat:
                    probe = probe.replace(str(exc_pat), "")
            if any(kw in probe for kw in keywords) and _ALT_POLITE_CUE_RE.search(probe):
                return alt_key, clean_text

    return "", clean_text


def build_alt_voice_prompt_block(char_data: dict[str, Any], reply_lang: str = "ja") -> str:
    """Build Gemini prompt instructions for any declared `alt_voice_personas` (e.g. Kisaki -> `[VOICE:kiki]`)."""
    alts = char_data.get("alt_voice_personas") if isinstance(char_data, dict) else None
    if not isinstance(alts, dict) or not alts:
        return ""
    is_ko = str(reply_lang or "ja").strip().lower() == "ko"
    blocks: list[str] = []
    for spec in alts.values():
        if not isinstance(spec, dict):
            continue
        rule = str(spec.get("prompt_rule_kr") if is_ko else spec.get("prompt_rule_jp") or "").strip()
        if rule:
            blocks.append(rule)
    return "\n\n".join(blocks)
_KO_SIGH_RE = re.compile(r"(^하아|^후우|^휴우|^에휴|……하아|피곤|졸려|귀찮)")
_KO_SHY_SOFT_RE = re.compile(r"(고마워|감사해요|미안|비밀|여기만|옆에 있어|좋아해|좋아해요|조금만|……|\.\.\.)")
_KO_SCOLD_RE = re.compile(r"(안 돼|안돼|안 됩니다|용서 못|반성|지각|땡땡이|몰수|혼날)")
_KO_EXCITED_RE = re.compile(r"(!.*!|최고|대단|해냈|맡겨|완벽|대승리|가자!|가요!)")


def _direct_korean_character_prosody(
    char_data: dict,
    text_out: str,
    clause_rms: float,
    auto_direct: bool,
    boost_offset: float,
    model_hdr: str,
    persona_variant: str,
) -> dict[str, Any]:
    """Korean-safe prosody directing: preserves character temperature dynamics using universal ASCII Fish s2 tags only."""
    vap = ((char_data.get("persona") or {}).get("voice_acting_profile")) or {}
    arch = str(vap.get("archetype") or "gentle_earnest_passion")
    base_temp = float(vap.get("base_temperature", 0.72))
    spike_temp = float(vap.get("spike_temperature", 0.84))
    top_p = float(vap.get("top_p", 0.80))
    rep_pen = float(vap.get("repetition_penalty", 1.13))
    forbidden = set(vap.get("forbidden_s2_tags") or [])

    def _pick_ascii(cands: list[str]) -> str:
        for tag in cands:
            if tag and tag not in forbidden:
                return tag
        return ""

    cue = "ko_baseline"
    target_temp = base_temp
    chosen_tag = ""

    if _KO_FLUSTER_RE.search(text_out):
        cue = "ko_fluster"
        target_temp = spike_temp + (0.02 if persona_variant in ("fanon_spicy_meme", "fanon_optimal_blend") else 0.0)
        if arch in ("cool_downer", "kuudere_deadpan"):
            chosen_tag = _pick_ascii(["[whisper]"])
        else:
            chosen_tag = _pick_ascii(["[excited]"])
    elif _KO_SIGH_RE.search(text_out):
        cue = "ko_sigh"
        target_temp = base_temp
        chosen_tag = _pick_ascii(["[sigh]"])
    elif _KO_LAUGH_RE.search(text_out):
        cue = "ko_laugh"
        target_temp = 0.5 * (base_temp + spike_temp)
        chosen_tag = _pick_ascii(["[laughs]", "[happy]"])
    elif _KO_SCOLD_RE.search(text_out):
        cue = "ko_scold"
        target_temp = 0.35 * base_temp + 0.65 * spike_temp
        chosen_tag = _pick_ascii(["[angry]"])
    elif _KO_SHY_SOFT_RE.search(text_out):
        cue = "ko_shy_soft"
        target_temp = 0.6 * base_temp + 0.4 * spike_temp
        chosen_tag = _pick_ascii(["[whisper]"])
    elif _KO_EXCITED_RE.search(text_out) or ("!" in text_out and clause_rms >= 0.16):
        cue = "ko_excited"
        if arch in ("cool_downer", "kuudere_deadpan"):
            target_temp = min(spike_temp, base_temp + 0.05)
        else:
            target_temp = 0.3 * base_temp + 0.7 * spike_temp
            chosen_tag = _pick_ascii(["[happy]", "[excited]"])

    if auto_direct:
        final_temp = round(max(0.48, min(0.92, target_temp + boost_offset)), 2)
        if arch in ("cool_downer", "kuudere_deadpan") and boost_offset <= 0.02:
            final_temp = round(min(final_temp, spike_temp + max(0.0, boost_offset)), 2)
    else:
        final_temp = round(base_temp, 2)
        chosen_tag = ""

    use_s2_tags = auto_direct and bool(chosen_tag) and model_hdr.strip().lower().startswith("s2")
    tts_text = f"{chosen_tag} {text_out}".strip() if use_s2_tags else text_out
    return {
        "clean_text": text_out,
        "tts_text": tts_text,
        "s2_tag": chosen_tag if use_s2_tags else "",
        "cue": cue,
        "temperature": final_temp,
        "top_p": round(min(top_p, 0.80), 2),
        "repetition_penalty": round(rep_pen, 2),
    }


def direct_character_emotion_and_prosody(
    char_data: dict,
    raw_text: str,
    clause_rms: float = 0.0,
    auto_direct: bool = True,
    boost_offset: float = 0.0,
    model_hdr: str = "s2.1-pro-free",
    persona_variant: str = "official",
    jp_beginner_coach: bool = False,
) -> dict[str, Any]:
    """
    Restore spoken Japanese punctuation dropped by STT and compute character-safe
    Fish Audio s2 acting tag + temperature/top_p/repetition_penalty strictly within
    each character's `persona.voice_acting_profile` (zero OOC high-tension flattening!).
    Supports `spike_directing` / `whisper_directing` from the 15-character patch
    and nuanced prosody tuning across Official vs 2차창작 variants.
    """
    text_clean_probe = _STAGE_DIR_RE.sub("", raw_text).strip()
    if text_clean_probe and _HANGUL_RE.search(text_clean_probe) and not _KANA_RE.search(text_clean_probe):
        return _direct_korean_character_prosody(
            char_data,
            text_clean_probe,
            clause_rms=clause_rms,
            auto_direct=auto_direct,
            boost_offset=boost_offset,
            model_hdr=model_hdr,
            persona_variant=persona_variant,
        )
    vap = ((char_data.get("persona") or {}).get("voice_acting_profile")) or {}
    arch = str(vap.get("archetype") or "gentle_earnest_passion")
    base_temp = float(vap.get("base_temperature", 0.72))
    spike_temp = float(vap.get("spike_temperature", 0.84))
    top_p = float(vap.get("top_p", 0.85))
    rep_pen = float(vap.get("repetition_penalty", 1.13))
    default_tag = str(vap.get("default_s2_tag") or "")
    spike_tag = str(vap.get("spike_directing") or vap.get("spike_s2_tag") or "")
    whisper_tag = str(vap.get("whisper_directing") or vap.get("whisper_s2_tag") or "")
    allowed = list(vap.get("allowed_s2_tags") or [])
    forbidden = set(vap.get("forbidden_s2_tags") or [])

    spike_is_excited = bool(spike_tag and _SPIKE_EXCITED_RE.search(spike_tag))
    spike_is_serious_or_scold = bool(spike_tag and _SPIKE_SERIOUS_RE.search(spike_tag))
    spike_is_fluster = bool(spike_tag and not spike_is_excited and not spike_is_serious_or_scold)
    bespoke_tags = {t for t in (default_tag, spike_tag, whisper_tag) if t}

    text_out = _STAGE_DIR_RE.sub("", raw_text).strip()
    if text_out:
        text_out = _OPEN_SIGH_RE.sub(r"\1……", text_out)
        text_out = _OPEN_HESIT_RE.sub(r"\1……", text_out)
        text_out = _OPEN_NN_RE.sub("ん、", text_out)
        text_out = _OPEN_GASP_RE.sub(r"\1……！？", text_out)
        if not _END_PUNCT_RE.search(text_out):
            if _QUESTION_END_RE.search(text_out):
                text_out += "？"
            elif arch not in ("cool_downer", "kuudere_deadpan", "lazy_my_pace") and _EXCLAIM_END_RE.search(text_out):
                text_out += "！"
            elif (arch in ("cool_downer", "lazy_my_pace") or persona_variant == "fanon_pure_love") and _TRAIL_END_RE.search(text_out):
                text_out += "……"

    def _pick_tag(candidates: list[str]) -> str:
        for cand in candidates:
            if cand and cand not in forbidden and (cand in bespoke_tags or not allowed or cand in allowed):
                return cand
        if default_tag and default_tag not in forbidden:
            return default_tag
        return ""

    cue = "baseline"
    target_temp = base_temp
    chosen_tag = _pick_tag([default_tag])

    if (
        (persona_variant in ("fanon_pure_love", "fanon_mainstream", "fanon_optimal_blend") or jp_beginner_coach)
        and _CUE_HUMID_LOVE_RE.search(text_out)
        and "！？" not in text_out
    ):
        cue = "humid_pure_love"
        target_temp = 0.55 * base_temp + 0.45 * spike_temp
        chosen_tag = _pick_tag(
            [
                whisper_tag,
                "[少し照れながら静かに]",
                "[甘く余裕のある声で]",
                "[不意を突かれて照れながら]",
                "[ほっとして嬉しそうに]",
                "[恥ずかしそうに慌てながら]",
                "[whisper]",
                default_tag,
            ]
        )
    elif spike_is_excited and _CUE_FANFARE_RE.search(text_out):
        cue = "fanfare_spike"
        target_temp = spike_temp
        chosen_tag = _pick_tag([spike_tag, "[excited]", default_tag])
    elif spike_is_serious_or_scold and _CUE_SERIOUS_GUARD_RE.search(text_out):
        cue = "serious_spike"
        target_temp = spike_temp
        chosen_tag = _pick_tag([spike_tag, default_tag])
    elif _CUE_FLUSTER_RE.search(text_out):
        cue = "fluster_panic"
        target_temp = spike_temp + (
            0.03 if persona_variant == "fanon_spicy_meme" else (0.02 if persona_variant == "fanon_optimal_blend" else 0.0)
        )
        first_fluster = [spike_tag] if spike_is_fluster else []
        if arch == "cool_downer":
            chosen_tag = _pick_tag(first_fluster + ["[少し照れながら静かに]", whisper_tag, "[whisper]", default_tag])
        elif arch == "kuudere_deadpan":
            chosen_tag = _pick_tag(first_fluster + ["[少し戸惑いながら静かに]", whisper_tag, "[whisper]", default_tag])
        elif arch == "lazy_my_pace":
            chosen_tag = _pick_tag(first_fluster + ["[不意を突かれて少し慌てながら]", whisper_tag, default_tag])
        elif arch == "sharp_tsundere":
            if _TSUN_DEFLECT_RE.search(text_out):
                chosen_tag = _pick_tag(first_fluster + ["[照れ隠しで強がりながら]", "[焦って慌てながら]", default_tag])
            else:
                chosen_tag = _pick_tag(first_fluster + ["[焦って慌てながら]", "[照れ隠しで強がりながら]", default_tag])
        elif arch == "bluff_gap_comic":
            chosen_tag = _pick_tag(first_fluster + ["[大慌てでパニックになりながら]", "[咳払いして必死に取り繕いながら]", "[excited]", default_tag])
        elif arch == "teasing_ojou_ara":
            chosen_tag = _pick_tag(["[不意を突かれて照れながら]", whisper_tag, "[whisper]", default_tag])
        elif arch == "timid_fragile":
            chosen_tag = _pick_tag(first_fluster + ["[大慌てでパニックになりながら]", "[びっくりして慌てながら]", "[おどおどと控えめな声で]", default_tag])
        elif arch == "genki_high_tension":
            chosen_tag = _pick_tag(first_fluster + ["[びっくりして声を上げて]", "[可愛く拗ねながら]", "[excited]", default_tag])
        else:
            chosen_tag = _pick_tag(first_fluster + ["[恥ずかしそうに慌てながら]", default_tag])
    elif spike_tag and _CUE_POUT_TEASE_RE.search(text_out):
        cue = "character_spike"
        target_temp = 0.3 * base_temp + 0.7 * spike_temp
        chosen_tag = _pick_tag([spike_tag, default_tag])
    elif _CUE_SIGH_RE.search(text_out):
        cue = "sigh_tired"
        target_temp = base_temp
        if _CUE_WRITTEN_SIGH_START_RE.search(text_out):
            chosen_tag = _pick_tag(["[呆れたため息まじりに]", "[気だるそうにゆったりと]", "[気だるそうに]", "[落ち着いた低い声で]", default_tag])
        else:
            chosen_tag = _pick_tag(["[sigh]", "[呆れたため息まじりに]", "[気だるそうに]", default_tag])
    elif _CUE_LAUGH_RE.search(text_out):
        cue = "laugh_tease"
        target_temp = 0.5 * (base_temp + spike_temp) + (
            0.02 if persona_variant == "fanon_spicy_meme" else (0.01 if persona_variant == "fanon_optimal_blend" else 0.0)
        )
        if _CUE_WRITTEN_LAUGH_START_RE.search(text_out):
            chosen_tag = _pick_tag(
                ["[悪戯っぽく笑って]", "[悪戯っぽく緩い声で]", "[甘く余裕のある声で]", "[明るく弾む声で]", "[自信満々に得意げな声で]", default_tag]
            )
        else:
            chosen_tag = _pick_tag(["[laughs]", "[悪戯っぽく笑って]", "[happy]", default_tag])
    elif _CUE_SCOLD_RE.search(text_out):
        cue = "scold_angry"
        target_temp = 0.35 * base_temp + 0.65 * spike_temp + (
            0.03 if persona_variant == "fanon_spicy_meme" else (0.02 if persona_variant == "fanon_optimal_blend" else 0.0)
        )
        scold_first = [spike_tag] if spike_is_serious_or_scold else []
        chosen_tag = _pick_tag(scold_first + ["[少し厳しくハキハキと]", "[angry]", "[可愛く拗ねながら]", default_tag])
    elif _CUE_SHY_SOFT_RE.search(text_out):
        cue = "shy_soft"
        target_temp = 0.6 * base_temp + 0.4 * spike_temp
        chosen_tag = _pick_tag([whisper_tag, "[少し照れながら静かに]", "[ほっとして嬉しそうに]", "[恥ずかしそうに慌てながら]", "[whisper]", default_tag])
    elif _CUE_EXCITED_RE.search(text_out) or ("！" in text_out and clause_rms >= 0.16):
        cue = "excited_passion"
        if arch in ("cool_downer", "kuudere_deadpan"):
            target_temp = min(spike_temp, base_temp + 0.05)
            chosen_tag = _pick_tag([default_tag])
        elif arch == "lazy_my_pace":
            target_temp = 0.45 * base_temp + 0.55 * spike_temp
            playful_allowed = [t for t in allowed if _PLAYFUL_TAG_RE.search(t)]
            chosen_tag = _pick_tag(
                ["[無邪気に少し弾んだ明るい声で]", "[悪戯っぽく緩い声で]"] + playful_allowed + [default_tag]
            )
        else:
            target_temp = 0.25 * base_temp + 0.75 * spike_temp
            exc_first = [spike_tag] if spike_is_excited else []
            chosen_tag = _pick_tag(exc_first + ["[excited]", "[目を輝かせて熱心に]", "[明るく弾む声で]", "[happy]", "[自信満々に得意げな声で]", default_tag])
    else:
        if (
            (persona_variant in ("fanon_pure_love", "fanon_optimal_blend") and "……" in text_out)
            or (whisper_tag and text_out.startswith("……"))
            or (jp_beginner_coach and _CUE_LOVER_COACH_WARM_RE.search(text_out) and "！？" not in text_out)
        ):
            cue = (
                "lover_coach_warm"
                if (jp_beginner_coach and _CUE_LOVER_COACH_WARM_RE.search(text_out))
                else "humid_soft_baseline"
            )
            if cue == "lover_coach_warm":
                target_temp = 0.65 * base_temp + 0.35 * spike_temp
            chosen_tag = _pick_tag([whisper_tag, "[少し照れながら静かに]", "[甘く余裕のある声で]", "[ほっとして嬉しそうに]", "[whisper]", default_tag])
        elif clause_rms >= 0.16 and arch not in ("cool_downer", "kuudere_deadpan"):
            target_temp = min(spike_temp, base_temp + 0.04)

    if auto_direct:
        final_temp = round(max(0.48, min(0.94, target_temp + boost_offset)), 2)
        if arch in ("cool_downer", "kuudere_deadpan") and boost_offset <= 0.02:
            final_temp = round(min(final_temp, spike_temp + max(0.0, boost_offset)), 2)
    else:
        final_temp = round(base_temp, 2)
        chosen_tag = ""

    use_s2_tags = auto_direct and bool(chosen_tag) and model_hdr.strip().lower().startswith("s2")
    tts_text = f"{chosen_tag} {text_out}".strip() if use_s2_tags else text_out
    return {
        "clean_text": text_out,
        "tts_text": tts_text,
        "s2_tag": chosen_tag if use_s2_tags else "",
        "cue": cue,
        "temperature": final_temp,
        "top_p": round(min(top_p, 0.80), 2),
        "repetition_penalty": round(rep_pen, 2),
    }


def load_bridge_config() -> dict:
    return load_user_config(app_root=ROOT_DIR)


def save_bridge_config(cfg: dict) -> None:
    save_user_config(cfg)


def load_all_character_configs() -> dict[str, dict]:
    """Load all config/characters/*.json sorted by Korean name using a signature-validated cache."""
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    chars_dir_res = CHARACTERS_DIR.resolve()
    dir_entries: list[tuple[str, int, int]] = []
    total_size = 0
    max_mtime_ns = 0
    with os.scandir(CHARACTERS_DIR) as it:
        for de in it:
            if de.is_file(follow_symlinks=False) and de.name.endswith(".json"):
                st = de.stat(follow_symlinks=False)
                if st.st_size > MAX_CHAR_CONFIG_BYTES:
                    continue
                dir_entries.append((de.name, int(st.st_mtime_ns), int(st.st_size)))
                total_size += int(st.st_size)
                if st.st_mtime_ns > max_mtime_ns:
                    max_mtime_ns = int(st.st_mtime_ns)
    sig = [str(chars_dir_res), len(dir_entries), max_mtime_ns, total_size]
    cache_path = get_cache_dir() / "characters_bundle_v1.json"
    if cache_path.is_file() and not cache_path.is_symlink():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("sig") == sig and isinstance(cached.get("chars"), dict):
                cached_chars = cached["chars"]
                if all(
                    is_safe_entity_id(k) and isinstance(v, dict) and is_safe_entity_id(str(v.get("id") or ""))
                    for k, v in cached_chars.items()
                ):
                    return cached_chars
        except Exception:
            pass

    chars: dict[str, dict] = {}
    items = []
    for fname, _, _ in dir_entries:
        try:
            fpath = (CHARACTERS_DIR / fname).resolve()
            if not fpath.is_relative_to(chars_dir_res):
                continue
            d = json.loads(fpath.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                continue
            cid = str(d.get("id") or "").strip()
            if not is_safe_entity_id(cid):
                continue
            d["id"] = cid
            alts = d.get("alt_voice_personas")
            if isinstance(alts, dict):
                safe_alts = {}
                for ak, spec in alts.items():
                    if not isinstance(spec, dict) or not is_safe_entity_id(str(ak)):
                        continue
                    vsid = str(spec.get("voice_slot_id") or f"{cid}_{str(ak).capitalize()}").strip()
                    if is_safe_entity_id(vsid):
                        safe_alts[ak] = spec
                d["alt_voice_personas"] = safe_alts
            items.append(d)
        except Exception:
            continue
    items.sort(key=lambda x: (0 if x.get("category", "playable") == "playable" else 1, x.get("school", ""), x.get("name_kr", x["id"])))
    for d in items:
        chars[d["id"]] = d
    try:
        if not cache_path.is_symlink():
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".chars_", suffix=".tmp", dir=str(cache_path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"sig": sig, "chars": chars}, ensure_ascii=False))
                os.replace(tmp_name, cache_path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    except Exception:
        pass
    return chars


def load_momotalk_profiles(characters: dict[str, dict]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load official MomoTalk name/costume/status-message entries mapped onto base character personas."""
    entries: list[dict[str, Any]] = []
    entry_map: dict[str, dict[str, Any]] = {}
    covered_bases: set[str] = set()

    if MOMOTALK_PROFILES_PATH.is_file() and not MOMOTALK_PROFILES_PATH.is_symlink():
        try:
            raw = json.loads(MOMOTALK_PROFILES_PATH.read_text(encoding="utf-8"))
            for item in raw.get("entries") or []:
                if not isinstance(item, dict):
                    continue
                eid = str(item.get("entry_id") or "").strip()
                bcid = str(item.get("base_cid") or eid).strip()
                if not is_safe_entity_id(eid) or not is_safe_entity_id(bcid) or bcid not in characters:
                    continue
                ent = dict(item)
                ent["entry_id"] = eid
                ent["base_cid"] = bcid
                raw_ptoks = ent.get("portrait_tokens")
                if isinstance(raw_ptoks, list):
                    ent["portrait_tokens"] = [
                        str(tok).strip()
                        for tok in raw_ptoks
                        if str(tok or "").strip()
                        and "/" not in str(tok)
                        and "\\" not in str(tok)
                        and ".." not in str(tok)
                        and "\x00" not in str(tok)
                    ]
                ent["name_kr"] = str(ent.get("name_kr") or characters[bcid].get("name_kr") or eid).strip()
                ent["status_message_kr"] = str(ent.get("status_message_kr") or "").strip()
                entries.append(ent)
                entry_map[eid] = ent
                covered_bases.add(bcid)
        except Exception:
            entries.clear()
            entry_map.clear()
            covered_bases.clear()

    # Auto-include any custom/drop-in character JSONs not present in momotalk_profiles.json
    for cid, ch in characters.items():
        if cid in covered_bases or not is_safe_entity_id(cid):
            continue
        full_nm = str(ch.get("name_kr") or cid).strip()
        short_nm = full_nm.split()[-1] if (" " in full_nm and ch.get("category", "playable") == "playable") else full_nm
        ent = {
            "entry_id": cid,
            "base_cid": cid,
            "ba_character_id": ch.get("character_id"),
            "is_costume_variant": False,
            "costume_label_kr": "",
            "name_kr": short_nm,
            "full_name_kr": full_nm,
            "status_message_kr": "",
            "status_message_jp": "",
            "portrait_tokens": [cid],
            "default_order": 9000,
        }
        entries.append(ent)
        entry_map[cid] = ent

    return entries, entry_map


def extract_model_id(raw: str) -> str:
    """
    Strictly extract a 32-char hexadecimal Fish Audio model ID from either a raw ID or
    an official `https://fish.audio/.../m/<32-hex>` URL. Rejects path traversal / SSRF inputs with `""`.
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


def list_audio_devices() -> tuple[list[tuple[int, str, int]], list[tuple[int, str, int]]]:
    """Return (inputs, outputs) as (device_index, label, default_samplerate) preferring DirectSound/WASAPI."""
    devs = sd.query_devices()
    hostapis = sd.query_hostapis()
    inputs = []
    outputs = []
    for idx, d in enumerate(devs):
        api_name = hostapis[d["hostapi"]]["name"]
        if "WDM-KS" in api_name:
            continue
        sr = int(round(d.get("default_samplerate") or 44100))
        label = f"[{idx}] {d['name']} ({api_name})"
        if d["max_input_channels"] > 0:
            inputs.append((idx, label, sr))
        if d["max_output_channels"] > 0:
            outputs.append((idx, label, sr))
    return inputs, outputs


def pick_default_vbcable_input(inputs: list[tuple[int, str, int]]) -> int | None:
    scored = []
    for idx, label, _ in inputs:
        low = label.lower()
        if "cable output" in low or "vb-audio" in low:
            # Prefer MME (continuous 44.1kHz VB-Cable stream clock) over WASAPI
            api_bonus = 3 if "mme" in low else (2 if "directsound" in low else 1)
            scored.append((api_bonus, idx))
    scored.sort(reverse=True)
    return scored[0][1] if scored else (inputs[0][0] if inputs else None)


def pick_default_real_output(outputs: list[tuple[int, str, int]]) -> int | None:
    """
    Pick real physical playback device for character voice (Realtek/wired earphones first),
    explicitly ignoring VB-Cable, Windows Sound Mapper, and speakerless `25G64`.
    """
    def _collect(skip_25g64: bool) -> list[tuple[int, int, int, int]]:
        scored = []
        for idx, label, _ in outputs:
            low = label.lower()
            if any(k in low for k in ("cable", "vb-audio", "sound mapper", "주 사운드")):
                continue
            if skip_25g64 and "25g64" in low:
                continue
            if any(k in low for k in ("realtek", "headphone", "headset", "이어폰", "헤드폰", "헤드셋")):
                dev_tier = 3
            elif not any(k in low for k in ("anmite", "nvidia", "hdmi", "display")):
                dev_tier = 2
            elif "anmite" not in low:
                dev_tier = 1
            else:
                dev_tier = 0
            # Prefer WASAPI (background-safe, native 48kHz) -> MME -> DirectSound
            api_bonus = 3 if "wasapi" in low else (2 if "mme" in low else 1)
            scored.append((dev_tier, api_bonus, -idx, idx))
        return scored

    scored = _collect(True)
    if not scored:
        scored = _collect(False)
    scored.sort(reverse=True)
    return scored[0][3] if scored else (outputs[0][0] if outputs else None)


class _CombinedCancelEvent:
    """Thread-safe cancellation view combining session shutdown and per-turn barge-in."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(ev.is_set() for ev in self._events)


def _attach_hover_tooltip(widget: tk.Misc, text: str | Callable[[], str]) -> None:
    """Attach a lightweight, non-blocking hover tooltip to any Tkinter widget."""
    tip_state: dict[str, Any] = {"win": None, "after_id": None}

    def _hide(_event: Any = None) -> None:
        aid = tip_state.get("after_id")
        if aid is not None:
            tip_state["after_id"] = None
            try:
                widget.after_cancel(aid)
            except Exception:
                pass
        win = tip_state.get("win")
        if win is not None:
            tip_state["win"] = None
            try:
                win.destroy()
            except Exception:
                pass

    def _show_now() -> None:
        tip_state["after_id"] = None
        msg = text() if callable(text) else str(text or "")
        if not msg.strip():
            return
        try:
            if not widget.winfo_exists():
                return
            x = widget.winfo_rootx() + 10
            y = widget.winfo_rooty() + widget.winfo_height() + 6
            win = tk.Toplevel(widget)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{x}+{y}")
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            tk.Label(
                win,
                text=msg,
                bg="#1E293B",
                fg="#F8FAFC",
                font=("Malgun Gothic", 8),
                padx=8,
                pady=4,
                justify="left",
            ).pack()
            tip_state["win"] = win
        except Exception:
            pass

    def _on_enter(_event: Any = None) -> None:
        _hide()
        try:
            tip_state["after_id"] = widget.after(380, _show_now)
        except Exception:
            pass

    widget.bind("<Enter>", _on_enter, add="+")
    widget.bind("<Leave>", _hide, add="+")
    widget.bind("<ButtonPress>", _hide, add="+")
    widget.bind("<Destroy>", _hide, add="+")


class AruFishBridgeApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MomoTalk")
        self._app_icon_img: Any = None
        ico_file = ROOT_DIR / "config" / "aru_bridge_icon.ico"
        png_icon_file = ROOT_DIR / "config" / "aru_bridge_icon_256.png"
        if ico_file.exists():
            try:
                self.root.iconbitmap(default=str(ico_file))
            except Exception:
                pass
        if png_icon_file.exists():
            try:
                self._app_icon_img = tk.PhotoImage(file=str(png_icon_file))
                self.root.iconphoto(True, self._app_icon_img)
            except Exception:
                pass
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        win_w = min(1160, max(1000, sw - 50))
        win_h = min(sh - 48, max(760, sh - 80)) if sh >= 780 else max(660, sh - 36)
        pos_x = max(15, (sw - win_w) // 2)
        pos_y = max(10, (sh - win_h) // 2 - 10)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(940, min(680, max(620, sh - 60)))
        self.root.configure(bg="#F3F5F8")
        self.root.bind_class("Entry", "<Control-KeyPress>", self._fix_korean_ime_shortcuts)

        self.cfg = load_bridge_config()
        self.characters: dict[str, dict] = load_all_character_configs()
        self.momotalk_entries, self.momotalk_entry_map = load_momotalk_profiles(self.characters)
        init_eid = str(self.cfg.get("selected_momotalk_entry_id") or "").strip()
        if init_eid in self.momotalk_entry_map:
            init_ent = self.momotalk_entry_map[init_eid]
            init_cid = str(init_ent["base_cid"])
            if init_ent.get("is_costume_variant") and init_cid in self.momotalk_entry_map:
                init_eid = init_cid
        else:
            init_cid = str(self.cfg.get("selected_character_id", "Aru") or "Aru")
            if init_cid not in self.characters:
                init_cid = "Aru" if "Aru" in self.characters else next(iter(self.characters), "Aru")
            init_eid = init_cid if init_cid in self.momotalk_entry_map else (self.momotalk_entries[0]["entry_id"] if self.momotalk_entries else init_cid)
        self.current_char_id = init_cid
        self.current_momotalk_entry_id = init_eid
        self.memory_mgr = MemoryManager()
        self._recent_called_entry_ids: list[str] = self.memory_mgr.get_recent_entry_ids()
        self._active_memory_session_lock = threading.Lock()
        self._active_memory_session: dict[str, Any] | None = None
        self._active_call_memory_context: str = ""
        self._memory_summary_queue: queue.Queue[tuple[str, str, str, bool]] = queue.Queue(maxsize=48)
        self._memory_summary_inflight: set[tuple[str, str, str]] = set()
        self._memory_summary_inflight_lock = threading.Lock()
        self._memory_summary_worker_started = False
        self._memory_recovery_scheduled = False
        self._idle_force_profile_card = False
        self._history_img_refs: list[tk.PhotoImage] = []
        self.temporary_call_var = tk.BooleanVar(value=bool(self.cfg.get("temporary_call_mode", False)))

        self.running = False
        self.companion_running = False
        self.stop_playback_flag = threading.Event()
        self.playback_gen_id = 0
        self.whisper_model: Any = None
        self.whisper_loading = False
        self.tokenizers: dict[str, Any] = {}
        self.hint_token_cache: dict[str, list[int]] = {}

        # Lazy-initialized Keep-Alive HTTP pool (avoids synchronous Windows SSL cert store load before first paint)
        self._http_client: httpx.Client | None = None
        self._http_client_lock = threading.Lock()
        self.last_http_activity = 0.0

        self.utterance_queue: queue.Queue[tuple[np.ndarray, int, float, float, bool]] = queue.Queue()
        self.tts_queue: queue.Queue[tuple[int, str, float, float, bool]] = queue.Queue()
        self.ordered_clause_queue: queue.Queue[tuple[int, int, queue.Queue[Any]]] = queue.Queue()
        self.pcm_play_queue: queue.Queue[tuple[int, np.ndarray]] = queue.Queue(maxsize=500)
        self.tts_executor: ThreadPoolExecutor | None = None

        self.trigger_play_queue: queue.Queue[str] = queue.Queue(maxsize=8)
        self.trigger_wav_cache: dict[str, tuple[np.ndarray, int]] = {}
        self.trigger_cache_lock = threading.Lock()

        self._sticky_ja_hits = 0
        self._active_capture_speaking = False
        self._active_stt_busy = False
        self._active_tts_inflight = 0
        self._active_tts_lock = threading.Lock()
        self._active_audio_playing_until = 0.0
        self._post_playback_guard_until = 0.0
        self._pa_lock = threading.Lock()
        self._active_pa_streams = 0
        self.cmb_in: Any = None
        self.cmb_mic: Any = None
        self.cmb_out: Any = None
        self.cmb_out_m1: Any = None
        self.cmb_trig: Any = None
        self.primary_outputs: list[str] = []
        self.trigger_outputs: list[str] = []
        self.mic_inputs: list[str] = []

        # Mode 1: Distributable PC Mic Companion + Mode C Gemini Session + 5-State Machine
        self.companion_sm = CompanionStateMachine(
            proactive_enabled=bool(self.cfg.get("companion_proactive_enable", True)),
            min_idle_sec=float(self.cfg.get("idle_ping_min_sec", 45.0)),
            max_idle_sec=float(self.cfg.get("idle_ping_max_sec", 75.0)),
            max_consecutive_pings=int(self.cfg.get("afk_max_consecutive_pings", 2)),
        )
        self.gemini_mgr = GeminiSessionManager(
            app_root=ROOT_DIR,
            http_client=None,
            log_cb=self.log,
        )
        self.mic_stt = LocalMicSttEngine(
            get_whisper_model=lambda: self.whisper_model,
            get_tokenizers=lambda: self.tokenizers,
            is_listening_allowed=self._is_companion_mic_allowed,
            on_speech_onset=self._on_companion_mic_onset,
            on_transcribed=self._on_companion_mic_transcribed,
            on_empty=self._on_companion_mic_empty,
            on_meter=self._on_companion_mic_meter,
            on_error=self._on_companion_mic_error,
            log_cb=self.log,
            is_playback_active=lambda: time.monotonic() < (self._active_audio_playing_until + 0.20),
            on_barge_in_duck=self._set_barge_in_duck,
        )
        self._companion_turn_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=8)
        self._companion_clause_seq = 0
        self._companion_cancel_event = threading.Event()
        self._turn_interrupt_lock = threading.Lock()
        self._active_turn_interrupt = threading.Event()
        self._barge_in_ducking = False
        self._recent_tts_jp_lines: deque[str] = deque(maxlen=12)
        self._companion_ui_lock = threading.Lock()
        self._companion_session_ready = False
        self._companion_mic_error = ""

        self.watchdog = ProactiveWatchdog(
            enabled=bool(self.cfg.get("proactive_enable", True)),
            silence_timeout_sec=float(self.cfg.get("proactive_timeout_sec", 18.0)),
            max_nudges=2,
            reply_wait_timeout_sec=14.0,
        )

        self.inputs: list[tuple[int, str, int]] = []
        self.outputs: list[tuple[int, str, int]] = []
        self.in_map: dict[str, tuple[int, int]] = {}
        self.out_map: dict[str, tuple[int, int]] = {}

        self.school_filter_var = tk.StringVar(value="전체 학원")
        self.search_var = tk.StringVar(value="")
        self.char_display_var = tk.StringVar(value="")
        self.display_to_cid: dict[str, str] = {}
        saved_pvar = str(self.cfg.get("persona_prompt_variant") or "official")
        self.persona_variant_var = tk.StringVar(
            value=PERSONA_VARIANT_LABELS.get(saved_pvar, PERSONA_VARIANT_LABELS["official"])
        )

        init_rlang = "ko" if str(self.cfg.get("reply_language", "ja") or "ja").strip().lower() == "ko" else "ja"
        self.reply_language_var = tk.StringVar(value=init_rlang)
        self._kr_fallback_warned: set[str] = set()
        self.api_key_var = tk.StringVar(value=self.cfg.get("fish_api_key", ""))
        init_voice_map = (self.cfg.get("saved_voices_kr") if init_rlang == "ko" else self.cfg.get("saved_voices")) or {}
        saved_char_voice = init_voice_map.get(init_cid, "" if init_rlang == "ko" else self.cfg.get("fish_reference_id", ""))
        self.ref_id_var = tk.StringVar(value=saved_char_voice)
        self.model_hdr_var = tk.StringVar(value=self.cfg.get("fish_model_header", "s2.1-pro-free"))
        saved_lang = self.cfg.get("stt_language", "auto")
        self.stt_lang_var = tk.StringVar(value=STT_LANG_LABELS.get(saved_lang, STT_LANG_LABELS["auto"]))
        saved_speed = self.cfg.get("streaming_speed_mode", "expressive")
        self.speed_mode_var = tk.StringVar(value=SPEED_MODE_LABELS.get(saved_speed, SPEED_MODE_LABELS["expressive"]))
        self.auto_emotion_var = tk.BooleanVar(value=bool(self.cfg.get("auto_emotion_directing", True)))
        saved_boost = float(self.cfg.get("expression_boost_offset", 0.0))
        closest_boost = min(BOOST_PRESET_LABELS.keys(), key=lambda k: abs(k - saved_boost))
        self.boost_preset_var = tk.StringVar(value=BOOST_PRESET_LABELS[closest_boost])
        self.pause_sec_var = tk.DoubleVar(value=float(self.cfg.get("pause_threshold_sec", 0.33)))
        self.vol_var = tk.DoubleVar(value=float(self.cfg.get("output_volume", 1.0)))

        self.proactive_prompt_var = tk.BooleanVar(value=bool(self.cfg.get("proactive_prompt_append", True)))
        self.jp_beginner_coach_var = tk.BooleanVar(value=bool(self.cfg.get("jp_beginner_lover_mode", False)))
        self.proactive_enable_var = tk.BooleanVar(value=bool(self.cfg.get("proactive_enable", True)))
        saved_ptime = float(self.cfg.get("proactive_timeout_sec", 18.0))
        closest_ptime = min(PROACTIVE_TIMEOUT_LABELS.keys(), key=lambda k: abs(k - saved_ptime))
        self.proactive_timeout_var = tk.StringVar(value=PROACTIVE_TIMEOUT_LABELS[closest_ptime])
        saved_pvol = float(self.cfg.get("proactive_trigger_volume", 1.0))
        closest_pvol = min(PROACTIVE_VOL_LABELS.keys(), key=lambda k: abs(k - saved_pvol))
        self.proactive_vol_var = tk.StringVar(value=PROACTIVE_VOL_LABELS[closest_pvol])

        self.in_choice_var = tk.StringVar()
        self.mic_choice_var = tk.StringVar()
        self.out_choice_var = tk.StringVar()
        self.trigger_out_choice_var = tk.StringVar()

        self.companion_proactive_var = tk.BooleanVar(value=bool(self.cfg.get("companion_proactive_enable", True)))
        self.companion_state_var = tk.StringVar(value="⚪ 대기 중 (시작 버튼을 눌러주세요)")
        self.companion_meter_var = tk.StringVar(value="░░░░░░░░░░░░ (마이크 대기 중)")
        self.companion_backend_badge_var = tk.StringVar()
        self.companion_range_var = tk.StringVar()
        self._browser_toggle_var = tk.StringVar(value="🌐 제미니 화면 보기")

        self._log_history: deque[str] = deque(maxlen=800)
        self._log_lock = threading.Lock()
        self._status_log_win: tk.Toplevel | None = None
        self._status_log_box: scrolledtext.ScrolledText | None = None

        saved_sub_mode = str(self.cfg.get("subtitle_mode", "jp_kr") or "jp_kr")
        if saved_sub_mode not in SUBTITLE_MODES:
            saved_sub_mode = "jp_kr"
        self.subtitle_mode_var = tk.StringVar(value=saved_sub_mode)

        b_model, b_think = split_legacy_browser_model(
            str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"),
            str(self.cfg.get("gemini_thinking_level", "low") or "low"),
        )
        self.cfg["gemini_browser_model"] = b_model
        self.cfg["gemini_thinking_level"] = b_think

        raw_sub_engine = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
        raw_sub_think = str(self.cfg.get("subtitle_thinking_level", "low") or "low")
        if raw_sub_think not in GEMINI_THINKING_LEVEL_LABELS:
            raw_sub_think = "low"
        if raw_sub_engine == "auto_llm":
            raw_sub_engine = "auto_inline"
        elif raw_sub_engine == "browser_hydrogen_low":
            raw_sub_engine = "web:hydrogen"
            raw_sub_think = "low"
        elif raw_sub_engine == "browser_hydrogen_high":
            raw_sub_engine = "web:hydrogen"
            raw_sub_think = "high"
        elif raw_sub_engine == "browser_flash_lite":
            raw_sub_engine = "web:auto"
        elif raw_sub_engine == "web:flash_lite":
            raw_sub_engine = "web:auto"
        elif raw_sub_engine in ("web:pro_31", "web:argon_4"):
            raw_sub_engine = "web:neon_4"

        self._extra_api_models: list[str] = [str(m).strip() for m in (self.cfg.get("api_models", []) or []) if str(m).strip()]
        self._extra_web_models: list[tuple[str, str]] = []
        raw_cfg_web = self.cfg.get("web_models") or []
        self._account_web_models: list[tuple[str, str]] = [
            (str(it[0]).strip(), str(it[1]).strip())
            for it in raw_cfg_web
            if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[0]).strip() and str(it[1]).strip()
        ]
        self._web_account_tier: str = str(self.cfg.get("web_account_tier", "") or "").strip()
        self._setup_panels_collapsed = False
        self.btn_toggle_setup: Any = None
        self.f0: Any = None
        self.f1: Any = None
        self.f2: Any = None
        self.f3: Any = None
        self._sub_api_no_key_warned: bool = False

        if raw_sub_engine != "fast_nmt" and not (
            raw_sub_engine == "auto_inline" or raw_sub_engine.startswith("web:") or raw_sub_engine.startswith("api:")
        ):
            raw_sub_engine = "auto_inline"
        self.cfg["subtitle_engine"] = raw_sub_engine
        self.cfg["subtitle_thinking_level"] = raw_sub_think

        sub_choices = self._subtitle_choices()
        self.subtitle_engine_var = tk.StringVar(value=resolve_subtitle_engine_label(raw_sub_engine, sub_choices))
        self.subtitle_thinking_var = tk.StringVar(value=GEMINI_THINKING_LEVEL_LABELS[raw_sub_think])
        self.companion_model_var = tk.StringVar(value="")
        self.companion_thinking_var = tk.StringVar(value=GEMINI_THINKING_LEVEL_LABELS[b_think])
        self._subtitle_lock = threading.Lock()
        self._subtitle_entries: list[SubtitleEntry] = []
        self._subtitle_seq = 0
        self._subtitle_mode_btns: dict[str, tk.Radiobutton] = {}
        self.gemini_mgr.browser.selected_model_code = b_model
        self.gemini_mgr.browser.selected_thinking_level = b_think
        self.subtitle_translator = RealtimeSubtitleTranslator(
            characters=self.characters,
            llm_translator=self._translate_subtitle_with_llm,
        )

        self._init_device_choices()
        self._ui_boot_complete = False
        self._last_active_persona_key: tuple[str, bool] | None = None

        self._closing: bool = False
        self._ui_queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._ui_poll_after_id: str | None = None

        # MomoTalk UI & Phone-Call Simulation State
        _steam_hint = str(self.cfg.get("steam_game_dir") or "").strip() or None
        self.momotalk_assets = MomoTalkAssetManager(ba_root_hint=_steam_hint)
        self._voice_gen_lock = threading.Lock()
        self._voice_gen_inflight: dict[tuple[str, str], tuple[str, str]] = {}  # (char_id, lang) -> (stage, msg)
        self._momotalk_photo_cache: dict[tuple[Any, ...], tk.PhotoImage] = {}
        self._momotalk_fallback_photo_cache: dict[tuple[Any, ...], tk.PhotoImage] = {}
        self._placeholder_photos: dict[tuple[int, str], tk.PhotoImage] = {}
        self._bg_asset_deque: deque[tuple[str, str, int, str]] = deque()
        self._bg_asset_pending: set[tuple[str, str, int, str]] = set()
        self._bg_asset_inflight: tuple[str, str, int, str] | None = None
        self._bg_asset_cond = threading.Condition()
        self._bg_asset_refresh_scheduled: bool = False
        self._rendering_roster: bool = False
        self._last_scrollregion: tuple[int, int] = (0, 0)
        self._last_yview_top: float = -1.0
        self._roster_active_rows: dict[int, dict[str, Any]] = {}
        self._roster_free_pool: list[dict[str, Any]] = []
        self._roster_empty_text_id: int | None = None
        self._roster_last_w: int = 0
        self._roster_last_sel_eid: str = ""
        self._roster_last_sel_active: bool = False
        self._subtitle_img_refs: list[tk.PhotoImage] = []
        self._momotalk_char_activated: bool = False
        self._momotalk_nav_tab: str = "all"
        self._momotalk_sort_mode: str = "default"
        self._momotalk_filtered_cids: list[str] = []
        self._call_ui_state: str = "idle"
        self._call_started_mono: float = 0.0
        self._call_connected_mono: float = 0.0
        self._ringback_state = RingbackStreamState()
        self._call_anim_phase: float = 0.0
        self._call_anim_after_id: str | None = None
        self._call_timer_after_id: str | None = None
        self._last_call_timer_txt: str = ""
        self._calling_canvas_items: dict[str, Any] = {}
        self._mode2_capture_ready: bool = False
        self._calling_login_required: bool = False
        self._call_state_lock = threading.Lock()
        self._startup_after_ids: list[str] = []
        self._stop_keepalive_event = threading.Event()
        self._log_dir_ready: bool = False
        self._zero_shot_wav_cache: dict[str, tuple[int, bytes]] = {}
        self._search_index_sig: Any = None
        self._char_search_cache: list[tuple[str, str, bool, str, str]] = []
        self._momotalk_search_default: list[tuple[dict[str, Any], str, str]] = []
        self._momotalk_search_name: list[tuple[dict[str, Any], str, str]] = []
        self._last_companion_meter: str = ""
        self._last_m2_meter: str = ""
        self._subtitle_has_placeholder: bool = False

        self._build_ui()
        self._refresh_companion_badges()
        self._refresh_character_filter_list(keep_current=True)
        self._last_active_persona_key = self._resolve_active_persona_for_gemini()
        self._ui_boot_complete = True
        self.mic_stt.set_character_context(self.current_char)
        self._sync_mic_stt_config()
        self._ui_poll_after_id = self.root.after(45, self._poll_ui_queue)
        self._start_momotalk_assets_index_async()
        self._bring_to_front()

        # Stagger heavy background warmups so window first paint & initial scroll stay uncontended
        def _defer_startup(delay_ms: int, fn: Any) -> None:
            try:
                aid = self.root.after(delay_ms, lambda f=fn: (not getattr(self, "_closing", False)) and f())
                self._startup_after_ids.append(aid)
            except Exception:
                pass

        _defer_startup(250, self._preload_whisper_async)
        _defer_startup(450, self._start_http_keepalive_daemon)
        _defer_startup(600, self._preload_trigger_wavs_async)
        _defer_startup(750, self._auto_sync_cached_web_models_async)
        _defer_startup(850, self.subtitle_translator.warmup_async)

        self.root.protocol("WM_DELETE_WINDOW", self._on_app_closing)
        atexit.register(self._cleanup_on_exit)
        if not str(self.cfg.get("fish_api_key", "")).strip():
            _defer_startup(300, lambda: self.open_settings_dialog(first_run=True, initial_tab=1))

    @property
    def http_client(self) -> httpx.Client:
        """Lazily create persistent Keep-Alive Fish Audio HTTP pool on first network use."""
        if self._http_client is not None:
            return self._http_client
        with self._http_client_lock:
            if self._http_client is None:
                self._http_client = httpx.Client(
                    verify=True,
                    follow_redirects=False,
                    timeout=httpx.Timeout(25.0, connect=8.0),
                    limits=httpx.Limits(max_keepalive_connections=4, max_connections=8, keepalive_expiry=120.0),
                )
            return self._http_client

    def _bring_to_front(self) -> None:
        """Guarantee the window appears immediately on top of Explorer / IDE on Windows 11."""
        try:
            self.root.update_idletasks()
            self.root.state("normal")
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.focus_force()
            if os.name == "nt":
                frame_hex = self.root.wm_frame()
                hwnd = int(frame_hex, 16) if frame_hex and frame_hex != "0x0" else self.root.winfo_id()
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            aid = self.root.after(
                600,
                lambda: (not getattr(self, "_closing", False)) and self.root.attributes("-topmost", False),
            )
            self._startup_after_ids.append(aid)
        except Exception:
            pass

    @property
    def current_char(self) -> dict:
        return self.characters.get(self.current_char_id) or {
            "id": "Aru",
            "name_kr": "리쿠하치마 아루",
            "name_jp": "陸八魔アル",
            "school": "게헨나 학원",
            "club": "흥신소 68 (사장)",
            "user_title_jp": "先生",
            "persona": {},
        }

    def _get_reply_language(self) -> str:
        val = str(self.reply_language_var.get() if hasattr(self, "reply_language_var") else self.cfg.get("reply_language", "ja") or "ja").strip().lower()
        return "ko" if val == "ko" else "ja"

    def _voice_lang_code(self) -> str:
        return "KR" if self._get_reply_language() == "ko" else "JP"

    def _get_saved_voice_id(
        self,
        char_id: str | None = None,
        reply_lang: str | None = None,
        allow_jp_fallback: bool = False,
    ) -> str:
        cid = char_id or self.current_char_id
        rlang = reply_lang or self._get_reply_language()
        if rlang == "ko":
            kr_id = extract_model_id(str((self.cfg.get("saved_voices_kr") or {}).get(cid) or ""))
            if kr_id:
                return kr_id
            if allow_jp_fallback:
                return extract_model_id(str((self.cfg.get("saved_voices") or {}).get(cid) or ""))
            return ""
        return extract_model_id(str((self.cfg.get("saved_voices") or {}).get(cid) or ""))

    def _set_saved_voice_id(self, char_id: str, model_id: str, reply_lang: str | None = None) -> None:
        clean_id = extract_model_id(model_id)
        if not char_id or not clean_id:
            return
        rlang = reply_lang or self._get_reply_language()
        bucket = "saved_voices_kr" if rlang == "ko" else "saved_voices"
        self.cfg.setdefault(bucket, {})[char_id] = clean_id

    def _get_student_voice_link_flags(self, entry_or_cid: str) -> tuple[bool, bool]:
        """Return `(has_jp_voice, has_kr_voice)` Fish Audio link flags for a student's base character ID."""
        ent = self.momotalk_entry_map.get(entry_or_cid)
        bcid = str((ent or {}).get("base_cid") or entry_or_cid or "").strip()
        if not bcid:
            return False, False
        jp_id = self._get_saved_voice_id(bcid, reply_lang="ja", allow_jp_fallback=False)
        kr_id = self._get_saved_voice_id(bcid, reply_lang="ko", allow_jp_fallback=False)
        return bool(jp_id), bool(kr_id)

    def _get_resolved_ba_game_dir(self) -> Path:
        return resolve_ba_game_dir(cfg=self.cfg)

    def _apply_steam_game_dir(self, new_dir: str | Path) -> bool:
        """Persist a validated Steam Blue Archive directory and refresh MomoTalk assets/caches."""
        cand = str(new_dir or "").strip()
        if cand and not is_valid_ba_game_dir(cand):
            return False
        norm_val = str(Path(cand).parent if cand and Path(cand).name.lower() == "bluearchive_data" else cand)
        self.cfg["steam_game_dir"] = norm_val
        save_bridge_config(self.cfg)
        clear_steam_voice_availability_cache()
        if hasattr(self, "momotalk_assets") and self.momotalk_assets is not None:
            self.momotalk_assets.set_ba_root_hint(norm_val or None)
            self._start_momotalk_assets_index_async()
        self._refresh_momotalk_header_and_cards()
        return True

    def _has_active_language_voice_link(self, entry_or_cid: str) -> bool:
        """Return True when the student has a Fish Audio voice link for the CURRENT active language (JP or KR)."""
        has_jp, has_kr = self._get_student_voice_link_flags(entry_or_cid)
        return has_kr if self._get_reply_language() == "ko" else has_jp

    def _get_active_voice_gen_state(self, char_id: str, lang_up: str) -> tuple[str, str] | None:
        with self._voice_gen_lock:
            hit = self._voice_gen_inflight.get((char_id, lang_up))
            if hit is not None:
                return hit
            other = "JP" if lang_up == "KR" else "KR"
            return self._voice_gen_inflight.get((char_id, other))

    def _has_student_voice_link(self, entry_or_cid: str) -> bool:
        """Return True when either a Japanese or Korean Fish Audio voice link is configured for the student."""
        has_jp, has_kr = self._get_student_voice_link_flags(entry_or_cid)
        return has_jp or has_kr

    def _format_voice_link_tooltip(self, entry_or_cid: str) -> str:
        has_jp, has_kr = self._get_student_voice_link_flags(entry_or_cid)
        langs: list[str] = []
        if has_jp:
            langs.append("🇯🇵 일본어")
        if has_kr:
            langs.append("🇰🇷 한국어")
        if not langs:
            return ""
        extra_alt = ""
        ent = self.momotalk_entry_map.get(entry_or_cid)
        bcid = str((ent or {}).get("base_cid") or entry_or_cid or "").strip()
        ch = self.characters.get(bcid) or {}
        alts = ch.get("alt_voice_personas") if isinstance(ch, dict) else None
        if isinstance(alts, dict):
            alt_notes: list[str] = []
            for ak, spec in alts.items():
                if not isinstance(spec, dict):
                    continue
                slot_id = str(spec.get("voice_slot_id") or f"{bcid}_{ak.capitalize()}").strip()
                ajp = bool(self._get_saved_voice_id(slot_id, reply_lang="ja", allow_jp_fallback=False))
                akr = bool(self._get_saved_voice_id(slot_id, reply_lang="ko", allow_jp_fallback=False))
                if ajp or akr:
                    alangs = "/".join(x for x, ok in (("JP", ajp), ("KR", akr)) if ok)
                    alt_notes.append(f"🎭 {ak.upper()}({alangs})")
            if alt_notes:
                extra_alt = f" · {' '.join(alt_notes)}"
        return f"💗 Fish Audio 목소리 연결됨 ({' · '.join(langs)}{extra_alt})"

    def _get_momotalk_heart_photo(self, size: int = 22, enqueue: bool = True) -> tuple[tk.PhotoImage | None, bool]:
        """Resolve Blue Archive MomoTalk right-edge pink heart icon (Steam asset or crisp procedural fallback)."""
        return self._resolve_momotalk_photo("ui", "heart", size=size, bg_hex="#FFFFFF", ring_hex="#FB7390", enqueue=enqueue)

    def _char_ref_paths(self, char_id: str | None = None, lang: str | None = None) -> tuple[Path, Path]:
        cid = str(char_id or self.current_char_id or "").strip()
        if not is_safe_entity_id(cid):
            raise ValueError(f"Invalid or unsafe character_id: {cid!r}")
        lang_up = str(lang or self._voice_lang_code()).strip().upper()
        if lang_up not in ("JP", "KR"):
            raise ValueError(f"Invalid voice language code: {lang_up!r}")
        user_models_root = get_user_models_dir().resolve()
        user_dir = (user_models_root / cid / lang_up).resolve()
        if not user_dir.is_relative_to(user_models_root):
            raise ValueError(f"Path traversal blocked for character_id={cid!r}")
        user_wav = user_dir / f"{cid}_{lang_up}_FishAudio_Reference.wav"
        legacy_models_root = (ROOT_DIR / "models").resolve()
        legacy_dir = (legacy_models_root / cid / lang_up).resolve()
        if legacy_dir.is_relative_to(legacy_models_root):
            legacy_wav = legacy_dir / f"{cid}_{lang_up}_FishAudio_Reference.wav"
            if legacy_wav.exists() and not legacy_wav.is_symlink() and not user_wav.exists():
                return legacy_wav, legacy_dir / "fish_samples"
        return user_wav, user_dir / "fish_samples"

    def _is_audio_pipeline_active(self) -> bool:
        return bool(self.running or self.companion_running)

    @staticmethod
    def _fix_korean_ime_shortcuts(event: tk.Event):
        if event.keysym.lower() in ("v", "c", "x", "a"):
            return None
        w = event.widget
        if event.keycode == 86:
            w.event_generate("<<Paste>>")
            return "break"
        if event.keycode == 67:
            w.event_generate("<<Copy>>")
            return "break"
        if event.keycode == 88:
            w.event_generate("<<Cut>>")
            return "break"
        if event.keycode == 65:
            w.selection_range(0, tk.END)
            return "break"
        return None

    def _inc_pa_stream(self) -> None:
        with self._pa_lock:
            self._active_pa_streams += 1

    def _dec_pa_stream(self) -> None:
        with self._pa_lock:
            self._active_pa_streams = max(0, self._active_pa_streams - 1)

    def _close_pa_stream(self, stream: sd.OutputStream | sd.InputStream | None) -> None:
        if stream is None:
            return
        try:
            try:
                stream.stop()
            finally:
                stream.close()
        except Exception:
            pass
        finally:
            self._dec_pa_stream()

    def refresh_audio_devices(self, reinit_portaudio: bool = True, log_info: bool = False) -> None:
        """Re-enumerate audio endpoints safely (recovering from USB/HDMI hotplug index shifts)."""
        if reinit_portaudio and not self._is_audio_pipeline_active():
            for _ in range(18):
                with self._pa_lock:
                    if self._active_pa_streams == 0:
                        break
                time.sleep(0.025)
            with self._pa_lock:
                if not self._is_audio_pipeline_active() and self._active_pa_streams == 0:
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        pass

        self.inputs, self.outputs = list_audio_devices()
        self.in_map = {lbl: (idx, sr) for idx, lbl, sr in self.inputs}
        self.out_map = {lbl: (idx, sr) for idx, lbl, sr in self.outputs}
        self.primary_outputs = list_primary_output_labels(self.outputs)
        self.trigger_outputs = list_trigger_output_labels(self.outputs)
        self.mic_inputs = list_microphone_labels(self.inputs)

        in_labels = list(self.in_map.keys())
        if self.cmb_in is not None:
            self.cmb_in["values"] = in_labels
        if self.cmb_mic is not None:
            self.cmb_mic["values"] = self.mic_inputs
        if self.cmb_out is not None:
            self.cmb_out["values"] = self.primary_outputs
        if self.cmb_out_m1 is not None:
            self.cmb_out_m1["values"] = self.primary_outputs
        if self.cmb_trig is not None:
            self.cmb_trig["values"] = self.trigger_outputs

        # Resolve microphone input (Mode 1)
        prev_mic = self.mic_choice_var.get().strip() or str(self.cfg.get("mic_input_device") or "").strip()
        matched_mic = match_device_label_by_name(prev_mic, self.mic_inputs, require_same_api=False)
        if matched_mic:
            self.mic_choice_var.set(matched_mic)
        elif self.mic_inputs:
            self.mic_choice_var.set(self.mic_inputs[0])
        else:
            self.mic_choice_var.set("(연결된 물리 마이크 없음)")

        # Resolve capture input (name + host API resilient across hotplug)
        prev_in = self.in_choice_var.get().strip()
        matched_in = (
            match_device_label_by_name(prev_in, in_labels, require_same_api=True)
            or match_device_label_by_name(prev_in, in_labels, require_same_api=False)
        )
        if matched_in:
            self.in_choice_var.set(matched_in)
        else:
            def_in = pick_default_vbcable_input(self.inputs)
            chosen_in = next((lbl for idx, lbl, _ in self.inputs if idx == def_in), "")
            if chosen_in:
                self.in_choice_var.set(chosen_in)

        # Resolve primary output (never Sound Mapper or 25G64; fallback to Realtek/headphones)
        prev_out = self.out_choice_var.get().strip() or str(self.cfg.get("primary_output_device") or "").strip()
        matched_out = match_device_label_by_name(prev_out, self.primary_outputs, require_same_api=False)
        if matched_out:
            self.out_choice_var.set(matched_out)
        else:
            def_out = pick_default_real_output(self.outputs)
            chosen_out = next((lbl for idx, lbl, _ in self.outputs if idx == def_out and lbl in self.primary_outputs), "")
            if not chosen_out and self.primary_outputs:
                chosen_out = self.primary_outputs[0]
            if chosen_out:
                self.out_choice_var.set(chosen_out)

        primary_idx = self.out_map.get(self.out_choice_var.get(), (None, 44100))[0]

        # Resolve secondary trigger output (ANMITE WASAPI first)
        prev_trig = self.trigger_out_choice_var.get().strip() or str(self.cfg.get("proactive_trigger_device") or "").strip()
        matched_trig = match_device_label_by_name(prev_trig, self.trigger_outputs, require_same_api=False)
        if matched_trig:
            self.trigger_out_choice_var.set(matched_trig)
        else:
            def_trig = pick_default_trigger_output(self.outputs, primary_out_idx=primary_idx)
            chosen_trig = next(
                (lbl for idx, lbl, _ in self.outputs if idx == def_trig and lbl in self.trigger_outputs),
                "",
            )
            if not chosen_trig and self.trigger_outputs:
                chosen_trig = self.trigger_outputs[0]
            if chosen_trig:
                self.trigger_out_choice_var.set(chosen_trig)

        if log_info:
            self.log(
                f"🔄 [오디오 장치 새로고침] 출력='{self.out_choice_var.get()}' | 트리거='{self.trigger_out_choice_var.get()}'"
            )

    def _on_click_refresh_devices(self) -> None:
        self.refresh_audio_devices(reinit_portaudio=not self._is_audio_pipeline_active(), log_info=True)
        self.save_settings_ui(silent=True)

    def refresh_devices(self) -> None:
        self._on_click_refresh_devices()

    def _init_device_choices(self) -> None:
        self.refresh_audio_devices(reinit_portaudio=False, log_info=False)

    def _start_momotalk_assets_index_async(self) -> None:
        t = threading.Thread(
            target=self._momotalk_asset_worker_loop,
            daemon=True,
            name="momotalk-asset-worker",
        )
        t.start()

    def _resolve_portrait_char_data(self, key: str) -> dict[str, Any]:
        ent = self.momotalk_entry_map.get(key)
        if ent is not None:
            base = dict(self.characters.get(ent["base_cid"]) or {})
            base["id"] = ent["base_cid"]
            base["entry_id"] = ent["entry_id"]
            base["name_kr"] = ent.get("name_kr") or base.get("name_kr") or key
            base["portrait_tokens"] = list(ent.get("portrait_tokens") or [])
            return base
        return dict(self.characters.get(key) or {"id": key, "entry_id": key, "name_kr": key, "school": ""})

    def _momotalk_asset_worker_loop(self) -> None:
        try:
            self.momotalk_assets.ensure_index()
        except Exception:
            pass
        if getattr(self, "_closing", False):
            return
        self._schedule_ui(self._on_momotalk_assets_indexed)

        last_refresh = 0.0
        dirty = False
        while not getattr(self, "_closing", False):
            item: tuple[str, str, int, str] | None = None
            with self._bg_asset_cond:
                while not self._bg_asset_deque and not getattr(self, "_closing", False):
                    if dirty and (time.monotonic() - last_refresh) >= 0.08:
                        dirty = False
                        last_refresh = time.monotonic()
                        self._trigger_bg_assets_refresh()
                    self._bg_asset_cond.wait(timeout=0.14)
                if getattr(self, "_closing", False):
                    break
                if self._bg_asset_deque:
                    item = self._bg_asset_deque.popleft()
                    self._bg_asset_inflight = item
            if item is None:
                continue

            kind, key, size, ring_hex = item
            try:
                if kind == "portrait":
                    ch = self._resolve_portrait_char_data(key)
                    self.momotalk_assets.get_portrait_png(
                        ch,
                        size=size,
                        bg_rgb=None,
                        ring_rgb=self._hex_to_rgb(ring_hex),
                        use_steam_if_unindexed=True,
                        cached_only=False,
                    )
                elif kind == "school":
                    self.momotalk_assets.get_school_icon_png(
                        key,
                        size=size,
                        use_steam_if_unindexed=True,
                        cached_only=False,
                    )
                elif kind == "ui":
                    self.momotalk_assets.get_ui_icon_png(
                        key,
                        size=size,
                        use_steam_if_unindexed=True,
                        cached_only=False,
                    )
                dirty = True
            except Exception:
                pass
            finally:
                with self._bg_asset_cond:
                    self._bg_asset_pending.discard(item)
                    if self._bg_asset_inflight == item:
                        self._bg_asset_inflight = None

            # Yield briefly so Tkinter UI thread never starves on Python's GIL while scrolling
            time.sleep(0.001)
            now = time.monotonic()
            if dirty and (now - last_refresh) >= 0.09:
                dirty = False
                last_refresh = now
                self._trigger_bg_assets_refresh()

    def _trigger_bg_assets_refresh(self) -> None:
        if getattr(self, "_closing", False) or getattr(self, "_bg_asset_refresh_scheduled", False):
            return
        self._bg_asset_refresh_scheduled = True
        self._schedule_ui(self._on_bg_assets_batch_ready)

    def _on_bg_assets_batch_ready(self) -> None:
        self._bg_asset_refresh_scheduled = False
        if getattr(self, "_closing", False):
            return
        self._refresh_visible_roster_portraits()
        if self._momotalk_char_activated:
            self._refresh_momotalk_header_and_cards()
        if self._call_ui_state == "calling":
            self._redraw_calling_canvas()
        if self._subtitle_entries:
            self._rerender_subtitles()

    def _schedule_ui(self, fn: Any) -> None:
        if getattr(self, "_closing", False):
            return
        if threading.current_thread() is threading.main_thread():
            try:
                self.root.after(0, fn)
            except Exception:
                pass
        else:
            self._ui_queue.put(fn)

    def _poll_ui_queue(self) -> None:
        self._ui_poll_after_id = None
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        if not getattr(self, "_closing", False):
            try:
                self._ui_poll_after_id = self.root.after(45, self._poll_ui_queue)
            except Exception:
                self._ui_poll_after_id = None

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
        h = (hex_color or "#FFFFFF").strip().lstrip("#")
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        return (255, 255, 255)

    def _get_placeholder_photo(self, size: int = 50, ring_hex: str = "#E2E8F0") -> tk.PhotoImage | None:
        """Return a single shared transparent-corner silhouette PhotoImage per (size, ring) with zero per-row cost."""
        pkey = (int(size), str(ring_hex).upper())
        cached = self._placeholder_photos.get(pkey)
        if cached is not None:
            return cached
        try:
            from src.extractor.ba_momotalk_assets import render_fallback_avatar_png

            png_bytes = render_fallback_avatar_png(
                name_kr="MomoTalk",
                school_kr="",
                size=int(size),
                bg_rgb=None,
                ring_rgb=self._hex_to_rgb(ring_hex),
            )
            photo = tk.PhotoImage(data=base64.b64encode(png_bytes).decode("ascii"))
            self._placeholder_photos[pkey] = photo
            return photo
        except Exception:
            return None

    def _resolve_momotalk_photo(
        self,
        kind: str,
        key: str,
        size: int = 50,
        bg_hex: str = "#FFFFFF",
        ring_hex: str = "#FB7390",
        enqueue: bool = True,
    ) -> tuple[tk.PhotoImage | None, bool]:
        """Return `(PhotoImage | None, is_final_cached_asset)` without blocking the UI thread on LZ4."""
        skind = str(kind)
        skey = str(key)
        isize = int(size)
        sring = str(ring_hex).upper()

        if skind == "portrait":
            cache_key: tuple[Any, ...] = (skind, skey, isize, sring)
        else:
            cache_key = (skind, skey, isize, str(bg_hex).upper(), sring)

        cached = self._momotalk_photo_cache.get(cache_key)
        if cached is not None:
            return cached, True

        ring_rgb = self._hex_to_rgb(sring)
        ch = self._resolve_portrait_char_data(skey) if skind == "portrait" else {}

        has_disk = False
        if skind == "portrait":
            has_disk = self.momotalk_assets.has_cached_portrait(ch, size=isize, bg_rgb=None, ring_rgb=ring_rgb)
        elif skind == "school":
            has_disk = self.momotalk_assets.has_cached_school_icon(skey, size=isize)
        elif skind == "ui":
            has_disk = self.momotalk_assets.has_cached_ui_icon(skey, size=isize)

        if has_disk:
            png_bytes: bytes | None = None
            if skind == "portrait":
                png_bytes = self.momotalk_assets.get_portrait_png(
                    ch, size=isize, bg_rgb=None, ring_rgb=ring_rgb, cached_only=True
                )
            elif skind == "school":
                png_bytes = self.momotalk_assets.get_school_icon_png(skey, size=isize, cached_only=True)
            elif skind == "ui":
                png_bytes = self.momotalk_assets.get_ui_icon_png(skey, size=isize, cached_only=True)
            if png_bytes:
                try:
                    photo = tk.PhotoImage(data=base64.b64encode(png_bytes).decode("ascii"))
                    self._momotalk_photo_cache[cache_key] = photo
                    return photo, True
                except Exception:
                    return None, False

        # Disk cache miss: prioritize currently visible viewport via bounded LIFO deque and return instant placeholder
        if enqueue and not getattr(self, "_closing", False):
            task_key = (skind, skey, isize, sring)
            with self._bg_asset_cond:
                if task_key != getattr(self, "_bg_asset_inflight", None):
                    if task_key in self._bg_asset_pending:
                        try:
                            self._bg_asset_deque.remove(task_key)
                        except ValueError:
                            pass
                        self._bg_asset_deque.appendleft(task_key)
                    else:
                        self._bg_asset_deque.appendleft(task_key)
                        self._bg_asset_pending.add(task_key)
                        while len(self._bg_asset_deque) > 96:
                            dropped = self._bg_asset_deque.pop()
                            self._bg_asset_pending.discard(dropped)
                self._bg_asset_cond.notify()

        if skind == "portrait":
            return self._get_placeholder_photo(isize, sring), False

        fb = self._momotalk_fallback_photo_cache.get(cache_key)
        if fb is not None:
            return fb, False

        fb_bytes: bytes | None = None
        if skind == "school":
            fb_bytes = self.momotalk_assets.get_school_icon_png(skey, size=isize, cached_only=True)
        elif skind == "ui":
            fb_bytes = self.momotalk_assets.get_ui_icon_png(skey, size=isize, cached_only=True)
        if not fb_bytes:
            return None, False
        try:
            fb_photo = tk.PhotoImage(data=base64.b64encode(fb_bytes).decode("ascii"))
            self._momotalk_fallback_photo_cache[cache_key] = fb_photo
            return fb_photo, False
        except Exception:
            return None, False

    def _get_momotalk_photo(
        self,
        kind: str,
        key: str,
        size: int = 50,
        bg_hex: str = "#FFFFFF",
        ring_hex: str = "#FB7390",
    ) -> tk.PhotoImage | None:
        photo, _ = self._resolve_momotalk_photo(kind, key, size=size, bg_hex=bg_hex, ring_hex=ring_hex, enqueue=True)
        return photo

    def _on_momotalk_assets_indexed(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._refresh_visible_roster_portraits(enqueue_missing=True)
        if self._momotalk_char_activated:
            self._refresh_momotalk_header_and_cards()
        if self._call_ui_state == "calling":
            self._redraw_calling_canvas()
        if self._subtitle_entries:
            self._rerender_subtitles()

    def _build_momotalk_chrome(self) -> None:
        # Off-screen container preserving all legacy/meta controls completely invisible on main screen
        self._legacy_backing_frame = tk.Frame(self.root, bg="#f8fafc")
        self.lbl_header_title = tk.Label(self._legacy_backing_frame, text="MomoTalk")
        self.lbl_gpu_status = tk.Label(self._legacy_backing_frame, text="Whisper GPU 로딩 중...")
        self.btn_hdr_browser = tk.Button(
            self._legacy_backing_frame,
            text="browser",
            command=self.toggle_gemini_browser_window,
        )

        # 1. Top MomoTalk Coral-Pink Header Bar ("MomoTalk" + Gear Icon ONLY)
        hdr = tk.Frame(self.root, bg="#FB7390", padx=16, pady=8)
        hdr.pack(fill="x")
        tk.Frame(self.root, bg="#E45C7A", height=2).pack(fill="x")

        h_left = tk.Frame(hdr, bg="#FB7390")
        h_left.pack(side="left")
        momo_icon = self._get_momotalk_photo("ui", "momotalk", 26, "#FB7390")
        if momo_icon is not None:
            self._hdr_momo_icon_ref = momo_icon
            tk.Label(h_left, image=momo_icon, bg="#FB7390").pack(side="left", padx=(0, 8))
        else:
            tk.Label(h_left, text="💬", bg="#FB7390", fg="#FFFFFF", font=("Segoe UI Emoji", 14, "bold")).pack(
                side="left", padx=(0, 6)
            )
        tk.Label(
            h_left,
            text="MomoTalk",
            font=("Malgun Gothic", 14, "bold"),
            fg="#FFFFFF",
            bg="#FB7390",
        ).pack(side="left")

        self.btn_hdr_settings = tk.Button(
            hdr,
            text="⚙️",
            command=lambda: self.open_settings_dialog(first_run=False, initial_tab=0),
            bg="#FB7390",
            fg="#FFFFFF",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            font=("Segoe UI Emoji", 12, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        )
        self.btn_hdr_settings.pack(side="right")

        self.btn_hdr_jp_coach = tk.Button(
            hdr,
            text="",
            command=self.toggle_jp_beginner_coach_mode,
            relief="flat",
            bd=0,
            padx=11,
            pady=4,
            cursor="hand2",
        )
        self.btn_hdr_jp_coach.pack(side="right", padx=(0, 8))
        self._update_jp_coach_toggle_style()

        self.reply_lang_pill = tk.Frame(hdr, bg="#E45C7A", padx=2, pady=2)
        self.reply_lang_pill.pack(side="right", padx=(0, 8))
        self.btn_hdr_lang_ja = tk.Button(
            self.reply_lang_pill,
            text="🇯🇵 일본어",
            command=lambda: self.set_reply_language("ja"),
            relief="flat",
            bd=0,
            padx=9,
            pady=2,
            cursor="hand2",
        )
        self.btn_hdr_lang_ja.pack(side="left", padx=(0, 2))
        self.btn_hdr_lang_ko = tk.Button(
            self.reply_lang_pill,
            text="🇰🇷 한국어",
            command=lambda: self.set_reply_language("ko"),
            relief="flat",
            bd=0,
            padx=9,
            pady=2,
            cursor="hand2",
        )
        self.btn_hdr_lang_ko.pack(side="left")
        self._update_reply_language_toggle_style()

        self.btn_hdr_login_confirm = tk.Button(
            hdr,
            text="✅ 로그인 확인",
            command=self.confirm_browser_login_async,
            bg="#FEF3C7",
            fg="#92400E",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        )
        # Packed dynamically only if interactive browser login is actively needed during a call

        # 2. Main 3-Column MomoTalk Shell
        main_body = tk.Frame(self.root, bg="#F3F5F8")
        main_body.pack(fill="both", expand=True)

        # Col 1: Left Dark-Slate Navigation Rail (#374254)
        self.nav_rail = tk.Frame(main_body, bg="#374254", width=56)
        self.nav_rail.pack(side="left", fill="y")
        self.nav_rail.pack_propagate(False)

        self._nav_btn_all = tk.Button(
            self.nav_rail,
            text="👤",
            command=lambda: self._set_momotalk_nav_tab("all"),
            bg="#4B586E",
            fg="#FFFFFF",
            activebackground="#4B586E",
            activeforeground="#FFFFFF",
            font=("Segoe UI Emoji", 14, "bold"),
            relief="flat",
            bd=0,
            pady=12,
            cursor="hand2",
        )
        self._nav_btn_all.pack(fill="x", pady=(10, 4))

        self._nav_btn_star = tk.Button(
            self.nav_rail,
            text="💬",
            command=lambda: self._set_momotalk_nav_tab("recent"),
            bg="#374254",
            fg="#CBD5E1",
            activebackground="#4B586E",
            activeforeground="#FFFFFF",
            font=("Segoe UI Emoji", 14, "bold"),
            relief="flat",
            bd=0,
            pady=12,
            cursor="hand2",
        )
        self._nav_btn_star.pack(fill="x", pady=4)
        _attach_hover_tooltip(self._nav_btn_all, "전체 학생 목록")
        _attach_hover_tooltip(self._nav_btn_star, "최근 통화한 학생 (앱 재시작 시 유지)")

        btn_nav_settings = tk.Button(
            self.nav_rail,
            text="⚙️",
            command=lambda: self.open_settings_dialog(first_run=False, initial_tab=0),
            bg="#374254",
            fg="#CBD5E1",
            activebackground="#4B586E",
            activeforeground="#FFFFFF",
            font=("Segoe UI Emoji", 13, "bold"),
            relief="flat",
            bd=0,
            pady=12,
            cursor="hand2",
        )
        btn_nav_settings.pack(side="bottom", fill="x", pady=(0, 8))
        _attach_hover_tooltip(btn_nav_settings, "환경 설정")

        self._nav_btn_memory = tk.Button(
            self.nav_rail,
            text="🧠",
            command=lambda: self.open_memory_note_dialog(initial_tab=0 if self._momotalk_char_activated else 1),
            bg="#374254",
            fg="#CBD5E1",
            activebackground="#4B586E",
            activeforeground="#FFFFFF",
            font=("Segoe UI Emoji", 13, "bold"),
            relief="flat",
            bd=0,
            pady=12,
            cursor="hand2",
        )
        self._nav_btn_memory.pack(side="bottom", fill="x", pady=(0, 2))
        _attach_hover_tooltip(self._nav_btn_memory, "기억 노트 & 공통 선생님 프로필")

        # Col 2: Student Roster Pane (Name + Brief Status Message ONLY)
        roster_pane = tk.Frame(main_body, bg="#FFFFFF", width=325)
        roster_pane.pack(side="left", fill="y")
        roster_pane.pack_propagate(False)
        tk.Frame(main_body, bg="#DCE3EC", width=1).pack(side="left", fill="y")

        r_top = tk.Frame(roster_pane, bg="#FFFFFF", padx=10, pady=10)
        r_top.pack(fill="x")

        r_title_row = tk.Frame(r_top, bg="#FFFFFF")
        r_title_row.pack(fill="x", pady=(0, 8))
        self.lbl_roster_count = tk.Label(
            r_title_row,
            text="학생",
            bg="#FFFFFF",
            fg="#2A3446",
            font=("Malgun Gothic", 10, "bold"),
        )
        self.lbl_roster_count.pack(side="left")
        self.btn_roster_sort = tk.Button(
            r_title_row,
            text="기본순 ⇅",
            command=self._toggle_momotalk_sort,
            bg="#F1F5F9",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        )
        self.btn_roster_sort.pack(side="right")

        s_box = tk.Frame(r_top, bg="#F1F5F9", highlightbackground="#CBD5E1", highlightthickness=1, padx=6, pady=4)
        s_box.pack(fill="x")
        tk.Label(s_box, text="🔍", bg="#F1F5F9", fg="#64748B", font=("Segoe UI Emoji", 9, "bold")).pack(side="left")
        self.ent_momo_search = tk.Entry(
            s_box,
            textvariable=self.search_var,
            bg="#F1F5F9",
            fg="#1E293B",
            relief="flat",
            font=("Malgun Gothic", 9),
        )
        self.ent_momo_search.pack(side="left", fill="x", expand=True, padx=4)
        self.ent_momo_search.bind("<KeyRelease>", lambda _e: self._refresh_character_filter_list(keep_current=False))
        tk.Button(
            s_box,
            text="✕",
            command=self._clear_momotalk_search,
            bg="#F1F5F9",
            fg="#94A3B8",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=3,
            pady=0,
            cursor="hand2",
        ).pack(side="right")

        tk.Frame(roster_pane, bg="#E2E8F0", height=1).pack(fill="x")

        list_wrap = tk.Frame(roster_pane, bg="#FFFFFF")
        list_wrap.pack(fill="both", expand=True)
        self.roster_canvas = tk.Canvas(list_wrap, bg="#FFFFFF", highlightthickness=0, bd=0, yscrollincrement=24)
        self.roster_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self._on_roster_yview)
        self.roster_canvas.configure(yscrollcommand=self._on_roster_canvas_yscroll)
        self.roster_scroll.pack(side="right", fill="y")
        self.roster_canvas.pack(side="left", fill="both", expand=True)
        self._roster_hover_cid: str | None = None
        self._roster_img_refs: list[tk.PhotoImage] = []
        self.roster_canvas.bind("<Configure>", lambda _e: self._render_roster_visible())
        self.roster_canvas.bind("<Button-1>", self._on_roster_canvas_click)
        self.roster_canvas.bind("<Motion>", self._on_roster_canvas_motion)
        self.roster_canvas.bind("<Leave>", lambda _e: self._set_roster_hover(None))
        self.roster_canvas.bind("<MouseWheel>", self._on_roster_mousewheel)

        # Col 3: Right Profile / Calling / Live Subtitles Pane
        self._momo_right_pane = tk.Frame(main_body, bg="#FFFFFF")
        self._momo_right_pane.pack(side="left", fill="both", expand=True)

        # Top Character Profile & Emoji Call Button Bar
        r_hdr = tk.Frame(self._momo_right_pane, bg="#FFFFFF", padx=16, pady=10, height=64)
        r_hdr.pack(fill="x")
        tk.Frame(self._momo_right_pane, bg="#E2E8F0", height=1).pack(fill="x")

        self.lbl_momo_hdr_avatar = tk.Label(r_hdr, bg="#FFFFFF")
        self.lbl_momo_hdr_avatar.pack(side="left", padx=(0, 12))

        r_hdr_info = tk.Frame(r_hdr, bg="#FFFFFF")
        r_hdr_info.pack(side="left", fill="x", expand=True)
        r_hdr_name_row = tk.Frame(r_hdr_info, bg="#FFFFFF")
        r_hdr_name_row.pack(fill="x")
        self.lbl_momo_hdr_name = tk.Label(
            r_hdr_name_row,
            text="",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 12, "bold"),
            anchor="w",
        )
        self.lbl_momo_hdr_name.pack(side="left")
        self.lbl_momo_hdr_heart = tk.Label(r_hdr_name_row, bg="#FFFFFF")
        self._hdr_heart_ref: tk.PhotoImage | None = None
        _attach_hover_tooltip(
            self.lbl_momo_hdr_heart,
            lambda: self._format_voice_link_tooltip(self.current_momotalk_entry_id or self.current_char_id),
        )
        self.lbl_momo_hdr_sub = tk.Label(
            r_hdr_info,
            text="",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 9),
            anchor="w",
        )
        self.lbl_momo_hdr_sub.pack(fill="x", pady=(1, 0))

        self.btn_momotalk_call = tk.Button(
            r_hdr,
            text="📞",
            command=self.toggle_momotalk_call,
            state="disabled",
            bg="#E2E8F0",
            fg="#94A3B8",
            disabledforeground="#94A3B8",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            font=("Segoe UI Emoji", 13, "bold"),
            relief="flat",
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self.btn_momotalk_call.pack(side="right")
        _attach_hover_tooltip(self.btn_momotalk_call, "통화 시작 / 종료")

        self.btn_momotalk_memory = tk.Button(
            r_hdr,
            text="🧠",
            command=lambda: self.open_memory_note_dialog(initial_tab=0),
            state="disabled",
            bg="#F1F5F9",
            fg="#64748B",
            disabledforeground="#94A3B8",
            activebackground="#FFEAF0",
            activeforeground="#D94B6A",
            font=("Segoe UI Emoji", 12, "bold"),
            relief="flat",
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.btn_momotalk_memory.pack(side="right", padx=(0, 6))
        _attach_hover_tooltip(self.btn_momotalk_memory, "이 학생의 기억 노트 · 스냅샷 복원 · 선생님 프로필")

        self.btn_momotalk_ephemeral = tk.Button(
            r_hdr,
            text="🕵️ 임시",
            command=self.toggle_temporary_call_mode,
            bg="#F1F5F9",
            fg="#64748B",
            activebackground="#FEF3C7",
            activeforeground="#92400E",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=9,
            pady=6,
            cursor="hand2",
        )
        self.btn_momotalk_ephemeral.pack(side="right", padx=(0, 6))
        _attach_hover_tooltip(
            self.btn_momotalk_ephemeral,
            "임시 통화 토글 (ON: 대화 기록·장기 기억을 저장/주입하지 않음 · 테스트용)",
        )
        self._update_ephemeral_toggle_style()

        # On-demand 1-click auto voice create & link button (shown dynamically ONLY when voice link is missing)
        self.btn_momotalk_auto_voice = tk.Button(
            r_hdr,
            text="✨ 목소리 생성",
            command=self._on_click_quick_auto_voice,
            bg="#FFF1F4",
            fg="#D94B6A",
            activebackground="#FECDD3",
            activeforeground="#BE185D",
            disabledforeground="#92400E",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=10,
            pady=6,
            cursor="hand2",
        )
        _attach_hover_tooltip(
            self.btn_momotalk_auto_voice,
            lambda: self._format_missing_voice_cta_tooltip(),
        )

        # Center Stack container ("idle", "calling", "connected")
        self._momo_center_stack = tk.Frame(self._momo_right_pane, bg="#F6F8FB")
        self._momo_center_stack.pack(fill="both", expand=True)

        # State 1: Idle MomoTalk View
        self._momo_idle_frame = tk.Frame(self._momo_center_stack, bg="#F6F8FB")

        self._momo_empty_logo_lbl = tk.Label(self._momo_idle_frame, bg="#F6F8FB")
        empty_momo = self._get_momotalk_photo("ui", "momotalk", 88, "#F6F8FB")
        if empty_momo is not None:
            self._empty_logo_ref = empty_momo
            self._momo_empty_logo_lbl.configure(image=empty_momo)
        self._momo_empty_logo_lbl.place(relx=0.5, rely=0.48, anchor="center")

        # Clean in-game MomoTalk student profile card (hidden until a student is clicked)
        self._momo_card_outer = tk.Frame(
            self._momo_idle_frame,
            bg="#FFFFFF",
            highlightbackground="#DCE3EC",
            highlightthickness=1,
            padx=34,
            pady=28,
        )
        self.lbl_momo_card_avatar = tk.Label(self._momo_card_outer, bg="#FFFFFF")
        self.lbl_momo_card_avatar.pack(pady=(4, 14))
        self.lbl_momo_card_heart = tk.Label(self._momo_card_outer, bg="#FFFFFF")
        self._card_heart_ref: tk.PhotoImage | None = None
        _attach_hover_tooltip(
            self.lbl_momo_card_heart,
            lambda: self._format_voice_link_tooltip(self.current_momotalk_entry_id or self.current_char_id),
        )
        self.lbl_momo_card_title = tk.Label(
            self._momo_card_outer,
            text="",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 15, "bold"),
        )
        self.lbl_momo_card_title.pack()
        self._momo_card_div = tk.Frame(self._momo_card_outer, bg="#E2E8F0", height=1)
        self._momo_card_div.pack(fill="x", pady=(10, 10))
        self.lbl_momo_card_status = tk.Label(
            self._momo_card_outer,
            text="",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 10),
            wraplength=360,
            justify="center",
        )
        self.lbl_momo_card_status.pack(pady=(0, 16))
        self.btn_momo_start_cta = tk.Button(
            self._momo_card_outer,
            text="📞",
            command=self.start_momotalk_call,
            state="disabled",
            bg="#E2E8F0",
            fg="#94A3B8",
            disabledforeground="#94A3B8",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            font=("Segoe UI Emoji", 15, "bold"),
            relief="flat",
            padx=24,
            pady=8,
            cursor="hand2",
        )
        self.btn_momo_start_cta.pack()

        # On-demand 1-click auto voice create & link bar on profile card (hidden automatically once voice is linked)
        self._momo_card_voice_bar = tk.Frame(self._momo_card_outer, bg="#FFFFFF")
        self.btn_momo_card_auto_voice = tk.Button(
            self._momo_card_voice_bar,
            text="✨ 목소리 자동 생성 · 연결",
            command=self._on_click_quick_auto_voice,
            bg="#FFF1F4",
            fg="#D94B6A",
            activebackground="#FECDD3",
            activeforeground="#BE185D",
            disabledforeground="#92400E",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=16,
            pady=7,
            cursor="hand2",
        )
        self.btn_momo_card_auto_voice.pack()
        self.lbl_momo_card_voice_status = tk.Label(
            self._momo_card_voice_bar,
            text="",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            wraplength=360,
            justify="center",
        )
        self.lbl_momo_card_voice_status.pack(pady=(4, 0))

        # Messenger-style past conversation timeline (shown automatically when student has saved history)
        self._momo_history_frame = tk.Frame(self._momo_idle_frame, bg="#F6F8FB")
        hist_bar = tk.Frame(self._momo_history_frame, bg="#FFFFFF", padx=14, pady=8)
        hist_bar.pack(fill="x")
        tk.Frame(self._momo_history_frame, bg="#E2E8F0", height=1).pack(fill="x")

        self.lbl_history_meta = tk.Label(
            hist_bar,
            text="💬 지난 모모톡 기록",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        )
        self.lbl_history_meta.pack(side="left", fill="x", expand=True)

        tk.Button(
            hist_bar,
            text="👤 프로필",
            command=self._toggle_idle_profile_card_view,
            bg="#F1F5F9",
            fg="#475569",
            activebackground="#E2E8F0",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=9,
            pady=3,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            hist_bar,
            text="🧠 기억 노트",
            command=lambda: self.open_memory_note_dialog(initial_tab=0),
            bg="#FFEAF0",
            fg="#D94B6A",
            activebackground="#FECDD3",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=9,
            pady=3,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))

        hist_host = tk.Frame(
            self._momo_history_frame,
            bg="#FFFFFF",
            highlightbackground="#E2E8F0",
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        hist_host.pack(fill="both", expand=True, padx=10, pady=8)

        self.history_box = scrolledtext.ScrolledText(
            hist_host,
            height=10,
            font=("Malgun Gothic", 10),
            bg="#FFFFFF",
            fg="#1E293B",
            wrap="word",
            padx=14,
            pady=10,
            relief="flat",
            bd=0,
            state="disabled",
        )
        self.history_box.pack(fill="both", expand=True)
        self.history_box.tag_configure(
            "sess_div",
            font=("Malgun Gothic", 8, "bold"),
            foreground="#64748B",
            background="#F1F5F9",
            justify="center",
            spacing1=10,
            spacing3=6,
        )
        self.history_box.tag_configure(
            "speaker",
            font=("Malgun Gothic", 10, "bold"),
            foreground="#D94B6A",
            spacing1=8,
            spacing3=2,
        )
        self.history_box.tag_configure(
            "sensei_speaker",
            font=("Malgun Gothic", 9, "bold"),
            foreground="#0284C7",
            justify="right",
            spacing1=8,
            spacing3=2,
        )
        self.history_box.tag_configure(
            "sensei_line",
            font=("Malgun Gothic", 10, "bold"),
            foreground="#0F172A",
            background="#E0F2FE",
            justify="right",
            rmargin=10,
            spacing3=3,
        )
        self.history_box.tag_configure(
            "stt_fixed_note",
            font=("Malgun Gothic", 8),
            foreground="#0F766E",
            justify="right",
            rmargin=10,
            spacing3=4,
        )
        self.history_box.tag_configure(
            "jp_dual_line",
            font=("Malgun Gothic", 10, "bold"),
            foreground="#1E293B",
            lmargin1=14,
            lmargin2=14,
            spacing3=1,
        )
        self.history_box.tag_configure(
            "kr_dual_line",
            font=("Malgun Gothic", 10),
            foreground="#475569",
            lmargin1=14,
            lmargin2=14,
            spacing3=5,
        )

        self.btn_card_back_to_history = tk.Button(
            self._momo_card_outer,
            text="💬 지난 대화 기록 보기",
            command=self._toggle_idle_profile_card_view,
            bg="#F1F5F9",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        )

        # State 2: Outgoing Phone Calling Waiting Screen
        self._momo_calling_frame = tk.Frame(self._momo_center_stack, bg="#263040")
        call_inner = tk.Frame(self._momo_calling_frame, bg="#263040")
        call_inner.place(relx=0.5, rely=0.49, anchor="center")

        self.calling_canvas = tk.Canvas(
            call_inner,
            width=240,
            height=200,
            bg="#263040",
            highlightthickness=0,
            bd=0,
        )
        self.calling_canvas.pack(pady=(0, 6))
        self._calling_portrait_ref: tk.PhotoImage | None = None

        self.lbl_calling_name = tk.Label(
            call_inner,
            text="",
            bg="#263040",
            fg="#FFFFFF",
            font=("Malgun Gothic", 16, "bold"),
        )
        self.lbl_calling_name.pack()
        self.lbl_calling_sub = tk.Label(
            call_inner,
            text="",
            bg="#263040",
            fg="#94A3B8",
            font=("Malgun Gothic", 9),
        )
        self.lbl_calling_sub.pack(pady=(2, 10))
        self.lbl_calling_status = tk.Label(
            call_inner,
            text="통화 연결 중...",
            bg="#263040",
            fg="#FDA4AF",
            font=("Malgun Gothic", 12, "bold"),
        )
        self.lbl_calling_status.pack()
        self.lbl_calling_timer = tk.Label(
            call_inner,
            text="00:00",
            bg="#263040",
            fg="#E2E8F0",
            font=("Consolas", 11, "bold"),
        )
        self.lbl_calling_timer.pack(pady=(2, 10))

        self._calling_login_bar = tk.Frame(call_inner, bg="#3B2F2F", padx=12, pady=8)
        tk.Label(
            self._calling_login_bar,
            text="🔑 로그인이 필요합니다 (로그인 완료 후 아래 버튼을 눌러 주세요)",
            bg="#3B2F2F",
            fg="#FDE68A",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(pady=(0, 5))
        lb_row = tk.Frame(self._calling_login_bar, bg="#3B2F2F")
        lb_row.pack()
        tk.Button(
            lb_row,
            text="🌐 로그인 창 열기",
            command=self.toggle_gemini_browser_window,
            bg="#1E40AF",
            fg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            lb_row,
            text="✅ 완료 확인",
            command=self.confirm_browser_login_async,
            bg="#16A34A",
            fg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        ).pack(side="left")

        tk.Button(
            call_inner,
            text="📵",
            command=self.stop_momotalk_call,
            bg="#EF4444",
            fg="#FFFFFF",
            activebackground="#DC2626",
            activeforeground="#FFFFFF",
            font=("Segoe UI Emoji", 14, "bold"),
            relief="flat",
            padx=26,
            pady=8,
            cursor="hand2",
        ).pack(pady=(8, 0))

        # State 3: Connected Live Call Bar + Real-Time Subtitles Host (minimal immersion bar)
        self._momo_connected_bar = tk.Frame(self._momo_center_stack, bg="#2A3446")
        live_top = tk.Frame(self._momo_connected_bar, bg="#2A3446", padx=14, pady=8)
        live_top.pack(fill="x")
        self.lbl_live_badge = tk.Label(
            live_top,
            text="🟢",
            bg="#2A3446",
            fg="#10B981",
            font=("Segoe UI Emoji", 9, "bold"),
        )
        self.lbl_live_badge.pack(side="left", padx=(0, 4))
        self.lbl_live_call_timer = tk.Label(
            live_top,
            text="00:00",
            bg="#2A3446",
            fg="#FFFFFF",
            font=("Consolas", 10, "bold"),
        )
        self.lbl_live_call_timer.pack(side="left", padx=(0, 10))
        self.lbl_live_char_name = tk.Label(
            live_top,
            text="",
            bg="#2A3446",
            fg="#E2E8F0",
            font=("Malgun Gothic", 10, "bold"),
            anchor="w",
        )
        self.lbl_live_char_name.pack(side="left", fill="x", expand=True)

        self.lbl_live_ephemeral_badge = tk.Label(
            live_top,
            text="🕵️ 임시 통화 (기억 안 함)",
            bg="#FEF3C7",
            fg="#92400E",
            font=("Malgun Gothic", 8, "bold"),
            padx=7,
            pady=2,
        )

        btn_live_hangup = tk.Button(
            live_top,
            text="📵",
            command=self.stop_momotalk_call,
            bg="#EF4444",
            fg="#FFFFFF",
            activebackground="#DC2626",
            font=("Segoe UI Emoji", 10, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        )
        btn_live_hangup.pack(side="right", padx=(6, 0))
        _attach_hover_tooltip(btn_live_hangup, "통화 종료 (자동 기억 정리)")

        btn_live_reset = tk.Button(
            live_top,
            text="🔄",
            command=self.reset_companion_conversation_async,
            bg="#3B82F6",
            fg="#FFFFFF",
            font=("Segoe UI Emoji", 9, "bold"),
            relief="flat",
            padx=8,
            pady=4,
            cursor="hand2",
        )
        btn_live_reset.pack(side="right", padx=(4, 0))
        _attach_hover_tooltip(btn_live_reset, "이번 통화만 새 주제로 시작 (저장된 장기 기억 유지)")

        btn_live_nudge = tk.Button(
            live_top,
            text="👋",
            command=self._on_live_nudge_clicked,
            bg="#0EA5E9",
            fg="#FFFFFF",
            font=("Segoe UI Emoji", 9, "bold"),
            relief="flat",
            padx=8,
            pady=4,
            cursor="hand2",
        )
        btn_live_nudge.pack(side="right", padx=(4, 0))
        _attach_hover_tooltip(btn_live_nudge, "캐릭터가 먼저 말 걸게 하기")

        btn_live_abort = tk.Button(
            live_top,
            text="🔇",
            command=self.abort_current_playback,
            bg="#475569",
            fg="#FFFFFF",
            font=("Segoe UI Emoji", 9, "bold"),
            relief="flat",
            padx=8,
            pady=4,
            cursor="hand2",
        )
        btn_live_abort.pack(side="right", padx=(4, 0))
        _attach_hover_tooltip(btn_live_abort, "현재 재생 중인 캐릭터 음성 멈추기")

        self._momo_subtitle_host = tk.Frame(self._momo_center_stack, bg="#F3F5F8", padx=10, pady=8)
        self._momo_idle_frame.pack(fill="both", expand=True)

    def _build_ui(self) -> None:
        self._build_momotalk_chrome()

        # Minimal off-screen backing widgets kept strictly unmapped for state & test compatibility
        f0 = tk.Frame(self._legacy_backing_frame)
        f0.pack()
        self.f0 = f0
        schools = sorted({c.get("school", "기타") for c in self.characters.values() if c.get("school")})
        self.cmb_school = ttk.Combobox(
            f0,
            textvariable=self.school_filter_var,
            values=["전체 학원", PATCH15_FILTER_LABEL] + schools,
            state="readonly",
        )
        self.cmb_char = ttk.Combobox(f0, textvariable=self.char_display_var, values=[], state="readonly")
        self.lbl_char_badge = tk.Label(f0, text="")
        self.lbl_acting_badge = tk.Label(f0, text="")
        self.lbl_char_summary = tk.Label(f0, text="")
        self.cmb_persona_variant = ttk.Combobox(
            f0,
            textvariable=self.persona_variant_var,
            values=list(PERSONA_VARIANT_LABELS.values()),
            state="readonly",
        )

        f1 = tk.Frame(self._legacy_backing_frame)
        f1.pack()
        self.f1 = f1
        self.btn_open_ref = tk.Button(f1, text="ref", command=self.open_reference_folder)

        f2 = tk.Frame(self._legacy_backing_frame)
        f2.pack()
        self.f2 = f2
        self.btn_test_tts = tk.Button(f2, text="tts", command=self.test_aru_tts_async)

        f3 = tk.Frame(self._legacy_backing_frame)
        f3.pack()
        self.f3 = f3
        self.mode_notebook = ttk.Notebook(f3)
        tab_m1 = tk.Frame(self.mode_notebook)
        tab_m2 = tk.Frame(self.mode_notebook)
        self.mode_notebook.add(tab_m1, text="mode1")
        self.mode_notebook.add(tab_m2, text="mode2")
        if self.cfg.get("bridge_mode") == "gemini_live_audio":
            self.mode_notebook.select(tab_m2)

        self.cmb_mic = ttk.Combobox(tab_m1, textvariable=self.mic_choice_var, values=self.mic_inputs, state="readonly")
        self.cmb_out_m1 = ttk.Combobox(
            tab_m1, textvariable=self.out_choice_var, values=self.primary_outputs, state="readonly"
        )
        self.btn_login_confirm = tk.Button(tab_m1, text="login", command=self.confirm_browser_login_async)
        self.cmb_companion_model = ttk.Combobox(
            tab_m1,
            textvariable=self.companion_model_var,
            values=[lbl for _, lbl in self._active_web_models()],
            state="readonly",
        )
        self.cmb_companion_thinking = ttk.Combobox(
            tab_m1,
            textvariable=self.companion_thinking_var,
            values=[lbl for _, lbl in GEMINI_THINKING_LEVELS],
            state="readonly",
        )
        self.btn_sync_models = tk.Button(
            tab_m1, text="계정 모델 동기화", command=lambda: self.sync_companion_models_async(None)
        )
        self.btn_companion_toggle = tk.Button(tab_m1, text="AI 보이스 대화 시작", command=self.toggle_companion_mode)
        self.lbl_companion_meter = tk.Label(tab_m1, textvariable=self.companion_meter_var)
        self.lbl_companion_state = tk.Label(tab_m1, textvariable=self.companion_state_var)

        self.cmb_in = ttk.Combobox(tab_m2, textvariable=self.in_choice_var, values=list(self.in_map.keys()), state="readonly")
        self.cmb_out = ttk.Combobox(tab_m2, textvariable=self.out_choice_var, values=self.primary_outputs, state="readonly")
        self.cmb_trig = ttk.Combobox(
            tab_m2, textvariable=self.trigger_out_choice_var, values=self.trigger_outputs, state="readonly"
        )
        self.btn_toggle = tk.Button(tab_m2, text="실시간 음성 브릿지 시작", command=self.toggle_bridge)
        self.lbl_meter = tk.Label(tab_m2, text="")
        self.lbl_proactive_status = tk.Label(tab_m2, text="")

        # Step 4: MomoTalk Real-Time Dialogue Subtitle Panel
        f4 = tk.Frame(
            self._momo_subtitle_host,
            bg="#FFFFFF",
            highlightbackground="#E2E8F0",
            highlightthickness=1,
            padx=12,
            pady=8,
        )
        f4.pack(fill="both", expand=True)

        sub_bar = tk.Frame(f4, bg="#FFFFFF")
        sub_bar.pack(fill="x", pady=(0, 6))
        tk.Label(
            sub_bar,
            text="💬 실시간 대사 자막",
            bg="#FFFFFF",
            fg="#2A3446",
            font=("Malgun Gothic", 10, "bold"),
        ).pack(side="left", padx=(0, 10))

        self._subtitle_mode_btns.clear()
        for mode_code in SUBTITLE_MODES:
            rb = tk.Radiobutton(
                sub_bar,
                text=SUBTITLE_MODE_LABELS[mode_code],
                variable=self.subtitle_mode_var,
                value=mode_code,
                indicatoron=False,
                command=self._on_subtitle_mode_changed,
                font=("Malgun Gothic", 9),
                bg="#F1F5F9",
                fg="#64748B",
                selectcolor="#FFEAF0",
                activebackground="#FFF1F4",
                activeforeground="#D94B6A",
                relief="flat",
                bd=0,
                padx=10,
                pady=3,
                cursor="hand2",
            )
            rb.pack(side="left", padx=(0, 6))
            self._subtitle_mode_btns[mode_code] = rb
        self._update_subtitle_mode_buttons_style()

        tk.Button(
            sub_bar,
            text="🧹",
            command=self.clear_subtitles,
            bg="#F1F5F9",
            fg="#475569",
            font=("Segoe UI Emoji", 9, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        ).pack(side="right")

        # Hidden backward-compatible subtitle/setup controls kept off-screen
        self.btn_toggle_setup = tk.Button(
            self._legacy_backing_frame,
            text="🔼 초기설정 접기",
            command=self.toggle_setup_panels_collapse,
        )
        self.cmb_subtitle_thinking = ttk.Combobox(
            self._legacy_backing_frame,
            textvariable=self.subtitle_thinking_var,
            values=[lbl for _, lbl in GEMINI_THINKING_LEVELS],
            state="readonly",
            width=11,
        )
        self.cmb_subtitle_thinking.bind("<<ComboboxSelected>>", self._on_subtitle_thinking_changed)
        self.cmb_subtitle_engine = ttk.Combobox(
            self._legacy_backing_frame,
            textvariable=self.subtitle_engine_var,
            values=[lbl for _, lbl in self._subtitle_choices()],
            state="readonly",
            width=23,
        )
        self.cmb_subtitle_engine.bind("<<ComboboxSelected>>", self._on_subtitle_engine_changed)

        self.subtitle_box = scrolledtext.ScrolledText(
            f4,
            height=8,
            font=("Malgun Gothic", 11),
            bg="#FFFFFF",
            fg="#1E293B",
            wrap="word",
            padx=16,
            pady=12,
            relief="flat",
            bd=0,
        )
        self.subtitle_box.pack(fill="both", expand=True)
        self.subtitle_box.tag_configure(
            "speaker",
            font=("Malgun Gothic", 10, "bold"),
            foreground="#D94B6A",
            spacing1=8,
            spacing3=2,
        )
        self.subtitle_box.tag_configure(
            "sensei_speaker",
            font=("Malgun Gothic", 9, "bold"),
            foreground="#0284C7",
            justify="right",
            spacing1=8,
            spacing3=2,
        )
        self.subtitle_box.tag_configure(
            "sensei_line",
            font=("Malgun Gothic", 11, "bold"),
            foreground="#0F172A",
            background="#E0F2FE",
            justify="right",
            rmargin=10,
            spacing3=4,
        )
        self.subtitle_box.tag_configure(
            "jp_only_line",
            font=("Malgun Gothic", 12, "bold"),
            foreground="#1E293B",
            lmargin1=14,
            lmargin2=14,
            spacing3=3,
        )
        self.subtitle_box.tag_configure(
            "kr_only_line",
            font=("Malgun Gothic", 12, "bold"),
            foreground="#1E293B",
            lmargin1=14,
            lmargin2=14,
            spacing3=3,
        )
        self.subtitle_box.tag_configure(
            "jp_dual_line",
            font=("Malgun Gothic", 11, "bold"),
            foreground="#334155",
            lmargin1=14,
            lmargin2=14,
            spacing3=1,
        )
        self.subtitle_box.tag_configure(
            "kr_dual_line",
            font=("Malgun Gothic", 11, "bold"),
            foreground="#0369A1",
            lmargin1=14,
            lmargin2=14,
            spacing3=5,
        )
        self.subtitle_box.tag_configure(
            "pending_line",
            font=("Malgun Gothic", 10),
            foreground="#94A3B8",
            lmargin1=14,
            lmargin2=14,
            spacing3=4,
        )
        self.subtitle_box.tag_configure("refining_tag", font=("Malgun Gothic", 9, "bold"), foreground="#FB7390")
        self.subtitle_box.tag_configure("placeholder", font=("Malgun Gothic", 10), foreground="#94A3B8")
        self.subtitle_box.bind("<Control-c>", lambda e: None)
        self._rerender_subtitles()

        self.log(f"[MomoTalk 에디션 v5] 블루 아카이브 캐릭터 페르소나 {len(self.characters)}명 + Steam 모모톡 UI 로드 완료!")
        self.log("[맞춤 감정 디렉팅] 쿨·다우너/츤데레/허세폭주/하이텐션 등 캐릭터 고유 텐션에 맞춰 호흡·문장부호·Fish s2 연기 태그가 자동 적용됩니다.")

    # ------------------------------------------------------------------
    # MomoTalk Roster, Profile Card, Calling & Connection UI Controllers
    # ------------------------------------------------------------------

    ROW_H = 68

    def _set_momotalk_nav_tab(self, tab: str) -> None:
        self._momotalk_nav_tab = "recent" if tab in ("recent", "starred") else "all"
        if self._momotalk_nav_tab == "all":
            self._nav_btn_all.configure(bg="#4B586E", fg="#FFFFFF")
            self._nav_btn_star.configure(bg="#374254", fg="#CBD5E1")
        else:
            self._nav_btn_all.configure(bg="#374254", fg="#CBD5E1")
            self._nav_btn_star.configure(bg="#4B586E", fg="#FFFFFF")
        self._refresh_character_filter_list(keep_current=True)

    def _toggle_momotalk_sort(self) -> None:
        order = ("default", "name", "heart")
        cur = getattr(self, "_momotalk_sort_mode", "default")
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "default"
        self._momotalk_sort_mode = nxt
        lbl_map = {"default": "기본순 ⇅", "name": "이름순 ⇅", "heart": "💗 하트순 ⇅"}
        self.btn_roster_sort.configure(text=lbl_map[nxt])
        self._refresh_character_filter_list(keep_current=True)

    def _clear_momotalk_search(self) -> None:
        self.search_var.set("")
        self._refresh_character_filter_list(keep_current=True)

    def _on_roster_yview(self, *args: object) -> None:
        self.roster_canvas.yview(*args)
        self._render_roster_visible()

    def _on_roster_canvas_yscroll(self, first: str, last: str) -> None:
        self.roster_scroll.set(first, last)
        try:
            f_val = float(first)
        except Exception:
            return
        if (not getattr(self, "_rendering_roster", False)) and abs(f_val - getattr(self, "_last_yview_top", -1.0)) > 1e-4:
            self._last_yview_top = f_val
            self._render_roster_visible()

    def _on_roster_mousewheel(self, event: tk.Event[Any]) -> None:
        delta = -1 if int(getattr(event, "delta", 0)) > 0 else 1
        before = self.roster_canvas.yview()
        self.roster_canvas.yview_scroll(delta * 2, "units")
        if self.roster_canvas.yview() != before and not getattr(self, "_rendering_roster", False):
            self._render_roster_visible()

    def _update_roster_row_appearance(self, slot: dict[str, Any], is_sel: bool, is_hov: bool) -> None:
        cv = self.roster_canvas
        bg_hex = "#FFEAF0" if is_sel else ("#F8FAFC" if is_hov else "#FFFFFF")
        if slot.get("bg_hex") != bg_hex:
            slot["bg_hex"] = bg_hex
            cv.itemconfigure(slot["bg"], fill=bg_hex)
        if slot.get("is_sel") != is_sel:
            slot["is_sel"] = is_sel
            cv.itemconfigure(slot["sel_bar"], state="normal" if is_sel else "hidden")
            cv.itemconfigure(slot["sel_ring"], state="normal" if is_sel else "hidden")
            cv.itemconfigure(slot["name"], fill="#D94B6A" if is_sel else "#263238")

    def _set_roster_hover(self, cid: str | None) -> None:
        prev = getattr(self, "_roster_hover_cid", None)
        if cid == prev:
            return
        self._roster_hover_cid = cid
        active_sel = self.current_momotalk_entry_id if self._momotalk_char_activated else ""
        for slot in getattr(self, "_roster_active_rows", {}).values():
            s_eid = slot.get("eid")
            if s_eid == prev or s_eid == cid:
                self._update_roster_row_appearance(
                    slot,
                    is_sel=bool(active_sel and s_eid == active_sel),
                    is_hov=(s_eid == cid),
                )

    def _on_roster_canvas_motion(self, event: tk.Event[Any]) -> None:
        y = self.roster_canvas.canvasy(event.y)
        idx = int(y // self.ROW_H)
        cid = self._momotalk_filtered_cids[idx] if 0 <= idx < len(self._momotalk_filtered_cids) else None
        self._set_roster_hover(cid)

    def _on_roster_canvas_click(self, event: tk.Event[Any]) -> None:
        y = self.roster_canvas.canvasy(event.y)
        idx = int(y // self.ROW_H)
        if 0 <= idx < len(self._momotalk_filtered_cids):
            self.select_momotalk_character(self._momotalk_filtered_cids[idx])

    @staticmethod
    def _clip_text(txt: str, max_chars: int) -> str:
        t = (txt or "").strip().replace("\n", " ")
        return (t[: max_chars - 1] + "…") if len(t) > max_chars else t

    def _create_roster_slot(self, w: int) -> dict[str, Any]:
        cv = self.roster_canvas
        return {
            "bg": cv.create_rectangle(0, 0, w, self.ROW_H, fill="#FFFFFF", outline="", state="hidden", tags="row"),
            "sel_bar": cv.create_rectangle(0, 0, 4, self.ROW_H, fill="#FB7390", outline="", state="hidden", tags="row"),
            "div": cv.create_line(14, self.ROW_H - 1, w - 10, self.ROW_H - 1, fill="#EDF2F7", state="hidden", tags="row"),
            "img": cv.create_image(38, 34, state="hidden", tags="row"),
            "sel_ring": cv.create_oval(14, 10, 62, 58, outline="#FB7390", width=2, state="hidden", tags="row"),
            "name": cv.create_text(
                74,
                23,
                text="",
                anchor="w",
                fill="#263238",
                font=("Malgun Gothic", 10, "bold"),
                state="hidden",
                tags="row",
            ),
            "status": cv.create_text(
                74,
                45,
                text="",
                anchor="w",
                fill="#78879B",
                font=("Malgun Gothic", 9),
                state="hidden",
                tags="row",
            ),
            "heart": cv.create_image(max(96, w - 24), 34, state="hidden", tags="row"),
            "row_idx": -1,
            "eid": "",
            "w": 0,
            "visible": False,
            "has_status": False,
            "has_voice": False,
            "is_sel": None,
            "bg_hex": "",
            "is_placeholder": True,
            "heart_is_placeholder": True,
            "photo_ref": None,
            "heart_ref": None,
        }

    def _hide_roster_slot(self, slot: dict[str, Any]) -> None:
        if not slot.get("visible", False):
            return
        cv = self.roster_canvas
        for k in ("bg", "sel_bar", "div", "img", "sel_ring", "name", "status", "heart"):
            cv.itemconfigure(slot[k], state="hidden")
        slot["visible"] = False
        slot["row_idx"] = -1
        slot["eid"] = ""
        slot["has_voice"] = False
        slot["is_sel"] = None
        slot["bg_hex"] = ""

    def _bind_roster_slot(
        self,
        slot: dict[str, Any],
        idx: int,
        eid: str,
        w: int,
        is_sel: bool,
        is_hov: bool,
        force_all: bool = False,
    ) -> None:
        cv = self.roster_canvas
        ent = self.momotalk_entry_map.get(eid) or {}
        status_msg = str(ent.get("status_message_kr") or "").strip()
        has_status = bool(status_msg)
        has_voice = self._has_student_voice_link(eid)
        y0 = idx * self.ROW_H
        y1 = y0 + self.ROW_H

        if not slot.get("visible", False):
            for k in ("bg", "div", "img", "name"):
                cv.itemconfigure(slot[k], state="normal")
            slot["visible"] = True
            slot["is_sel"] = None
            slot["bg_hex"] = ""

        if slot.get("row_idx") != idx or slot.get("w") != w or slot.get("has_status") != has_status:
            cv.coords(slot["bg"], 0, y0, w, y1)
            cv.coords(slot["sel_bar"], 0, y0, 4, y1)
            cv.coords(slot["div"], 14, y1 - 1, w - 10, y1 - 1)
            cv.coords(slot["img"], 38, y0 + 34)
            cv.coords(slot["sel_ring"], 14, y0 + 10, 62, y0 + 58)
            cv.coords(slot["name"], 74, y0 + (23 if has_status else 34))
            if has_status:
                cv.coords(slot["status"], 74, y0 + 45)
            cv.coords(slot["heart"], max(96, w - 24), y0 + 34)
            slot["row_idx"] = idx
            slot["w"] = w

        content_changed = (
            slot.get("eid") != eid
            or slot.get("has_status") != has_status
            or slot.get("has_voice") != has_voice
            or force_all
        )
        if content_changed:
            slot["eid"] = eid
            slot["has_status"] = has_status
            slot["has_voice"] = has_voice
            name_max = 15 if has_voice else 18
            status_max = 18 if has_voice else 22
            cv.itemconfigure(slot["name"], text=self._clip_text(str(ent.get("name_kr") or eid), name_max))
            if has_status:
                cv.itemconfigure(slot["status"], text=self._clip_text(status_msg, status_max), state="normal")
            else:
                cv.itemconfigure(slot["status"], state="hidden")
            if has_voice:
                heart_img, heart_final = self._get_momotalk_heart_photo(size=22, enqueue=True)
                if heart_img is not None:
                    slot["heart_ref"] = heart_img
                    self._roster_heart_photo_ref = heart_img
                    cv.itemconfigure(slot["heart"], image=heart_img, state="normal")
                    slot["heart_is_placeholder"] = not heart_final
                else:
                    cv.itemconfigure(slot["heart"], state="hidden")
            else:
                slot["heart_ref"] = None
                slot["heart_is_placeholder"] = False
                cv.itemconfigure(slot["heart"], state="hidden")

            portrait, is_final = self._resolve_momotalk_photo(
                "portrait", eid, 48, "#FFFFFF", "#E2E8F0", enqueue=True
            )
            if portrait is not None:
                slot["photo_ref"] = portrait
                cv.itemconfigure(slot["img"], image=portrait)
            slot["is_placeholder"] = not is_final
        else:
            if slot.get("is_placeholder", True):
                portrait, is_final = self._resolve_momotalk_photo(
                    "portrait", eid, 48, "#FFFFFF", "#E2E8F0", enqueue=False
                )
                if is_final and portrait is not None:
                    slot["photo_ref"] = portrait
                    cv.itemconfigure(slot["img"], image=portrait)
                    slot["is_placeholder"] = False
            if has_voice and slot.get("heart_is_placeholder", False):
                heart_img, heart_final = self._get_momotalk_heart_photo(size=22, enqueue=False)
                if heart_final and heart_img is not None:
                    slot["heart_ref"] = heart_img
                    self._roster_heart_photo_ref = heart_img
                    cv.itemconfigure(slot["heart"], image=heart_img, state="normal")
                    slot["heart_is_placeholder"] = False

        self._update_roster_row_appearance(slot, is_sel=is_sel, is_hov=is_hov)

    def _refresh_visible_roster_portraits(self, enqueue_missing: bool = False) -> None:
        if not hasattr(self, "roster_canvas") or not getattr(self, "_roster_active_rows", None):
            return
        cv = self.roster_canvas
        changed = False
        heart_img, heart_final = self._get_momotalk_heart_photo(size=22, enqueue=enqueue_missing)
        for slot in self._roster_active_rows.values():
            if not slot.get("visible"):
                continue
            if slot.get("has_voice") and slot.get("heart_is_placeholder", False) and heart_img is not None and (heart_final or enqueue_missing):
                slot["heart_ref"] = heart_img
                self._roster_heart_photo_ref = heart_img
                cv.itemconfigure(slot["heart"], image=heart_img, state="normal")
                slot["heart_is_placeholder"] = not heart_final
                changed = True
            if not slot.get("is_placeholder", True):
                continue
            eid = str(slot.get("eid") or "")
            if not eid:
                continue
            portrait, is_final = self._resolve_momotalk_photo(
                "portrait",
                eid,
                48,
                "#FFFFFF",
                "#E2E8F0",
                enqueue=enqueue_missing,
            )
            if portrait is not None and (is_final or enqueue_missing):
                slot["photo_ref"] = portrait
                cv.itemconfigure(slot["img"], image=portrait)
                slot["is_placeholder"] = not is_final
                changed = True
        if changed:
            self._roster_img_refs = [
                s["photo_ref"] for s in self._roster_active_rows.values() if s.get("photo_ref") is not None
            ]

    def _sync_roster_selection_visual(self) -> None:
        if not getattr(self, "_roster_active_rows", None):
            self._render_roster_visible()
            return
        prev_sel = getattr(self, "_roster_last_sel_eid", "")
        cur_sel = self.current_momotalk_entry_id if self._momotalk_char_activated else ""
        hov_cid = getattr(self, "_roster_hover_cid", None)
        self._roster_last_sel_eid = cur_sel
        self._roster_last_sel_active = bool(self._momotalk_char_activated)
        for slot in self._roster_active_rows.values():
            s_eid = slot.get("eid")
            if s_eid == prev_sel or s_eid == cur_sel:
                self._update_roster_row_appearance(
                    slot,
                    is_sel=bool(cur_sel and s_eid == cur_sel),
                    is_hov=(s_eid == hov_cid),
                )

    def _render_roster_visible(self, force_all: bool = False) -> None:
        if not hasattr(self, "roster_canvas") or getattr(self, "_rendering_roster", False):
            return
        self._rendering_roster = True
        try:
            cv = self.roster_canvas
            total = len(self._momotalk_filtered_cids)
            w = max(290, int(cv.winfo_width() or 315))
            total_h = max(1, total * self.ROW_H)
            if (w, total_h) != getattr(self, "_last_scrollregion", (0, 0)):
                self._last_scrollregion = (w, total_h)
                cv.configure(scrollregion=(0, 0, w, total_h))

            if total == 0:
                for slot in self._roster_active_rows.values():
                    self._hide_roster_slot(slot)
                    self._roster_free_pool.append(slot)
                self._roster_active_rows.clear()
                self._roster_img_refs = []
                empty_msg = "최근 통화한 학생이 없습니다." if self._momotalk_nav_tab == "recent" else "검색 결과가 없습니다."
                if self._roster_empty_text_id is None:
                    self._roster_empty_text_id = cv.create_text(
                        w // 2,
                        90,
                        text=empty_msg,
                        fill="#94A3B8",
                        font=("Malgun Gothic", 9, "bold"),
                        tags="empty_msg",
                    )
                else:
                    cv.coords(self._roster_empty_text_id, w // 2, 90)
                    cv.itemconfigure(self._roster_empty_text_id, text=empty_msg, state="normal")
                return

            if self._roster_empty_text_id is not None:
                cv.itemconfigure(self._roster_empty_text_id, state="hidden")

            top_y = float(cv.canvasy(0))
            view_h = float(max(240, cv.winfo_height() or 520))
            first_idx = max(0, int((top_y - self.ROW_H) // self.ROW_H))
            last_idx = min(total, int((top_y + view_h + self.ROW_H * 2) // self.ROW_H) + 1)

            stale = [idx for idx in self._roster_active_rows if idx < first_idx or idx >= last_idx]
            for idx in stale:
                self._roster_free_pool.append(self._roster_active_rows.pop(idx))

            cur_sel = self.current_momotalk_entry_id if self._momotalk_char_activated else ""
            self._roster_last_sel_eid = cur_sel
            self._roster_last_sel_active = bool(self._momotalk_char_activated)
            hov_cid = getattr(self, "_roster_hover_cid", None)

            # Bind bottom-up so top-most visible uncached rows sit at the front of the LIFO extraction deque
            for i in range(last_idx - 1, first_idx - 1, -1):
                eid = self._momotalk_filtered_cids[i]
                slot = self._roster_active_rows.get(i)
                if slot is None:
                    slot = self._roster_free_pool.pop() if self._roster_free_pool else self._create_roster_slot(w)
                    self._roster_active_rows[i] = slot
                self._bind_roster_slot(
                    slot,
                    i,
                    eid,
                    w,
                    is_sel=bool(cur_sel and eid == cur_sel),
                    is_hov=(eid == hov_cid),
                    force_all=force_all,
                )

            for unused_slot in self._roster_free_pool:
                self._hide_roster_slot(unused_slot)

            self._roster_img_refs = [
                s["photo_ref"] for s in self._roster_active_rows.values() if s.get("photo_ref") is not None
            ]
        finally:
            self._rendering_roster = False

    def _get_active_memory_key(self) -> tuple[str, str]:
        """Return `(base_cid, track)` for the currently selected student and persona variant."""
        use_custom = bool(self.cfg.get("use_custom_persona", False))
        vcode = PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), str(self.cfg.get("persona_prompt_variant") or "official"))
        track = persona_variant_to_memory_track(vcode, use_custom_persona=use_custom)
        if use_custom:
            return "__custom__", "custom"
        return str(self.current_char_id or "Aru"), track

    def _update_ephemeral_toggle_style(self) -> None:
        is_eph = bool(self.temporary_call_var.get())
        if hasattr(self, "btn_momotalk_ephemeral"):
            if is_eph:
                self.btn_momotalk_ephemeral.configure(
                    text="🕵️ 기억 끄기 ON",
                    bg="#FEF3C7",
                    fg="#92400E",
                    activebackground="#FDE68A",
                    activeforeground="#78350F",
                )
            else:
                self.btn_momotalk_ephemeral.configure(
                    text="🕵️ 임시",
                    bg="#F1F5F9",
                    fg="#64748B",
                    activebackground="#E2E8F0",
                    activeforeground="#1E293B",
                )
        if hasattr(self, "lbl_live_ephemeral_badge"):
            try:
                if is_eph:
                    self.lbl_live_ephemeral_badge.pack(side="left", padx=(0, 8))
                else:
                    self.lbl_live_ephemeral_badge.pack_forget()
            except Exception:
                pass

    def toggle_temporary_call_mode(self) -> None:
        new_val = not bool(self.temporary_call_var.get())
        self.temporary_call_var.set(new_val)
        self.cfg["temporary_call_mode"] = new_val
        self._update_ephemeral_toggle_style()
        if getattr(self, "_ui_boot_complete", False):
            save_bridge_config(self.cfg)
        if new_val:
            self.log("🕵️ [임시 통화 ON] 다음 통화부터는 대화 기록과 장기 기억을 저장/주입하지 않습니다 (테스트용).")
        else:
            self.log("🧠 [임시 통화 OFF] 다음 통화부터 대화 기록 저장 및 학생별 기억 유지가 정상 동작합니다.")

    def _toggle_idle_profile_card_view(self) -> None:
        self._idle_force_profile_card = not bool(self._idle_force_profile_card)
        self._refresh_momotalk_header_and_cards()

    def open_memory_note_dialog(self, initial_tab: int = 0) -> None:
        if self._momotalk_char_activated:
            base_cid, track = self._get_active_memory_key()
            eid = self.current_momotalk_entry_id or self.current_char_id
            ent = self.momotalk_entry_map.get(eid) or {}
            disp_name = "커스텀 페르소나" if base_cid == "__custom__" else str(ent.get("name_kr") or self.current_char.get("name_kr") or base_cid)
        else:
            base_cid, track, disp_name = "", "official", "선생님 프로필"
        MemoryNoteDialog(
            self.root,
            self.memory_mgr,
            base_cid=base_cid,
            student_name_kr=disp_name,
            track=track,
            on_changed=self._on_memory_data_changed,
            initial_tab=initial_tab,
        )

    def _on_memory_data_changed(self) -> None:
        self._recent_called_entry_ids = self.memory_mgr.get_recent_entry_ids()
        if getattr(self, "_momotalk_nav_tab", "all") == "recent":
            self._refresh_character_filter_list(keep_current=True)
        self._refresh_momotalk_header_and_cards()

    def _delete_history_session(self, base_cid: str, track: str, session_id: str) -> None:
        if not messagebox.askyesno(
            "통화 기록 삭제",
            "이 통화 기록을 모모톡 타임라인에서 삭제할까요?\n(장기 기억 수정/되돌리기는 🧠 기억 노트에서 할 수 있습니다.)",
        ):
            return
        if self.memory_mgr.delete_session(base_cid, track, session_id):
            self.log(f"🗑️ [기록 삭제] 통화 세션({session_id})을 삭제했습니다.")
            self._on_memory_data_changed()

    def _render_momotalk_history_timeline(self, base_cid: str, track: str, disp_name: str, eid: str) -> None:
        if not hasattr(self, "history_box"):
            return
        b = self.memory_mgr.get_student_bucket(base_cid, track)
        sessions = [s for s in (b.get("sessions") or []) if s.get("turns")]
        tr_lbl = MEMORY_TRACK_LABELS.get(track, track)
        conf_n = len(b.get("confirmed_memories") or [])
        pend_n = len(b.get("pending_memories") or [])
        meta_parts = [f"💬 지난 모모톡 기록 ({len(sessions)}회)", tr_lbl, f"기억 {conf_n}개"]
        if pend_n > 0:
            meta_parts.append(f"⏳ 보류 {pend_n}개")
        self.lbl_history_meta.configure(text="  ·  ".join(meta_parts))

        mode = self.subtitle_mode_var.get()
        if mode not in SUBTITLE_MODES:
            mode = "jp_kr"

        self.history_box.configure(state="normal")
        self.history_box.delete("1.0", tk.END)
        self._history_img_refs = []

        for s_idx, sess in enumerate(sessions[-15:]):
            sid = str(sess.get("session_id") or "")
            s_ts = float(sess.get("started_at") or sess.get("ended_at") or 0.0)
            s_time_str = datetime.datetime.fromtimestamp(s_ts).strftime("%Y-%m-%d %H:%M") if s_ts > 0 else "이전 통화"
            s_turns = sess.get("turns") or []
            costume_lbl = str(sess.get("costume_label_kr") or "").strip()
            costume_tag = f" · {costume_lbl}" if costume_lbl else ""
            if s_idx > 0:
                self.history_box.insert(tk.END, "\n")
            self.history_box.insert(
                tk.END,
                f"  ── {s_time_str} 통화{costume_tag} ({len(s_turns)}턴)  ",
                "sess_div",
            )
            del_btn = tk.Button(
                self.history_box,
                text="삭제",
                command=lambda c=base_cid, t=track, x=sid: self._delete_history_session(c, t, x),
                bg="#FEE2E2",
                fg="#B91C1C",
                activebackground="#FECACA",
                font=("Malgun Gothic", 7, "bold"),
                relief="flat",
                padx=5,
                pady=0,
                cursor="hand2",
            )
            self.history_box.window_create(tk.END, window=del_btn)
            self.history_box.insert(tk.END, " ──  \n", "sess_div")

            s_eid = str(sess.get("entry_id") or eid)
            prev_role = ""
            for turn in s_turns:
                role = str(turn.get("role") or "")
                if role == "user":
                    raw_u = str(turn.get("raw_stt") or turn.get("kr_text") or turn.get("jp_text") or "").strip()
                    corr_u = str(turn.get("corrected_text") or "").strip()
                    shown_u = corr_u or raw_u
                    if not shown_u:
                        continue
                    if prev_role != "user":
                        self.history_box.insert(tk.END, "▸ 선생님\n", "sensei_speaker")
                    self.history_box.insert(tk.END, f" {shown_u} \n", "sensei_line")
                    if corr_u and raw_u and corr_u != raw_u:
                        self.history_box.insert(tk.END, f"✨ 문맥 교정됨 (원문: {raw_u})\n", "stt_fixed_note")
                    prev_role = "user"
                else:
                    jp = str(turn.get("jp_text") or "").strip()
                    kr = str(turn.get("kr_text") or "").strip()
                    if not jp and not kr:
                        continue
                    if prev_role != "assistant":
                        av = self._get_momotalk_photo("portrait", s_eid, 22, "#FFFFFF", "#FB7390")
                        if av is not None:
                            self._history_img_refs.append(av)
                            self.history_box.image_create(tk.END, image=av)
                            self.history_box.insert(tk.END, f" ▸ {disp_name}\n", "speaker")
                        else:
                            self.history_box.insert(tk.END, f"▸ {disp_name}\n", "speaker")
                    if mode == "jp_only":
                        self.history_box.insert(tk.END, f"{jp or kr}\n", "jp_dual_line")
                    elif mode == "kr_only":
                        self.history_box.insert(tk.END, f"{kr or jp}\n", "kr_dual_line")
                    else:
                        if jp and kr and jp == kr:
                            self.history_box.insert(tk.END, f"{jp}\n", "jp_dual_line")
                        else:
                            if jp:
                                self.history_box.insert(tk.END, f"{jp}\n", "jp_dual_line")
                            if kr:
                                self.history_box.insert(tk.END, f"{kr}\n", "kr_dual_line")
                    prev_role = "assistant"

        self.history_box.see(tk.END)
        self.history_box.configure(state="disabled")

    def _start_active_memory_session(self) -> None:
        """Open a persistent session (unless ephemeral) and capture the memory context for this call."""
        base_cid, track = self._get_active_memory_key()
        is_eph = bool(self.temporary_call_var.get())
        if is_eph:
            with self._active_memory_session_lock:
                self._active_memory_session = {
                    "base_cid": base_cid,
                    "track": track,
                    "session_id": "",
                    "ephemeral": True,
                }
            self._active_call_memory_context = ""
            self.log("🕵️ [임시 통화] 기억 주입 및 대화 기록 저장을 건너뜁니다.")
            return

        mem_ctx, has_past = self.memory_mgr.build_memory_context_block(
            base_cid=base_cid,
            track=track,
            char_data=None if base_cid == "__custom__" else self.current_char,
        )
        self._active_call_memory_context = mem_ctx
        if mem_ctx:
            counts = self.memory_mgr.get_student_memory_counts(base_cid, track)
            self.log(
                f"🧠 [기억 연동] 확정 기억 {counts['confirmed']}개 · 화제 {counts['threads']}개 · "
                f"{'이전 대화 맥락' if has_past else '공통 선생님 프로필'}을 반영합니다."
            )

        eid = getattr(self, "current_momotalk_entry_id", self.current_char_id)
        ent = self.momotalk_entry_map.get(eid) or {}
        costume_kr = str(ent.get("costume_label_kr") or "").strip() if ent.get("is_costume_variant") else ""
        sess_id = self.memory_mgr.begin_session(
            base_cid=base_cid,
            track=track,
            entry_id=base_cid if ent.get("is_costume_variant") else eid,
            costume_label_kr=costume_kr,
            reply_language=self._get_reply_language(),
        )
        with self._active_memory_session_lock:
            self._active_memory_session = {
                "base_cid": base_cid,
                "track": track,
                "session_id": sess_id,
                "ephemeral": False,
            }

    def _record_memory_turn(
        self,
        role: str,
        jp_text: str = "",
        kr_text: str = "",
        raw_stt: str = "",
        is_proactive_ping: bool = False,
    ) -> None:
        with self._active_memory_session_lock:
            sess = dict(self._active_memory_session) if self._active_memory_session else None
        if not sess or sess.get("ephemeral") or not sess.get("session_id"):
            return
        try:
            _n, should_cp = self.memory_mgr.append_turn(
                base_cid=sess["base_cid"],
                track=sess["track"],
                session_id=sess["session_id"],
                role=role,
                jp_text=jp_text,
                kr_text=kr_text,
                raw_stt=raw_stt,
                is_proactive_ping=is_proactive_ping,
            )
            if should_cp:
                self._enqueue_memory_summary(sess["base_cid"], sess["track"], sess["session_id"], is_checkpoint=True)
        except Exception as exc:
            self.log(f"[기억 기록 경고] {exc}")

    def _finalize_active_memory_session(self, run_summary: bool = True) -> None:
        with self._active_memory_session_lock:
            sess = self._active_memory_session
            self._active_memory_session = None
        if not sess or sess.get("ephemeral") or not sess.get("session_id"):
            return
        try:
            res = self.memory_mgr.end_session(
                base_cid=sess["base_cid"],
                track=sess["track"],
                session_id=sess["session_id"],
            )
            if res.get("kept"):
                self._recent_called_entry_ids = self.memory_mgr.get_recent_entry_ids()
                if not self._closing:
                    self._schedule_ui(self._on_memory_data_changed)
            if run_summary and res.get("needs_summary"):
                self._enqueue_memory_summary(sess["base_cid"], sess["track"], sess["session_id"], is_checkpoint=False)
        except Exception as exc:
            self.log(f"[기억 종료 처리 경고] {exc}")

    def _ensure_memory_summary_worker(self) -> None:
        if self._memory_summary_worker_started:
            return
        self._memory_summary_worker_started = True
        threading.Thread(
            target=self._memory_summary_worker_loop,
            daemon=True,
            name="memory-summary-worker",
        ).start()

    def _enqueue_memory_summary(self, base_cid: str, track: str, session_id: str, is_checkpoint: bool = False) -> None:
        if self._closing or not base_cid or not session_id:
            return
        key = (base_cid, track, session_id)
        with self._memory_summary_inflight_lock:
            if key in self._memory_summary_inflight:
                return
            self._memory_summary_inflight.add(key)
        self._ensure_memory_summary_worker()
        try:
            self._memory_summary_queue.put_nowait((base_cid, track, session_id, bool(is_checkpoint)))
        except queue.Full:
            with self._memory_summary_inflight_lock:
                self._memory_summary_inflight.discard(key)

    def _schedule_pending_memory_recovery_async(self) -> None:
        """Pick up any crash-interrupted unsummarized sessions gently once a Gemini backend is ready."""
        if self._memory_recovery_scheduled or self._closing:
            return
        self._memory_recovery_scheduled = True

        def _scan() -> None:
            try:
                pending = self.memory_mgr.list_pending_summary_targets()
                for bcid, tr, sid in pending[:5]:
                    self._enqueue_memory_summary(bcid, tr, sid, is_checkpoint=False)
            except Exception:
                pass

        threading.Thread(target=_scan, daemon=True, name="memory-recovery-scan").start()

    def _memory_summary_worker_loop(self) -> None:
        while not self._closing:
            try:
                base_cid, track, session_id, is_checkpoint = self._memory_summary_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            key = (base_cid, track, session_id)
            try:
                ch_data = None if base_cid == "__custom__" else self.characters.get(base_cid)
                req = self.memory_mgr.prepare_summary_request(
                    base_cid=base_cid,
                    track=track,
                    session_id=session_id,
                    char_data=ch_data,
                )
                if req is None:
                    continue
                backend = str(self.cfg.get("gemini_backend", "browser"))
                gem_key = str(self.cfg.get("gemini_api_key", ""))
                gem_model = str(self.cfg.get("gemini_api_model", "gemini-3.5-flash-lite"))
                b_model, _b_think = split_legacy_browser_model(
                    str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"),
                    str(self.cfg.get("gemini_thinking_level", "low") or "low"),
                )
                kind_lbl = "중간 체크포인트" if is_checkpoint else "통화 종료"
                self.log(f"🧠 [기억 정제 시작] {base_cid} ({kind_lbl} · 문맥 오타 교정 및 핵심 기억 추출 중)...")
                raw_out = self.gemini_mgr.run_background_task(
                    backend_mode=backend,
                    api_key=gem_key,
                    api_model=gem_model,
                    prompt=req["prompt"],
                    browser_model=b_model,
                    thinking_level="low",
                    timeout_sec=45.0,
                )
                if not raw_out:
                    continue
                parsed = parse_memory_extraction_json(raw_out)
                if parsed is None:
                    self.log("[기억 정제 경고] 요약 응답 JSON 해석에 실패해 기존 기억을 안전하게 유지합니다.")
                    continue
                stats = self.memory_mgr.apply_extraction_result(
                    base_cid=base_cid,
                    track=track,
                    session_id=session_id,
                    parsed=parsed,
                    covered_turn_count=int(req["covered_turn_count"]),
                    snapshot_reason=f"{kind_lbl} 자동 요약 전 백업",
                )
                self.log(
                    f"🧠 [기억 정제 완료] 확정 +{stats['added_confirmed']} · 승격 +{stats['promoted']} · "
                    f"보류 +{stats['added_pending']} · STT 교정 {stats['stt_repaired']}건"
                )
                if not self._closing:
                    self._schedule_ui(self._refresh_momotalk_header_and_cards)
            except Exception as exc:
                self.log(f"[기억 정제 오류] {exc}")
            finally:
                with self._memory_summary_inflight_lock:
                    self._memory_summary_inflight.discard(key)

    def _refresh_momotalk_header_and_cards(self) -> None:
        if not hasattr(self, "btn_momotalk_call"):
            return

        if not self._momotalk_char_activated:
            self.lbl_momo_hdr_avatar.configure(image="", text="")
            self.lbl_momo_hdr_name.configure(text="")
            self.lbl_momo_hdr_sub.configure(text="")
            if hasattr(self, "lbl_momo_hdr_heart"):
                self.lbl_momo_hdr_heart.pack_forget()
            if hasattr(self, "lbl_momo_card_heart"):
                self.lbl_momo_card_heart.place_forget()
            if hasattr(self, "btn_momotalk_auto_voice"):
                self.btn_momotalk_auto_voice.pack_forget()
            if hasattr(self, "_momo_card_voice_bar"):
                self._momo_card_voice_bar.pack_forget()
            self.btn_momotalk_call.configure(
                state="disabled",
                text="📞",
                bg="#E2E8F0",
                fg="#94A3B8",
            )
            if hasattr(self, "btn_momotalk_memory"):
                self.btn_momotalk_memory.configure(state="disabled", text="🧠", bg="#F1F5F9", fg="#94A3B8")
            self._momo_card_outer.place_forget()
            if hasattr(self, "_momo_history_frame"):
                self._momo_history_frame.pack_forget()
            self._momo_empty_logo_lbl.place(relx=0.5, rely=0.48, anchor="center")
            self.btn_momo_start_cta.configure(
                state="disabled",
                text="📞",
                bg="#E2E8F0",
                fg="#94A3B8",
            )
            return

        eid = self.current_momotalk_entry_id or self.current_char_id
        ent = self.momotalk_entry_map.get(eid) or {}
        ch = self.current_char
        disp_name = str(ent.get("name_kr") or ch.get("name_kr") or eid)
        status_msg = str(ent.get("status_message_kr") or "").strip()

        hdr_av = self._get_momotalk_photo("portrait", eid, 42, "#FFFFFF", "#FB7390")
        if hdr_av is not None:
            self._hdr_avatar_ref = hdr_av
            self.lbl_momo_hdr_avatar.configure(image=hdr_av)
        self.lbl_momo_hdr_name.configure(text=disp_name)
        self.lbl_momo_hdr_sub.configure(text=status_msg)
        has_voice_link = self._has_student_voice_link(eid)
        if hasattr(self, "lbl_momo_hdr_heart"):
            if has_voice_link:
                hdr_heart, _ = self._get_momotalk_heart_photo(size=18, enqueue=True)
                if hdr_heart is not None:
                    self._hdr_heart_ref = hdr_heart
                    self.lbl_momo_hdr_heart.configure(image=hdr_heart)
                    self.lbl_momo_hdr_heart.pack(side="left", padx=(6, 0))
                else:
                    self.lbl_momo_hdr_heart.pack_forget()
            else:
                self.lbl_momo_hdr_heart.pack_forget()
        if hasattr(self, "lbl_momo_card_heart"):
            if has_voice_link:
                card_heart, _ = self._get_momotalk_heart_photo(size=24, enqueue=True)
                if card_heart is not None:
                    self._card_heart_ref = card_heart
                    self.lbl_momo_card_heart.configure(image=card_heart)
                    self.lbl_momo_card_heart.place(relx=1.0, rely=0.0, anchor="ne", x=12, y=-8)
                else:
                    self.lbl_momo_card_heart.place_forget()
            else:
                self.lbl_momo_card_heart.place_forget()

        if self._call_ui_state in ("calling", "connected"):
            self.btn_momotalk_call.configure(state="normal", text="📵", bg="#EF4444", fg="#FFFFFF")
        else:
            self.btn_momotalk_call.configure(state="normal", text="📞", bg="#FB7390", fg="#FFFFFF")

        base_cid, track = self._get_active_memory_key()
        mem_counts = self.memory_mgr.get_student_memory_counts(base_cid, track)
        has_hist = mem_counts["sessions"] > 0

        if hasattr(self, "btn_momotalk_memory"):
            if mem_counts["pending"] > 0:
                self.btn_momotalk_memory.configure(
                    state="normal",
                    text=f"🧠 {mem_counts['confirmed']}+",
                    bg="#FEF3C7",
                    fg="#92400E",
                )
            elif mem_counts["confirmed"] > 0:
                self.btn_momotalk_memory.configure(
                    state="normal",
                    text=f"🧠 {mem_counts['confirmed']}",
                    bg="#FFEAF0",
                    fg="#D94B6A",
                )
            else:
                self.btn_momotalk_memory.configure(
                    state="normal",
                    text="🧠",
                    bg="#F1F5F9",
                    fg="#475569",
                )
        self._update_ephemeral_toggle_style()
        self._update_missing_voice_cta_ui()

        self._momo_empty_logo_lbl.place_forget()
        if has_hist and (not self._idle_force_profile_card) and hasattr(self, "_momo_history_frame"):
            self._momo_card_outer.place_forget()
            self._momo_history_frame.pack(fill="both", expand=True)
            self._render_momotalk_history_timeline(base_cid, track, disp_name, eid)
        else:
            if hasattr(self, "_momo_history_frame"):
                self._momo_history_frame.pack_forget()
            self._momo_card_outer.place(relx=0.5, rely=0.47, anchor="center", relwidth=0.66)
            if hasattr(self, "btn_card_back_to_history"):
                if has_hist:
                    self.btn_card_back_to_history.pack(pady=(10, 0))
                else:
                    self.btn_card_back_to_history.pack_forget()

        card_av = self._get_momotalk_photo("portrait", eid, 104, "#FFFFFF", "#FB7390")
        if card_av is not None:
            self._card_avatar_ref = card_av
            self.lbl_momo_card_avatar.configure(image=card_av)
        self.lbl_momo_card_title.configure(text=disp_name)
        self.lbl_momo_card_status.configure(text=status_msg)
        self.btn_momo_start_cta.configure(
            state="normal",
            text="📞",
            bg="#FB7390",
            fg="#FFFFFF",
        )
        if hasattr(self, "lbl_live_char_name"):
            self.lbl_live_char_name.configure(text=disp_name)
        if hasattr(self, "lbl_calling_name"):
            self.lbl_calling_name.configure(text=disp_name)
            self.lbl_calling_sub.configure(text=status_msg)

    def _format_missing_voice_cta_tooltip(self) -> str:
        if not self._momotalk_char_activated:
            return ""
        cid = self.current_char_id
        v_lang = self._voice_lang_code()
        gen_state = self._get_active_voice_gen_state(cid, v_lang)
        if gen_state is not None:
            return gen_state[1] or "목소리를 자동 생성·연결하는 중입니다..."
        from src.controller.fish_batch_builder import get_student_voice_availability

        avail = get_student_voice_availability(self.current_char, lang=v_lang, cfg=self.cfg)
        if avail == "unvoiced_npc":
            return "인게임 음성이 없는 NPC입니다. 클릭해 Fish Audio 공개 보이스를 검색·연결할 수 있습니다."
        if avail == "fallback_other_lang":
            other = "일본어(JP)" if v_lang == "KR" else "한국어(KR)"
            return f"현재 언어 음성 팩이 없어 대신 {other} 원음으로 Fish Audio 보이스를 자동 생성하고 링크를 연결합니다."
        lang_nm = "🇰🇷 한국어 더빙" if v_lang == "KR" else "🇯🇵 일본어 원음"
        return (
            f"이 학생의 {lang_nm} 목소리 링크가 없습니다.\n"
            "클릭 한 번으로 스팀 원음 추출 → Fish Audio 보이스 자동 생성 → 모모톡 링크 연결까지 한 번에 완료합니다."
        )

    def _update_missing_voice_cta_ui(self) -> None:
        if not hasattr(self, "btn_momotalk_auto_voice") or not hasattr(self, "_momo_card_voice_bar"):
            return
        if (
            not ENABLE_ON_DEMAND_VOICE_UI
            or not self._momotalk_char_activated
            or bool(self.cfg.get("use_custom_persona", False))
            or self._call_ui_state in ("calling", "connected")
        ):
            self.btn_momotalk_auto_voice.pack_forget()
            self._momo_card_voice_bar.pack_forget()
            return

        cid = self.current_char_id
        v_lang = self._voice_lang_code()
        other_lang = "JP" if v_lang == "KR" else "KR"
        gen_state = self._get_active_voice_gen_state(cid, v_lang)
        has_active_link = self._has_active_language_voice_link(cid)
        has_jp, has_kr = self._get_student_voice_link_flags(cid)

        from src.controller.fish_batch_builder import get_student_voice_availability

        avail = get_student_voice_availability(self.current_char, lang=v_lang, cfg=self.cfg)
        other_linked = has_jp if other_lang == "JP" else has_kr

        # Active generation in progress for this student -> display live progress pill & card status
        if gen_state is not None:
            stage, msg = gen_state
            stage_short = {
                "sync": "⏳ 계정 확인 중...",
                "extract": "⏳ 원음 추출 중...",
                "upload": "⏳ 보이스 생성 중...",
                "alt": "⏳ 보조 보이스 생성...",
                "link": "⏳ 링크 연결 중...",
            }.get(stage, "⏳ 생성 중...")
            self.btn_momotalk_auto_voice.configure(
                text=stage_short,
                state="disabled",
                bg="#FEF3C7",
                fg="#92400E",
            )
            if not self.btn_momotalk_auto_voice.winfo_manager():
                self.btn_momotalk_auto_voice.pack(side="right", padx=(0, 6))
            self.btn_momo_card_auto_voice.configure(
                text=stage_short,
                state="disabled",
                bg="#FEF3C7",
                fg="#92400E",
            )
            self.lbl_momo_card_voice_status.configure(text=msg, fg="#B45309")
            if not self._momo_card_voice_bar.winfo_manager():
                self._momo_card_voice_bar.pack(pady=(12, 0))
            return

        # Already linked for current language (or JP-only student in KR mode whose JP link is already set) -> hide cleanly
        if has_active_link or (avail == "fallback_other_lang" and other_linked):
            self.btn_momotalk_auto_voice.pack_forget()
            self._momo_card_voice_bar.pack_forget()
            return

        if avail == "unvoiced_npc":
            hdr_txt = "🔍 보이스 검색"
            card_txt = "🔍 Fish Audio 공개 보이스 검색 · 연결"
            hint_txt = "인게임 보이스 미실장 캐릭터예요 — 공개 보이스를 검색해 바로 연결할 수 있어요"
            bg_c, fg_c, abg_c, afg_c = "#EFF6FF", "#1D4ED8", "#DBEAFE", "#1E40AF"
        elif avail == "fallback_other_lang":
            fb_lbl = "🇯🇵 일본어 원음" if other_lang == "JP" else "🇰🇷 한국어 더빙"
            hdr_txt = f"✨ {other_lang} 원음 생성"
            card_txt = f"✨ {fb_lbl} 자동 생성 · 연결"
            hint_txt = "이 언어 더빙 팩이 없는 학생이에요 — 원음 보이스를 자동 생성해 바로 연결합니다"
            bg_c, fg_c, abg_c, afg_c = "#FFF1F4", "#D94B6A", "#FECDD3", "#BE185D"
        elif v_lang == "KR":
            hdr_txt = "✨ KR 목소리 생성"
            card_txt = "✨ 🇰🇷 한국어 목소리 자동 생성 · 연결"
            hint_txt = (
                "한국어 더빙 링크가 아직 없어요 — 버튼 한 번으로 자동 생성하고 연결합니다"
                if has_jp
                else "목소리 링크가 없어요 — 버튼 한 번으로 한국어 보이스를 자동 생성·연결합니다"
            )
            bg_c, fg_c, abg_c, afg_c = "#F0FDFA", "#0F766E", "#CCFBF1", "#115E59"
        else:
            hdr_txt = "✨ 목소리 생성"
            card_txt = "✨ 🇯🇵 일본어 목소리 자동 생성 · 연결"
            hint_txt = "목소리 링크가 없어요 — 버튼 한 번으로 자동 생성하고 모모톡에 바로 연결합니다"
            bg_c, fg_c, abg_c, afg_c = "#FFF1F4", "#D94B6A", "#FECDD3", "#BE185D"

        self.btn_momotalk_auto_voice.configure(
            text=hdr_txt,
            state="normal",
            bg=bg_c,
            fg=fg_c,
            activebackground=abg_c,
            activeforeground=afg_c,
        )
        if not self.btn_momotalk_auto_voice.winfo_manager():
            self.btn_momotalk_auto_voice.pack(side="right", padx=(0, 6))

        self.btn_momo_card_auto_voice.configure(
            text=card_txt,
            state="normal",
            bg=bg_c,
            fg=fg_c,
            activebackground=abg_c,
            activeforeground=afg_c,
        )
        self.lbl_momo_card_voice_status.configure(text=hint_txt, fg="#64748B")
        if not self._momo_card_voice_bar.winfo_manager():
            self._momo_card_voice_bar.pack(pady=(12, 0))

    def _on_click_quick_auto_voice(self) -> None:
        """1-click main screen CTA: auto-generate & link voice, or open search for unvoiced NPCs."""
        if not ENABLE_ON_DEMAND_VOICE_UI or not self._momotalk_char_activated:
            return
        cid = self.current_char_id
        v_lang = self._voice_lang_code()
        other_lang = "JP" if v_lang == "KR" else "KR"
        from src.controller.fish_batch_builder import get_student_voice_availability

        avail = get_student_voice_availability(self.current_char, lang=v_lang, cfg=self.cfg)
        if avail == "unvoiced_npc":
            self.open_voice_search_dialog()
            return
        target_lang = other_lang if avail == "fallback_other_lang" else v_lang
        self.auto_generate_current_student_voice_async(target_cid=cid, target_lang=target_lang)

    def select_momotalk_character(self, entry_or_cid: str) -> None:
        ent = self.momotalk_entry_map.get(entry_or_cid)
        if ent is not None:
            eid = ent["entry_id"]
            bcid = ent["base_cid"]
        elif entry_or_cid in self.characters:
            eid = entry_or_cid
            bcid = entry_or_cid
        else:
            return
        if bcid not in self.characters:
            return
        prev_eid = getattr(self, "current_momotalk_entry_id", "")
        self._momotalk_char_activated = True
        self._idle_force_profile_card = False
        self.current_momotalk_entry_id = eid
        self.cfg["selected_momotalk_entry_id"] = eid
        self._apply_selected_character(bcid)
        if prev_eid != eid:
            self._on_active_persona_potentially_changed(f"모모톡 선택: {ent.get('name_kr', eid) if ent else eid}")
        self._sync_roster_selection_visual()
        if self._call_ui_state == "calling":
            self._redraw_calling_canvas()

    def _on_live_nudge_clicked(self) -> None:
        if self.companion_running:
            self.trigger_manual_companion_ping_async()
        elif self.running:
            self.trigger_manual_nudge_async()

    def toggle_momotalk_call(self) -> None:
        if self._call_ui_state in ("calling", "connected") or self.companion_running or self.running:
            self.stop_momotalk_call()
        else:
            self.start_momotalk_call()

    def start_momotalk_call(self) -> None:
        if not self._momotalk_char_activated:
            messagebox.showinfo("학생 선택 필요", "먼저 왼쪽 모모톡 목록에서 대화할 학생을 눌러 주세요.")
            return
        if self.companion_running or self.running or self._call_ui_state in ("calling", "connected"):
            return

        # Pre-check credentials & Whisper before starting ringback/calling screen
        if not str(self.cfg.get("fish_api_key", "")).strip():
            messagebox.showwarning("Fish Audio API Key 필요", "먼저 ⚙️ 설정 창에서 Fish Audio API Key를 입력해 주세요.")
            self.open_settings_dialog(first_run=True, initial_tab=1)
            return

        bridge_mode = str(self.cfg.get("bridge_mode", "pc_mic") or "pc_mic")
        if bridge_mode != "gemini_live_audio":
            backend = str(self.cfg.get("gemini_backend", "browser"))
            if backend == "api" and not str(self.cfg.get("gemini_api_key", "")).strip():
                messagebox.showwarning("Gemini API Key 필요", "먼저 ⚙️ 설정 창에서 Gemini API Key를 입력하거나 전용 브라우저 모드로 전환해 주세요.")
                self.open_settings_dialog(first_run=False, initial_tab=1)
                return

        if self.whisper_model is None:
            messagebox.showinfo("잠시만 기다려 주세요", "로컬 Whisper 음성인식 모델이 아직 초기화 중입니다. 2~3초 뒤 다시 눌러 주세요.")
            return

        # Guard if this student's voice is currently being auto-generated, or offer 1-click auto-create when missing
        if ENABLE_ON_DEMAND_VOICE_UI and not bool(self.cfg.get("use_custom_persona", False)):
            cid_chk = self.current_char_id
            v_lang_chk = self._voice_lang_code()
            gen_st = self._get_active_voice_gen_state(cid_chk, v_lang_chk)
            if gen_st is not None:
                messagebox.showinfo(
                    "목소리 생성 진행 중",
                    f"지금 [{self.current_char.get('name_kr', cid_chk)}]의 목소리를 자동 생성하고 있습니다.\n"
                    "잠시만 기다려 주시면 완료 즉시 통화할 수 있어요!",
                )
                return

            has_active_link = self._has_active_language_voice_link(cid_chk)
            has_any_link = self._has_student_voice_link(cid_chk)
            # In KR mode, if JP link already exists, Fish Audio can speak Korean via JP model without blocking.
            # Prompt only when there is NO usable voice link at all for this student!
            if not has_active_link and not (v_lang_chk == "KR" and has_any_link):
                from src.controller.fish_batch_builder import get_student_voice_availability

                avail = get_student_voice_availability(self.current_char, lang=v_lang_chk, cfg=self.cfg)
                cname_chk = str(self.current_char.get("name_kr") or cid_chk)
                other_lang_chk = "JP" if v_lang_chk == "KR" else "KR"
                if avail == "unvoiced_npc":
                    open_srch = messagebox.askyesno(
                        "목소리 링크 없음 (미실장 NPC)",
                        f"[{cname_chk}]은(는) 인게임 음성이 미실장된 NPC이며 연결된 목소리 링크가 없습니다.\n\n"
                        "지금 Fish Audio 공개 보이스 검색기를 열어 목소리를 연결할까요?",
                    )
                    if open_srch:
                        self.open_voice_search_dialog()
                    return
                target_gen_lang = other_lang_chk if avail == "fallback_other_lang" else v_lang_chk
                lang_label = "한국어 더빙" if target_gen_lang == "KR" else "일본어 원음"
                choice = messagebox.askyesnocancel(
                    "목소리 링크 없음 — 1클릭 자동 생성",
                    f"[{cname_chk}]의 목소리 링크가 아직 연결되지 않았습니다.\n\n"
                    f"• [예(Y)] 지금 바로 {lang_label} 목소리를 자동 생성·연결하고 통화 시작 (권장)\n"
                    "• [아니오(N)] 링크 없이 로컬 Zero-Shot으로 바로 통화 시작\n"
                    "• [취소] 돌아가기",
                )
                if choice is None:
                    return
                if choice is True:
                    self.auto_generate_current_student_voice_async(
                        target_cid=cid_chk,
                        target_lang=target_gen_lang,
                        start_call_after=True,
                    )
                    return

        self._mode2_capture_ready = False
        self._calling_login_required = False
        eid = getattr(self, "current_momotalk_entry_id", self.current_char_id)
        ent_rec = self.momotalk_entry_map.get(eid)
        if ent_rec is not None and ent_rec.get("is_costume_variant"):
            bcid_rec = str(ent_rec.get("base_cid") or self.current_char_id)
            if bcid_rec in self.momotalk_entry_map:
                eid = bcid_rec
        if not bool(self.temporary_call_var.get()):
            if eid and eid not in self._recent_called_entry_ids:
                self._recent_called_entry_ids.insert(0, eid)
            elif eid in self._recent_called_entry_ids:
                self._recent_called_entry_ids.remove(eid)
                self._recent_called_entry_ids.insert(0, eid)
            self.memory_mgr.record_recent_call(
                base_cid=self.current_char_id,
                entry_id=eid,
                increment_calls=False,
            )
        self._ringback_state.start()
        self._set_call_ui_state("calling")

        if bridge_mode == "gemini_live_audio":
            self.toggle_bridge()
        else:
            self.toggle_companion_mode()

        if not self.companion_running and not self.running:
            self._ringback_state.stop()
            self._set_call_ui_state("idle")

    def stop_momotalk_call(self) -> None:
        self._ringback_state.stop()
        if self.companion_running:
            self.toggle_companion_mode()
        if self.running:
            self.toggle_bridge()
        self._calling_login_required = False
        self._set_call_ui_state("idle")

    def _set_calling_login_required(self, required: bool) -> None:
        self._calling_login_required = bool(required)
        if not hasattr(self, "btn_hdr_login_confirm"):
            return
        if self._calling_login_required:
            try:
                self.btn_hdr_login_confirm.pack(side="right", padx=(6, 0), before=self.btn_hdr_settings)
            except Exception:
                pass
            if hasattr(self, "_calling_login_bar"):
                self._calling_login_bar.pack(pady=(6, 4))
            if hasattr(self, "lbl_calling_status"):
                self.lbl_calling_status.configure(text="🔑 로그인 대기 중...", fg="#FDE68A")
        else:
            try:
                self.btn_hdr_login_confirm.pack_forget()
            except Exception:
                pass
            if hasattr(self, "_calling_login_bar"):
                self._calling_login_bar.pack_forget()
            if hasattr(self, "lbl_calling_status") and self._call_ui_state == "calling":
                self.lbl_calling_status.configure(text="통화 연결 중...", fg="#FDA4AF")

    def _set_call_ui_state(self, state: str) -> None:
        if state not in ("idle", "calling", "connected"):
            state = "idle"
        with self._call_state_lock:
            self._call_ui_state = state
            now = time.monotonic()
            if state == "calling":
                self._call_started_mono = now
            elif state == "connected":
                self._call_connected_mono = now

        # Cancel timers if leaving active call state
        if state == "idle":
            if self._call_anim_after_id is not None:
                try:
                    self.root.after_cancel(self._call_anim_after_id)
                except Exception:
                    pass
                self._call_anim_after_id = None
            if self._call_timer_after_id is not None:
                try:
                    self.root.after_cancel(self._call_timer_after_id)
                except Exception:
                    pass
                self._call_timer_after_id = None
            self._set_calling_login_required(False)

        if not hasattr(self, "_momo_center_stack"):
            return

        for frame in (self._momo_idle_frame, self._momo_calling_frame, self._momo_connected_bar, self._momo_subtitle_host):
            try:
                frame.pack_forget()
            except Exception:
                pass

        if state == "idle":
            self._momo_idle_frame.pack(fill="both", expand=True)
        elif state == "calling":
            self._momo_calling_frame.pack(fill="both", expand=True)
            self._call_anim_phase = 0.0
            self.lbl_calling_timer.configure(text="00:00")
            self.lbl_calling_status.configure(
                text="🔑 로그인 대기 중..." if self._calling_login_required else "통화 연결 중...",
                fg="#FDE68A" if self._calling_login_required else "#FDA4AF",
            )
            self._redraw_calling_canvas()
            self._animate_calling_canvas()
            self._tick_call_timer()
        else:
            if self._call_anim_after_id is not None:
                try:
                    self.root.after_cancel(self._call_anim_after_id)
                except Exception:
                    pass
                self._call_anim_after_id = None
            self._set_calling_login_required(False)
            self._momo_connected_bar.pack(fill="x")
            self._momo_subtitle_host.pack(fill="both", expand=True)
            self.lbl_live_call_timer.configure(text="00:00")
            self._rerender_subtitles()
            self._tick_call_timer()

        self._refresh_momotalk_header_and_cards()

    def _on_call_connected_ui(self) -> None:
        with self._call_state_lock:
            if self._call_ui_state != "calling":
                return
        if not (self.companion_running or self.running):
            return
        self._set_call_ui_state("connected")
        cname = self.current_char.get("name_kr", self.current_char_id)
        self.log(f"📞 [통화 연결 완료] '{cname}'과(와) 실시간 보이스 통화 및 대사 자막 표시를 시작합니다.")

    def _complete_call_connection_from_audio_thread(self, stream: Any, play_sr: int) -> None:
        should_chime = False
        with self._call_state_lock:
            if self._call_ui_state == "calling":
                self._call_ui_state = "connected"
                self._call_connected_mono = time.monotonic()
                should_chime = True
        self._ringback_state.stop()
        if should_chime:
            if stream is not None:
                try:
                    chime = MomoTalkCallAudioFX.build_connect_pickup_chime(play_sr, volume=0.25)
                    vol = float(self.vol_var.get() or 1.0)
                    if vol != 1.0:
                        chime = np.clip(chime * vol, -0.95, 0.95).astype(np.float32)
                    stream.write(chime)
                except Exception:
                    pass
            # Run UI transition cleanly (note _call_ui_state is now "connected", so apply UI switch directly)
            def _apply_connected() -> None:
                if not (self.companion_running or self.running):
                    self._set_call_ui_state("idle")
                    return
                self._set_call_ui_state("connected")
                cname = self.current_char.get("name_kr", self.current_char_id)
                self.log(f"📞 [통화 연결 완료] '{cname}'과(와) 실시간 보이스 통화 및 자막을 시작합니다.")

            self._schedule_ui(_apply_connected)

    def _redraw_calling_canvas(self) -> None:
        if not hasattr(self, "calling_canvas"):
            return
        cv = self.calling_canvas
        cx, cy = 120, 100
        items = self._calling_canvas_items
        rings = items.get("rings")
        if not rings:
            cv.delete("all")
            rings = [cv.create_oval(cx - 58, cy - 58, cx + 58, cy + 58, outline="#FB7390", width=3) for _ in range(3)]
            items["rings"] = rings
            items["border"] = cv.create_oval(cx - 60, cy - 60, cx + 60, cy + 60, outline="#FB7390", width=2)
            items["portrait_item"] = cv.create_image(cx, cy)
            items["portrait_eid"] = None

        # Update 3 animated expanding concentric pulse rings in-place (zero canvas item churn)
        for k, ring_id in enumerate(rings):
            prog = (self._call_anim_phase + k / 3.0) % 1.0
            r = 58 + prog * 54
            col = "#FB7390" if prog < 0.35 else ("#F472B6" if prog < 0.70 else "#475569")
            width = max(1, int(round(3.5 * (1.0 - prog))))
            cv.coords(ring_id, cx - r, cy - r, cx + r, cy + r)
            cv.itemconfigure(ring_id, outline=col, width=width)

        cur_eid = getattr(self, "current_momotalk_entry_id", "") or self.current_char_id
        portrait, is_final = self._resolve_momotalk_photo("portrait", cur_eid, 114, "#263040", "#FB7390", enqueue=True)
        if portrait is not None and (
            items.get("portrait_eid") != cur_eid
            or (is_final and not items.get("portrait_final", False))
            or portrait is not self._calling_portrait_ref
        ):
            self._calling_portrait_ref = portrait
            items["portrait_eid"] = cur_eid
            items["portrait_final"] = is_final
            cv.itemconfigure(items["portrait_item"], image=portrait)

    def _animate_calling_canvas(self) -> None:
        if self._call_ui_state != "calling" or getattr(self, "_closing", False):
            self._call_anim_after_id = None
            return
        self._call_anim_phase = (self._call_anim_phase + 0.035) % 1.0
        self._redraw_calling_canvas()
        try:
            self._call_anim_after_id = self.root.after(55, self._animate_calling_canvas)
        except Exception:
            self._call_anim_after_id = None

    def _tick_call_timer(self) -> None:
        state = self._call_ui_state
        if state not in ("calling", "connected") or getattr(self, "_closing", False):
            self._call_timer_after_id = None
            self._last_call_timer_txt = ""
            return
        ref = self._call_started_mono if state == "calling" else self._call_connected_mono
        secs = max(0, int(time.monotonic() - ref))
        mm, ss = divmod(secs, 60)
        txt = f"{mm:02d}:{ss:02d}"
        cache_key = f"{state}:{txt}"
        try:
            if cache_key != self._last_call_timer_txt:
                self._last_call_timer_txt = cache_key
                if state == "calling" and hasattr(self, "lbl_calling_timer"):
                    self.lbl_calling_timer.configure(text=txt)
                elif state == "connected" and hasattr(self, "lbl_live_call_timer"):
                    self.lbl_live_call_timer.configure(text=txt)
            self._call_timer_after_id = self.root.after(250, self._tick_call_timer)
        except Exception:
            self._call_timer_after_id = None

    def reload_characters_from_disk(self) -> None:
        self.characters = load_all_character_configs()
        self.momotalk_entries, self.momotalk_entry_map = load_momotalk_profiles(self.characters)
        self.subtitle_translator.update_characters(self.characters)
        schools = sorted({c.get("school", "기타") for c in self.characters.values() if c.get("school")})
        vals = ["전체 학원", PATCH15_FILTER_LABEL] + schools
        self.cmb_school.config(values=vals)
        self._refresh_character_filter_list(keep_current=True)
        self._on_active_persona_potentially_changed("페르소나 새로고침")
        self.log(f"[새로고침 완료] config/characters 폴더에서 총 {len(self.characters)}명의 캐릭터 페르소나를 불러왔습니다.")

    def _ensure_search_indices(self) -> None:
        entries = getattr(self, "momotalk_entries", [])
        sig = (id(self.characters), len(self.characters), id(entries), len(entries))
        if sig == self._search_index_sig:
            return
        self._search_index_sig = sig

        char_cache: list[tuple[str, str, bool, str, str]] = []
        for cid, c in self.characters.items():
            patch15 = is_patch15_character(c)
            school = str(c.get("school", ""))
            haystack = " ".join(
                (
                    cid,
                    str(c.get("name_kr", "")),
                    str(c.get("name_jp", "")),
                    str(c.get("ruby_jp", "")),
                    school,
                    str(c.get("club", "")),
                )
            ).lower()
            tag_patch = "⭐ " if patch15 else ""
            tag_npc = " [NPC]" if c.get("category") == "npc" else ""
            disp = f"{tag_patch}{c.get('name_kr', cid)} ({c.get('name_jp', '')}) — {school}{tag_npc}"
            char_cache.append((cid, school, patch15, haystack, disp))
        self._char_search_cache = char_cache

        momo_rows: list[tuple[dict[str, Any], str, str]] = []
        seen_base_cids: set[str] = set()
        for ent in entries:
            if ent.get("is_costume_variant"):
                continue
            eid = ent["entry_id"]
            bcid = ent["base_cid"]
            if bcid in seen_base_cids:
                continue
            seen_base_cids.add(bcid)
            ch = self.characters.get(bcid) or {}
            hay = " ".join(
                (
                    eid,
                    bcid,
                    str(ent.get("name_kr") or ""),
                    str(ent.get("full_name_kr") or ""),
                    str(ent.get("status_message_kr") or ""),
                    str(ch.get("name_jp") or ""),
                    str(ch.get("ruby_jp") or ""),
                )
            ).lower()
            momo_rows.append((ent, eid, hay))

        self._momotalk_search_default = sorted(
            momo_rows,
            key=lambda row: (
                int(row[0].get("default_order", 9999)),
                0 if not row[0].get("is_costume_variant") else 1,
                str(row[0].get("name_kr") or ""),
            ),
        )
        self._momotalk_search_name = sorted(
            momo_rows,
            key=lambda row: (str(row[0].get("name_kr") or ""), row[1]),
        )

    def _refresh_character_filter_list(self, keep_current: bool = True) -> None:
        self._ensure_search_indices()
        sch = self.school_filter_var.get()
        q = self.search_var.get().strip().lower()
        nav_tab = getattr(self, "_momotalk_nav_tab", "all")
        sort_mode = getattr(self, "_momotalk_sort_mode", "default")

        # 1. Maintain off-screen legacy character combo (one base persona per character)
        self.display_to_cid.clear()
        cid_to_disp: dict[str, str] = {}
        disp_list: list[str] = []
        matched_base_cids: list[str] = []
        is_patch_filter = sch == PATCH15_FILTER_LABEL
        is_all_school = sch == "전체 학원"
        for cid, school, patch15, haystack, disp in self._char_search_cache:
            if is_patch_filter:
                if not patch15:
                    continue
            elif not is_all_school and school != sch:
                continue
            if q and q not in haystack:
                continue
            self.display_to_cid[disp] = cid
            cid_to_disp[cid] = disp
            disp_list.append(disp)
            matched_base_cids.append(cid)
        self.cmb_char.config(values=disp_list)

        # 2. Filter & sort MomoTalk roster entries using precomputed sorted indices
        if nav_tab == "recent":
            recent_ids = list(getattr(self, "_recent_called_entry_ids", []) or [])
            recent_pos = {eid: idx for idx, eid in enumerate(recent_ids)}
            matched_recent = [
                (recent_pos[eid], eid)
                for _ent, eid, hay in self._momotalk_search_default
                if eid in recent_pos and (not q or q in hay)
            ]
            matched_recent.sort()
            filtered_eids = [eid for _, eid in matched_recent]
        else:
            source_rows = self._momotalk_search_name if sort_mode == "name" else self._momotalk_search_default
            filtered_eids = [eid for _ent, eid, hay in source_rows if not q or q in hay]
            if sort_mode == "heart":
                filtered_eids.sort(key=lambda eid: 0 if self._has_student_voice_link(eid) else 1)

        self._momotalk_filtered_cids = filtered_eids
        self._last_scrollregion = (0, 0)
        if hasattr(self, "lbl_roster_count"):
            hdr_lbl = "최근 통화" if nav_tab == "recent" else "학생"
            self.lbl_roster_count.configure(text=f"{hdr_lbl} ({len(filtered_eids)})")

        if not matched_base_cids:
            self.char_display_var.set("검색 결과 없음")
        else:
            target_cid = self.current_char_id if self.current_char_id in cid_to_disp else matched_base_cids[0]
            self.char_display_var.set(cid_to_disp[target_cid])
            self._apply_selected_character(target_cid)
        self._render_roster_visible(force_all=True)

    def _on_char_combo_selected(self, _event=None) -> None:
        cid = self.display_to_cid.get(self.char_display_var.get())
        if cid:
            self.select_momotalk_character(cid)

    def _apply_selected_character(self, cid: str) -> None:
        prev_cid = self.current_char_id
        # Save previous character's voice ID if non-empty
        prev_voice = extract_model_id(self.ref_id_var.get())
        if prev_cid and prev_voice:
            self._set_saved_voice_id(prev_cid, prev_voice)

        self.current_char_id = cid
        cur_ent = self.momotalk_entry_map.get(getattr(self, "current_momotalk_entry_id", ""))
        if cur_ent is None or cur_ent.get("base_cid") != cid:
            self.current_momotalk_entry_id = cid
        self._sticky_ja_hits = 0
        self.cfg["selected_character_id"] = cid
        self.cfg["selected_momotalk_entry_id"] = self.current_momotalk_entry_id
        c = self.current_char
        persona = c.get("persona") or {}
        sm = persona.get("speech_mechanics_jp") or {}

        saved_voice = self._get_saved_voice_id(cid)
        self.ref_id_var.set(saved_voice)
        self.cfg["fish_reference_id"] = saved_voice
        if getattr(self, "_ui_boot_complete", False):
            save_bridge_config(self.cfg)

        fp = sm.get("first_person") or "私"
        s_addr = (c.get("user_title") or "선생님") if self._get_reply_language() == "ko" else (sm.get("sensei_address") or c.get("user_title_jp") or "先生")
        v_lang = self._voice_lang_code()
        cv_name = ((c.get("voice_variants") or {}).get(v_lang) or {}).get("voice_actor") or (
            ((c.get("voice_variants") or {}).get("JP") or {}).get("voice_actor") or "보이스 미실장/NPC"
        )
        voiced_packs = len(((c.get("local_voice_packs") or {}).get(v_lang.lower()) or []))
        pack_tag = (
            f"스팀 음성팩 {voiced_packs}개"
            if (voiced_packs and v_lang == "JP")
            else (
                f"스팀 KR 음성팩 {voiced_packs}개"
                if voiced_packs
                else ("스팀 보이스 보유" if c.get("molru_prefix") and c.get("category") != "npc" else "보이스 미실장 NPC")
            )
        )

        self.lbl_char_badge.config(
            text=f"🎓 [{c.get('school', '')} · {c.get('club', '')}] {c.get('name_kr', cid)} ({c.get('name_jp', '')})  |  CV({v_lang}): {cv_name} ({pack_tag})  |  1인칭: {fp}  |  선생님 호칭: {s_addr}"
        )
        vap = persona.get("voice_acting_profile") or {}
        if vap:
            arch_kr = vap.get("archetype_kr", "표준형")
            b_t = vap.get("base_temperature", 0.72)
            s_t = vap.get("spike_temperature", 0.84)
            d_tag = vap.get("default_s2_tag", "")
            sp_tag = vap.get("spike_directing", "")
            wh_tag = vap.get("whisper_directing", "")
            open_tics = " / ".join((vap.get("opening_reactions_jp") or [])[:3])
            forb = vap.get("forbidden_s2_tags") or []
            guard_note = f" (OOC 차단: {', '.join(forb)})" if forb else ""
            if sp_tag or wh_tag:
                tone_summary = f"기본: {d_tag} · 급변: {sp_tag} · 속삭임: {wh_tag}"
            else:
                tone_summary = f"기본 톤: {d_tag}  |  첫 호흡 리액션: {open_tics}"
            self.lbl_acting_badge.config(
                text=f"🎭 연기 프로파일: [{arch_kr}] temp {b_t:.2f}→{s_t:.2f}{guard_note}  |  {tone_summary}"
            )
        else:
            self.lbl_acting_badge.config(text="")
        self._refresh_persona_summary_ui()
        self.btn_test_tts.config(text=f"🔊 [{c.get('name_kr', cid)}] 시그니처 대사 테스트")
        if not self.running:
            self.btn_toggle.config(text=f"▶️ 실시간 [{c.get('name_kr', cid)}] 음성 브릿지 시작")
        self.mic_stt.set_character_context(c)
        self._refresh_momotalk_header_and_cards()
        if prev_cid != cid:
            self._on_active_persona_potentially_changed(f"캐릭터 변경: {c.get('name_kr', cid)}")

    def _on_persona_variant_changed(self, _event=None) -> None:
        self._refresh_persona_summary_ui()
        self.save_settings_ui(silent=True)
        self._refresh_momotalk_header_and_cards()
        self._on_active_persona_potentially_changed("페르소나 해석 모드 변경")

    def _update_reply_language_toggle_style(self) -> None:
        if not hasattr(self, "btn_hdr_lang_ja") or not hasattr(self, "btn_hdr_lang_ko"):
            return
        is_ko = self._get_reply_language() == "ko"
        active_style = {
            "bg": "#FFFFFF",
            "fg": "#D94B6A",
            "activebackground": "#FFEAF0",
            "activeforeground": "#BE185D",
            "font": ("Malgun Gothic", 9, "bold"),
        }
        inactive_style = {
            "bg": "#E45C7A",
            "fg": "#FFF1F4",
            "activebackground": "#D94B6A",
            "activeforeground": "#FFFFFF",
            "font": ("Malgun Gothic", 9),
        }
        self.btn_hdr_lang_ja.configure(**(inactive_style if is_ko else active_style))
        self.btn_hdr_lang_ko.configure(**(active_style if is_ko else inactive_style))

    def set_reply_language(self, lang_code: str) -> None:
        new_lang = "ko" if str(lang_code or "").strip().lower() == "ko" else "ja"
        prev_lang = self._get_reply_language()
        if new_lang == prev_lang and self.cfg.get("reply_language") == new_lang:
            self._update_reply_language_toggle_style()
            return
        # Save current character's voice ID under the previous language before switching
        prev_voice = extract_model_id(self.ref_id_var.get())
        if self.current_char_id and prev_voice:
            self._set_saved_voice_id(self.current_char_id, prev_voice, reply_lang=prev_lang)

        self.reply_language_var.set(new_lang)
        self.cfg["reply_language"] = new_lang
        coach_turned_off = False
        if new_lang == "ko" and bool(self.jp_beginner_coach_var.get()):
            self.jp_beginner_coach_var.set(False)
            self.cfg["jp_beginner_lover_mode"] = False
            self._update_jp_coach_toggle_style()
            coach_turned_off = True

        new_voice = self._get_saved_voice_id(self.current_char_id, reply_lang=new_lang)
        self.ref_id_var.set(new_voice)
        self.cfg["fish_reference_id"] = new_voice
        self._update_reply_language_toggle_style()
        self._apply_selected_character(self.current_char_id)
        self.save_settings_ui(silent=True)
        self._sync_mic_stt_config()

        if new_lang == "ko":
            extra = " (일본어 코칭은 자동 해제되었습니다)" if coach_turned_off else ""
            self.log(f"🇰🇷 [답변 언어: 한국어] 학생들이 캐릭터 고유 말투를 유지한 자연스러운 한국어로 대답합니다.{extra}")
        else:
            self.log("🇯🇵 [답변 언어: 일본어] 학생들이 원작 일본어 구어체로 대답합니다.")
        self._on_active_persona_potentially_changed(f"답변 언어 변경: {'한국어' if new_lang == 'ko' else '일본어'}")

    def _update_jp_coach_toggle_style(self) -> None:
        if not hasattr(self, "btn_hdr_jp_coach") or self.btn_hdr_jp_coach is None:
            return
        active = bool(self.jp_beginner_coach_var.get())
        if active:
            self.btn_hdr_jp_coach.configure(
                text="💖 일본어 코칭 ON",
                bg="#FFFFFF",
                fg="#D94B6A",
                activebackground="#FFEAF0",
                activeforeground="#BE185D",
                font=("Malgun Gothic", 9, "bold"),
            )
        else:
            self.btn_hdr_jp_coach.configure(
                text="🌱 일본어 코칭 OFF",
                bg="#E45C7A",
                fg="#FFF1F4",
                activebackground="#D94B6A",
                activeforeground="#FFFFFF",
                font=("Malgun Gothic", 9, "bold"),
            )

    def _sync_mic_stt_config(self) -> None:
        coach_on = bool(self.jp_beginner_coach_var.get()) and self._get_reply_language() == "ja"
        lang_cfg = str(self.cfg.get("stt_language", "auto") or "auto").strip()
        eff_lang = lang_cfg if lang_cfg in ("ja", "ko") else ("ja_beginner_bi" if coach_on else "ko_auto")
        base_sil = float(self.cfg.get("mic_silence_end_sec", 0.68) or 0.68)
        eff_sil = max(base_sil, 1.08) if coach_on else base_sil
        self.mic_stt.configure(
            rms_threshold=float(self.cfg.get("mic_vad_rms_threshold", 0.010)),
            silence_end_sec=eff_sil,
            stt_language=eff_lang,
        )

    def toggle_jp_beginner_coach_mode(self) -> None:
        new_val = not bool(self.jp_beginner_coach_var.get())
        if new_val and self._get_reply_language() == "ko":
            prev_voice = extract_model_id(self.ref_id_var.get())
            if self.current_char_id and prev_voice:
                self._set_saved_voice_id(self.current_char_id, prev_voice, reply_lang="ko")
            self.reply_language_var.set("ja")
            self.cfg["reply_language"] = "ja"
            ja_voice = self._get_saved_voice_id(self.current_char_id, reply_lang="ja")
            self.ref_id_var.set(ja_voice)
            self.cfg["fish_reference_id"] = ja_voice
            self._update_reply_language_toggle_style()
            self.log("🇯🇵 [일본어 코칭 연동] 답변 언어를 자동으로 일본어로 전환했습니다.")
        self.jp_beginner_coach_var.set(new_val)
        self.cfg["jp_beginner_lover_mode"] = new_val
        self._update_jp_coach_toggle_style()
        self.save_settings_ui(silent=True)
        self._sync_mic_stt_config()
        if new_val:
            self.log("🌸 [일본어 코칭 ON] 캐릭터 페르소나는 유지한 채, 일본어가 서툰 상대에게 맞춰 쉬운 일상 일본어로 자연스럽게 배려하는 모드가 활성화되었습니다(선생님식 교정 없음).")
        else:
            self.log("🌱 [일본어 코칭 OFF] 기본 캐릭터 회화 모드로 복귀했습니다.")
        self._on_active_persona_potentially_changed("서툰 일본어 배려 모드 ON" if new_val else "서툰 일본어 배려 모드 OFF")

    def _refresh_persona_summary_ui(self) -> None:
        c = self.current_char
        persona = c.get("persona") or {}
        fanon = persona.get("fanon_dc_ba_gallery") or {}
        vcode = PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")
        if vcode == "official" or not fanon:
            self.lbl_char_summary.config(
                text=f"📘 [공식 원작 정사] {persona.get('summary', '')}",
                fg="#334155",
            )
        else:
            f_sum = fanon.get("summary_kr", persona.get("summary", ""))
            if len(f_sum) > 195:
                f_sum = f_sum[:192] + "..."
            if vcode == "fanon_optimal_blend":
                blend_kr = fanon.get("optimal_blend_ratio_kr") or fanon.get("mainstream_tendency_kr", "캐릭터 맞춤 최적 배합")
                self.lbl_char_summary.config(
                    text=f"🌟 [프롬프트 2-D 최적배합 · {blend_kr}] {f_sum}",
                    fg="#0f766e",
                )
            else:
                tendency = fanon.get("mainstream_tendency_kr", "2차창작 인기 해석")
                color_map = {
                    "fanon_mainstream": "#4338ca",
                    "fanon_pure_love": "#be185d",
                    "fanon_spicy_meme": "#b45309",
                }
                self.lbl_char_summary.config(
                    text=f"🔥 [2차창작 · {tendency}] {f_sum}",
                    fg=color_map.get(vcode, "#4338ca"),
                )

    def _update_subtitle_mode_buttons_style(self) -> None:
        cur = self.subtitle_mode_var.get()
        for code, btn in self._subtitle_mode_btns.items():
            if code == cur:
                btn.config(bg="#FFEAF0", fg="#D94B6A", font=("Malgun Gothic", 9, "bold"))
            else:
                btn.config(bg="#F1F5F9", fg="#64748B", font=("Malgun Gothic", 9))

    def _on_subtitle_mode_changed(self) -> None:
        self._update_subtitle_mode_buttons_style()
        self._rerender_subtitles()
        if self._call_ui_state == "idle":
            self._refresh_momotalk_header_and_cards()
        self.save_settings_ui(silent=True)

    def clear_subtitles(self) -> None:
        self._subtitle_entries.clear()
        self._rerender_subtitles()

    def toggle_setup_panels_collapse(self) -> None:
        self._setup_panels_collapsed = not self._setup_panels_collapsed
        if self._setup_panels_collapsed:
            for pnl in (self.f1, self.f2):
                if pnl is not None:
                    pnl.pack_forget()
            if self.btn_toggle_setup is not None:
                self.btn_toggle_setup.config(text="🔽 초기설정 펼치기")
        else:
            if self.f1 is not None and self.f0 is not None:
                self.f1.pack(fill="x", padx=16, pady=2, after=self.f0)
            if self.f2 is not None and self.f1 is not None:
                self.f2.pack(fill="x", padx=16, pady=2, after=self.f1)
            if self.btn_toggle_setup is not None:
                self.btn_toggle_setup.config(text="🔼 초기설정 접기")

    def _active_web_models(self) -> list[tuple[str, str]]:
        if self._account_web_models:
            seen = {c for c, _ in self._account_web_models}
            extra = [(c, l) for c, l in self._extra_web_models if c not in seen]
            return list(self._account_web_models) + extra
        return list(GEMINI_BROWSER_MODELS) + [
            (c, l) for c, l in self._extra_web_models if c not in GEMINI_BROWSER_MODEL_LABELS
        ]

    def _subtitle_choices(self) -> list[tuple[str, str]]:
        cur_sub = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
        extra_api = list(self._extra_api_models)
        if cur_sub.startswith("api:"):
            suffix = cur_sub[4:].strip()
            if suffix and suffix not in extra_api:
                extra_api.append(suffix)
        extra_web = list(self._extra_web_models)
        if cur_sub.startswith("web:"):
            suffix = cur_sub[4:].strip()
            active_web_codes = {c for c, _ in self._active_web_models()}
            if suffix and suffix not in active_web_codes and all(c != suffix for c, _ in extra_web):
                extra_web.append((suffix, f"🌐 웹 제미니: {suffix}"))
        return get_subtitle_engine_choices(
            extra_api_models=extra_api,
            extra_web_models=extra_web,
            web_models=self._account_web_models or None,
        )

    def _sync_subtitle_thinking_widget(self) -> None:
        if not hasattr(self, "cmb_subtitle_thinking") or self.cmb_subtitle_thinking is None:
            return
        cur_eng = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
        self.cmb_subtitle_thinking.configure(state="disabled" if cur_eng == "fast_nmt" else "readonly")

    def _on_subtitle_engine_changed(self, _event: Any = None) -> None:
        choices = self._subtitle_choices()
        lbl_to_code = {lbl: code for code, lbl in choices}
        code = lbl_to_code.get(self.subtitle_engine_var.get().strip(), "auto_inline")
        self.cfg["subtitle_engine"] = code
        save_bridge_config(self.cfg)
        self._sync_subtitle_thinking_widget()
        self.log(f"[자막 모델 변경] {resolve_subtitle_engine_label(code, choices)}")

    def _on_subtitle_thinking_changed(self, _event: Any = None) -> None:
        lbl_to_code = {lbl: code for code, lbl in GEMINI_THINKING_LEVELS}
        level = lbl_to_code.get(self.subtitle_thinking_var.get().strip(), "low")
        self.cfg["subtitle_thinking_level"] = level
        save_bridge_config(self.cfg)
        self.log(f"[자막 추론 품질 변경] {GEMINI_THINKING_LEVEL_LABELS.get(level, level)}")

    def _on_companion_model_changed(self, _event: Any = None) -> None:
        backend = str(self.cfg.get("gemini_backend", "browser"))
        sel = self.companion_model_var.get().strip()
        lvl = str(self.cfg.get("gemini_thinking_level", "low") or "low")
        if lvl not in GEMINI_THINKING_LEVEL_LABELS:
            lvl = "low"
        if backend == "api":
            lbl_to_code = {lbl: code for code, lbl in GEMINI_API_MODELS}
            code = lbl_to_code.get(sel, sel or "gemini-3.5-flash-lite")
            self.cfg["gemini_api_model"] = code
            self.log(f"[대화 모델 변경] Gemini API -> {code} (추론: {GEMINI_THINKING_LEVEL_LABELS.get(lvl, lvl)})")
        else:
            all_web = self._active_web_models()
            lbl_to_code = {lbl: code for code, lbl in all_web}
            code = lbl_to_code.get(sel, "hydrogen")
            self.cfg["gemini_browser_model"] = code
            if code in ("carbon_4", "carbon"):
                self.log("💡 [참고] 4 카본은 Google AI Ultra 계정 전용입니다. 미구독 계정에서는 업그레이드 안내가 자동 취소됩니다.")
            self.gemini_mgr.browser.set_chat_model_async(code, lvl)
            b_lbl = dict(all_web).get(code, GEMINI_BROWSER_MODEL_LABELS.get(code, code))
            self.log(f"[대화 모델 변경] 웹 제미니 -> {b_lbl} (추론: {GEMINI_THINKING_LEVEL_LABELS.get(lvl, lvl)})")
        save_bridge_config(self.cfg)
        self._refresh_companion_badges()

    def _on_companion_thinking_changed(self, _event: Any = None) -> None:
        lbl_to_code = {lbl: code for code, lbl in GEMINI_THINKING_LEVELS}
        lvl = lbl_to_code.get(self.companion_thinking_var.get().strip(), "low")
        self.cfg["gemini_thinking_level"] = lvl
        backend = str(self.cfg.get("gemini_backend", "browser"))
        if backend == "api":
            api_code = str(self.cfg.get("gemini_api_model", "gemini-3.5-flash-lite") or "gemini-3.5-flash-lite")
            self.log(f"[대화 추론 품질 변경] Gemini API({api_code}) -> {GEMINI_THINKING_LEVEL_LABELS.get(lvl, lvl)}")
        else:
            b_code, _ = split_legacy_browser_model(str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"), lvl)
            self.cfg["gemini_browser_model"] = b_code
            self.gemini_mgr.browser.set_chat_model_async(b_code, lvl)
            self.log(f"[대화 추론 품질 변경] 웹 제미니({b_code}) -> {GEMINI_THINKING_LEVEL_LABELS.get(lvl, lvl)}")
        save_bridge_config(self.cfg)
        self._refresh_companion_badges()

    def _translate_subtitle_with_llm(
        self,
        jp_text: str,
        style_guide: str,
        context_lines: list[str],
        engine: str,
        thinking_level: str = "low",
    ) -> str:
        """Route subtitle refinement requests to Gemini API or the hidden 2nd browser CDP tab."""
        eff = (engine or "auto_inline").strip()
        eff_level = thinking_level if thinking_level in GEMINI_THINKING_LEVEL_LABELS else "low"
        if eff == "auto_llm":
            eff = "auto_inline"
        elif eff == "browser_hydrogen_low":
            eff, eff_level = "web:hydrogen", "low"
        elif eff == "browser_hydrogen_high":
            eff, eff_level = "web:hydrogen", "high"
        elif eff in ("browser_flash_lite", "web:flash_lite"):
            eff = "web:auto"
        elif eff in ("web:pro_31", "web:argon_4"):
            eff = "web:neon_4"

        if eff == "fast_nmt":
            return ""

        backend = str(self.cfg.get("gemini_backend", "browser"))
        api_key = str(self.cfg.get("gemini_api_key", "")).strip()
        api_model = str(self.cfg.get("gemini_api_model", "gemini-3.5-flash-lite")).strip() or "gemini-3.5-flash-lite"

        def _web_timeout(model_code: str, level: str) -> float:
            if level == "xhigh":
                return 60.0
            if level == "high":
                return 48.0
            if level == "medium":
                return 34.0
            return 20.0 if model_code in ("auto", "flash_lite") else 26.0

        if eff.startswith("web:"):
            b_model = eff[4:].strip() or "hydrogen"
            return self.gemini_mgr.browser.translate_via_browser_tab(
                jp_text=jp_text,
                style_guide=style_guide,
                context_lines=context_lines,
                model_code=b_model,
                thinking_level=eff_level,
                timeout_sec=_web_timeout(b_model, eff_level),
            )

        if eff.startswith("api:"):
            sub_api_model = eff[4:].strip() or api_model
            if api_key:
                out = self.gemini_mgr.api.translate_short_line(
                    api_key=api_key,
                    model=sub_api_model,
                    jp_text=jp_text,
                    style_guide=style_guide,
                    context_lines=context_lines,
                    thinking_level=eff_level,
                )
                if out:
                    return out
            elif not self._sub_api_no_key_warned:
                self._sub_api_no_key_warned = True
                self.log(f"⚠️ [자막 API 전환] Gemini API 키가 없어 '{sub_api_model}' 대신 로그인된 웹 제미니(Hydrogen)로 자동 폴백합니다.")
            return self.gemini_mgr.browser.translate_via_browser_tab(
                jp_text=jp_text,
                style_guide=style_guide,
                context_lines=context_lines,
                model_code="hydrogen",
                thinking_level=eff_level,
                timeout_sec=_web_timeout("hydrogen", eff_level),
            )

        if eff == "auto_inline":
            b_active, _ = split_legacy_browser_model(
                str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"),
                eff_level,
            )
            if backend == "api" and api_key:
                out = self.gemini_mgr.api.translate_short_line(
                    api_key=api_key,
                    model=api_model,
                    jp_text=jp_text,
                    style_guide=style_guide,
                    context_lines=context_lines,
                    thinking_level=eff_level,
                )
                if out:
                    return out
            if self.gemini_mgr.browser.is_running() and not self.gemini_mgr.browser.login_mode_active:
                out = self.gemini_mgr.browser.translate_via_browser_tab(
                    jp_text=jp_text,
                    style_guide=style_guide,
                    context_lines=context_lines,
                    model_code=b_active,
                    thinking_level=eff_level,
                    timeout_sec=_web_timeout(b_active, eff_level),
                )
                if out:
                    return out
            if api_key:
                out = self.gemini_mgr.api.translate_short_line(
                    api_key=api_key,
                    model=api_model,
                    jp_text=jp_text,
                    style_guide=style_guide,
                    context_lines=context_lines,
                    thinking_level=eff_level,
                )
                if out:
                    return out
            return self.gemini_mgr.browser.translate_via_browser_tab(
                jp_text=jp_text,
                style_guide=style_guide,
                context_lines=context_lines,
                model_code=b_active,
                thinking_level=eff_level,
                timeout_sec=_web_timeout(b_active, eff_level),
            )
        return ""

    def _prepare_subtitle_bundle(
        self,
        clean_jp: str,
        char_data: dict[str, Any] | None,
        is_turn_start: bool,
        pre_kr_future: Future[str] | None = None,
    ) -> dict[str, Any]:
        """Build thread-safe progressive subtitle state synced to Fish Audio playback start."""
        c = char_data or self.current_char
        sub_engine = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
        if sub_engine == "auto_llm":
            sub_engine = "auto_inline"
        sub_thinking = str(self.cfg.get("subtitle_thinking_level", "low") or "low")
        refine_box: dict[str, Any] = {"text": "", "done": False, "entry": None}
        will_refine = False
        kr_future = pre_kr_future

        def _make_refine_cb(box: dict[str, Any]) -> Any:
            def _on_refined(refined_ko: str) -> None:
                with self._subtitle_lock:
                    box["done"] = True
                    box["text"] = refined_ko
                    entry = box["entry"]
                if entry is not None:
                    def _apply() -> None:
                        if entry in self._subtitle_entries:
                            if refined_ko:
                                entry.kr_text = refined_ko
                            entry.refining = False
                            self._rerender_subtitles()

                    try:
                        self.root.after(0, _apply)
                    except Exception:
                        pass

            return _on_refined

        if kr_future is None and clean_jp:
            kr_future, will_refine = self.subtitle_translator.submit_progressive(
                clean_jp,
                char_data=c,
                engine=sub_engine,
                thinking_level=sub_thinking,
                on_refined=_make_refine_cb(refine_box),
            )

        active_eid = str(c.get("id") or self.current_char_id)
        active_name = str(c.get("name_kr") or self.current_char_id)
        if active_eid == self.current_char_id:
            ent = self.momotalk_entry_map.get(getattr(self, "current_momotalk_entry_id", ""))
            if ent is not None and ent.get("base_cid") == active_eid:
                active_eid = str(ent["entry_id"])
                active_name = str(ent.get("name_kr") or active_name)

        return {
            "type": "subtitle_start",
            "char_id": active_eid,
            "char_name_kr": active_name,
            "char_name_jp": str(c.get("name_jp") or ""),
            "char_data": c,
            "jp_text": clean_jp,
            "kr_future": kr_future,
            "pre_inline": pre_kr_future is not None,
            "subtitle_engine": sub_engine,
            "subtitle_thinking_level": sub_thinking,
            "will_refine": will_refine,
            "refine_box": refine_box,
            "refine_cb_factory": _make_refine_cb,
            "is_turn_start": bool(is_turn_start),
        }

    def _insert_subtitle_entry(
        self,
        entry: SubtitleEntry,
        prev_cid: str | None,
        has_previous: bool,
        mode: str,
    ) -> None:
        show_header = entry.is_turn_start or (entry.char_id != prev_cid)
        if has_previous and show_header:
            self.subtitle_box.insert(tk.END, "\n")

        if entry.char_id == "__sensei__":
            if show_header:
                self.subtitle_box.insert(tk.END, "▸ 선생님\n", "sensei_speaker")
            s_txt = (entry.kr_text or entry.jp_text or "").strip()
            if s_txt:
                self.subtitle_box.insert(tk.END, f" {s_txt} \n", "sensei_line")
            return

        if show_header:
            speaker = entry.char_name_kr or entry.char_name_jp or entry.char_id or "캐릭터"
            avatar = self._get_momotalk_photo("portrait", entry.char_id, 24, "#FFFFFF", "#FB7390")
            if avatar is not None:
                self._subtitle_img_refs.append(avatar)
                self.subtitle_box.image_create(tk.END, image=avatar)
                self.subtitle_box.insert(tk.END, f" ▸ {speaker}\n", "speaker")
            else:
                self.subtitle_box.insert(tk.END, f"▸ {speaker}\n", "speaker")

        jp = (entry.jp_text or "").strip()
        kr = (entry.kr_text or "").strip()
        if mode == "jp_only":
            if jp:
                self.subtitle_box.insert(tk.END, f"{jp}\n", "jp_only_line")
        elif mode == "kr_only":
            if kr:
                self.subtitle_box.insert(tk.END, kr, "kr_only_line")
                if entry.refining:
                    self.subtitle_box.insert(tk.END, " ⟳", "refining_tag")
                self.subtitle_box.insert(tk.END, "\n", "kr_only_line")
            elif entry.pending:
                self.subtitle_box.insert(tk.END, "(자막 번역 중...)\n", "pending_line")
            elif jp:
                self.subtitle_box.insert(tk.END, f"{jp}\n", "kr_only_line")
        else:  # jp_kr
            if jp and kr and jp == kr:
                self.subtitle_box.insert(tk.END, f"{jp}\n", "jp_dual_line")
            else:
                if jp:
                    self.subtitle_box.insert(tk.END, f"{jp}\n", "jp_dual_line")
                if kr:
                    self.subtitle_box.insert(tk.END, kr, "kr_dual_line")
                    if entry.refining:
                        self.subtitle_box.insert(tk.END, " ⟳", "refining_tag")
                    self.subtitle_box.insert(tk.END, "\n", "kr_dual_line")
                elif entry.pending and contains_japanese(jp):
                    self.subtitle_box.insert(tk.END, "(자막 번역 중...)\n", "pending_line")

    def _append_subtitle_entry_fast(self, entry: SubtitleEntry) -> None:
        """Append a newly added subtitle entry in O(1) without wiping existing Text content."""
        prev_cid = self._subtitle_entries[-1].char_id if self._subtitle_entries else None
        had_prev = bool(self._subtitle_entries)
        self._subtitle_entries.append(entry)
        if len(self._subtitle_entries) > 250:
            self._subtitle_entries = self._subtitle_entries[-250:]
            self._rerender_subtitles()
            return
        if not hasattr(self, "subtitle_box") or self.subtitle_box is None:
            return
        mode = self.subtitle_mode_var.get()
        if mode not in SUBTITLE_MODES:
            mode = "jp_kr"
        self.subtitle_box.configure(state="normal")
        if self._subtitle_has_placeholder:
            self.subtitle_box.delete("1.0", tk.END)
            self._subtitle_img_refs = []
            self._subtitle_has_placeholder = False
        self._insert_subtitle_entry(entry, prev_cid=prev_cid, has_previous=had_prev, mode=mode)
        self.subtitle_box.see(tk.END)
        self.subtitle_box.configure(state="disabled")

    def _rerender_subtitles(self) -> None:
        if not hasattr(self, "subtitle_box") or self.subtitle_box is None:
            return
        mode = self.subtitle_mode_var.get()
        if mode not in SUBTITLE_MODES:
            mode = "jp_kr"

        self.subtitle_box.configure(state="normal")
        self.subtitle_box.delete("1.0", tk.END)
        self._subtitle_img_refs = []

        if not self._subtitle_entries:
            self.subtitle_box.insert(
                tk.END,
                "대화를 시작하면 캐릭터의 실시간 대사 자막이 이곳에 표시됩니다.\n",
                "placeholder",
            )
            self._subtitle_has_placeholder = True
            self.subtitle_box.configure(state="disabled")
            return

        self._subtitle_has_placeholder = False
        prev_cid: str | None = None
        for idx, entry in enumerate(self._subtitle_entries):
            self._insert_subtitle_entry(entry, prev_cid=prev_cid, has_previous=(idx > 0), mode=mode)
            prev_cid = entry.char_id

        self.subtitle_box.see(tk.END)
        self.subtitle_box.configure(state="disabled")

    def _schedule_subtitle_from_playback(self, item: dict[str, Any]) -> None:
        jp_text = str(item.get("jp_text") or "").strip()
        if not jp_text:
            return
        kr_future = item.get("kr_future")
        pre_inline = bool(item.get("pre_inline", False))
        sub_engine = str(item.get("subtitle_engine") or self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
        if sub_engine == "auto_llm":
            sub_engine = "auto_inline"
        sub_thinking = str(item.get("subtitle_thinking_level") or self.cfg.get("subtitle_thinking_level", "low") or "low")
        char_data = item.get("char_data")
        will_refine = bool(item.get("will_refine", False))
        refine_box = item.get("refine_box")
        refine_cb_factory = item.get("refine_cb_factory")

        def _append_on_ui() -> None:
            self._subtitle_seq += 1
            kr_text = ""
            pending = False
            if kr_future is not None:
                if kr_future.done():
                    try:
                        kr_text = str(kr_future.result() or "").strip()
                    except Exception:
                        kr_text = ""
                    if pre_inline and kr_text:
                        self.subtitle_translator.record_dialogue_pair(
                            jp_text,
                            kr_text,
                            char_data=char_data,
                            quality=f"llm:auto_inline:{sub_thinking}",
                        )
                else:
                    pending = True

            refining_now = bool(will_refine)
            if isinstance(refine_box, dict):
                with self._subtitle_lock:
                    if refine_box.get("done"):
                        refined_val = str(refine_box.get("text") or "").strip()
                        if refined_val:
                            kr_text = refined_val
                        refining_now = False

            entry = SubtitleEntry(
                entry_id=self._subtitle_seq,
                char_id=str(item.get("char_id") or ""),
                char_name_kr=str(item.get("char_name_kr") or ""),
                char_name_jp=str(item.get("char_name_jp") or ""),
                jp_text=jp_text,
                kr_text=kr_text,
                is_turn_start=bool(item.get("is_turn_start", True)),
                pending=pending,
                refining=refining_now,
            )
            if isinstance(refine_box, dict):
                with self._subtitle_lock:
                    refine_box["entry"] = entry
                    if refine_box.get("done"):
                        refined_val = str(refine_box.get("text") or "").strip()
                        if refined_val:
                            entry.kr_text = refined_val
                        entry.refining = False

            self._append_subtitle_entry_fast(entry)

            def _hook_future(fut_obj: Any, is_inline: bool) -> None:
                def _on_translated(fut: Any) -> None:
                    try:
                        resolved = str(fut.result() or "").strip()
                    except Exception:
                        resolved = ""

                    if is_inline and (not resolved) and contains_japanese(jp_text):
                        fb_box = {"text": "", "done": False, "entry": entry}
                        fb_cb = refine_cb_factory(fb_box) if callable(refine_cb_factory) else None
                        fb_fut, fb_ref = self.subtitle_translator.submit_progressive(
                            jp_text,
                            char_data=char_data,
                            engine=sub_engine,
                            thinking_level=sub_thinking,
                            on_refined=fb_cb,
                        )
                        def _mark_ref() -> None:
                            if entry in self._subtitle_entries:
                                entry.refining = bool(fb_ref)
                        try:
                            self.root.after(0, _mark_ref)
                        except Exception:
                            pass
                        _hook_future(fb_fut, False)
                        return

                    if is_inline and resolved:
                        self.subtitle_translator.record_dialogue_pair(
                            jp_text,
                            resolved,
                            char_data=char_data,
                            quality=f"llm:auto_inline:{sub_thinking}",
                        )

                    def _update_entry() -> None:
                        if entry in self._subtitle_entries:
                            if resolved:
                                entry.kr_text = resolved
                            entry.pending = False
                            self._rerender_subtitles()

                    try:
                        self.root.after(0, _update_entry)
                    except Exception:
                        pass

                fut_obj.add_done_callback(_on_translated)

            if pending and kr_future is not None:
                _hook_future(kr_future, pre_inline)
            elif pre_inline and (not kr_text) and contains_japanese(jp_text):
                fb_box = {"text": "", "done": False, "entry": entry}
                fb_cb = refine_cb_factory(fb_box) if callable(refine_cb_factory) else None
                fb_fut, fb_ref = self.subtitle_translator.submit_progressive(
                    jp_text,
                    char_data=char_data,
                    engine=sub_engine,
                    thinking_level=sub_thinking,
                    on_refined=fb_cb,
                )
                entry.pending = not fb_fut.done()
                entry.refining = bool(fb_ref)
                if fb_fut.done():
                    try:
                        entry.kr_text = str(fb_fut.result() or "").strip()
                    except Exception:
                        pass
                else:
                    _hook_future(fb_fut, False)
                self._rerender_subtitles()

        try:
            self.root.after(0, _append_on_ui)
        except Exception:
            pass

    def open_status_log_window(self) -> None:
        win = self._status_log_win
        if win is not None:
            try:
                if win.winfo_exists():
                    win.deiconify()
                    win.lift()
                    win.focus_force()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        win.title("상태 및 진단 로그 — Voice Companion")
        win.geometry("780x440")
        win.configure(bg="#0f172a")
        self._status_log_win = win

        top = tk.Frame(win, bg="#1e293b", padx=10, pady=6)
        top.pack(fill="x")
        tk.Label(
            top,
            text=f"파일 로그: {RUNTIME_LOG_PATH}",
            bg="#1e293b",
            fg="#94a3b8",
            font=("Consolas", 8),
        ).pack(side="left")

        def _copy_all() -> None:
            with self._log_lock:
                txt = "\n".join(self._log_history)
            pyperclip.copy(txt)
            messagebox.showinfo("복사 완료", "상태 로그 전체를 클립보드에 복사했습니다.", parent=win)

        def _open_log_dir() -> None:
            if os.name == "nt":
                _open_directory_in_explorer(RUNTIME_LOG_PATH.parent, (get_logs_dir(),))

        def _clear_view() -> None:
            box.configure(state="normal")
            box.delete("1.0", tk.END)
            box.configure(state="disabled")

        def _on_close() -> None:
            self._status_log_win = None
            self._status_log_box = None
            try:
                win.destroy()
            except Exception:
                pass

        tk.Button(top, text="닫기", command=_on_close, bg="#334155", fg="#ffffff", relief="flat", padx=8, pady=2).pack(side="right", padx=(4, 0))
        tk.Button(top, text="화면 비우기", command=_clear_view, bg="#334155", fg="#e2e8f0", relief="flat", padx=8, pady=2).pack(side="right", padx=4)
        tk.Button(top, text="로그 폴더", command=_open_log_dir, bg="#334155", fg="#e2e8f0", relief="flat", padx=8, pady=2).pack(side="right", padx=4)
        tk.Button(top, text="전체 복사", command=_copy_all, bg="#1e40af", fg="#ffffff", relief="flat", padx=8, pady=2).pack(side="right", padx=4)

        box = scrolledtext.ScrolledText(win, font=("Consolas", 9), bg="#0f172a", fg="#e2e8f0", wrap="word")
        box.pack(fill="both", expand=True, padx=8, pady=8)
        with self._log_lock:
            snapshot = list(self._log_history)
        if snapshot:
            box.insert("1.0", "\n".join(snapshot) + "\n")
            box.see(tk.END)
        box.configure(state="disabled")
        self._status_log_box = box
        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _active_secret_values(self) -> tuple[str, ...]:
        secrets_out: list[str] = []
        try:
            cfg = getattr(self, "cfg", None)
            if isinstance(cfg, dict):
                for k in ("fish_api_key", "gemini_api_key"):
                    v = str(cfg.get(k) or "").strip()
                    if len(v) >= 8:
                        secrets_out.append(v)
        except Exception:
            pass
        try:
            if hasattr(self, "api_key_var"):
                v2 = str(self.api_key_var.get() or "").strip()
                if len(v2) >= 8 and v2 not in secrets_out:
                    secrets_out.append(v2)
        except Exception:
            pass
        return tuple(secrets_out)

    def log(self, msg: str) -> None:
        safe_msg = _redact_secrets(str(msg or ""), extra_secrets=self._active_secret_values())
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {safe_msg}"
        with self._log_lock:
            self._log_history.append(line)
            try:
                if not self._log_dir_ready:
                    RUNTIME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    self._log_dir_ready = True
                if not RUNTIME_LOG_PATH.is_symlink():
                    with open(RUNTIME_LOG_PATH, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
            except Exception:
                pass

        if self._status_log_win is None or self._status_log_box is None or getattr(self, "_closing", False):
            return

        def _append_to_viewer() -> None:
            box = self._status_log_box
            win = self._status_log_win
            if box is None or win is None:
                return
            try:
                if win.winfo_exists():
                    box.configure(state="normal")
                    box.insert(tk.END, line + "\n")
                    box.see(tk.END)
                    box.configure(state="disabled")
            except Exception:
                pass

        self._schedule_ui(_append_to_viewer)

    def _start_http_keepalive_daemon(self) -> None:
        """Keep TCP+TLS socket to api.fish.audio permanently warm (saves ~420ms on first clause!)."""
        def _worker() -> None:
            while not self._closing and not self._stop_keepalive_event.is_set():
                api_key = str(self.cfg.get("fish_api_key", "") or "").strip()
                if api_key and (time.time() - self.last_http_activity >= 18.0):
                    try:
                        keepalive_url = _validate_fish_api_url(f"{FISH_MODEL_URL}?page_size=1")
                        with self.http_client.stream(
                            "GET",
                            keepalive_url,
                            headers={"Authorization": f"Bearer {api_key}"},
                        ) as _resp:
                            pass
                        self.last_http_activity = time.time()
                    except Exception:
                        pass
                self._stop_keepalive_event.wait(5.0)

        threading.Thread(target=_worker, daemon=True, name="fish-http-keepalive").start()

    def _preload_whisper_async(self) -> None:
        def _worker() -> None:
            self.whisper_loading = True
            t0 = time.time()
            _ensure_cuda_dll_paths()
            try:
                from faster_whisper import WhisperModel
                from faster_whisper.tokenizer import Tokenizer
            except Exception as imp_exc:
                self.whisper_loading = False
                self.log(f"[오류] faster-whisper 모듈 로드 실패: {imp_exc}")
                return
            whisper_model_name = "small"
            if whisper_model_name not in _ALLOWED_WHISPER_MODELS or "/" in whisper_model_name or "\\" in whisper_model_name:
                self.whisper_loading = False
                self.log(f"[오류] 허용되지 않은 Whisper 모델 이름: {whisper_model_name!r}")
                return
            try:
                # CUDA GPU: native INT8 + Dynamic 8s Mel Window runs in ~0.10-0.16s
                # faster-whisper loads CTranslate2 binary model.bin + JSON vocab only (zero remote code execution)
                self.whisper_model = WhisperModel(whisper_model_name, device="cuda", compute_type="int8_float32")
                self.tokenizers = {
                    "ja": Tokenizer(self.whisper_model.hf_tokenizer, True, task="transcribe", language="ja"),
                    "ko": Tokenizer(self.whisper_model.hf_tokenizer, True, task="transcribe", language="ko"),
                }
                # Warm up CUDA Dynamic-Mel (4s/6s/8s/12s buckets) so live clauses have 0ms shape-JIT lag
                prompt_w = self.whisper_model.get_prompt(self.tokenizers["ja"], [], without_timestamps=True)
                for bucket_sec in (4, 6, 8, 12):
                    dummy_feats = self.whisper_model.feature_extractor(
                        np.zeros(WHISPER_SR * min(bucket_sec, 4), dtype=np.float32),
                        chunk_length=bucket_sec,
                    )[:, : bucket_sec * 100]
                    enc_w = self.whisper_model.encode(dummy_feats)
                    if bucket_sec == 8:
                        self.whisper_model.model.generate(enc_w, [prompt_w], beam_size=1, max_length=64)
                dt = time.time() - t0
                self.root.after(
                    0,
                    lambda: self.lbl_gpu_status.config(
                        text=f"⚡ Whisper CUDA 초저지연 엔진 준비 완료 (~0.13s, {dt:.1f}s)", fg="#4ade80"
                    ),
                )
                self.log(f"[GPU 준비 완료] CUDA 초저지연 동적 INT8 엔진 워밍업 완료 ({dt:.1f}초).")
            except Exception as exc:
                self.log(f"[GPU 경고] CUDA 로딩 실패, CPU 폴백 시도: {exc}")
                try:
                    self.whisper_model = WhisperModel(whisper_model_name, device="cpu", compute_type="int8")
                    self.tokenizers = {
                        "ja": Tokenizer(self.whisper_model.hf_tokenizer, True, task="transcribe", language="ja"),
                        "ko": Tokenizer(self.whisper_model.hf_tokenizer, True, task="transcribe", language="ko"),
                    }
                    self.root.after(0, lambda: self.lbl_gpu_status.config(text="⚪ Whisper CPU 모드 준비됨", fg="#fbbf24"))
                except Exception as exc2:
                    self.log(f"[오류] Whisper 초기화 실패: {exc2}")
            finally:
                self.whisper_loading = False

        threading.Thread(target=_worker, daemon=True).start()

    def abort_current_playback(self) -> None:
        self.playback_gen_id += 1
        self.stop_playback_flag.set()
        for q in (self.tts_queue, self.ordered_clause_queue, self.pcm_play_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def open_reference_folder(self) -> None:
        c = self.current_char
        v_lang = self._voice_lang_code()
        comb_wav, _ = self._char_ref_paths(c["id"], lang=v_lang)
        allowed_roots = (get_user_models_dir(), ROOT_DIR / "models")
        if comb_wav.exists():
            _reveal_file_in_explorer(comb_wav, allowed_roots)
            return

        def _extract_worker() -> None:
            self.log(f"[스팀 추출] '{c.get('name_kr', c['id'])}' ({v_lang})의 스팀 원음(.molru)에서 깨끗한 스튜디오 WAV를 추출 중입니다...")
            try:
                res = build_character_fish_reference(c, lang=v_lang, game_dir=self._get_resolved_ba_game_dir())
                self.log(f"[추출 완료] {res['clip_count']}개 클립 ({res['total_duration_sec']}초) -> {res['combined_wav']}")
                _reveal_file_in_explorer(Path(res["combined_wav"]), allowed_roots)
            except Exception as exc:
                safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
                self.log(f"[스팀 추출 안내] {safe_exc}")
                self.root.after(0, lambda m=safe_exc: messagebox.showwarning("스팀 음성 팩 안내", m))

        threading.Thread(target=_extract_worker, daemon=True).start()

    def copy_jp_prompt(self) -> None:
        c = self.current_char
        vcode = PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")
        prompt, vlabel = resolve_voice_persona_prompt(c, vcode)
        if not prompt:
            messagebox.showwarning("프롬프트 없음", "선택한 캐릭터의 통화용 프롬프트가 비어 있습니다.")
            return
        lead_applied = bool(self.proactive_prompt_var.get())
        if lead_applied:
            prompt = build_proactive_lead_prompt(c, prompt)
        is_ko = self._get_reply_language() == "ko"
        coach_applied = bool(self.jp_beginner_coach_var.get()) and (not is_ko)
        if coach_applied:
            prompt = build_jp_beginner_lover_coach_prompt(c, prompt)
        elif is_ko:
            ko_guide = build_character_ko_style_guide(c, jp_beginner_coach=False)
            prompt = f"{prompt}\n\n{ko_guide}\n\n{KOREAN_VOICE_OUTPUT_LOCK}"
        pyperclip.copy(prompt)
        self.save_settings_ui(silent=True)
        lead_tag = " + [선제 질문·대화 리드 규칙]" if lead_applied else ""
        coach_tag = " + [🌸 서툰 일본어 눈높이 배려]" if coach_applied else (" + [🇰🇷 한국어 답변]" if is_ko else "")
        self.log(f"[프롬프트 복사] '{c.get('name_kr')}' ({c.get('name_jp')}) — {vlabel}{lead_tag}{coach_tag} 복사 완료!")
        lang_desc = "한국어 100%" if is_ko else "일본어 100%"
        messagebox.showinfo(
            f"통화용 {'한국어' if is_ko else '일본어'} 프롬프트 복사 완료",
            f"[{c.get('name_kr')} ({c.get('name_jp')})]\n적용 버전: {vlabel}{lead_tag}{coach_tag}\n\n{lang_desc} 실시간 음성통화(Gemini Live) 전용 프롬프트가 클립보드에 복사되었습니다!\n(공식 원작 해석 프롬프트1과 2차창작 프롬프트2는 완전히 분리되어 보존됩니다.)",
        )

    def copy_rp_chat_prompt(self) -> None:
        c = self.current_char
        persona = c.get("persona") or {}
        prompt = persona.get("japanese_rp_chat_prompt") or persona.get("gemini_live_voice_prompt", "")
        if not prompt:
            messagebox.showwarning("프롬프트 없음", "선택한 캐릭터의 RP 프롬프트가 비어 있습니다.")
            return
        pyperclip.copy(prompt)
        self.log(f"[RP 프롬프트 복사] '{c.get('name_kr')}' ({c.get('name_jp')}) 공식 심층 채팅/RP 페르소나 복사 완료!")
        messagebox.showinfo(
            "공식 심층 RP·채팅 프롬프트 복사 완료",
            f"[{c.get('name_kr')} ({c.get('name_jp')})]\n문장 길이 제한 없이 캐릭터의 내면 심리·모모톡·대면 대화를 100% 재현하는 공식 일본어 심층 RP 프롬프트가 복사되었습니다!",
        )

    def open_persona_inspector(self) -> None:
        c = self.current_char
        persona = c.get("persona") or {}
        sm = persona.get("speech_mechanics_jp") or {}
        quotes = persona.get("verbatim_reference_quotes_jp") or []
        fanon = persona.get("fanon_dc_ba_gallery") or {}

        win = tk.Toplevel(self.root)
        win.title(f"공식 원작 vs 2차창작 정밀 분석 — {c.get('name_kr')} ({c.get('name_jp')})")
        win.geometry("920x740")
        win.configure(bg="#ffffff")

        qbar = tk.Frame(win, bg="#f8fafc", padx=12, pady=8, highlightbackground="#e2e8f0", highlightthickness=1)
        qbar.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(qbar, text="원클릭 복사:", bg="#f8fafc", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 6))

        def _quick_copy(vcode: str) -> None:
            p_txt, p_lbl = resolve_voice_persona_prompt(c, vcode)
            if p_txt:
                lead_applied = bool(self.proactive_prompt_var.get())
                if lead_applied:
                    p_txt = build_proactive_lead_prompt(c, p_txt)
                is_ko = self._get_reply_language() == "ko"
                coach_applied = bool(self.jp_beginner_coach_var.get()) and (not is_ko)
                if coach_applied:
                    p_txt = build_jp_beginner_lover_coach_prompt(c, p_txt)
                elif is_ko:
                    ko_guide = build_character_ko_style_guide(c, jp_beginner_coach=False)
                    p_txt = f"{p_txt}\n\n{ko_guide}\n\n{KOREAN_VOICE_OUTPUT_LOCK}"
                pyperclip.copy(p_txt)
                self.persona_variant_var.set(PERSONA_VARIANT_LABELS[vcode])
                self._on_persona_variant_changed()
                lead_tag = " + [선제 질문·대화 리드]" if lead_applied else ""
                coach_tag = " + [🌸 서툰 일본어 눈높이 배려]" if coach_applied else (" + [🇰🇷 한국어 답변]" if is_ko else "")
                self.log(f"[상세창 복사] '{c.get('name_kr')}' — {p_lbl}{lead_tag}{coach_tag} 복사 및 적용 완료!")
                messagebox.showinfo("복사 완료", f"[{c.get('name_kr')}]\n{p_lbl}{lead_tag}{coach_tag}\n클립보드에 복사되었습니다!", parent=win)

        tk.Button(qbar, text="📘 프롬프트1(공식 정사)", command=lambda: _quick_copy("official"), bg="#dbeafe", fg="#1e40af", font=("Malgun Gothic", 8, "bold"), padx=7).pack(side="left", padx=2)
        tk.Button(qbar, text="🎭 2-A(주류·순애드리프트)", command=lambda: _quick_copy("fanon_mainstream"), bg="#e0e7ff", fg="#3730a3", font=("Malgun Gothic", 8, "bold"), padx=7).pack(side="left", padx=2)
        tk.Button(qbar, text="💖 2-B(고습도 순애)", command=lambda: _quick_copy("fanon_pure_love"), bg="#fce7f3", fg="#9d174d", font=("Malgun Gothic", 8, "bold"), padx=7).pack(side="left", padx=2)
        tk.Button(qbar, text="🔥 2-C(매운맛 음해·개그)", command=lambda: _quick_copy("fanon_spicy_meme"), bg="#fef3c7", fg="#92400e", font=("Malgun Gothic", 8, "bold"), padx=7).pack(side="left", padx=2)
        tk.Button(qbar, text="🌟 2-D(캐릭터별 최적배합 통합)", command=lambda: _quick_copy("fanon_optimal_blend"), bg="#ccfbf1", fg="#115e59", font=("Malgun Gothic", 8, "bold"), padx=8).pack(side="left", padx=3)

        txt = scrolledtext.ScrolledText(win, font=("Malgun Gothic", 10), wrap="word", padx=14, pady=12)
        txt.pack(fill="both", expand=True, padx=12, pady=10)

        vap = persona.get("voice_acting_profile") or {}
        vap_lines = []
        if vap:
            extra_vap = []
            if vap.get("spike_directing"):
                extra_vap.append(f"• 감정 급변(Spike) 디렉팅 태그: {vap.get('spike_directing')}")
            if vap.get("whisper_directing"):
                extra_vap.append(f"• 속삭임·데레(Whisper) 디렉팅 태그: {vap.get('whisper_directing')}")
            if vap.get("pitch_baseline") or vap.get("tempo_baseline"):
                extra_vap.append(f"• 음역 / 템포 가이드: pitch={vap.get('pitch_baseline', '')} / tempo={vap.get('tempo_baseline', '')}")
            if vap.get("steam_clip_curation_guide"):
                extra_vap.append(f"• Steam 클립 WAV 큐레이션 가이드: {vap.get('steam_clip_curation_guide')}")
            vap_lines = [
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "【2-B. 캐릭터 고유 성우 감정·호흡 디렉팅 프로파일 (Fish s2.1-pro)】",
                f"• 연기 성향 아키타입: {vap.get('archetype_kr', '')} ({vap.get('archetype', '')})",
                f"• 기본 텐션 / 감정 급변 온도: base_temp={vap.get('base_temperature')} → spike_temp={vap.get('spike_temperature')} (top_p={vap.get('top_p')})",
                f"• 호흡 최소 길이 / 문장 휴지: min_clause={vap.get('min_clause_sec')}s / end_pause={vap.get('end_pause_sec')}s",
                f"• 기본 연기 디렉팅 태그: {vap.get('default_s2_tag', '')}",
                *extra_vap,
                f"• 허용 감정 태그: {', '.join(vap.get('allowed_s2_tags') or [])}",
                f"• 캐릭터 붕괴 방지(차단 태그): {', '.join(vap.get('forbidden_s2_tags') or ['없음'])}",
                "",
            ]

        fanon_lines = []
        if fanon:
            memes = fanon.get("representative_memes_kr") or []
            emp = fanon.get("empirical_18m_stats") or {}
            emp_lines = []
            if emp:
                top_t = emp.get("top_comic_titles_kr") or []
                emp_lines = [
                    f"• 18개월 개념글 실증 통계 ({emp.get('period', '2025.05~2026.11')}): "
                    f"총 {int(emp.get('recommend_comic_count') or 0):,}건 "
                    f"(창작 {int(emp.get('creation_count') or 0):,}건 / 번역 {int(emp.get('translation_count') or 0):,}건) · "
                    f"누적 추천 {int(emp.get('total_recommends') or 0):,}회",
                    *(
                        ["• 해당 캐릭터 18개월 최고추천 개념글 만화 TOP:", *[f"  - {tt}" for tt in top_t]]
                        if top_t
                        else []
                    ),
                ]
            tiers = fanon.get("fanon_tiers") or {}
            tier_lines = []
            if isinstance(tiers, dict) and tiers:
                tier_lines.append("• 단계별(Lv.1~Lv.3) 동적 페르소나 구성:")
                for tkey in ("Lv.1", "Lv.2", "Lv.3"):
                    tobj = tiers.get(tkey)
                    if not isinstance(tobj, dict):
                        continue
                    tname = tobj.get("tier_name", tkey)
                    wpct = tobj.get("blend_weight_pct")
                    wpct_str = f" ({wpct}%)" if wpct is not None else ""
                    if tkey == "Lv.3":
                        trigs = tobj.get("triggers") or []
                        t_labels = [str(tr.get("label_kr") or tr.get("trigger_key") or "") for tr in trigs if isinstance(tr, dict)]
                        detail = ", ".join(x for x in t_labels if x)
                        tier_lines.append(f"  - {tkey}{wpct_str} [{tname}]: {len(trigs)}종 트리거" + (f" ({detail})" if detail else ""))
                    else:
                        t_items = tobj.get("items") or []
                        t_titles = [str(it.get("title_kr") or "") for it in t_items if isinstance(it, dict)]
                        detail = ", ".join(x for x in t_titles[:3] if x)
                        if len(t_titles) > 3:
                            detail += f" 외 {len(t_titles) - 3}건"
                        tier_lines.append(f"  - {tkey}{wpct_str} [{tname}]: {len(t_items)}건" + (f" ({detail})" if detail else ""))
            fanon_lines = [
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "【6. 인기 만화 2차 창작 해석 (공식과 별도 분리)】",
                f"• 분석 기준: {fanon.get('source_basis', '인기 만화 2차 창작 해석')}",
                *emp_lines,
                f"• 2차창작 주류 소비 비중: {fanon.get('mainstream_tendency_kr', '')}",
                f"• 프롬프트 2-D 캐릭터 맞춤 최적 배합비: {fanon.get('optimal_blend_ratio_kr', '')}",
                f"• 2차창작 요약: {fanon.get('summary_kr', '')}",
                "• 대표 만화 클리셰 · 정실/습도 · 음해/발작버튼 밈:",
                *[f"  - {m}" for m in memes],
                *tier_lines,
                "",
                "──────────────────────────────────────────────────",
                "【6-D. 프롬프트 2-D: 캐릭터별 2차창작 최적 배합 통합본 통화 프롬프트 (日本語 · 추천)】",
                fanon.get("voice_prompt_optimal_blend_jp", ""),
                "",
                "──────────────────────────────────────────────────",
                "【6-A. 프롬프트 2-A: 2차창작 주류 종합 · 순애 드리프트 통화 프롬프트 (日本語)】",
                fanon.get("voice_prompt_mainstream_jp", ""),
                "",
                "──────────────────────────────────────────────────",
                "【6-B. 프롬프트 2-B: 2차창작 고습도 순애 · 정실 무브 통화 프롬프트 (日本語)】",
                fanon.get("voice_prompt_pure_love_jp", ""),
                "",
                "──────────────────────────────────────────────────",
                "【6-C. 프롬프트 2-C: 2차창작 매운맛 개그 · 음해 · 발작버튼 통화 프롬프트 (日本語)】",
                fanon.get("voice_prompt_spicy_meme_jp", ""),
            ]

        extra_meta = []
        if persona.get("timeline_basis"):
            extra_meta.append(f"■ 타임라인 기준: {persona.get('timeline_basis')}")
        if persona.get("compendium_upgrade_note"):
            extra_meta.append(f"■ 패치 업그레이드 노트: {persona.get('compendium_upgrade_note')}")
        forb_expr = sm.get("forbidden_expressions") or []
        forb_line = [f"• 발화 금칙어 (절대 사용 금지): {', '.join(forb_expr)}"] if forb_expr else []

        report_lines = [
            f"■ 캐릭터: {c.get('name_kr')} / {c.get('name_jp')} ({c.get('ruby_jp', '')})",
            f"■ 소속: {c.get('school')} ({c.get('school_jp', '')}) — {c.get('club')} ({c.get('club_jp', '')})",
            f"■ 공식 정사 요약: {persona.get('summary', '')}",
            *extra_meta,
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "【1. 공식 원작 심층 성격 · 내면 심리 · 갭모에 분석 (메인/인연/이벤트 시나리오 정사)】",
            persona.get("personality_analysis_kr", "(상세 분석 정보)"),
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "【2. 일본어 원문 말투 메커니즘 (口調・一人称・二人称・語尾)】",
            f"• 일인칭 (一人称): {sm.get('first_person', '')}",
            f"• 선생님 호칭 (先生の呼び方): {sm.get('sensei_address', c.get('user_title_jp', ''))}",
            f"• 주변 인물 호칭: {sm.get('peer_address_examples', '')}",
            f"• 기본 어조 (基本口調): {sm.get('base_register', '')}",
            f"• 대표 어미 (語尾): {', '.join(sm.get('signature_endings') or [])}",
            f"• 감탄사 · 웃음소리 · 말버릇: {', '.join(sm.get('interjections_and_laughs') or [])}",
            f"• 모모톡 문체 특징: {sm.get('texting_momotalk_style', '')}",
            f"• 감정 변화 · 갭모에 전환: {sm.get('emotional_and_gap_shifts', '')}",
            *forb_line,
            "",
            *vap_lines,
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "【3. 인게임 원문 레퍼런스 대사 (日本語 Verbatim Quotes)】",
            *[f"  「{q}」" for q in quotes],
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "【4. 프롬프트 1: 공식 원작 해석 Gemini Live 실시간 음성통화 프롬프트 (日本語)】",
            persona.get("gemini_live_voice_prompt", ""),
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "【5. 공식 심층 텍스트 채팅 / 롤플레잉 프롬프트 (日本語)】",
            persona.get("japanese_rp_chat_prompt", ""),
            *fanon_lines,
        ]
        txt.insert("1.0", "\n".join(report_lines))
        txt.configure(state="disabled")

    def save_settings_ui(self, silent: bool = False) -> bool:
        clean_id = extract_model_id(self.ref_id_var.get())
        self.ref_id_var.set(clean_id)
        self.cfg["emotion_engine_version"] = 4
        self.cfg["selected_character_id"] = self.current_char_id
        self.cfg["persona_prompt_variant"] = PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")
        self.cfg["fish_api_key"] = self.api_key_var.get().strip()
        self.cfg["fish_reference_id"] = clean_id
        self.cfg["reply_language"] = self._get_reply_language()
        if clean_id:
            self._set_saved_voice_id(self.current_char_id, clean_id)
        self.cfg["fish_model_header"] = self.model_hdr_var.get().strip()
        self.cfg["stt_language"] = STT_LABEL_TO_CODE.get(self.stt_lang_var.get(), "auto")
        self.cfg["streaming_speed_mode"] = SPEED_LABEL_TO_CODE.get(self.speed_mode_var.get(), "expressive")
        self.cfg["auto_emotion_directing"] = bool(self.auto_emotion_var.get())
        self.cfg["expression_boost_offset"] = float(BOOST_LABEL_TO_VAL.get(self.boost_preset_var.get(), 0.0))
        self.cfg["pause_threshold_sec"] = float(self.pause_sec_var.get())
        self.cfg["output_volume"] = float(self.vol_var.get())
        self.cfg["proactive_prompt_append"] = bool(self.proactive_prompt_var.get())
        self.cfg["jp_beginner_lover_mode"] = bool(self.jp_beginner_coach_var.get())
        self.cfg["temporary_call_mode"] = bool(self.temporary_call_var.get())
        self.cfg["proactive_enable"] = bool(self.proactive_enable_var.get())
        self.cfg["proactive_timeout_sec"] = float(PROACTIVE_TIMEOUT_VALS.get(self.proactive_timeout_var.get(), 18.0))
        self.cfg["proactive_trigger_volume"] = float(PROACTIVE_VOL_VALS.get(self.proactive_vol_var.get(), 1.0))
        self.cfg["proactive_trigger_device"] = self.trigger_out_choice_var.get()
        self.cfg["primary_output_device"] = self.out_choice_var.get()
        mic_val = self.mic_choice_var.get().strip()
        if mic_val and not mic_val.startswith("("):
            self.cfg["mic_input_device"] = mic_val
        self.cfg["companion_proactive_enable"] = bool(self.companion_proactive_var.get())
        self.cfg["subtitle_mode"] = self.subtitle_mode_var.get()
        sub_choices = self._subtitle_choices()
        sub_lbl_to_code = {lbl: code for code, lbl in sub_choices}
        self.cfg["subtitle_engine"] = sub_lbl_to_code.get(
            self.subtitle_engine_var.get().strip(),
            str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline"),
        )
        t_lbl_to_code = {lbl: code for code, lbl in GEMINI_THINKING_LEVELS}
        self.cfg["subtitle_thinking_level"] = t_lbl_to_code.get(
            self.subtitle_thinking_var.get().strip(),
            str(self.cfg.get("subtitle_thinking_level", "low") or "low"),
        )
        self.cfg["gemini_thinking_level"] = t_lbl_to_code.get(
            self.companion_thinking_var.get().strip(),
            str(self.cfg.get("gemini_thinking_level", "low") or "low"),
        )
        comp_sel = self.companion_model_var.get().strip()
        if comp_sel:
            if str(self.cfg.get("gemini_backend", "browser")) == "api":
                api_lbl_to_code = {lbl: code for code, lbl in GEMINI_API_MODELS}
                self.cfg["gemini_api_model"] = api_lbl_to_code.get(comp_sel, comp_sel)
            else:
                web_lbl_to_code = {lbl: code for code, lbl in self._active_web_models()}
                if comp_sel in web_lbl_to_code:
                    self.cfg["gemini_browser_model"] = web_lbl_to_code[comp_sel]
        try:
            if hasattr(self, "mode_notebook"):
                tab_idx = self.mode_notebook.index(self.mode_notebook.select())
                self.cfg["bridge_mode"] = "gemini_live_audio" if tab_idx == 1 else "pc_mic"
        except Exception:
            pass
        self.cfg["call_bridge_mode"] = "mode2" if self.cfg.get("bridge_mode") == "gemini_live_audio" else "mode1"
        save_bridge_config(self.cfg)
        self.watchdog.configure(
            enabled=self.cfg["proactive_enable"],
            silence_timeout_sec=self.cfg["proactive_timeout_sec"],
        )
        self.companion_sm.configure(
            proactive_enabled=self.cfg["companion_proactive_enable"],
            min_idle_sec=float(self.cfg.get("idle_ping_min_sec", 45.0)),
            max_idle_sec=float(self.cfg.get("idle_ping_max_sec", 75.0)),
            max_consecutive_pings=int(self.cfg.get("afk_max_consecutive_pings", 2)),
        )
        self._sync_mic_stt_config()
        if getattr(self, "_ui_boot_complete", False):
            self._render_roster_visible(force_all=True)
        if not silent:
            self.log(f"[설정 저장] API 키, 감정 디렉팅 및 '{self.current_char.get('name_kr')}' 보이스 ID 설정을 저장했습니다.")
        return True

    def paste_api_key_and_save(self) -> None:
        raw = pyperclip.paste().strip()
        if not raw:
            messagebox.showwarning("클립보드 비어있음", "먼저 Fish Audio 개발자 메뉴에서 API Key를 복사해 주세요!")
            return
        self.api_key_var.set(raw)
        self.save_settings_ui(silent=True)
        self.log(f"[API Key 저장 완료] 길이 {len(raw)}자 키가 등록되었습니다!")
        messagebox.showinfo("API Key 등록 완료", "Fish Audio API Key가 입력 및 저장되었습니다!")

    def paste_url_from_clipboard(self) -> None:
        raw = pyperclip.paste().strip()
        if not raw:
            messagebox.showwarning("클립보드 비어있음", "먼저 Fish Audio 사이트에서 원하는 블루 아카이브 캐릭터 페이지 주소(URL)를 복사해 주세요!")
            return
        mid = extract_model_id(raw)
        if not mid:
            messagebox.showwarning(
                "유효하지 않은 보이스 ID",
                "올바른 Fish Audio 모델 ID(32자리 16진수) 또는 https://fish.audio/.../m/<ID> 주소가 아닙니다.",
            )
            return
        self.ref_id_var.set(mid)
        self.save_settings_ui(silent=True)
        cname = self.current_char.get("name_kr", self.current_char_id)
        self.log(f"[보이스 장착 완료] '{cname}' 전용 보이스 ID 저장 완료: {mid}")
        messagebox.showinfo("보이스 적용 완료", f"[{cname}] 모델 ID [{mid}]가 적용 및 자동 기억되었습니다!\n바로 [🔊 일본어 시그니처 대사 들어보기]를 눌러 테스트해 보세요!")

    def open_voice_search_dialog(self) -> None:
        api_key = self.api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning("API Key 필요", "인기 보이스 검색을 위해 먼저 1단계에 Fish Audio API Key를 입력해 주세요!\n(키 발급 전이라면 브라우저에서 마음에 드는 보이스 주소만 복사해 [📋 복사한 URL 바로 적용]을 누르셔도 됩니다.)")
            return

        c = self.current_char
        default_query = _JP_PAREN_SPLIT_RE.split(c.get("name_jp", "陸八魔アル"))[0].strip()

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Fish Audio 인기 보이스 검색기 — {c.get('name_kr')} ({default_query})")
        dlg.geometry("700x450")
        dlg.configure(bg="#ffffff")

        top = tk.Frame(dlg, bg="#ffffff", padx=12, pady=10)
        top.pack(fill="x")
        q_var = tk.StringVar(value=default_query)
        tk.Label(
            top,
            text=f"검색어 (현재 캐릭터 일본어명 자동 입력됨 — 성/이름만 검색하거나 '{c['id']}', 'ブルーアーカイブ' 등도 가능):",
            bg="#ffffff",
        ).pack(anchor="w")
        s_row = tk.Frame(top, bg="#ffffff")
        s_row.pack(fill="x", pady=4)
        ent = tk.Entry(s_row, textvariable=q_var, width=42, font=("Malgun Gothic", 10))
        ent.pack(side="left", padx=(0, 8))

        cols = ("title", "tasks", "id")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=13)
        tree.heading("title", text="보이스 모델 이름")
        tree.heading("tasks", text="사용 횟수(인기도)")
        tree.heading("id", text="Model ID")
        tree.column("title", width=350)
        tree.column("tasks", width=110, anchor="e")
        tree.column("id", width=190)
        tree.pack(fill="both", expand=True, padx=12, pady=4)

        def _fetch_search_items(query_text: str) -> tuple[int, list[dict[str, Any]], str]:
            search_url = _validate_fish_api_url(FISH_MODEL_URL)
            with requests.get(
                search_url,
                headers={"Authorization": f"Bearer {api_key}"},
                params={"page_size": 25, "title": query_text, "sort_by": "task_count"},
                timeout=15,
                verify=True,
                allow_redirects=False,
                stream=True,
            ) as resp:
                buf = bytearray()
                for chunk in resp.iter_content(chunk_size=32768):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > MAX_FISH_SEARCH_RESPONSE_BYTES:
                        raise ValueError("Fish Audio 검색 응답 크기가 허용 한도를 초과했습니다.")
                raw_txt = buf.decode("utf-8", errors="replace")
                if resp.status_code != 200:
                    return resp.status_code, [], _redact_secrets(raw_txt[:200], extra_secrets=(api_key,))
                parsed = json.loads(raw_txt) if raw_txt else {}
                raw_items = parsed.get("items") if isinstance(parsed, dict) else []
                return 200, (raw_items if isinstance(raw_items, list) else []), ""

        def _do_search() -> None:
            for row in tree.get_children():
                tree.delete(row)
            try:
                status_code, items, err_preview = _fetch_search_items(q_var.get().strip())
                if status_code == 200:
                    # If full name has 0 hits, automatically fall back to given name in katakana/kanji
                    if not items and len(q_var.get().strip()) >= 4:
                        short_q = c.get("name_jp", "")[-3:]
                        st2, items2, _ = _fetch_search_items(short_q)
                        if st2 == 200:
                            items = items2
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        mid = extract_model_id(str(it.get("_id") or it.get("id") or ""))
                        if not mid:
                            continue
                        title = str(it.get("title") or "")
                        tasks = int(it.get("task_count") or 0)
                        tree.insert("", tk.END, values=(title, f"{tasks:,}", mid))
                else:
                    messagebox.showerror("검색 실패", f"HTTP {status_code}: {err_preview}")
            except Exception as exc:
                messagebox.showerror("오류", _redact_secrets(str(exc), extra_secrets=(api_key,)))

        def _select_item(_event=None) -> None:
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], "values")
            title, _, mid = vals
            self.ref_id_var.set(mid)
            self.save_settings_ui(silent=True)
            if getattr(self, "_momotalk_sort_mode", "default") == "heart":
                self._refresh_character_filter_list(keep_current=True)
            else:
                self._render_roster_visible(force_all=True)
            self._refresh_momotalk_header_and_cards()
            self.log(f"[인기 보이스 장착] [{c.get('name_kr')}] '{title}' (ID: {mid}) 저장 완료!")
            dlg.destroy()

        tk.Button(s_row, text="🔍 인기순 검색", command=_do_search, bg="#2563eb", fg="#ffffff", padx=12).pack(side="left", padx=(0, 6))
        tk.Button(s_row, text="✅ 선택 보이스 장착", command=_select_item, bg="#16a34a", fg="#ffffff", padx=12).pack(side="left")
        tree.bind("<Double-1>", _select_item)
        dlg.after(100, _do_search)

    def auto_generate_current_student_voice_async(
        self,
        *,
        target_cid: str | None = None,
        target_lang: str | None = None,
        visibility_override: str | None = None,
        alt_voice_key: str | None = None,
        force_recreate: bool = False,
        start_call_after: bool = False,
        show_success_popup: bool = True,
        on_done: Any = None,
        http_session: Any = None,
    ) -> None:
        """
        Distribution-ready 1-click on-demand student voice generator & auto-linker.
        Extracts studio WAVs from Steam (auto-discovered across all drives), checks the user's
        Fish Audio account first to prevent duplicates, uploads & links straight into MomoTalk,
        and guides recovery if Steam folder or Fish Audio private slot quota needs attention.
        """
        api_key = self.api_key_var.get().strip() or str(self.cfg.get("fish_api_key", "") or "").strip()
        if not api_key:
            open_set = messagebox.askyesno(
                "Fish Audio API Key 필요",
                "학생 목소리를 자동 생성·연결하려면 먼저 Fish Audio API Key가 필요합니다.\n\n"
                "지금 설정 창 1단계를 열어 API Key를 입력할까요?",
            )
            if open_set:
                self.open_settings_dialog(first_run=False, initial_tab=1)
            return

        self.save_settings_ui(silent=True)
        base_cid = str(target_cid or self.current_char_id or "Aru").strip()
        base_char = self.characters.get(base_cid) or self.current_char
        if alt_voice_key:
            alt_char = build_alt_voice_char_data(base_char, alt_voice_key)
            if alt_char is None:
                return
            char_data = alt_char
            slot_cid = str(char_data["id"])
        else:
            char_data = base_char
            slot_cid = str(char_data.get("id") or base_cid)

        v_lang = "KR" if str(target_lang or self._voice_lang_code()).strip().upper() == "KR" else "JP"
        rlang = "ko" if v_lang == "KR" else "ja"
        inflight_key = (base_cid, v_lang)

        with self._voice_gen_lock:
            if inflight_key in self._voice_gen_inflight:
                self.log(f"[안내] '{char_data.get('name_kr', slot_cid)}' ({v_lang}) 목소리 생성이 이미 진행 중입니다.")
                return
            self._voice_gen_inflight[inflight_key] = ("sync", f"🔍 [{char_data.get('name_kr', slot_cid)} ({v_lang})] 준비 중...")

        self._refresh_momotalk_header_and_cards()

        def _set_stage(stage: str, msg: str) -> None:
            with self._voice_gen_lock:
                if inflight_key in self._voice_gen_inflight:
                    self._voice_gen_inflight[inflight_key] = (stage, msg)
            self._schedule_ui(self._refresh_momotalk_header_and_cards)

        def _on_linked_ui(linked_cid: str, linked_lang: str, linked_mid: str) -> None:
            def _apply_ui() -> None:
                linked_rlang = "ko" if linked_lang.upper() == "KR" else "ja"
                self._set_saved_voice_id(linked_cid, linked_mid, reply_lang=linked_rlang)
                if linked_cid == self.current_char_id and self._get_reply_language() == linked_rlang:
                    self.ref_id_var.set(linked_mid)
                    self.cfg["fish_reference_id"] = linked_mid
                if getattr(self, "_momotalk_sort_mode", "default") == "heart":
                    self._refresh_character_filter_list(keep_current=True)
                else:
                    self._render_roster_visible(force_all=True)
                self._refresh_momotalk_header_and_cards()

            self._schedule_ui(_apply_ui)

        def _worker() -> None:
            from src.controller.fish_batch_builder import auto_generate_and_link_student_voice

            res: dict[str, Any] = {"ok": False, "fatal_code": "", "error": "알 수 없는 오류"}
            try:
                res = auto_generate_and_link_student_voice(
                    char_data=char_data,
                    lang=v_lang,
                    api_key=api_key,
                    cfg=self.cfg,
                    characters=self.characters,
                    visibility=visibility_override,
                    game_dir=self._get_resolved_ba_game_dir(),
                    include_cover=True,
                    check_account_first=not force_recreate,
                    include_alt_voices=(not bool(alt_voice_key)),
                    force_recreate=force_recreate,
                    asset_mgr=getattr(self, "momotalk_assets", None),
                    http_session=http_session,
                    on_stage=_set_stage,
                    on_model_linked=_on_linked_ui,
                    log_cb=self.log,
                )
            except Exception as exc:
                res = {"ok": False, "fatal_code": "", "error": f"모델 생성 중 예외 발생: {exc}"}
                self.log(f"[오류] {res['error']}")
            finally:
                with self._voice_gen_lock:
                    self._voice_gen_inflight.pop(inflight_key, None)

            def _finish_on_ui() -> None:
                if getattr(self, "_closing", False):
                    return
                if getattr(self, "_momotalk_sort_mode", "default") == "heart":
                    self._refresh_character_filter_list(keep_current=True)
                else:
                    self._render_roster_visible(force_all=True)
                self._refresh_momotalk_header_and_cards()

                if callable(on_done):
                    try:
                        on_done(res)
                    except Exception:
                        pass

                nm = str(char_data.get("name_kr") or slot_cid)
                if res.get("ok"):
                    mid = str(res.get("model_id") or "")
                    src_kind = str(res.get("source") or "created")
                    alt_ok = [a for a in (res.get("alt_results") or []) if a.get("ok")]
                    alt_note = f"\n🎭 보조 보이스 {len(alt_ok)}종도 함께 자동 연결되었습니다." if alt_ok else ""
                    if start_call_after and self.current_char_id == base_cid:
                        self.start_momotalk_call()
                        return
                    if show_success_popup:
                        if src_kind == "account_sync":
                            messagebox.showinfo(
                                "기존 보이스 자동 연결 완료",
                                f"내 Fish Audio 계정에 이미 만들어 둔 [{nm} ({v_lang})] 모델을 찾아 바로 연결했습니다!{alt_note}\n"
                                f"Model ID: {mid}\n\n💗 모모톡 하트가 활성화되었으며 바로 통화할 수 있습니다!",
                            )
                        else:
                            vis_used = str(res.get("visibility") or "private")
                            messagebox.showinfo(
                                "보이스 자동 생성 · 연결 완료",
                                f"[{nm} ({v_lang})] 목소리가 자동 생성되어 모모톡에 연결되었습니다! ({vis_used}){alt_note}\n"
                                f"Model ID: {mid}\n\n💗 이제 바로 📞 통화 버튼을 눌러 대화해 보세요!",
                            )
                    return

                # Smart end-user recovery flows for distributed PCs
                fcode = str(res.get("fatal_code") or "")
                err_txt = str(res.get("error") or "모델 생성 실패")

                if fcode == "AUTH_ERROR":
                    if messagebox.askyesno(
                        "Fish Audio API Key 확인 필요",
                        f"{err_txt}\n\n설정 창 1단계를 열어 API Key를 확인하시겠습니까?",
                    ):
                        self.open_settings_dialog(first_run=False, initial_tab=1)
                    return

                if fcode == "STEAM_MISSING":
                    pick_choice = messagebox.askyesnocancel(
                        "스팀 블루 아카이브 폴더 안내",
                        "스팀 블루 아카이브 설치 폴더를 자동으로 찾지 못했습니다.\n\n"
                        "• [예(Y)] 직접 `BlueArchive` 설치 폴더 지정하기 (한 번만 지정하면 자동 저장 후 즉시 생성)\n"
                        "• [아니오(N)] 스팀 없이 Fish Audio 공개 보이스 검색하기\n"
                        "• [취소] 닫기",
                    )
                    if pick_choice is True:
                        picked = filedialog.askdirectory(title="스팀 BlueArchive 설치 폴더를 선택해 주세요")
                        if picked:
                            if self._apply_steam_game_dir(picked):
                                self.log(f"✅ [스팀 경로 저장] '{self.cfg.get('steam_game_dir')}' — 목소리 생성을 자동으로 이어서 진행합니다.")
                                self.auto_generate_current_student_voice_async(
                                    target_cid=base_cid,
                                    target_lang=v_lang,
                                    visibility_override=visibility_override,
                                    alt_voice_key=alt_voice_key,
                                    force_recreate=force_recreate,
                                    start_call_after=start_call_after,
                                    show_success_popup=show_success_popup,
                                    on_done=on_done,
                                )
                            else:
                                messagebox.showwarning(
                                    "폴더 확인 필요",
                                    "선택한 폴더 안에서 블루 아카이브 리소스(`BlueArchive_Data/StreamingAssets/...`)를 찾지 못했습니다.\n"
                                    "`.../steamapps/common/BlueArchive` 폴더를 선택해 주세요.",
                                )
                    elif pick_choice is False:
                        self.open_voice_search_dialog()
                    return

                if fcode == "NO_VOICE_PACK":
                    fb_lang = str(res.get("fallback_lang") or "").strip().upper()
                    if fb_lang in ("JP", "KR") and fb_lang != v_lang:
                        fb_lbl = "일본어(JP) 원음" if fb_lang == "JP" else "한국어(KR) 더빙"
                        if messagebox.askyesno(
                            "대체 언어 보이스 생성 안내",
                            f"[{nm}]은(는) 스팀 클라이언트에 {v_lang} 음성 팩이 아직 없습니다.\n\n"
                            f"대신 {fb_lbl}으로 목소리를 자동 생성·연결할까요?\n"
                            "(일본어 원음 모델로도 한국어/일본어 대화를 자연스럽게 발화할 수 있습니다.)",
                        ):
                            self.auto_generate_current_student_voice_async(
                                target_cid=base_cid,
                                target_lang=fb_lang,
                                visibility_override=visibility_override,
                                alt_voice_key=alt_voice_key,
                                force_recreate=force_recreate,
                                start_call_after=start_call_after,
                                show_success_popup=show_success_popup,
                                on_done=on_done,
                            )
                        return
                    if messagebox.askyesno(
                        "보이스 미실장 캐릭터 안내",
                        f"{err_txt}\n\n대신 Fish Audio 공개 보이스 검색기에서 목소리를 찾아 연결할까요?",
                    ):
                        self.open_voice_search_dialog()
                    return

                if fcode == "QUOTA_LIMIT":
                    req_vis = str(res.get("requested_visibility") or "private").strip().lower()
                    if req_vis == "private":
                        q_choice = messagebox.askyesnocancel(
                            "Fish Audio 비공개 슬롯 한도 도달",
                            "Fish Audio 비공개(private) 보이스 슬롯 한도에 도달했습니다.\n"
                            "(Plus 요금제는 비공개 10개 한도 · 공개(public) 무제한)\n\n"
                            "• [예(Y)] 공개(public) 모델로 바로 다시 생성 & 자동 연결하기\n"
                            "• [아니오(N)] 이미 올라간 공개 보이스 검색기 열기\n"
                            "• [취소] 닫기 (추출된 로컬 WAV로 Zero-Shot 발화 가능)",
                        )
                        if q_choice is True:
                            self.auto_generate_current_student_voice_async(
                                target_cid=base_cid,
                                target_lang=v_lang,
                                visibility_override="public",
                                alt_voice_key=alt_voice_key,
                                force_recreate=force_recreate,
                                start_call_after=start_call_after,
                                show_success_popup=show_success_popup,
                                on_done=on_done,
                            )
                        elif q_choice is False:
                            self.open_voice_search_dialog()
                        return

                    comb_wav = Path(str(res.get("combined_wav") or ""))
                    samples_dir = Path(str(res.get("samples_dir") or ""))
                    sample_wavs = sorted(samples_dir.glob("*.wav")) if samples_dir.is_dir() else []
                    target_wav = comb_wav if comb_wav.is_file() else (sample_wavs[0] if sample_wavs else comb_wav)
                    q_choice2 = messagebox.askyesnocancel(
                        "Fish Audio 요금제/크레딧 한도 안내 (WAV 추출 완료)",
                        (
                            f"{err_txt}\n\n"
                            f"[{nm} ({v_lang})] 고품질 스튜디오 WAV는 이미 내 PC에 정상 추출되었습니다!\n\n"
                            "• [예(Y)] 이미 공유된 공개 보이스 검색기 열기\n"
                            "• [아니오(N)] 방금 추출된 WAV 폴더 열기 (웹 업로드 또는 Zero-Shot)\n"
                            "• [취소] 닫기"
                        ),
                    )
                    if q_choice2 is True:
                        self.open_voice_search_dialog()
                    elif q_choice2 is False and target_wav.exists():
                        _reveal_file_in_explorer(target_wav, (get_user_models_dir(), ROOT_DIR / "models"))
                    return

                safe_err_popup = _redact_secrets(err_txt, extra_secrets=self._active_secret_values())
                messagebox.showerror("보이스 자동 생성 실패", safe_err_popup[:380])

            self._schedule_ui(_finish_on_ui)

        threading.Thread(target=_worker, daemon=True, name=f"auto-voice-{slot_cid}-{v_lang}").start()

    def auto_create_aru_model_async(self) -> None:
        """Backwards-compatible entry point invoked from Settings Tab 2."""
        self.auto_generate_current_student_voice_async()

    def _get_trigger_wavs(self) -> dict[str, tuple[np.ndarray, int]]:
        with self.trigger_cache_lock:
            if not self.trigger_wav_cache:
                self.trigger_wav_cache = ensure_trigger_wavs_cached(TRIGGER_CACHE_DIR)
            return self.trigger_wav_cache

    def _preload_trigger_wavs_async(self) -> None:
        def _worker() -> None:
            try:
                wavs = self._get_trigger_wavs()
                self.log(f"[선제 발화 준비] 로컬 SAPI 트리거 음성 {len(wavs)}종 캐시 완료 (과금 0원).")
            except Exception as exc:
                self.log(f"[선제 발화 경고] 트리거 음성 초기화 실패: {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_proactive_settings_changed(self, _event=None) -> None:
        self.save_settings_ui(silent=True)

    def _open_device_stereo_stream(
        self,
        dev_lbl: str,
        preferred_sr: int = FISH_PCM_SR,
        blocksize: int = 512,
    ) -> tuple[sd.OutputStream, int, bool]:
        dev_idx, dev_sr = self.out_map.get(dev_lbl, (None, 44100))
        sr_candidates = (dev_sr, preferred_sr) if "wasapi" in dev_lbl.lower() else (preferred_sr, dev_sr)
        last_exc: Exception | None = None
        for sr in sr_candidates:
            for bs in (blocksize, 1024):
                try:
                    stream = sd.OutputStream(
                        samplerate=int(sr),
                        channels=2,
                        dtype="float32",
                        device=dev_idx,
                        blocksize=bs,
                    )
                    stream.start()
                    self._inc_pa_stream()
                    return stream, int(sr), (int(sr) != preferred_sr)
                except Exception as exc:
                    last_exc = exc
        raise last_exc if last_exc is not None else RuntimeError(f"출력 장치 열기 실패: {dev_lbl}")

    def _play_trigger_oneshot(self, key: str = "nudge_manual") -> None:
        trig_lbl = self.trigger_out_choice_var.get()
        if not trig_lbl:
            self.log("[트리거 안내] 선택된 트리거 스피커 장치가 없습니다.")
            return
        try:
            wavs = self._get_trigger_wavs()
            audio, src_sr = wavs.get(key) or wavs["nudge_manual"]
            vol = float(PROACTIVE_VOL_VALS.get(self.proactive_vol_var.get(), 1.0))
            stream, out_sr, _ = self._open_device_stereo_stream(trig_lbl, preferred_sr=src_sr, blocksize=1024)
            try:
                # 0.60s inaudible ultrasonic pilot (-94dB) wakes NVIDIA HDMI/DP monitor DAC cleanly
                n_warm = max(2, (int(out_sr * 0.60) // 2) * 2)
                warm_1d = np.tile(np.array([2e-5, -2e-5], dtype=np.float32), n_warm // 2)
                stream.write(np.column_stack([warm_1d, warm_1d]))
                sig = np.clip(audio * vol, -0.98, 0.98).astype(np.float32)
                if out_sr != src_sr:
                    g = gcd(src_sr, out_sr)
                    sig = np.clip(_resample_pcm_1d(sig, out_sr // g, src_sr // g), -0.98, 0.98).astype(np.float32)
                stream.write(np.column_stack([sig, sig]))
                stream.write(np.zeros((int(out_sr * 0.15), 2), dtype=np.float32))
            finally:
                self._close_pa_stream(stream)
            self.log(f"📢 [수동 트리거 송출 완료] '{trig_lbl}' @ {out_sr}Hz (볼륨 {int(vol * 100)}%)")
        except Exception as exc:
            self.log(f"[트리거 송출 오류] {exc}")

    def trigger_manual_nudge_async(self) -> None:
        if not self.running:
            self.refresh_audio_devices(reinit_portaudio=True, log_info=False)
        self.save_settings_ui(silent=True)
        if self.running:
            self.watchdog.record_manual_nudge(time.monotonic())
            try:
                self.trigger_play_queue.put_nowait("nudge_manual")
                self.log("💬 [수동 선제 발화] 보조 스피커로 대화 리드 트리거를 송출합니다!")
            except queue.Full:
                pass
        else:
            threading.Thread(target=self._play_trigger_oneshot, args=("nudge_manual",), daemon=True).start()

    def test_aru_tts_async(self) -> None:
        if not self.running:
            self.refresh_audio_devices(reinit_portaudio=True, log_info=False)
        self.save_settings_ui(silent=True)
        if not self.cfg["fish_api_key"]:
            messagebox.showwarning("API Key 필요", "먼저 Fish Audio API Key를 입력해 주세요!")
            return
        c = self.current_char
        persona = c.get("persona") or {}
        if self._get_reply_language() == "ko":
            user_title = str(c.get("user_title") or "선생님").strip()
            sm = persona.get("speech_mechanics_jp") or {}
            smode = str(sm.get("speech_mode") or "")
            base_reg = str(sm.get("base_register") or "")
            summary = str(persona.get("summary") or "")
            is_casual = ("tameguchi" in smode) or ("タメ口" in base_reg) or ("반말" in summary)
            sample_line = (
                persona.get("sample_tts_line_kr")
                or (
                    f"{user_title}, 기다리고 있었어! 오늘도 나랑 즐겁게 얘기하자!"
                    if is_casual
                    else f"{user_title}, 기다리고 있었어요! 오늘도 잘 부탁드려요!"
                )
            )
        else:
            sample_line = (
                persona.get("sample_tts_line_jp")
                or (persona.get("verbatim_reference_quotes_jp") or [""])[0]
                or "先生、今日もよろしくお願いしますね！"
            )
        if self.running:
            self.tts_queue.put((0, sample_line, time.time(), 0.12, True))
        else:
            threading.Thread(target=self._synthesize_and_play_oneshot, args=(sample_line,), daemon=True).start()

    def test_alt_voice_tts_async(self, alt_key: str) -> None:
        """Play a sample line using an alt voice persona (e.g. Kisaki's `kiki` mode) right from Settings."""
        self.save_settings_ui(silent=True)
        if not self.cfg.get("fish_api_key"):
            messagebox.showwarning("API Key 필요", "먼저 Fish Audio API Key를 입력해 주세요!")
            return
        c = self.current_char
        alts = c.get("alt_voice_personas") if isinstance(c, dict) else None
        spec = (alts or {}).get(alt_key) if isinstance(alts, dict) else None
        if not isinstance(spec, dict):
            return
        if self._get_reply_language() == "ko":
            sample_line = str(spec.get("sample_tts_line_kr") or "안녕하세요, 선생님! 키키예요!")
        else:
            sample_line = str(spec.get("sample_tts_line_jp") or "先生こんにちは！キキです！")
        tagged = f"[VOICE:{alt_key}] {sample_line}"
        if self.running:
            self.tts_queue.put((0, sample_line, time.time(), 0.12, True, None, alt_key))
        else:
            threading.Thread(target=self._synthesize_and_play_oneshot, args=(tagged,), daemon=True).start()

    def _build_fish_request(
        self,
        text: str,
        latency_mode: str = "balanced",
        clause_rms: float = 0.0,
        voice_persona_key: str = "",
    ) -> tuple[dict[str, str], bytes, str, dict[str, Any]]:
        api_key = self.cfg.get("fish_api_key", "").strip()
        base_char = self.current_char
        base_cid = str(base_char.get("id") or self.current_char_id)
        rlang = self._get_reply_language()
        v_lang = self._voice_lang_code()

        # Resolve sentence-level alt voice switching (e.g. Kisaki <-> Kiki via `[VOICE:kiki]` or smart fallback)
        det_vkey, clean_input_text = resolve_sentence_voice_persona(base_char, text)
        eff_vkey = (voice_persona_key or det_vkey or "").strip().lower()
        alt_char = build_alt_voice_char_data(base_char, eff_vkey) if eff_vkey else None
        c = alt_char if alt_char is not None else base_char
        cid = str(c.get("id") or base_cid)
        alt_spec = ((base_char.get("alt_voice_personas") or {}).get(eff_vkey) or {}) if alt_char is not None else {}
        voice_persona_label = str(alt_spec.get("label_kr") or eff_vkey).split("(")[0].strip() if alt_char is not None else ""

        if alt_char is not None:
            ref_id = self._get_saved_voice_id(cid, reply_lang=rlang, allow_jp_fallback=False)
            if not ref_id and rlang == "ko":
                # Prefer local KR Zero-Shot pure alt WAV if available; else fall back to alt JP model
                alt_comb_kr, alt_sdir_kr = self._char_ref_paths(cid, lang="KR")
                has_local_kr = alt_comb_kr.exists() or (alt_sdir_kr.exists() and any(alt_sdir_kr.glob("*.wav")))
                if not has_local_kr:
                    alt_jp_id = self._get_saved_voice_id(cid, reply_lang="ja", allow_jp_fallback=False)
                    if alt_jp_id:
                        ref_id = alt_jp_id
        else:
            ref_id = extract_model_id(self.cfg.get("fish_reference_id", ""))
            if not ref_id and rlang == "ko":
                kr_saved = self._get_saved_voice_id(cid, reply_lang="ko", allow_jp_fallback=False)
                if kr_saved:
                    ref_id = kr_saved
                else:
                    jp_fallback = self._get_saved_voice_id(cid, reply_lang="ja", allow_jp_fallback=False)
                    if jp_fallback:
                        ref_id = jp_fallback
                        if cid not in self._kr_fallback_warned:
                            self._kr_fallback_warned.add(cid)
                            self.log(
                                f"💡 [한국어 보이스 안내] '{c.get('name_kr', cid)}' 한국어 전용 모델이 아직 없어 기존 일본어 모델로 한국어를 발화합니다. "
                                "한국어 성우 더빙 원음을 쓰려면 [⚙️ 설정 → 1단계 보이스 자동 생성]을 눌러주세요!"
                            )
        model_hdr = self.cfg.get("fish_model_header", "s2.1-pro-free")
        auto_dir = bool(self.auto_emotion_var.get())
        boost_val = float(BOOST_LABEL_TO_VAL.get(self.boost_preset_var.get(), 0.0))
        p_var = PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")

        use_custom_non_jp = bool(self.cfg.get("use_custom_persona", False)) and not bool(_KANA_RE.search(clean_input_text))
        if use_custom_non_jp:
            clean_custom = _CUSTOM_MD_STRIP_RE.sub("", clean_input_text).strip()
            dir_info = {
                "clean_text": clean_custom,
                "tts_text": clean_custom,
                "s2_tag": "",
                "cue": "custom_persona",
                "temperature": 0.72,
                "top_p": 0.78,
                "repetition_penalty": 1.12,
            }
        else:
            dir_info = direct_character_emotion_and_prosody(
                c,
                clean_input_text,
                clause_rms=clause_rms,
                auto_direct=auto_dir,
                boost_offset=boost_val,
                model_hdr=model_hdr,
                persona_variant=p_var,
                jp_beginner_coach=bool(self.cfg.get("jp_beginner_lover_mode", False)) and (rlang == "ja"),
            )
        dir_info["voice_persona_key"] = eff_vkey if alt_char is not None else ""
        dir_info["voice_persona_label"] = voice_persona_label
        dir_info["voice_slot_id"] = cid
        tts_text = dir_info["tts_text"]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "model": model_hdr,
        }
        chunk_len = 250 if latency_mode == "normal" else 200
        if ref_id:
            headers["Content-Type"] = "application/json"
            payload_bytes = json.dumps(
                {
                    "text": tts_text,
                    "reference_id": ref_id,
                    "format": "pcm",
                    "sample_rate": FISH_PCM_SR,
                    "chunk_length": chunk_len,
                    "normalize": True,
                    "latency": latency_mode,
                    "temperature": dir_info["temperature"],
                    "top_p": dir_info["top_p"],
                    "repetition_penalty": dir_info["repetition_penalty"],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            mode_tag = f"ID:{ref_id[:8]}"
        else:
            comb_wav, samples_dir = self._char_ref_paths(cid, lang=v_lang)
            sample_wavs = sorted(samples_dir.glob("*.wav")) if samples_dir.exists() else []
            if not sample_wavs and not comb_wav.exists():
                eff_gdir = self._get_resolved_ba_game_dir()
                try:
                    res = build_character_fish_reference(c, lang=v_lang, game_dir=eff_gdir)
                    sample_wavs = res["sample_files"]
                    comb_wav = res["combined_wav"]
                except Exception:
                    if v_lang == "KR":
                        comb_wav, samples_dir = self._char_ref_paths(cid, lang="JP")
                        sample_wavs = sorted(samples_dir.glob("*.wav")) if samples_dir.exists() else []
                        if not sample_wavs and not comb_wav.exists():
                            try:
                                res = build_character_fish_reference(c, lang="JP", game_dir=eff_gdir)
                                sample_wavs = res["sample_files"]
                                comb_wav = res["combined_wav"]
                            except Exception:
                                if alt_char is None:
                                    raise
                    elif alt_char is None:
                        raise

            # Safety fallback for alt voice if neither alt model ID nor Steam alt WAV exists: fall back to base voice ID
            if alt_char is not None and not sample_wavs and not comb_wav.exists():
                base_ref_id = extract_model_id(self.cfg.get("fish_reference_id", "")) or self._get_saved_voice_id(
                    base_cid, reply_lang=rlang, allow_jp_fallback=True
                )
                if base_ref_id:
                    headers["Content-Type"] = "application/json"
                    payload_bytes = json.dumps(
                        {
                            "text": tts_text,
                            "reference_id": base_ref_id,
                            "format": "pcm",
                            "sample_rate": FISH_PCM_SR,
                            "chunk_length": chunk_len,
                            "normalize": True,
                            "latency": latency_mode,
                            "temperature": dir_info["temperature"],
                            "top_p": dir_info["top_p"],
                            "repetition_penalty": dir_info["repetition_penalty"],
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    return headers, payload_bytes, f"ID:{base_ref_id[:8]}(base-fb)", dir_info

            transcript_map: dict[str, str] = {}
            tr_json = samples_dir / "transcripts.json"
            if tr_json.exists():
                try:
                    transcript_map = json.loads(tr_json.read_text(encoding="utf-8"))
                except Exception:
                    transcript_map = {}

            # Send up to 3 cleanest studio clips (~15-22s total) with verbatim transcripts if available;
            # otherwise fall back to the stitched studio reference WAV (far more stable than 1 short 5s clip).
            ref_wavs = sample_wavs[:3] if sample_wavs else ([comb_wav] if comb_wav.exists() else [])
            references_payload = []
            for ref_wav in ref_wavs:
                if ref_wav.is_symlink() or not ref_wav.is_file():
                    continue
                st_wav = ref_wav.stat()
                if st_wav.st_size > MAX_ZEROSHOT_WAV_BYTES:
                    continue
                ref_key = str(ref_wav)
                ref_mtime = int(st_wav.st_mtime_ns)
                cached_wav = self._zero_shot_wav_cache.get(ref_key)
                if cached_wav is not None and cached_wav[0] == ref_mtime:
                    wav_bytes = cached_wav[1]
                else:
                    wav_bytes = ref_wav.read_bytes()
                    if len(self._zero_shot_wav_cache) >= 16:
                        self._zero_shot_wav_cache.clear()
                    self._zero_shot_wav_cache[ref_key] = (ref_mtime, wav_bytes)
                ref_txt = str(transcript_map.get(ref_wav.name) or transcript_map.get(ref_wav.stem) or "").strip()
                references_payload.append({"audio": wav_bytes, "text": ref_txt})

            headers["Content-Type"] = "application/msgpack"
            payload_bytes = msgpack.packb(
                {
                    "text": tts_text,
                    "references": references_payload,
                    "format": "pcm",
                    "sample_rate": FISH_PCM_SR,
                    "chunk_length": chunk_len,
                    "normalize": True,
                    "latency": latency_mode,
                    "temperature": dir_info["temperature"],
                    "top_p": dir_info["top_p"],
                    "repetition_penalty": dir_info["repetition_penalty"],
                },
                use_bin_type=True,
            )
            mode_tag = f"Zero-Shot({len(references_payload)}clip)"
        return headers, payload_bytes, mode_tag, dir_info

    def _open_stereo_output_stream(self) -> tuple[sd.OutputStream, int, bool]:
        out_lbl = self.out_choice_var.get()
        return self._open_device_stereo_stream(out_lbl, preferred_sr=FISH_PCM_SR, blocksize=512)

    def _synthesize_and_play_oneshot(self, text: str) -> None:
        self.stop_playback_flag.clear()
        t_start = time.time()
        c = self.current_char
        cname = c.get("name_kr", self.current_char_id)
        stream: sd.OutputStream | None = None
        try:
            headers, payload_bytes, mode_tag, dir_info = self._build_fish_request(text, latency_mode="balanced", clause_rms=0.12)
            clean_jp = str(dir_info.get("clean_text") or "").strip()
            sub_bundle = self._prepare_subtitle_bundle(clean_jp, c, is_turn_start=True) if clean_jp else None
            stream, play_sr, need_resample = self._open_stereo_output_stream()
            first_chunk = True
            first_emitted = True
            leftover = b""
            vol = float(self.cfg.get("output_volume", 1.0) or 1.0)
            pcm_scale = np.float32(vol / 32768.0)
            resampler = StreamingPolyphaseResampler(FISH_PCM_SR, play_sr) if need_resample else None
            fade_tail_n = max(4, int(play_sr * 0.004))
            tail_hold = np.empty(0, dtype=np.float32)

            tts_url = _validate_fish_api_url(FISH_TTS_URL)
            total_pcm_bytes = 0
            with self.http_client.stream("POST", tts_url, headers=headers, content=payload_bytes) as resp:
                self.last_http_activity = time.time()
                if resp.status_code != 200:
                    err_body = _read_bounded_httpx_error(resp, MAX_HTTP_ERROR_BYTES)
                    self.log(f"[Fish TTS 오류 HTTP {resp.status_code}] {err_body[:250]}")
                    return

                for chunk in resp.iter_bytes(chunk_size=2048):
                    if self.stop_playback_flag.is_set():
                        break
                    if not chunk:
                        continue
                    total_pcm_bytes += len(chunk)
                    if total_pcm_bytes > MAX_TTS_STREAM_BYTES:
                        self.log(f"[Fish TTS 보안 경고] 단일 스트림 크기 한도({MAX_TTS_STREAM_BYTES} bytes) 초과로 중단했습니다.")
                        break
                    if first_chunk:
                        first_chunk = False
                        ttfa = time.time() - t_start
                        act_lbl = f"{dir_info['s2_tag']} " if dir_info["s2_tag"] else ""
                        vp_lbl = f" 🎭{dir_info['voice_persona_label']}" if dir_info.get("voice_persona_label") else ""
                        self.log(
                            f"🔊 [{cname}{vp_lbl} 발화 ({act_lbl}temp={dir_info['temperature']:.2f}, {ttfa:.2f}s, {mode_tag})] {dir_info['clean_text']}"
                        )
                        if sub_bundle is not None:
                            self._schedule_subtitle_from_playback(sub_bundle)

                    buf = leftover + chunk
                    even_len = len(buf) & ~1
                    if even_len == 0:
                        leftover = buf
                        continue
                    leftover = buf[even_len:]

                    pcm_i16 = np.frombuffer(buf[:even_len], dtype=np.int16)
                    audio_f32 = pcm_i16.astype(np.float32)
                    audio_f32 *= pcm_scale
                    if resampler is not None:
                        audio_f32 = resampler.process(audio_f32)
                        if audio_f32.size == 0:
                            continue
                    else:
                        if tail_hold.size:
                            audio_f32 = np.concatenate([tail_hold, audio_f32])
                        if audio_f32.size <= fade_tail_n:
                            tail_hold = audio_f32
                            continue
                        tail_hold = audio_f32[-fade_tail_n:]
                        audio_f32 = audio_f32[:-fade_tail_n]
                    if first_emitted:
                        audio_f32 = _apply_micro_fade_1d(audio_f32, play_sr, fade_in=True, fade_ms=4.0)
                        first_emitted = False
                    np.clip(audio_f32, -0.985, 0.985, out=audio_f32)
                    stream.write(_mono_to_stereo(audio_f32))

            if not self.stop_playback_flag.is_set():
                tail_f32 = resampler.flush() if resampler is not None else tail_hold
                if tail_f32.size > 0:
                    tail_f32 = _apply_micro_fade_1d(
                        tail_f32, play_sr, fade_in=first_emitted, fade_out=True, fade_ms=4.0
                    )
                    np.clip(tail_f32, -0.985, 0.985, out=tail_f32)
                    stream.write(_mono_to_stereo(tail_f32))
                stream.write(np.zeros((int(play_sr * 0.12), 2), dtype=np.float32))
        except Exception as exc:
            self.log(f"[재생/레퍼런스 오류] {exc}")
        finally:
            self._close_pa_stream(stream)

    def toggle_bridge(self) -> None:
        if self.companion_running:
            self.toggle_companion_mode()
        cname = self.current_char.get("name_kr", self.current_char_id)
        if self.running:
            self.running = False
            self.abort_current_playback()
            if self.tts_executor is not None:
                try:
                    self.tts_executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                self.tts_executor = None
            self.btn_toggle.config(text=f"▶️ 실시간 [{cname}] 음성 브릿지 시작", bg="#16a34a")
            self.lbl_meter.config(text="중지됨 — 다시 시작하려면 위 초록색 시작 버튼을 눌러주세요", fg="#64748b")
            self.lbl_proactive_status.config(text="🤖 선제 발화: 중지됨", fg="#64748b")
            self.log("[중지] 실시간 브릿지를 중지했습니다.")
            return

        self.refresh_audio_devices(reinit_portaudio=True, log_info=False)
        self.save_settings_ui(silent=True)
        if not self.cfg["fish_api_key"]:
            messagebox.showwarning("API Key 필요", "먼저 Fish Audio API Key를 입력해 주세요!")
            return
        if self.whisper_model is None:
            messagebox.showinfo("잠시만 기다려 주세요", "로컬 Whisper 모델이 아직 초기화 중입니다. 2~3초 뒤 다시 눌러 주세요.")
            return

        for q in (self.utterance_queue, self.trigger_play_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self.abort_current_playback()
        self.stop_playback_flag.clear()
        self._sticky_ja_hits = 0
        self._active_capture_speaking = False
        self._active_stt_busy = False
        with self._active_tts_lock:
            self._active_tts_inflight = 0
        self._active_audio_playing_until = 0.0
        self.watchdog.reset(
            enabled=bool(self.proactive_enable_var.get()),
            silence_timeout_sec=float(PROACTIVE_TIMEOUT_VALS.get(self.proactive_timeout_var.get(), 18.0)),
        )
        self.tts_executor = ThreadPoolExecutor(max_workers=2)

        self.running = True
        self.btn_toggle.config(text=f"⏹ [{cname}] 실시간 브릿지 작동 중 (클릭 시 중지)", bg="#dc2626")
        self.lbl_meter.config(text="░░░░░░░░░░░░ (강도: 0.000) — Gemini Live 소리 대기 중...", fg="#2563eb")
        out_lbl = self.out_choice_var.get() or "미지정"
        trig_lbl = self.trigger_out_choice_var.get() or "미지정"
        if self.proactive_enable_var.get():
            self.lbl_proactive_status.config(text="🤖 선제 발화: 첫 대화 대기 중", fg="#0f766e")
        else:
            self.lbl_proactive_status.config(text="🤖 선제 발화: 꺼짐", fg="#64748b")
        vap = (self.current_char.get("persona") or {}).get("voice_acting_profile") or {}
        arch_kr = vap.get("archetype_kr", "맞춤형")
        self.log(
            f"[실시간 가동] '{cname}' ({arch_kr}) 맞춤 감정·호흡 디렉팅 모드로 변환을 시작합니다! "
            f"(출력: {out_lbl} | 트리거: {trig_lbl})"
        )

        threading.Thread(target=self._audio_player_loop, daemon=True).start()
        threading.Thread(target=self._clause_order_drainer_loop, daemon=True).start()
        threading.Thread(target=self._tts_prefetch_loop, daemon=True).start()
        threading.Thread(target=self._stt_loop, daemon=True).start()
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._trigger_player_loop, daemon=True).start()
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

    def _abort_bridge_on_device_error(self, err_msg: str) -> None:
        self.log(err_msg)
        self.running = False
        if self.companion_running:
            self.companion_running = False
            self.mic_stt.stop()
            self.companion_sm.stop_session()
        self._ringback_state.stop()
        self.abort_current_playback()
        cname = self.current_char.get("name_kr", self.current_char_id)

        def _reset_ui() -> None:
            try:
                self._set_call_ui_state("idle")
                self.btn_toggle.config(text=f"▶️ 실시간 [{cname}] 음성 브릿지 시작", bg="#16a34a")
                self.lbl_meter.config(text=f"⚠️ {err_msg}", fg="#dc2626")
                self.lbl_proactive_status.config(text="🤖 선제 발화: 장치 오류로 중지됨", fg="#dc2626")
                if hasattr(self, "btn_companion_toggle"):
                    self.btn_companion_toggle.config(text="▶️ AI 보이스 대화 시작", bg="#16a34a")
                    self.companion_state_var.set(f"⚠️ 장치 오류: {err_msg}")
            except Exception:
                pass

        try:
            self.root.after(0, _reset_ui)
        except Exception:
            pass

    def _audio_player_loop(self) -> None:
        """Keep Stereo OutputStream open with live switching & USB-unplug recovery."""
        stream: sd.OutputStream | None = None
        play_sr = FISH_PCM_SR
        idle_zeros = np.zeros((int(play_sr * 0.008), 2), dtype=np.float32)
        active_lbl = ""

        def _primary_candidates(preferred_lbl: str) -> list[tuple[str, str]]:
            all_outs = list(self.out_map.keys())
            api_order = lambda s: (0 if "mme" in s.lower() else (1 if "directsound" in s.lower() else 2))
            seen: set[str] = set()
            cands: list[tuple[str, str]] = []

            def _add_with_alts(ui_lbl: str) -> None:
                if not ui_lbl:
                    return
                if ui_lbl in self.out_map and ui_lbl not in seen:
                    seen.add(ui_lbl)
                    cands.append((ui_lbl, ui_lbl))
                alts = [
                    alt
                    for alt in all_outs
                    if alt != ui_lbl
                    and alt not in seen
                    and match_device_label_by_name(ui_lbl, [alt], require_same_api=False) is not None
                ]
                alts.sort(key=api_order)
                for alt in alts:
                    seen.add(alt)
                    cands.append((alt, ui_lbl))

            _add_with_alts(preferred_lbl)
            for p_lbl in self.primary_outputs:
                if p_lbl != preferred_lbl:
                    _add_with_alts(p_lbl)
            return cands

        def _open_primary(lbl: str) -> bool:
            nonlocal stream, play_sr, idle_zeros, active_lbl
            self._close_pa_stream(stream)
            stream = None
            last_exc: Exception | None = None
            for open_lbl, ui_lbl in _primary_candidates(lbl):
                try:
                    stream, play_sr, _ = self._open_device_stereo_stream(
                        open_lbl, preferred_sr=FISH_PCM_SR, blocksize=512
                    )
                    idle_zeros = np.zeros((int(play_sr * 0.008), 2), dtype=np.float32)
                    self._active_play_sr = play_sr
                    self._active_need_resample = play_sr != FISH_PCM_SR
                    active_lbl = ui_lbl
                    if ui_lbl != lbl:
                        try:
                            self.root.after(0, lambda v=ui_lbl: self.out_choice_var.set(v))
                        except Exception:
                            pass
                    self.log(f"🎧 [캐릭터 음성 출력 연결] '{open_lbl}' @ {play_sr}Hz 준비 완료")
                    return True
                except Exception as exc:
                    last_exc = exc
            self.log(f"[출력 장치 오류] {last_exc or lbl}")
            return False

        if not _open_primary(self.out_choice_var.get()):
            self._abort_bridge_on_device_error("[출력 장치 오류] 사용 가능한 스피커/이어폰을 열지 못했습니다.")
            return

        try:
            while self._is_audio_pipeline_active():
                desired_lbl = self.out_choice_var.get()
                if desired_lbl and desired_lbl != active_lbl:
                    if not _open_primary(desired_lbl):
                        self._abort_bridge_on_device_error(f"[출력 장치 오류] '{desired_lbl}' 전환 실패")
                        break

                try:
                    q_wait = 0.022 if time.monotonic() < self._active_audio_playing_until else 0.008
                    gen_id, stereo_chunk = self.pcm_play_queue.get(timeout=q_wait)
                    if gen_id != self.playback_gen_id or self.stop_playback_flag.is_set():
                        continue
                    if self._call_ui_state == "calling":
                        self._complete_call_connection_from_audio_thread(stream, play_sr)
                    if isinstance(stereo_chunk, dict):
                        if stereo_chunk.get("type") == "subtitle_start":
                            self._schedule_subtitle_from_playback(stereo_chunk)
                        continue
                    now_mono = time.monotonic()
                    self._active_audio_playing_until = now_mono + 0.15
                    self.watchdog.on_playback_active(now_mono)
                    if stream is not None:
                        stream.write(stereo_chunk * 0.25 if self._barge_in_ducking else stereo_chunk)
                except queue.Empty:
                    if stream is not None:
                        if self._call_ui_state == "calling" and self._ringback_state.is_active:
                            vol = float(self.vol_var.get() or 1.0)
                            ring_chunk = self._ringback_state.next_chunk(len(idle_zeros), play_sr, volume=vol)
                            stream.write(ring_chunk)
                            if (
                                self.running
                                and self._mode2_capture_ready
                                and (time.monotonic() - self._call_started_mono) >= 1.8
                            ):
                                self._complete_call_connection_from_audio_thread(stream, play_sr)
                        else:
                            if self._call_ui_state == "calling" and self.running and self._mode2_capture_ready and (
                                time.monotonic() - self._call_started_mono
                            ) >= 1.8:
                                self._complete_call_connection_from_audio_thread(stream, play_sr)
                            stream.write(idle_zeros)
                except Exception as write_exc:
                    self.log(f"[출력 스트림 복구 시도] {write_exc}")
                    self.refresh_audio_devices(reinit_portaudio=False, log_info=False)
                    if not _open_primary(self.out_choice_var.get()):
                        self._abort_bridge_on_device_error(f"[출력 스트림 오류] {write_exc}")
                        break
        finally:
            self._close_pa_stream(stream)

    def _synthesize_clause_to_queue(
        self,
        my_gen: int,
        clause_no: int,
        text: str,
        t_captured: float,
        clause_rms: float,
        is_opening: bool,
        clause_pcm_q: queue.Queue[Any],
        pre_kr_future: Future[str] | None = None,
        voice_persona_key: str = "",
    ) -> None:
        with self._active_tts_lock:
            self._active_tts_inflight += 1
        try:
            if (not self._is_audio_pipeline_active()) or my_gen != self.playback_gen_id or self.stop_playback_flag.is_set():
                return

            speed_code = SPEED_LABEL_TO_CODE.get(self.speed_mode_var.get(), "expressive")
            fish_lat = "normal" if speed_code == "full_turn" else "balanced"
            c = self.current_char
            cname = c.get("name_kr", self.current_char_id)
            t_req = time.time()

            headers, payload_bytes, _, dir_info = self._build_fish_request(
                text,
                latency_mode=fish_lat,
                clause_rms=clause_rms,
                voice_persona_key=voice_persona_key,
            )
            clean_jp = str(dir_info.get("clean_text") or "").strip()
            sub_bundle = (
                self._prepare_subtitle_bundle(
                    clean_jp,
                    c,
                    is_turn_start=bool(is_opening),
                    pre_kr_future=pre_kr_future,
                )
                if clean_jp
                else None
            )
            play_sr = getattr(self, "_active_play_sr", FISH_PCM_SR)
            need_resample = getattr(self, "_active_need_resample", False)
            vol = float(self.cfg.get("output_volume", 1.0) or 1.0)
            pcm_scale = np.float32(vol / 32768.0)
            resampler = StreamingPolyphaseResampler(FISH_PCM_SR, play_sr) if need_resample else None
            fade_tail_n = max(4, int(play_sr * 0.004))
            tail_hold = np.empty(0, dtype=np.float32)

            first_chunk = True
            first_emitted = True
            leftover = b""
            tts_url = _validate_fish_api_url(FISH_TTS_URL)
            total_pcm_bytes = 0
            with self.http_client.stream("POST", tts_url, headers=headers, content=payload_bytes) as resp:
                self.last_http_activity = time.time()
                if resp.status_code != 200:
                    err_body = _read_bounded_httpx_error(resp, MAX_HTTP_ERROR_BYTES)
                    self.log(f"[Fish TTS 오류 HTTP {resp.status_code}] {err_body[:220]}")
                    return

                for chunk in resp.iter_bytes(chunk_size=2048):
                    if (not self._is_audio_pipeline_active()) or my_gen != self.playback_gen_id or self.stop_playback_flag.is_set():
                        break
                    if not chunk:
                        continue
                    total_pcm_bytes += len(chunk)
                    if total_pcm_bytes > MAX_TTS_STREAM_BYTES:
                        self.log(f"[Fish TTS 보안 경고] 단일 스트림 크기 한도({MAX_TTS_STREAM_BYTES} bytes) 초과로 중단했습니다.")
                        break
                    if first_chunk:
                        first_chunk = False
                        ttfa = time.time() - t_req
                        e2e = time.time() - t_captured
                        act_lbl = f"{dir_info['s2_tag']} " if dir_info["s2_tag"] else ""
                        vp_lbl = f" 🎭{dir_info['voice_persona_label']}" if dir_info.get("voice_persona_label") else ""
                        lat_badge = "⚡" if fish_lat == "balanced" else "✨"
                        self.log(
                            f"🔊 [{cname}{vp_lbl} 발화 #{clause_no} ({act_lbl}temp={dir_info['temperature']:.2f} | {lat_badge}TTS {ttfa:.2f}s | 총 {e2e:.2f}s)] {dir_info['clean_text']}"
                        )
                        if sub_bundle is not None:
                            clause_pcm_q.put(sub_bundle)

                    buf = leftover + chunk
                    even_len = len(buf) & ~1
                    if even_len == 0:
                        leftover = buf
                        continue
                    leftover = buf[even_len:]

                    pcm_i16 = np.frombuffer(buf[:even_len], dtype=np.int16)
                    audio_f32 = pcm_i16.astype(np.float32)
                    audio_f32 *= pcm_scale
                    if resampler is not None:
                        audio_f32 = resampler.process(audio_f32)
                        if audio_f32.size == 0:
                            continue
                    else:
                        if tail_hold.size:
                            audio_f32 = np.concatenate([tail_hold, audio_f32])
                        if audio_f32.size <= fade_tail_n:
                            tail_hold = audio_f32
                            continue
                        tail_hold = audio_f32[-fade_tail_n:]
                        audio_f32 = audio_f32[:-fade_tail_n]
                    if first_emitted:
                        audio_f32 = _apply_micro_fade_1d(audio_f32, play_sr, fade_in=True, fade_ms=4.0)
                        first_emitted = False
                    np.clip(audio_f32, -0.985, 0.985, out=audio_f32)
                    clause_pcm_q.put(_mono_to_stereo(audio_f32))

            if self._is_audio_pipeline_active() and my_gen == self.playback_gen_id and not self.stop_playback_flag.is_set():
                tail_f32 = resampler.flush() if resampler is not None else tail_hold
                if tail_f32.size > 0:
                    tail_f32 = _apply_micro_fade_1d(
                        tail_f32, play_sr, fade_in=first_emitted, fade_out=True, fade_ms=4.0
                    )
                    np.clip(tail_f32, -0.985, 0.985, out=tail_f32)
                    clause_pcm_q.put(_mono_to_stereo(tail_f32))
                clause_pcm_q.put(np.zeros((int(play_sr * 0.025), 2), dtype=np.float32))
        except Exception as exc:
            self.log(f"[TTS 스트리밍 오류] {exc}")
        finally:
            clause_pcm_q.put(None)
            with self._active_tts_lock:
                self._active_tts_inflight = max(0, self._active_tts_inflight - 1)

    def _clause_order_drainer_loop(self) -> None:
        """Drain parallel-synthesized clause queues strictly in clause arrival order into pcm_play_queue."""
        while self._is_audio_pipeline_active():
            try:
                my_gen, _clause_no, clause_pcm_q = self.ordered_clause_queue.get(timeout=0.04)
            except queue.Empty:
                continue

            while self._is_audio_pipeline_active():
                if my_gen != self.playback_gen_id or self.stop_playback_flag.is_set():
                    break
                try:
                    item = clause_pcm_q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    break
                self.pcm_play_queue.put((my_gen, item))

    def _tts_prefetch_loop(self) -> None:
        """Submit Clause N+1 to 2-worker pool immediately WHILE Clause N is still streaming/playing!"""
        while self._is_audio_pipeline_active():
            try:
                raw_item = self.tts_queue.get(timeout=0.04)
            except queue.Empty:
                continue

            if len(raw_item) >= 7:
                clause_no, text, t_captured, clause_rms, is_opening, pre_kr_future, voice_persona_key = raw_item[:7]
            elif len(raw_item) == 6:
                clause_no, text, t_captured, clause_rms, is_opening, pre_kr_future = raw_item
                voice_persona_key = ""
            else:
                clause_no, text, t_captured, clause_rms, is_opening = raw_item
                pre_kr_future = None
                voice_persona_key = ""

            my_gen = self.playback_gen_id
            self.stop_playback_flag.clear()
            clause_pcm_q: queue.Queue[Any] = queue.Queue()
            self.ordered_clause_queue.put((my_gen, clause_no, clause_pcm_q))
            ex = self.tts_executor
            if ex is not None:
                ex.submit(
                    self._synthesize_clause_to_queue,
                    my_gen,
                    clause_no,
                    text,
                    t_captured,
                    clause_rms,
                    is_opening,
                    clause_pcm_q,
                    pre_kr_future,
                    voice_persona_key,
                )

    def _trigger_player_loop(self) -> None:
        """Keep secondary monitor speaker (ANMITE) DAC awake via ultrasonic pilot and emit cached SAPI cues."""
        active_lbl = ""
        stream: sd.OutputStream | None = None
        out_sr = 48000
        idle_pilot = np.zeros((512, 2), dtype=np.float32)
        chunk_frames = 1024

        def _open_for(lbl: str) -> bool:
            nonlocal active_lbl, stream, out_sr, idle_pilot, chunk_frames
            self._close_pa_stream(stream)
            stream = None
            if not lbl:
                active_lbl = ""
                return False
            try:
                wavs = self._get_trigger_wavs()
                sample_sr = next(iter(wavs.values()))[1]
                stream, out_sr, _ = self._open_device_stereo_stream(lbl, preferred_sr=sample_sr, blocksize=1024)
                active_lbl = lbl
                n_idle = max(4, (int(out_sr * 0.016) // 2) * 2)
                # -94dB Nyquist pilot keeps NVIDIA HDMI/DP audio link awake (0ms wake-up clipping, 0dB audible noise)
                pilot_1d = np.tile(np.array([2e-5, -2e-5], dtype=np.float32), n_idle // 2)
                idle_pilot = np.column_stack([pilot_1d, pilot_1d])
                chunk_frames = max(512, int(out_sr * 0.04))
                self.log(f"🔊 [트리거 스피커 대기 연결] '{lbl}' @ {out_sr}Hz 준비 완료")
                return True
            except Exception as exc:
                self.log(f"[트리거 스피커 경고] '{lbl}' 열기 실패 (선제 발화 비활성): {exc}")
                active_lbl = lbl
                return False

        try:
            _open_for(self.trigger_out_choice_var.get())
            while self.running:
                desired_lbl = self.trigger_out_choice_var.get()
                if desired_lbl != active_lbl:
                    _open_for(desired_lbl)

                try:
                    key = self.trigger_play_queue.get(timeout=0.015)
                except queue.Empty:
                    if stream is not None:
                        stream.write(idle_pilot)
                    else:
                        time.sleep(0.05)
                    continue

                if stream is None:
                    continue
                wavs = self._get_trigger_wavs()
                audio, src_sr = wavs.get(key) or wavs["nudge_1"]
                vol = float(PROACTIVE_VOL_VALS.get(self.proactive_vol_var.get(), 1.0))
                sig = np.clip(audio * vol, -0.98, 0.98).astype(np.float32)
                if out_sr != src_sr:
                    g = gcd(src_sr, out_sr)
                    sig = np.clip(_resample_pcm_1d(sig, out_sr // g, src_sr // g), -0.98, 0.98).astype(np.float32)
                stereo = np.column_stack([sig, sig])
                for pos in range(0, len(stereo), chunk_frames):
                    if not self.running:
                        break
                    stream.write(stereo[pos : pos + chunk_frames])
        except Exception as exc:
            self.log(f"[트리거 출력 오류] {exc}")
        finally:
            self._close_pa_stream(stream)

    def _watchdog_loop(self) -> None:
        """Monitor silence after character playback completes and emit proactive cues with away protection."""
        last_ui_ts = 0.0
        last_ui_pair: tuple[str, str] | None = None
        while self.running:
            now_mono = time.monotonic()
            with self._active_tts_lock:
                tts_busy = self._active_tts_inflight > 0
            is_busy = (
                self._active_capture_speaking
                or self._active_stt_busy
                or tts_busy
                or (not self.utterance_queue.empty())
                or (not self.tts_queue.empty())
                or (not self.ordered_clause_queue.empty())
                or (not self.pcm_play_queue.empty())
                or (now_mono < self._active_audio_playing_until)
            )
            nudge_key = self.watchdog.tick(now_mono, is_pipeline_busy=is_busy)
            st = self.watchdog.get_status(now_mono, is_pipeline_busy=is_busy)

            if nudge_key:
                try:
                    self.trigger_play_queue.put_nowait(nudge_key)
                except queue.Full:
                    pass
                self.log(
                    f"🤖 [침묵 감지 → 선제 발화 유도 ({st.consecutive_nudges}/{st.max_nudges})] "
                    f"보조 스피커로 대화를 리드하도록 신호를 보냅니다."
                )

            if (now_mono - last_ui_ts) >= 0.25:
                last_ui_ts = now_mono
                if not bool(self.cfg.get("proactive_enable", True)):
                    txt, col = "🤖 선제 발화: 꺼짐", "#64748b"
                elif st.state == ProactiveWatchdog.DISARMED:
                    txt, col = "🤖 선제 발화: 첫 대화 대기 중", "#0f766e"
                elif st.state == ProactiveWatchdog.AWAITING_GEMINI_REPLY:
                    txt, col = f"⚡ 선제 발화 유도 중 ({st.consecutive_nudges}/{st.max_nudges})", "#d97706"
                elif st.state == ProactiveWatchdog.AUTO_PAUSED:
                    txt, col = "⏸ 자리비움 자동대기 (말 걸면 재개)", "#dc2626"
                elif is_busy:
                    txt, col = "🎙️ 대화 진행 중 (타이머 리셋)", "#2563eb"
                else:
                    txt, col = (
                        f"🤖 무음 카운트 {int(st.elapsed_sec)}/{int(st.timeout_sec)}s "
                        f"(유도 {st.consecutive_nudges}/{st.max_nudges})",
                        "#0f766e",
                    )
                if (txt, col) != last_ui_pair:
                    last_ui_pair = (txt, col)
                    def _upd_m2(t=txt, c=col) -> None:
                        self.lbl_proactive_status.config(text=t, fg=c)
                        if self.running:
                            self.companion_state_var.set(t)

                    self._schedule_ui(_upd_m2)

            time.sleep(0.15)

    def _capture_loop(self) -> None:
        """25ms Capture + Character-Adaptive Breath/Clause Slicer (prevents mid-word Japanese sokuon 'っ' cuts!)."""
        in_lbl = self.in_choice_var.get()
        active_in_sr = 44100
        block_dur = 0.025

        pre_roll: deque[np.ndarray] = deque(maxlen=4)
        speech_frames: list[np.ndarray] = []
        rms_history: list[float] = []
        speaking = False
        silence_sec = 0.0
        dip_sec = 0.0
        voiced_sec = 0.0
        turn_idle_sec = 1.0
        rolling_rms = 0.015
        rms_threshold = 0.0038
        is_first_clause = True
        clause_counter = 0
        cb_count = 0
        first_signal_logged = False

        def _dispatch_frames(frames_slice: list[np.ndarray], rms_slice: list[float], is_opening: bool = False) -> None:
            nonlocal clause_counter
            if not frames_slice:
                return
            clause_counter += 1
            full = np.concatenate(frames_slice)
            if active_in_sr != WHISPER_SR:
                g = gcd(active_in_sr, WHISPER_SR)
                full = _resample_pcm_1d(full, WHISPER_SR // g, active_in_sr // g)
            clause_rms = float(np.percentile(rms_slice, 85)) if rms_slice else 0.0
            self.utterance_queue.put((full, clause_counter, time.time(), clause_rms, is_opening))

        def _find_valley_cut(rms_list: list[float], search_frames: int) -> tuple[int, float]:
            n = len(rms_list)
            start = max(12, n - search_frames)
            best_i = n - 2
            best_v = 999.0
            for i in range(start, n - 1):
                v = 0.5 * (rms_list[i - 1] + rms_list[i])
                if v < best_v:
                    best_v = v
                    best_i = i
            return best_i, best_v

        def _callback(indata: np.ndarray, frames: int, time_info, status) -> None:
            nonlocal speech_frames, rms_history, speaking, silence_sec, dip_sec, voiced_sec
            nonlocal turn_idle_sec, rolling_rms, is_first_clause, cb_count, first_signal_logged

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
                bars = min(int(max(rms * 150, peak * 45)), 12)
                if rms >= rms_threshold or speaking:
                    tag = "🎭 첫 리액션·구절 수집 중!" if is_first_clause else "🎙️ 연속 감정 구절 스트리밍 중!"
                    fg_col = "#16a34a"
                elif peak >= 0.002:
                    tag = "미세 신호 감지"
                    fg_col = "#2563eb"
                else:
                    tag = "무음 (미러링/볼륨 확인)"
                    fg_col = "#64748b"
                meter_str = ("█" * bars) + ("░" * (12 - bars)) + f" (강도: {peak:.3f}) — {tag}"
                if meter_str != self._last_m2_meter:
                    self._last_m2_meter = meter_str
                    def _upd_m2_meter(m=meter_str, c=fg_col) -> None:
                        self.lbl_meter.config(text=m, fg=c)
                        if self.running:
                            self.companion_meter_var.set(m)

                    self._schedule_ui(_upd_m2_meter)

            speed_code = str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive")
            vap = ((self.current_char.get("persona") or {}).get("voice_acting_profile")) or {}
            char_min_clause = float(vap.get("min_clause_sec", 1.42))
            char_end_pause = float(vap.get("end_pause_sec", 0.34))

            if rms >= rms_threshold:
                if not first_signal_logged:
                    first_signal_logged = True
                    self.log(f"📡 [오디오 유입 확인] VB-Cable 신호 수신 시작! (peak={peak:.3f}, rms={rms:.4f})")

                if not speaking:
                    speaking = True
                    self._active_capture_speaking = True
                    silence_sec = 0.0
                    dip_sec = 0.0
                    for pf in pre_roll:
                        speech_frames.append(pf)
                        rms_history.append(rms_threshold)
                    pre_roll.clear()

                rolling_rms = 0.88 * rolling_rms + 0.12 * rms
                speech_frames.append(mono.copy())
                rms_history.append(rms)
                voiced_sec += block_dur
                silence_sec = 0.0
                turn_idle_sec = 0.0

                dip_limit = max(rms_threshold * 1.25, rolling_rms * 0.25)
                if rms < dip_limit:
                    dip_sec += block_dur
                else:
                    dip_sec = 0.0
            else:
                if not speaking:
                    pre_roll.append(mono.copy())
                    turn_idle_sec += block_dur
                    if turn_idle_sec >= 0.75:
                        is_first_clause = True
                    return

                speech_frames.append(mono.copy())
                rms_history.append(rms)
                silence_sec += block_dur
                dip_sec += block_dur

            buf_dur = len(speech_frames) * block_dur

            if speed_code == "ultra":
                # Fast streaming mode — guarded (>=0.22s silence) so Japanese sokuon 'っ' (0.10-0.17s) is NEVER split!
                min_v = 1.22 if is_first_clause else 1.40
                if voiced_sec >= min_v and silence_sec >= 0.22:
                    _dispatch_frames(speech_frames, rms_history, is_opening=is_first_clause)
                    speech_frames = []
                    rms_history = []
                    speaking = False
                    self._active_capture_speaking = False
                    voiced_sec = 0.0
                    dip_sec = 0.0
                    silence_sec = 0.0
                    is_first_clause = False
                    return
                if voiced_sec >= 2.85 and buf_dur >= 3.20:
                    cut_i, valley_rms = _find_valley_cut(rms_history, search_frames=26)
                    if valley_rms < rolling_rms * 0.35 or buf_dur >= 4.20:
                        _dispatch_frames(speech_frames[:cut_i], rms_history[:cut_i], is_opening=is_first_clause)
                        speech_frames = speech_frames[cut_i:]
                        rms_history = rms_history[cut_i:]
                        voiced_sec = len(speech_frames) * block_dur
                        dip_sec = 0.0
                        is_first_clause = False
                        return
                end_pause = 0.28
            elif speed_code == "full_turn":
                # Full-sentence/turn acting mode — waits for clear sentence/turn finish
                end_pause = max(0.48, char_end_pause + 0.14)
            else:
                # Default "expressive" mode — faster first-clause turnaround while guarding Japanese sokuon 'っ' (>=0.24s)!
                breath_pause = max(0.26, char_end_pause - 0.07)
                eff_min_clause = max(0.88, char_min_clause * 0.65) if is_first_clause else char_min_clause
                eff_breath = max(0.24, breath_pause - 0.02) if is_first_clause else breath_pause
                if voiced_sec >= eff_min_clause and silence_sec >= eff_breath:
                    _dispatch_frames(speech_frames, rms_history, is_opening=is_first_clause)
                    speech_frames = []
                    rms_history = []
                    speaking = False
                    self._active_capture_speaking = False
                    voiced_sec = 0.0
                    dip_sec = 0.0
                    silence_sec = 0.0
                    is_first_clause = False
                    return
                if voiced_sec >= 3.60 and buf_dur >= 4.10 and dip_sec >= 0.22:
                    cut_i, valley_rms = _find_valley_cut(rms_history, search_frames=30)
                    if valley_rms < rolling_rms * 0.32 or buf_dur >= 5.60:
                        _dispatch_frames(speech_frames[:cut_i], rms_history[:cut_i], is_opening=is_first_clause)
                        speech_frames = speech_frames[cut_i:]
                        rms_history = rms_history[cut_i:]
                        voiced_sec = len(speech_frames) * block_dur
                        dip_sec = 0.0
                        is_first_clause = False
                        return
                end_pause = char_end_pause

            if silence_sec >= end_pause or (buf_dur >= 7.5 and silence_sec >= 0.22):
                if voiced_sec >= 0.25:
                    _dispatch_frames(speech_frames, rms_history, is_opening=is_first_clause)
                    is_first_clause = False
                speech_frames = []
                rms_history = []
                speaking = False
                self._active_capture_speaking = False
                silence_sec = 0.0
                dip_sec = 0.0
                voiced_sec = 0.0

        in_candidates: list[tuple[str, int | None, int]] = []
        seen_lbls: set[str] = set()
        if in_lbl in self.in_map:
            idx0, sr0 = self.in_map[in_lbl]
            in_candidates.append((in_lbl, idx0, sr0))
            seen_lbls.add(in_lbl)

        fallback_inputs: list[tuple[int, int, str, int]] = []
        for idx, lbl, sr in self.inputs:
            if lbl in seen_lbls:
                continue
            low = lbl.lower()
            if "cable output" in low or "vb-audio" in low:
                api_rank = 0 if "mme" in low else (1 if "directsound" in low else 2)
                fallback_inputs.append((api_rank, idx, lbl, sr))
        fallback_inputs.sort()
        for _, idx, lbl, sr in fallback_inputs:
            in_candidates.append((lbl, idx, sr))
            seen_lbls.add(lbl)

        opened = False
        last_exc: Exception | None = None
        for cand_lbl, cand_idx, cand_sr in in_candidates:
            active_in_sr = int(cand_sr)
            cand_block = max(256, int(active_in_sr * block_dur))
            for ch in (2, 1):
                try:
                    self._inc_pa_stream()
                    try:
                        with sd.InputStream(
                            samplerate=active_in_sr,
                            channels=ch,
                            dtype="float32",
                            device=cand_idx,
                            blocksize=cand_block,
                            callback=_callback,
                        ):
                            opened = True
                            self._mode2_capture_ready = True
                            self.log(f"[오디오 수신 연결] 초저지연(25ms) 입력 열기: {cand_lbl} ({active_in_sr}Hz)")
                            if cand_lbl != in_lbl:
                                try:
                                    self.root.after(0, lambda v=cand_lbl: self.in_choice_var.set(v))
                                except Exception:
                                    pass
                            while self.running:
                                time.sleep(0.08)
                    finally:
                        self._dec_pa_stream()
                    break
                except Exception as exc:
                    last_exc = exc
            if opened:
                break

        if not opened:
            self._abort_bridge_on_device_error(f"[입력 장치 오류] {last_exc or in_lbl}")

    def _get_char_whisper_hint_tokens(self, tok: Any, char_data: dict) -> list[int]:
        cid = char_data.get("id", "")
        if cid in self.hint_token_cache:
            return self.hint_token_cache[cid]
        vap = ((char_data.get("persona") or {}).get("voice_acting_profile")) or {}
        hint_str = str(vap.get("whisper_hint_jp") or "").strip()
        if not hint_str:
            return []
        try:
            toks = list(tok.encode(" " + hint_str))[:45]
        except Exception:
            toks = []
        self.hint_token_cache[cid] = toks
        return toks

    def _stt_loop(self) -> None:
        """Dynamic 4s/6s/8s/12s Mel-Window CUDA INT8 Whisper with Sticky-JA fastpath."""
        from faster_whisper.tokenizer import Tokenizer

        while self.running:
            try:
                audio_16k, clause_no, t_captured, clause_rms, is_opening = self.utterance_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            peak = float(np.max(np.abs(audio_16k))) if audio_16k.size else 0.0
            if peak < 0.010:
                continue
            raw_peak = peak
            if peak < 0.28:
                audio_16k = np.clip(audio_16k * (0.36 / peak), -1.0, 1.0).astype(np.float32)

            lang_setting = STT_LABEL_TO_CODE.get(self.stt_lang_var.get(), "auto")
            t0 = time.time()
            self._active_stt_busy = True
            try:
                padded = np.concatenate([audio_16k, np.zeros(int(WHISPER_SR * 0.15), dtype=np.float32)])
                need_sec = len(padded) / float(WHISPER_SR) + 0.35
                cl = next((b for b in (4, 6, 8, 12, 16, 24, 30) if b >= need_sec), 30)
                feats = self.whisper_model.feature_extractor(padded, chunk_length=cl)
                n_f = cl * 100
                if feats.shape[-1] < n_f:
                    feats = np.pad(feats, ((0, 0), (0, n_f - feats.shape[-1])))
                else:
                    feats = feats[:, :n_f]

                enc = self.whisper_model.encode(feats)

                use_sticky_ja = lang_setting == "auto" and self._sticky_ja_hits >= 2
                if lang_setting == "auto" and not use_sticky_ja:
                    det_list = self.whisper_model.model.detect_language(enc)[0]
                    probs = dict(det_list)
                    p_ja = probs.get("<|ja|>", 0.0)
                    p_ko = probs.get("<|ko|>", 0.0)
                    if max(p_ja, p_ko) < 0.28:
                        continue
                    det_lang = "ja" if p_ja >= p_ko else "ko"
                    if det_lang == "ja" and p_ja >= 0.88:
                        self._sticky_ja_hits += 1
                    else:
                        self._sticky_ja_hits = 0
                elif use_sticky_ja:
                    det_lang = "ja"
                else:
                    det_lang = lang_setting

                c = self.current_char

                def _decode_with_lang(lang_code: str):
                    tok_l = self.tokenizers.get(lang_code) or Tokenizer(
                        self.whisper_model.hf_tokenizer, True, task="transcribe", language=lang_code
                    )
                    hint_toks = (
                        self._get_char_whisper_hint_tokens(tok_l, c)
                        if (lang_code == "ja" and raw_peak >= 0.018)
                        else []
                    )
                    prompt_l = self.whisper_model.get_prompt(tok_l, hint_toks, without_timestamps=True)
                    supp_l = (
                        list(self.whisper_model.get_suppressed_tokens(tok_l, [-1]))
                        if hasattr(self.whisper_model, "get_suppressed_tokens")
                        else [-1]
                    )
                    gen_res = self.whisper_model.model.generate(
                        enc,
                        [prompt_l],
                        beam_size=1,
                        max_length=160,
                        suppress_blank=True,
                        suppress_tokens=supp_l,
                        no_repeat_ngram_size=3,
                        repetition_penalty=1.08,
                        return_no_speech_prob=True,
                    )[0]
                    return tok_l, gen_res

                tok, res = _decode_with_lang(det_lang)
                if use_sticky_ja and res.no_speech_prob > 0.35:
                    det_list = self.whisper_model.model.detect_language(enc)[0]
                    probs = dict(det_list)
                    p_ja = probs.get("<|ja|>", 0.0)
                    p_ko = probs.get("<|ko|>", 0.0)
                    if max(p_ja, p_ko) < 0.28:
                        continue
                    if p_ja < 0.50 and p_ko > p_ja:
                        self._sticky_ja_hits = 0
                        det_lang = "ko"
                        tok, res = _decode_with_lang("ko")

                if res.no_speech_prob > 0.58:
                    continue

                tokens = [t for t in res.sequences_ids[0] if t < tok.eot]
                text = tok.decode(tokens).strip()
                dt = time.time() - t0
                if not text or text in HALLUCINATION_PHRASES or contains_trigger_echo(text):
                    continue
                # Guard against prompt-header echo on near-silent breaths
                header_prefix = f"{c.get('school_jp', '')} {c.get('club_jp', '')} {c.get('name_jp', '')}".strip()
                if header_prefix and text in header_prefix:
                    continue
                if len(text) <= 1 and (res.no_speech_prob > 0.28 or text in {"ん", "あ", "え", "."}):
                    continue

                self.watchdog.on_vbcable_speech_detected(time.monotonic(), is_new_turn=is_opening)
                self.log(f"📝 [구절 인식 #{clause_no} ({det_lang}/{cl}s, {dt:.2f}s)] {text}")
                self.tts_queue.put((clause_no, text, t_captured, clause_rms, is_opening))
            except Exception as exc:
                self.log(f"[STT 오류] {exc}")
            finally:
                self._active_stt_busy = False

    # =========================================================================
    # Mode 1: Distributable PC Mic + Mode C Gemini + 5-State Proactive Engine
    # =========================================================================

    def _apply_synced_web_models(self, models: list[tuple[str, str]], tier: str, persist: bool = True) -> None:
        if not models:
            return
        unlocked_codes = {c for c, _ in models}
        self._account_web_models = list(models)
        if tier:
            self._web_account_tier = tier
            self.cfg["web_account_tier"] = tier
        self.cfg["web_models"] = [[c, l] for c, l in models]
        self.cfg["web_models_schema_version"] = 2
        self._extra_web_models = [(c, l) for c, l in models if c not in GEMINI_BROWSER_MODEL_LABELS]

        cur_bmodel = str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen")
        if cur_bmodel not in unlocked_codes:
            self.cfg["gemini_browser_model"] = "hydrogen" if "hydrogen" in unlocked_codes else models[0][0]
        cur_sub = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
        if cur_sub.startswith("web:") and cur_sub[4:].strip() not in unlocked_codes:
            self.cfg["subtitle_engine"] = "auto_inline"

        if persist:
            save_bridge_config(self.cfg)
        try:
            self.root.after(0, self._refresh_companion_badges)
        except Exception:
            pass

    def _discover_web_models_async(self) -> None:
        def _probe() -> None:
            try:
                models, tier = self.gemini_mgr.browser.sync_account_web_models(allow_connect=False)
            except Exception:
                models, tier = [], ""
            if models:
                self._apply_synced_web_models(models, tier, persist=True)

        threading.Thread(target=_probe, daemon=True).start()

    def _auto_sync_cached_web_models_async(self) -> None:
        """Silently refresh account-tier Gemini web models on startup if a saved login profile exists."""
        if self._account_web_models:
            return
        try:
            prof = get_browser_profile_dir() / "Default"
            if not prof.exists():
                return
        except Exception:
            return

        def _probe() -> None:
            try:
                models, tier = self.gemini_mgr.browser.sync_account_web_models(allow_connect=True)
            except Exception:
                models, tier = [], ""
            if models:
                self._apply_synced_web_models(models, tier, persist=True)
                tier_txt = f"{tier} 계정" if tier else "웹 제미니 계정"
                self.log(f"[계정 모델 자동 동기화] {tier_txt} 사용 가능 모델 {len(models)}개 반영 완료.")

        threading.Thread(target=_probe, daemon=True, name="auto-sync-web-models").start()

    def sync_companion_models_async(self, ui_cb: Any = None) -> None:
        backend = str(self.cfg.get("gemini_backend", "browser"))
        use_browser = bool(ui_cb is not None or backend == "browser")
        if ui_cb is None and hasattr(self, "btn_sync_models") and self.btn_sync_models is not None:
            try:
                self.btn_sync_models.configure(state="disabled", text="동기화 중...")
            except Exception:
                pass

        def _restore_btn() -> None:
            if hasattr(self, "btn_sync_models") and self.btn_sync_models is not None:
                try:
                    self.btn_sync_models.configure(state="normal", text="🔄 계정 모델 동기화")
                except Exception:
                    pass

        def _worker() -> None:
            models: list[tuple[str, str]] = []
            tier = ""
            reason = ""
            try:
                if use_browser:
                    models, tier = self.gemini_mgr.browser.sync_account_web_models(allow_connect=True)
                    if not models and tier in ("LOGIN_REQUIRED", "NO_PICKER"):
                        reason = tier
                    elif not models and tier.startswith("PROBE_ERR:"):
                        reason = tier
                    if models:
                        self._apply_synced_web_models(models, tier, persist=True)
                        tier_txt = f"{tier} 계정" if tier else "웹 제미니 계정"
                        self.log(f"[계정 모델 동기화] {tier_txt} 선택 가능 모델 {len(models)}개를 대화·자막 목록에 반영했습니다.")
                    elif ui_cb is None:
                        if reason == "LOGIN_REQUIRED" or self.gemini_mgr.browser.login_mode_active:
                            self.log("[계정 모델 동기화 안내] 먼저 전용 브라우저에서 구글 로그인을 완료한 뒤 [✅ 구글 로그인 완료 확인]을 눌러 주세요.")
                        else:
                            self.log(f"[계정 모델 동기화 안내] 브라우저 모델 목록을 불러오지 못했습니다 ({reason or '응답 없음'}).")
                else:
                    api_key = str(self.cfg.get("gemini_api_key", "")).strip()
                    if not api_key:
                        self.log("[계정 모델 동기화 안내] 공식 Gemini API 모드에서는 설정에서 Gemini API Key를 먼저 입력해 주세요.")
                    else:
                        fetched = self.gemini_mgr.api.list_generate_models(api_key)
                        if fetched:
                            self.cfg["api_models"] = list(fetched)
                            save_bridge_config(self.cfg)
                            self.log(f"[계정 모델 동기화] Gemini API 사용 가능 모델 {len(fetched)}개를 반영했습니다.")
            except Exception as exc:
                reason = str(exc)
                if ui_cb is None:
                    self.log(f"[계정 모델 동기화 오류] {exc}")
            finally:
                try:
                    self.root.after(0, _restore_btn)
                    self.root.after(0, self._refresh_companion_badges)
                except Exception:
                    pass
                if ui_cb is not None:
                    try:
                        ui_cb(models, tier, reason)
                    except Exception:
                        pass

        threading.Thread(target=_worker, daemon=True, name="sync-companion-models").start()

    def _refresh_companion_badges(self) -> None:
        self._extra_api_models = [str(m).strip() for m in (self.cfg.get("api_models", []) or []) if str(m).strip()]
        raw_cfg_web = self.cfg.get("web_models") or []
        if raw_cfg_web:
            parsed_web = [
                (str(it[0]).strip(), str(it[1]).strip())
                for it in raw_cfg_web
                if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[0]).strip() and str(it[1]).strip()
            ]
            if parsed_web:
                self._account_web_models = parsed_web
        tier_val = str(self.cfg.get("web_account_tier", "") or "").strip()
        if tier_val:
            self._web_account_tier = tier_val

        backend = str(self.cfg.get("gemini_backend", "browser"))
        api_model = str(self.cfg.get("gemini_api_model", "gemini-3.5-flash-lite")).strip() or "gemini-3.5-flash-lite"
        browser_model, chat_thinking = split_legacy_browser_model(
            str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"),
            str(self.cfg.get("gemini_thinking_level", "low") or "low"),
        )
        self.cfg["gemini_browser_model"] = browser_model
        self.cfg["gemini_thinking_level"] = chat_thinking
        t_short = GEMINI_THINKING_LEVEL_LABELS.get(chat_thinking, chat_thinking)

        if backend == "api":
            badge = f"⚡ Gemini API ({api_model} · {t_short})"
            known_api = list(GEMINI_API_MODELS)
            known_api_codes = {c for c, _ in known_api}
            for extra in self._extra_api_models:
                if extra not in known_api_codes:
                    known_api.append((extra, extra))
                    known_api_codes.add(extra)
            choices = [lbl for _, lbl in known_api]
            cur_val = GEMINI_API_MODEL_LABELS.get(api_model, api_model)
            cmb_state = "normal"
        else:
            all_web = self._active_web_models()
            all_web_labels = {c: l for c, l in all_web}
            if browser_model not in all_web_labels and all_web:
                browser_model = "hydrogen" if "hydrogen" in all_web_labels else all_web[0][0]
                self.cfg["gemini_browser_model"] = browser_model
            b_lbl = all_web_labels.get(browser_model, browser_model)
            tier_tag = f"[{self._web_account_tier}]" if self._web_account_tier else ""
            badge = f"🌐 전용 브라우저{tier_tag} ({b_lbl.split('(')[0].strip()} · {t_short})"
            choices = [lbl for _, lbl in all_web]
            cur_val = all_web_labels.get(browser_model, choices[0] if choices else "")
            cmb_state = "readonly"
            self.gemini_mgr.browser.selected_model_code = browser_model
            self.gemini_mgr.browser.selected_thinking_level = chat_thinking

        self.companion_backend_badge_var.set(badge)
        self.companion_model_var.set(cur_val)
        self.companion_thinking_var.set(t_short)
        if hasattr(self, "cmb_companion_model") and self.cmb_companion_model is not None:
            self.cmb_companion_model.configure(values=choices, state=cmb_state)

        sub_eng = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
        if sub_eng == "auto_llm":
            sub_eng = "auto_inline"
            self.cfg["subtitle_engine"] = sub_eng
        sub_thinking = str(self.cfg.get("subtitle_thinking_level", "low") or "low")
        if sub_thinking not in GEMINI_THINKING_LEVEL_LABELS:
            sub_thinking = "low"
            self.cfg["subtitle_thinking_level"] = sub_thinking
        sub_choices = self._subtitle_choices()
        if hasattr(self, "cmb_subtitle_engine") and self.cmb_subtitle_engine is not None:
            self.cmb_subtitle_engine.configure(values=[lbl for _, lbl in sub_choices])
        self.subtitle_engine_var.set(resolve_subtitle_engine_label(sub_eng, sub_choices))
        self.subtitle_thinking_var.set(GEMINI_THINKING_LEVEL_LABELS[sub_thinking])
        self._sync_subtitle_thinking_widget()

        lo = int(round(float(self.cfg.get("idle_ping_min_sec", 45.0))))
        hi = int(round(float(self.cfg.get("idle_ping_max_sec", 75.0))))
        afk_n = int(self.cfg.get("afk_max_consecutive_pings", 2))
        self.companion_range_var.set(f"무음 대기: {lo}~{hi}초 랜덤 · {afk_n}회 무응답 시 AFK")

    def open_settings_dialog(self, first_run: bool = False, initial_tab: int = 0) -> None:
        def _on_saved(updated_cfg: dict[str, Any]) -> None:
            prev_bmodel = str(self.cfg.get("gemini_browser_model", "hydrogen"))
            prev_bthink = str(self.cfg.get("gemini_thinking_level", "low"))
            prev_steam_dir = str(self.cfg.get("steam_game_dir", "") or "").strip()
            self.cfg.update(updated_cfg)
            new_steam_dir = str(self.cfg.get("steam_game_dir", "") or "").strip()
            if new_steam_dir != prev_steam_dir:
                clear_steam_voice_availability_cache()
                if hasattr(self, "momotalk_assets") and self.momotalk_assets is not None:
                    self.momotalk_assets.set_ba_root_hint(new_steam_dir or None)
                    self._start_momotalk_assets_index_async()

            # Sync Tab 0..3 settings back to App Tk variables & MomoTalk UI
            pvar = str(self.cfg.get("persona_prompt_variant", "official") or "official")
            self.persona_variant_var.set(PERSONA_VARIANT_LABELS.get(pvar, PERSONA_VARIANT_LABELS["official"]))
            self.proactive_prompt_var.set(bool(self.cfg.get("proactive_prompt_append", True)))
            rlang = "ko" if str(self.cfg.get("reply_language", "ja") or "ja").strip().lower() == "ko" else "ja"
            self.reply_language_var.set(rlang)
            self._update_reply_language_toggle_style()
            self.jp_beginner_coach_var.set(False if rlang == "ko" else bool(self.cfg.get("jp_beginner_lover_mode", False)))
            self._update_jp_coach_toggle_style()

            self.api_key_var.set(str(self.cfg.get("fish_api_key", "") or ""))
            saved_char_voice = self._get_saved_voice_id(self.current_char_id, reply_lang=rlang) or (
                "" if rlang == "ko" else str(self.cfg.get("fish_reference_id", "") or "")
            )
            self.ref_id_var.set(saved_char_voice)
            self.model_hdr_var.set(str(self.cfg.get("fish_model_header", "s2.1-pro-free") or "s2.1-pro-free"))

            smode = str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive")
            self.speed_mode_var.set(SPEED_MODE_LABELS.get(smode, SPEED_MODE_LABELS["expressive"]))
            self.auto_emotion_var.set(bool(self.cfg.get("auto_emotion_directing", True)))
            sboost = float(self.cfg.get("expression_boost_offset", 0.0) or 0.0)
            self.boost_preset_var.set(BOOST_PRESET_LABELS[min(BOOST_PRESET_LABELS.keys(), key=lambda k: abs(k - sboost))])
            self.vol_var.set(float(self.cfg.get("output_volume", 1.0) or 1.0))

            slang = str(self.cfg.get("stt_language", "auto") or "auto")
            self.stt_lang_var.set(STT_LANG_LABELS.get(slang, STT_LANG_LABELS["auto"]))

            p_out = str(self.cfg.get("primary_output_device", "") or "").strip()
            if p_out and p_out in self.out_map:
                self.out_choice_var.set(p_out)
            m_in = str(self.cfg.get("mic_input_device", "") or "").strip()
            if m_in and m_in in self.in_map:
                self.mic_choice_var.set(m_in)
            t_out = str(self.cfg.get("proactive_trigger_device", "") or "").strip()
            if t_out and t_out in self.out_map:
                self.trigger_out_choice_var.set(t_out)

            self.companion_proactive_var.set(bool(self.cfg.get("companion_proactive_enable", True)))
            self.proactive_enable_var.set(bool(self.cfg.get("proactive_enable", True)))
            ptime = float(self.cfg.get("proactive_timeout_sec", 18.0) or 18.0)
            self.proactive_timeout_var.set(PROACTIVE_TIMEOUT_LABELS[min(PROACTIVE_TIMEOUT_LABELS.keys(), key=lambda k: abs(k - ptime))])
            pvol = float(self.cfg.get("proactive_trigger_volume", 1.0) or 1.0)
            self.proactive_vol_var.set(PROACTIVE_VOL_LABELS[min(PROACTIVE_VOL_LABELS.keys(), key=lambda k: abs(k - pvol))])

            if isinstance(self.cfg.get("api_models"), list):
                self._extra_api_models = [str(m).strip() for m in self.cfg["api_models"] if str(m).strip()]
            raw_web = self.cfg.get("web_models") or []
            if isinstance(raw_web, list) and raw_web:
                self._account_web_models = [
                    (str(it[0]).strip(), str(it[1]).strip())
                    for it in raw_web
                    if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[0]).strip() and str(it[1]).strip()
                ]

            new_bmodel, new_bthink = split_legacy_browser_model(
                str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"),
                str(self.cfg.get("gemini_thinking_level", "low") or "low"),
            )
            self.cfg["gemini_browser_model"] = new_bmodel
            self.cfg["gemini_thinking_level"] = new_bthink
            self.companion_thinking_var.set(GEMINI_THINKING_LEVEL_LABELS.get(new_bthink, GEMINI_THINKING_LEVEL_LABELS["low"]))

            sub_choices = self._subtitle_choices()
            self.cmb_subtitle_engine.configure(values=[lbl for _, lbl in sub_choices])
            self.subtitle_engine_var.set(resolve_subtitle_engine_label(str(self.cfg.get("subtitle_engine", "auto_inline")), sub_choices))
            st_code = str(self.cfg.get("subtitle_thinking_level", "low") or "low")
            self.subtitle_thinking_var.set(GEMINI_THINKING_LEVEL_LABELS.get(st_code, GEMINI_THINKING_LEVEL_LABELS["low"]))
            self._sync_sub_thinking_controls()

            try:
                if hasattr(self, "mode_notebook"):
                    self.mode_notebook.select(1 if self.cfg.get("bridge_mode") == "gemini_live_audio" else 0)
            except Exception:
                pass

            if str(self.cfg.get("gemini_backend", "browser")) == "browser" and (new_bmodel != prev_bmodel or new_bthink != prev_bthink):
                self.gemini_mgr.browser.set_chat_model_async(new_bmodel, new_bthink)
            self.watchdog.configure(
                enabled=bool(self.proactive_enable_var.get()),
                silence_timeout_sec=float(self.cfg.get("proactive_timeout_sec", 18.0)),
            )
            self.companion_sm.configure(
                proactive_enabled=bool(self.companion_proactive_var.get()),
                min_idle_sec=float(self.cfg.get("idle_ping_min_sec", 45.0)),
                max_idle_sec=float(self.cfg.get("idle_ping_max_sec", 75.0)),
                max_consecutive_pings=int(self.cfg.get("afk_max_consecutive_pings", 2)),
            )
            self._sync_mic_stt_config()
            self._refresh_persona_summary_ui()
            self._refresh_companion_badges()
            self._refresh_momotalk_header_and_cards()
            if getattr(self, "_momotalk_sort_mode", "default") == "heart":
                self._refresh_character_filter_list(keep_current=True)
            else:
                self._render_roster_visible(force_all=True)
            self._on_active_persona_potentially_changed("설정/커스텀 프롬프트 변경")
            self.log("[환경 설정 적용] %LOCALAPPDATA%\\VoiceCompanionData\\config.json 저장 및 실시간 반영 완료.")

        SettingsDialog(
            self.root,
            self.cfg,
            on_saved=_on_saved,
            first_run=first_run,
            on_open_logs=self.open_status_log_window,
            extra_api_models=self._extra_api_models,
            extra_web_models=self._extra_web_models,
            web_models=self._account_web_models or None,
            on_sync_web_models=self.sync_companion_models_async,
            app=self,
            initial_tab=initial_tab,
        )

    def _on_companion_proactive_toggled(self) -> None:
        self.save_settings_ui(silent=True)

    def _resolve_active_persona_for_gemini(self) -> tuple[str, bool]:
        """Return (persona_prompt, use_japanese_lock)."""
        rlang = self._get_reply_language()
        is_ko = rlang == "ko"
        coach_on = bool(self.jp_beginner_coach_var.get()) and (not is_ko)
        if bool(self.cfg.get("use_custom_persona", False)):
            custom = str(self.cfg.get("custom_persona_prompt") or "").strip()
            if is_ko:
                return f"{custom}\n\n[답변 언어 설정: 한국어]\n반드시 자연스러운 한국어로만 대답하세요.", False
            if coach_on:
                return build_jp_beginner_lover_coach_prompt(None, custom), True
            return custom, False
        c = self.current_char
        vcode = PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")
        prompt, _ = resolve_voice_persona_prompt(c, vcode)
        ent = self.momotalk_entry_map.get(getattr(self, "current_momotalk_entry_id", ""))
        if ent is not None and ent.get("base_cid") == self.current_char_id and ent.get("costume_label_kr"):
            costume_kr = str(ent["costume_label_kr"]).strip()
            costume_line = f"\n\n【衣装・モモトーク状態】\n・現在の衣装：{ent.get('name_kr', '')}（{costume_kr}）"
            if ent.get("status_message_jp"):
                costume_line += f"\n・現在のモモトーク一言：「{ent['status_message_jp']}」"
            elif ent.get("status_message_kr"):
                costume_line += f"\n・現在のモモトーク一言：「{ent['status_message_kr']}」"
            prompt = f"{prompt}{costume_line}"
        alt_voice_rule = build_alt_voice_prompt_block(c, reply_lang=rlang)
        if is_ko:
            ko_guide = build_character_ko_style_guide(c, jp_beginner_coach=False)
            prompt = f"{prompt}\n\n[답변 언어 설정: 한국어]\n{ko_guide}"
            if alt_voice_rule:
                prompt = f"{prompt}\n\n{alt_voice_rule}"
            return prompt, False
        if coach_on:
            prompt = build_jp_beginner_lover_coach_prompt(c, prompt)
        if alt_voice_rule:
            prompt = f"{prompt}\n\n{alt_voice_rule}"
        return prompt, True

    def _on_active_persona_potentially_changed(self, reason_kr: str) -> None:
        """Sync character mic STT hints and open a fresh Gemini chat when active persona changes."""
        self.mic_stt.set_character_context(self.current_char)
        new_key = self._resolve_active_persona_for_gemini()
        if new_key == getattr(self, "_last_active_persona_key", None):
            return
        self._last_active_persona_key = new_key
        if not getattr(self, "_ui_boot_complete", False):
            return
        if self.companion_running and self._companion_session_ready:
            self.log(f"🔄 [{reason_kr}] 이전 대화 기록과 섞이지 않도록 새 제미니 채팅방을 열고 첫인사를 다시 시작합니다.")
            self.reset_companion_conversation_async()
        else:
            self.gemini_mgr.invalidate_session()

    def _set_barge_in_duck(self, active: bool) -> None:
        self._barge_in_ducking = bool(active)

    def _is_companion_mic_allowed(self) -> bool:
        return bool(
            self.companion_running
            and self._companion_session_ready
            and not self._companion_mic_error
            and self._call_ui_state != "calling"
        )

    @staticmethod
    def _normalize_echo_text(text: str) -> str:
        return _ECHO_NORM_RE.sub("", text or "").lower()

    def _is_speaker_echo(self, text: str) -> bool:
        if not self.mic_stt.last_utterance_during_playback:
            return False
        if self._get_reply_language() != "ko" and _HANGUL_RE.search(text or ""):
            return False
        norm_in = self._normalize_echo_text(text)
        if len(norm_in) < 2:
            return False
        recent_blob = self._normalize_echo_text("".join(self._recent_tts_jp_lines))
        return bool(recent_blob and norm_in in recent_blob)

    def _on_companion_mic_onset(self) -> None:
        if self.companion_sm.on_mic_voice_start(time.monotonic()):
            self.log("🎙️ [음성 감지] 사용자 발화 시작 → 무음 선톡 타이머 0초 초기화 (LISTENING)")

    def _on_companion_mic_empty(self) -> None:
        self.companion_sm.on_stt_empty(time.monotonic())

    def _on_companion_mic_transcribed(self, text: str) -> None:
        if not self.companion_running:
            return
        clean = (text or "").strip()
        if not clean:
            self.companion_sm.on_stt_empty(time.monotonic())
            return
        if self._is_speaker_echo(clean):
            self.log(f"[에코 필터] 캐릭터 음성 되울림으로 판단해 무시했습니다: {clean}")
            self.companion_sm.on_stt_empty(time.monotonic())
            return

        now = time.monotonic()
        with self.companion_sm._lock:
            cur_state = self.companion_sm.state
            pre_state = self.companion_sm._pre_listen_state
        pb_busy = now < (self._active_audio_playing_until + 0.25)
        if (
            cur_state in (CompanionState.SPEAKING, CompanionState.THINKING)
            or pre_state in (CompanionState.SPEAKING, CompanionState.THINKING)
            or pb_busy
        ):
            with self._turn_interrupt_lock:
                self._active_turn_interrupt.set()
                self.abort_current_playback()
                self._active_audio_playing_until = 0.0
                self._barge_in_ducking = False
            self.log(f"🎙️ [끼어들기 반영] 캐릭터 발화·생성 중단 후 새 발화 즉시 전송: {clean}")

        if self.companion_sm.on_user_speech_confirmed(now):
            pending_users: list[str] = []
            while True:
                try:
                    q_kind, q_payload = self._companion_turn_queue.get_nowait()
                    if q_kind == "user" and str(q_payload).strip():
                        pending_users.append(str(q_payload).strip())
                except queue.Empty:
                    break
            merged = " ".join([*pending_users, clean]).strip()
            if self._momotalk_char_activated and self._call_ui_state == "connected":
                def _append_sensei_sub(txt_u: str = clean) -> None:
                    self._subtitle_seq += 1
                    self._append_subtitle_entry_fast(
                        SubtitleEntry(
                            entry_id=self._subtitle_seq,
                            char_id="__sensei__",
                            char_name_kr="선생님",
                            char_name_jp="先生",
                            jp_text=txt_u,
                            kr_text=txt_u,
                            is_turn_start=True,
                        )
                    )

                self._schedule_ui(_append_sensei_sub)
            self._record_memory_turn(
                role="user",
                jp_text=merged,
                kr_text=merged,
                raw_stt=merged,
            )
            try:
                self._companion_turn_queue.put_nowait(("user", merged))
            except queue.Full:
                self.log("[안내] 새 발화를 대기열에 추가할 수 없습니다.")

    def _on_companion_mic_meter(self, rms: float, peak: float, is_speaking: bool) -> None:
        bars = min(int(max(rms * 140, peak * 42)), 12)
        tag = "🗣️ 말하기 감지 중 (LISTENING)" if is_speaking else "대기 중"
        meter_str = ("█" * bars) + ("░" * (12 - bars)) + f" (RMS {rms:.3f}) — {tag}"
        if meter_str != self._last_companion_meter:
            self._last_companion_meter = meter_str
            self._schedule_ui(lambda m=meter_str: self.companion_meter_var.set(m))

    def _on_companion_mic_error(self, msg: str) -> None:
        self._companion_mic_error = msg
        self.log(f"[마이크 오류] {msg}")
        try:
            self.root.after(
                0,
                lambda: (
                    self.companion_state_var.set(f"⚠️ {msg}"),
                    self.lbl_companion_state.config(fg="#dc2626"),
                ),
            )
        except Exception:
            pass

    def _build_mic_fallback_candidates(self, primary_lbl: str) -> list[tuple[int | None, int, str]]:
        """Build ordered microphone fallback list across MME -> DirectSound -> WASAPI -> OS default."""
        candidates: list[tuple[int | None, int, str]] = []
        seen_idx: set[int | None] = set()
        target_key = _norm_dev_key(primary_lbl)

        def _api_rank(lbl: str) -> int:
            low = lbl.lower()
            if "mme" in low:
                return 0
            if "directsound" in low:
                return 1
            if "wasapi" in low:
                return 2
            return 3

        same_dev = [
            (idx, sr, lbl)
            for idx, lbl, sr in self.inputs
            if "wdm-ks" not in lbl.lower() and _norm_dev_key(lbl) == target_key
        ]
        same_dev.sort(key=lambda item: (_api_rank(item[2]), item[0]))
        for idx, sr, lbl in same_dev:
            if idx not in seen_idx:
                seen_idx.add(idx)
                candidates.append((idx, int(sr), lbl))

        for lbl in self.mic_inputs:
            if lbl in self.in_map:
                idx, sr = self.in_map[lbl]
                if idx not in seen_idx:
                    seen_idx.add(idx)
                    candidates.append((idx, int(sr), lbl))

        candidates.append((None, 44100, "Windows 기본 마이크"))
        return candidates

    def toggle_gemini_browser_window(self) -> None:
        def _worker() -> None:
            if self.gemini_mgr.browser.is_running():
                new_vis = not self.gemini_mgr.browser.window_visible
                self.gemini_mgr.browser.set_window_visible(new_vis)
                lbl = "🌐 제미니 화면 숨기기" if new_vis else "🌐 제미니 화면 보기"
                self.root.after(0, lambda l=lbl: self._browser_toggle_var.set(l))
                self.log(f"[전용 브라우저] 창 {'표시' if new_vis else '백그라운드 숨김'} 전환 완료.")
            else:
                self.log("[전용 브라우저] gemini.google.com 세션을 여는 중입니다...")
                ok, reason = self.gemini_mgr.browser.ensure_ready_and_check_login(show_if_ready=True)
                if ok:
                    self.root.after(0, lambda: self._browser_toggle_var.set("🌐 제미니 화면 숨기기"))
                    self.log("[전용 브라우저] 로그인 세션 확인 완료 (표시 상태).")
                    self._discover_web_models_async()
                elif reason == "LOGIN_REQUIRED":
                    self.root.after(0, lambda: self._browser_toggle_var.set("🌐 제미니 화면 숨기기"))
                    self.log("[구글 로그인 필요] 열린 브라우저 창에서 구글 로그인 후 [✅ 구글 로그인 완료 확인]을 눌러 주세요.")
                else:
                    self.log(f"[전용 브라우저 오류] {reason}")

        threading.Thread(target=_worker, daemon=True).start()

    def confirm_browser_login_async(self) -> None:
        def _worker() -> None:
            self.log("[로그인 확인] 구글 로그인 세션을 검증하고 백그라운드로 전환합니다...")
            ok, reason = self.gemini_mgr.browser.complete_interactive_login()
            if ok:
                self.root.after(0, lambda: self._browser_toggle_var.set("🌐 제미니 화면 보기"))
                self.log("[로그인 완료] 세션이 %LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile 에 안전하게 저장되었습니다!")
                self._discover_web_models_async()
                self._schedule_ui(lambda: self._set_calling_login_required(False))
                if self.companion_running:
                    self._ringback_state.resume()
                    self._companion_session_ready = True
                    self.companion_sm.on_opening_greeting_started(time.monotonic())
                    try:
                        self._companion_turn_queue.put_nowait(("prime", ""))
                    except queue.Full:
                        pass
            else:
                self.log(f"[로그인 미완료] 아직 로그인 화면입니다 ({reason}). 브라우저 창에서 로그인을 마친 뒤 다시 눌러 주세요.")

        threading.Thread(target=_worker, daemon=True).start()

    def _drain_companion_turn_queue(self) -> None:
        while not self._companion_turn_queue.empty():
            try:
                self._companion_turn_queue.get_nowait()
            except queue.Empty:
                break

    def reset_companion_conversation_async(self) -> None:
        backend = str(self.cfg.get("gemini_backend", "browser"))
        if not self.companion_running:
            self.gemini_mgr.invalidate_session()
            threading.Thread(
                target=lambda b=backend: self.gemini_mgr.reset_conversation(b),
                daemon=True,
                name="gemini-reset-chat",
            ).start()
            self.log("[이번 통화 새 주제로] 저장된 장기 기억은 유지한 채 다음 시작 시 새 채팅방에서 시작합니다.")
            return
        with self._turn_interrupt_lock:
            self._active_turn_interrupt.set()
            self.abort_current_playback()
            self._active_audio_playing_until = 0.0
            self._barge_in_ducking = False
        self._recent_tts_jp_lines.clear()
        self._drain_companion_turn_queue()
        self.gemini_mgr.invalidate_session()
        cur_base_cid, cur_track = self._get_active_memory_key()
        with self._active_memory_session_lock:
            active_sess = dict(self._active_memory_session) if self._active_memory_session else None
        if active_sess and (active_sess.get("base_cid") != cur_base_cid or active_sess.get("track") != cur_track):
            self._finalize_active_memory_session(run_summary=True)
            self._start_active_memory_session()
        elif not bool(self.temporary_call_var.get()):
            mem_ctx, _ = self.memory_mgr.build_memory_context_block(
                base_cid=cur_base_cid,
                track=cur_track,
                char_data=None if cur_base_cid == "__custom__" else self.current_char,
            )
            self._active_call_memory_context = mem_ctx
        if backend == "api":
            self.gemini_mgr.reset_conversation(backend)
        self.log("🔄 [이번 통화 새 주제로] 저장된 장기 기억은 유지한 채 현재 통화만 새 화제로 다시 시작합니다.")
        self.companion_sm.on_opening_greeting_started(time.monotonic())
        try:
            self._companion_turn_queue.put_nowait(("prime", ""))
        except queue.Full:
            pass

    def trigger_manual_companion_ping_async(self) -> None:
        if not self.companion_running:
            messagebox.showinfo("모드 1 대기 중", "먼저 [▶️ AI 보이스 대화 시작]을 눌러 대화를 시작해 주세요.")
            return
        if self.companion_sm.trigger_manual_ping(time.monotonic()):
            self.log("💬 [수동 선톡 테스트] 제미니에게 '[IDLE_PING]'을 전송합니다!")
            try:
                self._companion_turn_queue.put_nowait(("ping", IDLE_PING_TOKEN))
            except queue.Full:
                pass
        else:
            st = self.companion_sm.get_status().kr_label
            self.log(f"[수동 선톡 대기] 현재 상태({st})가 완료된 후 다시 시도해 주세요.")

    def toggle_companion_mode(self) -> None:
        if self.companion_running:
            self.companion_running = False
            self._companion_session_ready = False
            self._companion_mic_error = ""
            self._companion_cancel_event.set()
            with self._turn_interrupt_lock:
                self._active_turn_interrupt.set()
            self._barge_in_ducking = False
            self._recent_tts_jp_lines.clear()
            self.mic_stt.stop()
            self.companion_sm.stop_session()
            self.gemini_mgr.invalidate_session()
            self.abort_current_playback()
            self._finalize_active_memory_session(run_summary=not self._closing)
            if self.tts_executor is not None:
                try:
                    self.tts_executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                self.tts_executor = None
            self.btn_companion_toggle.config(text="▶️ AI 보이스 대화 시작", bg="#16a34a")
            self.companion_state_var.set("⚪ 중지됨")
            self.companion_meter_var.set("░░░░░░░░░░░░ (마이크 중지됨)")
            self.log("[모드 1 중지] PC 마이크 + 제미니 보이스 대화를 종료했습니다.")
            return

        if self.running:
            self.toggle_bridge()

        self.refresh_audio_devices(reinit_portaudio=True, log_info=False)
        self.save_settings_ui(silent=True)

        if not str(self.cfg.get("fish_api_key", "")).strip():
            messagebox.showwarning("Fish Audio API Key 필요", "먼저 설정 창에서 Fish Audio API Key를 입력해 주세요.")
            self.open_settings_dialog(first_run=True)
            return

        backend = str(self.cfg.get("gemini_backend", "browser"))
        if backend == "api" and not str(self.cfg.get("gemini_api_key", "")).strip():
            messagebox.showwarning("Gemini API Key 필요", "공식 Gemini API 모드용 Gemini API Key를 입력하거나 전용 브라우저 모드로 전환해 주세요.")
            self.open_settings_dialog(first_run=False)
            return

        mic_lbl = self.mic_choice_var.get().strip()
        if not mic_lbl or mic_lbl not in self.in_map:
            messagebox.showwarning(
                "마이크 장치 필요",
                "PC에 연결된 마이크 입력 장치를 찾을 수 없습니다.\n마이크 연결 후 [🔄 장치 새로고침]을 누르거나 [모드 2: Gemini Live 오디오 브릿지]를 이용해 주세요.",
            )
            return

        while not self._companion_turn_queue.empty():
            try:
                self._companion_turn_queue.get_nowait()
            except queue.Empty:
                break

        self.abort_current_playback()
        self.stop_playback_flag.clear()
        self._companion_cancel_event.clear()
        with self._turn_interrupt_lock:
            self._active_turn_interrupt = threading.Event()
        self._barge_in_ducking = False
        self._recent_tts_jp_lines.clear()
        with self._active_tts_lock:
            self._active_tts_inflight = 0
        self._active_audio_playing_until = 0.0
        self._post_playback_guard_until = 0.0
        self._companion_clause_seq = 0
        self._companion_mic_error = ""
        self._companion_session_ready = (backend == "api")

        self._start_active_memory_session()
        self.tts_executor = ThreadPoolExecutor(max_workers=2)
        self.companion_running = True
        self.companion_sm.start_session(time.monotonic())
        self.btn_companion_toggle.config(text="⏹ AI 보이스 대화 중지", bg="#dc2626")
        self.companion_state_var.set("🔄 초기화 중...")
        self.log("💡 [안내] 제미니 브라우저 안의 마이크 버튼을 누를 필요 없이, 이 프로그램이 PC 마이크를 상시 연속 감지합니다.")

        threading.Thread(target=self._audio_player_loop, daemon=True).start()
        threading.Thread(target=self._clause_order_drainer_loop, daemon=True).start()
        threading.Thread(target=self._tts_prefetch_loop, daemon=True).start()
        threading.Thread(target=self._companion_turn_worker, daemon=True).start()
        threading.Thread(target=self._companion_watchdog_loop, daemon=True).start()

        mic_idx, mic_sr = self.in_map[mic_lbl]
        fallback_mics = self._build_mic_fallback_candidates(mic_lbl)
        self._sync_mic_stt_config()
        self.mic_stt.set_character_context(self.current_char)
        self.mic_stt.start(mic_idx, mic_sr, mic_lbl, fallback_candidates=fallback_mics)

        def _boot_gemini() -> None:
            if backend == "browser":
                self.log("[모드 1 시작] 전용 브라우저 제미니 세션을 준비 중입니다...")
                ok, reason = self.gemini_mgr.browser.ensure_ready_and_check_login(show_if_ready=False)
                if not ok:
                    if reason == "LOGIN_REQUIRED":
                        self._ringback_state.pause()
                        self.root.after(0, lambda: self._browser_toggle_var.set("🌐 제미니 화면 숨기기"))
                        self._schedule_ui(lambda: self._set_calling_login_required(True))
                        self.log(
                            "🔑 [구글 로그인 안내] 열린 브라우저 창에서 구글 로그인을 완료한 뒤 [✅ 구글 로그인 완료 확인] 버튼을 눌러 주세요!"
                        )
                    else:
                        self.log(f"[브라우저 오류] {reason}")
                        self._ringback_state.stop()
                        self._schedule_ui(self.stop_momotalk_call)
                    return
                self.root.after(0, lambda: self._browser_toggle_var.set("🌐 제미니 화면 보기"))
                self._discover_web_models_async()
            if self.companion_running:
                self._companion_session_ready = True
                self._schedule_pending_memory_recovery_async()
                self.companion_sm.on_opening_greeting_started(time.monotonic())
                try:
                    self._companion_turn_queue.put_nowait(("prime", ""))
                except queue.Full:
                    pass

        threading.Thread(target=_boot_gemini, daemon=True).start()

    def _wait_for_audio_drain_and_idle(
        self,
        expected_turn_seq: int | None = None,
        interrupt_event: threading.Event | None = None,
    ) -> None:
        """Wait until all prefetched Fish Audio clauses finish playing before returning to IDLE/AFK."""
        deadline = time.monotonic() + 90.0
        while self.companion_running and time.monotonic() < deadline:
            if interrupt_event is not None and interrupt_event.is_set():
                break
            with self._active_tts_lock:
                inflight = self._active_tts_inflight
            busy = (
                inflight > 0
                or (not self.tts_queue.empty())
                or (not self.ordered_clause_queue.empty())
                or (not self.pcm_play_queue.empty())
                or (time.monotonic() < self._active_audio_playing_until + 0.22)
            )
            if not busy:
                break
            time.sleep(0.06)
        self._post_playback_guard_until = 0.0
        if interrupt_event is not None and interrupt_event.is_set():
            return
        new_state = self.companion_sm.on_turn_finished(time.monotonic(), expected_turn_seq=expected_turn_seq)
        if new_state == CompanionState.AFK:
            self.log("⏸ [자리 비움(AFK) 전환] 연속 무응답 선톡 한도에 도달해 타이머를 일시 정지합니다. 말을 걸면 즉시 재개됩니다.")

    def _companion_turn_worker(self) -> None:
        while self.companion_running:
            try:
                kind, payload = self._companion_turn_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if not self.companion_running:
                break

            with self._turn_interrupt_lock:
                cur_turn_cancel = threading.Event()
                self._active_turn_interrupt = cur_turn_cancel
                self.stop_playback_flag.clear()
            my_turn_seq = self.companion_sm.current_turn_seq
            combined_cancel = _CombinedCancelEvent(self._companion_cancel_event, cur_turn_cancel)

            backend = str(self.cfg.get("gemini_backend", "browser"))
            gem_key = str(self.cfg.get("gemini_api_key", ""))
            gem_model = str(self.cfg.get("gemini_api_model", "gemini-3.5-flash-lite"))
            browser_model, chat_thinking = split_legacy_browser_model(
                str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"),
                str(self.cfg.get("gemini_thinking_level", "low") or "low"),
            )
            sub_engine = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
            if sub_engine == "auto_llm":
                sub_engine = "auto_inline"
            sub_mode = self.subtitle_mode_var.get()
            reply_lang = self._get_reply_language()
            use_ko_lock = reply_lang == "ko"
            jp_coach_on = bool(self.jp_beginner_coach_var.get()) and (not use_ko_lock)
            persona_prompt, use_jp_lock = self._resolve_active_persona_for_gemini()
            bilingual_inline = bool(use_jp_lock and (not use_ko_lock) and sub_mode != "jp_only" and sub_engine == "auto_inline")
            ko_style_guide = (
                build_character_ko_style_guide(self.current_char, jp_beginner_coach=jp_coach_on)
                if (use_jp_lock or use_ko_lock)
                else ""
            )
            t_start = time.time()

            emitted_tts_any = False
            turn_spoken_parts: list[str] = []
            turn_kr_futures: list[Future[str] | None] = []

            def _enqueue_companion_tts(
                sentence: str,
                is_first: bool,
                kr_fut: Future[str] | None = None,
            ) -> None:
                nonlocal emitted_tts_any
                if (not self.companion_running) or combined_cancel.is_set():
                    return
                voice_key, stripped_sent = resolve_sentence_voice_persona(self.current_char, sentence)
                if use_jp_lock:
                    clean_sent = sanitize_japanese_tts_text(stripped_sent, preserve_voice_tag=False)
                    if (not clean_sent) or (not contains_japanese(clean_sent)):
                        self.log(f"[Companion][TTS차단] 일본어 미포함/한국어 대사 차단: {sentence[:40]!r}")
                        return
                elif use_ko_lock:
                    clean_sent = sanitize_korean_tts_text(stripped_sent, preserve_voice_tag=False)
                    if (not clean_sent) or (not has_korean_script(clean_sent)):
                        self.log(f"[Companion][TTS차단] 한국어 미포함/일본어 대사 차단: {sentence[:40]!r}")
                        return
                else:
                    clean_sent = strip_voice_persona_tags(stripped_sent)
                first_flag = bool(is_first or (not emitted_tts_any))
                emitted_tts_any = True
                turn_spoken_parts.append(clean_sent)
                turn_kr_futures.append(kr_fut)
                self._recent_tts_jp_lines.append(clean_sent)
                self.companion_sm.on_speaking_started(time.monotonic())
                self._companion_clause_seq += 1
                self.tts_queue.put(
                    (self._companion_clause_seq, clean_sent, t_start, 0.14, first_flag, kr_fut, voice_key)
                )

            def _on_sentence(sentence: str, is_first: bool) -> None:
                _enqueue_companion_tts(sentence, is_first)

            def _on_sentence_pair(jp_sent: str, kr_fut: Future[str], is_first: bool) -> None:
                _enqueue_companion_tts(jp_sent, is_first, kr_fut=kr_fut)

            mem_ctx = str(getattr(self, "_active_call_memory_context", "") or "")

            try:
                if kind == "prime":
                    self.log("🧠 [새 대화 시작] 캐릭터 페르소나와 [IDLE_PING] 시스템 규칙을 전송합니다...")
                    reply = self.gemini_mgr.prime_and_greet(
                        backend_mode=backend,
                        api_key=gem_key,
                        api_model=gem_model,
                        persona_prompt=persona_prompt,
                        use_japanese_lock=use_jp_lock,
                        on_sentence=_on_sentence,
                        cancel_event=combined_cancel,
                        browser_model=browser_model,
                        thinking_level=chat_thinking,
                        bilingual_inline=bilingual_inline,
                        ko_style_guide=ko_style_guide,
                        on_sentence_pair=_on_sentence_pair if bilingual_inline else None,
                        jp_beginner_coach=jp_coach_on,
                        use_korean_lock=use_ko_lock,
                        memory_context=mem_ctx,
                    )
                else:
                    if kind == "ping":
                        self.log("🤖 [선톡 발동] 침묵 감지 → 제미니에게 '[IDLE_PING]' 전송 중...")
                    reply = self.gemini_mgr.send_message(
                        backend_mode=backend,
                        api_key=gem_key,
                        api_model=gem_model,
                        persona_prompt=persona_prompt,
                        use_japanese_lock=use_jp_lock,
                        user_text=payload,
                        on_sentence=_on_sentence,
                        cancel_event=combined_cancel,
                        browser_model=browser_model,
                        thinking_level=chat_thinking,
                        bilingual_inline=bilingual_inline,
                        ko_style_guide=ko_style_guide,
                        on_sentence_pair=_on_sentence_pair if bilingual_inline else None,
                        jp_beginner_coach=jp_coach_on,
                        use_korean_lock=use_ko_lock,
                        memory_context=mem_ctx,
                    )
                if reply and (not combined_cancel.is_set()):
                    self.log(f"💬 [제미니 응답] {reply}")
                if (turn_spoken_parts or reply) and (not combined_cancel.is_set()):
                    spoken_joined = " ".join(p for p in turn_spoken_parts if p).strip()
                    kr_resolved: list[str] = []
                    for kf in turn_kr_futures:
                        if kf is None:
                            continue
                        try:
                            val = str(kf.result(timeout=1.5) or "").strip()
                            if val:
                                kr_resolved.append(val)
                        except Exception:
                            pass
                    kr_joined = " ".join(kr_resolved).strip()
                    if (not kr_joined) and (not use_ko_lock) and turn_spoken_parts:
                        cached_kr: list[str] = []
                        for p in turn_spoken_parts:
                            try:
                                ck = self.subtitle_translator._cache_key(p, self.current_char)
                                cv = self.subtitle_translator._cache_get(ck)
                                if cv:
                                    cached_kr.append(cv)
                            except Exception:
                                pass
                        if cached_kr:
                            kr_joined = " ".join(cached_kr).strip()
                    if use_ko_lock:
                        jp_save = spoken_joined or strip_voice_persona_tags(str(reply or ""))
                        kr_save = jp_save
                    else:
                        jp_save = spoken_joined or strip_voice_persona_tags(str(reply or ""))
                        kr_save = kr_joined
                    self._record_memory_turn(
                        role="assistant",
                        jp_text=jp_save,
                        kr_text=kr_save,
                        is_proactive_ping=(kind == "ping"),
                    )
            except Exception as exc:
                if not combined_cancel.is_set():
                    self.log(f"[제미니 연동 오류] {exc}")
            finally:
                if kind == "prime" and (not combined_cancel.is_set()) and self._call_ui_state == "calling":
                    self._ringback_state.stop()
                    self._schedule_ui(self._on_call_connected_ui)
                self._wait_for_audio_drain_and_idle(expected_turn_seq=my_turn_seq, interrupt_event=cur_turn_cancel)

    def _companion_watchdog_loop(self) -> None:
        last_ui = 0.0
        last_ui_pair: tuple[str, str] | None = None
        while self.companion_running:
            now = time.monotonic()
            if self._companion_session_ready and (not self._companion_mic_error):
                if self.companion_sm.tick(now):
                    try:
                        self._companion_turn_queue.put_nowait(("ping", IDLE_PING_TOKEN))
                    except queue.Full:
                        pass

            if (now - last_ui) >= 0.25:
                last_ui = now
                if self._companion_mic_error:
                    msg = f"⚠️ {self._companion_mic_error}"
                    col = "#dc2626"
                elif not self._companion_session_ready:
                    if self.gemini_mgr.browser.login_mode_active:
                        msg = "🔑 구글 로그인 대기 중 ([✅ 구글 로그인 완료 확인] 클릭)"
                        col = "#d97706"
                    else:
                        msg = "🔄 제미니 세션 준비 중..."
                        col = "#d97706"
                else:
                    st = self.companion_sm.get_status(now)
                    if st.state == CompanionState.IDLE:
                        if st.proactive_enabled:
                            msg = (
                                f"🟢 대기 중 (IDLE) — 무음 {int(st.elapsed_idle_sec)}/{int(st.target_idle_sec)}s "
                                f"(선톡 {st.consecutive_pings}/{st.max_pings})"
                            )
                            col = "#0f766e"
                        else:
                            msg = "🟢 대기 중 (IDLE) — 선톡 꺼짐"
                            col = "#0f766e"
                    elif st.state == CompanionState.LISTENING:
                        msg = "🎙️ 사용자 말하기 (LISTENING — 타이머 초기화)"
                        col = "#16a34a"
                    elif st.state == CompanionState.THINKING:
                        msg = "🧠 제미니 응답 중 (THINKING)"
                        col = "#d97706"
                    elif st.state == CompanionState.SPEAKING:
                        msg = "🔊 Fish Audio 재생 중 (SPEAKING)"
                        col = "#2563eb"
                    else:
                        msg = "⏸ 자리 비움 (AFK — 말을 걸면 재개)"
                        col = "#dc2626"

                if (msg, col) != last_ui_pair:
                    last_ui_pair = (msg, col)
                    def _upd(m=msg, c=col) -> None:
                        self.companion_state_var.set(m)
                        self.lbl_companion_state.config(fg=c)

                    self._schedule_ui(_upd)
            time.sleep(0.15)

    def _cleanup_on_exit(self) -> None:
        self._closing = True
        self.running = False
        self.companion_running = False
        self._stop_keepalive_event.set()
        try:
            self._finalize_active_memory_session(run_summary=False)
        except Exception:
            pass
        try:
            with self._bg_asset_cond:
                self._bg_asset_cond.notify_all()
        except Exception:
            pass
        for aid in list(getattr(self, "_startup_after_ids", [])):
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
        if hasattr(self, "_startup_after_ids"):
            self._startup_after_ids.clear()
        for attr in ("_ui_poll_after_id", "_call_anim_after_id", "_call_timer_after_id"):
            aid = getattr(self, attr, None)
            if aid is not None:
                setattr(self, attr, None)
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
        try:
            self._ringback_state.stop()
        except Exception:
            pass
        try:
            self.mic_stt.stop()
        except Exception:
            pass
        try:
            self.gemini_mgr.close()
        except Exception:
            pass
        try:
            self.subtitle_translator.close()
        except Exception:
            pass
        try:
            if getattr(self, "_http_client", None) is not None:
                self._http_client.close()
        except Exception:
            pass

    def _on_app_closing(self) -> None:
        self._cleanup_on_exit()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


_SINGLE_INSTANCE_MUTEX: Any = None


def _focus_existing_bridge_window() -> bool:
    if os.name != "nt":
        return False
    found_ref = [False]
    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]

        def _enum_cb(hwnd: int, _lparam: int) -> bool:
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value == "MomoTalk" or "실시간 보이스 브릿지" in buf.value:
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    found_ref[0] = True
                    return False
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    except Exception:
        pass
    return found_ref[0]


def _get_bridge_pid_file() -> Path:
    return get_cache_dir() / "bridge.pid"


def _write_bridge_pid_file() -> None:
    try:
        pid_file = _get_bridge_pid_file()
        if pid_file.is_symlink():
            return
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        def _cleanup_pid() -> None:
            try:
                if pid_file.exists() and not pid_file.is_symlink() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    pid_file.unlink(missing_ok=True)
            except Exception:
                pass

        atexit.register(_cleanup_pid)
    except Exception:
        pass


def _terminate_stale_windowless_bridges() -> None:
    """Terminate a stale windowless previous bridge process recorded in bridge.pid using native Win32 API."""
    if os.name != "nt":
        return
    try:
        pid_file = _get_bridge_pid_file()
        if not pid_file.is_file() or pid_file.is_symlink():
            return
        raw_pid = pid_file.read_text(encoding="utf-8").strip()
        if not raw_pid.isdigit():
            return
        stale_pid = int(raw_pid)
        if stale_pid <= 0 or stale_pid == os.getpid():
            return
        PROCESS_TERMINATE = 0x0001
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.TerminateProcess.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int

        handle = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, stale_pid)
        if not handle:
            return
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = ctypes.c_uint32(len(buf))
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                if Path(buf.value).name.lower() in ("python.exe", "pythonw.exe"):
                    k32.TerminateProcess(handle, 1)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        pass


def main() -> None:
    global _SINGLE_INSTANCE_MUTEX
    try:
        if os.name == "nt":
            try:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
                k32.CreateMutexW.restype = ctypes.c_void_p
                _SINGLE_INSTANCE_MUTEX = k32.CreateMutexW(
                    None, False, "Local\\BlueArchive_AruFishBridge_v4_SingleInstance"
                )
                if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                    if _focus_existing_bridge_window():
                        return
                    _terminate_stale_windowless_bridges()
                    time.sleep(0.25)
                    _SINGLE_INSTANCE_MUTEX = k32.CreateMutexW(
                        None, False, "Local\\BlueArchive_AruFishBridge_v4_SingleInstance"
                    )
                _write_bridge_pid_file()
            except Exception:
                pass
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BlueArchive.AruVoiceBridge.v4")
            except Exception:
                pass
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        root = tk.Tk()
        AruFishBridgeApp(root)
        root.mainloop()
    except Exception:
        _report_fatal_startup_error("보이스 브릿지 실행 오류", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
