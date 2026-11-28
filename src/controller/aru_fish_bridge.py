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
import gc
import importlib
import json
import math
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
from copy import deepcopy
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
    (re.compile(r"(?i)((?:x-goog-api-key|x-api-key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}\]]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:api_)?key=)[^&\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r'("(?:fish_api_key|gemini_api_key|claude_api_key|openai_api_key|fish_mcp_oauth_data|colab_s2pro_api_key|access_token|refresh_token|client_secret|id_token)"\s*:\s*")[^"]*(")'), r"\1[REDACTED]\2"),
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
        try:
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
        except OSError:
            return
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
    """Lazy-import `scipy.signal.resample_poly` on first audio resampling call (with numpy fallback if scipy is absent)."""
    global _RESAMPLE_POLY_FN
    if _RESAMPLE_POLY_FN is None:
        try:
            from scipy.signal import resample_poly as _sp_resample_poly

            _RESAMPLE_POLY_FN = _sp_resample_poly
        except ImportError:
            def _np_linear_resample(x: np.ndarray, u: int, d: int) -> np.ndarray:
                arr = np.asarray(x, dtype=np.float32).reshape(-1)
                if arr.size <= 1 or u <= 0 or d <= 0 or u == d:
                    return arr
                n_out = max(1, int(round(arr.size * (float(u) / float(d)))))
                xp = np.linspace(0.0, 1.0, arr.size, dtype=np.float64)
                xnew = np.linspace(0.0, 1.0, n_out, dtype=np.float64)
                return np.interp(xnew, xp, arr).astype(np.float32)

            _RESAMPLE_POLY_FN = _np_linear_resample
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
        c32 = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if not np.all(np.isfinite(c32)):
            c32 = np.nan_to_num(c32, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
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
    from src.controller.fish_mcp_backend import (
        FishMcpAuthError,
        FishMcpBackend,
        FishMcpError,
        FishMcpQuotaError,
    )
    from src.controller.colab_s2pro_backend import (
        ColabS2ProAuthError,
        ColabS2ProBackend,
        ColabS2ProError,
        ColabS2ProUnavailableError,
        resolve_s2pro_streaming_params,
        sanitize_s2pro_open_weights_directing,
        soft_limit_pcm_f32,
        split_s2pro_opening_clause,
        validate_colab_server_url,
    )
    from src.controller.tts_cache import TtsDiskCache
    from src.controller.gemini_connector import (
        AI_PROVIDER_LABELS,
        AI_PROVIDER_SHORT_NAMES,
        GEMINI_API_MODEL_LABELS,
        GEMINI_API_MODELS,
        GEMINI_BROWSER_MODEL_LABELS,
        GEMINI_BROWSER_MODELS,
        GEMINI_THINKING_LEVEL_LABELS,
        GEMINI_THINKING_LEVELS,
        IDLE_PING_TOKEN,
        KOREAN_VOICE_OUTPUT_LOCK,
        GeminiSessionManager,
        coerce_safe_api_model_id,
        extract_safe_api_model_id,
        extract_voice_tag_prefix,
        get_provider_api_models,
        get_provider_browser_models,
        get_provider_default_api_model,
        get_provider_default_browser_model,
        has_korean_script,
        normalize_ai_provider,
        pick_best_gemini_free_model,
        sanitize_japanese_tts_text,
        sanitize_korean_tts_text,
        split_legacy_browser_model,
        strip_voice_persona_tags,
    )
    from src.controller.jp_conversation_coach import build_jp_beginner_lover_coach_prompt
    from src.controller.ko_speech_profiles import build_korean_voice_acting_guide
    from src.controller.local_mic_stt import (
        LocalMicSttEngine,
        list_microphone_labels,
    )
    from src.controller.memory_dialog import MemoryNoteDialog
    from src.controller.memory_manager import (
        MEMORY_TRACK_LABELS,
        MemoryManager,
        format_timestamp_safe,
        parse_memory_extraction_json,
        persona_variant_to_memory_track,
    )
    from src.controller import persona_enhancer as _persona_enhancer_mod
    from src.controller.persona_enhancer import (
        CHARACTER_DNA_HEADER_PREFIX,
        build_character_dna_block,
        build_fanon_enhancement_block,
        build_per_turn_persona_anchor,
        compress_shared_voice_boilerplate,
        compute_variant_tts_prosody,
        get_llm_temperature_for_variant,
        get_variant_badge_info,
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
    from src.controller.rvc_launcher import (
        focus_existing_rvc_window,
        get_rvc_status,
        is_rvc_runtime_available,
        launch_rvc_live_app,
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
        WHISPER_LABEL_TO_CODE,
        WHISPER_MODEL_LABELS,
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
    from src.utils import settings_recovery as _settings_recovery_mod
    from src.utils.settings_recovery import (
        ConfigLoadReport,
        SettingsResetResult,
        is_virtual_loopback_mic_label,
        load_user_config_with_report,
        reset_settings_to_defaults,
    )
    from src.utils.ui_scroll import (
        ScrollableFrame,
        attach_scrollable_mousewheel,
        bind_responsive_wrap,
        clamp_window_position,
        fit_window_to_screen,
        make_pinned_footer,
    )
except Exception:
    _report_fatal_startup_error("보이스 브릿지 모듈 로드 오류", traceback.format_exc())
    raise

try:
    migrate_legacy_workspace_data(ROOT_DIR)
    CONFIG_PATH = get_config_path()
    CHARACTERS_DIR = ROOT_DIR / "config" / "characters"
    MOMOTALK_PROFILES_PATH = ROOT_DIR / "config" / "momotalk_profiles.json"
    RUNTIME_LOG_PATH = get_logs_dir() / "bridge_runtime.log"
    TRIGGER_CACHE_DIR = get_trigger_cache_dir()
    _INITIAL_RUNTIME_LOG_PATH = RUNTIME_LOG_PATH
    _INITIAL_TRIGGER_CACHE_DIR = TRIGGER_CACHE_DIR
except Exception:
    _report_fatal_startup_error("보이스 브릿지 초기화 오류", traceback.format_exc())
    raise


def _get_runtime_log_path() -> Path:
    """Resolve `bridge_runtime.log` dynamically so post-import `VOICE_COMPANION_DATA_DIR` overrides apply cleanly."""
    if RUNTIME_LOG_PATH != _INITIAL_RUNTIME_LOG_PATH:
        return RUNTIME_LOG_PATH
    return get_logs_dir() / "bridge_runtime.log"


def _get_trigger_cache_dir() -> Path:
    """Resolve the trigger WAV cache directory dynamically at call time."""
    if TRIGGER_CACHE_DIR != _INITIAL_TRIGGER_CACHE_DIR:
        return TRIGGER_CACHE_DIR
    return get_trigger_cache_dir()

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_MODEL_URL = "https://api.fish.audio/model"
FISH_PCM_SR = 44100
WHISPER_SR = 16000
FISH_MCP_FALLBACK_HTTP_STATUSES = frozenset({402, 404, 410, 429})

# Runtime security & DoS guardrails
MAX_TTS_STREAM_BYTES = 32 * 1024 * 1024           # 32 MB (~6 min 44.1kHz PCM) per TTS stream
MAX_HTTP_ERROR_BYTES = 4096                       # 4 KB max read for HTTP error diagnostics
MAX_FISH_SEARCH_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MB max JSON body for Fish model search
MAX_ZEROSHOT_WAV_BYTES = 25 * 1024 * 1024         # 25 MB max local reference WAV read
MAX_CHAR_CONFIG_BYTES = 5 * 1024 * 1024           # 5 MB max character JSON file size
_ALLOWED_WHISPER_MODELS = frozenset({"tiny", "base", "small", "medium", "large-v3-turbo", "turbo"})
_OUTPUT_VOLUME_MIN = 0.10
_OUTPUT_VOLUME_MAX = 2.00


def _resolve_whisper_suppressed_tokens(model: Any, tok: Any) -> list[int]:
    """Resolve Whisper suppressed token IDs safely across faster-whisper 1.2.1+ and mock models."""
    if hasattr(model, "get_suppressed_tokens"):
        try:
            res = model.get_suppressed_tokens(tok, [-1])
            if res is not None:
                return list(res)
        except Exception:
            pass
    try:
        from faster_whisper.transcribe import get_suppressed_tokens as _fw_get_suppressed_tokens

        res = _fw_get_suppressed_tokens(tok, [-1])
        if res is not None:
            return list(res)
    except Exception:
        pass
    non_speech = getattr(tok, "non_speech_tokens", None)
    if non_speech:
        try:
            return list(non_speech)
        except Exception:
            pass
    return []
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


def _is_screen_vision_available(project_root: Path | None = None) -> bool:
    """Return True iff the local-only `src.screen_vision` package exists inside `ROOT_DIR` and imports cleanly."""
    try:
        root = (project_root if project_root is not None else ROOT_DIR).resolve()
        init_py = root / "src" / "screen_vision" / "__init__.py"
        if not init_py.is_file() or init_py.is_symlink():
            return False
        if not init_py.resolve().is_relative_to(root):
            return False
        mod = importlib.import_module("src.screen_vision")
        avail_fn = getattr(mod, "is_screen_vision_available", None)
        if callable(avail_fn) and not bool(avail_fn()):
            return False
        if not bool(getattr(mod, "SCREEN_VISION_AVAILABLE", True)):
            return False
        for req_sym in (
            "load_screen_vision_config",
            "save_screen_vision_config",
            "ScreenWatcher",
            "GlobalHotkeyManager",
            "build_screen_turn_addon",
            "EconomicsStudyBoard",
            "MiniSubtitleOverlay",
        ):
            if not hasattr(mod, req_sym):
                return False
        return True
    except Exception:
        return False


is_screen_vision_available = _is_screen_vision_available


def _safe_cfg_float(
    val: Any,
    default: float,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Parse a finite float from user config safely without raising on None/bool/NaN/Inf/bad strings."""
    try:
        if isinstance(val, bool) or val is None:
            return default
        f = float(val)
        if not math.isfinite(f):
            return default
        if lo is not None:
            f = max(lo, f)
        if hi is not None:
            f = min(hi, f)
        return f
    except Exception:
        return default


def _safe_cfg_int(
    val: Any,
    default: int,
    lo: int | None = None,
    hi: int | None = None,
) -> int:
    """Parse a finite bounded int from user config safely without raising on corrupt values."""
    try:
        if isinstance(val, bool) or val is None:
            return default
        f = float(val)
        if not math.isfinite(f):
            return default
        n = int(round(f))
        if lo is not None:
            n = max(lo, n)
        if hi is not None:
            n = min(hi, n)
        return n
    except Exception:
        return default


def _safe_cfg_model_header(val: Any, default: str = "s2.1-pro-free") -> str:
    s = str(val or "").strip() if isinstance(val, str) else ""
    return s or default


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
    if not isinstance(c, dict):
        return False
    raw_persona = c.get("persona")
    persona = raw_persona if isinstance(raw_persona, dict) else {}
    raw_vap = persona.get("voice_acting_profile")
    vap = raw_vap if isinstance(raw_vap, dict) else {}
    return bool(persona.get("patch_kit_v15") or vap.get("spike_directing") or vap.get("whisper_directing"))


def _mono_to_stereo(mono: np.ndarray) -> np.ndarray:
    """Duplicate 1-D float32 mono samples into contiguous 2-ch stereo without column_stack overhead."""
    out = np.empty((mono.shape[0], 2), dtype=np.float32)
    out[:, 0] = mono
    out[:, 1] = mono
    return out


def _call_compress_shared_voice_boilerplate(raw_p: str, variant_code: str, *, reply_lang: str = "ja") -> str:
    try:
        return compress_shared_voice_boilerplate(raw_p, variant_code, reply_lang=reply_lang)
    except TypeError:
        try:
            return compress_shared_voice_boilerplate(raw_p, reply_lang=reply_lang)
        except TypeError:
            return compress_shared_voice_boilerplate(raw_p, variant_code)


def _call_build_fanon_enhancement_block(char_data: dict[str, Any], variant_code: str, *, reply_lang: str = "ja") -> str:
    try:
        return build_fanon_enhancement_block(char_data, variant_code, reply_lang=reply_lang)
    except TypeError:
        return build_fanon_enhancement_block(char_data, variant_code)


def _call_build_proactive_lead_prompt(
    char_data: dict[str, Any],
    base_prompt: str,
    *,
    reply_lang: str = "ja",
) -> str:
    try:
        return build_proactive_lead_prompt(char_data, base_prompt, reply_lang=reply_lang)
    except TypeError:
        return build_proactive_lead_prompt(char_data, base_prompt)


def _call_build_per_turn_persona_anchor(
    char_data: dict[str, Any],
    variant_code: str,
    *,
    reply_lang: str = "ja",
    use_custom: bool = False,
    recent_spoken_lines: list[str] | None = None,
    recent_turn_openers: list[str] | None = None,
    bilingual_inline: bool = False,
    custom_prompt: str = "",
) -> str:
    try:
        return build_per_turn_persona_anchor(
            char_data,
            variant_code,
            reply_lang=reply_lang,
            use_custom=use_custom,
            recent_spoken_lines=recent_spoken_lines,
            recent_turn_openers=recent_turn_openers,
            bilingual_inline=bilingual_inline,
            custom_prompt=custom_prompt,
        )
    except TypeError:
        try:
            return build_per_turn_persona_anchor(
                char_data,
                variant_code,
                reply_lang=reply_lang,
                use_custom=use_custom,
                recent_spoken_lines=recent_spoken_lines,
                bilingual_inline=bilingual_inline,
                custom_prompt=custom_prompt,
            )
        except TypeError:
            return build_per_turn_persona_anchor(
                char_data,
                variant_code,
                reply_lang=reply_lang,
                use_custom=use_custom,
                recent_spoken_lines=recent_spoken_lines,
            )


def _call_build_long_turn_persona_refresh_block(
    char_data: dict[str, Any],
    mode_code: str = "1",
    reply_lang: str = "ja",
    *,
    bilingual_inline: bool = False,
    use_custom_persona: bool = False,
    custom_prompt: str = "",
    costume_label_kr: str = "",
) -> str:
    fn = getattr(_persona_enhancer_mod, "build_long_turn_persona_refresh_block", None)
    if not callable(fn):
        return ""
    try:
        return str(
            fn(
                char_data,
                mode_code,
                reply_lang,
                bilingual_inline=bilingual_inline,
                use_custom_persona=use_custom_persona,
                custom_prompt=custom_prompt,
                costume_label_kr=costume_label_kr,
            )
            or ""
        )
    except Exception:
        return ""


def _call_build_memory_context_block(
    memory_mgr: Any,
    *,
    base_cid: str,
    track: str,
    char_data: dict[str, Any] | None,
    current_variant: str,
    reply_lang: str = "ja",
    max_recent_turns: int | None = None,
) -> tuple[str, bool]:
    kwargs: dict[str, Any] = {
        "base_cid": base_cid,
        "track": track,
        "char_data": char_data,
        "current_variant": current_variant,
        "reply_lang": reply_lang,
    }
    if max_recent_turns is not None:
        kwargs["max_recent_turns"] = max_recent_turns
    try:
        return memory_mgr.build_memory_context_block(**kwargs)
    except TypeError:
        kwargs.pop("reply_lang", None)
        try:
            return memory_mgr.build_memory_context_block(**kwargs)
        except TypeError:
            kwargs.pop("max_recent_turns", None)
            return memory_mgr.build_memory_context_block(**kwargs)


def _call_reset_browser_profile(user_data_dir: Path | None = None) -> tuple[bool, str]:
    fn = getattr(_settings_recovery_mod, "reset_browser_profile", None)
    if callable(fn):
        return fn(user_data_dir=user_data_dir)
    import shutil as _shutil

    target = (user_data_dir / "BrowserProfile") if user_data_dir is not None else get_browser_profile_dir()
    if not target.exists():
        return True, "AI 전용 브라우저 프로필(BrowserProfile)이 이미 비어 있습니다."
    try:
        _shutil.rmtree(target)
        return True, "AI 전용 브라우저 로그인 세션(BrowserProfile)을 깨끗하게 초기화했습니다."
    except PermissionError:
        return (
            False,
            "열려 있는 AI 전용 브라우저 창이 프로필 폴더를 사용 중입니다. 브라우저 창을 모두 닫은 뒤 다시 시도해 주세요.",
        )
    except OSError as exc:
        return False, f"브라우저 프로필 초기화에 실패했습니다: {exc}"


def resolve_voice_persona_prompt(
    char_data: dict,
    variant_code: str,
    *,
    include_enhancement: bool = True,
    include_dna: bool = True,
    reply_lang: str = "ja",
) -> tuple[str, str]:
    """Return (voice_prompt, human_label) with unified 2-layer DNA + variant resolution (Official strictly 0% fanon)."""
    raw_persona = char_data.get("persona") if isinstance(char_data, dict) else None
    persona = raw_persona if isinstance(raw_persona, dict) else {}
    raw_fanon = persona.get("fanon_dc_ba_gallery")
    fanon = raw_fanon if isinstance(raw_fanon, dict) else {}
    norm_vcode = str(variant_code or "official").strip().lower()
    if norm_vcode == "fanon_mainstream":
        raw_p = fanon.get("voice_prompt_mainstream_jp") or persona.get("gemini_live_voice_prompt", "")
        lbl = PERSONA_VARIANT_LABELS["fanon_mainstream"]
    elif norm_vcode == "fanon_pure_love":
        raw_p = fanon.get("voice_prompt_pure_love_jp") or persona.get("gemini_live_voice_prompt", "")
        lbl = PERSONA_VARIANT_LABELS["fanon_pure_love"]
    elif norm_vcode == "fanon_spicy_meme":
        raw_p = fanon.get("voice_prompt_spicy_meme_jp") or persona.get("gemini_live_voice_prompt", "")
        lbl = PERSONA_VARIANT_LABELS["fanon_spicy_meme"]
    elif norm_vcode == "fanon_optimal_blend":
        raw_p = (
            fanon.get("voice_prompt_optimal_blend_jp")
            or fanon.get("voice_prompt_mainstream_jp")
            or persona.get("gemini_live_voice_prompt", "")
        )
        lbl = PERSONA_VARIANT_LABELS["fanon_optimal_blend"]
    else:
        norm_vcode = "official"
        raw_p = persona.get("gemini_live_voice_prompt", "")
        lbl = PERSONA_VARIANT_LABELS["official"]

    compact_p = _call_compress_shared_voice_boilerplate(raw_p, norm_vcode, reply_lang=reply_lang)
    if include_dna:
        dna = build_character_dna_block(char_data, variant_code=norm_vcode, reply_lang=reply_lang)
        if dna and dna not in compact_p and CHARACTER_DNA_HEADER_PREFIX not in compact_p:
            compact_p = f"{compact_p}\n\n{dna}" if compact_p else dna
    if include_enhancement and norm_vcode != "official":
        enh = _call_build_fanon_enhancement_block(char_data, norm_vcode, reply_lang=reply_lang)
        if enh:
            compact_p = f"{compact_p}\n\n{enh}" if compact_p else enh
    return compact_p, lbl

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
    r"(二人きり|帰さない|帰しません|他の生徒|他の子|私だけ|あたしだけ|独占|離さない|離しません|ずっとそば|ぎゅっ|撫でて|なでなで|正妻|泊まって|心臓の音|ドキドキ|好き……|大好き……|そばにいて|会いたかった|隣にいて)"
)
_CUE_MEME_PROTEST_RE = re.compile(
    r"(風評|心外|誤解|濡れ衣|反則|ずるいです|却下|サボり|自爆|言い訳|挑発|そんなわけ|ありえません|どういうこと|反省文|没収)"
)
_TEMPO_BASELINE_RE = re.compile(r"(?:^\s*(\d+(?:\.\d+)?)\s*x?\b|(\d+\.\d+)\s*x)", re.IGNORECASE)
_OPEN_REACT_TRIM_RE = re.compile(r"^[…\.\s「『（(]+|[、。！？!?…\.〜~・\s」』）)]+$")
_OPEN_REACT_LAUGH_RE = re.compile(
    r"(ふふ|フフ|くふふ|クフフ|うふふ|ウフフ|えへ|エヘ|うへ|ウヘ|あは|アハ|うは|ウハ|きゃは|キャハ|きひ|キヒ|くす|にひ|ふっふ|パヒャ|ぱひゃ|わっは)"
)
_OPEN_REACT_SIGH_RE = re.compile(r"(はあ|はぁ|ハァ|ふぅ|ふう|ふわ|ふぁ|むにゃ)")
_OPEN_REACT_FLUSTER_RE = re.compile(
    r"(！？|\?!|あぅ|はぅ|ひゃぁ|うわあ|こ、こほん|は、はあ|だ、誰|わ、わわ|ちょ、)"
)


def _parse_tempo_baseline(vap: dict[str, Any]) -> float:
    """Parse `voice_acting_profile.tempo_baseline` (e.g. '0.90x (...)') safely clamped to [0.90, 1.06]."""
    if not isinstance(vap, dict):
        return 1.00
    raw = vap.get("tempo_baseline")
    if raw is None or isinstance(raw, bool):
        return 1.00
    if isinstance(raw, (int, float)):
        val = float(raw)
    else:
        m = _TEMPO_BASELINE_RE.search(str(raw).strip())
        if not m:
            return 1.00
        tok = m.group(1) or m.group(2)
        try:
            val = float(tok)
        except (TypeError, ValueError):
            return 1.00
    if not math.isfinite(val):
        return 1.00
    return round(max(0.90, min(1.06, val)), 2)


def _match_character_opening_reaction(vap: dict[str, Any], text_out: str) -> str:
    """
    Detect if `text_out` begins with one of the character's own `opening_reactions_jp`
    sigh/laugh/fluster interjections so Fish s2 directing responds cleanly without double-synthesis.
    Returns `"fluster"`, `"laugh"`, `"sigh"`, or `""`.
    """
    if not text_out or not isinstance(vap, dict):
        return ""
    ops = vap.get("opening_reactions_jp")
    if not isinstance(ops, (list, tuple)):
        return ""
    probe = text_out.lstrip("…・\u3000 ")
    if not probe:
        return ""
    for raw_op in ops:
        op_full = str(raw_op or "").strip()
        if not op_full:
            continue
        stem = _OPEN_REACT_TRIM_RE.sub("", op_full)
        if len(stem) < 2:
            continue
        if text_out.startswith(op_full) or probe.startswith(stem):
            if _OPEN_REACT_FLUSTER_RE.search(op_full):
                return "fluster"
            if _OPEN_REACT_LAUGH_RE.search(stem):
                return "laugh"
            if _OPEN_REACT_SIGH_RE.search(stem):
                return "sigh"
    return ""


def _match_character_lv3_trigger(
    char_data: dict[str, Any],
    text_out: str,
    persona_variant: str,
) -> str:
    """
    Inspect `char_data.persona.fanon_dc_ba_gallery.fanon_tiers["Lv.3"].triggers` for fanon variants
    and return the matching trigger's `s2_directing_mode` (`"whisper_directing"` or `"spike_directing"`).
    Always returns `""` for `"official"` so Canon stays 100% free of fanon triggers.
    """
    if not text_out or str(persona_variant or "official").strip().lower() == "official":
        return ""
    persona = char_data.get("persona") if isinstance(char_data, dict) else None
    fanon = persona.get("fanon_dc_ba_gallery") if isinstance(persona, dict) else None
    tiers = fanon.get("fanon_tiers") if isinstance(fanon, dict) else None
    lv3 = tiers.get("Lv.3") if isinstance(tiers, dict) else None
    triggers = lv3.get("triggers") if isinstance(lv3, dict) else None
    if not isinstance(triggers, list):
        return ""

    preferred_mode = (
        "whisper_directing"
        if persona_variant == "fanon_pure_love"
        else ("spike_directing" if persona_variant == "fanon_spicy_meme" else "")
    )
    best_mode = ""
    best_score: tuple[int, int] = (0, 0)
    for trig in triggers:
        if not isinstance(trig, dict):
            continue
        mode = str(trig.get("s2_directing_mode") or "").strip()
        if mode not in ("whisper_directing", "spike_directing"):
            continue
        kws = trig.get("trigger_keywords_jp")
        if not isinstance(kws, (list, tuple)):
            continue
        hits = 0
        for kw in kws:
            kw_s = str(kw or "").strip()
            if kw_s and kw_s in text_out:
                hits += 1
        if hits <= 0:
            continue
        pref = 1 if (preferred_mode and mode == preferred_mode) else 0
        score = (pref, hits)
        if score > best_score:
            best_score = score
            best_mode = mode
    return best_mode
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

_BRACKET_TAG_RE = re.compile(r"\[[^\]]+\]")
_CS_WHISPER_HINT_RE = re.compile(r"(静か|囁|ささや|照れ|甘く|優しく|控えめ|小さく|ほっと|おどおど|戸惑)")
_CS_SIGH_HINT_RE = re.compile(r"(ため息|溜息|気だる|眠|落ち着|ゆったり)")
_CS_LAUGH_HINT_RE = re.compile(r"(笑|くす|ふふ|悪戯)")
_CS_ANGRY_HINT_RE = re.compile(r"(怒|厳し|真剣|鋭く|叱|窘|拗ね|強がり)")
_CS_EXCITED_HINT_RE = re.compile(r"(元気|輝|弾ん|明る|嬉し|パニック|慌て|びっくり|得意|熱心|焦って|咳払い)")
_CREDIT_SAVER_MAX_ASCII_TAG_BYTES = 14


def compress_s2_tag_for_credit_saver(
    tag: str,
    *,
    cue: str = "",
    archetype: str = "",
    forbidden: set[str] | frozenset[str] | None = None,
) -> str:
    """
    Convert heavy 30-45 byte Japanese S2 acting tags into short ASCII Fish Audio emotion tags
    (or omit neutral baseline tags) to conserve UTF-8 byte credits on MCP / paid models.
    """
    forb = forbidden or frozenset()
    clean_tag = str(tag or "").strip()
    cue_key = str(cue or "").strip().lower()
    arch = str(archetype or "").strip().lower()

    def _pick(cands: list[str]) -> str:
        for cand in cands:
            if cand and cand not in forb:
                return cand
        return ""

    # Omit baseline / neutral tags entirely in Credit-Saver mode (saves 36-45 B per clause!)
    if cue_key in ("baseline", "ko_baseline", "custom_persona"):
        return ""

    # Already a compact ASCII tag (e.g. [happy], [whisper], [sigh], [excited], [laughs], [angry])
    if (
        clean_tag.startswith("[")
        and clean_tag.endswith("]")
        and clean_tag.isascii()
        and len(clean_tag.encode("utf-8")) <= _CREDIT_SAVER_MAX_ASCII_TAG_BYTES
    ):
        return "" if clean_tag in forb else clean_tag

    if cue_key in ("humid_pure_love", "humid_soft_baseline", "lover_coach_warm", "shy_soft", "ko_shy_soft", "char_lv3_whisper"):
        return _pick(["[whisper]"])
    if cue_key in ("sigh_tired", "ko_sigh"):
        return _pick(["[sigh]"])
    if cue_key in ("laugh_tease", "ko_laugh"):
        return _pick(["[laughs]", "[happy]"])
    if cue_key in ("scold_angry", "serious_spike", "ko_scold"):
        return _pick(["[angry]"])
    if cue_key in ("fanfare_spike", "excited_passion", "ko_excited"):
        if arch in ("cool_downer", "kuudere_deadpan"):
            return ""
        return _pick(["[excited]", "[happy]"])
    if cue_key in ("fluster_panic", "ko_fluster", "char_lv3_spike"):
        if cue_key == "char_lv3_spike" and clean_tag and _CS_ANGRY_HINT_RE.search(clean_tag):
            return _pick(["[angry]"])
        if arch in ("cool_downer", "kuudere_deadpan", "teasing_ojou_ara"):
            return _pick(["[whisper]"])
        return _pick(["[excited]"])

    if not clean_tag:
        return ""
    if _CS_WHISPER_HINT_RE.search(clean_tag):
        return _pick(["[whisper]"])
    if _CS_SIGH_HINT_RE.search(clean_tag):
        return _pick(["[sigh]"])
    if _CS_LAUGH_HINT_RE.search(clean_tag):
        return _pick(["[laughs]", "[happy]"])
    if _CS_ANGRY_HINT_RE.search(clean_tag):
        return _pick(["[angry]"])
    if _CS_EXCITED_HINT_RE.search(clean_tag):
        if arch in ("cool_downer", "kuudere_deadpan"):
            return ""
        return _pick(["[excited]", "[happy]"])
    return ""


def apply_credit_saver_to_tts_text(
    tts_text: str,
    *,
    cue: str = "",
    archetype: str = "",
    forbidden: set[str] | frozenset[str] | None = None,
) -> str:
    """
    Shorten or strip heavy multi-byte `[...]` acting tags inside `tts_text` while keeping
    the spoken dialogue intact and avoiding redundant duplicate tags.
    """
    raw = str(tts_text or "").strip()
    if not raw or "[" not in raw:
        return raw

    seen_tag = False

    def _repl(match: re.Match[str]) -> str:
        nonlocal seen_tag
        tok = match.group(0)
        # Keep short ASCII tags like [happy], [whisper], [pause] (unless baseline-neutral)
        if tok.isascii() and len(tok.encode("utf-8")) <= _CREDIT_SAVER_MAX_ASCII_TAG_BYTES:
            if not seen_tag and str(cue or "").strip().lower() in ("baseline", "ko_baseline", "custom_persona"):
                seen_tag = True
                return ""
            seen_tag = True
            return tok
        short = compress_s2_tag_for_credit_saver(
            tok,
            cue="" if seen_tag else cue,
            archetype=archetype,
            forbidden=forbidden,
        )
        seen_tag = True
        return short

    out = _BRACKET_TAG_RE.sub(_repl, raw)
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return out


def _direct_korean_character_prosody(
    char_data: dict,
    text_out: str,
    clause_rms: float,
    auto_direct: bool,
    boost_offset: float,
    model_hdr: str,
    persona_variant: str,
    credit_saver: bool = False,
) -> dict[str, Any]:
    """Korean-safe prosody directing: preserves character temperature dynamics using universal ASCII Fish s2 tags only."""
    raw_persona = char_data.get("persona") if isinstance(char_data, dict) else None
    persona = raw_persona if isinstance(raw_persona, dict) else {}
    raw_vap = persona.get("voice_acting_profile")
    vap = raw_vap if isinstance(raw_vap, dict) else {}
    arch = str(vap.get("archetype") or "gentle_earnest_passion")
    base_temp = _safe_cfg_float(vap.get("base_temperature", 0.72), 0.72, 0.3, 1.2)
    spike_temp = _safe_cfg_float(vap.get("spike_temperature", 0.84), 0.84, 0.3, 1.2)
    top_p = _safe_cfg_float(vap.get("top_p", 0.80), 0.80, 0.1, 1.0)
    rep_pen = _safe_cfg_float(vap.get("repetition_penalty", 1.13), 1.13, 0.8, 2.0)
    raw_forb = vap.get("forbidden_s2_tags")
    forbidden = set(raw_forb) if isinstance(raw_forb, (list, tuple, set, frozenset)) else set()

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

    base_tempo = _parse_tempo_baseline(vap)
    speed = 1.00
    cool_headroom = 0.0
    if auto_direct:
        variant_speed, target_temp, chosen_tag, cue, cool_headroom = compute_variant_tts_prosody(
            persona_variant,
            cue=cue,
            archetype=arch,
            base_temp=base_temp,
            spike_temp=spike_temp,
            current_temp=target_temp,
            current_tag=chosen_tag,
            whisper_tag="[whisper]",
            spike_tag="[excited]",
            default_tag="",
            pick_tag_fn=lambda cands: _pick_ascii([t for t in cands if t.isascii()]),
            text_out=text_out,
        )
        speed = round(max(0.85, min(1.18, base_tempo * variant_speed)), 2)
        final_temp = round(max(0.48, min(0.92, target_temp + boost_offset)), 2)
        if arch in ("cool_downer", "kuudere_deadpan") and boost_offset <= 0.02:
            final_temp = round(min(final_temp, spike_temp + cool_headroom + max(0.0, boost_offset)), 2)
    else:
        final_temp = round(base_temp, 2)
        chosen_tag = ""

    if auto_direct and credit_saver:
        chosen_tag = compress_s2_tag_for_credit_saver(
            chosen_tag,
            cue=cue,
            archetype=arch,
            forbidden=forbidden,
        )

    use_s2_tags = auto_direct and bool(chosen_tag) and model_hdr.strip().lower().startswith("s2")
    tts_text = f"{chosen_tag} {text_out}".strip() if use_s2_tags else text_out
    return {
        "clean_text": text_out,
        "tts_text": tts_text,
        "s2_tag": chosen_tag if use_s2_tags else "",
        "cue": cue,
        "archetype": arch,
        "forbidden_s2_tags": sorted(forbidden),
        "persona_variant": persona_variant,
        "speed": speed,
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
    credit_saver: bool = False,
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
            credit_saver=credit_saver,
        )
    raw_persona = char_data.get("persona") if isinstance(char_data, dict) else None
    persona = raw_persona if isinstance(raw_persona, dict) else {}
    raw_vap = persona.get("voice_acting_profile")
    vap = raw_vap if isinstance(raw_vap, dict) else {}
    arch = str(vap.get("archetype") or "gentle_earnest_passion")
    base_temp = _safe_cfg_float(vap.get("base_temperature", 0.72), 0.72, 0.3, 1.2)
    spike_temp = _safe_cfg_float(vap.get("spike_temperature", 0.84), 0.84, 0.3, 1.2)
    top_p = _safe_cfg_float(vap.get("top_p", 0.85), 0.85, 0.1, 1.0)
    rep_pen = _safe_cfg_float(vap.get("repetition_penalty", 1.13), 1.13, 0.8, 2.0)
    default_tag = str(vap.get("default_s2_tag") or "")
    spike_tag = str(vap.get("spike_directing") or vap.get("spike_s2_tag") or "")
    whisper_tag = str(vap.get("whisper_directing") or vap.get("whisper_s2_tag") or "")
    raw_allowed = vap.get("allowed_s2_tags")
    allowed = list(raw_allowed) if isinstance(raw_allowed, (list, tuple, set, frozenset)) else []
    raw_forb = vap.get("forbidden_s2_tags")
    forbidden = set(raw_forb) if isinstance(raw_forb, (list, tuple, set, frozenset)) else set()

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
    open_react_kind = _match_character_opening_reaction(vap, text_out)

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
    elif _CUE_FLUSTER_RE.search(text_out) or open_react_kind == "fluster":
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
    elif persona_variant in ("fanon_spicy_meme", "fanon_optimal_blend") and _CUE_MEME_PROTEST_RE.search(text_out):
        cue = "meme_spicy_spike"
        target_temp = 0.25 * base_temp + 0.75 * spike_temp
        chosen_tag = _pick_tag(
            [
                spike_tag,
                "[呆れたため息まじりに]",
                "[少し厳しくハキハキと]",
                "[悪戯っぽく笑って]",
                "[少し照れながら静かに]",
                default_tag,
            ]
        )
    elif spike_tag and _CUE_POUT_TEASE_RE.search(text_out):
        cue = "character_spike"
        target_temp = 0.3 * base_temp + 0.7 * spike_temp
        chosen_tag = _pick_tag([spike_tag, default_tag])
    elif _CUE_SIGH_RE.search(text_out) or open_react_kind == "sigh":
        cue = "sigh_tired"
        target_temp = base_temp
        if _CUE_WRITTEN_SIGH_START_RE.search(text_out) or open_react_kind == "sigh":
            chosen_tag = _pick_tag(["[呆れたため息まじりに]", "[気だるそうにゆったりと]", "[気だるそうに]", "[落ち着いた低い声で]", default_tag])
        else:
            chosen_tag = _pick_tag(["[sigh]", "[呆れたため息まじりに]", "[気だるそうに]", default_tag])
    elif _CUE_LAUGH_RE.search(text_out) or open_react_kind == "laugh":
        cue = "laugh_tease"
        target_temp = 0.5 * (base_temp + spike_temp) + (
            0.02 if persona_variant == "fanon_spicy_meme" else (0.01 if persona_variant == "fanon_optimal_blend" else 0.0)
        )
        if _CUE_WRITTEN_LAUGH_START_RE.search(text_out) or open_react_kind == "laugh":
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
    elif (lv3_mode := _match_character_lv3_trigger(char_data, text_out, persona_variant)):
        if lv3_mode == "whisper_directing":
            cue = "char_lv3_whisper"
            target_temp = 0.55 * base_temp + 0.45 * spike_temp
            chosen_tag = _pick_tag(
                [
                    whisper_tag,
                    "[少し照れながら静かに]",
                    "[甘く余裕のある声で]",
                    "[不意を突かれて照れながら]",
                    "[ほっとして嬉しそうに]",
                    "[whisper]",
                    default_tag,
                ]
            )
        else:
            cue = "char_lv3_spike"
            target_temp = 0.25 * base_temp + 0.75 * spike_temp
            chosen_tag = _pick_tag(
                [
                    spike_tag,
                    "[呆れたため息まじりに]",
                    "[少し厳しくハキハキと]",
                    "[悪戯っぽく笑って]",
                    "[恥ずかしそうに慌てながら]",
                    default_tag,
                ]
            )
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

    base_tempo = _parse_tempo_baseline(vap)
    speed = 1.00
    cool_headroom = 0.0
    if auto_direct:
        variant_speed, target_temp, chosen_tag, cue, cool_headroom = compute_variant_tts_prosody(
            persona_variant,
            cue=cue,
            archetype=arch,
            base_temp=base_temp,
            spike_temp=spike_temp,
            current_temp=target_temp,
            current_tag=chosen_tag,
            whisper_tag=whisper_tag,
            spike_tag=spike_tag,
            default_tag=default_tag,
            pick_tag_fn=_pick_tag,
            text_out=text_out,
        )
        speed = round(max(0.85, min(1.18, base_tempo * variant_speed)), 2)
        final_temp = round(max(0.48, min(0.94, target_temp + boost_offset)), 2)
        if arch in ("cool_downer", "kuudere_deadpan") and boost_offset <= 0.02:
            final_temp = round(min(final_temp, spike_temp + cool_headroom + max(0.0, boost_offset)), 2)
        if credit_saver:
            chosen_tag = compress_s2_tag_for_credit_saver(
                chosen_tag,
                cue=cue,
                archetype=arch,
                forbidden=forbidden,
            )
    else:
        final_temp = round(base_temp, 2)
        chosen_tag = ""

    use_s2_tags = auto_direct and bool(chosen_tag) and model_hdr.strip().lower().startswith("s2")
    tts_text = f"{chosen_tag} {text_out}".strip() if use_s2_tags else text_out
    if credit_saver and "[" in tts_text:
        tts_text = apply_credit_saver_to_tts_text(
            tts_text,
            cue=cue,
            archetype=arch,
            forbidden=forbidden,
        )
    return {
        "clean_text": text_out,
        "tts_text": tts_text,
        "s2_tag": chosen_tag if use_s2_tags else "",
        "cue": cue,
        "archetype": arch,
        "forbidden_s2_tags": sorted(forbidden),
        "persona_variant": persona_variant,
        "speed": speed,
        "temperature": final_temp,
        "top_p": round(min(top_p, 0.80), 2),
        "repetition_penalty": round(rep_pen, 2),
    }


def load_bridge_config_with_report() -> ConfigLoadReport:
    return load_user_config_with_report(app_root=ROOT_DIR)


def load_bridge_config() -> dict:
    return load_user_config(app_root=ROOT_DIR)


def save_bridge_config(cfg: dict) -> None:
    save_user_config(cfg)


def load_all_character_configs() -> dict[str, dict]:
    """Load all config/characters/*.json sorted by Korean name using a signature-validated cache."""
    if not CHARACTERS_DIR.is_dir():
        try:
            CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return {}
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


APP_DISPLAY_NAME = "MomoTalk Live"
_VOICED_CORE_NPC_IDS: frozenset[str] = frozenset({"GSC_President"})


def is_momotalk_released_character(ch: dict[str, Any] | None) -> bool:
    """
    Return True iff a character should appear in the MomoTalk Live roster:
    - All playable students (`category != "npc"`), OR
    - Official voiced core NPCs (non-zero `character_id`, non-empty `local_voice_packs`,
      or explicitly allowlisted voiced story NPCs such as `GSC_President`).
    Excludes unvoiced/unreleased story NPCs (e.g. Prof_Niyaniya, Hiromi, Kuzunoha)
    while keeping their persona JSON files intact under `config/characters/`.
    """
    if not isinstance(ch, dict):
        return False
    cat = str(ch.get("category") or "playable").strip().lower()
    if cat != "npc":
        return True
    cid = str(ch.get("id") or "").strip()
    if cid in _VOICED_CORE_NPC_IDS:
        return True
    try:
        if int(ch.get("character_id") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    lvp = ch.get("local_voice_packs")
    if isinstance(lvp, dict):
        for lang_key in ("jp", "kr"):
            packs = lvp.get(lang_key)
            if isinstance(packs, list) and any(str(p or "").strip() for p in packs):
                return True
    return False


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
                if (
                    not is_safe_entity_id(eid)
                    or not is_safe_entity_id(bcid)
                    or bcid not in characters
                    or not is_momotalk_released_character(characters.get(bcid))
                ):
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
        if cid in covered_bases or not is_safe_entity_id(cid) or not is_momotalk_released_character(ch):
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
    try:
        devs = sd.query_devices()
        hostapis = sd.query_hostapis()
        inputs = []
        outputs = []
        for idx, d in enumerate(devs):
            h_idx = d.get("hostapi")
            api_name = (
                hostapis[h_idx]["name"]
                if isinstance(h_idx, int) and 0 <= h_idx < len(hostapis)
                else "Unknown"
            )
            if "WDM-KS" in api_name:
                continue
            sr = int(round(d.get("default_samplerate") or 44100))
            label = f"[{idx}] {d['name']} ({api_name})"
            if d["max_input_channels"] > 0:
                inputs.append((idx, label, sr))
            if d["max_output_channels"] > 0:
                outputs.append((idx, label, sr))
        return inputs, outputs
    except Exception:
        return [], []


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
    """Attach a lightweight, screen-clamped, non-blocking hover tooltip to any Tkinter widget."""
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
            win = tk.Toplevel(widget)
            win.withdraw()
            win.wm_overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            box = tk.Frame(
                win,
                bg="#1E293B",
                highlightbackground="#475569",
                highlightthickness=1,
            )
            box.pack()
            tk.Label(
                box,
                text=msg,
                bg="#1E293B",
                fg="#F8FAFC",
                font=("Malgun Gothic", 8),
                padx=8,
                pady=4,
                wraplength=340,
                justify="left",
            ).pack()
            win.update_idletasks()
            tw = max(40, int(win.winfo_reqwidth() or 160))
            th = max(20, int(win.winfo_reqheight() or 28))
            wx = int(widget.winfo_rootx())
            wy = int(widget.winfo_rooty())
            wh = max(1, int(widget.winfo_height() or widget.winfo_reqheight() or 24))
            x = wx + 8
            y = wy + wh + 6
            cx, cy = clamp_window_position(win, x, y, width=tw, height=th, margin=8)
            # If clamped upward over the widget itself, flip cleanly above the widget.
            if cy < wy + wh and wy - th - 6 >= 8:
                cx, cy = clamp_window_position(win, x, wy - th - 6, width=tw, height=th, margin=8)
            win.wm_geometry(f"+{cx}+{cy}")
            win.deiconify()
            tip_state["win"] = win
        except Exception:
            pass

    def _on_enter(_event: Any = None) -> None:
        _hide()
        try:
            tip_state["after_id"] = widget.after(360, _show_now)
        except Exception:
            pass

    widget.bind("<Enter>", _on_enter, add="+")
    widget.bind("<Leave>", _hide, add="+")
    widget.bind("<ButtonPress>", _hide, add="+")
    widget.bind("<Destroy>", _hide, add="+")


class AruFishBridgeApp:
    def __init__(self, root: tk.Tk, *, enable_colab_on_startup: bool | None = None) -> None:
        self.root = root
        self.root.title(APP_DISPLAY_NAME)
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
        fit_window_to_screen(
            self.root,
            preferred_w=1160,
            preferred_h=780,
            min_w=820,
            min_h=520,
            max_screen_ratio=0.92,
        )
        self.root.configure(bg="#F3F5F8")
        self.root.bind_class("Entry", "<Control-KeyPress>", self._fix_korean_ime_shortcuts)
        self.root.bind_all("<Control-KeyPress>", self._handle_main_window_shortcut, add="+")

        self._config_load_report: ConfigLoadReport = load_bridge_config_with_report()
        self.cfg = self._config_load_report.config
        if enable_colab_on_startup is False and bool(self.cfg.get("colab_s2pro_enabled", False)):
            self.cfg["colab_s2pro_enabled"] = False
            if not str(self.cfg.get("fish_model_header") or "").strip():
                self.cfg["fish_model_header"] = "s2.1-pro-free"
            try:
                save_user_config(self.cfg)
            except Exception:
                pass
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
            if init_cid not in self.characters or init_cid not in self.momotalk_entry_map:
                if "Aru" in self.momotalk_entry_map:
                    init_cid = "Aru"
                elif self.momotalk_entries:
                    init_cid = str(self.momotalk_entries[0]["base_cid"])
                elif "Aru" in self.characters:
                    init_cid = "Aru"
                else:
                    init_cid = next(iter(self.characters), "Aru")
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
        self._memory_summary_pending_rescan: set[tuple[str, str, str]] = set()
        self._memory_summary_inflight_lock = threading.Lock()
        self._memory_summary_worker_started = False
        self._memory_recovery_scheduled = False
        self._tts_error_notice_shown: bool = False
        self._last_mcp_tts_error_msg: str = ""
        self._last_tk_cb_err_mono: float = 0.0
        self._idle_force_profile_card = False
        self._history_img_refs: list[tk.PhotoImage] = []
        self.temporary_call_var = tk.BooleanVar(value=bool(self.cfg.get("temporary_call_mode", False)))

        self.running = False
        self.companion_running = False
        self._rvc_proc: subprocess.Popen[Any] | None = None
        self._tools_menu: tk.Menu | None = None
        self._rvc_runtime_available: bool = is_rvc_runtime_available(ROOT_DIR)
        self.stop_playback_flag = threading.Event()
        self.playback_gen_id = 0
        self._playback_lock = threading.Lock()
        self._zero_shot_cache_lock = threading.Lock()
        self._oneshot_tts_lock = threading.Lock()
        self.whisper_model: Any = None
        self.whisper_loading = False
        self._whisper_lock = threading.Lock()
        self._whisper_reload_lock = threading.Lock()
        self._whisper_load_gen = 0
        self._whisper_is_cuda = False
        self._whisper_loaded_model = ""
        self._whisper_compute_type = ""
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
        self._trigger_cache_loaded_dir: Path | None = None
        self.trigger_cache_lock = threading.Lock()

        self._sticky_ja_hits = 0
        self._active_capture_speaking = False
        self._active_stt_busy = False
        self._active_tts_inflight = 0
        self._active_tts_lock = threading.Lock()
        self._active_play_sr: int = FISH_PCM_SR
        self._active_need_resample: bool = False
        self._output_stream_ready = threading.Event()
        self._output_stream_ready.set()
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
            min_idle_sec=_safe_cfg_float(self.cfg.get("idle_ping_min_sec", 45.0), 45.0, 5.0, 3600.0),
            max_idle_sec=_safe_cfg_float(self.cfg.get("idle_ping_max_sec", 75.0), 75.0, 5.0, 3600.0),
            max_consecutive_pings=_safe_cfg_int(self.cfg.get("afk_max_consecutive_pings", 2), 2, 1, 20),
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
            on_stream_open=self._inc_pa_stream,
            on_stream_close=self._dec_pa_stream,
        )
        self._companion_turn_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=8)
        self._companion_turn_wakeup = threading.Event()
        self._companion_clause_seq = 0
        self._companion_cancel_event = threading.Event()
        self._companion_session_lock = threading.Lock()
        self._companion_session_id: int = 0
        self._audio_session_id: int = 0
        self._login_confirm_lock = threading.Lock()
        self._login_confirm_inflight: bool = False
        self._provider_switch_lock = threading.Lock()
        self._provider_switch_seq: int = 0
        self._web_model_discover_lock = threading.Lock()
        self._web_model_discover_inflight: bool = False
        self._turn_interrupt_lock = threading.Lock()
        self._active_turn_interrupt = threading.Event()
        self._barge_in_ducking = False
        self._recent_tts_jp_lines: deque[str] = deque(maxlen=20)
        self._recent_tts_turn_openers: deque[str] = deque(maxlen=6)
        self._companion_ui_lock = threading.Lock()
        self._companion_session_ready = False
        self._companion_mic_error = ""

        self.watchdog = ProactiveWatchdog(
            enabled=bool(self.cfg.get("proactive_enable", True)),
            silence_timeout_sec=_safe_cfg_float(self.cfg.get("proactive_timeout_sec", 18.0), 18.0, 5.0, 3600.0),
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
        self.api_key_var = tk.StringVar(value=str(self.cfg.get("fish_api_key", "") or ""))
        raw_voice_map = self.cfg.get("saved_voices_kr") if init_rlang == "ko" else self.cfg.get("saved_voices")
        init_voice_map = raw_voice_map if isinstance(raw_voice_map, dict) else {}
        saved_char_voice = str(
            init_voice_map.get(init_cid, "" if init_rlang == "ko" else (self.cfg.get("fish_reference_id", "") or "")) or ""
        )
        self.ref_id_var = tk.StringVar(value=saved_char_voice)
        self.model_hdr_var = tk.StringVar(value=_safe_cfg_model_header(self.cfg.get("fish_model_header", "s2.1-pro-free")))
        saved_lang = str(self.cfg.get("stt_language", "auto") or "auto")
        self.stt_lang_var = tk.StringVar(value=STT_LANG_LABELS.get(saved_lang, STT_LANG_LABELS["auto"]))
        saved_speed = str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive")
        self.speed_mode_var = tk.StringVar(value=SPEED_MODE_LABELS.get(saved_speed, SPEED_MODE_LABELS["expressive"]))
        self.auto_emotion_var = tk.BooleanVar(value=bool(self.cfg.get("auto_emotion_directing", True)))
        saved_boost = _safe_cfg_float(self.cfg.get("expression_boost_offset", 0.0), 0.0, -0.5, 0.5)
        closest_boost = min(BOOST_PRESET_LABELS.keys(), key=lambda k: abs(k - saved_boost))
        self.boost_preset_var = tk.StringVar(value=BOOST_PRESET_LABELS[closest_boost])
        self.pause_sec_var = tk.DoubleVar(value=_safe_cfg_float(self.cfg.get("pause_threshold_sec", 0.33), 0.33, 0.1, 3.0))
        self.vol_var = tk.DoubleVar(
            value=_safe_cfg_float(self.cfg.get("output_volume", 1.0), 1.0, _OUTPUT_VOLUME_MIN, _OUTPUT_VOLUME_MAX)
        )

        self.proactive_prompt_var = tk.BooleanVar(value=bool(self.cfg.get("proactive_prompt_append", True)))
        self.jp_beginner_coach_var = tk.BooleanVar(value=bool(self.cfg.get("jp_beginner_lover_mode", False)))
        self.proactive_enable_var = tk.BooleanVar(value=bool(self.cfg.get("proactive_enable", True)))
        saved_ptime = _safe_cfg_float(self.cfg.get("proactive_timeout_sec", 18.0), 18.0, 5.0, 3600.0)
        closest_ptime = min(PROACTIVE_TIMEOUT_LABELS.keys(), key=lambda k: abs(k - saved_ptime))
        self.proactive_timeout_var = tk.StringVar(value=PROACTIVE_TIMEOUT_LABELS[closest_ptime])
        saved_pvol = _safe_cfg_float(self.cfg.get("proactive_trigger_volume", 1.0), 1.0, 0.1, 2.0)
        closest_pvol = min(PROACTIVE_VOL_LABELS.keys(), key=lambda k: abs(k - saved_pvol))
        self.proactive_vol_var = tk.StringVar(value=PROACTIVE_VOL_LABELS[closest_pvol])

        self.in_choice_var = tk.StringVar()
        self.mic_choice_var = tk.StringVar()
        self.out_choice_var = tk.StringVar()
        self.trigger_out_choice_var = tk.StringVar()

        # Thread-safe snapshots of Tk variables read by background audio/TTS/STT threads
        self._live_out_choice: str = ""
        self._live_in_choice: str = ""
        self._live_mic_choice: str = ""
        self._live_trigger_out_choice: str = ""
        self._live_vol: float = _safe_cfg_float(
            self.cfg.get("output_volume", 1.0), 1.0, _OUTPUT_VOLUME_MIN, _OUTPUT_VOLUME_MAX
        )
        self._live_proactive_vol_lbl: str = self.proactive_vol_var.get()
        self._live_speed_mode_lbl: str = self.speed_mode_var.get()
        self._live_auto_emotion: bool = bool(self.auto_emotion_var.get())
        self._live_boost_preset_lbl: str = self.boost_preset_var.get()
        self._live_persona_variant_lbl: str = self.persona_variant_var.get()
        self._live_stt_lang_lbl: str = self.stt_lang_var.get()
        self._bind_live_var_mirrors()

        self.companion_proactive_var = tk.BooleanVar(value=bool(self.cfg.get("companion_proactive_enable", True)))
        self.companion_state_var = tk.StringVar(value="⚪ 대기 중 (시작 버튼을 눌러주세요)")
        self.companion_meter_var = tk.StringVar(value="░░░░░░░░░░░░ (마이크 대기 중)")
        self.companion_backend_badge_var = tk.StringVar()
        self.companion_range_var = tk.StringVar()
        init_provider = normalize_ai_provider(self.cfg.get("ai_provider", "gemini"))
        self.cfg["ai_provider"] = init_provider
        self.ai_provider_var = tk.StringVar(value=init_provider)
        self._browser_toggle_var = tk.StringVar(value=f"🌐 {AI_PROVIDER_SHORT_NAMES[init_provider]} 화면 보기")

        self._log_history: deque[str] = deque(maxlen=800)
        self._log_lock = threading.Lock()
        self._status_log_win: tk.Toplevel | None = None
        self._status_log_box: scrolledtext.ScrolledText | None = None
        self._status_log_auto_scroll_var: tk.BooleanVar | None = None
        self._status_log_filter_var: tk.StringVar | None = None
        self._status_log_count_var: tk.StringVar | None = None
        self._roster_voice_only: bool = False
        self._last_roster_filter_key: tuple[Any, ...] | None = None
        self._subtitle_font_size: int = _safe_cfg_int(self.cfg.get("subtitle_font_size", 11), 11, 9, 18)
        self._subtitle_user_scrolled_up: bool = False
        self._footer_whisper_lbl: tk.Label | None = None

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

        for _prov in ("gemini", "claude", "chatgpt"):
            self._get_active_api_model(_prov)
        for _prov, _lkey in (
            ("gemini", "api_models"),
            ("claude", "claude_api_models"),
            ("chatgpt", "openai_api_models"),
        ):
            if isinstance(self.cfg.get(_lkey), list):
                self.cfg[_lkey] = self._sanitize_api_model_list(self.cfg.get(_lkey), _prov)

        self._extra_api_models: list[str] = self._sanitize_api_model_list(self.cfg.get("api_models", []), "gemini")
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
            raw_sub_engine == "auto_inline"
            or raw_sub_engine.startswith(("web:", "api:", "claude_web:", "claude_api:", "chatgpt_web:", "openai_api:"))
        ):
            raw_sub_engine = "auto_inline"
        for _sub_pref, _sub_prov in (("api:", "gemini"), ("claude_api:", "claude"), ("openai_api:", "chatgpt")):
            if raw_sub_engine.startswith(_sub_pref):
                _sub_mid = extract_safe_api_model_id(raw_sub_engine[len(_sub_pref) :], provider=_sub_prov)
                if _sub_mid and self._is_matching_provider_api_model(_sub_mid, _sub_prov):
                    raw_sub_engine = f"{_sub_pref}{_sub_mid}"
                else:
                    raw_sub_engine = "auto_inline"
                break
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
        self.gemini_mgr.set_provider(init_provider, browser_model=self._get_active_browser_model(init_provider))
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
        self._bg_asset_thread: threading.Thread | None = None
        self._bg_asset_reindex_requested: bool = False
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
        self._calling_auth_kind: str = ""
        self._calling_stage_text: str = ""
        self._calling_error_text: str = ""
        self._calling_prime_started_mono: float = 0.0
        self._calling_delay_hint_shown: bool = False
        self._call_state_lock = threading.Lock()
        self._startup_after_ids: list[str] = []
        self._pending_ui_after_ids: set[str] = set()
        self._startup_bg_threads: list[threading.Thread] = []
        self._startup_bg_threads_lock = threading.Lock()
        self._stop_keepalive_event = threading.Event()
        self._log_dir_ready: bool = False
        self._log_dir_ready_path: Path | None = None
        self._zero_shot_wav_cache: dict[str, tuple[int, bytes]] = {}
        self._search_index_sig: Any = None
        self._char_search_cache: list[tuple[str, str, bool, str, str]] = []
        self._momotalk_search_default: list[tuple[dict[str, Any], str, str]] = []
        self._momotalk_search_name: list[tuple[dict[str, Any], str, str]] = []
        self._last_companion_meter: str = ""
        self._last_m2_meter: str = ""
        self._subtitle_has_placeholder: bool = False
        self._cfg_persist_lock = threading.Lock()
        self._fish_auto_sync_lock = threading.Lock()
        self._fish_auto_sync_inflight: bool = False
        self._fish_mcp_fallback_logged: bool = False
        self._fish_mcp_auth_warned: bool = False
        self._fish_mcp_quota_warned: bool = False
        self.tts_cache = TtsDiskCache(enabled=bool(self.cfg.get("fish_tts_cache_enabled", True)))
        self.fish_mcp_backend = FishMcpBackend(
            load_tokens_cb=self._load_fish_mcp_tokens,
            save_tokens_cb=self._save_fish_mcp_tokens,
            log_cb=self.log,
        )
        self._colab_s2pro_offline_warned: bool = False
        self.colab_s2pro_backend = ColabS2ProBackend(
            url_getter=lambda: str(self.cfg.get("colab_s2pro_url", "") or "").strip(),
            api_key_getter=lambda: str(self.cfg.get("colab_s2pro_api_key", "") or "").strip(),
            steam_dir_getter=lambda: str(self.cfg.get("steam_game_dir", "") or "").strip(),
            log_cb=self.log,
        )

        # Optional local-only Screen Vision & Economics Study Board (absent in public release)
        self._sv_available: bool = _is_screen_vision_available(ROOT_DIR)
        self._sv_cfg: dict[str, Any] = {}
        self._screen_watcher: Any = None
        self._study_board: Any = None
        self._mini_overlay: Any = None
        self._global_hotkeys: Any = None
        self._sv_targets: list[tuple[str, dict[str, Any]]] = []
        self._sv_target_var: tk.StringVar | None = None
        self._sv_status_var: tk.StringVar | None = None
        self._sv_instant_look_busy: bool = False
        self._sv_cleaned_up: bool = False
        if self._sv_available:
            self._init_screen_vision()

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
        _defer_startup(950, self._auto_sync_fish_account_models_async)

        self.root.protocol("WM_DELETE_WINDOW", self._on_app_closing)
        self.root.bind("<Destroy>", self._on_root_destroyed, add="+")
        try:
            self.root.report_callback_exception = self._on_tk_callback_exception
        except Exception:
            pass
        atexit.register(self._cleanup_on_exit)
        if self._config_load_report.had_issues:
            _defer_startup(350, self._announce_startup_config_recovery)
        if not self._has_fish_tts_credentials():
            _defer_startup(300, lambda: self.open_settings_dialog(first_run=True, initial_tab=1))

    def _on_tk_callback_exception(self, exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        tb_raw = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        safe_tb = _redact_secrets(tb_raw, extra_secrets=self._active_secret_values())
        short_err = _redact_secrets(f"{getattr(exc_type, '__name__', 'Error')}: {exc_value}", extra_secrets=self._active_secret_values())
        try:
            self.log(f"⚠️ [UI 콜백 오류] {short_err}")
        except Exception:
            pass
        try:
            crash_log = get_logs_dir() / "bridge_crash.log"
            crash_log.parent.mkdir(parents=True, exist_ok=True)
            if not crash_log.is_symlink():
                with crash_log.open("a", encoding="utf-8") as fh:
                    fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] UI Callback Error\n{safe_tb}\n")
        except Exception:
            pass
        now_m = time.monotonic()
        if (
            not getattr(self, "_closing", False)
            and (now_m - getattr(self, "_last_tk_cb_err_mono", 0.0)) >= 10.0
        ):
            self._last_tk_cb_err_mono = now_m
            try:
                if self.root.winfo_exists() and self.root.winfo_viewable():
                    messagebox.showerror(
                        "UI 동작 오류",
                        f"화면 동작 중 예기치 못한 오류가 발생했습니다.\n\n{short_err[:240]}\n\n"
                        "문제가 계속되면 [🧰 도구 → 🔄 설정 기본값 복원]을 실행해 주세요.",
                        parent=self.root,
                    )
            except Exception:
                pass

    def _announce_startup_config_recovery(self) -> None:
        report = self._config_load_report
        if report is None or not report.had_issues:
            return
        if report.repaired_fields:
            preview = ", ".join(report.repaired_fields[:8])
            if len(report.repaired_fields) > 8:
                preview += f" 외 {len(report.repaired_fields) - 8}건"
            self.log(f"🛠️ [설정 자동 보정] 잘못된 설정 {len(report.repaired_fields)}개 항목을 안전한 기본값으로 보정했습니다: {preview}")
        if report.fish_api_key_dpapi_failed:
            self.log("🔑 [Fish API 키 안내] 저장된 키를 복호화하지 못했습니다(다른 PC/Windows 계정). ⚙️ 설정에서 다시 입력해 주세요.")
            if not getattr(self, "_closing", False):
                try:
                    messagebox.showwarning(
                        "Fish Audio API 키 재입력 안내",
                        "이전에 저장된 Fish Audio API 키를 현재 Windows 계정에서 복호화하지 못했습니다.\n"
                        "(다른 PC나 다른 Windows 계정에서 복사된 설정 파일일 수 있습니다.)\n\n"
                        "⚙️ 설정 → 1단계에서 Fish Audio API 키를 다시 입력해 주세요.",
                        parent=self.root,
                    )
                except Exception:
                    pass
        if report.status == "corrupt_json":
            fish_txt = "유지됨" if report.fish_api_key_preserved else "복구할 키 없음"
            bak_txt = f"\n• 백업 위치: {report.backup_dir}" if report.backup_dir is not None else ""
            self.log(
                f"🔄 [손상 설정 자동 복구] config.json 오류({report.status})로 기본값을 복원했습니다 "
                f"(Fish API 키: {fish_txt} | AI 브라우저 로그인 세션: 유지됨)."
            )
            if not getattr(self, "_closing", False):
                try:
                    messagebox.showwarning(
                        "설정 파일 자동 복구 안내",
                        "손상된 설정 파일(config.json)을 감지해 안전한 기본값으로 자동 복구했습니다.\n\n"
                        f"• Fish Audio API 키: {fish_txt}\n"
                        "• AI 전용 브라우저 로그인 세션(Gemini·Claude·ChatGPT · BrowserProfile): 유지됨"
                        f"{bak_txt}",
                        parent=self.root,
                    )
                except Exception:
                    pass
        elif report.status == "unreadable":
            self.log(
                "⚠️ [설정 파일 읽기 오류] config.json을 일시적으로 읽을 수 없어 안전한 기본값으로 시작했습니다 "
                "(기존 설정 파일은 덮어쓰지 않고 보존했습니다)."
            )
            if not getattr(self, "_closing", False):
                try:
                    messagebox.showwarning(
                        "설정 파일 읽기 안내",
                        "설정 파일(config.json)을 일시적으로 읽을 수 없어 기본값으로 실행했습니다.\n"
                        "기존 설정 파일은 덮어쓰지 않고 그대로 보존되어 있습니다.\n"
                        "보안 프로그램 잠금이나 권한을 확인한 뒤 다시 실행해 주세요.",
                        parent=self.root,
                    )
                except Exception:
                    pass

    def _bind_live_var_mirrors(self) -> None:
        """Keep plain-Python mirrors of Tk variables so background audio/TTS/STT threads never call Tcl."""

        def _watch(var: tk.Variable, attr: str, transform: Any = str) -> None:
            def _cb(*_args: object) -> None:
                try:
                    setattr(self, attr, transform(var.get()))
                except Exception:
                    pass

            try:
                var.trace_add("write", _cb)
            except Exception:
                pass

        _watch(self.out_choice_var, "_live_out_choice", lambda v: str(v or ""))
        _watch(self.in_choice_var, "_live_in_choice", lambda v: str(v or ""))
        _watch(self.mic_choice_var, "_live_mic_choice", lambda v: str(v or ""))
        _watch(self.trigger_out_choice_var, "_live_trigger_out_choice", lambda v: str(v or ""))
        _watch(self.vol_var, "_live_vol", lambda v: _safe_cfg_float(v, 1.0, _OUTPUT_VOLUME_MIN, _OUTPUT_VOLUME_MAX))
        _watch(self.proactive_vol_var, "_live_proactive_vol_lbl", lambda v: str(v or ""))
        _watch(self.speed_mode_var, "_live_speed_mode_lbl", lambda v: str(v or ""))
        _watch(self.auto_emotion_var, "_live_auto_emotion", bool)
        _watch(self.boost_preset_var, "_live_boost_preset_lbl", lambda v: str(v or ""))
        _watch(self.persona_variant_var, "_live_persona_variant_lbl", lambda v: str(v or ""))
        _watch(self.stt_lang_var, "_live_stt_lang_lbl", lambda v: str(v or ""))

    def _track_startup_bg_thread(self, th: threading.Thread) -> None:
        lock = getattr(self, "_startup_bg_threads_lock", None)
        if lock is None:
            return
        with lock:
            self._startup_bg_threads = [
                t for t in self._startup_bg_threads if callable(getattr(t, "is_alive", None)) and t.is_alive()
            ]
            self._startup_bg_threads.append(th)

    def _stop_momotalk_asset_worker(self, *, join_timeout: float = 2.0) -> None:
        assets = getattr(self, "momotalk_assets", None)
        close_fn = getattr(assets, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
        worker: threading.Thread | None = None
        try:
            with self._bg_asset_cond:
                self._bg_asset_reindex_requested = False
                self._bg_asset_deque.clear()
                self._bg_asset_pending.clear()
                self._bg_asset_cond.notify_all()
                worker = self._bg_asset_thread
        except Exception:
            worker = getattr(self, "_bg_asset_thread", None)
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            try:
                worker.join(timeout=max(0.0, float(join_timeout)))
            except Exception:
                pass

    def _on_root_destroyed(self, event: Any = None) -> None:
        if event is not None and getattr(event, "widget", None) is not self.root:
            return
        self._closing = True
        self._cleanup_screen_vision()
        self._cancel_pending_after_timers()
        self._stop_momotalk_asset_worker(join_timeout=0.5)

    def _cancel_pending_after_timers(self) -> None:
        for aid in list(getattr(self, "_startup_after_ids", [])):
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
        if hasattr(self, "_startup_after_ids"):
            self._startup_after_ids.clear()
        for aid in list(getattr(self, "_pending_ui_after_ids", ())):
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
        if hasattr(self, "_pending_ui_after_ids"):
            self._pending_ui_after_ids.clear()
        for attr in ("_ui_poll_after_id", "_call_anim_after_id", "_call_timer_after_id"):
            aid = getattr(self, attr, None)
            if aid is not None:
                setattr(self, attr, None)
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass

    def _persist_cfg_locked(self) -> None:
        with self._cfg_persist_lock:
            save_bridge_config(self.cfg)

    def _load_fish_mcp_tokens(self) -> str | dict[str, Any] | None:
        raw = self.cfg.get("fish_mcp_oauth_data")
        return raw if raw else None

    def _save_fish_mcp_tokens(self, tokens: dict[str, Any]) -> None:
        with self._cfg_persist_lock:
            if isinstance(tokens, dict) and tokens:
                self.cfg["fish_mcp_oauth_data"] = json.dumps(tokens, ensure_ascii=False, separators=(",", ":"))
            else:
                self.cfg["fish_mcp_oauth_data"] = ""
            save_bridge_config(self.cfg)

    def _is_fish_mcp_authenticated(self) -> bool:
        backend = getattr(self, "fish_mcp_backend", None)
        if backend is None:
            return False
        try:
            return bool(backend.is_authenticated())
        except Exception:
            return False

    def _is_colab_s2pro_enabled(self, overrides: dict[str, Any] | None = None) -> bool:
        if isinstance(overrides, dict) and "colab_s2pro_enabled" in overrides:
            if not bool(overrides.get("colab_s2pro_enabled")):
                return False
            url = str(overrides.get("colab_s2pro_url", "") or "").strip()
            if not url:
                return False
            try:
                validate_colab_server_url(url, resolve_dns=False)
                return True
            except ValueError:
                return False
        if not bool(self.cfg.get("colab_s2pro_enabled", False)):
            return False
        backend = getattr(self, "colab_s2pro_backend", None)
        if backend is None:
            return False
        try:
            return bool(backend.is_configured())
        except Exception:
            return False

    def colab_s2pro_check_health(
        self,
        *,
        override_url: str | None = None,
        override_key: str | None = None,
    ) -> tuple[bool, str, float, list[str]]:
        backend = getattr(self, "colab_s2pro_backend", None)
        if backend is None:
            return False, "Colab S2-Pro 백엔드가 초기화되지 않았습니다.", 0.0, []
        return backend.check_health(override_url=override_url, override_key=override_key)

    def colab_s2pro_shutdown_gpu(
        self,
        *,
        unassign: bool = True,
        override_url: str | None = None,
        override_key: str | None = None,
        timeout: float = 10.0,
    ) -> tuple[bool, str]:
        """Request remote shutdown (and optional Colab GPU runtime unassign) on the S2-Pro server."""
        backend = getattr(self, "colab_s2pro_backend", None)
        if backend is None:
            return False, "Colab S2-Pro 백엔드가 초기화되지 않았습니다."
        ok, msg = backend.request_remote_shutdown(
            unassign=unassign,
            override_url=override_url,
            override_key=override_key,
            timeout=timeout,
        )
        if ok:
            self._colab_s2pro_offline_warned = True
            cfg = getattr(self, "cfg", None)
            if isinstance(cfg, dict) and (
                override_url is None
                or str(override_url).strip().rstrip("/") == str(cfg.get("colab_s2pro_url", "") or "").strip().rstrip("/")
            ):
                lock = getattr(self, "_cfg_persist_lock", None)
                try:
                    if lock is not None:
                        with lock:
                            cfg["colab_s2pro_enabled"] = False
                            save_user_config(cfg)
                    else:
                        cfg["colab_s2pro_enabled"] = False
                        save_user_config(cfg)
                except Exception:
                    pass
            try:
                self.log(f"🛑 [Colab S2-Pro] {msg}")
            except Exception:
                pass
            try:
                self._schedule_ui(self._refresh_companion_badges)
            except Exception:
                pass
        return ok, msg

    def _refresh_colab_gpu_release_button(self) -> None:
        """Show the top-bar `🛑 GPU 반납` shortcut ONLY while Colab S2-Pro mode is active."""
        btn = getattr(self, "btn_hdr_colab_gpu_release", None)
        if btn is None:
            return
        try:
            active = self._is_colab_s2pro_enabled()
            is_mapped = bool(btn.winfo_manager())
            if active and not is_mapped:
                anchor = getattr(self, "btn_hdr_tools", None)
                if anchor is not None:
                    btn.pack(side="right", padx=(0, 6), before=anchor)
                else:
                    btn.pack(side="right", padx=(0, 6))
            elif (not active) and is_mapped:
                btn.pack_forget()
        except Exception:
            pass

    def request_colab_gpu_release_ui(self) -> None:
        """Interactive confirmation + non-blocking remote Colab GPU release triggered from header or Tools menu."""
        if not self._is_colab_s2pro_enabled():
            messagebox.showinfo(
                "Colab GPU 반납",
                "현재 Colab S2-Pro 전용 서버가 활성화되어 있지 않습니다.",
                parent=self.root,
            )
            return
        if not str(self.cfg.get("colab_s2pro_api_key", "") or "").strip():
            messagebox.showwarning(
                "Colab GPU 반납",
                "인증 토큰(Key)이 설정되어 있지 않아 원격 GPU 반납을 실행할 수 없습니다.\n"
                "설정에서 인증 토큰을 확인하거나 Colab [2단계 셀(GPU 반납)]을 직접 실행해 주세요.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Colab GPU 원격 반납",
            "현재 실행 중인 Colab S2-Pro GPU 서버를 원격으로 종료하고 GPU를 반납할까요?\n\n"
            "(다음에 다시 쓰려면 2_코랩_실행하기.bat 또는 Colab 1단계 셀을 실행하면 됩니다.)",
            parent=self.root,
        ):
            return

        btn = getattr(self, "btn_hdr_colab_gpu_release", None)
        if btn is not None:
            try:
                btn.configure(state="disabled", text="🛑 반납 중...")
            except Exception:
                pass

        def _worker() -> None:
            ok, msg = self.colab_s2pro_shutdown_gpu(unassign=True, timeout=10.0)

            def _apply() -> None:
                if btn is not None:
                    try:
                        btn.configure(state="normal", text="🛑 GPU 반납")
                    except Exception:
                        pass
                if getattr(self, "_closing", False):
                    return
                try:
                    if ok:
                        messagebox.showinfo("Colab GPU 반납 완료", msg, parent=self.root)
                    else:
                        messagebox.showwarning("Colab GPU 반납 안내", msg, parent=self.root)
                except Exception:
                    pass

            try:
                self._schedule_ui(_apply)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="colab-gpu-release-ui").start()

    def _prewarm_colab_s2pro_current_char(
        self,
        *,
        char_override: dict[str, Any] | None = None,
        override_url: str | None = None,
        override_key: str | None = None,
    ) -> None:
        """Asynchronously upload/cache the active character's S2-Pro reference before first speech."""
        backend = getattr(self, "colab_s2pro_backend", None)
        if backend is None:
            return
        if override_url is None and not self._is_colab_s2pro_enabled():
            return
        c = char_override if isinstance(char_override, dict) else getattr(self, "current_char", None)
        cid = str((c.get("id") if isinstance(c, dict) else None) or getattr(self, "current_char_id", "") or "").strip()
        if not cid:
            return
        try:
            rlang = self._get_reply_language()
            backend.prewarm_character_reference_async(
                cid,
                rlang,
                char_data=c if isinstance(c, dict) else None,
                override_url=override_url,
                override_key=override_key,
            )
        except Exception:
            pass

    def _has_fish_tts_credentials(self, overrides: dict[str, Any] | None = None) -> bool:
        api_key = (
            str(overrides.get("fish_api_key", "") or "").strip()
            if (isinstance(overrides, dict) and "fish_api_key" in overrides)
            else str(self.cfg.get("fish_api_key", "") or "").strip()
        )
        return bool(
            self._is_colab_s2pro_enabled(overrides=overrides)
            or api_key
            or self._is_fish_mcp_authenticated()
        )

    def _is_tts_cache_enabled(self, overrides: dict[str, Any] | None = None) -> bool:
        if isinstance(overrides, dict) and "fish_tts_cache_enabled" in overrides:
            enabled = bool(overrides.get("fish_tts_cache_enabled"))
            cache = getattr(self, "tts_cache", None)
            return enabled and (cache is not None)
        enabled = bool(self.cfg.get("fish_tts_cache_enabled", True))
        cache = getattr(self, "tts_cache", None)
        if cache is not None:
            cache.enabled = enabled
        return enabled and (cache is not None)

    def _should_prefer_fish_mcp(self, overrides: dict[str, Any] | None = None) -> bool:
        if not self._is_fish_mcp_authenticated():
            return False
        if isinstance(overrides, dict) and "fish_api_key" in overrides:
            has_api_key = bool(str(overrides.get("fish_api_key", "") or "").strip())
        else:
            has_api_key = bool(str(self.cfg.get("fish_api_key", "") or "").strip())
        if isinstance(overrides, dict) and "fish_mcp_primary_mode" in overrides:
            mcp_primary = bool(overrides.get("fish_mcp_primary_mode"))
        else:
            mcp_primary = bool(self.cfg.get("fish_mcp_primary_mode", False))
        return mcp_primary or (not has_api_key)

    def _can_fallback_to_fish_mcp(self, overrides: dict[str, Any] | None = None) -> bool:
        if isinstance(overrides, dict) and "fish_mcp_fallback_enabled" in overrides:
            fb_on = bool(overrides.get("fish_mcp_fallback_enabled"))
        else:
            fb_on = bool(self.cfg.get("fish_mcp_fallback_enabled", True))
        return fb_on and self._is_fish_mcp_authenticated()

    def _make_ephemeral_colab_backend(self, overrides: dict[str, Any] | None) -> ColabS2ProBackend | None:
        """Create an isolated temporary `ColabS2ProBackend` when ephemeral Voice Test overrides Colab URL/Key."""
        if not isinstance(overrides, dict) or "colab_s2pro_url" not in overrides:
            return None
        eff_url = str(overrides.get("colab_s2pro_url", "") or "").strip()
        eff_key = str(overrides.get("colab_s2pro_api_key", self.cfg.get("colab_s2pro_api_key", "")) or "").strip()
        eff_steam = str(overrides.get("steam_game_dir", self.cfg.get("steam_game_dir", "")) or "").strip()
        cfg_url = str(self.cfg.get("colab_s2pro_url", "") or "").strip()
        cfg_key = str(self.cfg.get("colab_s2pro_api_key", "") or "").strip()
        cfg_steam = str(self.cfg.get("steam_game_dir", "") or "").strip()
        if eff_url == cfg_url and eff_key == cfg_key and eff_steam == cfg_steam:
            return None
        return ColabS2ProBackend(
            url_getter=lambda u=eff_url: u,
            api_key_getter=lambda k=eff_key: k,
            steam_dir_getter=lambda s=eff_steam: s,
            log_cb=self.log,
        )

    def fish_mcp_has_cached_balance(self) -> bool:
        backend = getattr(self, "fish_mcp_backend", None)
        if backend is None:
            return False
        try:
            bal = backend.get_cached_balance()
            return isinstance(bal, dict) and bal.get("remaining_credits") is not None
        except Exception:
            return False

    def fish_mcp_is_login_in_progress(self) -> bool:
        backend = getattr(self, "fish_mcp_backend", None)
        if backend is None or not hasattr(backend, "is_login_in_progress"):
            return False
        try:
            return bool(backend.is_login_in_progress())
        except Exception:
            return False

    def fish_mcp_cancel_login(self) -> None:
        backend = getattr(self, "fish_mcp_backend", None)
        if backend is None or not hasattr(backend, "cancel_login"):
            return
        try:
            backend.cancel_login()
        except Exception:
            pass

    def get_fish_mcp_ui_status(self) -> tuple[bool, str]:
        if not self._is_fish_mcp_authenticated():
            return False, "미연결"
        bal = None
        try:
            bal = self.fish_mcp_backend.get_cached_balance()
        except Exception:
            bal = None
        if isinstance(bal, dict) and bal.get("remaining_credits") is not None:
            rem = int(bal["remaining_credits"])
            plan = str(bal.get("plan") or "").strip()
            plan_txt = f" ({plan})" if plan and plan.lower() != "unknown" else ""
            return True, f"연결됨{plan_txt} · 남은 웹 크레딧: {rem:,} B"
        return True, "연결됨 · 잔액 미조회 (새로고침 클릭)"

    def fish_mcp_login_blocking(
        self,
        *,
        status_cb: Any = None,
        cancel_check: Any = None,
        force_new_client: bool = False,
    ) -> tuple[bool, str]:
        def _emit_status(text: str) -> None:
            if callable(status_cb):
                try:
                    status_cb(text)
                except Exception:
                    pass

        def _is_cancelled() -> bool:
            if callable(cancel_check):
                try:
                    return bool(cancel_check())
                except Exception:
                    return False
            return False

        try:
            if _is_cancelled():
                return False, "OAuth 로그인이 취소되었습니다."
            self.fish_mcp_backend.login_interactive(
                timeout_sec=180.0,
                status_cb=status_cb,
                cancel_check=cancel_check,
                force_new_client=force_new_client,
            )
            self._fish_mcp_fallback_logged = False
            self._fish_mcp_auth_warned = False
            self._fish_mcp_quota_warned = False
            bal_note = ""
            if not _is_cancelled():
                _emit_status("크레딧 잔액 동기화 중...")
                try:
                    bal = self.fish_mcp_backend.get_credit_balance(force_refresh=True)
                    rem = bal.get("remaining_credits")
                    plan = str(bal.get("plan") or "").strip()
                    if rem is not None:
                        plan_txt = f" ({plan})" if plan and plan.lower() != "unknown" else ""
                        bal_note = f"\n남은 웹 크레딧{plan_txt}: {int(rem):,} B"
                        self.log(f"💳 [Fish Audio MCP] 연결됨{plan_txt} · 남은 웹 크레딧: {int(rem):,} B")
                    else:
                        raw_obj = bal.get("raw")
                        raw_keys = sorted(raw_obj.keys()) if isinstance(raw_obj, dict) else []
                        self.log(f"[Fish MCP 잔액 초기 조회 경고] 잔액 수치를 찾지 못했습니다 (keys={raw_keys})")
                        bal_note = "\n(참고: 계정은 연결되었으나 서버 응답에서 크레딧 수치를 찾지 못했습니다. [잔액 새로고침]을 눌러 다시 확인해 주세요.)"
                except Exception as bal_exc:
                    safe_bal_err = _redact_secrets(str(bal_exc), extra_secrets=self._active_secret_values())
                    self.log(f"[Fish MCP 잔액 초기 조회 오류] {safe_bal_err}")
                    bal_note = f"\n(초기 잔액 조회 실패: {safe_bal_err}\n[잔액 새로고침] 버튼을 눌러 다시 시도해 주세요.)"
            self.log("🔐 [Fish Audio MCP] 공식 OAuth 웹 크레딧 계정 연결이 완료되었습니다.")
            return True, f"Fish Audio 공식 MCP 웹 크레딧 계정 연결이 완료되었습니다!{bal_note}"
        except Exception as exc:
            err_msg = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
            self.log(f"[Fish Audio MCP 로그인 오류] {err_msg}")
            return False, err_msg

    def fish_mcp_refresh_balance_blocking(self) -> tuple[bool, str]:
        if not self._is_fish_mcp_authenticated():
            return False, "먼저 Fish Audio 공식 MCP 웹 크레딧에 로그인해 주세요."
        try:
            bal = self.fish_mcp_backend.get_credit_balance(force_refresh=True)
            rem = bal.get("remaining_credits")
            plan = str(bal.get("plan") or "").strip()
            if rem is not None:
                plan_txt = f" ({plan})" if plan and plan.lower() != "unknown" else ""
                msg = f"연결됨{plan_txt} · 남은 웹 크레딧: {int(rem):,} B"
                self.log(f"💳 [Fish Audio MCP] {msg}")
                return True, msg
            raw_obj = bal.get("raw")
            raw_keys = sorted(raw_obj.keys()) if isinstance(raw_obj, dict) else []
            err_msg = (
                "크레딧 잔액을 동기화하지 못했습니다 (서버 응답에 잔액 수치 미포함). "
                "잠시 후 [잔액 새로고침]을 다시 시도해 주세요."
            )
            self.log(f"[Fish Audio MCP 잔액 파싱 실패] keys={raw_keys}")
            return False, err_msg
        except Exception as exc:
            err_msg = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
            self.log(f"[Fish Audio MCP 잔액 조회 오류] {err_msg}")
            return False, err_msg

    def fish_mcp_logout(self) -> None:
        self.fish_mcp_backend.logout()
        self._fish_mcp_fallback_logged = False
        self._fish_mcp_auth_warned = False
        self._fish_mcp_quota_warned = False
        self.log("🔌 [Fish Audio MCP] OAuth 연결을 해제하고 저장된 토큰을 삭제했습니다.")

    def get_tts_cache_stats(self) -> dict[str, Any]:
        cache = getattr(self, "tts_cache", None)
        if cache is None:
            return {"file_count": 0, "total_bytes": 0, "total_mb": 0.0, "enabled": False}
        return cache.stats()

    def clear_tts_cache(self) -> int:
        cache = getattr(self, "tts_cache", None)
        if cache is None:
            return 0
        removed = int(cache.clear())
        self.log(f"🗑️ [로컬 TTS 캐시] 캐시 파일 {removed}개를 삭제했습니다.")
        return removed

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

    def _read_tk_var_safe(self, var_attr: str, fallback: Any) -> Any:
        if threading.current_thread() is threading.main_thread() and hasattr(self, var_attr):
            try:
                return getattr(self, var_attr).get()
            except Exception:
                pass
        return fallback

    def _get_reply_language(self) -> str:
        raw = self._read_tk_var_safe("reply_language_var", self.cfg.get("reply_language", "ja"))
        val = str(raw or "ja").strip().lower()
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
        self._persist_cfg_locked()
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

    def _refresh_audio_devices_data_only(self, reinit_portaudio: bool = False) -> None:
        """Thread-safe PortAudio device re-enumeration without touching any Tkinter widget or StringVar."""
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

        inputs, outputs = list_audio_devices()
        in_map = {lbl: (idx, sr) for idx, lbl, sr in inputs}
        out_map = {lbl: (idx, sr) for idx, lbl, sr in outputs}
        primary_outputs = list_primary_output_labels(outputs)
        trigger_outputs = list_trigger_output_labels(outputs)
        mic_inputs = [m for m in list_microphone_labels(inputs) if not is_virtual_loopback_mic_label(m)]
        self.inputs = inputs
        self.outputs = outputs
        self.in_map = in_map
        self.out_map = out_map
        self.primary_outputs = primary_outputs
        self.trigger_outputs = trigger_outputs
        self.mic_inputs = mic_inputs

    def refresh_audio_devices(self, reinit_portaudio: bool = True, log_info: bool = False) -> None:
        """Re-enumerate audio endpoints safely (recovering from USB/HDMI hotplug index shifts)."""
        self._refresh_audio_devices_data_only(reinit_portaudio=reinit_portaudio)
        if threading.current_thread() is not threading.main_thread():
            self._schedule_ui(lambda li=log_info: self.refresh_audio_devices(reinit_portaudio=False, log_info=li))
            return

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

        # Resolve microphone input (Mode 1) — never bind virtual cables/loopback as PC mic
        prev_mic = self.mic_choice_var.get().strip() or str(self.cfg.get("mic_input_device") or "").strip()
        if prev_mic.startswith("(") or is_virtual_loopback_mic_label(prev_mic):
            prev_mic = ""
        valid_mics = [m for m in self.mic_inputs if not is_virtual_loopback_mic_label(m)]
        matched_mic = match_device_label_by_name(prev_mic, valid_mics, require_same_api=False)
        if matched_mic:
            self.mic_choice_var.set(matched_mic)
        elif valid_mics:
            self.mic_choice_var.set(valid_mics[0])
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
        prev_mic = self.mic_choice_var.get().strip()
        had_mic_err = bool(self._companion_mic_error)
        self.refresh_audio_devices(reinit_portaudio=not self._is_audio_pipeline_active(), log_info=True)
        self.save_settings_ui(silent=True)
        new_mic = self.mic_choice_var.get().strip()
        if (
            self.companion_running
            and new_mic
            and not new_mic.startswith("(")
            and not is_virtual_loopback_mic_label(new_mic)
            and new_mic in self.in_map
            and (new_mic != prev_mic or had_mic_err or not self.mic_stt.running)
        ):
            self._restart_companion_mic_nonblocking(new_mic)

    def refresh_devices(self) -> None:
        self._on_click_refresh_devices()

    def _init_device_choices(self) -> None:
        self.refresh_audio_devices(reinit_portaudio=False, log_info=False)

    def _start_momotalk_assets_index_async(self) -> None:
        if getattr(self, "_closing", False):
            return
        with self._bg_asset_cond:
            self._bg_asset_reindex_requested = True
            cur_t = self._bg_asset_thread
            if cur_t is not None and cur_t.is_alive():
                self._bg_asset_cond.notify_all()
                return
            t = threading.Thread(
                target=self._momotalk_asset_worker_loop,
                daemon=True,
                name="momotalk-asset-worker",
            )
            self._bg_asset_thread = t
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
        last_refresh = 0.0
        dirty = False
        while not getattr(self, "_closing", False):
            do_reindex = False
            item: tuple[str, str, int, str] | None = None
            with self._bg_asset_cond:
                while (
                    (not self._bg_asset_reindex_requested)
                    and (not self._bg_asset_deque)
                    and (not getattr(self, "_closing", False))
                ):
                    if dirty and (time.monotonic() - last_refresh) >= 0.08:
                        dirty = False
                        last_refresh = time.monotonic()
                        self._trigger_bg_assets_refresh()
                    self._bg_asset_cond.wait(timeout=0.14)
                if getattr(self, "_closing", False):
                    break
                if self._bg_asset_reindex_requested:
                    self._bg_asset_reindex_requested = False
                    do_reindex = True
                elif self._bg_asset_deque:
                    item = self._bg_asset_deque.popleft()
                    self._bg_asset_inflight = item
            if do_reindex:
                try:
                    self.momotalk_assets.ensure_index()
                except Exception:
                    pass
                if getattr(self, "_closing", False):
                    break
                self._schedule_ui(self._on_momotalk_assets_indexed)
                continue
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
                if not self.root.winfo_exists():
                    return
                holder: list[str] = []

                def _run_tracked() -> None:
                    if holder:
                        self._pending_ui_after_ids.discard(holder[0])
                    if getattr(self, "_closing", False):
                        return
                    try:
                        if self.root.winfo_exists():
                            fn()
                    except Exception:
                        pass

                aid = self.root.after(0, _run_tracked)
                holder.append(aid)
                self._pending_ui_after_ids.add(aid)
            except Exception:
                pass
        else:
            self._ui_queue.put(fn)

    def _copy_to_clipboard(self, text: str) -> bool:
        """Copy text to the Windows clipboard via pyperclip with Tk fallback (survives transient clipboard locks)."""
        s = str(text or "")
        try:
            pyperclip.copy(s)
            return True
        except Exception:
            pass
        try:
            if hasattr(self, "root") and self.root is not None and self.root.winfo_exists():
                self.root.clipboard_clear()
                self.root.clipboard_append(s)
                self.root.update_idletasks()
                return True
        except Exception:
            pass
        return False

    def _paste_from_clipboard(self) -> str:
        """Read text from the Windows clipboard via pyperclip with Tk fallback."""
        try:
            raw = pyperclip.paste()
            if raw is not None:
                return str(raw)
        except Exception:
            pass
        try:
            if hasattr(self, "root") and self.root is not None and self.root.winfo_exists():
                return str(self.root.clipboard_get() or "")
        except Exception:
            pass
        return ""

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
        self.lbl_header_title = tk.Label(self._legacy_backing_frame, text=APP_DISPLAY_NAME)
        self.lbl_gpu_status = tk.Label(self._legacy_backing_frame, text="Whisper GPU 로딩 중...")
        self.btn_hdr_browser = tk.Button(
            self._legacy_backing_frame,
            text="browser",
            command=self.toggle_gemini_browser_window,
        )

        # 1. Top MomoTalk Coral-Pink Header Bar ("MomoTalk Live" + Controls)
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
        brand_row = tk.Frame(h_left, bg="#FB7390")
        brand_row.pack(side="left")
        self.lbl_hdr_brand_main = tk.Label(
            brand_row,
            text="MomoTalk",
            font=("Malgun Gothic", 14, "bold"),
            fg="#FFFFFF",
            bg="#FB7390",
        )
        self.lbl_hdr_brand_main.pack(side="left", anchor="s")
        self.lbl_hdr_brand_live = tk.Label(
            brand_row,
            text="Live",
            font=("Malgun Gothic", 8, "bold"),
            fg="#FFE4EC",
            bg="#FB7390",
        )
        self.lbl_hdr_brand_live.pack(side="left", anchor="s", padx=(3, 0), pady=(0, 2))

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
        _attach_hover_tooltip(self.btn_hdr_settings, "환경 설정 열기 (Ctrl+,)")

        self.btn_hdr_tools = tk.Button(
            hdr,
            text="🧰 도구",
            command=self._open_tools_menu,
            bg="#E45C7A",
            fg="#FFFFFF",
            activebackground="#D94B6A",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            bd=0,
            padx=9,
            pady=3,
            cursor="hand2",
        )
        self.btn_hdr_tools.pack(side="right", padx=(0, 6))
        _attach_hover_tooltip(
            self.btn_hdr_tools,
            (
                "통합 도구 (실시간 RVC 모드 · 스팀 음성 추출 · Fish 일괄 업로드 · 로그)"
                if self._rvc_runtime_available
                else "통합 도구 (스팀 음성 추출 · Fish 일괄 업로드 · 보이스 검색 · 로그)"
            ),
        )

        self.btn_hdr_jp_coach = tk.Button(
            hdr,
            text="",
            command=self.toggle_jp_beginner_coach_mode,
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
        )
        self.btn_hdr_jp_coach.pack(side="right", padx=(0, 6))
        self._update_jp_coach_toggle_style()
        _attach_hover_tooltip(
            self.btn_hdr_jp_coach,
            "서툰 일본어 눈높이 배려 모드 (ON 시 쉬운 일상 일본어로 천천히 대화)",
        )

        self.reply_lang_pill = tk.Frame(hdr, bg="#E45C7A", padx=2, pady=2)
        self.reply_lang_pill.pack(side="right", padx=(0, 6))
        self.btn_hdr_lang_ja = tk.Button(
            self.reply_lang_pill,
            text="🇯🇵 일본어",
            command=lambda: self.set_reply_language("ja"),
            relief="flat",
            bd=0,
            padx=8,
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
            padx=8,
            pady=2,
            cursor="hand2",
        )
        self.btn_hdr_lang_ko.pack(side="left")
        self._update_reply_language_toggle_style()
        _attach_hover_tooltip(self.btn_hdr_lang_ja, "캐릭터가 원작 일본어 음성으로 대답합니다")
        _attach_hover_tooltip(self.btn_hdr_lang_ko, "캐릭터가 한국어 말투로 대답합니다")

        self.ai_provider_pill = tk.Frame(hdr, bg="#E45C7A", padx=2, pady=2)
        self.ai_provider_pill.pack(side="right", padx=(0, 6))
        self.btn_hdr_ai_gemini = tk.Button(
            self.ai_provider_pill,
            text="✨ Gemini",
            command=lambda: self.set_ai_provider("gemini"),
            relief="flat",
            bd=0,
            padx=7,
            pady=2,
            cursor="hand2",
        )
        self.btn_hdr_ai_gemini.pack(side="left", padx=(0, 2))
        self.btn_hdr_ai_claude = tk.Button(
            self.ai_provider_pill,
            text="🧡 Claude",
            command=lambda: self.set_ai_provider("claude"),
            relief="flat",
            bd=0,
            padx=7,
            pady=2,
            cursor="hand2",
        )
        self.btn_hdr_ai_claude.pack(side="left", padx=(0, 2))
        self.btn_hdr_ai_chatgpt = tk.Button(
            self.ai_provider_pill,
            text="💬 GPT",
            command=lambda: self.set_ai_provider("chatgpt"),
            relief="flat",
            bd=0,
            padx=7,
            pady=2,
            cursor="hand2",
        )
        self.btn_hdr_ai_chatgpt.pack(side="left")
        self._update_ai_provider_toggle_style()
        _attach_hover_tooltip(self.btn_hdr_ai_gemini, "대화 AI 엔진을 Google Gemini로 전환")
        _attach_hover_tooltip(self.btn_hdr_ai_claude, "대화 AI 엔진을 Anthropic Claude로 전환")
        _attach_hover_tooltip(self.btn_hdr_ai_chatgpt, "대화 AI 엔진을 OpenAI ChatGPT로 전환")

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

        self.btn_hdr_colab_gpu_release = tk.Button(
            hdr,
            text="🛑 GPU 반납",
            command=self.request_colab_gpu_release_ui,
            bg="#BE123C",
            fg="#FFFFFF",
            activebackground="#9F1239",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            bd=0,
            padx=9,
            pady=3,
            cursor="hand2",
        )
        _attach_hover_tooltip(
            self.btn_hdr_colab_gpu_release,
            "실행 중인 Colab S2-Pro GPU 서버를 원격으로 종료하고 GPU를 반납합니다",
        )
        self._refresh_colab_gpu_release_button()

        # Pinned slim app status footer (packed BEFORE main_body so it is NEVER clipped on compact screens)
        app_footer = make_pinned_footer(
            self.root,
            bg="#F8FAFC",
            border_color="#E2E8F0",
            padx=12,
            pady=(3, 4),
        )
        self._app_footer_bar = app_footer
        lbl_ftr_hint = tk.Label(
            app_footer,
            text="Ctrl+F 검색 · Ctrl+, 설정 · Ctrl+M 기억 · Ctrl+L 로그",
            bg="#F8FAFC",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            cursor="hand2",
        )
        lbl_ftr_hint.pack(side="right")
        lbl_ftr_hint.bind("<Button-1>", lambda _e: self.open_settings_dialog(first_run=False, initial_tab=0))
        _attach_hover_tooltip(lbl_ftr_hint, "단축키 안내 (클릭하면 환경 설정이 열립니다)")

        self._footer_whisper_lbl = tk.Label(
            app_footer,
            text="⏳ Whisper 준비 중...",
            bg="#F8FAFC",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            cursor="hand2",
        )
        self._footer_whisper_lbl.pack(side="right", padx=(0, 12))
        self._footer_whisper_lbl.bind("<Button-1>", lambda _e: self.open_status_log_window())
        _attach_hover_tooltip(self._footer_whisper_lbl, "로컬 Whisper 음성인식 상태 (클릭하면 진단 로그 열기)")

        lbl_ftr_backend = tk.Label(
            app_footer,
            textvariable=self.companion_backend_badge_var,
            bg="#F8FAFC",
            fg="#475569",
            font=("Malgun Gothic", 8),
            anchor="w",
            cursor="hand2",
        )
        lbl_ftr_backend.pack(side="left", fill="x", expand=True)
        lbl_ftr_backend.bind("<Button-1>", lambda _e: self.open_settings_dialog(first_run=False, initial_tab=1))
        _attach_hover_tooltip(lbl_ftr_backend, "현재 활성 AI · TTS 엔진 (클릭하면 연결 설정 열기)")

        # 2. Main 3-Column MomoTalk Shell
        main_body = tk.Frame(self.root, bg="#F3F5F8")
        main_body.pack(fill="both", expand=True)

        # Col 1: Left Dark-Slate Navigation Rail (#374254)
        self.nav_rail = tk.Frame(main_body, bg="#374254", width=54)
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
        _attach_hover_tooltip(btn_nav_settings, "환경 설정 (Ctrl+,)")

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
        _attach_hover_tooltip(self._nav_btn_memory, "기억 노트 & 공통 선생님 프로필 (Ctrl+M)")

        # Col 2: Student Roster Pane (Name + Brief Status Message ONLY; adapts cleanly to compact widths)
        roster_pane = tk.Frame(main_body, bg="#FFFFFF", width=315)
        roster_pane.pack(side="left", fill="y")
        roster_pane.pack_propagate(False)
        self._roster_pane = roster_pane
        tk.Frame(main_body, bg="#DCE3EC", width=1).pack(side="left", fill="y")

        def _sync_roster_pane_width(event: Any = None) -> None:
            try:
                bw = int(getattr(event, "width", 0) or main_body.winfo_width() or 0)
                if bw <= 1:
                    return
                target_w = max(240, min(325, int(bw * 0.29)))
                cur_w = int(roster_pane.cget("width") or 315)
                if abs(cur_w - target_w) >= 6:
                    roster_pane.configure(width=target_w)
            except Exception:
                pass

        main_body.bind("<Configure>", _sync_roster_pane_width, add="+")

        r_top = tk.Frame(roster_pane, bg="#FFFFFF", padx=10, pady=10)
        r_top.pack(fill="x")

        r_title_row = tk.Frame(r_top, bg="#FFFFFF")
        r_title_row.pack(fill="x", pady=(0, 8))
        self.btn_roster_sort = tk.Button(
            r_title_row,
            text="기본순 ⇅",
            command=self._toggle_momotalk_sort,
            bg="#F1F5F9",
            fg="#475569",
            activebackground="#E2E8F0",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=2,
            cursor="hand2",
        )
        self.btn_roster_sort.pack(side="right")
        _attach_hover_tooltip(self.btn_roster_sort, "정렬 순서 전환 (기본순 → 이름순 → 💗 현재 언어 하트순)")

        self.btn_roster_voice_filter = tk.Button(
            r_title_row,
            text="💗",
            command=self._toggle_roster_voice_filter,
            bg="#F1F5F9",
            fg="#64748B",
            activebackground="#FFEAF0",
            activeforeground="#D94B6A",
            font=("Segoe UI Emoji", 8, "bold"),
            relief="flat",
            padx=6,
            pady=2,
            cursor="hand2",
        )
        self.btn_roster_voice_filter.pack(side="right", padx=(0, 4))
        _attach_hover_tooltip(self.btn_roster_voice_filter, "현재 언어의 목소리(Fish Audio)가 연결된 학생만 모아보기 토글")

        self.lbl_roster_count = tk.Label(
            r_title_row,
            text="학생",
            bg="#FFFFFF",
            fg="#2A3446",
            font=("Malgun Gothic", 10, "bold"),
            anchor="w",
        )
        self.lbl_roster_count.pack(side="left", fill="x", expand=True)

        s_box = tk.Frame(r_top, bg="#F1F5F9", highlightbackground="#CBD5E1", highlightthickness=1, padx=6, pady=4)
        s_box.pack(fill="x")
        tk.Label(s_box, text="🔍", bg="#F1F5F9", fg="#64748B", font=("Segoe UI Emoji", 9, "bold")).pack(side="left")
        btn_search_clear = tk.Button(
            s_box,
            text="✕",
            command=self._clear_momotalk_search,
            bg="#F1F5F9",
            fg="#94A3B8",
            activebackground="#E2E8F0",
            activeforeground="#334155",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=3,
            pady=0,
            cursor="hand2",
        )
        btn_search_clear.pack(side="right")
        _attach_hover_tooltip(btn_search_clear, "검색어 지우기 (Esc)")
        self.ent_momo_search = tk.Entry(
            s_box,
            textvariable=self.search_var,
            bg="#F1F5F9",
            fg="#1E293B",
            relief="flat",
            font=("Malgun Gothic", 9),
        )
        self.ent_momo_search.pack(side="left", fill="x", expand=True, padx=4)
        self.ent_momo_search.bind("<KeyRelease>", self._on_roster_search_key_release)
        self.ent_momo_search.bind("<Escape>", lambda _e: (self._clear_momotalk_search(), "break")[1])
        self.ent_momo_search.bind("<Down>", lambda _e: (self._step_roster_selection(1), "break")[1])
        self.ent_momo_search.bind("<Up>", lambda _e: (self._step_roster_selection(-1), "break")[1])
        self.ent_momo_search.bind("<Return>", self._on_roster_search_enter)
        _attach_hover_tooltip(
            self.ent_momo_search,
            "이름·일본어명·상태메시지로 빠른 검색 (Ctrl+F · ↑/↓ 이동 · Enter 선택 · Esc 초기화)",
        )

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
        self._detach_roster_wheel = attach_scrollable_mousewheel(
            self.roster_canvas,
            container=list_wrap,
            yview_widget=self.roster_canvas,
            units=2,
            on_scroll=self._scroll_roster_by_units,
            enable_keyboard=True,
        )

        # Col 3: Right Profile / Calling / Live Subtitles Pane
        self._momo_right_pane = tk.Frame(main_body, bg="#FFFFFF")
        self._momo_right_pane.pack(side="left", fill="both", expand=True)

        # Top Character Profile & Emoji Call Button Bar (pack right actions FIRST so buttons NEVER clip!)
        r_hdr = tk.Frame(self._momo_right_pane, bg="#FFFFFF", padx=14, pady=9)
        r_hdr.pack(fill="x")
        tk.Frame(self._momo_right_pane, bg="#E2E8F0", height=1).pack(fill="x")

        r_hdr_actions = tk.Frame(r_hdr, bg="#FFFFFF")
        r_hdr_actions.pack(side="right", padx=(8, 0))
        self._r_hdr_actions = r_hdr_actions

        self.btn_momotalk_call = tk.Button(
            r_hdr_actions,
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
            padx=13,
            pady=6,
            cursor="hand2",
        )
        self.btn_momotalk_call.pack(side="right")
        _attach_hover_tooltip(self.btn_momotalk_call, "통화 시작 / 종료")

        self.btn_momotalk_memory = tk.Button(
            r_hdr_actions,
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
            padx=9,
            pady=6,
            cursor="hand2",
        )
        self.btn_momotalk_memory.pack(side="right", padx=(0, 6))
        _attach_hover_tooltip(self.btn_momotalk_memory, "이 학생의 기억 노트 · 스냅샷 복원 · 선생님 프로필 (Ctrl+M)")

        self.btn_momotalk_ephemeral = tk.Button(
            r_hdr_actions,
            text="🕵️ 임시",
            command=self.toggle_temporary_call_mode,
            bg="#F1F5F9",
            fg="#64748B",
            activebackground="#FEF3C7",
            activeforeground="#92400E",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
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
            r_hdr_actions,
            text="✨ 목소리 생성",
            command=self._on_click_quick_auto_voice,
            bg="#FFF1F4",
            fg="#D94B6A",
            activebackground="#FECDD3",
            activeforeground="#BE185D",
            disabledforeground="#92400E",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=9,
            pady=6,
            cursor="hand2",
        )
        _attach_hover_tooltip(
            self.btn_momotalk_auto_voice,
            lambda: self._format_missing_voice_cta_tooltip(),
        )

        self.lbl_momo_hdr_avatar = tk.Label(r_hdr, bg="#FFFFFF")
        self.lbl_momo_hdr_avatar.pack(side="left", padx=(0, 10))

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
        self.lbl_momo_hdr_persona_badge = tk.Label(
            r_hdr_name_row,
            text="",
            bg="#EFF6FF",
            fg="#1D4ED8",
            font=("Malgun Gothic", 8, "bold"),
            padx=7,
            pady=1,
            cursor="hand2",
        )
        self.lbl_momo_hdr_persona_badge.bind(
            "<Button-1>", lambda _e: self.open_settings_dialog(initial_tab=0)
        )
        _attach_hover_tooltip(
            self.lbl_momo_hdr_persona_badge,
            "현재 활성 페르소나 모드 (클릭하면 설정에서 1 / 2-A~2-D를 바로 변경할 수 있습니다)",
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

        # Overflow-safe scrollable host for the MomoTalk student profile card
        self._momo_card_scroll = ScrollableFrame(
            self._momo_idle_frame,
            bg="#F6F8FB",
            padx=0,
            pady=0,
            bottom_pad=0,
            autohide_scrollbar=True,
        )
        # Clean in-game MomoTalk student profile card (hidden until a student is clicked)
        self._momo_card_outer = tk.Frame(
            self._momo_card_scroll.inner,
            bg="#FFFFFF",
            highlightbackground="#DCE3EC",
            highlightthickness=1,
            padx=28,
            pady=24,
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
            wraplength=320,
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
            wraplength=320,
            justify="center",
        )
        self.lbl_momo_card_voice_status.pack(pady=(4, 0))
        self._momo_card_scroll.canvas.bind("<Configure>", lambda _e: self._sync_momo_card_scroll_layout(), add="+")
        self._momo_card_outer.bind("<Configure>", lambda _e: self._sync_momo_card_scroll_layout(), add="+")
        bind_responsive_wrap(
            self.lbl_momo_card_status,
            pad=64,
            min_wrap=180,
            container=self._momo_card_outer,
            on_reflow=self._sync_momo_card_scroll_layout,
        )
        bind_responsive_wrap(
            self.lbl_momo_card_voice_status,
            pad=64,
            min_wrap=180,
            container=self._momo_card_outer,
            on_reflow=self._sync_momo_card_scroll_layout,
        )

        # Messenger-style past conversation timeline (shown automatically when student has saved history)
        self._momo_history_frame = tk.Frame(self._momo_idle_frame, bg="#F6F8FB")
        hist_bar = tk.Frame(self._momo_history_frame, bg="#FFFFFF", padx=14, pady=8)
        hist_bar.pack(fill="x")
        tk.Frame(self._momo_history_frame, bg="#E2E8F0", height=1).pack(fill="x")

        # Pack right action buttons BEFORE expanding meta label so buttons never clip!
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
        btn_hist_copy = tk.Button(
            hist_bar,
            text="📋 복사",
            command=self._copy_history_text,
            bg="#F1F5F9",
            fg="#475569",
            activebackground="#E2E8F0",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        )
        btn_hist_copy.pack(side="right", padx=(6, 0))
        _attach_hover_tooltip(btn_hist_copy, "표시 중인 대화 기록 전체 복사")

        self.lbl_history_meta = tk.Label(
            hist_bar,
            text="💬 지난 모모톡 기록",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        )
        self.lbl_history_meta.pack(side="left", fill="x", expand=True)

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
        self._detach_history_wheel = attach_scrollable_mousewheel(
            self.history_box,
            container=hist_host,
            yview_widget=self.history_box,
            units=3,
            enable_keyboard=False,
        )
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

        # State 2: Outgoing Phone Calling Waiting Screen (scrollable center + pinned hangup footer!)
        self._momo_calling_frame = tk.Frame(self._momo_center_stack, bg="#263040")
        calling_footer = make_pinned_footer(
            self._momo_calling_frame,
            bg="#1E293B",
            border_color="#334155",
            padx=16,
            pady=(10, 12),
        )
        self.btn_calling_hangup = tk.Button(
            calling_footer,
            text="📵 종료",
            command=self.stop_momotalk_call,
            bg="#EF4444",
            fg="#FFFFFF",
            activebackground="#DC2626",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 11, "bold"),
            relief="flat",
            padx=28,
            pady=8,
            cursor="hand2",
        )
        self.btn_calling_hangup.pack(anchor="center")

        self._momo_calling_scroll = ScrollableFrame(
            self._momo_calling_frame,
            bg="#263040",
            padx=16,
            pady=12,
            bottom_pad=16,
            autohide_scrollbar=True,
        )
        self._momo_calling_scroll.pack(fill="both", expand=True)
        self._calling_top_spacer = tk.Frame(self._momo_calling_scroll.inner, bg="#263040", height=1)
        self._calling_top_spacer.pack(fill="x")
        call_inner = tk.Frame(self._momo_calling_scroll.inner, bg="#263040")
        call_inner.pack(anchor="center", fill="x")
        self._calling_inner_frame = call_inner

        self.calling_canvas = tk.Canvas(
            call_inner,
            width=240,
            height=196,
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
        self.lbl_calling_timer.pack(pady=(2, 4))

        self.lbl_calling_stage = tk.Label(
            call_inner,
            text="",
            bg="#263040",
            fg="#CBD5E1",
            font=("Malgun Gothic", 9),
            wraplength=420,
            justify="center",
        )
        self.lbl_calling_stage.pack(pady=(0, 8))

        self._calling_quick_bar = tk.Frame(call_inner, bg="#263040")
        self.btn_calling_open_browser = tk.Button(
            self._calling_quick_bar,
            text="🌐 브라우저 창 보기",
            command=lambda: self.toggle_gemini_browser_window(force_visible=True),
            bg="#334155",
            fg="#E2E8F0",
            activebackground="#475569",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        )
        self.btn_calling_open_browser.pack()

        self._calling_login_bar = tk.Frame(call_inner, bg="#3B2F2F", padx=12, pady=8)
        self.lbl_calling_login_msg = tk.Label(
            self._calling_login_bar,
            text="🔑 로그인이 필요합니다 (로그인 완료 후 아래 버튼을 눌러 주세요)",
            bg="#3B2F2F",
            fg="#FDE68A",
            font=("Malgun Gothic", 9, "bold"),
            wraplength=420,
            justify="center",
        )
        self.lbl_calling_login_msg.pack(pady=(0, 6))
        lb_row = tk.Frame(self._calling_login_bar, bg="#3B2F2F")
        lb_row.pack()
        self.btn_calling_login_browser = tk.Button(
            lb_row,
            text="🌐 로그인 창 열기",
            command=lambda: self.toggle_gemini_browser_window(force_visible=True),
            bg="#1E40AF",
            fg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        )
        self.btn_calling_login_browser.pack(side="left", padx=(0, 6))
        self.btn_calling_login_confirm = tk.Button(
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
        )
        self.btn_calling_login_confirm.pack(side="left")

        self._calling_error_bar = tk.Frame(call_inner, bg="#3F2429", padx=14, pady=10)
        self.lbl_calling_error_title = tk.Label(
            self._calling_error_bar,
            text="⚠️ 통화 연결에 실패했습니다",
            bg="#3F2429",
            fg="#FDA4AF",
            font=("Malgun Gothic", 10, "bold"),
            wraplength=420,
            justify="center",
        )
        self.lbl_calling_error_title.pack(pady=(0, 4))
        self.lbl_calling_error_detail = tk.Label(
            self._calling_error_bar,
            text="",
            bg="#3F2429",
            fg="#FDE2E4",
            font=("Malgun Gothic", 9),
            wraplength=420,
            justify="center",
        )
        self.lbl_calling_error_detail.pack(pady=(0, 8))
        err_row = tk.Frame(self._calling_error_bar, bg="#3F2429")
        err_row.pack()
        err_row2 = tk.Frame(self._calling_error_bar, bg="#3F2429")
        err_row2.pack(pady=(6, 0))
        self.btn_calling_error_browser = tk.Button(
            err_row,
            text="🌐 창 보기",
            command=lambda: self.toggle_gemini_browser_window(force_visible=True),
            bg="#1E40AF",
            fg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        )
        self.btn_calling_error_retry = tk.Button(
            err_row,
            text="🔄 다시 시도",
            command=self.retry_companion_call_async,
            bg="#16A34A",
            fg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        )
        self.btn_calling_error_settings = tk.Button(
            err_row,
            text="⚙️ 설정",
            command=lambda: self.open_settings_dialog(first_run=False, initial_tab=1),
            bg="#475569",
            fg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        )
        self.btn_calling_error_reset = tk.Button(
            err_row2,
            text="🔄 설정 기본값 복원",
            command=self.reset_settings_to_defaults_ui,
            bg="#991B1B",
            fg="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=10,
            pady=3,
            cursor="hand2",
        )
        self.btn_calling_error_retry.pack(side="left", padx=(0, 6))
        self.btn_calling_error_settings.pack(side="left")
        self.btn_calling_error_reset.pack()

        self._momo_calling_scroll.canvas.bind("<Configure>", lambda _e: self._sync_calling_scroll_layout(), add="+")
        call_inner.bind("<Configure>", lambda _e: self._sync_calling_scroll_layout(), add="+")
        for _c_lbl in (
            self.lbl_calling_stage,
            self.lbl_calling_login_msg,
            self.lbl_calling_error_title,
            self.lbl_calling_error_detail,
        ):
            bind_responsive_wrap(
                _c_lbl,
                pad=52,
                min_wrap=200,
                max_wrap=520,
                container=self._momo_calling_scroll.inner,
                on_reflow=self._sync_calling_scroll_layout,
            )

        # State 3: Connected Live Call Bar + Real-Time Subtitles Host (minimal immersion bar)
        self._momo_connected_bar = tk.Frame(self._momo_center_stack, bg="#2A3446")
        live_top = tk.Frame(self._momo_connected_bar, bg="#2A3446", padx=14, pady=8)
        live_top.pack(fill="x")

        # Pack right-side live call controls BEFORE expanding name label so controls NEVER clip!
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

        # Compact live voice/mic state strip during active call
        live_mic_bar = tk.Frame(self._momo_connected_bar, bg="#1E293B", padx=14, pady=3)
        live_mic_bar.pack(fill="x")
        self.lbl_live_mic_meter = tk.Label(
            live_mic_bar,
            textvariable=self.companion_meter_var,
            bg="#1E293B",
            fg="#94A3B8",
            font=("Consolas", 8),
            anchor="e",
        )
        self.lbl_live_mic_meter.pack(side="right", padx=(8, 0))
        self.lbl_live_call_state = tk.Label(
            live_mic_bar,
            textvariable=self.companion_state_var,
            bg="#1E293B",
            fg="#CBD5E1",
            font=("Malgun Gothic", 8),
            anchor="w",
        )
        self.lbl_live_call_state.pack(side="left", fill="x", expand=True)

        if self._sv_available:
            self._build_screen_vision_bar(self._momo_connected_bar)

        # Collapsible mid-call auth/Cloudflare recovery bar (shown only when login/challenge occurs mid-call)
        self._connected_auth_bar = tk.Frame(self._momo_connected_bar, bg="#3B2F2F", padx=12, pady=6)
        self.btn_connected_auth_confirm = tk.Button(
            self._connected_auth_bar,
            text="✅ 확인 완료",
            command=self.confirm_browser_login_async,
            bg="#16A34A",
            fg="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=10,
            pady=3,
            cursor="hand2",
        )
        self.btn_connected_auth_confirm.pack(side="right")
        self.btn_connected_auth_browser = tk.Button(
            self._connected_auth_bar,
            text="🌐 창 열기",
            command=lambda: self.toggle_gemini_browser_window(force_visible=True),
            bg="#1E40AF",
            fg="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        )
        self.btn_connected_auth_browser.pack(side="right", padx=(0, 6))
        self.lbl_connected_auth_msg = tk.Label(
            self._connected_auth_bar,
            text="🔑 브라우저 로그인/보안 확인이 필요합니다.",
            bg="#3B2F2F",
            fg="#FDE68A",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_connected_auth_msg.pack(side="left", fill="x", expand=True, padx=(0, 8))
        bind_responsive_wrap(self.lbl_connected_auth_msg, pad=220, min_wrap=180, container=self._connected_auth_bar)

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
        sub_bar.pack(fill="x", pady=(0, 4))

        # Pack right-side subtitle utility controls FIRST so they never clip on compact widths
        btn_sub_clear = tk.Button(
            sub_bar,
            text="🧹",
            command=self.clear_subtitles,
            bg="#F1F5F9",
            fg="#475569",
            activebackground="#E2E8F0",
            font=("Segoe UI Emoji", 9, "bold"),
            relief="flat",
            padx=7,
            pady=3,
            cursor="hand2",
        )
        btn_sub_clear.pack(side="right")
        _attach_hover_tooltip(btn_sub_clear, "현재 실시간 자막 화면 비우기")

        btn_sub_copy = tk.Button(
            sub_bar,
            text="📋",
            command=self._copy_subtitles_text,
            bg="#F1F5F9",
            fg="#475569",
            activebackground="#E2E8F0",
            font=("Segoe UI Emoji", 9, "bold"),
            relief="flat",
            padx=7,
            pady=3,
            cursor="hand2",
        )
        btn_sub_copy.pack(side="right", padx=(0, 4))
        _attach_hover_tooltip(btn_sub_copy, "실시간 대사 자막 전체 복사")

        btn_sub_font_inc = tk.Button(
            sub_bar,
            text="A+",
            command=lambda: self._adjust_subtitle_font_size(1),
            bg="#F1F5F9",
            fg="#475569",
            activebackground="#E2E8F0",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=6,
            pady=3,
            cursor="hand2",
        )
        btn_sub_font_inc.pack(side="right", padx=(0, 3))
        _attach_hover_tooltip(btn_sub_font_inc, "자막 글자 크기 키우기")

        btn_sub_font_dec = tk.Button(
            sub_bar,
            text="A-",
            command=lambda: self._adjust_subtitle_font_size(-1),
            bg="#F1F5F9",
            fg="#475569",
            activebackground="#E2E8F0",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=6,
            pady=3,
            cursor="hand2",
        )
        btn_sub_font_dec.pack(side="right", padx=(0, 3))
        _attach_hover_tooltip(btn_sub_font_dec, "자막 글자 크기 줄이기")

        self.btn_sub_jump_bottom = tk.Button(
            sub_bar,
            text="⬇ 최신 자막",
            command=self._jump_subtitles_to_bottom,
            bg="#FFEAF0",
            fg="#D94B6A",
            activebackground="#FECDD3",
            activeforeground="#BE185D",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        )
        _attach_hover_tooltip(self.btn_sub_jump_bottom, "맨 아래 최신 자막으로 이동하고 자동 따라가기 재개")

        tk.Label(
            sub_bar,
            text="💬 실시간 대사 자막",
            bg="#FFFFFF",
            fg="#2A3446",
            font=("Malgun Gothic", 10, "bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        # Dedicated mode selector row so all 3 Korean subtitle mode pills NEVER clip even at minsize 820px
        sub_mode_row = tk.Frame(f4, bg="#FFFFFF")
        sub_mode_row.pack(fill="x", pady=(0, 6))
        self._subtitle_mode_btns.clear()
        for mode_code in SUBTITLE_MODES:
            rb = tk.Radiobutton(
                sub_mode_row,
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
                padx=9,
                pady=3,
                cursor="hand2",
            )
            rb.pack(side="left", padx=(0, 5))
            self._subtitle_mode_btns[mode_code] = rb
        self._update_subtitle_mode_buttons_style()

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
            font=("Malgun Gothic", self._subtitle_font_size),
            bg="#FFFFFF",
            fg="#1E293B",
            wrap="word",
            padx=16,
            pady=12,
            relief="flat",
            bd=0,
        )
        self.subtitle_box.configure(yscrollcommand=self._on_subtitle_yscroll)
        self.subtitle_box.pack(fill="both", expand=True)
        self._detach_subtitle_wheel = attach_scrollable_mousewheel(
            self.subtitle_box,
            container=f4,
            yview_widget=self.subtitle_box,
            units=3,
            enable_keyboard=False,
        )
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
            "system_speaker",
            font=("Malgun Gothic", 9, "bold"),
            foreground="#B45309",
            spacing1=8,
            spacing3=2,
        )
        self.subtitle_box.tag_configure(
            "system_line",
            font=("Malgun Gothic", 10, "bold"),
            foreground="#991B1B",
            background="#FEF2F2",
            lmargin1=10,
            lmargin2=10,
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
        self._apply_subtitle_font_size(self._subtitle_font_size, persist=False)
        self.subtitle_box.bind("<Control-c>", lambda e: None)
        self._rerender_subtitles()

        self.log(f"[MomoTalk 에디션 v5] 블루 아카이브 캐릭터 페르소나 {len(self.characters)}명 + Steam 모모톡 UI 로드 완료!")
        self.log("[맞춤 감정 디렉팅] 쿨·다우너/츤데레/허세폭주/하이텐션 등 캐릭터 고유 텐션에 맞춰 호흡·문장부호·Fish s2 연기 태그가 자동 적용됩니다.")

    # ------------------------------------------------------------------
    # MomoTalk Roster, Profile Card, Calling & Connection UI Controllers
    # ------------------------------------------------------------------

    ROW_H = 68

    def _set_gpu_status_ui(self, text: str, fg: str) -> None:
        try:
            self.lbl_gpu_status.config(text=text, fg=fg)
        except Exception:
            pass
        ftr_lbl = getattr(self, "_footer_whisper_lbl", None)
        if ftr_lbl is not None:
            light_fg = {
                "#4ade80": "#15803D",
                "#fbbf24": "#B45309",
                "#ef4444": "#DC2626",
            }.get(str(fg or "").lower(), "#475569")
            try:
                ftr_lbl.configure(text=text, fg=light_fg)
            except Exception:
                pass

    def _handle_main_window_shortcut(self, event: tk.Event[Any]) -> str | None:
        if getattr(self, "_closing", False):
            return None
        w = getattr(event, "widget", None)
        if w is None:
            return None
        try:
            if w.winfo_toplevel() is not self.root:
                return None
        except Exception:
            return None
        # Ignore Ctrl+Shift+... / Ctrl+Alt+... combos so global screen-vision hotkeys never collide
        state = int(getattr(event, "state", 0) or 0)
        if state & 0x0001:  # Shift
            return None
        ks = str(getattr(event, "keysym", "") or "").lower()
        kc = int(getattr(event, "keycode", 0) or 0)
        if ks == "comma" or kc == 188:
            self.open_settings_dialog(first_run=False, initial_tab=0)
            return "break"
        if ks == "f" or kc == 70:
            ent = getattr(self, "ent_momo_search", None)
            if ent is not None and ent.winfo_exists():
                try:
                    ent.focus_set()
                    ent.selection_range(0, tk.END)
                    ent.icursor(tk.END)
                except Exception:
                    pass
                return "break"
        if ks == "l" or kc == 76:
            self.open_status_log_window()
            return "break"
        if ks == "m" or kc == 77:
            self.open_memory_note_dialog(initial_tab=0 if self._momotalk_char_activated else 1)
            return "break"
        return None

    def _sync_momo_card_scroll_layout(self) -> None:
        sf = getattr(self, "_momo_card_scroll", None)
        card = getattr(self, "_momo_card_outer", None)
        if sf is None or card is None:
            return
        try:
            if not sf.winfo_exists() or not card.winfo_exists():
                return
            vp_h = max(1, int(sf.canvas.winfo_height() or 0))
            card_h = max(1, int(card.winfo_reqheight() or 0))
            target_h = max(vp_h, card_h + 44)
            cur_h = int(sf.inner.cget("height") or 0)
            if abs(cur_h - target_h) >= 2:
                sf.inner.configure(height=target_h)
            sf.refresh_scrollregion(defer=False)
        except Exception:
            pass

    def _sync_calling_scroll_layout(self) -> None:
        sf = getattr(self, "_momo_calling_scroll", None)
        spacer = getattr(self, "_calling_top_spacer", None)
        inner = getattr(self, "_calling_inner_frame", None)
        if sf is None or spacer is None or inner is None:
            return
        try:
            if not sf.winfo_exists():
                return
            vp_h = int(sf.canvas.winfo_height() or 0)
            content_h = int(inner.winfo_reqheight() or 0)
            if vp_h > 40 and content_h > 1:
                pad_top = max(4, min(140, (vp_h - content_h - 28) // 2))
            else:
                pad_top = 4
            cur_h = int(spacer.cget("height") or 1)
            if abs(cur_h - pad_top) >= 3:
                spacer.configure(height=pad_top)
            sf.refresh_scrollregion(defer=True)
        except Exception:
            pass

    def _on_subtitle_yscroll(self, first: str, last: str) -> None:
        try:
            self.subtitle_box.vbar.set(first, last)
        except Exception:
            pass
        try:
            f_val = float(first)
            l_val = float(last)
            scrolled_up = (l_val < 0.96) and not (f_val <= 1e-4 and l_val >= 1.0 - 1e-4)
        except Exception:
            scrolled_up = False
        self._subtitle_user_scrolled_up = scrolled_up
        btn = getattr(self, "btn_sub_jump_bottom", None)
        if btn is not None:
            try:
                is_mapped = bool(btn.winfo_manager())
                if scrolled_up and not is_mapped:
                    btn.pack(side="right", padx=(0, 6))
                elif (not scrolled_up) and is_mapped:
                    btn.pack_forget()
            except Exception:
                pass

    def _jump_subtitles_to_bottom(self) -> None:
        if not hasattr(self, "subtitle_box") or self.subtitle_box is None:
            return
        try:
            self._subtitle_user_scrolled_up = False
            self.subtitle_box.see(tk.END)
            btn = getattr(self, "btn_sub_jump_bottom", None)
            if btn is not None and btn.winfo_manager():
                btn.pack_forget()
        except Exception:
            pass

    def _apply_subtitle_font_size(self, size: int, *, persist: bool = True) -> None:
        clamped = max(9, min(18, int(size)))
        self._subtitle_font_size = clamped
        if hasattr(self, "subtitle_box") and self.subtitle_box is not None:
            try:
                self.subtitle_box.configure(font=("Malgun Gothic", clamped))
                self.subtitle_box.tag_configure("speaker", font=("Malgun Gothic", max(9, clamped - 1), "bold"))
                self.subtitle_box.tag_configure("sensei_speaker", font=("Malgun Gothic", max(8, clamped - 2), "bold"))
                self.subtitle_box.tag_configure("sensei_line", font=("Malgun Gothic", clamped, "bold"))
                self.subtitle_box.tag_configure("system_speaker", font=("Malgun Gothic", max(8, clamped - 2), "bold"))
                self.subtitle_box.tag_configure("system_line", font=("Malgun Gothic", max(9, clamped - 1), "bold"))
                self.subtitle_box.tag_configure("jp_only_line", font=("Malgun Gothic", clamped + 1, "bold"))
                self.subtitle_box.tag_configure("kr_only_line", font=("Malgun Gothic", clamped + 1, "bold"))
                self.subtitle_box.tag_configure("jp_dual_line", font=("Malgun Gothic", clamped, "bold"))
                self.subtitle_box.tag_configure("kr_dual_line", font=("Malgun Gothic", clamped, "bold"))
                self.subtitle_box.tag_configure("pending_line", font=("Malgun Gothic", max(9, clamped - 1)))
                self.subtitle_box.tag_configure("refining_tag", font=("Malgun Gothic", max(8, clamped - 2), "bold"))
                self.subtitle_box.tag_configure("placeholder", font=("Malgun Gothic", max(9, clamped - 1)))
            except Exception:
                pass
        if persist:
            self.cfg["subtitle_font_size"] = clamped
            if getattr(self, "_ui_boot_complete", False):
                self._persist_cfg_locked()

    def _adjust_subtitle_font_size(self, delta: int) -> None:
        self._apply_subtitle_font_size(getattr(self, "_subtitle_font_size", 11) + int(delta), persist=True)

    def _copy_subtitles_text(self) -> None:
        if not hasattr(self, "subtitle_box") or self.subtitle_box is None:
            return
        try:
            txt = self.subtitle_box.get("1.0", tk.END).strip()
            if not txt or self._subtitle_has_placeholder:
                return
            pyperclip.copy(txt)
            self.log("📋 [자막 복사] 현재 실시간 대사 자막을 클립보드에 복사했습니다.")
        except Exception:
            pass

    def _copy_history_text(self) -> None:
        if not hasattr(self, "history_box") or self.history_box is None:
            return
        try:
            txt = self.history_box.get("1.0", tk.END).strip()
            if not txt:
                return
            pyperclip.copy(txt)
            self.log("📋 [기록 복사] 표시 중인 모모톡 대화 기록을 클립보드에 복사했습니다.")
        except Exception:
            pass

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

    def _toggle_roster_voice_filter(self) -> None:
        self._roster_voice_only = not bool(getattr(self, "_roster_voice_only", False))
        btn = getattr(self, "btn_roster_voice_filter", None)
        if btn is not None:
            if self._roster_voice_only:
                btn.configure(bg="#FFEAF0", fg="#D94B6A", text="💗 ON")
            else:
                btn.configure(bg="#F1F5F9", fg="#64748B", text="💗")
        self._refresh_character_filter_list(keep_current=True)

    def _clear_momotalk_search(self) -> None:
        self.search_var.set("")
        self._refresh_character_filter_list(keep_current=True)

    def _on_roster_search_key_release(self, event: tk.Event[Any]) -> None:
        if str(getattr(event, "keysym", "") or "") in ("Up", "Down", "Return", "Escape", "Left", "Right"):
            return
        self._refresh_character_filter_list(keep_current=False)

    def _on_roster_search_enter(self, _event: tk.Event[Any] | None = None) -> str:
        cids = list(getattr(self, "_momotalk_filtered_cids", []) or [])
        if not cids:
            return "break"
        cur = self.current_momotalk_entry_id if self._momotalk_char_activated else ""
        target = cur if cur in cids else cids[0]
        self.select_momotalk_character(target)
        self._ensure_roster_index_visible(cids.index(target))
        return "break"

    def _ensure_roster_index_visible(self, idx: int) -> None:
        if not hasattr(self, "roster_canvas") or idx < 0:
            return
        total = len(getattr(self, "_momotalk_filtered_cids", []) or [])
        if total <= 0:
            return
        cv = self.roster_canvas
        total_h = max(1, total * self.ROW_H)
        view_h = max(self.ROW_H, int(cv.winfo_height() or 400))
        if total_h <= view_h:
            cv.yview_moveto(0.0)
            self._render_roster_visible()
            return
        row_top = idx * self.ROW_H
        row_bot = row_top + self.ROW_H
        top_y = float(cv.canvasy(0))
        bot_y = top_y + float(view_h)
        if row_top < top_y:
            cv.yview_moveto(max(0.0, row_top / float(total_h)))
            self._render_roster_visible()
        elif row_bot > bot_y:
            cv.yview_moveto(min(1.0, max(0.0, (row_bot - view_h) / float(total_h))))
            self._render_roster_visible()

    def _step_roster_selection(self, delta: int) -> None:
        cids = list(getattr(self, "_momotalk_filtered_cids", []) or [])
        if not cids:
            return
        cur = self.current_momotalk_entry_id if self._momotalk_char_activated else ""
        if cur in cids:
            nxt_idx = max(0, min(len(cids) - 1, cids.index(cur) + int(delta)))
        else:
            nxt_idx = 0 if delta >= 0 else len(cids) - 1
        self.select_momotalk_character(cids[nxt_idx])
        self._ensure_roster_index_visible(nxt_idx)

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

    def _scroll_roster_by_units(self, steps: int) -> bool:
        if not hasattr(self, "roster_canvas") or steps == 0:
            return False
        before = self.roster_canvas.yview()
        if before[0] <= 1e-4 and before[1] >= 1.0 - 1e-4:
            return False
        self.roster_canvas.yview_scroll(int(steps), "units")
        moved = self.roster_canvas.yview() != before
        if moved and not getattr(self, "_rendering_roster", False):
            self._render_roster_visible()
        return moved

    def _on_roster_mousewheel(self, event: tk.Event[Any]) -> None:
        delta = -1 if int(getattr(event, "delta", 0)) > 0 else 1
        self._scroll_roster_by_units(delta * 2)

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
        has_voice = self._has_active_language_voice_link(eid)
        y0 = idx * self.ROW_H
        y1 = y0 + self.ROW_H

        if not slot.get("visible", False):
            for k in ("bg", "div", "img", "name"):
                cv.itemconfigure(slot[k], state="normal")
            slot["visible"] = True
            slot["is_sel"] = None
            slot["bg_hex"] = ""

        width_changed = slot.get("w") != w
        if slot.get("row_idx") != idx or width_changed or slot.get("has_status") != has_status:
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
            or width_changed
            or force_all
        )
        if content_changed:
            slot["eid"] = eid
            slot["has_status"] = has_status
            slot["has_voice"] = has_voice
            text_avail = max(96, w - (116 if has_voice else 88))
            name_max = max(8, min(20, int(text_avail // 13)))
            status_max = max(10, min(26, int(text_avail // 10)))
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
            w = max(210, int(cv.winfo_width() or 300))
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
            self._persist_cfg_locked()
        if new_val:
            self.log("🕵️ [임시 통화 ON] 다음 통화부터는 대화 기록과 장기 기억을 저장/주입하지 않습니다 (테스트용).")
        else:
            self.log("🧠 [임시 통화 OFF] 다음 통화부터 대화 기록 저장 및 학생별 기억 유지가 정상 동작합니다.")

    def _toggle_idle_profile_card_view(self) -> None:
        self._idle_force_profile_card = not bool(self._idle_force_profile_card)
        self._refresh_momotalk_header_and_cards()

    def _is_rvc_process_running(self) -> bool:
        proc = getattr(self, "_rvc_proc", None)
        if proc is None:
            return False
        try:
            if proc.poll() is None:
                return True
        except Exception:
            pass
        self._rvc_proc = None
        return False

    def _build_tools_menu(self) -> tk.Menu:
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg="#FFFFFF",
            fg="#0F172A",
            activebackground="#FFE4EC",
            activeforeground="#BE123C",
            font=("Malgun Gothic", 9),
        )
        rvc_ready = is_rvc_runtime_available(ROOT_DIR)
        self._rvc_runtime_available = rvc_ready
        if self._is_colab_s2pro_enabled():
            menu.add_command(
                label="🛑 Colab S2-Pro GPU 원격 반납",
                command=self.request_colab_gpu_release_ui,
            )
            menu.add_separator()
        if rvc_ready:
            rvc_label = (
                "⚡ 실시간 RVC 모드 실행 중 (창 보기)"
                if self._is_rvc_process_running()
                else "⚡ 실시간 RVC 모드 (Gemini Live × RVC)"
            )
            menu.add_command(label=rvc_label, command=self.launch_rvc_live_mode)
            menu.add_separator()
        if _is_screen_vision_available(ROOT_DIR):
            menu.add_command(
                label="📊 학습 보드 열기",
                command=self.open_economics_study_board,
            )
            menu.add_command(
                label="📌 항상 위 미니 자막 바 토글",
                command=self._toggle_mini_overlay,
            )
            menu.add_separator()
        menu.add_command(
            label="🎙️ 스팀 음성 추출 · Fish 일괄 업로드",
            command=self.open_fish_batch_dialog,
        )
        menu.add_command(
            label="🔍 Fish Audio 인기 보이스 검색",
            command=self.open_voice_search_dialog,
        )
        menu.add_command(
            label="🧠 기억 노트 & 선생님 프로필",
            command=lambda: self.open_memory_note_dialog(
                initial_tab=0 if self._momotalk_char_activated else 1
            ),
        )
        menu.add_separator()
        menu.add_command(
            label="🔄 설정 기본값 복원 (Fish 키·AI 로그인 유지)",
            command=self.reset_settings_to_defaults_ui,
        )
        menu.add_command(
            label="🌐 브라우저 로그인 프로필 초기화",
            command=self.reset_browser_profile_ui,
        )
        menu.add_command(
            label="📋 상태 로그 보기",
            command=self.open_status_log_window,
        )
        return menu

    def _snapshot_cfg_locked(self) -> dict[str, Any]:
        with self._cfg_persist_lock:
            return deepcopy(self.cfg)

    def _persist_batch_voice_link_locked(self, cid: str, lang_up: str, mid: str) -> None:
        bucket = "saved_voices_kr" if str(lang_up or "").upper() == "KR" else "saved_voices"
        with self._cfg_persist_lock:
            self.cfg.setdefault(bucket, {})[cid] = mid
            cur_rlang = "ko" if str(self.cfg.get("reply_language", "ja") or "ja").strip().lower() == "ko" else "ja"
            if cid == self.current_char_id and ((str(lang_up or "").upper() == "KR") == (cur_rlang == "ko")):
                self.cfg["fish_reference_id"] = mid
            save_bridge_config(self.cfg)

    def _persist_batch_voice_links_bulk_locked(self, links: list[tuple[str, str, str]]) -> str | None:
        """Atomically persist multiple synced `(char_id, lang, model_id)` links in one disk write."""
        if not links:
            return None
        updated_active_mid: str | None = None
        with self._cfg_persist_lock:
            cur_rlang = "ko" if str(self.cfg.get("reply_language", "ja") or "ja").strip().lower() == "ko" else "ja"
            for cid, lang_up, mid in links:
                if not cid or not mid:
                    continue
                is_kr = str(lang_up or "").upper() == "KR"
                bucket = "saved_voices_kr" if is_kr else "saved_voices"
                self.cfg.setdefault(bucket, {})[cid] = mid
                if cid == self.current_char_id and (is_kr == (cur_rlang == "ko")):
                    self.cfg["fish_reference_id"] = mid
                    updated_active_mid = mid
            save_bridge_config(self.cfg)
        return updated_active_mid

    def _auto_sync_fish_account_models_async(
        self,
        *,
        http_session: Any = None,
        timeout: float = 9.0,
        on_done: Any = None,
    ) -> threading.Thread | None:
        """
        Silently sync newly uploaded user-owned Fish Audio models (`GET /model?self=true`, read-only,
        zero credits spent) in a non-blocking background thread on app startup so manually uploaded
        web voices auto-link into `saved_voices` / `saved_voices_kr` without any user action.
        """
        if getattr(self, "_closing", False):
            return None
        api_key = str(self.cfg.get("fish_api_key") or "").strip()
        if not api_key:
            return None

        sync_lock = getattr(self, "_fish_auto_sync_lock", None)
        if sync_lock is None:
            sync_lock = threading.Lock()
            self._fish_auto_sync_lock = sync_lock
        with sync_lock:
            if getattr(self, "_fish_auto_sync_inflight", False):
                return None
            self._fish_auto_sync_inflight = True

        eff_timeout = max(2.0, min(15.0, float(timeout)))

        def _worker() -> None:
            res: dict[str, Any] = {"ok": False, "synced": 0}
            owns_sess = http_session is None
            sess: Any = http_session
            try:
                if getattr(self, "_closing", False):
                    return
                from src.controller.fish_batch_builder import sync_existing_fish_models

                if owns_sess:
                    sess = requests.Session()
                    sess.verify = True
                    sess.max_redirects = 0

                cfg_snap = self._snapshot_cfg_locked()
                pending_links: list[tuple[str, str, str]] = []
                res = sync_existing_fish_models(
                    api_key=api_key,
                    cfg=cfg_snap,
                    characters=self.characters,
                    overwrite=False,
                    prefer_new_self_owned=True,
                    http_session=sess,
                    log_cb=None,
                    max_retries=0,
                    timeout=eff_timeout,
                    persist_voice_link_cb=lambda c, l, m: pending_links.append((c, l, m)),
                )
                if getattr(self, "_closing", False):
                    return
                if res.get("ok") and pending_links:
                    active_mid = self._persist_batch_voice_links_bulk_locked(pending_links)
                    synced_cnt = len(pending_links)

                    def _apply_ui() -> None:
                        if getattr(self, "_closing", False):
                            return
                        if active_mid and hasattr(self, "ref_id_var"):
                            try:
                                self.ref_id_var.set(active_mid)
                            except Exception:
                                pass
                        try:
                            if getattr(self, "_momotalk_sort_mode", "default") == "heart" or bool(getattr(self, "_roster_voice_only", False)):
                                self._refresh_character_filter_list(keep_current=True)
                            else:
                                self._render_roster_visible(force_all=True)
                            self._refresh_momotalk_header_and_cards()
                        except Exception:
                            pass
                        try:
                            self.log(f"🔄 [자동 동기화] 내 Fish Audio 계정의 새 보이스 {synced_cnt}개를 자동으로 연결했습니다!")
                        except Exception:
                            pass

                    try:
                        self._schedule_ui(_apply_ui)
                    except Exception:
                        pass
            except Exception:
                # Silently ignore offline/auth/timeout errors on startup — zero popups
                pass
            finally:
                if owns_sess and sess is not None:
                    try:
                        sess.close()
                    except Exception:
                        pass
                with sync_lock:
                    self._fish_auto_sync_inflight = False
                if callable(on_done):
                    try:
                        on_done(res)
                    except Exception:
                        pass

        th = threading.Thread(target=_worker, daemon=True, name="auto-sync-fish-models")
        self._track_startup_bg_thread(th)
        th.start()
        return th

    def _open_tools_menu(self) -> None:
        menu = self._build_tools_menu()
        self._tools_menu = menu
        try:
            bx = self.btn_hdr_tools.winfo_rootx()
            by = self.btn_hdr_tools.winfo_rooty() + self.btn_hdr_tools.winfo_height() + 2
            menu.tk_popup(bx, by)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def open_fish_batch_dialog(self) -> None:
        from src.controller.fish_batch_dialog import FishBatchDialog

        def _on_linked(cid: str, lang_up: str, mid: str) -> None:
            def _sync_ui() -> None:
                if cid == self.current_char_id and str(lang_up or "").upper() == self._voice_lang_code():
                    if hasattr(self, "ref_id_var"):
                        try:
                            self.ref_id_var.set(mid)
                        except Exception:
                            pass
                if getattr(self, "_momotalk_sort_mode", "default") == "heart" or bool(getattr(self, "_roster_voice_only", False)):
                    self._refresh_character_filter_list(keep_current=True)
                else:
                    self._render_roster_visible(force_all=True)
                self._refresh_momotalk_header_and_cards()

            try:
                self._schedule_ui(_sync_ui)
            except Exception:
                pass

        def _on_done() -> None:
            try:
                self._refresh_momotalk_header_and_cards()
                self._render_roster_visible(force_all=True)
            except Exception:
                pass

        FishBatchDialog(
            self.root,
            cfg=self._snapshot_cfg_locked(),
            characters=self.characters,
            api_key_getter=lambda: str(self.cfg.get("fish_api_key") or "").strip(),
            on_model_linked=_on_linked,
            on_finished=_on_done,
            persist_voice_link_cb=self._persist_batch_voice_link_locked,
        )

    def launch_rvc_live_mode(self) -> None:
        if self._is_rvc_process_running():
            focus_existing_rvc_window()
            messagebox.showinfo(
                "실시간 RVC 실행 중",
                "이미 Gemini Live × 로컬 RVC 창이 실행 중입니다.\n열려 있는 RVC 창을 확인해 주세요.",
                parent=self.root,
            )
            return

        status = get_rvc_status(ROOT_DIR)
        if not status.get("available"):
            messagebox.showinfo(
                "로컬 RVC 미설치",
                str(status.get("reason") or "로컬 RVC 런타임을 찾을 수 없습니다."),
                parent=self.root,
            )
            return

        if self.companion_running or self.running or getattr(self, "_call_ui_state", "idle") != "idle":
            self.log("⚡ [RVC 전환] 진행 중인 모모톡 통화를 먼저 안전 종료합니다 (마이크·스피커·GPU 확보)...")
            self.stop_momotalk_call()

        char_id = str(self.current_char_id or "Aru").strip()
        lang = self._voice_lang_code()
        try:
            proc = launch_rvc_live_app(ROOT_DIR, char_id=char_id, lang=lang)
            self._rvc_proc = proc
            self.log(f"⚡ [실시간 RVC 실행] {char_id} ({lang}) — PID {proc.pid}")
        except Exception as exc:
            safe_err = _redact_secrets(str(exc))
            self.log(f"❌ [실시간 RVC 실행 실패] {safe_err}")
            messagebox.showerror(
                "실시간 RVC 실행 실패",
                f"Gemini Live × 로컬 RVC 실행 중 오류가 발생했습니다:\n{safe_err}",
                parent=self.root,
            )

    # ------------------------------------------------------------------ Local-Only Screen Vision & Study Board
    def _init_screen_vision(self) -> None:
        try:
            from src.screen_vision import (
                EconomicsStudyBoard,
                GlobalHotkeyManager,
                MiniSubtitleOverlay,
                ScreenWatcher,
                load_screen_vision_config,
            )

            self._sv_cfg = load_screen_vision_config()
            self._screen_watcher = ScreenWatcher(
                self._sv_cfg,
                is_busy_cb=self._is_screen_vision_busy,
                on_proactive_trigger=self._on_screen_proactive_trigger,
                on_status_change=self._on_screen_watcher_status_changed,
                on_log=self.log,
            )
            self._study_board = EconomicsStudyBoard(on_log=self.log)
            self._mini_overlay = MiniSubtitleOverlay(
                self.root,
                on_toggle_pause=self._on_screen_hotkey_toggle_share,
                on_instant_look=self._on_screen_instant_look,
                on_open_board=self.open_economics_study_board,
                on_close=self._on_mini_overlay_closed,
                on_position_changed=self._on_mini_overlay_position_changed,
                alpha=float(self._sv_cfg.get("mini_overlay_alpha", 0.90)),
                init_x=self._sv_cfg.get("mini_overlay_x"),
                init_y=self._sv_cfg.get("mini_overlay_y"),
            )
            self._global_hotkeys = GlobalHotkeyManager(on_log=self.log)
            self._sync_screen_vision_hotkeys()
        except Exception as exc:
            self._sv_available = False
            self._screen_watcher = None
            self._study_board = None
            self._mini_overlay = None
            self._global_hotkeys = None
            try:
                self.log(f"⚠️ [화면 공유 초기화 생략] {exc}")
            except Exception:
                pass

    def _build_screen_vision_bar(self, parent: tk.Misc) -> None:
        self._sv_target_var = tk.StringVar(value="🖥️ 모니터 1")
        self._sv_status_var = tk.StringVar(value="대기 중")

        bar = tk.Frame(parent, bg="#1E293B", padx=12, pady=5)
        bar.pack(fill="x")
        self._momo_screen_bar = bar

        # Row 1: Toggle + Mode + Expanding Target Picker + Refresh (fits comfortably even at 820px total width)
        row1 = tk.Frame(bar, bg="#1E293B")
        row1.pack(fill="x")

        self.btn_sv_toggle = tk.Button(
            row1,
            text="🖥️ 화면 공유 OFF",
            command=self._on_sv_toggle_clicked,
            bg="#334155",
            fg="#E2E8F0",
            activebackground="#475569",
            activeforeground="#FFFFFF",
            disabledforeground="#94A3B8",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=9,
            pady=3,
            cursor="hand2",
        )
        self.btn_sv_toggle.pack(side="left", padx=(0, 5))
        _attach_hover_tooltip(self.btn_sv_toggle, self._get_sv_toggle_tooltip)

        self.btn_sv_mode_chat = tk.Button(
            row1,
            text="🎮 대화",
            command=lambda: self._set_screen_vision_mode("active_chat"),
            bg="#2563EB",
            fg="#FFFFFF",
            activebackground="#1D4ED8",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=3,
            cursor="hand2",
        )
        self.btn_sv_mode_chat.pack(side="left", padx=(0, 2))
        _attach_hover_tooltip(self.btn_sv_mode_chat, "🎮 적극적 대화 모드 — 화면 변화에 맞춰 즉각 티키타카 리액션")

        self.btn_sv_mode_study = tk.Button(
            row1,
            text="📚 공부",
            command=lambda: self._set_screen_vision_mode("econ_study"),
            bg="#334155",
            fg="#CBD5E1",
            activebackground="#0F766E",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=3,
            cursor="hand2",
        )
        self.btn_sv_mode_study.pack(side="left", padx=(0, 6))
        _attach_hover_tooltip(self.btn_sv_mode_study, "📚 공부 모드 — 수식·그래프·학습 보드 자동 연동")

        self.btn_sv_refresh = tk.Button(
            row1,
            text="🔄",
            command=lambda: self._refresh_screen_vision_targets(silent=False),
            bg="#334155",
            fg="#E2E8F0",
            activebackground="#475569",
            activeforeground="#FFFFFF",
            font=("Segoe UI Emoji", 8, "bold"),
            relief="flat",
            padx=6,
            pady=2,
            cursor="hand2",
        )
        self.btn_sv_refresh.pack(side="right", padx=(4, 0))
        _attach_hover_tooltip(self.btn_sv_refresh, "모니터 및 캡처 가능한 창 목록 새로고침")

        self.cmb_sv_target = ttk.Combobox(
            row1,
            textvariable=self._sv_target_var,
            values=[],
            state="readonly",
            width=18,
        )
        self.cmb_sv_target.pack(side="left", fill="x", expand=True)
        self.cmb_sv_target.bind("<<ComboboxSelected>>", self._on_sv_target_selected)

        # Row 2: Quick Action Buttons (packed right FIRST so they NEVER clip) + Status label on left
        row2 = tk.Frame(bar, bg="#1E293B")
        row2.pack(fill="x", pady=(4, 0))

        self.btn_sv_overlay = tk.Button(
            row2,
            text="📌 미니 바",
            command=self._toggle_mini_overlay,
            bg="#334155",
            fg="#E2E8F0",
            activebackground="#475569",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=2,
            cursor="hand2",
        )
        self.btn_sv_overlay.pack(side="right", padx=(4, 0))
        _attach_hover_tooltip(self.btn_sv_overlay, "항상 위 미니 자막 바 표시/숨김 (Ctrl+Shift+B)")

        self.btn_sv_board = tk.Button(
            row2,
            text="📊 학습 보드",
            command=self.open_economics_study_board,
            bg="#334155",
            fg="#E2E8F0",
            activebackground="#475569",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=2,
            cursor="hand2",
        )
        self.btn_sv_board.pack(side="right", padx=(4, 0))
        _attach_hover_tooltip(self.btn_sv_board, "수식·그래프·학습 보드 열기")

        self.btn_sv_instant_look = tk.Button(
            row2,
            text="👀 지금 봐!",
            command=self._on_screen_instant_look,
            bg="#0284C7",
            fg="#FFFFFF",
            activebackground="#0369A1",
            activeforeground="#FFFFFF",
            disabledforeground="#94A3B8",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        )
        self.btn_sv_instant_look.pack(side="right", padx=(6, 0))
        _attach_hover_tooltip(self.btn_sv_instant_look, self._get_sv_instant_look_tooltip)

        self.lbl_sv_status = tk.Label(
            row2,
            textvariable=self._sv_status_var,
            bg="#1E293B",
            fg="#94A3B8",
            font=("Malgun Gothic", 8),
            anchor="w",
        )
        self.lbl_sv_status.pack(side="left", fill="x", expand=True)

        self._refresh_screen_vision_bar_ui()

    def _get_sv_toggle_tooltip(self) -> str:
        if self._get_ai_provider() != "gemini":
            return "화면 공유는 제미나이 모드 전용입니다"
        if self._screen_watcher is not None and self._screen_watcher.enabled:
            return "실시간 화면 공유 끄기 (일시정지/전환 단축키: Ctrl+Shift+S)"
        return "실시간 화면 공유 켜기 — 100% RAM 캡처 (민감 창 자동 보호 · Ctrl+Shift+S)"

    def _get_sv_instant_look_tooltip(self) -> str:
        if self._get_ai_provider() != "gemini":
            return "화면 공유는 제미나이 모드 전용입니다"
        return "지금 화면을 즉시 캡처해 캐릭터에게 반응/해설 요청 (Ctrl+Shift+V)"

    def _refresh_screen_vision_targets(self, *, silent: bool = True) -> None:
        if not self._sv_available:
            return
        try:
            from src.screen_vision import list_capturable_windows, list_monitors

            monitors = list_monitors()
            windows = list_capturable_windows(exclude_pids={os.getpid()})
        except Exception as exc:
            if not silent:
                self.log(f"⚠️ [화면 공유] 모니터/창 목록 조회 실패: {exc}")
            return

        targets: list[tuple[str, dict[str, Any]]] = []
        seen_labels: set[str] = set()
        for mon in monitors:
            lbl = mon.label
            if lbl in seen_labels:
                lbl = f"{lbl} #{mon.index + 1}"
            seen_labels.add(lbl)
            targets.append(
                (
                    lbl,
                    {
                        "target_kind": "monitor",
                        "target_monitor_index": int(mon.index),
                        "target_window_title": "",
                        "target_window_process": "",
                    },
                )
            )
        for win in windows[:40]:
            lbl = win.label
            if lbl in seen_labels:
                lbl = f"{lbl} ({win.hwnd})"
            seen_labels.add(lbl)
            targets.append(
                (
                    lbl,
                    {
                        "target_kind": "window",
                        "target_monitor_index": int(self._sv_cfg.get("target_monitor_index", 0) or 0),
                        "target_window_title": win.title,
                        "target_window_process": win.process_name,
                    },
                )
            )

        if not targets:
            targets.append(
                (
                    "🖥️ 모니터 1",
                    {
                        "target_kind": "monitor",
                        "target_monitor_index": 0,
                        "target_window_title": "",
                        "target_window_process": "",
                    },
                )
            )

        self._sv_targets = targets
        labels = [lbl for lbl, _ in targets]
        selected_lbl = labels[0]
        cur_kind = str(self._sv_cfg.get("target_kind") or "monitor")
        if cur_kind == "window":
            want_title = str(self._sv_cfg.get("target_window_title") or "").casefold()
            want_proc = str(self._sv_cfg.get("target_window_process") or "").casefold()
            for lbl, desc in targets:
                if desc["target_kind"] != "window":
                    continue
                t_low = str(desc["target_window_title"]).casefold()
                p_low = str(desc["target_window_process"]).casefold()
                if (want_title and want_title == t_low) or (want_proc and want_proc == p_low and (not want_title or want_title in t_low)):
                    selected_lbl = lbl
                    break
        else:
            want_idx = int(self._sv_cfg.get("target_monitor_index") or 0)
            for lbl, desc in targets:
                if desc["target_kind"] == "monitor" and int(desc["target_monitor_index"]) == want_idx:
                    selected_lbl = lbl
                    break

        if hasattr(self, "cmb_sv_target") and self.cmb_sv_target is not None:
            try:
                self.cmb_sv_target.configure(values=labels)
            except Exception:
                pass
        if self._sv_target_var is not None:
            self._sv_target_var.set(selected_lbl)
        if not silent:
            self.log(f"🔄 [화면 공유] 캡처 대상 목록을 새로고침했습니다 ({len(monitors)} 모니터 · {len(windows)} 창).")

    def _on_sv_target_selected(self, _event: Any = None) -> None:
        if not self._sv_available or self._sv_target_var is None:
            return
        chosen = self._sv_target_var.get()
        desc = next((d for lbl, d in self._sv_targets if lbl == chosen), None)
        if desc is None:
            return
        self._sv_cfg.update(desc)
        if self._screen_watcher is not None:
            self._screen_watcher.update_config(self._sv_cfg)
        try:
            from src.screen_vision.config import save_screen_vision_config

            save_screen_vision_config(self._sv_cfg)
        except Exception:
            pass
        self._refresh_screen_vision_bar_ui()
        self.log(f"🖥️ [화면 공유 대상 변경] {chosen}")

    def _on_sv_toggle_clicked(self) -> None:
        if not self._sv_available or self._screen_watcher is None:
            return
        if self._get_ai_provider() != "gemini":
            self.log("✨ [화면 공유] 화면 공유는 제미나이 모드 전용입니다.")
            self._refresh_screen_vision_bar_ui()
            return
        want = not self._screen_watcher.enabled
        self._set_screen_share_enabled(want, silent=False)

    def _set_screen_share_enabled(self, enabled: bool, *, silent: bool = False) -> None:
        if not self._sv_available or self._screen_watcher is None:
            return
        want = bool(enabled)
        if want and self._get_ai_provider() != "gemini":
            if not silent:
                self.log("✨ [화면 공유] 화면 공유는 제미나이 모드 전용입니다.")
            want = False
        was_enabled = bool(self._screen_watcher.enabled)
        if want:
            if not self._sv_targets:
                self._refresh_screen_vision_targets(silent=True)
            self._screen_watcher.update_config(self._sv_cfg)
            self._screen_watcher.set_enabled(True)
            if not silent and not was_enabled:
                mode_lbl = "📚 공부" if self._screen_watcher.mode == "econ_study" else "🎮 적극적 대화"
                self.log(f"🟢 [화면 공유 ON] {mode_lbl} 모드로 실시간 화면 공유를 시작했습니다 (100% RAM 캡처).")
        else:
            self._screen_watcher.set_enabled(False)
            if not silent and was_enabled:
                self.log("🖥️ [화면 공유 OFF] 실시간 화면 공유를 중지했습니다.")
        self._refresh_screen_vision_bar_ui()

    def _set_screen_vision_mode(self, mode: str, *, persist: bool = True) -> None:
        if not self._sv_available:
            return
        norm = "econ_study" if str(mode or "").strip().lower() == "econ_study" else "active_chat"
        self._sv_cfg["mode"] = norm
        if self._screen_watcher is not None:
            self._screen_watcher.set_mode(norm)
        if persist:
            try:
                from src.screen_vision.config import save_screen_vision_config

                save_screen_vision_config(self._sv_cfg)
            except Exception:
                pass
        self._refresh_screen_vision_bar_ui()
        mode_lbl = "📚 공부 모드" if norm == "econ_study" else "🎮 적극적 대화 모드"
        self.log(f"🎯 [화면 공유 모드] {mode_lbl}로 전환했습니다.")

    def _on_screen_hotkey_toggle_share(self) -> None:
        if not self._sv_available or self._screen_watcher is None or self._closing:
            return
        if self._get_ai_provider() != "gemini":
            self.log("✨ [화면 공유 단축키] 화면 공유는 제미나이 모드 전용입니다.")
            return
        if not self._screen_watcher.enabled:
            if not self.companion_running or self._call_ui_state != "connected":
                self.log("🖥️ [화면 공유 단축키] 통화가 연결된 상태에서 화면 공유를 켤 수 있습니다.")
                return
            self._set_screen_share_enabled(True, silent=False)
            return
        paused_now = self._screen_watcher.toggle_pause()
        self._refresh_screen_vision_bar_ui()
        if paused_now:
            self.log("⏸️ [화면 공유 일시정지] 단축키(Ctrl+Shift+S)로 화면 공유를 일시정지했습니다.")
        else:
            self.log("▶️ [화면 공유 재개] 화면 공유 감시를 재개했습니다.")

    def _on_screen_instant_look(self) -> None:
        if not self._sv_available or self._screen_watcher is None or self._closing:
            return
        if self._get_ai_provider() != "gemini":
            self.log("✨ [지금 봐!] 화면 공유는 제미나이 모드 전용입니다.")
            return
        if not self.companion_running or not self._companion_session_ready or self._call_ui_state != "connected":
            self.log("👀 [지금 봐!] 먼저 Gemini 통화를 연결한 뒤 사용해 주세요.")
            return
        if self._sv_instant_look_busy:
            return
        self._sv_instant_look_busy = True

        def _worker() -> None:
            try:
                if self._closing or self._screen_watcher is None:
                    return
                frame = self._screen_watcher.capture_for_instant_look()
                if frame is None:
                    self.log("⚠️ [지금 봐!] 현재 화면을 캡처하지 못했습니다. 대상 창/모니터를 확인해 주세요.")
                    return
                if frame.blocked_by_privacy:
                    self.log(
                        f"🔒 [프라이버시 보호] 민감한 창({frame.privacy_reason})이 감지되어 '지금 봐!' 전송을 차단했습니다."
                    )
                    return
                if (
                    self._closing
                    or (not self.companion_running)
                    or (not self._companion_session_ready)
                    or self._get_ai_provider() != "gemini"
                ):
                    return
                mode = str(self._screen_watcher.mode)
                prompt_hint = (
                    "지금 화면에 보이는 경제학 개념·문제·그래프를 함께 보고 핵심을 이해하기 쉽게 설명해 줘."
                    if mode == "econ_study"
                    else "지금 내가 보고 있는 화면을 같이 보고 자연스럽게 반응해 줘!"
                )
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
                    self._abort_active_browser_prompt()

                with self._turn_interrupt_lock:
                    confirmed = self.companion_sm.on_user_speech_confirmed(now)
                    if confirmed:
                        turn_seq = self.companion_sm.current_turn_seq
                        self._drain_companion_turn_queue()
                        enqueued = self._enqueue_companion_turn(
                            "instant_look",
                            {"frame": frame, "prompt_hint": prompt_hint},
                            revert_turn_seq=turn_seq,
                        )
                    else:
                        enqueued = False
                if confirmed and enqueued:
                    self.log(f"👀 [지금 봐!] 현재 화면({frame.target_label})을 캡처해 캐릭터에게 전달합니다...")
            except Exception as exc:
                self.log(f"⚠️ [지금 봐!] 처리 중 오류: {exc}")
            finally:
                self._sv_instant_look_busy = False

        threading.Thread(target=_worker, daemon=True, name="screen-vision-instant-look").start()

    def open_economics_study_board(self) -> None:
        if not self._sv_available or self._study_board is None:
            return
        try:
            self._study_board.open()
        except Exception as exc:
            self.log(f"⚠️ [학습 보드] 열기 실패: {exc}")

    def _toggle_mini_overlay(self) -> None:
        if not self._sv_available or self._mini_overlay is None or self._closing:
            return
        try:
            if self._mini_overlay.is_visible():
                self._mini_overlay.hide()
                self._sv_cfg["mini_overlay_enabled"] = False
                self.log("📌 [미니 자막 바] 숨김")
            else:
                self._mini_overlay.show()
                self._sv_cfg["mini_overlay_enabled"] = True
                self._sync_mini_overlay_status()
                if self._subtitle_entries:
                    self._mirror_subtitle_to_mini_overlay(self._subtitle_entries[-1])
                self.log("📌 [미니 자막 바] 항상 위 표시 (화면 캡처에서 자동 제외됨)")
            try:
                from src.screen_vision.config import save_screen_vision_config

                save_screen_vision_config(self._sv_cfg)
            except Exception:
                pass
            self._refresh_screen_vision_bar_ui()
        except Exception as exc:
            self.log(f"⚠️ [미니 자막 바] 토글 오류: {exc}")

    def _on_mini_overlay_closed(self) -> None:
        if not self._sv_available:
            return
        self._sv_cfg["mini_overlay_enabled"] = False
        try:
            from src.screen_vision.config import save_screen_vision_config

            save_screen_vision_config(self._sv_cfg)
        except Exception:
            pass
        self._refresh_screen_vision_bar_ui()

    def _on_mini_overlay_position_changed(self, x: int, y: int) -> None:
        if not self._sv_available:
            return
        self._sv_cfg["mini_overlay_x"] = int(x)
        self._sv_cfg["mini_overlay_y"] = int(y)
        try:
            from src.screen_vision.config import save_screen_vision_config

            save_screen_vision_config(self._sv_cfg)
        except Exception:
            pass

    def _sync_screen_vision_hotkeys(self) -> None:
        if not self._sv_available or self._global_hotkeys is None or self._closing:
            return
        try:
            if bool(self._sv_cfg.get("hotkeys_enabled", True)):
                self._global_hotkeys.start(
                    {
                        "toggle_share": lambda: self._schedule_ui(self._on_screen_hotkey_toggle_share),
                        "instant_look": lambda: self._schedule_ui(self._on_screen_instant_look),
                        "toggle_overlay": lambda: self._schedule_ui(self._toggle_mini_overlay),
                    }
                )
            else:
                self._global_hotkeys.stop()
        except Exception as exc:
            self.log(f"⚠️ [전역 단축키] 동기화 오류: {exc}")

    def apply_screen_vision_config(self, new_cfg: dict[str, Any]) -> None:
        if not self._sv_available:
            return
        try:
            from src.screen_vision.config import sanitize_screen_vision_config

            self._sv_cfg = sanitize_screen_vision_config(new_cfg)
            if self._screen_watcher is not None:
                self._screen_watcher.update_config(self._sv_cfg)
            if self._mini_overlay is not None:
                self._mini_overlay.set_alpha(float(self._sv_cfg.get("mini_overlay_alpha", 0.90)))
            self._sync_screen_vision_hotkeys()
            self._refresh_screen_vision_bar_ui()
            self.log("🖥️ [화면 공유 설정 적용] screen_vision_config.json 저장 및 실시간 반영 완료.")
        except Exception as exc:
            self.log(f"⚠️ [화면 공유 설정 반영 오류] {exc}")

    def _on_ai_provider_changed_for_screen_vision(self, new_provider: str) -> None:
        if not self._sv_available:
            return
        norm_p = normalize_ai_provider(new_provider)
        if norm_p != "gemini" and self._screen_watcher is not None and self._screen_watcher.enabled:
            self._set_screen_share_enabled(False, silent=True)
            short = AI_PROVIDER_SHORT_NAMES.get(norm_p, norm_p)
            self.log(f"🖥️ [화면 공유 자동 종료] 화면 공유는 제미나이 모드 전용입니다 ({short} 전환으로 꺼짐).")
        self._refresh_screen_vision_bar_ui()

    def _is_screen_vision_busy(self) -> bool:
        if self._closing or (not self.companion_running) or (not self._companion_session_ready):
            return True
        if self._call_ui_state != "connected":
            return True
        if self._get_ai_provider() != "gemini":
            return True
        if not self._companion_turn_queue.empty():
            return True
        if time.monotonic() < (self._active_audio_playing_until + 0.35):
            return True
        try:
            with self.companion_sm._lock:
                if self.companion_sm.state != CompanionState.IDLE:
                    return True
        except Exception:
            return True
        return False

    def _on_screen_proactive_trigger(self, trigger_kind: str, frame: Any) -> None:
        if self._closing or (not self._sv_available) or self._is_screen_vision_busy():
            return
        if frame is None or getattr(frame, "blocked_by_privacy", False):
            return
        now = time.monotonic()
        with self._turn_interrupt_lock:
            with self.companion_sm._lock:
                is_idle = self.companion_sm.state == CompanionState.IDLE
            if not is_idle:
                return
            triggered = self.companion_sm.trigger_manual_ping(now)
            if not triggered:
                return
            turn_seq = self.companion_sm.current_turn_seq
            hint = (
                "[STUDY_STUCK_CHECKIN]"
                if trigger_kind == "study_checkin"
                else "[SCREEN_CHANGE_REACT]"
            )
            enqueued = self._enqueue_companion_turn(
                trigger_kind,
                {"frame": frame, "prompt_hint": hint},
                revert_turn_seq=turn_seq,
            )
        if enqueued:
            if trigger_kind == "study_checkin":
                self.log("📚 [공부 체크인] 같은 화면 유지 감지 → 캐릭터가 가볍게 안부를 묻습니다...")
            else:
                self.log("👀 [화면 변화 감지] 새 장면 포착 → 캐릭터가 자연스럽게 반응합니다...")

    def _on_screen_watcher_status_changed(self, status: dict[str, Any]) -> None:
        if self._closing or not self._sv_available:
            return
        snap = dict(status) if isinstance(status, dict) else {}
        self._schedule_ui(lambda s=snap: self._refresh_screen_vision_bar_ui(status_override=s))

    def _sync_mini_overlay_status(self, status: dict[str, Any] | None = None) -> None:
        if self._mini_overlay is None:
            return
        st = status
        if st is None and self._screen_watcher is not None:
            try:
                st = self._screen_watcher.get_status()
            except Exception:
                st = {}
        st = st or {}
        try:
            self._mini_overlay.update_status(
                enabled=bool(st.get("enabled", False)),
                paused=bool(st.get("paused", False)),
                mode=str(st.get("mode") or self._sv_cfg.get("mode") or "active_chat"),
                target_label=str(st.get("target_label") or ""),
                privacy_blocked=bool(st.get("privacy_blocked", False)),
                privacy_reason=str(st.get("privacy_reason") or ""),
            )
        except Exception:
            pass

    def _refresh_screen_vision_bar_ui(self, status_override: dict[str, Any] | None = None) -> None:
        if not self._sv_available:
            return
        st = status_override
        if st is None and self._screen_watcher is not None:
            try:
                st = self._screen_watcher.get_status()
            except Exception:
                st = {}
        st = st or {}
        is_gemini = self._get_ai_provider() == "gemini"
        enabled = bool(st.get("enabled", False))
        paused = bool(st.get("paused", False))
        priv_blocked = bool(st.get("privacy_blocked", False))
        priv_reason = str(st.get("privacy_reason") or "").strip()
        mode = str(st.get("mode") or self._sv_cfg.get("mode") or "active_chat")
        tgt = str(st.get("target_label") or (self._sv_target_var.get() if self._sv_target_var is not None else "")).strip()

        if hasattr(self, "btn_sv_toggle") and self.btn_sv_toggle is not None:
            try:
                if not is_gemini:
                    self.btn_sv_toggle.configure(
                        text="🖥️ 화면 공유 OFF",
                        bg="#334155",
                        fg="#94A3B8",
                        state="disabled",
                    )
                elif enabled and priv_blocked:
                    self.btn_sv_toggle.configure(
                        text="🔒 자동 일시정지",
                        bg="#D97706",
                        fg="#FFFFFF",
                        state="normal",
                    )
                elif enabled and paused:
                    self.btn_sv_toggle.configure(
                        text="⏸️ 일시정지",
                        bg="#B45309",
                        fg="#FFFFFF",
                        state="normal",
                    )
                elif enabled:
                    self.btn_sv_toggle.configure(
                        text="🟢 화면 공유 ON",
                        bg="#15803D",
                        fg="#FFFFFF",
                        state="normal",
                    )
                else:
                    self.btn_sv_toggle.configure(
                        text="🖥️ 화면 공유 OFF",
                        bg="#334155",
                        fg="#E2E8F0",
                        state="normal",
                    )
            except Exception:
                pass

        if hasattr(self, "btn_sv_instant_look") and self.btn_sv_instant_look is not None:
            try:
                self.btn_sv_instant_look.configure(
                    state="normal" if is_gemini else "disabled",
                    bg="#0284C7" if is_gemini else "#334155",
                    fg="#FFFFFF" if is_gemini else "#94A3B8",
                )
            except Exception:
                pass

        if hasattr(self, "btn_sv_mode_chat") and hasattr(self, "btn_sv_mode_study"):
            try:
                is_study = mode == "econ_study"
                self.btn_sv_mode_chat.configure(
                    bg="#334155" if is_study else "#2563EB",
                    fg="#CBD5E1" if is_study else "#FFFFFF",
                )
                self.btn_sv_mode_study.configure(
                    bg="#0F766E" if is_study else "#334155",
                    fg="#FFFFFF" if is_study else "#CBD5E1",
                )
            except Exception:
                pass

        if hasattr(self, "btn_sv_overlay") and self.btn_sv_overlay is not None:
            try:
                ov_on = bool(self._mini_overlay is not None and self._mini_overlay.is_visible())
                self.btn_sv_overlay.configure(
                    bg="#7C3AED" if ov_on else "#334155",
                    fg="#FFFFFF" if ov_on else "#E2E8F0",
                )
            except Exception:
                pass

        if self._sv_status_var is not None:
            if not is_gemini:
                status_txt = "✨ Gemini 전용 (화면 공유는 제미나이 모드에서만 사용할 수 있습니다)"
                status_fg = "#FBBF24"
            elif enabled and priv_blocked:
                reason_part = f" ({priv_reason})" if priv_reason else ""
                status_txt = f"🔒 민감 창 보호 중{reason_part} — 일반 창으로 전환하면 자동 재개됩니다"
                status_fg = "#FDE68A"
            elif enabled and paused:
                status_txt = f"⏸️ 일시정지됨 ({tgt}) · Ctrl+Shift+S로 재개"
                status_fg = "#FDE68A"
            elif enabled:
                m_lbl = "📚 공부" if mode == "econ_study" else "🎮 적극적 대화"
                status_txt = f"감시 중 ({tgt} · {m_lbl})"
                status_fg = "#86EFAC"
            else:
                status_txt = "대기 중 (화면 공유 꺼짐 · 단축키 Ctrl+Shift+S / 지금 봐! Ctrl+Shift+V)"
                status_fg = "#94A3B8"
            self._sv_status_var.set(status_txt)
            if hasattr(self, "lbl_sv_status") and self.lbl_sv_status is not None:
                try:
                    self.lbl_sv_status.configure(fg=status_fg)
                except Exception:
                    pass

        self._sync_mini_overlay_status(st)

    def _on_study_visual_received(self, spec: dict[str, Any], *, turn_char: dict[str, Any] | None = None) -> None:
        if not self._sv_available or self._study_board is None or not isinstance(spec, dict):
            return
        try:
            ch = turn_char if isinstance(turn_char, dict) else (self.current_char if isinstance(self.current_char, dict) else {})
            char_name = str(ch.get("name_kr") or self.current_char_id or "").strip()
            auto_open = bool(self._sv_cfg.get("study_board_auto_open", True))
            self._study_board.push_visual(spec, character_name=char_name, auto_open=auto_open)
            self.log("📊 [학습 보드] 새 그래프/수식 카드 업데이트")
        except Exception as exc:
            self.log(f"⚠️ [학습 보드] 카드 업데이트 오류: {exc}")

    def _mirror_subtitle_to_mini_overlay(self, entry: SubtitleEntry) -> None:
        if self._mini_overlay is None or entry is None:
            return
        try:
            mode = str(self.subtitle_mode_var.get() or "jp_kr")
            if mode not in SUBTITLE_MODES:
                mode = "jp_kr"
            speaker = str(entry.char_name_kr or entry.char_name_jp or entry.char_id or "")
            self._mini_overlay.update_subtitle(
                speaker=speaker,
                jp_text=str(entry.jp_text or ""),
                kr_text=str(entry.kr_text or ""),
                subtitle_mode=mode,
            )
        except Exception:
            pass

    def _cleanup_screen_vision(self) -> None:
        if getattr(self, "_sv_cleaned_up", False):
            return
        self._sv_cleaned_up = True
        hk = getattr(self, "_global_hotkeys", None)
        if hk is not None:
            try:
                hk.stop()
            except Exception:
                pass
        watcher = getattr(self, "_screen_watcher", None)
        if watcher is not None:
            try:
                watcher.close()
            except Exception:
                pass
        board = getattr(self, "_study_board", None)
        if board is not None:
            try:
                board.close()
            except Exception:
                pass
        overlay = getattr(self, "_mini_overlay", None)
        if overlay is not None:
            try:
                overlay.destroy()
            except Exception:
                pass

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
            s_time_str = format_timestamp_safe(
                sess.get("started_at") or sess.get("ended_at"),
                "%Y-%m-%d %H:%M",
                "이전 통화",
            )
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
        rlang = self._get_reply_language()
        cur_vcode = (
            "custom"
            if bool(self.cfg.get("use_custom_persona", False))
            else PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")
        )
        is_eph = bool(self.temporary_call_var.get())
        if is_eph:
            with self._active_memory_session_lock:
                self._active_memory_session = {
                    "base_cid": base_cid,
                    "track": track,
                    "persona_variant": cur_vcode,
                    "reply_language": rlang,
                    "session_id": "",
                    "ephemeral": True,
                }
            self._active_call_memory_context = ""
            self.log("🕵️ [임시 통화] 기억 주입 및 대화 기록 저장을 건너뜁니다.")
            return

        try:
            mem_ctx, has_past = _call_build_memory_context_block(
                self.memory_mgr,
                base_cid=base_cid,
                track=track,
                char_data=None if base_cid == "__custom__" else self.current_char,
                current_variant=cur_vcode,
                reply_lang=rlang,
            )
            self._active_call_memory_context = mem_ctx
            if mem_ctx:
                counts = self.memory_mgr.get_student_memory_counts(base_cid, track)
                reset_note = " · 직전 대사 톤 리셋" if "[페르소나 전환 안내]" in mem_ctx else ""
                self.log(
                    f"🧠 [기억 연동] 확정 기억 {counts['confirmed']}개 · 화제 {counts['threads']}개 · "
                    f"{'이전 대화 맥락' if has_past else '공통 선생님 프로필'}을 반영합니다{reset_note}."
                )

            eid = getattr(self, "current_momotalk_entry_id", self.current_char_id)
            ent = self.momotalk_entry_map.get(eid) or {}
            costume_kr = str(ent.get("costume_label_kr") or "").strip() if ent.get("is_costume_variant") else ""
            sess_id = self.memory_mgr.begin_session(
                base_cid=base_cid,
                track=track,
                entry_id=base_cid if ent.get("is_costume_variant") else eid,
                costume_label_kr=costume_kr,
                reply_language=rlang,
                persona_variant=cur_vcode,
            )
            with self._active_memory_session_lock:
                self._active_memory_session = {
                    "base_cid": base_cid,
                    "track": track,
                    "persona_variant": cur_vcode,
                    "reply_language": rlang,
                    "session_id": sess_id,
                    "ephemeral": False,
                }
        except Exception as exc:
            safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
            self.log(f"⚠️ [기억 시작 경고] 기억 저장소 접근 오류({safe_exc}) → 이번 통화는 임시 통화(비저장)로 안전하게 진행합니다.")
            with self._active_memory_session_lock:
                self._active_memory_session = {
                    "base_cid": base_cid,
                    "track": track,
                    "persona_variant": cur_vcode,
                    "reply_language": rlang,
                    "session_id": "",
                    "ephemeral": True,
                }
            self._active_call_memory_context = ""

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
                # Preserve call-end / follow-up summary requests arriving while a checkpoint is in-flight
                self._memory_summary_pending_rescan.add(key)
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
                provider = self._get_ai_provider()
                backend = str(self.cfg.get("gemini_backend", "browser"))
                # Claude & ChatGPT Web enforce single concurrent stream per account:
                # defer mid-call checkpoint summaries to call end instead of opening a 2nd CDP tab mid-call.
                if is_checkpoint and self.companion_running and backend == "browser" and provider in ("claude", "chatgpt"):
                    short = AI_PROVIDER_SHORT_NAMES[provider]
                    self.log(f"🧠 [기억 정제 연기] {short} 웹 단일 스트림 보호를 위해 중간 체크포인트 요약을 통화 종료 시점으로 연기합니다.")
                    continue
                active_key = self._get_active_api_key(provider)
                active_api_model = self._get_active_api_model(provider)
                b_model = self._get_active_browser_model(provider)
                kind_lbl = "중간 체크포인트" if is_checkpoint else "통화 종료"
                self.log(f"🧠 [기억 정제 시작] {base_cid} ({kind_lbl} · 문맥 오타 교정 및 핵심 기억 추출 중)...")
                raw_out = self.gemini_mgr.run_background_task(
                    backend_mode=backend,
                    api_key=active_key,
                    api_model=active_api_model,
                    prompt=req["prompt"],
                    browser_model=b_model,
                    thinking_level="low",
                    timeout_sec=45.0,
                    provider=provider,
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
                # Refresh active call memory context immediately so long calls retain earlier turns
                with self._active_memory_session_lock:
                    cur_sess = dict(self._active_memory_session) if self._active_memory_session else None
                if (
                    cur_sess
                    and (not cur_sess.get("ephemeral"))
                    and cur_sess.get("base_cid") == base_cid
                    and cur_sess.get("track") == track
                ):
                    try:
                        refreshed_ctx, _ = _call_build_memory_context_block(
                            self.memory_mgr,
                            base_cid=base_cid,
                            track=track,
                            char_data=ch_data,
                            current_variant=str(cur_sess.get("persona_variant") or "official"),
                            reply_lang=str(cur_sess.get("reply_language") or self._get_reply_language()),
                            max_recent_turns=0,
                        )
                        self._active_call_memory_context = refreshed_ctx
                    except Exception:
                        pass
                if not self._closing:
                    self._schedule_ui(self._refresh_momotalk_header_and_cards)
            except Exception as exc:
                self.log(f"[기억 정제 오류] {exc}")
            finally:
                rescan_needed = False
                with self._memory_summary_inflight_lock:
                    self._memory_summary_inflight.discard(key)
                    if key in self._memory_summary_pending_rescan:
                        self._memory_summary_pending_rescan.discard(key)
                        rescan_needed = True
                if rescan_needed and not self._closing:
                    self._enqueue_memory_summary(base_cid, track, session_id, is_checkpoint=False)

    def _refresh_momotalk_header_and_cards(self) -> None:
        if not hasattr(self, "btn_momotalk_call"):
            return

        if not self._momotalk_char_activated:
            self.lbl_momo_hdr_avatar.configure(image="", text="")
            self.lbl_momo_hdr_name.configure(text="")
            self.lbl_momo_hdr_sub.configure(text="")
            if hasattr(self, "lbl_momo_hdr_heart"):
                self.lbl_momo_hdr_heart.pack_forget()
            if hasattr(self, "lbl_momo_hdr_persona_badge"):
                self.lbl_momo_hdr_persona_badge.pack_forget()
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
            if hasattr(self, "_momo_card_scroll"):
                self._momo_card_scroll.pack_forget()
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
        has_voice_link = self._has_active_language_voice_link(eid)
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
        if hasattr(self, "lbl_momo_hdr_persona_badge"):
            p_vcode = PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")
            b_txt, b_bg, b_fg = get_variant_badge_info(
                p_vcode,
                use_custom=bool(self.cfg.get("use_custom_persona", False)),
            )
            self.lbl_momo_hdr_persona_badge.configure(text=b_txt, bg=b_bg, fg=b_fg)
            self.lbl_momo_hdr_persona_badge.pack(side="left", padx=(8, 0))
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
            if hasattr(self, "_momo_card_scroll"):
                self._momo_card_scroll.pack_forget()
            self._momo_history_frame.pack(fill="both", expand=True)
            self._render_momotalk_history_timeline(base_cid, track, disp_name, eid)
        else:
            if hasattr(self, "_momo_history_frame"):
                self._momo_history_frame.pack_forget()
            if hasattr(self, "_momo_card_scroll"):
                self._momo_card_scroll.pack(fill="both", expand=True)
            self._momo_card_outer.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.72)
            if hasattr(self, "btn_card_back_to_history"):
                if has_hist:
                    self.btn_card_back_to_history.pack(pady=(10, 0))
                else:
                    self.btn_card_back_to_history.pack_forget()
            self._sync_momo_card_scroll_layout()
            if hasattr(self, "_momo_card_scroll"):
                self._momo_card_scroll.scroll_to_top()

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
            self._sync_momo_card_scroll_layout()
            return

        # Already linked for current language (or JP-only student in KR mode whose JP link is already set) -> hide cleanly
        if has_active_link or (avail == "fallback_other_lang" and other_linked):
            self.btn_momotalk_auto_voice.pack_forget()
            self._momo_card_voice_bar.pack_forget()
            self._sync_momo_card_scroll_layout()
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
        self._sync_momo_card_scroll_layout()

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
        if not self._has_fish_tts_credentials():
            messagebox.showwarning(
                "Fish Audio 인증 필요",
                "먼저 ⚙️ 설정 창에서 Fish Audio API Key를 입력하거나 공식 MCP 웹 크레딧(OAuth)에 로그인해 주세요.",
            )
            self.open_settings_dialog(first_run=True, initial_tab=1)
            return

        bridge_mode = str(self.cfg.get("bridge_mode", "pc_mic") or "pc_mic")
        if bridge_mode != "gemini_live_audio":
            provider = self._get_ai_provider()
            backend = str(self.cfg.get("gemini_backend", "browser"))
            if backend == "api" and not self._get_active_api_key(provider):
                api_lbl = {"gemini": "Gemini", "claude": "Claude", "chatgpt": "OpenAI(ChatGPT)"}[provider]
                messagebox.showwarning(
                    f"{api_lbl} API Key 필요",
                    f"먼저 ⚙️ 설정 창에서 {api_lbl} API Key를 입력하거나 전용 브라우저 모드로 전환해 주세요.",
                )
                self.open_settings_dialog(first_run=False, initial_tab=1)
                return

        if self.whisper_model is None:
            messagebox.showinfo("잠시만 기다려 주세요", "로컬 Whisper 음성인식 모델이 아직 초기화 중입니다. 2~3초 뒤 다시 눌러 주세요.")
            return

        # Guard if this student's voice is currently being auto-generated, or offer 1-click auto-create when missing
        if (
            ENABLE_ON_DEMAND_VOICE_UI
            and not bool(self.cfg.get("use_custom_persona", False))
            and not self._is_colab_s2pro_enabled()
        ):
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

        # CRITICAL PRIVACY INVARIANT: every new call starts with Screen Share OFF
        self._set_screen_share_enabled(False, silent=True)

        self._mode2_capture_ready = False
        self._calling_login_required = False
        self._calling_auth_kind = ""
        self._calling_error_text = ""
        self._calling_prime_started_mono = 0.0
        self._calling_delay_hint_shown = False
        if bridge_mode == "gemini_live_audio":
            self._calling_stage_text = "🎙️ Gemini Live 오디오 입력 연결 대기 중..."
        else:
            _p_init = self._get_ai_provider()
            _s_init = AI_PROVIDER_SHORT_NAMES[_p_init]
            _b_init = str(self.cfg.get("gemini_backend", "browser"))
            self._calling_stage_text = (
                f"🌐 {_s_init} 전용 브라우저 준비 중..."
                if _b_init == "browser"
                else f"⚡ {_s_init} API 첫인사 준비 중..."
            )
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
            try:
                self.memory_mgr.record_recent_call(
                    base_cid=self.current_char_id,
                    entry_id=eid,
                    increment_calls=False,
                )
            except Exception as exc:
                safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
                self.log(f"⚠️ [최근 통화 기록 경고] 저장소 접근 오류({safe_exc})를 무시하고 통화를 진행합니다.")
        self._ringback_state.start()
        self._set_call_ui_state("calling")
        if self._is_colab_s2pro_enabled():
            self._prewarm_colab_s2pro_current_char()

        if bridge_mode == "gemini_live_audio":
            self.toggle_bridge()
        else:
            self.toggle_companion_mode()

        if not self.companion_running and not self.running:
            self._ringback_state.stop()
            self._set_call_ui_state("idle")

    def stop_momotalk_call(self) -> None:
        self._set_screen_share_enabled(False, silent=True)
        self._ringback_state.stop()
        if self.companion_running:
            self.toggle_companion_mode()
        if self.running:
            self.toggle_bridge()
        self._calling_login_required = False
        self._calling_auth_kind = ""
        self._calling_error_text = ""
        self._calling_stage_text = ""
        self._calling_prime_started_mono = 0.0
        self._calling_delay_hint_shown = False
        self._set_call_ui_state("idle")

    def _sync_calling_quick_bar(self) -> None:
        if not hasattr(self, "_calling_quick_bar"):
            return
        try:
            self._calling_quick_bar.pack_forget()
        except Exception:
            pass
        is_m1_browser = (
            str(self.cfg.get("bridge_mode", "pc_mic") or "pc_mic") != "gemini_live_audio"
            and str(self.cfg.get("gemini_backend", "browser")) == "browser"
        )
        if (
            self._call_ui_state == "calling"
            and is_m1_browser
            and (not self._calling_login_required)
            and (not self._calling_error_text)
        ):
            short = AI_PROVIDER_SHORT_NAMES[self._get_ai_provider()]
            if hasattr(self, "btn_calling_open_browser"):
                self.btn_calling_open_browser.configure(text=f"🌐 {short} 창 보기")
            try:
                self._calling_quick_bar.pack(pady=(2, 4))
            except Exception:
                pass
        self._sync_calling_scroll_layout()

    def _set_calling_stage(self, text: str, *, fg: str = "#CBD5E1", session_id: int | None = None) -> None:
        if session_id is not None and session_id != self._companion_session_id:
            return
        self._calling_stage_text = str(text or "")
        if hasattr(self, "lbl_calling_stage"):
            try:
                self.lbl_calling_stage.configure(text=self._calling_stage_text, fg=fg)
            except Exception:
                pass

    def _clear_calling_error(self) -> None:
        self._calling_error_text = ""
        if hasattr(self, "_calling_error_bar"):
            try:
                self._calling_error_bar.pack_forget()
            except Exception:
                pass
        if self._call_ui_state == "calling" and not self._calling_login_required:
            if hasattr(self, "lbl_calling_status"):
                try:
                    self.lbl_calling_status.configure(text="통화 연결 중...", fg="#FDA4AF")
                except Exception:
                    pass
            self._sync_calling_quick_bar()

    def _set_calling_error(
        self,
        title: str,
        detail: str,
        *,
        provider: str | None = None,
        backend: str = "browser",
        session_id: int | None = None,
    ) -> None:
        if session_id is not None and session_id != self._companion_session_id:
            return
        p = normalize_ai_provider(provider or self._get_ai_provider())
        short = AI_PROVIDER_SHORT_NAMES[p]
        safe_detail = _redact_secrets(str(detail or ""), extra_secrets=self._active_secret_values())
        self._calling_error_text = safe_detail or title
        self._calling_prime_started_mono = 0.0
        self._calling_delay_hint_shown = False
        self._set_calling_login_required(False)
        if hasattr(self, "_calling_quick_bar"):
            try:
                self._calling_quick_bar.pack_forget()
            except Exception:
                pass
        if hasattr(self, "lbl_calling_status"):
            try:
                self.lbl_calling_status.configure(text="⚠️ 연결 실패", fg="#FCA5A5")
            except Exception:
                pass
        if hasattr(self, "lbl_calling_stage"):
            try:
                self.lbl_calling_stage.configure(text="", fg="#CBD5E1")
            except Exception:
                pass
        if hasattr(self, "lbl_calling_error_title"):
            try:
                self.lbl_calling_error_title.configure(text=title)
            except Exception:
                pass
        if hasattr(self, "lbl_calling_error_detail"):
            try:
                self.lbl_calling_error_detail.configure(text=safe_detail)
            except Exception:
                pass
        if hasattr(self, "btn_calling_error_browser"):
            try:
                self.btn_calling_error_browser.pack_forget()
                if backend == "browser":
                    self.btn_calling_error_browser.configure(text=f"🌐 {short} 창 보기")
                    self.btn_calling_error_browser.pack(side="left", padx=(0, 6), before=self.btn_calling_error_retry)
            except Exception:
                pass
        if hasattr(self, "_calling_error_bar") and self._call_ui_state == "calling":
            try:
                self._calling_error_bar.pack(pady=(6, 4))
            except Exception:
                pass
        self._sync_calling_scroll_layout()
        if hasattr(self, "_momo_calling_scroll") and hasattr(self, "_calling_error_bar"):
            try:
                def _scroll_err_bar() -> None:
                    if getattr(self, "_closing", False):
                        return
                    try:
                        if self._momo_calling_scroll.winfo_exists() and self._calling_error_bar.winfo_exists():
                            self._momo_calling_scroll.scroll_to_widget(self._calling_error_bar, pad=12)
                    except Exception:
                        pass

                self._momo_calling_scroll.after_idle(_scroll_err_bar)
            except Exception:
                pass

    def _enter_auth_recovery_ui(
        self,
        session_id: int,
        provider: str,
        *,
        kind: str = "login",
        detail: str = "",
    ) -> None:
        if session_id != self._companion_session_id or not self.companion_running:
            return
        if normalize_ai_provider(provider) != self._get_ai_provider():
            return
        self._set_calling_login_required(True, kind=kind, detail=detail)

    def _enter_calling_error_ui(
        self,
        session_id: int,
        provider: str,
        *,
        backend: str,
        title: str,
        detail: str,
    ) -> None:
        if session_id != self._companion_session_id or not self.companion_running:
            return
        if self._call_ui_state == "calling":
            self._set_calling_error(title, detail, provider=provider, backend=backend, session_id=session_id)

    def _set_calling_login_required(
        self,
        required: bool,
        *,
        kind: str = "login",
        detail: str = "",
    ) -> None:
        self._calling_login_required = bool(required)
        self._calling_auth_kind = kind if self._calling_login_required else ""
        if not hasattr(self, "btn_hdr_login_confirm"):
            return
        short = AI_PROVIDER_SHORT_NAMES[self._get_ai_provider()]
        is_challenge = self._calling_auth_kind == "challenge"
        if self._calling_login_required:
            self._calling_error_text = ""
            self._calling_prime_started_mono = 0.0
            self._calling_delay_hint_shown = False
            if hasattr(self, "_calling_error_bar"):
                try:
                    self._calling_error_bar.pack_forget()
                except Exception:
                    pass
            if hasattr(self, "_calling_quick_bar"):
                try:
                    self._calling_quick_bar.pack_forget()
                except Exception:
                    pass

            hdr_txt = "✅ 보안 확인 완료" if is_challenge else "✅ 로그인 확인"
            calling_status_txt = f"🛡️ {short} 보안 확인 대기 중..." if is_challenge else f"🔑 {short} 로그인 대기 중..."
            calling_msg = (
                f"🛡️ {short} 보안 확인(Cloudflare 사람 확인)이 필요합니다.\n열린 브라우저 창에서 확인을 마친 뒤 [✅ 확인 완료]를 눌러 주세요."
                if is_challenge
                else f"🔑 {short} 로그인이 필요합니다.\n열린 브라우저 창에서 로그인을 완료한 뒤 [✅ 확인 완료]를 눌러 주세요."
            )
            connected_msg = (
                f"🛡️ {short} 보안 확인 필요 — 브라우저 창에서 확인 후 [✅ 확인 완료]를 눌러 주세요."
                if is_challenge
                else f"🔑 {short} 로그인/세션 복구 필요 — 창에서 로그인 후 [✅ 확인 완료]를 눌러 주세요."
            )
            stage_hint = (
                f"{short} 브라우저의 사람 확인을 직접 완료해 주세요"
                if is_challenge
                else f"{short} 브라우저 창에서 계정에 로그인해 주세요"
            )
            if detail:
                safe_d = _redact_secrets(str(detail), extra_secrets=self._active_secret_values())
                if safe_d and len(safe_d) <= 120:
                    stage_hint = safe_d

            try:
                self.btn_hdr_login_confirm.configure(text=hdr_txt)
                self.btn_hdr_login_confirm.pack(side="right", padx=(6, 0), before=self.btn_hdr_settings)
            except Exception:
                pass
            if hasattr(self, "lbl_calling_login_msg"):
                self.lbl_calling_login_msg.configure(text=calling_msg)
            if hasattr(self, "btn_calling_login_browser"):
                self.btn_calling_login_browser.configure(text=f"🌐 {short} 창 열기")
            if hasattr(self, "btn_calling_login_confirm"):
                self.btn_calling_login_confirm.configure(text="✅ 확인 완료")
            if hasattr(self, "lbl_connected_auth_msg"):
                self.lbl_connected_auth_msg.configure(text=connected_msg)
            if hasattr(self, "btn_connected_auth_browser"):
                self.btn_connected_auth_browser.configure(text=f"🌐 {short} 창 열기")

            if self._call_ui_state == "calling":
                if hasattr(self, "_connected_auth_bar"):
                    try:
                        self._connected_auth_bar.pack_forget()
                    except Exception:
                        pass
                if hasattr(self, "_calling_login_bar"):
                    try:
                        self._calling_login_bar.pack(pady=(6, 4))
                    except Exception:
                        pass
                if hasattr(self, "lbl_calling_status"):
                    self.lbl_calling_status.configure(text=calling_status_txt, fg="#FDE68A")
                self._set_calling_stage(stage_hint, fg="#FDE68A")
                self._sync_calling_scroll_layout()
                if hasattr(self, "_momo_calling_scroll") and hasattr(self, "_calling_login_bar"):
                    try:
                        def _scroll_login_bar() -> None:
                            if getattr(self, "_closing", False):
                                return
                            try:
                                if self._momo_calling_scroll.winfo_exists() and self._calling_login_bar.winfo_exists():
                                    self._momo_calling_scroll.scroll_to_widget(self._calling_login_bar, pad=12)
                            except Exception:
                                pass

                        self._momo_calling_scroll.after_idle(_scroll_login_bar)
                    except Exception:
                        pass
            elif self._call_ui_state == "connected":
                if hasattr(self, "_calling_login_bar"):
                    try:
                        self._calling_login_bar.pack_forget()
                    except Exception:
                        pass
                if hasattr(self, "_connected_auth_bar"):
                    try:
                        self._connected_auth_bar.pack(fill="x")
                    except Exception:
                        pass
        else:
            try:
                self.btn_hdr_login_confirm.pack_forget()
            except Exception:
                pass
            if hasattr(self, "_calling_login_bar"):
                try:
                    self._calling_login_bar.pack_forget()
                except Exception:
                    pass
            if hasattr(self, "_connected_auth_bar"):
                try:
                    self._connected_auth_bar.pack_forget()
                except Exception:
                    pass
            if hasattr(self, "lbl_calling_status") and self._call_ui_state == "calling" and not self._calling_error_text:
                self.lbl_calling_status.configure(text="통화 연결 중...", fg="#FDA4AF")
                self._sync_calling_quick_bar()

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
            self._calling_error_text = ""
            self._calling_stage_text = ""
            self._calling_prime_started_mono = 0.0
            self._calling_delay_hint_shown = False
            if hasattr(self, "_calling_error_bar"):
                try:
                    self._calling_error_bar.pack_forget()
                except Exception:
                    pass
            if hasattr(self, "_calling_quick_bar"):
                try:
                    self._calling_quick_bar.pack_forget()
                except Exception:
                    pass
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
            self._momo_calling_frame.pack(fill="both", expand=True)
            self._call_anim_phase = 0.0
            self.lbl_calling_timer.configure(text="00:00")
            if self._calling_error_text:
                self.lbl_calling_status.configure(text="⚠️ 연결 실패", fg="#FCA5A5")
            elif self._calling_login_required:
                short = AI_PROVIDER_SHORT_NAMES[self._get_ai_provider()]
                self.lbl_calling_status.configure(
                    text=f"🛡️ {short} 보안 확인 대기 중..." if self._calling_auth_kind == "challenge" else f"🔑 {short} 로그인 대기 중...",
                    fg="#FDE68A",
                )
            else:
                self.lbl_calling_status.configure(text="통화 연결 중...", fg="#FDA4AF")
                if hasattr(self, "_calling_error_bar"):
                    try:
                        self._calling_error_bar.pack_forget()
                    except Exception:
                        pass
            if hasattr(self, "lbl_calling_stage"):
                self.lbl_calling_stage.configure(
                    text=self._calling_stage_text,
                    fg="#FDE68A" if self._calling_login_required else "#CBD5E1",
                )
            self._sync_calling_quick_bar()
            self._sync_calling_scroll_layout()
            if hasattr(self, "_momo_calling_scroll"):
                self._momo_calling_scroll.scroll_to_top()
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
            if self._call_timer_after_id is not None:
                try:
                    self.root.after_cancel(self._call_timer_after_id)
                except Exception:
                    pass
                self._call_timer_after_id = None
            self._calling_error_text = ""
            self._calling_prime_started_mono = 0.0
            self._calling_delay_hint_shown = False
            if hasattr(self, "_calling_error_bar"):
                try:
                    self._calling_error_bar.pack_forget()
                except Exception:
                    pass
            if hasattr(self, "_calling_quick_bar"):
                try:
                    self._calling_quick_bar.pack_forget()
                except Exception:
                    pass
            self._set_calling_login_required(False)
            self._momo_connected_bar.pack(fill="x")
            self._momo_subtitle_host.pack(fill="both", expand=True)
            self.lbl_live_call_timer.configure(text="00:00")
            self._rerender_subtitles()
            self._tick_call_timer()
            if self._sv_available:
                self._refresh_screen_vision_targets(silent=True)
                self._refresh_screen_vision_bar_ui()

        self._refresh_momotalk_header_and_cards()

    def _on_call_connected_ui(self, expected_session_id: int | None = None) -> None:
        if expected_session_id is not None and expected_session_id != self._companion_session_id:
            return
        with self._call_state_lock:
            if self._call_ui_state != "calling":
                return
        if not (self.companion_running or self.running):
            return
        self._set_call_ui_state("connected")
        cname = self.current_char.get("name_kr", self.current_char_id)
        self.log(f"📞 [통화 연결 완료] '{cname}'과(와) 실시간 보이스 통화 및 대사 자막 표시를 시작합니다.")

    def _complete_call_connection_from_audio_thread(
        self,
        stream: Any,
        play_sr: int,
        expected_session_id: int | None = None,
        expected_audio_sid: int | None = None,
    ) -> None:
        if expected_session_id is not None and expected_session_id != self._companion_session_id:
            return
        if expected_audio_sid is not None and expected_audio_sid != self._audio_session_id:
            return
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
                    vol = _safe_cfg_float(self._live_vol, 1.0, _OUTPUT_VOLUME_MIN, _OUTPUT_VOLUME_MAX)
                    if vol != 1.0:
                        chime = np.clip(chime * vol, -0.95, 0.95).astype(np.float32)
                    stream.write(chime)
                except Exception:
                    pass
            # Run UI transition cleanly (note _call_ui_state is now "connected", so apply UI switch directly)
            def _apply_connected() -> None:
                if expected_session_id is not None and expected_session_id != self._companion_session_id:
                    return
                if expected_audio_sid is not None and expected_audio_sid != self._audio_session_id:
                    return
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
        self._call_anim_after_id = None
        if self._call_ui_state != "calling" or getattr(self, "_closing", False):
            return
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        self._call_anim_phase = (self._call_anim_phase + 0.035) % 1.0
        self._redraw_calling_canvas()
        if self._call_ui_state == "calling" and not getattr(self, "_closing", False):
            try:
                self._call_anim_after_id = self.root.after(55, self._animate_calling_canvas)
            except Exception:
                self._call_anim_after_id = None

    def _tick_call_timer(self) -> None:
        self._call_timer_after_id = None
        state = self._call_ui_state
        if state not in ("calling", "connected") or getattr(self, "_closing", False):
            self._last_call_timer_txt = ""
            return
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
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
            if (
                state == "calling"
                and self.companion_running
                and (not self._calling_login_required)
                and (not self._calling_error_text)
                and self._calling_prime_started_mono > 0.0
                and (not self._calling_delay_hint_shown)
                and (time.monotonic() - self._calling_prime_started_mono) >= 10.0
            ):
                self._calling_delay_hint_shown = True
                p_cur = self._get_ai_provider()
                short_cur = AI_PROVIDER_SHORT_NAMES[p_cur]
                if str(self.cfg.get("gemini_backend", "browser")) == "browser":
                    self._set_calling_stage(
                        f"⏳ {short_cur} 응답이 지연 중입니다 — [🌐 {short_cur} 창 보기]로 팝업·로그인을 확인해 보세요",
                        fg="#FDE68A",
                    )
                else:
                    self._set_calling_stage(
                        f"⏳ {short_cur} API 응답이 지연 중입니다 — 잠시만 기다려 주세요",
                        fg="#FDE68A",
                    )
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
            if not is_momotalk_released_character(c):
                continue
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
        voice_only = bool(getattr(self, "_roster_voice_only", False))
        if nav_tab == "recent":
            recent_ids = list(getattr(self, "_recent_called_entry_ids", []) or [])
            recent_pos = {eid: idx for idx, eid in enumerate(recent_ids)}
            matched_recent = [
                (recent_pos[eid], eid)
                for _ent, eid, hay in self._momotalk_search_default
                if eid in recent_pos and (not q or q in hay) and (not voice_only or self._has_active_language_voice_link(eid))
            ]
            matched_recent.sort()
            filtered_eids = [eid for _, eid in matched_recent]
        else:
            source_rows = self._momotalk_search_name if sort_mode == "name" else self._momotalk_search_default
            filtered_eids = [
                eid
                for _ent, eid, hay in source_rows
                if (not q or q in hay) and (not voice_only or self._has_active_language_voice_link(eid))
            ]
            if sort_mode == "heart":
                filtered_eids.sort(key=lambda eid: 0 if self._has_active_language_voice_link(eid) else 1)

        self._momotalk_filtered_cids = filtered_eids
        self._last_scrollregion = (0, 0)
        filter_key = (q, nav_tab, sort_mode, voice_only, self._get_reply_language())
        if filter_key != getattr(self, "_last_roster_filter_key", None):
            self._last_roster_filter_key = filter_key
            if hasattr(self, "roster_canvas"):
                try:
                    self.roster_canvas.yview_moveto(0.0)
                    self._last_yview_top = 0.0
                except Exception:
                    pass
        if hasattr(self, "lbl_roster_count"):
            hdr_lbl = "최근 통화" if nav_tab == "recent" else "학생"
            v_suffix = " 💗" if voice_only else ""
            self.lbl_roster_count.configure(text=f"{hdr_lbl}{v_suffix} ({len(filtered_eids)})")

        if not matched_base_cids:
            self.char_display_var.set("검색 결과 없음")
        elif (not getattr(self, "_ui_boot_complete", False)) or (self.current_char_id not in self.characters):
            target_cid = self.current_char_id if self.current_char_id in cid_to_disp else matched_base_cids[0]
            self.char_display_var.set(cid_to_disp[target_cid])
            self._apply_selected_character(target_cid)
        elif self.current_char_id in cid_to_disp:
            self.char_display_var.set(cid_to_disp[self.current_char_id])
        else:
            # Keep active character & live call untouched while user is merely filtering/searching roster
            self.char_display_var.set(cid_to_disp[matched_base_cids[0]])
        self._render_roster_visible(force_all=True)

    def _on_char_combo_selected(self, _event=None) -> None:
        cid = self.display_to_cid.get(self.char_display_var.get())
        if cid:
            self.select_momotalk_character(cid)

    def _clear_subtitle_translator_context(self) -> None:
        tr = getattr(self, "subtitle_translator", None)
        clear_fn = getattr(tr, "clear_context", None) if tr is not None else None
        if callable(clear_fn):
            try:
                clear_fn()
            except Exception:
                pass
        elif tr is not None:
            ctx = getattr(tr, "_recent_context", None)
            if ctx is not None and hasattr(ctx, "clear"):
                try:
                    ctx.clear()
                except Exception:
                    pass

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
            self._persist_cfg_locked()

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
        if getattr(self, "_ui_boot_complete", False) and self._is_colab_s2pro_enabled():
            self._prewarm_colab_s2pro_current_char(char_override=c)
        if not self.running:
            self.btn_toggle.config(text=f"▶️ 실시간 [{c.get('name_kr', cid)}] 음성 브릿지 시작")
        self.mic_stt.set_character_context(c)
        self._refresh_momotalk_header_and_cards()
        if prev_cid != cid:
            self._clear_subtitle_translator_context()
            self._on_active_persona_potentially_changed(f"캐릭터 변경: {c.get('name_kr', cid)}")

    def _on_persona_variant_changed(self, _event=None) -> None:
        self._refresh_persona_summary_ui()
        self.save_settings_ui(silent=True)
        self._refresh_momotalk_header_and_cards()
        self._on_active_persona_potentially_changed("페르소나 해석 모드 변경")

    def _get_ai_provider(self) -> str:
        raw = self._read_tk_var_safe("ai_provider_var", self.cfg.get("ai_provider", "gemini"))
        return normalize_ai_provider(raw or self.cfg.get("ai_provider", "gemini"))

    def _get_active_api_key(self, provider: str | None = None) -> str:
        p = normalize_ai_provider(provider or self._get_ai_provider())
        key_name = {"gemini": "gemini_api_key", "claude": "claude_api_key", "chatgpt": "openai_api_key"}[p]
        return str(self.cfg.get(key_name, "") or "").strip()

    def _is_matching_provider_api_model(self, model_id: str, provider: str) -> bool:
        p = normalize_ai_provider(provider)
        low = str(model_id or "").strip().lower()
        if not low:
            return False
        if p == "gemini":
            return not low.startswith(("claude-", "gpt-"))
        if p == "claude":
            return not low.startswith(("gemini-", "gpt-"))
        return not low.startswith(("gemini-", "claude-"))

    def _sanitize_api_model_list(self, raw_models: Any, provider: str) -> list[str]:
        p = normalize_ai_provider(provider)
        if not isinstance(raw_models, (list, tuple)):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_models:
            cid = extract_safe_api_model_id(item, provider=p)
            if cid and cid not in seen and self._is_matching_provider_api_model(cid, p):
                seen.add(cid)
                out.append(cid)
        return out

    def _get_active_api_model(self, provider: str | None = None) -> str:
        p = normalize_ai_provider(provider or self._get_ai_provider())
        cfg_key = {"gemini": "gemini_api_model", "claude": "claude_api_model", "chatgpt": "openai_api_model"}[p]
        default_m = get_provider_default_api_model(p)
        clean = coerce_safe_api_model_id(self.cfg.get(cfg_key, default_m), provider=p, default=default_m)
        if not self._is_matching_provider_api_model(clean, p):
            clean = default_m
        if isinstance(self.cfg, dict) and cfg_key in self.cfg and self.cfg.get(cfg_key) != clean:
            self.cfg[cfg_key] = clean
        return clean

    def _get_active_browser_model(self, provider: str | None = None) -> str:
        p = normalize_ai_provider(provider or self._get_ai_provider())
        if p == "gemini":
            b_m, _ = split_legacy_browser_model(
                str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"),
                str(self.cfg.get("gemini_thinking_level", "low") or "low"),
            )
            return b_m
        cfg_key = "claude_browser_model" if p == "claude" else "chatgpt_browser_model"
        return str(self.cfg.get(cfg_key, "default") or "default").strip() or "default"

    def _get_active_extra_api_models(self, provider: str | None = None) -> list[str]:
        p = normalize_ai_provider(provider or self._get_ai_provider())
        if p == "gemini":
            return self._sanitize_api_model_list(self._extra_api_models, "gemini")
        cfg_key = "claude_api_models" if p == "claude" else "openai_api_models"
        return self._sanitize_api_model_list(self.cfg.get(cfg_key, []), p)

    @staticmethod
    def _is_valid_synced_web_models(models: Any, provider: str | None = None) -> bool:
        if not isinstance(models, (list, tuple)) or not models:
            return False
        clean = [
            (str(it[0]).strip(), str(it[1]).strip())
            for it in models
            if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[0]).strip() and str(it[1]).strip()
        ]
        if not clean:
            return False
        if len(clean) == 1 and clean[0][0].lower() == "default":
            return False
        return True

    def _get_active_account_web_models(self, provider: str | None = None) -> list[tuple[str, str]]:
        p = normalize_ai_provider(provider or self._get_ai_provider())
        if p == "gemini":
            parsed = list(self._account_web_models)
        else:
            cfg_key = "claude_web_models" if p == "claude" else "chatgpt_web_models"
            raw = self.cfg.get(cfg_key) or []
            parsed = [
                (str(it[0]).strip(), str(it[1]).strip())
                for it in raw
                if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[0]).strip() and str(it[1]).strip()
            ]
        return parsed if self._is_valid_synced_web_models(parsed, p) else []

    def _abort_active_browser_prompt(self) -> None:
        """Cooperatively abort any in-flight browser prompt/boot/sync so _io_lock releases quickly."""
        try:
            if hasattr(self.gemini_mgr, "abort_active_prompt"):
                self.gemini_mgr.abort_active_prompt()
            elif hasattr(self.gemini_mgr.browser, "abort_active_prompt"):
                self.gemini_mgr.browser.abort_active_prompt()
        except Exception:
            pass

    def _is_browser_provider_active(self, provider: str) -> bool:
        """Lock-free check whether the isolated browser is already running for `provider`."""
        p = normalize_ai_provider(provider)
        try:
            if hasattr(self.gemini_mgr, "is_provider_active"):
                return bool(self.gemini_mgr.is_provider_active(p))
        except Exception:
            pass
        try:
            br = self.gemini_mgr.browser
            return normalize_ai_provider(getattr(br, "provider", "")) == p and bool(br.is_running())
        except Exception:
            return False

    def _ensure_browser_ready_and_check_login(
        self,
        *,
        show_if_ready: bool = False,
        allow_interactive_login: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, str]:
        fn = (
            self.gemini_mgr.ensure_ready_and_check_login
            if hasattr(self.gemini_mgr, "ensure_ready_and_check_login")
            else self.gemini_mgr.browser.ensure_ready_and_check_login
        )
        try:
            return fn(
                show_if_ready=show_if_ready,
                allow_interactive_login=allow_interactive_login,
                cancel_event=cancel_event,
            )
        except TypeError:
            return fn(
                show_if_ready=show_if_ready,
                allow_interactive_login=allow_interactive_login,
            )

    def _cancel_active_companion_turn(self, *, drain_queue: bool = True, abort_browser: bool = True) -> None:
        with self._turn_interrupt_lock:
            self._active_turn_interrupt.set()
            self.abort_current_playback()
            self._active_audio_playing_until = 0.0
            self._barge_in_ducking = False
            if drain_queue:
                self._drain_companion_turn_queue()
        if abort_browser:
            self._abort_active_browser_prompt()

    def _try_fast_set_provider_if_idle(self, new_p: str, browser_model: str) -> bool:
        """Apply set_provider synchronously ONLY when no browser process/CDP is running and _io_lock is uncontended."""
        br = getattr(self.gemini_mgr, "browser", None)
        if br is None or self.companion_running:
            return False
        io_lock = getattr(br, "_io_lock", None)
        if io_lock is None:
            return False
        acquired = False
        try:
            acquired = bool(io_lock.acquire(blocking=False))
            if not acquired:
                return False
            if br.is_running() or getattr(br, "_cdp", None) is not None:
                return False
            self.gemini_mgr.set_provider(new_p, browser_model=browser_model)
            return True
        except Exception:
            return False
        finally:
            if acquired:
                try:
                    io_lock.release()
                except Exception:
                    pass

    def _switch_browser_provider_nonblocking(
        self,
        new_p: str,
        browser_model: str,
        *,
        relaunch_call: bool = False,
    ) -> None:
        """Switch provider without ever blocking the Tk main thread on _io_lock."""
        p = normalize_ai_provider(new_p)
        short = AI_PROVIDER_SHORT_NAMES[p]
        backend = str(self.cfg.get("gemini_backend", "browser"))

        if (not relaunch_call) and self._try_fast_set_provider_if_idle(p, browser_model):
            return

        if self.companion_running:
            self._cancel_active_companion_turn(drain_queue=True, abort_browser=True)
            self.gemini_mgr.invalidate_session()
        else:
            self._abort_active_browser_prompt()

        with self._provider_switch_lock:
            self._provider_switch_seq += 1
            my_seq = self._provider_switch_seq

        if relaunch_call and self.companion_running:
            if backend == "api" and not self._get_active_api_key(p):
                api_lbl = {"gemini": "Gemini", "claude": "Claude", "chatgpt": "OpenAI(ChatGPT)"}[p]
                self.log(f"⚠️ [{short} API 키 필요] 설정 창에서 {api_lbl} API Key를 입력하거나 전용 브라우저 모드로 전환해 주세요.")
                threading.Thread(
                    target=self._run_bg_provider_switch,
                    args=(p, browser_model, my_seq),
                    daemon=True,
                    name="switch-ai-provider",
                ).start()
                self.open_settings_dialog(first_run=False, initial_tab=1)
                return

            if backend == "browser":
                self._companion_session_ready = False
                session_id = self._companion_session_id
                cancel_ev = self._companion_cancel_event
                if self._call_ui_state == "calling":
                    self._clear_calling_error()
                    self._set_calling_login_required(False)
                    self._set_calling_stage(f"🌐 {short} 전용 브라우저 전환 중...", session_id=session_id)
                    self._ringback_state.resume()

                def _relaunch_browser() -> None:
                    with self._provider_switch_lock:
                        if my_seq != self._provider_switch_seq:
                            return
                    if (
                        self._closing
                        or (not self.companion_running)
                        or session_id != self._companion_session_id
                        or cancel_ev.is_set()
                        or self._get_ai_provider() != p
                    ):
                        return
                    self.log(f"🌐 [{short} 브라우저 전환] 전용 브라우저에서 {short} 세션을 준비 중입니다...")
                    try:
                        self.gemini_mgr.set_provider(p, browser_model=browser_model)
                    except Exception as exc:
                        safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
                        self.log(f"[{short} 브라우저 전환 오류] {safe_exc}")
                        if session_id == self._companion_session_id and self.companion_running:
                            self._ringback_state.stop()
                            if self._call_ui_state == "calling":
                                self._schedule_ui(
                                    lambda sid=session_id, pr=p, d=safe_exc: self._enter_calling_error_ui(
                                        sid,
                                        pr,
                                        backend="browser",
                                        title=f"⚠️ {AI_PROVIDER_SHORT_NAMES[pr]} 브라우저 전환 실패",
                                        detail=d,
                                    )
                                )
                            elif (not cancel_ev.is_set()) and self._get_ai_provider() == p:
                                self._companion_session_ready = True
                        return
                    if (
                        self._closing
                        or (not self.companion_running)
                        or session_id != self._companion_session_id
                        or cancel_ev.is_set()
                        or self._get_ai_provider() != p
                    ):
                        return
                    ok, reason = self._ensure_browser_ready_and_check_login(
                        show_if_ready=False,
                        cancel_event=cancel_ev,
                    )
                    if (
                        self._closing
                        or (not self.companion_running)
                        or session_id != self._companion_session_id
                        or cancel_ev.is_set()
                        or self._get_ai_provider() != p
                    ):
                        return
                    if not ok:
                        if reason in ("CANCELLED", "ABORTED"):
                            return
                        if reason in ("LOGIN_REQUIRED", "CHALLENGE_REQUIRED"):
                            auth_kind = "challenge" if reason == "CHALLENGE_REQUIRED" else "login"
                            self._ringback_state.pause()
                            self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 숨기기"))
                            self._schedule_ui(
                                lambda sid=session_id, pr=p, k=auth_kind: self._enter_auth_recovery_ui(sid, pr, kind=k)
                            )
                            if auth_kind == "challenge":
                                self.log(
                                    f"🛡️ [{short} 보안 확인 안내] 열린 브라우저 창에서 Cloudflare 사람 확인을 완료한 뒤 [✅ 확인 완료]를 눌러 주세요!"
                                )
                            else:
                                self.log(
                                    f"🔑 [{short} 로그인 안내] 열린 브라우저 창에서 {short} 로그인을 완료한 뒤 [✅ 확인 완료]를 눌러 주세요!"
                                )
                        else:
                            safe_reason = _redact_secrets(str(reason), extra_secrets=self._active_secret_values())
                            self.log(f"[{short} 브라우저 오류] {safe_reason}")
                            self._ringback_state.stop()
                            if self._call_ui_state == "calling":
                                self._schedule_ui(
                                    lambda sid=session_id, pr=p, d=safe_reason: self._enter_calling_error_ui(
                                        sid,
                                        pr,
                                        backend="browser",
                                        title=f"⚠️ {AI_PROVIDER_SHORT_NAMES[pr]} 브라우저 연결 실패",
                                        detail=d,
                                    )
                                )
                            else:
                                if (
                                    self.companion_running
                                    and session_id == self._companion_session_id
                                    and (not cancel_ev.is_set())
                                    and self._get_ai_provider() == p
                                ):
                                    self._companion_session_ready = True

                                def _append_relaunch_err(pr_short: str = short, err_txt: str = safe_reason) -> None:
                                    self._subtitle_seq += 1
                                    self._append_subtitle_entry_fast(
                                        SubtitleEntry(
                                            entry_id=self._subtitle_seq,
                                            char_id="__system__",
                                            char_name_kr=f"{pr_short} 연결 안내",
                                            char_name_jp="",
                                            jp_text=f"{pr_short} 브라우저 전환에 실패했습니다: {err_txt}",
                                            kr_text=f"{pr_short} 브라우저 전환에 실패했습니다: {err_txt}",
                                            is_turn_start=True,
                                        )
                                    )

                                self._schedule_ui(_append_relaunch_err)
                        return
                    self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 보기"))
                    self._schedule_ui(lambda: self._set_calling_login_required(False))
                    if (
                        self.companion_running
                        and session_id == self._companion_session_id
                        and (not cancel_ev.is_set())
                        and self._get_ai_provider() == p
                    ):
                        self._companion_session_ready = True
                        self._calling_prime_started_mono = time.monotonic()
                        self._calling_delay_hint_shown = False
                        self._schedule_ui(
                            lambda sid=session_id, s=short: self._set_calling_stage(
                                f"🧠 {s} 첫인사 응답 기다리는 중...",
                                session_id=sid,
                            )
                        )
                        self._schedule_ui(lambda sid=session_id: self._reset_conversation_if_session(sid))

                threading.Thread(target=_relaunch_browser, daemon=True, name="switch-ai-browser").start()
                return

            # API backend while call is active
            threading.Thread(
                target=self._run_bg_provider_switch,
                args=(p, browser_model, my_seq),
                daemon=True,
                name="switch-ai-provider",
            ).start()
            self.reset_companion_conversation_async()
            return

        threading.Thread(
            target=self._run_bg_provider_switch,
            args=(p, browser_model, my_seq),
            daemon=True,
            name="switch-ai-provider",
        ).start()

    def _run_bg_provider_switch(self, provider: str, browser_model: str, expected_seq: int) -> None:
        with self._provider_switch_lock:
            if expected_seq != self._provider_switch_seq or self._closing:
                return
            try:
                self.gemini_mgr.set_provider(provider, browser_model=browser_model)
            except Exception as exc:
                safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
                self.log(f"[AI 엔진 전환 오류] {safe_exc}")

    def _reset_conversation_if_session(self, expected_session_id: int) -> None:
        if expected_session_id != self._companion_session_id or not self.companion_running:
            return
        self.reset_companion_conversation_async()

    def _get_active_web_tier(self, provider: str | None = None) -> str:
        p = normalize_ai_provider(provider or self._get_ai_provider())
        if p == "gemini":
            return str(self._web_account_tier or self.cfg.get("web_account_tier", "") or "").strip()
        cfg_key = "claude_web_account_tier" if p == "claude" else "chatgpt_web_account_tier"
        return str(self.cfg.get(cfg_key, "") or "").strip()

    def _update_ai_provider_toggle_style(self) -> None:
        if not hasattr(self, "btn_hdr_ai_gemini"):
            return
        cur = self._get_ai_provider()
        active_style = {
            "bg": "#FFFFFF",
            "fg": "#D94B6A",
            "activebackground": "#FFEAF0",
            "activeforeground": "#BE185D",
            "font": ("Malgun Gothic", 8, "bold"),
        }
        inactive_style = {
            "bg": "#E45C7A",
            "fg": "#FFF1F4",
            "activebackground": "#D94B6A",
            "activeforeground": "#FFFFFF",
            "font": ("Malgun Gothic", 8),
        }
        for p_code, btn in (
            ("gemini", self.btn_hdr_ai_gemini),
            ("claude", self.btn_hdr_ai_claude),
            ("chatgpt", self.btn_hdr_ai_chatgpt),
        ):
            btn.configure(**(active_style if cur == p_code else inactive_style))

    def set_ai_provider(self, provider: str, *, save: bool = True, restart_call: bool = True) -> None:
        new_p = normalize_ai_provider(provider)
        prev_p = self._get_ai_provider()
        self.ai_provider_var.set(new_p)
        self.cfg["ai_provider"] = new_p
        self._update_ai_provider_toggle_style()
        short = AI_PROVIDER_SHORT_NAMES[new_p]
        self._browser_toggle_var.set(f"🌐 {short} 화면 보기")

        if new_p != prev_p:
            # If subtitle engine belonged to the previous provider, reset smoothly to auto_inline
            cur_sub = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
            if cur_sub not in ("auto_inline", "fast_nmt"):
                self.cfg["subtitle_engine"] = "auto_inline"
            self.log(f"🤖 [AI 엔진 전환] {AI_PROVIDER_LABELS[new_p]} 모드로 전환했습니다.")
        self._refresh_companion_badges()
        self._sync_calling_quick_bar()
        self._on_ai_provider_changed_for_screen_vision(new_p)
        if save:
            self._persist_cfg_locked()

        if new_p != prev_p:
            self._switch_browser_provider_nonblocking(
                new_p,
                self._get_active_browser_model(new_p),
                relaunch_call=bool(restart_call and self.companion_running),
            )

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
        self._refresh_character_filter_list(keep_current=True)
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
        self.cfg["mic_stt_language"] = eff_lang
        base_sil = _safe_cfg_float(self.cfg.get("mic_silence_end_sec", 0.68), 0.68, 0.35, 2.5)
        eff_sil = max(base_sil, 1.08) if coach_on else base_sil
        eff_beam = 3 if getattr(self, "_whisper_is_cuda", False) else 1
        rms_th = _safe_cfg_float(self.cfg.get("mic_vad_rms_threshold", 0.010), 0.010, 0.002, 0.15)
        try:
            self.mic_stt.configure(
                rms_threshold=rms_th,
                silence_end_sec=eff_sil,
                stt_language=eff_lang,
                beam_size=eff_beam,
            )
        except TypeError:
            self.mic_stt.configure(
                rms_threshold=rms_th,
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
            self._refresh_momotalk_header_and_cards()
            self._refresh_character_filter_list(keep_current=True)
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
        if self._subtitle_entries:
            self._mirror_subtitle_to_mini_overlay(self._subtitle_entries[-1])
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

    def _active_web_models(self, provider: str | None = None) -> list[tuple[str, str]]:
        p = normalize_ai_provider(provider or self._get_ai_provider())
        acct_web = self._get_active_account_web_models(p)
        if acct_web:
            seen = {c for c, _ in acct_web}
            extra = [(c, l) for c, l in (self._extra_web_models if p == "gemini" else []) if c not in seen]
            return list(acct_web) + extra
        base = get_provider_browser_models(p)
        base_codes = {c for c, _ in base}
        extra = [(c, l) for c, l in (self._extra_web_models if p == "gemini" else []) if c not in base_codes]
        return base + extra

    def _subtitle_choices(self) -> list[tuple[str, str]]:
        p = self._get_ai_provider()
        cur_sub = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
        extra_api = self._get_active_extra_api_models(p)
        api_prefix = {"gemini": "api:", "claude": "claude_api:", "chatgpt": "openai_api:"}[p]
        web_prefix = {"gemini": "web:", "claude": "claude_web:", "chatgpt": "chatgpt_web:"}[p]
        if cur_sub.startswith(api_prefix):
            suffix = extract_safe_api_model_id(cur_sub[len(api_prefix) :], provider=p)
            if suffix and self._is_matching_provider_api_model(suffix, p) and suffix not in extra_api:
                extra_api.append(suffix)
        extra_web = list(self._extra_web_models) if p == "gemini" else []
        if cur_sub.startswith(web_prefix):
            suffix = cur_sub[len(web_prefix) :].strip()
            active_web_codes = {c for c, _ in self._active_web_models(p)}
            if suffix and suffix not in active_web_codes and all(c != suffix for c, _ in extra_web):
                extra_web.append((suffix, f"🌐 웹 {AI_PROVIDER_SHORT_NAMES[p]}: {suffix}"))
        acct_web = self._get_active_account_web_models(p)
        return get_subtitle_engine_choices(
            extra_api_models=extra_api,
            extra_web_models=extra_web,
            web_models=acct_web or None,
            provider=p,
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
        self._persist_cfg_locked()
        self._sync_subtitle_thinking_widget()
        self.log(f"[자막 모델 변경] {resolve_subtitle_engine_label(code, choices)}")

    def _on_subtitle_thinking_changed(self, _event: Any = None) -> None:
        lbl_to_code = {lbl: code for code, lbl in GEMINI_THINKING_LEVELS}
        level = lbl_to_code.get(self.subtitle_thinking_var.get().strip(), "low")
        self.cfg["subtitle_thinking_level"] = level
        self._persist_cfg_locked()
        self.log(f"[자막 추론 품질 변경] {GEMINI_THINKING_LEVEL_LABELS.get(level, level)}")

    def _on_companion_model_changed(self, _event: Any = None) -> None:
        p = self._get_ai_provider()
        short = AI_PROVIDER_SHORT_NAMES[p]
        backend = str(self.cfg.get("gemini_backend", "browser"))
        sel = self.companion_model_var.get().strip()
        lvl = str(self.cfg.get("gemini_thinking_level", "low") or "low")
        if lvl not in GEMINI_THINKING_LEVEL_LABELS:
            lvl = "low"
        if backend == "api":
            fallback_m = self._get_active_api_model(p)
            active_web = self._active_web_models(p)
            web_tokens = {c for c, _ in active_web} | {l for _, l in active_web}
            raw_sel = "" if sel in web_tokens else sel
            code = coerce_safe_api_model_id(raw_sel, provider=p, default=fallback_m)
            if not self._is_matching_provider_api_model(code, p):
                code = fallback_m
            cfg_key = {"gemini": "gemini_api_model", "claude": "claude_api_model", "chatgpt": "openai_api_model"}[p]
            self.cfg[cfg_key] = code
            self.log(f"[대화 모델 변경] {short} API -> {code} (추론: {GEMINI_THINKING_LEVEL_LABELS.get(lvl, lvl)})")
        else:
            all_web = self._active_web_models(p)
            lbl_to_code = {lbl: code for code, lbl in all_web}
            def_b = get_provider_default_browser_model(p)
            code = lbl_to_code.get(sel, def_b)
            cfg_key = {"gemini": "gemini_browser_model", "claude": "claude_browser_model", "chatgpt": "chatgpt_browser_model"}[p]
            self.cfg[cfg_key] = code
            if p == "gemini" and code in ("carbon_4", "carbon"):
                self.log("💡 [참고] 4 카본은 Google AI Ultra 계정 전용입니다. 미구독 계정에서는 업그레이드 안내가 자동 취소됩니다.")
            self.gemini_mgr.browser.set_chat_model_async(code, lvl)
            b_lbl = dict(all_web).get(code, code)
            self.log(f"[대화 모델 변경] 웹 {short} -> {b_lbl} (추론: {GEMINI_THINKING_LEVEL_LABELS.get(lvl, lvl)})")
        self._persist_cfg_locked()
        self._refresh_companion_badges()

    def _on_companion_thinking_changed(self, _event: Any = None) -> None:
        p = self._get_ai_provider()
        short = AI_PROVIDER_SHORT_NAMES[p]
        lbl_to_code = {lbl: code for code, lbl in GEMINI_THINKING_LEVELS}
        lvl = lbl_to_code.get(self.companion_thinking_var.get().strip(), "low")
        self.cfg["gemini_thinking_level"] = lvl
        backend = str(self.cfg.get("gemini_backend", "browser"))
        if backend == "api":
            api_code = self._get_active_api_model(p)
            self.log(f"[대화 추론 품질 변경] {short} API({api_code}) -> {GEMINI_THINKING_LEVEL_LABELS.get(lvl, lvl)}")
        else:
            b_code = self._get_active_browser_model(p)
            if p == "gemini":
                self.cfg["gemini_browser_model"] = b_code
            self.gemini_mgr.browser.set_chat_model_async(b_code, lvl)
            self.log(f"[대화 추론 품질 변경] 웹 {short}({b_code}) -> {GEMINI_THINKING_LEVEL_LABELS.get(lvl, lvl)}")
        self._persist_cfg_locked()
        self._refresh_companion_badges()

    def _translate_subtitle_with_llm(
        self,
        jp_text: str,
        style_guide: str,
        context_lines: list[str],
        engine: str,
        thinking_level: str = "low",
    ) -> str:
        """Route subtitle refinement requests to active/selected AI API (Gemini/Claude/OpenAI) or hidden browser tab."""
        if getattr(self, "_closing", False):
            return ""
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

        provider = self._get_ai_provider()
        backend = str(self.cfg.get("gemini_backend", "browser"))

        def _web_timeout(model_code: str, level: str) -> float:
            if level == "xhigh":
                return 60.0
            if level == "high":
                return 48.0
            if level == "medium":
                return 34.0
            return 20.0 if model_code in ("auto", "flash_lite", "haiku", "mini") else 26.0

        def _call_provider_api(target_p: str, target_model: str) -> str:
            k = self._get_active_api_key(target_p)
            if not k:
                return ""
            default_m = self._get_active_api_model(target_p)
            m = coerce_safe_api_model_id(target_model, provider=target_p, default=default_m)
            if not self._is_matching_provider_api_model(m, target_p):
                m = default_m
            return self.gemini_mgr.get_api_backend(target_p).translate_short_line(
                api_key=k,
                model=m,
                jp_text=jp_text,
                style_guide=style_guide,
                context_lines=context_lines,
                thinking_level=eff_level,
            )

        def _is_single_stream_web_busy(target_browser_p: str | None = None) -> bool:
            active_bp = normalize_ai_provider(target_browser_p or self.gemini_mgr.browser.provider)
            return bool(self.companion_running and backend == "browser" and active_bp in ("claude", "chatgpt"))

        for prefix, target_p in (("web:", "gemini"), ("claude_web:", "claude"), ("chatgpt_web:", "chatgpt")):
            if eff.startswith(prefix):
                b_model = eff[len(prefix) :].strip() or get_provider_default_browser_model(target_p)
                if self.gemini_mgr.browser.provider != target_p:
                    # Avoid hijacking an active different-provider browser; fall back to current browser or API
                    if backend == "api":
                        out_fb = _call_provider_api(provider, self._get_active_api_model(provider))
                        if out_fb:
                            return out_fb
                    b_model = self._get_active_browser_model(self.gemini_mgr.browser.provider)
                if _is_single_stream_web_busy(self.gemini_mgr.browser.provider):
                    out_fb = _call_provider_api(provider, self._get_active_api_model(provider))
                    return out_fb or ""
                return self.gemini_mgr.browser.translate_via_browser_tab(
                    jp_text=jp_text,
                    style_guide=style_guide,
                    context_lines=context_lines,
                    model_code=b_model,
                    thinking_level=eff_level,
                    timeout_sec=_web_timeout(b_model, eff_level),
                )

        for prefix, target_p in (("api:", "gemini"), ("claude_api:", "claude"), ("openai_api:", "chatgpt")):
            if eff.startswith(prefix):
                default_sub_m = self._get_active_api_model(target_p)
                sub_api_model = coerce_safe_api_model_id(
                    eff[len(prefix) :].strip(),
                    provider=target_p,
                    default=default_sub_m,
                )
                if not self._is_matching_provider_api_model(sub_api_model, target_p):
                    sub_api_model = default_sub_m
                out = _call_provider_api(target_p, sub_api_model)
                if out:
                    return out
                if _is_single_stream_web_busy(self.gemini_mgr.browser.provider):
                    return ""
                if not self._get_active_api_key(target_p) and not self._sub_api_no_key_warned:
                    self._sub_api_no_key_warned = True
                    api_lbl = {"gemini": "Gemini", "claude": "Claude", "chatgpt": "OpenAI"}[target_p]
                    cur_short = AI_PROVIDER_SHORT_NAMES[self.gemini_mgr.browser.provider]
                    self.log(f"⚠️ [자막 API 전환] {api_lbl} API 키가 없어 '{sub_api_model}' 대신 로그인된 웹 {cur_short}로 자동 폴백합니다.")
                fb_model = self._get_active_browser_model(self.gemini_mgr.browser.provider)
                return self.gemini_mgr.browser.translate_via_browser_tab(
                    jp_text=jp_text,
                    style_guide=style_guide,
                    context_lines=context_lines,
                    model_code=fb_model,
                    thinking_level=eff_level,
                    timeout_sec=_web_timeout(fb_model, eff_level),
                )

        if eff == "auto_inline":
            b_active = self._get_active_browser_model(provider)
            if backend == "api":
                out = _call_provider_api(provider, self._get_active_api_model(provider))
                if out:
                    return out
            # Claude & ChatGPT web enforce a strict single concurrent stream per account:
            # avoid spawning a 2nd translator tab while main conversation is active.
            if provider in ("claude", "chatgpt") and backend == "browser" and self.companion_running:
                out = _call_provider_api(provider, self._get_active_api_model(provider))
                return out or ""
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
            out = _call_provider_api(provider, self._get_active_api_model(provider))
            if out:
                return out
            if provider in ("claude", "chatgpt") and backend == "browser":
                return ""
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
        p_var = PERSONA_LABEL_TO_CODE.get(
            self._live_persona_variant_lbl,
            str(self.cfg.get("persona_prompt_variant") or "official"),
        )
        jp_coach = bool(self.cfg.get("jp_beginner_lover_mode", False)) and (self._get_reply_language() == "ja")
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
                            if self._subtitle_entries and self._subtitle_entries[-1] is entry:
                                self._mirror_subtitle_to_mini_overlay(entry)

                    self._schedule_ui(_apply)

            return _on_refined

        if kr_future is None and clean_jp:
            kr_future, will_refine = self.subtitle_translator.submit_progressive(
                clean_jp,
                char_data=c,
                engine=sub_engine,
                thinking_level=sub_thinking,
                on_refined=_make_refine_cb(refine_box),
                persona_variant=p_var,
                jp_beginner_coach=jp_coach,
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
            "persona_variant": p_var,
            "jp_beginner_coach": jp_coach,
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

        if entry.char_id == "__system__":
            if show_header:
                hdr = (entry.char_name_kr or "시스템 안내").strip()
                self.subtitle_box.insert(tk.END, f"⚠️ {hdr}\n", "system_speaker")
            sys_txt = (entry.kr_text or entry.jp_text or "").strip()
            if sys_txt:
                self.subtitle_box.insert(tk.END, f" {sys_txt} \n", "system_line")
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
        self._mirror_subtitle_to_mini_overlay(entry)
        if len(self._subtitle_entries) > 250:
            self._subtitle_entries = self._subtitle_entries[-250:]
            self._rerender_subtitles()
            return
        if not hasattr(self, "subtitle_box") or self.subtitle_box is None:
            return
        mode = self.subtitle_mode_var.get()
        if mode not in SUBTITLE_MODES:
            mode = "jp_kr"
        was_placeholder = bool(self._subtitle_has_placeholder)
        try:
            prev_first, prev_last = self.subtitle_box.yview()
            at_bottom = was_placeholder or (prev_last >= 0.96) or (not self._subtitle_user_scrolled_up)
        except Exception:
            prev_first, at_bottom = 0.0, True
        self.subtitle_box.configure(state="normal")
        if self._subtitle_has_placeholder:
            self.subtitle_box.delete("1.0", tk.END)
            self._subtitle_img_refs = []
            self._subtitle_has_placeholder = False
        self._insert_subtitle_entry(entry, prev_cid=prev_cid, has_previous=had_prev, mode=mode)
        if at_bottom:
            self.subtitle_box.see(tk.END)
        else:
            try:
                self.subtitle_box.yview_moveto(prev_first)
            except Exception:
                pass
        self.subtitle_box.configure(state="disabled")

    def _rerender_subtitles(self) -> None:
        if not hasattr(self, "subtitle_box") or self.subtitle_box is None:
            return
        mode = self.subtitle_mode_var.get()
        if mode not in SUBTITLE_MODES:
            mode = "jp_kr"

        was_placeholder = bool(self._subtitle_has_placeholder)
        try:
            prev_first, prev_last = self.subtitle_box.yview()
            at_bottom = was_placeholder or (prev_last >= 0.96) or (not self._subtitle_user_scrolled_up)
        except Exception:
            prev_first, at_bottom = 0.0, True

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
            self._subtitle_user_scrolled_up = False
            btn_j = getattr(self, "btn_sub_jump_bottom", None)
            if btn_j is not None:
                try:
                    btn_j.pack_forget()
                except Exception:
                    pass
            self.subtitle_box.configure(state="disabled")
            return

        self._subtitle_has_placeholder = False
        prev_cid: str | None = None
        for idx, entry in enumerate(self._subtitle_entries):
            self._insert_subtitle_entry(entry, prev_cid=prev_cid, has_previous=(idx > 0), mode=mode)
            prev_cid = entry.char_id

        if at_bottom:
            self.subtitle_box.see(tk.END)
        else:
            try:
                self.subtitle_box.yview_moveto(prev_first)
            except Exception:
                pass
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
        p_var = str(item.get("persona_variant") or self.cfg.get("persona_prompt_variant", "official") or "official")
        jp_coach = bool(item.get("jp_beginner_coach", False))
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
                            persona_variant=p_var,
                            jp_beginner_coach=jp_coach,
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
                            persona_variant=p_var,
                            jp_beginner_coach=jp_coach,
                        )
                        def _mark_ref() -> None:
                            with self._subtitle_lock:
                                if fb_box.get("done"):
                                    return
                                if entry in self._subtitle_entries:
                                    entry.refining = bool(fb_ref)
                        self._schedule_ui(_mark_ref)
                        _hook_future(fb_fut, False)
                        return

                    if is_inline and resolved:
                        self.subtitle_translator.record_dialogue_pair(
                            jp_text,
                            resolved,
                            char_data=char_data,
                            quality=f"llm:auto_inline:{sub_thinking}",
                            persona_variant=p_var,
                            jp_beginner_coach=jp_coach,
                        )

                    def _update_entry() -> None:
                        if entry in self._subtitle_entries:
                            if resolved:
                                entry.kr_text = resolved
                            entry.pending = False
                            self._rerender_subtitles()
                            if self._subtitle_entries and self._subtitle_entries[-1] is entry:
                                self._mirror_subtitle_to_mini_overlay(entry)

                    self._schedule_ui(_update_entry)

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
                    persona_variant=p_var,
                    jp_beginner_coach=jp_coach,
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

        self._schedule_ui(_append_on_ui)

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
        win.title(f"상태 및 진단 로그 — {APP_DISPLAY_NAME}")
        win.configure(bg="#0f172a")
        fit_window_to_screen(win, 820, 500, min_w=540, min_h=340, center_on=self.root)
        self._status_log_win = win

        filter_var = tk.StringVar(value="")
        auto_scroll_var = tk.BooleanVar(value=True)
        count_var = tk.StringVar(value="")
        self._status_log_filter_var = filter_var
        self._status_log_auto_scroll_var = auto_scroll_var
        self._status_log_count_var = count_var

        def _on_close(_event: Any = None) -> None:
            self._status_log_win = None
            self._status_log_box = None
            self._status_log_auto_scroll_var = None
            self._status_log_filter_var = None
            self._status_log_count_var = None
            try:
                win.destroy()
            except Exception:
                pass

        def _copy_all() -> None:
            with self._log_lock:
                txt = "\n".join(self._log_history)
            if self._copy_to_clipboard(txt):
                messagebox.showinfo("복사 완료", "상태 로그 전체를 클립보드에 복사했습니다.", parent=win)
            else:
                messagebox.showwarning("복사 실패", "클립보드가 다른 프로그램에서 사용 중입니다. 잠시 후 다시 시도해 주세요.", parent=win)

        def _open_log_dir() -> None:
            if os.name == "nt":
                log_dir = _get_runtime_log_path().parent
                _open_directory_in_explorer(log_dir, (get_logs_dir(),))

        def _clear_view() -> None:
            box.configure(state="normal")
            box.delete("1.0", tk.END)
            box.configure(state="disabled")
            count_var.set("화면 비움 (파일 로그는 유지됨)")

        # Pinned footer packed FIRST so buttons are NEVER cut off even on compact screens
        ftr = make_pinned_footer(win, bg="#1e293b", border_color="#334155", padx=12, pady=(8, 10))
        tk.Button(
            ftr,
            text="닫기 (Esc)",
            command=_on_close,
            bg="#475569",
            fg="#ffffff",
            activebackground="#64748b",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            ftr,
            text="전체 복사",
            command=_copy_all,
            bg="#1e40af",
            fg="#ffffff",
            activebackground="#1d4ed8",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            ftr,
            text="로그 폴더",
            command=_open_log_dir,
            bg="#334155",
            fg="#e2e8f0",
            activebackground="#475569",
            font=("Malgun Gothic", 9),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            ftr,
            text="화면 비우기",
            command=_clear_view,
            bg="#334155",
            fg="#e2e8f0",
            activebackground="#475569",
            font=("Malgun Gothic", 9),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        tk.Label(
            ftr,
            textvariable=count_var,
            bg="#1e293b",
            fg="#94a3b8",
            font=("Malgun Gothic", 8),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        top = tk.Frame(win, bg="#1e293b", padx=12, pady=8)
        top.pack(fill="x")
        lbl_log_path = tk.Label(
            top,
            text=f"파일 로그: {_get_runtime_log_path()}",
            bg="#1e293b",
            fg="#94a3b8",
            font=("Consolas", 8),
            anchor="w",
            justify="left",
        )
        lbl_log_path.pack(fill="x", pady=(0, 6))
        bind_responsive_wrap(lbl_log_path, pad=28, min_wrap=260, container=top)

        filter_row = tk.Frame(top, bg="#1e293b")
        filter_row.pack(fill="x")
        tk.Checkbutton(
            filter_row,
            text="자동 스크롤",
            variable=auto_scroll_var,
            bg="#1e293b",
            fg="#e2e8f0",
            selectcolor="#0f172a",
            activebackground="#1e293b",
            activeforeground="#ffffff",
            font=("Malgun Gothic", 8),
            cursor="hand2",
        ).pack(side="right", padx=(8, 0))
        tk.Label(filter_row, text="🔍", bg="#1e293b", fg="#94a3b8", font=("Segoe UI Emoji", 9)).pack(side="left", padx=(0, 4))
        ent_filter = tk.Entry(
            filter_row,
            textvariable=filter_var,
            bg="#0f172a",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            relief="flat",
            highlightbackground="#334155",
            highlightthickness=1,
            font=("Malgun Gothic", 9),
        )
        ent_filter.pack(side="left", fill="x", expand=True)
        btn_f_clear = tk.Button(
            filter_row,
            text="✕",
            command=lambda: filter_var.set(""),
            bg="#334155",
            fg="#cbd5e1",
            activebackground="#475569",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=6,
            pady=1,
            cursor="hand2",
        )
        btn_f_clear.pack(side="left", padx=(4, 0))
        _attach_hover_tooltip(btn_f_clear, "로그 필터 지우기")

        box_host = tk.Frame(win, bg="#0f172a")
        box_host.pack(fill="both", expand=True, padx=8, pady=8)
        box = scrolledtext.ScrolledText(box_host, font=("Consolas", 9), bg="#0f172a", fg="#e2e8f0", wrap="word")
        box.pack(fill="both", expand=True)
        attach_scrollable_mousewheel(box, container=box_host, yview_widget=box, units=3, enable_keyboard=False)

        def _render_filtered_logs(*_args: object) -> None:
            if not box.winfo_exists():
                return
            fq = filter_var.get().strip().lower()
            with self._log_lock:
                snapshot = list(self._log_history)
            matched = [ln for ln in snapshot if (not fq or fq in ln.lower())]
            box.configure(state="normal")
            box.delete("1.0", tk.END)
            if matched:
                box.insert("1.0", "\n".join(matched) + "\n")
                if auto_scroll_var.get():
                    box.see(tk.END)
            box.configure(state="disabled")
            if fq:
                count_var.set(f"필터 일치: {len(matched)} / 전체 {len(snapshot)}줄")
            else:
                count_var.set(f"전체 {len(snapshot)}줄 (Ctrl+F 검색)")

        filter_var.trace_add("write", _render_filtered_logs)
        _render_filtered_logs()

        def _focus_log_filter(_event: Any = None) -> str:
            try:
                ent_filter.focus_set()
                ent_filter.selection_range(0, tk.END)
            except Exception:
                pass
            return "break"

        win.bind("<Escape>", _on_close)
        win.bind("<Control-f>", _focus_log_filter)
        win.bind("<Control-F>", _focus_log_filter)
        self._status_log_box = box
        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _active_secret_values(self) -> tuple[str, ...]:
        secrets_out: list[str] = []
        try:
            cfg = getattr(self, "cfg", None)
            if isinstance(cfg, dict):
                for k in ("fish_api_key", "gemini_api_key", "claude_api_key", "openai_api_key", "colab_s2pro_api_key"):
                    v = str(cfg.get(k) or "").strip()
                    if len(v) >= 8:
                        secrets_out.append(v)
                raw_oauth = cfg.get("fish_mcp_oauth_data")
                if raw_oauth:
                    oauth_obj: Any = None
                    if isinstance(raw_oauth, dict):
                        oauth_obj = raw_oauth
                    elif isinstance(raw_oauth, str) and raw_oauth.strip():
                        if len(raw_oauth.strip()) >= 8:
                            secrets_out.append(raw_oauth.strip())
                        try:
                            oauth_obj = json.loads(raw_oauth)
                        except Exception:
                            oauth_obj = None
                    if isinstance(oauth_obj, dict):
                        stack: list[Any] = [oauth_obj]
                        while stack:
                            cur = stack.pop()
                            if isinstance(cur, dict):
                                for ok, ov in cur.items():
                                    if isinstance(ov, (dict, list)):
                                        stack.append(ov)
                                    elif str(ok).lower() in (
                                        "access_token",
                                        "refresh_token",
                                        "client_secret",
                                        "id_token",
                                    ):
                                        sv = str(ov or "").strip()
                                        if len(sv) >= 8 and sv not in secrets_out:
                                            secrets_out.append(sv)
                            elif isinstance(cur, list):
                                stack.extend(cur)
        except Exception:
            pass
        try:
            v2 = str(self._read_tk_var_safe("api_key_var", "") or "").strip()
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
                log_path = _get_runtime_log_path()
                if self._log_dir_ready_path != log_path.parent:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    self._log_dir_ready_path = log_path.parent
                    self._log_dir_ready = True
                if not log_path.is_symlink():
                    with open(log_path, "a", encoding="utf-8") as f:
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
                    f_var = getattr(self, "_status_log_filter_var", None)
                    fq = str(f_var.get() or "").strip().lower() if f_var is not None else ""
                    if fq and fq not in line.lower():
                        return
                    a_var = getattr(self, "_status_log_auto_scroll_var", None)
                    want_auto = bool(a_var.get()) if a_var is not None else True
                    try:
                        at_bot = box.yview()[1] >= 0.96
                    except Exception:
                        at_bot = True
                    box.configure(state="normal")
                    box.insert(tk.END, line + "\n")
                    if want_auto and at_bot:
                        box.see(tk.END)
                    box.configure(state="disabled")
                    c_var = getattr(self, "_status_log_count_var", None)
                    if c_var is not None and not fq:
                        c_var.set(f"전체 {len(self._log_history)}줄 (Ctrl+F 검색)")
            except Exception:
                pass

        self._schedule_ui(_append_to_viewer)

    def _start_http_keepalive_daemon(self) -> None:
        """Keep TCP+TLS socket to Colab S2-Pro or api.fish.audio permanently warm (saves ~200-420ms on first clause!)."""
        def _worker() -> None:
            while not self._closing and not self._stop_keepalive_event.is_set():
                if self._is_colab_s2pro_enabled():
                    if time.time() - self.last_http_activity >= 20.0:
                        try:
                            ok, _rtt = self.colab_s2pro_backend.ping_health()
                            if ok:
                                self.last_http_activity = time.time()
                        except Exception:
                            pass
                    self._stop_keepalive_event.wait(5.0)
                    continue
                api_key = str(self.cfg.get("fish_api_key", "") or "").strip()
                if (
                    api_key
                    and (not bool(self.cfg.get("fish_mcp_primary_mode", False)))
                    and (time.time() - self.last_http_activity >= 18.0)
                ):
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

        th = threading.Thread(target=_worker, daemon=True, name="fish-http-keepalive")
        self._track_startup_bg_thread(th)
        th.start()

    def _preload_whisper_async(self) -> None:
        if getattr(self, "_closing", False):
            return
        with self._whisper_lock:
            self._whisper_load_gen += 1
            my_gen = self._whisper_load_gen
            self.whisper_loading = True

        def _worker() -> None:
            t0 = time.time()
            try:
                _ensure_cuda_dll_paths()
                try:
                    from faster_whisper import WhisperModel
                    from faster_whisper.tokenizer import Tokenizer
                except Exception as imp_exc:
                    self.log(f"[오류] faster-whisper 모듈 로드 실패: {imp_exc}")
                    return

                cuda_count = 0
                try:
                    import ctranslate2

                    cuda_count = int(ctranslate2.get_cuda_device_count() or 0)
                except Exception:
                    cuda_count = 0

                raw_cfg_model = str(self.cfg.get("whisper_model", "auto") or "auto").strip().lower()
                if raw_cfg_model == "auto":
                    whisper_model_name = "large-v3-turbo" if cuda_count > 0 else "small"
                else:
                    whisper_model_name = raw_cfg_model

                if (
                    whisper_model_name not in _ALLOWED_WHISPER_MODELS
                    or "/" in whisper_model_name
                    or "\\" in whisper_model_name
                ):
                    self.log(f"[오류] 허용되지 않은 Whisper 모델 이름: {whisper_model_name!r}")
                    return

                with self._whisper_reload_lock:
                    with self._whisper_lock:
                        if my_gen != self._whisper_load_gen:
                            return
                        if (
                            self.whisper_model is not None
                            and self._whisper_loaded_model == whisper_model_name
                            and (self._whisper_is_cuda or cuda_count == 0)
                        ):
                            return

                    self._schedule_ui(
                        lambda m=whisper_model_name: self._set_gpu_status_ui(
                            f"⏳ Whisper ({m}) 로딩 중...", "#fbbf24"
                        )
                    )

                    # Release previous model before loading a new one to stay safely within 4GB VRAM
                    prev_model = None
                    with self._whisper_lock:
                        if my_gen != self._whisper_load_gen:
                            return
                        if self.whisper_model is not None:
                            prev_model = self.whisper_model
                            self.whisper_model = None
                            self.tokenizers = {}
                            self.hint_token_cache.clear()
                    if prev_model is not None:
                        del prev_model
                        gc.collect()

                    attempts: list[tuple[str, str, str]] = []
                    if cuda_count > 0:
                        attempts.append((whisper_model_name, "cuda", "int8_float16"))
                        attempts.append((whisper_model_name, "cuda", "int8_float32"))
                        if whisper_model_name in ("large-v3-turbo", "turbo", "medium"):
                            attempts.append(("small", "cuda", "int8_float16"))
                            attempts.append(("small", "cuda", "int8_float32"))
                    cpu_primary = "small" if whisper_model_name in ("large-v3-turbo", "turbo") else whisper_model_name
                    attempts.append((cpu_primary, "cpu", "int8"))
                    if cpu_primary != "small":
                        attempts.append(("small", "cpu", "int8"))

                    loaded_model: Any = None
                    loaded_tokenizers: dict[str, Any] = {}
                    loaded_name = ""
                    loaded_dev = ""
                    loaded_ct = ""
                    last_exc: Exception | None = None

                    for cand_name, cand_dev, cand_ct in attempts:
                        with self._whisper_lock:
                            if my_gen != self._whisper_load_gen or getattr(self, "_closing", False):
                                return
                        try:
                            # faster-whisper loads CTranslate2 binary model.bin + JSON vocab only (zero remote code execution)
                            cand_model = WhisperModel(cand_name, device=cand_dev, compute_type=cand_ct)
                            cand_toks = {
                                "ja": Tokenizer(cand_model.hf_tokenizer, True, task="transcribe", language="ja"),
                                "ko": Tokenizer(cand_model.hf_tokenizer, True, task="transcribe", language="ko"),
                            }
                            if cand_dev == "cuda":
                                from src.controller.local_mic_stt import _is_128mel_whisper_model

                                use_std_30s_w = _is_128mel_whisper_model(cand_model, cand_name)
                                warmup_buckets = (30,) if use_std_30s_w else (4, 6, 8, 12)
                                prompt_w = cand_model.get_prompt(
                                    cand_toks["ja"],
                                    [],
                                    without_timestamps=(not use_std_30s_w),
                                )
                                for bucket_sec in warmup_buckets:
                                    dummy_wave = np.zeros(WHISPER_SR * bucket_sec, dtype=np.float32)
                                    dummy_feats = cand_model.feature_extractor(
                                        dummy_wave,
                                        chunk_length=bucket_sec,
                                    )
                                    n_f = bucket_sec * 100
                                    if dummy_feats.shape[-1] < n_f:
                                        pad_val = float(np.min(dummy_feats)) if dummy_feats.size else -1.0
                                        dummy_feats = np.pad(
                                            dummy_feats,
                                            ((0, 0), (0, n_f - dummy_feats.shape[-1])),
                                            mode="constant",
                                            constant_values=pad_val,
                                        )
                                    else:
                                        dummy_feats = dummy_feats[:, :n_f]
                                    enc_w = cand_model.encode(dummy_feats)
                                    if use_std_30s_w or bucket_sec == 8:
                                        cand_model.model.generate(enc_w, [prompt_w], beam_size=3, max_length=64)
                            loaded_model = cand_model
                            loaded_tokenizers = cand_toks
                            loaded_name = cand_name
                            loaded_dev = cand_dev
                            loaded_ct = cand_ct
                            break
                        except Exception as exc:
                            last_exc = exc
                            self.log(f"[Whisper 폴백] {cand_name} ({cand_dev}/{cand_ct}) 로드 실패: {exc}")
                            gc.collect()

                    if loaded_model is None:
                        self.log(f"[오류] Whisper 초기화 실패: {last_exc}")
                        self._schedule_ui(
                            lambda: self._set_gpu_status_ui("❌ Whisper 모델 로드 실패", "#ef4444")
                        )
                        return

                    with self._whisper_lock:
                        if my_gen != self._whisper_load_gen:
                            del loaded_model
                            gc.collect()
                            return
                        self.whisper_model = loaded_model
                        self.tokenizers = loaded_tokenizers
                        self.hint_token_cache.clear()
                        self._whisper_is_cuda = (loaded_dev == "cuda")
                        self._whisper_loaded_model = loaded_name
                        self._whisper_compute_type = loaded_ct

                    eff_beam = 3 if loaded_dev == "cuda" else 1
                    try:
                        self.mic_stt.configure(beam_size=eff_beam)
                    except TypeError:
                        pass
                    self._schedule_ui(self._sync_mic_stt_config)

                    dt = time.time() - t0
                    if loaded_dev == "cuda":
                        self._schedule_ui(
                            lambda d=dt, m=loaded_name: self._set_gpu_status_ui(
                                f"⚡ Whisper CUDA ({m}) 준비 완료 ({d:.1f}s)", "#4ade80"
                            )
                        )
                        self.log(
                            f"[GPU 준비 완료] Whisper CUDA '{loaded_name}' ({loaded_ct}, beam={eff_beam}) 워밍업 완료 ({dt:.1f}초)."
                        )
                    else:
                        self._schedule_ui(
                            lambda d=dt, m=loaded_name: self._set_gpu_status_ui(
                                f"⚪ Whisper CPU ({m}) 준비 완료 ({d:.1f}s)", "#fbbf24"
                            )
                        )
                        self.log(
                            f"[CPU 준비 완료] Whisper CPU '{loaded_name}' ({loaded_ct}, beam={eff_beam}) 로드 완료 ({dt:.1f}초)."
                        )
            finally:
                with self._whisper_lock:
                    if my_gen == self._whisper_load_gen:
                        self.whisper_loading = False

        th = threading.Thread(target=_worker, daemon=True, name="whisper-preload")
        self._track_startup_bg_thread(th)
        th.start()

    def abort_current_playback(self) -> None:
        with self._playback_lock:
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
                self._schedule_ui(lambda m=safe_exc: messagebox.showwarning("스팀 음성 팩 안내", m))

        threading.Thread(target=_extract_worker, daemon=True).start()

    def copy_jp_prompt(self) -> None:
        c = self.current_char
        vcode = PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")
        is_ko = self._get_reply_language() == "ko"
        prompt, vlabel = resolve_voice_persona_prompt(c, vcode, reply_lang=("ko" if is_ko else "ja"))
        if not prompt:
            messagebox.showwarning("프롬프트 없음", "선택한 캐릭터의 통화용 프롬프트가 비어 있습니다.")
            return
        lead_applied = bool(self.proactive_prompt_var.get())
        if lead_applied:
            prompt = _call_build_proactive_lead_prompt(c, prompt, reply_lang=("ko" if is_ko else "ja"))
        coach_applied = bool(self.jp_beginner_coach_var.get()) and (not is_ko)
        if coach_applied:
            prompt = build_jp_beginner_lover_coach_prompt(c, prompt)
        elif is_ko:
            ko_guide = build_korean_voice_acting_guide(c, persona_variant=vcode, include_samples=True)
            prompt = f"{prompt}\n\n{ko_guide}\n\n{KOREAN_VOICE_OUTPUT_LOCK}"
        if not self._copy_to_clipboard(prompt):
            messagebox.showwarning("복사 실패", "클립보드가 다른 프로그램에서 사용 중입니다. 잠시 후 다시 시도해 주세요.")
            return
        self.save_settings_ui(silent=True)
        lead_tag = " + [선제 질문·대화 리드 규칙]" if lead_applied else ""
        coach_tag = " + [🌸 서툰 일본어 눈높이 배려]" if coach_applied else (" + [🇰🇷 한국어 답변]" if is_ko else "")
        self.log(f"[프롬프트 복사] '{c.get('name_kr')}' ({c.get('name_jp')}) — {vlabel}{lead_tag}{coach_tag} 복사 완료!")
        lang_desc = "한국어 100%" if is_ko else "일본어 100%"
        messagebox.showinfo(
            f"통화용 {'한국어' if is_ko else '일본어'} 프롬프트 복사 완료",
            f"[{c.get('name_kr')} ({c.get('name_jp')})]\n적용 버전: {vlabel}{lead_tag}{coach_tag}\n\n{lang_desc} 실시간 AI 음성통화 전용 프롬프트가 클립보드에 복사되었습니다!\n(공식 원작 해석 프롬프트1과 2차창작 프롬프트2는 완전히 분리되어 보존됩니다.)",
        )

    def copy_rp_chat_prompt(self) -> None:
        c = self.current_char
        persona = c.get("persona") or {}
        prompt = persona.get("japanese_rp_chat_prompt") or persona.get("gemini_live_voice_prompt", "")
        if not prompt:
            messagebox.showwarning("프롬프트 없음", "선택한 캐릭터의 RP 프롬프트가 비어 있습니다.")
            return
        if not self._copy_to_clipboard(prompt):
            messagebox.showwarning("복사 실패", "클립보드가 다른 프로그램에서 사용 중입니다. 잠시 후 다시 시도해 주세요.")
            return
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
        win.configure(bg="#ffffff")
        fit_window_to_screen(win, 920, 720, min_w=600, min_h=440, center_on=self.root)

        # Pinned bottom footer packed FIRST so Close / Copy All stay visible on compact screens
        ftr = make_pinned_footer(win, bg="#f8fafc", border_color="#e2e8f0", padx=12, pady=(8, 10))
        tk.Label(
            ftr,
            text="Ctrl+F 본문 검색 · Esc 창 닫기",
            bg="#f8fafc",
            fg="#64748b",
            font=("Malgun Gothic", 8),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        qbar = tk.Frame(win, bg="#f8fafc", padx=12, pady=8, highlightbackground="#e2e8f0", highlightthickness=1)
        qbar.pack(fill="x", padx=12, pady=(10, 0))

        def _quick_copy(vcode: str) -> None:
            is_ko = self._get_reply_language() == "ko"
            p_txt, p_lbl = resolve_voice_persona_prompt(c, vcode, reply_lang=("ko" if is_ko else "ja"))
            if p_txt:
                lead_applied = bool(self.proactive_prompt_var.get())
                if lead_applied:
                    p_txt = _call_build_proactive_lead_prompt(c, p_txt, reply_lang=("ko" if is_ko else "ja"))
                coach_applied = bool(self.jp_beginner_coach_var.get()) and (not is_ko)
                if coach_applied:
                    p_txt = build_jp_beginner_lover_coach_prompt(c, p_txt)
                elif is_ko:
                    ko_guide = build_korean_voice_acting_guide(c, persona_variant=vcode, include_samples=True)
                    p_txt = f"{p_txt}\n\n{ko_guide}\n\n{KOREAN_VOICE_OUTPUT_LOCK}"
                if not self._copy_to_clipboard(p_txt):
                    messagebox.showwarning("복사 실패", "클립보드가 다른 프로그램에서 사용 중입니다. 잠시 후 다시 시도해 주세요.", parent=win)
                    return
                self.persona_variant_var.set(PERSONA_VARIANT_LABELS[vcode])
                self._on_persona_variant_changed()
                lead_tag = " + [선제 질문·대화 리드]" if lead_applied else ""
                coach_tag = " + [🌸 서툰 일본어 눈높이 배려]" if coach_applied else (" + [🇰🇷 한국어 답변]" if is_ko else "")
                self.log(f"[상세창 복사] '{c.get('name_kr')}' — {p_lbl}{lead_tag}{coach_tag} 복사 및 적용 완료!")
                messagebox.showinfo("복사 완료", f"[{c.get('name_kr')}]\n{p_lbl}{lead_tag}{coach_tag}\n클립보드에 복사되었습니다!", parent=win)

        # 2-row compact button layout so all 5 copy buttons stay unclipped even at 600px width
        q_row1 = tk.Frame(qbar, bg="#f8fafc")
        q_row1.pack(fill="x")
        tk.Label(q_row1, text="원클릭 복사:", bg="#f8fafc", fg="#1e293b", font=("Malgun Gothic", 9, "bold")).pack(
            side="left", padx=(0, 6)
        )
        tk.Button(
            q_row1,
            text="📘 프롬프트1 (공식 정사)",
            command=lambda: _quick_copy("official"),
            bg="#dbeafe",
            fg="#1e40af",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        ).pack(side="left", padx=2)
        tk.Button(
            q_row1,
            text="🌟 2-D (최적배합 통합 · 추천)",
            command=lambda: _quick_copy("fanon_optimal_blend"),
            bg="#ccfbf1",
            fg="#115e59",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        ).pack(side="left", padx=2)

        q_row2 = tk.Frame(qbar, bg="#f8fafc")
        q_row2.pack(fill="x", pady=(5, 0))
        tk.Button(
            q_row2,
            text="🎭 2-A (주류·순애드리프트)",
            command=lambda: _quick_copy("fanon_mainstream"),
            bg="#e0e7ff",
            fg="#3730a3",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        ).pack(side="left", padx=(0, 3))
        tk.Button(
            q_row2,
            text="💖 2-B (고습도 순애)",
            command=lambda: _quick_copy("fanon_pure_love"),
            bg="#fce7f3",
            fg="#9d174d",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        ).pack(side="left", padx=3)
        tk.Button(
            q_row2,
            text="🔥 2-C (매운맛 음해·개그)",
            command=lambda: _quick_copy("fanon_spicy_meme"),
            bg="#fef3c7",
            fg="#92400e",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        ).pack(side="left", padx=3)

        # Inline find bar for quick navigation inside long character reports
        find_bar = tk.Frame(win, bg="#ffffff", padx=12, pady=6)
        find_bar.pack(fill="x")
        find_var = tk.StringVar(value="")
        find_status_var = tk.StringVar(value="")
        tk.Label(find_bar, text="🔍 본문 찾기:", bg="#ffffff", fg="#475569", font=("Malgun Gothic", 8, "bold")).pack(
            side="left", padx=(0, 6)
        )
        ent_find = tk.Entry(
            find_bar,
            textvariable=find_var,
            bg="#f8fafc",
            fg="#0f172a",
            relief="flat",
            highlightbackground="#cbd5e1",
            highlightthickness=1,
            font=("Malgun Gothic", 9),
            width=26,
        )
        ent_find.pack(side="left", padx=(0, 6))
        match_ranges: list[tuple[str, str]] = []
        match_idx = [-1]

        txt_host = tk.Frame(win, bg="#ffffff")
        txt_host.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        txt = scrolledtext.ScrolledText(txt_host, font=("Malgun Gothic", 10), wrap="word", padx=14, pady=12)
        txt.pack(fill="both", expand=True)
        txt.tag_configure("find_hit", background="#fef08a", foreground="#0f172a")
        txt.tag_configure("find_cur", background="#fb7390", foreground="#ffffff")
        attach_scrollable_mousewheel(txt, container=txt_host, yview_widget=txt, units=3, enable_keyboard=False)

        def _refresh_find(*_args: object) -> None:
            if not txt.winfo_exists():
                return
            txt.tag_remove("find_hit", "1.0", tk.END)
            txt.tag_remove("find_cur", "1.0", tk.END)
            match_ranges.clear()
            match_idx[0] = -1
            q = find_var.get().strip()
            if not q:
                find_status_var.set("")
                return
            pos = "1.0"
            while True:
                hit = txt.search(q, pos, stopindex=tk.END, nocase=True)
                if not hit:
                    break
                end_pos = f"{hit}+{len(q)}c"
                txt.tag_add("find_hit", hit, end_pos)
                match_ranges.append((hit, end_pos))
                pos = end_pos
            if match_ranges:
                _jump_find(0)
            else:
                find_status_var.set("일치 없음")

        def _jump_find(delta: int) -> None:
            if not match_ranges or not txt.winfo_exists():
                return
            if match_idx[0] < 0:
                match_idx[0] = 0
            else:
                match_idx[0] = (match_idx[0] + delta) % len(match_ranges)
            txt.tag_remove("find_cur", "1.0", tk.END)
            st, en = match_ranges[match_idx[0]]
            txt.tag_add("find_cur", st, en)
            txt.see(st)
            find_status_var.set(f"{match_idx[0] + 1} / {len(match_ranges)}")

        tk.Button(
            find_bar,
            text="▲ 이전",
            command=lambda: _jump_find(-1),
            bg="#f1f5f9",
            fg="#334155",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=2,
            cursor="hand2",
        ).pack(side="left", padx=(0, 3))
        tk.Button(
            find_bar,
            text="▼ 다음",
            command=lambda: _jump_find(1),
            bg="#f1f5f9",
            fg="#334155",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=2,
            cursor="hand2",
        ).pack(side="left", padx=(0, 6))
        tk.Label(find_bar, textvariable=find_status_var, bg="#ffffff", fg="#64748b", font=("Malgun Gothic", 8)).pack(
            side="left"
        )
        find_var.trace_add("write", _refresh_find)
        ent_find.bind("<Return>", lambda _e: (_jump_find(1), "break")[1])

        def _copy_full_report() -> None:
            try:
                pyperclip.copy(txt.get("1.0", tk.END).strip())
                messagebox.showinfo("복사 완료", "페르소나 정밀 분석 리포트 전체를 복사했습니다.", parent=win)
            except Exception:
                pass

        tk.Button(
            ftr,
            text="닫기 (Esc)",
            command=win.destroy,
            bg="#e2e8f0",
            fg="#334155",
            activebackground="#cbd5e1",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            ftr,
            text="📋 전체 리포트 복사",
            command=_copy_full_report,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        ).pack(side="right")

        def _focus_find(_e: Any = None) -> str:
            try:
                ent_find.focus_set()
                ent_find.selection_range(0, tk.END)
            except Exception:
                pass
            return "break"

        win.bind("<Escape>", lambda _e: win.destroy())
        win.bind("<Control-f>", _focus_find)
        win.bind("<Control-F>", _focus_find)

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
            "【4. 프롬프트 1: 공식 원작 해석 실시간 AI 음성통화 프롬프트 (日本語)】",
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
        self.cfg["fish_model_header"] = _safe_cfg_model_header(self.model_hdr_var.get())
        st_lang = STT_LABEL_TO_CODE.get(self.stt_lang_var.get(), "auto")
        self.cfg["stt_language"] = st_lang
        coach_active = bool(self.jp_beginner_coach_var.get()) and self._get_reply_language() == "ja"
        self.cfg["mic_stt_language"] = (
            st_lang if st_lang in ("ja", "ko") else ("ja_beginner_bi" if coach_active else "ko_auto")
        )
        self.cfg["streaming_speed_mode"] = SPEED_LABEL_TO_CODE.get(self.speed_mode_var.get(), "expressive")
        self.cfg["auto_emotion_directing"] = bool(self.auto_emotion_var.get())
        self.cfg["expression_boost_offset"] = _safe_cfg_float(BOOST_LABEL_TO_VAL.get(self.boost_preset_var.get(), 0.0), 0.0, -0.5, 0.5)
        try:
            raw_pause = self.pause_sec_var.get()
        except Exception:
            raw_pause = self.cfg.get("pause_threshold_sec", 0.33)
        try:
            raw_vol = self.vol_var.get()
        except Exception:
            raw_vol = self.cfg.get("output_volume", 1.0)
        self.cfg["pause_threshold_sec"] = _safe_cfg_float(raw_pause, 0.33, 0.1, 3.0)
        self.cfg["output_volume"] = _safe_cfg_float(raw_vol, 1.0, _OUTPUT_VOLUME_MIN, _OUTPUT_VOLUME_MAX)
        self.cfg["proactive_prompt_append"] = bool(self.proactive_prompt_var.get())
        self.cfg["jp_beginner_lover_mode"] = bool(self.jp_beginner_coach_var.get())
        self.cfg["temporary_call_mode"] = bool(self.temporary_call_var.get())
        self.cfg["proactive_enable"] = bool(self.proactive_enable_var.get())
        self.cfg["proactive_timeout_sec"] = float(PROACTIVE_TIMEOUT_VALS.get(self.proactive_timeout_var.get(), 18.0))
        self.cfg["proactive_trigger_volume"] = float(PROACTIVE_VOL_VALS.get(self.proactive_vol_var.get(), 1.0))
        self.cfg["proactive_trigger_device"] = self.trigger_out_choice_var.get()
        self.cfg["primary_output_device"] = self.out_choice_var.get()
        mic_val = self.mic_choice_var.get().strip()
        if mic_val and not mic_val.startswith("(") and not is_virtual_loopback_mic_label(mic_val):
            self.cfg["mic_input_device"] = mic_val
        elif is_virtual_loopback_mic_label(self.cfg.get("mic_input_device", "")):
            self.cfg["mic_input_device"] = ""
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
        p = self._get_ai_provider()
        api_cfg_key = {"gemini": "gemini_api_model", "claude": "claude_api_model", "chatgpt": "openai_api_model"}[p]
        web_cfg_key = {"gemini": "gemini_browser_model", "claude": "claude_browser_model", "chatgpt": "chatgpt_browser_model"}[p]
        backend = str(self.cfg.get("gemini_backend", "browser"))
        comp_sel = self.companion_model_var.get().strip()
        if backend == "api":
            existing_safe = self._get_active_api_model(p)
            extracted = ""
            if comp_sel:
                active_web = self._active_web_models(p)
                web_tokens = {c for c, _ in active_web} | {l for _, l in active_web}
                if comp_sel not in web_tokens:
                    cand = extract_safe_api_model_id(comp_sel, provider=p)
                    if cand and self._is_matching_provider_api_model(cand, p):
                        extracted = cand
            self.cfg[api_cfg_key] = extracted or existing_safe
        elif comp_sel:
            web_lbl_to_code = {lbl: code for code, lbl in self._active_web_models(p)}
            if comp_sel in web_lbl_to_code:
                self.cfg[web_cfg_key] = web_lbl_to_code[comp_sel]
        try:
            if hasattr(self, "mode_notebook"):
                tab_idx = self.mode_notebook.index(self.mode_notebook.select())
                self.cfg["bridge_mode"] = "gemini_live_audio" if tab_idx == 1 else "pc_mic"
        except Exception:
            pass
        self.cfg["call_bridge_mode"] = "mode2" if self.cfg.get("bridge_mode") == "gemini_live_audio" else "mode1"
        self._persist_cfg_locked()
        self.watchdog.configure(
            enabled=self.cfg["proactive_enable"],
            silence_timeout_sec=self.cfg["proactive_timeout_sec"],
        )
        self.companion_sm.configure(
            proactive_enabled=self.cfg["companion_proactive_enable"],
            min_idle_sec=_safe_cfg_float(self.cfg.get("idle_ping_min_sec", 45.0), 45.0, 5.0, 3600.0),
            max_idle_sec=_safe_cfg_float(self.cfg.get("idle_ping_max_sec", 75.0), 75.0, 5.0, 3600.0),
            max_consecutive_pings=_safe_cfg_int(self.cfg.get("afk_max_consecutive_pings", 2), 2, 1, 20),
        )
        self._sync_mic_stt_config()
        if getattr(self, "_ui_boot_complete", False):
            self._render_roster_visible(force_all=True)
        if not silent:
            self.log(f"[설정 저장] API 키, 감정 디렉팅 및 '{self.current_char.get('name_kr')}' 보이스 ID 설정을 저장했습니다.")
        return True

    def paste_api_key_and_save(self) -> None:
        raw = self._paste_from_clipboard().strip()
        if not raw:
            messagebox.showwarning("클립보드 비어있음", "먼저 Fish Audio 개발자 메뉴에서 API Key를 복사해 주세요!")
            return
        self.api_key_var.set(raw)
        self.save_settings_ui(silent=True)
        self.log(f"[API Key 저장 완료] 길이 {len(raw)}자 키가 등록되었습니다!")
        messagebox.showinfo("API Key 등록 완료", "Fish Audio API Key가 입력 및 저장되었습니다!")

    def paste_url_from_clipboard(self) -> None:
        raw = self._paste_from_clipboard().strip()
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
        api_key = self.api_key_var.get().strip() or str(self.cfg.get("fish_api_key", "") or "").strip()
        if not api_key:
            messagebox.showwarning("API Key 필요", "인기 보이스 검색을 위해 먼저 1단계에 Fish Audio API Key를 입력해 주세요!\n(키 발급 전이라면 브라우저에서 마음에 드는 보이스 주소만 복사해 [📋 복사한 URL 바로 적용]을 누르셔도 됩니다.)")
            return

        c = self.current_char
        target_cid = str(c.get("id") or self.current_char_id or "Aru").strip()
        target_rlang = self._get_reply_language()
        default_query = _JP_PAREN_SPLIT_RE.split(c.get("name_jp", "陸八魔アル"))[0].strip()

        dlg = tk.Toplevel(self.root)
        dlg.title(f"Fish Audio 인기 보이스 검색기 — {c.get('name_kr')} ({default_query})")
        dlg.configure(bg="#ffffff")
        fit_window_to_screen(dlg, 740, 500, min_w=520, min_h=360, center_on=self.root)

        # Pinned footer packed FIRST so Apply/Close actions are ALWAYS reachable
        ftr = make_pinned_footer(dlg, bg="#f8fafc", border_color="#e2e8f0", padx=12, pady=(8, 10))

        top = tk.Frame(dlg, bg="#ffffff", padx=12, pady=10)
        top.pack(fill="x")
        q_var = tk.StringVar(value=default_query)
        status_var = tk.StringVar(value="")
        lbl_hint = tk.Label(
            top,
            text=f"검색어 (현재 캐릭터 일본어명 자동 입력됨 — 성/이름만 검색하거나 '{c['id']}', 'ブルーアーカイブ' 등도 가능):",
            bg="#ffffff",
            fg="#334155",
            font=("Malgun Gothic", 9),
            anchor="w",
            justify="left",
        )
        lbl_hint.pack(fill="x")
        bind_responsive_wrap(lbl_hint, pad=28, min_wrap=240, container=top)
        s_row = tk.Frame(top, bg="#ffffff")
        s_row.pack(fill="x", pady=6)
        tk.Label(top, textvariable=status_var, bg="#ffffff", fg="#64748b", font=("Malgun Gothic", 8)).pack(anchor="w")

        tree_wrap = tk.Frame(dlg, bg="#ffffff")
        tree_wrap.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        cols = ("title", "tasks", "id")
        tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=12)
        tree_sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=tree_sb.set)
        tree.heading("title", text="보이스 모델 이름")
        tree.heading("tasks", text="사용 횟수(인기도)")
        tree.heading("id", text="Model ID")
        tree.column("title", width=340, minwidth=160, stretch=True)
        tree.column("tasks", width=110, minwidth=80, anchor="e", stretch=False)
        tree.column("id", width=210, minwidth=140, stretch=False)
        tree_sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        attach_scrollable_mousewheel(tree, container=tree_wrap, yview_widget=tree, units=3, enable_keyboard=False)

        search_seq = [0]

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
            if getattr(self, "_closing", False):
                return
            try:
                if not dlg.winfo_exists():
                    return
            except Exception:
                return
            query_now = q_var.get().strip()
            search_seq[0] += 1
            my_seq = search_seq[0]
            for row in tree.get_children():
                tree.delete(row)
            status_var.set("🔍 Fish Audio 인기 보이스를 검색하는 중...")
            btn_search.configure(state="disabled")

            def _worker() -> None:
                err_kind = ""
                err_msg = ""
                rows_out: list[tuple[str, str, str]] = []
                try:
                    status_code, items, err_preview = _fetch_search_items(query_now)
                    if status_code == 200:
                        if not items and len(query_now) >= 4:
                            short_q = str(c.get("name_jp") or "")[-3:]
                            if short_q:
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
                            tasks = _safe_cfg_int(it.get("task_count"), 0, 0)
                            rows_out.append((title, f"{tasks:,}", mid))
                    else:
                        err_kind = "검색 실패"
                        err_msg = f"HTTP {status_code}: {err_preview}"
                except Exception as exc:
                    err_kind = "오류"
                    err_msg = _redact_secrets(str(exc), extra_secrets=(api_key,))

                def _apply_results() -> None:
                    try:
                        if not dlg.winfo_exists() or my_seq != search_seq[0]:
                            return
                        btn_search.configure(state="normal")
                        if err_kind:
                            status_var.set(f"⚠️ {err_kind}")
                            messagebox.showerror(err_kind, err_msg, parent=dlg)
                            return
                        for row in tree.get_children():
                            tree.delete(row)
                        for vals in rows_out:
                            tree.insert("", tk.END, values=vals)
                        status_var.set(f"검색 완료: {len(rows_out)}개 보이스")
                    except Exception:
                        pass

                self._schedule_ui(_apply_results)

            threading.Thread(target=_worker, daemon=True, name="fish-voice-search").start()

        def _select_item(_event=None) -> None:
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], "values")
            if not vals or len(vals) < 3:
                return
            title, _, mid = vals[0], vals[1], vals[2]
            clean_mid = extract_model_id(str(mid or ""))
            if not clean_mid:
                return
            with self._cfg_persist_lock:
                self._set_saved_voice_id(target_cid, clean_mid, reply_lang=target_rlang)
                if self.current_char_id == target_cid and self._get_reply_language() == target_rlang:
                    self.cfg["fish_reference_id"] = clean_mid
                save_bridge_config(self.cfg)
            if self.current_char_id == target_cid and self._get_reply_language() == target_rlang:
                self.ref_id_var.set(clean_mid)
            if getattr(self, "_momotalk_sort_mode", "default") == "heart" or bool(getattr(self, "_roster_voice_only", False)):
                self._refresh_character_filter_list(keep_current=True)
            else:
                self._render_roster_visible(force_all=True)
            self._refresh_momotalk_header_and_cards()
            self.log(f"[인기 보이스 장착] [{c.get('name_kr')}] '{title}' (ID: {clean_mid}) 저장 완료!")
            dlg.destroy()

        btn_search = tk.Button(
            s_row,
            text="🔍 인기순 검색",
            command=_do_search,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=4,
            cursor="hand2",
        )
        btn_search.pack(side="right", padx=(6, 0))
        ent = tk.Entry(s_row, textvariable=q_var, font=("Malgun Gothic", 10))
        ent.pack(side="left", fill="x", expand=True)

        tk.Button(
            ftr,
            text="닫기 (Esc)",
            command=dlg.destroy,
            bg="#e2e8f0",
            fg="#334155",
            activebackground="#cbd5e1",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=5,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            ftr,
            text="✅ 선택 보이스 장착",
            command=_select_item,
            bg="#16a34a",
            fg="#ffffff",
            activebackground="#15803d",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
        ).pack(side="right")
        tk.Label(
            ftr,
            text="팁: 더블클릭 또는 Enter로 바로 장착",
            bg="#f8fafc",
            fg="#64748b",
            font=("Malgun Gothic", 8),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        ent.bind("<Return>", lambda _e: _do_search())
        tree.bind("<Double-1>", _select_item)
        tree.bind("<Return>", _select_item)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        ent.focus_set()
        init_search_aid = [dlg.after(100, _do_search)]

        def _on_search_dlg_destroy(ev: Any) -> None:
            if getattr(ev, "widget", None) is not dlg:
                return
            aid = init_search_aid[0]
            if aid is not None:
                init_search_aid[0] = None
                try:
                    dlg.after_cancel(aid)
                except Exception:
                    pass

        dlg.bind("<Destroy>", _on_search_dlg_destroy, add="+")

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
                with self._cfg_persist_lock:
                    self._set_saved_voice_id(linked_cid, linked_mid, reply_lang=linked_rlang)
                    if linked_cid == self.current_char_id and self._get_reply_language() == linked_rlang:
                        self.cfg["fish_reference_id"] = linked_mid
                if linked_cid == self.current_char_id and self._get_reply_language() == linked_rlang:
                    self.ref_id_var.set(linked_mid)
                if getattr(self, "_momotalk_sort_mode", "default") == "heart" or bool(getattr(self, "_roster_voice_only", False)):
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
                    cfg=self._snapshot_cfg_locked(),
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
                    persist_voice_link_cb=self._persist_batch_voice_link_locked,
                )
            except Exception as exc:
                safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
                res = {"ok": False, "fatal_code": "", "error": f"모델 생성 중 예외 발생: {safe_exc}"}
                self.log(f"[오류] {res['error']}")
            finally:
                with self._voice_gen_lock:
                    self._voice_gen_inflight.pop(inflight_key, None)

            def _finish_on_ui() -> None:
                if getattr(self, "_closing", False):
                    return
                if getattr(self, "_momotalk_sort_mode", "default") == "heart" or bool(getattr(self, "_roster_voice_only", False)):
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
                err_txt = _redact_secrets(
                    str(res.get("error") or "모델 생성 실패"),
                    extra_secrets=self._active_secret_values(),
                )

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
        tdir = _get_trigger_cache_dir()
        with self.trigger_cache_lock:
            if (not self.trigger_wav_cache) or self._trigger_cache_loaded_dir != tdir:
                self.trigger_wav_cache = ensure_trigger_wavs_cached(tdir)
                self._trigger_cache_loaded_dir = tdir
            return self.trigger_wav_cache

    def _preload_trigger_wavs_async(self) -> None:
        if getattr(self, "_closing", False):
            return

        def _worker() -> None:
            if getattr(self, "_closing", False):
                return
            try:
                wavs = self._get_trigger_wavs()
                self.log(f"[선제 발화 준비] 로컬 SAPI 트리거 음성 {len(wavs)}종 캐시 완료 (과금 0원).")
            except Exception as exc:
                self.log(f"[선제 발화 경고] 트리거 음성 초기화 실패: {exc}")

        th = threading.Thread(target=_worker, daemon=True, name="trigger-wav-preload")
        self._track_startup_bg_thread(th)
        th.start()

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
                stream = None
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
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                    last_exc = exc
        raise last_exc if last_exc is not None else RuntimeError(f"출력 장치 열기 실패: {dev_lbl}")

    def _play_trigger_oneshot(self, key: str = "nudge_manual") -> None:
        trig_lbl = str(self._read_tk_var_safe("trigger_out_choice_var", self._live_trigger_out_choice or self.cfg.get("proactive_trigger_device", "")) or "")
        if not trig_lbl:
            self.log("[트리거 안내] 선택된 트리거 스피커 장치가 없습니다.")
            return
        try:
            wavs = self._get_trigger_wavs()
            entry = wavs.get(key) or wavs.get("nudge_manual") or next(iter(wavs.values()), None)
            if entry is None:
                self.log("[트리거 안내] 사용할 수 있는 로컬 트리거 음성이 없습니다.")
                return
            audio, src_sr = entry
            vol_lbl = str(self._read_tk_var_safe("proactive_vol_var", self._live_proactive_vol_lbl) or "")
            vol = _safe_cfg_float(PROACTIVE_VOL_VALS.get(vol_lbl, self.cfg.get("proactive_trigger_volume", 1.0)), 1.0, 0.1, 2.0)
            stream, out_sr, _ = self._open_device_stereo_stream(trig_lbl, preferred_sr=src_sr, blocksize=1024)
            try:
                # 0.60s inaudible ultrasonic pilot (-94dB) wakes NVIDIA HDMI/DP monitor DAC cleanly
                n_warm = max(2, (int(out_sr * 0.60) // 2) * 2)
                warm_1d = np.tile(np.array([2e-5, -2e-5], dtype=np.float32), n_warm // 2)
                stream.write(np.column_stack([warm_1d, warm_1d]))
                sig = np.clip(audio * vol, -0.98, 0.98).astype(np.float32)
                if src_sr > 0 and out_sr > 0 and out_sr != src_sr:
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

    def _resolve_saved_voice_id_with_overrides(
        self,
        cid: str,
        *,
        reply_lang: str,
        overrides: dict[str, Any] | None = None,
        allow_jp_fallback: bool = False,
    ) -> str:
        if isinstance(overrides, dict):
            key_primary = "saved_voices_kr_override" if reply_lang == "ko" else "saved_voices_override"
            ov_map = overrides.get(key_primary)
            if isinstance(ov_map, dict) and cid in ov_map:
                ov_val = extract_model_id(ov_map.get(cid, ""))
                if ov_val or not allow_jp_fallback:
                    return ov_val
            if allow_jp_fallback and reply_lang == "ko":
                ov_jp = overrides.get("saved_voices_override")
                if isinstance(ov_jp, dict) and cid in ov_jp:
                    ov_jp_val = extract_model_id(ov_jp.get(cid, ""))
                    if ov_jp_val:
                        return ov_jp_val
        return self._get_saved_voice_id(cid, reply_lang=reply_lang, allow_jp_fallback=allow_jp_fallback)

    def test_aru_tts_async(self, *, overrides: dict[str, Any] | None = None) -> None:
        pipeline_active = self._is_audio_pipeline_active()
        if not pipeline_active:
            self.refresh_audio_devices(reinit_portaudio=True, log_info=False)
        if overrides is None:
            self.save_settings_ui(silent=True)
        if not self._has_fish_tts_credentials(overrides=overrides):
            messagebox.showwarning(
                "Fish Audio 인증 필요",
                "먼저 ⚙️ 설정 창에서 Fish Audio API Key를 입력하거나 공식 MCP 웹 크레딧(OAuth)에 로그인해 주세요!",
            )
            return
        c = self.current_char
        persona = c.get("persona") or {}
        if isinstance(overrides, dict) and "reply_language" in overrides:
            rlang = "ko" if str(overrides.get("reply_language") or "").strip().lower() == "ko" else "ja"
        else:
            rlang = self._get_reply_language()
        if rlang == "ko":
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
        if pipeline_active:
            with self._playback_lock:
                cur_gen = self.playback_gen_id
                if not self._barge_in_ducking:
                    self.stop_playback_flag.clear()
            self.tts_queue.put((0, sample_line, time.time(), 0.12, True, None, "", cur_gen, c, "", False, overrides))
        else:
            threading.Thread(
                target=self._synthesize_and_play_oneshot,
                args=(sample_line,),
                kwargs={"overrides": overrides},
                daemon=True,
            ).start()

    def test_alt_voice_tts_async(self, alt_key: str, *, overrides: dict[str, Any] | None = None) -> None:
        """Play a sample line using an alt voice persona (e.g. Kisaki's `kiki` mode) right from Settings."""
        pipeline_active = self._is_audio_pipeline_active()
        if not pipeline_active:
            self.refresh_audio_devices(reinit_portaudio=True, log_info=False)
        if overrides is None:
            self.save_settings_ui(silent=True)
        if not self._has_fish_tts_credentials(overrides=overrides):
            messagebox.showwarning(
                "Fish Audio 인증 필요",
                "먼저 ⚙️ 설정 창에서 Fish Audio API Key를 입력하거나 공식 MCP 웹 크레딧(OAuth)에 로그인해 주세요!",
            )
            return
        c = self.current_char
        alts = c.get("alt_voice_personas") if isinstance(c, dict) else None
        spec = (alts or {}).get(alt_key) if isinstance(alts, dict) else None
        if not isinstance(spec, dict):
            return
        if isinstance(overrides, dict) and "reply_language" in overrides:
            rlang = "ko" if str(overrides.get("reply_language") or "").strip().lower() == "ko" else "ja"
        else:
            rlang = self._get_reply_language()
        if rlang == "ko":
            sample_line = str(spec.get("sample_tts_line_kr") or "안녕하세요, 선생님! 키키예요!")
        else:
            sample_line = str(spec.get("sample_tts_line_jp") or "先生こんにちは！キキです！")
        tagged = f"[VOICE:{alt_key}] {sample_line}"
        if pipeline_active:
            with self._playback_lock:
                cur_gen = self.playback_gen_id
                if not self._barge_in_ducking:
                    self.stop_playback_flag.clear()
            self.tts_queue.put((0, sample_line, time.time(), 0.12, True, None, alt_key, cur_gen, c, "", False, overrides))
        else:
            threading.Thread(
                target=self._synthesize_and_play_oneshot,
                args=(tagged,),
                kwargs={"overrides": overrides},
                daemon=True,
            ).start()

    def _build_fish_request(
        self,
        text: str,
        latency_mode: str = "balanced",
        clause_rms: float = 0.0,
        voice_persona_key: str = "",
        char_override: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[dict[str, str], bytes, str, dict[str, Any]]:
        ov = overrides if isinstance(overrides, dict) else None
        api_key = (
            str(ov.get("fish_api_key", "") or "").strip()
            if (ov is not None and "fish_api_key" in ov)
            else str(self.cfg.get("fish_api_key", "") or "").strip()
        )
        base_char = char_override if isinstance(char_override, dict) else self.current_char
        base_cid = str(base_char.get("id") or self.current_char_id)
        is_current_char = base_cid == self.current_char_id
        if ov is not None and "reply_language" in ov:
            rlang = "ko" if str(ov.get("reply_language") or "").strip().lower() == "ko" else "ja"
            v_lang = "KR" if rlang == "ko" else "JP"
        else:
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
        cfg_ref_raw = (
            ov.get("fish_reference_id", "")
            if (ov is not None and "fish_reference_id" in ov)
            else self.cfg.get("fish_reference_id", "")
        )

        if alt_char is not None:
            ref_id = self._resolve_saved_voice_id_with_overrides(cid, reply_lang=rlang, overrides=ov, allow_jp_fallback=False)
            if not ref_id and rlang == "ko":
                # Prefer local KR Zero-Shot pure alt WAV if available; else fall back to alt JP model
                alt_comb_kr, alt_sdir_kr = self._char_ref_paths(cid, lang="KR")
                has_local_kr = alt_comb_kr.exists() or (alt_sdir_kr.exists() and any(alt_sdir_kr.glob("*.wav")))
                if not has_local_kr:
                    alt_jp_id = self._resolve_saved_voice_id_with_overrides(cid, reply_lang="ja", overrides=ov, allow_jp_fallback=False)
                    if alt_jp_id:
                        ref_id = alt_jp_id
        else:
            ref_id = (
                extract_model_id(cfg_ref_raw)
                if is_current_char
                else self._resolve_saved_voice_id_with_overrides(cid, reply_lang=rlang, overrides=ov, allow_jp_fallback=False)
            )
            if not ref_id and rlang == "ko":
                kr_saved = self._resolve_saved_voice_id_with_overrides(cid, reply_lang="ko", overrides=ov, allow_jp_fallback=False)
                if kr_saved:
                    ref_id = kr_saved
                else:
                    jp_fallback = self._resolve_saved_voice_id_with_overrides(cid, reply_lang="ja", overrides=ov, allow_jp_fallback=False)
                    if jp_fallback:
                        ref_id = jp_fallback
                        if cid not in self._kr_fallback_warned:
                            self._kr_fallback_warned.add(cid)
                            self.log(
                                f"💡 [한국어 보이스 안내] '{c.get('name_kr', cid)}' 한국어 전용 모델이 아직 없어 기존 일본어 모델로 한국어를 발화합니다. "
                                "한국어 성우 더빙 원음을 쓰려면 [⚙️ 설정 → 1단계 보이스 자동 생성]을 눌러주세요!"
                            )
        raw_hdr = ov.get("fish_model_header", self.cfg.get("fish_model_header", "s2.1-pro-free")) if ov is not None else self.cfg.get("fish_model_header", "s2.1-pro-free")
        model_hdr = _safe_cfg_model_header(raw_hdr)
        auto_dir = bool(ov["auto_emotion_directing"]) if (ov is not None and "auto_emotion_directing" in ov) else bool(self._read_tk_var_safe("auto_emotion_var", self._live_auto_emotion))
        if ov is not None and "expression_boost_offset" in ov:
            boost_val = _safe_cfg_float(ov.get("expression_boost_offset", 0.0), 0.0, -0.5, 0.5)
        else:
            boost_lbl = str(self._read_tk_var_safe("boost_preset_var", self._live_boost_preset_lbl) or "")
            boost_val = _safe_cfg_float(BOOST_LABEL_TO_VAL.get(boost_lbl, self.cfg.get("expression_boost_offset", 0.0)), 0.0, -0.5, 0.5)
        if ov is not None and "persona_variant" in ov:
            p_var = str(ov.get("persona_variant") or "official")
        else:
            p_var_lbl = str(self._read_tk_var_safe("persona_variant_var", self._live_persona_variant_lbl) or "")
            p_var = PERSONA_LABEL_TO_CODE.get(p_var_lbl, str(self.cfg.get("persona_prompt_variant") or "official"))
        credit_saver_enabled = bool(ov["fish_credit_saver_mode"]) if (ov is not None and "fish_credit_saver_mode" in ov) else bool(self.cfg.get("fish_credit_saver_mode", True))
        api_credit_saver = credit_saver_enabled and (str(model_hdr or "").strip().lower() != "s2.1-pro-free")
        use_custom_flag = bool(ov["use_custom_persona"]) if (ov is not None and "use_custom_persona" in ov) else bool(self.cfg.get("use_custom_persona", False))
        jp_coach_flag = bool(ov["jp_beginner_lover_mode"]) if (ov is not None and "jp_beginner_lover_mode" in ov) else bool(self.cfg.get("jp_beginner_lover_mode", False))

        use_custom_non_jp = use_custom_flag and not bool(_KANA_RE.search(clean_input_text))
        if use_custom_non_jp:
            clean_custom = _CUSTOM_MD_STRIP_RE.sub("", clean_input_text).strip()
            dir_info = {
                "clean_text": clean_custom,
                "tts_text": clean_custom,
                "s2_tag": "",
                "cue": "custom_persona",
                "archetype": "",
                "forbidden_s2_tags": [],
                "persona_variant": "custom",
                "speed": 1.00,
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
                jp_beginner_coach=jp_coach_flag and (rlang == "ja"),
                credit_saver=api_credit_saver,
            )
        dir_info["voice_persona_key"] = eff_vkey if alt_char is not None else ""
        dir_info["voice_persona_label"] = voice_persona_label
        dir_info["voice_slot_id"] = cid
        dir_info["reference_id"] = ref_id
        dir_info["reply_lang"] = rlang
        dir_info["model_header"] = model_hdr
        dir_info.setdefault("persona_variant", p_var)
        dir_info.setdefault("speed", 1.00)
        if credit_saver_enabled:
            dir_info["mcp_tts_text"] = apply_credit_saver_to_tts_text(
                dir_info["tts_text"],
                cue=str(dir_info.get("cue") or ""),
                archetype=str(dir_info.get("archetype") or ""),
                forbidden=set(dir_info.get("forbidden_s2_tags") or ()),
            )
        else:
            dir_info["mcp_tts_text"] = dir_info["tts_text"]
        tts_text = dir_info["tts_text"]
        tts_speed = round(max(0.85, min(1.18, float(dir_info.get("speed", 1.00)))), 2)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "model": model_hdr,
        }
        chunk_len = 250 if latency_mode == "normal" else 200
        if ref_id:
            headers["Content-Type"] = "application/json"
            payload_obj: dict[str, Any] = {
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
            }
            if abs(tts_speed - 1.0) >= 0.005:
                payload_obj["prosody"] = {"speed": tts_speed, "volume": 0.0}
            payload_bytes = json.dumps(
                payload_obj,
                ensure_ascii=False,
            ).encode("utf-8")
            mode_tag = f"ID:{ref_id[:8]}"
        else:
            comb_wav, samples_dir = self._char_ref_paths(cid, lang=v_lang)
            sample_wavs = sorted(samples_dir.glob("*.wav")) if samples_dir.exists() else []
            if not sample_wavs and not comb_wav.exists():
                ov_steam = str(ov.get("steam_game_dir", "") or "").strip() if ov is not None else ""
                eff_gdir = Path(ov_steam) if ov_steam else self._get_resolved_ba_game_dir()
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
                base_ref_id = extract_model_id(cfg_ref_raw) or self._resolve_saved_voice_id_with_overrides(
                    base_cid, reply_lang=rlang, overrides=ov, allow_jp_fallback=True
                )
                if base_ref_id:
                    dir_info["reference_id"] = base_ref_id
                    headers["Content-Type"] = "application/json"
                    fb_payload: dict[str, Any] = {
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
                    }
                    if abs(tts_speed - 1.0) >= 0.005:
                        fb_payload["prosody"] = {"speed": tts_speed, "volume": 0.0}
                    payload_bytes = json.dumps(
                        fb_payload,
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
                with self._zero_shot_cache_lock:
                    cached_wav = self._zero_shot_wav_cache.get(ref_key)
                if cached_wav is not None and cached_wav[0] == ref_mtime:
                    wav_bytes = cached_wav[1]
                else:
                    wav_bytes = ref_wav.read_bytes()
                    with self._zero_shot_cache_lock:
                        if len(self._zero_shot_wav_cache) >= 16:
                            self._zero_shot_wav_cache.clear()
                        self._zero_shot_wav_cache[ref_key] = (ref_mtime, wav_bytes)
                ref_txt = str(transcript_map.get(ref_wav.name) or transcript_map.get(ref_wav.stem) or "").strip()
                references_payload.append({"audio": wav_bytes, "text": ref_txt})

            if not references_payload:
                raise RuntimeError(f"Zero-Shot 참조 음성 WAV를 찾을 수 없습니다 ({cid}/{v_lang}).")

            headers["Content-Type"] = "application/msgpack"
            zs_payload: dict[str, Any] = {
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
            }
            if abs(tts_speed - 1.0) >= 0.005:
                zs_payload["prosody"] = {"speed": tts_speed, "volume": 0.0}
            payload_bytes = msgpack.packb(
                zs_payload,
                use_bin_type=True,
            )
            mode_tag = f"Zero-Shot({len(references_payload)}clip)"
        return headers, payload_bytes, mode_tag, dir_info

    def _build_mcp_tts_context(
        self,
        text: str,
        clause_rms: float = 0.0,
        voice_persona_key: str = "",
        char_override: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve character directing, Credit-Saver MCP text, and saved public `voice_id` without requiring Steam WAVs."""
        ov = overrides if isinstance(overrides, dict) else None
        base_char = char_override if isinstance(char_override, dict) else self.current_char
        base_cid = str(base_char.get("id") or self.current_char_id)
        is_current_char = base_cid == self.current_char_id
        if ov is not None and "reply_language" in ov:
            rlang = "ko" if str(ov.get("reply_language") or "").strip().lower() == "ko" else "ja"
        else:
            rlang = self._get_reply_language()

        det_vkey, clean_input_text = resolve_sentence_voice_persona(base_char, text)
        eff_vkey = (voice_persona_key or det_vkey or "").strip().lower()
        alt_char = build_alt_voice_char_data(base_char, eff_vkey) if eff_vkey else None
        c = alt_char if alt_char is not None else base_char
        cid = str(c.get("id") or base_cid)
        alt_spec = ((base_char.get("alt_voice_personas") or {}).get(eff_vkey) or {}) if alt_char is not None else {}
        voice_persona_label = str(alt_spec.get("label_kr") or eff_vkey).split("(")[0].strip() if alt_char is not None else ""
        cfg_ref_raw = (
            ov.get("fish_reference_id", "")
            if (ov is not None and "fish_reference_id" in ov)
            else self.cfg.get("fish_reference_id", "")
        )

        if alt_char is not None:
            ref_id = self._resolve_saved_voice_id_with_overrides(cid, reply_lang=rlang, overrides=ov, allow_jp_fallback=(rlang == "ko"))
            if not ref_id:
                ref_id = (extract_model_id(cfg_ref_raw) if is_current_char else "") or self._resolve_saved_voice_id_with_overrides(
                    base_cid, reply_lang=rlang, overrides=ov, allow_jp_fallback=True
                )
        else:
            ref_id = extract_model_id(cfg_ref_raw) if is_current_char else ""
            if not ref_id:
                ref_id = self._resolve_saved_voice_id_with_overrides(cid, reply_lang=rlang, overrides=ov, allow_jp_fallback=(rlang == "ko"))

        raw_hdr = ov.get("fish_model_header", self.cfg.get("fish_model_header", "s2.1-pro-free")) if ov is not None else self.cfg.get("fish_model_header", "s2.1-pro-free")
        model_hdr = _safe_cfg_model_header(raw_hdr)
        auto_dir = bool(ov["auto_emotion_directing"]) if (ov is not None and "auto_emotion_directing" in ov) else bool(self._read_tk_var_safe("auto_emotion_var", self._live_auto_emotion))
        if ov is not None and "expression_boost_offset" in ov:
            boost_val = _safe_cfg_float(ov.get("expression_boost_offset", 0.0), 0.0, -0.5, 0.5)
        else:
            boost_lbl = str(self._read_tk_var_safe("boost_preset_var", self._live_boost_preset_lbl) or "")
            boost_val = _safe_cfg_float(BOOST_LABEL_TO_VAL.get(boost_lbl, self.cfg.get("expression_boost_offset", 0.0)), 0.0, -0.5, 0.5)
        if ov is not None and "persona_variant" in ov:
            p_var = str(ov.get("persona_variant") or "official")
        else:
            p_var_lbl = str(self._read_tk_var_safe("persona_variant_var", self._live_persona_variant_lbl) or "")
            p_var = PERSONA_LABEL_TO_CODE.get(p_var_lbl, str(self.cfg.get("persona_prompt_variant") or "official"))
        credit_saver_enabled = bool(ov["fish_credit_saver_mode"]) if (ov is not None and "fish_credit_saver_mode" in ov) else bool(self.cfg.get("fish_credit_saver_mode", True))
        use_custom_flag = bool(ov["use_custom_persona"]) if (ov is not None and "use_custom_persona" in ov) else bool(self.cfg.get("use_custom_persona", False))
        jp_coach_flag = bool(ov["jp_beginner_lover_mode"]) if (ov is not None and "jp_beginner_lover_mode" in ov) else bool(self.cfg.get("jp_beginner_lover_mode", False))

        use_custom_non_jp = use_custom_flag and not bool(_KANA_RE.search(clean_input_text))
        if use_custom_non_jp:
            clean_custom = _CUSTOM_MD_STRIP_RE.sub("", clean_input_text).strip()
            dir_info = {
                "clean_text": clean_custom,
                "tts_text": clean_custom,
                "s2_tag": "",
                "cue": "custom_persona",
                "archetype": "",
                "forbidden_s2_tags": [],
                "persona_variant": "custom",
                "speed": 1.00,
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
                jp_beginner_coach=jp_coach_flag and (rlang == "ja"),
                credit_saver=credit_saver_enabled,
            )
        dir_info["voice_persona_key"] = eff_vkey if alt_char is not None else ""
        dir_info["voice_persona_label"] = voice_persona_label
        dir_info["voice_slot_id"] = cid
        dir_info["reference_id"] = ref_id
        dir_info["reply_lang"] = rlang
        dir_info["model_header"] = model_hdr
        dir_info.setdefault("persona_variant", p_var)
        dir_info.setdefault("speed", 1.00)
        dir_info["mcp_tts_text"] = dir_info["tts_text"]
        return dir_info

    def _compute_tts_cache_key(self, dir_info: dict[str, Any], backend_tag: str, tts_text: str) -> str:
        cid = str(dir_info.get("voice_slot_id") or self.current_char_id or "default").strip()
        rlang = str(dir_info.get("reply_lang") or self._get_reply_language()).strip().lower()
        ref_id = str(dir_info.get("reference_id") or "default").strip().lower()
        pvar = str(dir_info.get("persona_variant") or "official").strip().lower()
        spd = float(dir_info.get("speed", 1.00) or 1.00)
        voice_slot = f"{rlang}:{ref_id}:{pvar}:{spd:.2f}"
        return TtsDiskCache.make_key(cid, voice_slot, backend_tag, tts_text)

    def _synthesize_via_mcp_cached(
        self,
        dir_info: dict[str, Any],
        *,
        cancel_check: Any,
        cache_enabled_override: bool | None = None,
    ) -> tuple[np.ndarray, int, bool, int] | None:
        """
        Check local `TtsDiskCache` first, otherwise synthesize via `FishMcpBackend` with Credit-Saver text.
        Returns `(mono_float32, sample_rate, from_cache, utf8_bytes)` or `None` on cancellation/error.
        """
        mcp_text = str(dir_info.get("mcp_tts_text") or dir_info.get("tts_text") or dir_info.get("clean_text") or "").strip()
        if not mcp_text:
            return None
        utf8_bytes = len(mcp_text.encode("utf-8"))
        cache_enabled = (
            bool(cache_enabled_override) and (getattr(self, "tts_cache", None) is not None)
            if cache_enabled_override is not None
            else self._is_tts_cache_enabled()
        )
        cache_key = self._compute_tts_cache_key(dir_info, "fish_mcp", mcp_text)

        if cache_enabled:
            try:
                hit = self.tts_cache.get_pcm(cache_key)
                if hit is not None:
                    return hit[0], hit[1], True, utf8_bytes
            except Exception:
                pass

        if cancel_check is not None and cancel_check():
            return None

        voice_id = str(dir_info.get("reference_id") or "").strip() or None
        rlang = str(dir_info.get("reply_lang") or self._get_reply_language()).strip().lower()
        try:
            pcm_f32, src_sr, raw_audio = self.fish_mcp_backend.synthesize_to_pcm(
                mcp_text,
                voice_id=voice_id,
                language=rlang,
                cancel_check=cancel_check,
            )
        except FishMcpAuthError as exc:
            self._last_mcp_tts_error_msg = (
                "🔑 Fish Audio 공식 MCP 인증이 만료되었거나 필요합니다. ⚙️ 설정 → 1단계에서 다시 로그인하거나 Fish API 키를 입력해 주세요."
            )
            if not self._fish_mcp_auth_warned:
                self._fish_mcp_auth_warned = True
                self.log(
                    f"🔑 [Fish MCP 인증 만료] 공식 MCP OAuth 로그인이 만료되었거나 필요합니다. "
                    f"⚙️ 설정 → 1단계에서 다시 로그인해 주세요. ({exc})"
                )
            else:
                self.log(f"[Fish MCP 인증 오류] {exc}")
            return None
        except FishMcpQuotaError as exc:
            self._last_mcp_tts_error_msg = (
                "💳 Fish Audio MCP 웹 크레딧 한도에 도달했습니다. ⚙️ 설정에서 잔액을 확인하거나 API 키·크레딧 절약 모드를 확인해 주세요."
            )
            if not self._fish_mcp_quota_warned:
                self._fish_mcp_quota_warned = True
                self.log(
                    f"💳 [Fish MCP 웹 크레딧 부족] 무료/패키지 웹 크레딧 한도에 도달했습니다. "
                    f"⚙️ 설정에서 잔액을 확인하거나 로컬 TTS 캐시·크레딧 절약 모드를 활용해 주세요. ({exc})"
                )
            else:
                self.log(f"[Fish MCP 크레딧 한도] {exc}")
            return None
        except FishMcpError as exc:
            self._last_mcp_tts_error_msg = f"⚠️ Fish Audio MCP 음성 합성 오류가 발생했습니다: {_redact_secrets(str(exc), extra_secrets=self._active_secret_values())[:140]}"
            if cancel_check is None or not cancel_check():
                self.log(f"[Fish MCP 합성 오류] {exc}")
            return None

        self._last_mcp_tts_error_msg = ""

        if cancel_check is not None and cancel_check():
            return None

        if cache_enabled:
            try:
                if raw_audio:
                    self.tts_cache.put_audio_bytes(cache_key, raw_audio, ext="auto")
                else:
                    self.tts_cache.put_pcm(cache_key, pcm_f32, src_sr)
            except Exception:
                pass

        return pcm_f32, src_sr, False, utf8_bytes

    def _emit_mono_pcm_to_clause_queue(
        self,
        mono_f32: np.ndarray,
        src_sr: int,
        *,
        my_gen: int,
        play_sr: int,
        vol: float,
        clause_pcm_q: queue.Queue[Any],
        sub_bundle: Any,
        log_line: str,
    ) -> None:
        """Feed `(mono_float32, src_sr)` through StreamingPolyphaseResampler -> 4ms micro-fade -> clip -> stereo -> clause_pcm_q."""
        if (not self._is_audio_pipeline_active()) or my_gen != self.playback_gen_id or self.stop_playback_flag.is_set():
            return
        arr = np.asarray(mono_f32, dtype=np.float32)
        if arr.ndim == 2:
            arr = np.mean(arr, axis=1, dtype=np.float32)
        if arr.size == 0:
            return

        if log_line:
            self.log(log_line)
        if sub_bundle is not None:
            clause_pcm_q.put(sub_bundle)

        need_resample = int(src_sr) != int(play_sr)
        resampler = StreamingPolyphaseResampler(int(src_sr), int(play_sr)) if need_resample else None
        fade_tail_n = max(4, int(play_sr * 0.004))
        tail_hold = np.empty(0, dtype=np.float32)
        first_emitted = True
        scale = np.float32(vol)
        step = 2048

        for offset in range(0, arr.size, step):
            if (not self._is_audio_pipeline_active()) or my_gen != self.playback_gen_id or self.stop_playback_flag.is_set():
                return
            audio_f32 = arr[offset : offset + step].astype(np.float32, copy=True)
            audio_f32 *= scale
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
            audio_f32 = soft_limit_pcm_f32(audio_f32)
            clause_pcm_q.put((int(play_sr), _mono_to_stereo(audio_f32)))

        if self._is_audio_pipeline_active() and my_gen == self.playback_gen_id and not self.stop_playback_flag.is_set():
            tail_f32 = resampler.flush() if resampler is not None else tail_hold
            if tail_f32.size > 0:
                tail_f32 = _apply_micro_fade_1d(
                    tail_f32, play_sr, fade_in=first_emitted, fade_out=True, fade_ms=4.0
                )
                tail_f32 = soft_limit_pcm_f32(tail_f32)
                clause_pcm_q.put((int(play_sr), _mono_to_stereo(tail_f32)))
            clause_pcm_q.put((int(play_sr), np.zeros((int(play_sr * 0.025), 2), dtype=np.float32)))

    def _write_mono_pcm_to_oneshot_stream(
        self,
        mono_f32: np.ndarray,
        src_sr: int,
        *,
        stream: sd.OutputStream,
        play_sr: int,
        vol: float,
        sub_bundle: Any,
        log_line: str,
    ) -> None:
        """Play `(mono_float32, src_sr)` through StreamingPolyphaseResampler -> 4ms micro-fade -> clip -> stereo OutputStream."""
        if self.stop_playback_flag.is_set():
            return
        arr = np.asarray(mono_f32, dtype=np.float32)
        if arr.ndim == 2:
            arr = np.mean(arr, axis=1, dtype=np.float32)
        if arr.size == 0:
            return

        if log_line:
            self.log(log_line)
        if sub_bundle is not None:
            self._schedule_subtitle_from_playback(sub_bundle)

        need_resample = int(src_sr) != int(play_sr)
        resampler = StreamingPolyphaseResampler(int(src_sr), int(play_sr)) if need_resample else None
        fade_tail_n = max(4, int(play_sr * 0.004))
        tail_hold = np.empty(0, dtype=np.float32)
        first_emitted = True
        scale = np.float32(vol)
        step = 2048

        for offset in range(0, arr.size, step):
            if self.stop_playback_flag.is_set():
                return
            audio_f32 = arr[offset : offset + step].astype(np.float32, copy=True)
            audio_f32 *= scale
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
            audio_f32 = soft_limit_pcm_f32(audio_f32)
            stream.write(_mono_to_stereo(audio_f32))

        if not self.stop_playback_flag.is_set():
            tail_f32 = resampler.flush() if resampler is not None else tail_hold
            if tail_f32.size > 0:
                tail_f32 = _apply_micro_fade_1d(
                    tail_f32, play_sr, fade_in=first_emitted, fade_out=True, fade_ms=4.0
                )
                tail_f32 = soft_limit_pcm_f32(tail_f32)
                stream.write(_mono_to_stereo(tail_f32))
            stream.write(np.zeros((int(play_sr * 0.12), 2), dtype=np.float32))

    def _open_stereo_output_stream(self, *, out_lbl_override: str | None = None) -> tuple[sd.OutputStream, int, bool]:
        out_lbl = str(out_lbl_override or "").strip() or str(self._read_tk_var_safe("out_choice_var", self._live_out_choice) or "")
        return self._open_device_stereo_stream(out_lbl, preferred_sr=FISH_PCM_SR, blocksize=512)

    def _build_colab_s2pro_tts_context(
        self,
        text: str,
        clause_rms: float = 0.0,
        voice_persona_key: str = "",
        char_override: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve rich S2-Pro emotion directing + character studio reference slot (credit_saver=False since Colab is free/unlimited!)."""
        ov = overrides if isinstance(overrides, dict) else None
        base_char = char_override if isinstance(char_override, dict) else self.current_char
        base_cid = str(base_char.get("id") or self.current_char_id)
        if ov is not None and "reply_language" in ov:
            rlang = "ko" if str(ov.get("reply_language") or "").strip().lower() == "ko" else "ja"
        else:
            rlang = self._get_reply_language()

        det_vkey, clean_input_text = resolve_sentence_voice_persona(base_char, text)
        eff_vkey = (voice_persona_key or det_vkey or "").strip().lower()
        alt_char = build_alt_voice_char_data(base_char, eff_vkey) if eff_vkey else None
        c = alt_char if alt_char is not None else base_char
        cid = str(c.get("id") or base_cid)
        alt_spec = ((base_char.get("alt_voice_personas") or {}).get(eff_vkey) or {}) if alt_char is not None else {}
        voice_persona_label = str(alt_spec.get("label_kr") or eff_vkey).split("(")[0].strip() if alt_char is not None else ""

        auto_dir = bool(ov["auto_emotion_directing"]) if (ov is not None and "auto_emotion_directing" in ov) else bool(self._read_tk_var_safe("auto_emotion_var", self._live_auto_emotion))
        if ov is not None and "expression_boost_offset" in ov:
            boost_val = _safe_cfg_float(ov.get("expression_boost_offset", 0.0), 0.0, -0.5, 0.5)
        else:
            boost_lbl = str(self._read_tk_var_safe("boost_preset_var", self._live_boost_preset_lbl) or "")
            boost_val = _safe_cfg_float(BOOST_LABEL_TO_VAL.get(boost_lbl, self.cfg.get("expression_boost_offset", 0.0)), 0.0, -0.5, 0.5)
        if ov is not None and "persona_variant" in ov:
            p_var = str(ov.get("persona_variant") or "official")
        else:
            p_var_lbl = str(self._read_tk_var_safe("persona_variant_var", self._live_persona_variant_lbl) or "")
            p_var = PERSONA_LABEL_TO_CODE.get(p_var_lbl, str(self.cfg.get("persona_prompt_variant") or "official"))
        use_custom_flag = bool(ov["use_custom_persona"]) if (ov is not None and "use_custom_persona" in ov) else bool(self.cfg.get("use_custom_persona", False))
        jp_coach_flag = bool(ov["jp_beginner_lover_mode"]) if (ov is not None and "jp_beginner_lover_mode" in ov) else bool(self.cfg.get("jp_beginner_lover_mode", False))
        use_custom_non_jp = use_custom_flag and not bool(_KANA_RE.search(clean_input_text))

        if use_custom_non_jp:
            clean_custom = _CUSTOM_MD_STRIP_RE.sub("", clean_input_text).strip()
            dir_info: dict[str, Any] = {
                "clean_text": clean_custom,
                "tts_text": clean_custom,
                "s2_tag": "",
                "cue": "custom_persona",
                "archetype": "",
                "forbidden_s2_tags": [],
                "persona_variant": "custom",
                "speed": 1.00,
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
                model_hdr="s2-pro",
                persona_variant=p_var,
                jp_beginner_coach=jp_coach_flag and (rlang == "ja"),
                credit_saver=False,
            )
        # Sanitize tags & sampling params strictly for open-source S2-Pro weights
        # (prevents Japanese prose panic tags / [excited] / temp>=0.85 from causing cracked shrieks)
        safe_tts, safe_tag, safe_temp, safe_top_p, safe_rep_pen = sanitize_s2pro_open_weights_directing(
            str(dir_info.get("tts_text") or dir_info.get("clean_text") or ""),
            cue=str(dir_info.get("cue") or ""),
            auto_direct=auto_dir and (not use_custom_non_jp),
            temperature=float(dir_info.get("temperature", 0.72)),
            top_p=float(dir_info.get("top_p", 0.78)),
            repetition_penalty=float(dir_info.get("repetition_penalty", 1.12)),
            forbidden_tags=dir_info.get("forbidden_s2_tags") or (),
        )
        dir_info["tts_text"] = safe_tts
        dir_info["s2_tag"] = safe_tag
        dir_info["temperature"] = safe_temp
        dir_info["top_p"] = safe_top_p
        dir_info["repetition_penalty"] = safe_rep_pen

        dir_info["voice_persona_key"] = eff_vkey if alt_char is not None else ""
        dir_info["voice_persona_label"] = voice_persona_label
        dir_info["voice_slot_id"] = cid
        dir_info["char_obj"] = c
        dir_info["reference_id"] = f"ba_{cid}_{'KR' if rlang == 'ko' else 'JP'}"
        dir_info["reply_lang"] = rlang
        dir_info["model_header"] = "colab-s2-pro"
        dir_info.setdefault("persona_variant", p_var)
        dir_info.setdefault("speed", 1.00)
        return dir_info

    def _stream_colab_s2pro_adaptive(
        self,
        *,
        tts_text: str,
        s2_dir: dict[str, Any],
        speed_code: str,
        play_sr: int,
        vol: float,
        cache_enabled: bool,
        s2_cache_key: str,
        cancel_check: Any,
        on_playback_start: Any,
        emit_stereo: Any,
        trailing_silence_sec: float,
        backend_override: ColabS2ProBackend | None = None,
    ) -> bool:
        """
        Stream Colab S2-Pro PCM16 with mathematical minimum-cushion adaptive pre-buffering.
        Buffers only until `buffered_audio_sec >= cushion_sec` OR stream EOF (whichever comes
        first), then seamlessly streams remaining chunks through the same polyphase resampler
        & 4ms micro-fade state (0% mid-sentence cutouts, 0 phase seams, ~50ms barge-in abort,
        and full `tts_cache` saving). Returns True if any audio chunk was received.
        """
        s2_backend = backend_override if backend_override is not None else self.colab_s2pro_backend
        cushion_sec = s2_backend.compute_prebuffer_cushion_sec(
            tts_text,
            speed=float(s2_dir.get("speed", 1.0) or 1.0),
        )
        s2_chunk_len, s2_max_tokens = resolve_s2pro_streaming_params(speed_code, len(tts_text))

        first_chunk = True
        playback_started = False
        first_emitted = True
        leftover = b""
        pcm_scale = np.float32(vol / 32768.0)
        resampler_s2: StreamingPolyphaseResampler | None = None
        active_src_sr = FISH_PCM_SR
        fade_tail_n = max(4, int(play_sr * 0.004))
        tail_hold = np.empty(0, dtype=np.float32)
        prebuf_chunks: list[bytes] = []
        raw_pcm_chunks: list[bytes] = []
        total_pcm_bytes = 0
        cur_burst_bytes = 0
        last_chunk_mono = 0.0
        burst_durs: list[float] = []
        emit_block_sec = 0.0
        t_stream_start = time.perf_counter()

        def _feed_pcm_bytes(chunk_bytes: bytes) -> None:
            nonlocal leftover, tail_hold, first_emitted, emit_block_sec
            if not chunk_bytes:
                return
            # Slice into <=4096B (~46ms @ 44.1kHz) frames so barge-in stops playback promptly
            for off in range(0, len(chunk_bytes), 4096):
                if cancel_check():
                    return
                sl = chunk_bytes[off : off + 4096]
                buf = leftover + sl
                even_len = len(buf) & ~1
                if even_len == 0:
                    leftover = buf
                    continue
                leftover = buf[even_len:]

                audio_f32 = np.frombuffer(buf[:even_len], dtype=np.int16).astype(np.float32)
                audio_f32 *= pcm_scale
                if resampler_s2 is not None:
                    audio_f32 = resampler_s2.process(audio_f32)
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
                audio_f32 = soft_limit_pcm_f32(audio_f32)
                t_e0 = time.perf_counter()
                emit_stereo(_mono_to_stereo(audio_f32))
                emit_block_sec += time.perf_counter() - t_e0

        def _begin_playback(reached_eof: bool) -> None:
            nonlocal playback_started
            if playback_started or cancel_check():
                return
            playback_started = True
            buf_audio_sec = (total_pcm_bytes / 2.0) / float(max(8000, active_src_sr))
            on_playback_start(cushion_sec, buf_audio_sec, reached_eof)
            if prebuf_chunks:
                joined_pre = b"".join(prebuf_chunks)
                prebuf_chunks.clear()
                _feed_pcm_bytes(joined_pre)

        for pcm_chunk, src_sr in s2_backend.stream_tts_pcm(
            tts_text,
            char_id=str(s2_dir["voice_slot_id"]),
            reply_lang=str(s2_dir["reply_lang"]),
            char_data=s2_dir.get("char_obj"),
            temperature=float(s2_dir.get("temperature", 0.70)),
            top_p=float(s2_dir.get("top_p", 0.76)),
            repetition_penalty=float(s2_dir.get("repetition_penalty", 1.12)),
            chunk_length=s2_chunk_len,
            max_new_tokens=s2_max_tokens,
            cancel_check=cancel_check,
        ):
            if cancel_check():
                break
            now_mono = time.perf_counter()
            if first_chunk:
                first_chunk = False
                if backend_override is None and self._colab_s2pro_offline_warned:
                    self._colab_s2pro_offline_warned = False
                    self._schedule_ui(self._refresh_companion_badges)
                self.last_http_activity = time.time()
                active_src_sr = int(src_sr or FISH_PCM_SR)
                if active_src_sr != int(play_sr):
                    resampler_s2 = StreamingPolyphaseResampler(active_src_sr, play_sr)
            elif last_chunk_mono > 0.0 and (now_mono - last_chunk_mono) >= 0.12 and cur_burst_bytes > 0:
                burst_durs.append((cur_burst_bytes / 2.0) / float(max(8000, active_src_sr)))
                cur_burst_bytes = 0

            cur_burst_bytes += len(pcm_chunk)
            total_pcm_bytes += len(pcm_chunk)
            if cache_enabled:
                raw_pcm_chunks.append(pcm_chunk)

            if not playback_started:
                prebuf_chunks.append(pcm_chunk)
                buffered_audio_sec = (total_pcm_bytes / 2.0) / float(max(8000, active_src_sr))
                if cushion_sec <= 0.0 or buffered_audio_sec >= cushion_sec:
                    _begin_playback(reached_eof=False)
            else:
                _feed_pcm_bytes(pcm_chunk)
            # Refresh after emit so any synchronous blocking sink (`stream.write`) never fakes a server gap
            last_chunk_mono = time.perf_counter()

        if first_chunk:
            return False

        if not cancel_check():
            # Skip RTF/burst update when a synchronous blocking sink (oneshot `stream.write`) throttled
            # the HTTP loop, so 1.0x real-time playback pacing never skews fast-GPU RTF or burst estimates.
            if emit_block_sec <= 0.20:
                t_req_start = s2_backend.get_last_tts_request_start()
                eff_start = t_req_start if t_req_start >= t_stream_start else t_stream_start
                wall_sec = time.perf_counter() - eff_start
                audio_dur_sec = (total_pcm_bytes / 2.0) / float(max(8000, active_src_sr))
                obs_burst = min(3.5, max(burst_durs)) if burst_durs else None
                s2_backend.record_stream_observation(
                    audio_dur_sec,
                    wall_sec,
                    burst_audio_sec=obs_burst,
                )
            if not playback_started:
                _begin_playback(reached_eof=True)
            if not cancel_check():
                tail_f32 = resampler_s2.flush() if resampler_s2 is not None else tail_hold
                if tail_f32.size > 0:
                    tail_f32 = _apply_micro_fade_1d(
                        tail_f32, play_sr, fade_in=first_emitted, fade_out=True, fade_ms=4.0
                    )
                    tail_f32 = soft_limit_pcm_f32(tail_f32)
                    emit_stereo(_mono_to_stereo(tail_f32))
                if trailing_silence_sec > 0.0:
                    emit_stereo(np.zeros((int(play_sr * trailing_silence_sec), 2), dtype=np.float32))
            if cache_enabled and raw_pcm_chunks and not cancel_check():
                try:
                    joined = b"".join(raw_pcm_chunks)
                    even_total = len(joined) & ~1
                    if even_total >= 2:
                        unscaled = np.frombuffer(joined[:even_total], dtype=np.int16).astype(np.float32) * np.float32(
                            1.0 / 32768.0
                        )
                        self.tts_cache.put_pcm(s2_cache_key, unscaled, active_src_sr)
                except Exception:
                    pass
        return True

    def _synthesize_and_play_oneshot(self, text: str, *, overrides: dict[str, Any] | None = None) -> None:
        self.stop_playback_flag.set()
        with self._oneshot_tts_lock:
            self._synthesize_and_play_oneshot_locked(text, overrides=overrides)

    def _synthesize_and_play_oneshot_locked(self, text: str, *, overrides: dict[str, Any] | None = None) -> None:
        self.stop_playback_flag.clear()
        ov = overrides if isinstance(overrides, dict) else None
        t_start = time.time()
        c = self.current_char
        cname = c.get("name_kr", self.current_char_id)
        stream: sd.OutputStream | None = None
        ephemeral_s2_backend: ColabS2ProBackend | None = None
        cancel_fn = self.stop_playback_flag.is_set
        eff_fish_api_key = (
            str(ov.get("fish_api_key", "") or "").strip()
            if (ov is not None and "fish_api_key" in ov)
            else str(self.cfg.get("fish_api_key", "") or "").strip()
        )
        oneshot_sub_bundle: Any = None
        oneshot_sub_built = False

        def _make_oneshot_sub_bundle(clean_jp_txt: str) -> dict[str, Any] | None:
            nonlocal oneshot_sub_bundle, oneshot_sub_built
            if oneshot_sub_built:
                return oneshot_sub_bundle
            src = str(clean_jp_txt or "").strip()
            if not src:
                return None
            oneshot_sub_bundle = self._prepare_subtitle_bundle(src, c, is_turn_start=True)
            oneshot_sub_built = True
            return oneshot_sub_bundle

        try:
            out_override = str(ov.get("primary_output_device", "") or "").strip() if ov is not None else ""
            stream, play_sr, need_resample = self._open_stereo_output_stream(out_lbl_override=out_override or None)
            raw_vol = ov.get("output_volume", self.cfg.get("output_volume", 1.0)) if ov is not None else self.cfg.get("output_volume", 1.0)
            vol = _safe_cfg_float(raw_vol, 1.0, _OUTPUT_VOLUME_MIN, _OUTPUT_VOLUME_MAX)
            cache_enabled = self._is_tts_cache_enabled(overrides=ov)

            # 0) Dedicated Google Colab Fish Audio S2-Pro Server path (Option B: Free / Unlimited / Zero Congestion)
            if self._is_colab_s2pro_enabled(overrides=ov):
                ephemeral_s2_backend = self._make_ephemeral_colab_backend(ov)
                s2_dir = self._build_colab_s2pro_tts_context(text, clause_rms=0.12, overrides=ov)
                clean_jp = str(s2_dir.get("clean_text") or "").strip()
                tts_text = str(s2_dir.get("tts_text") or clean_jp).strip()
                sub_bundle = _make_oneshot_sub_bundle(clean_jp)
                s2_cache_key = self._compute_tts_cache_key(s2_dir, "colab_s2pro", tts_text)
                if cache_enabled:
                    try:
                        cached_s2 = self.tts_cache.get_pcm(s2_cache_key)
                    except Exception:
                        cached_s2 = None
                    if cached_s2 is not None:
                        ttfa = time.time() - t_start
                        vp_lbl = f" 🎭{s2_dir['voice_persona_label']}" if s2_dir.get("voice_persona_label") else ""
                        log_line = f"🔊 [{cname}{vp_lbl} 발화 (☁️S2-Pro ⚡캐시, {ttfa:.2f}s)] {s2_dir['clean_text']}"
                        self._write_mono_pcm_to_oneshot_stream(
                            cached_s2[0],
                            cached_s2[1],
                            stream=stream,
                            play_sr=play_sr,
                            vol=vol,
                            sub_bundle=sub_bundle,
                            log_line=log_line,
                        )
                        return

                oneshot_started = False
                try:
                    def _on_oneshot_start(cushion_sec: float, buf_sec: float, reached_eof: bool) -> None:
                        nonlocal oneshot_started
                        oneshot_started = True
                        ttfa = time.time() - t_start
                        act_lbl = f"{s2_dir['s2_tag']} " if s2_dir.get("s2_tag") else ""
                        vp_lbl = f" 🎭{s2_dir['voice_persona_label']}" if s2_dir.get("voice_persona_label") else ""
                        if cushion_sec > 0.0:
                            buf_tag = f" | 적응버퍼 {buf_sec:.1f}s/{cushion_sec:.1f}s{'(EOF)' if reached_eof else ''} {ttfa:.2f}s"
                        else:
                            buf_tag = f", {ttfa:.2f}s"
                        self.log(
                            f"🔊 [{cname}{vp_lbl} 발화 (☁️Colab S2-Pro {act_lbl}temp={s2_dir['temperature']:.2f}{buf_tag})] {s2_dir['clean_text']}"
                        )
                        if sub_bundle is not None:
                            self._schedule_subtitle_from_playback(sub_bundle)

                    got_any = self._stream_colab_s2pro_adaptive(
                        tts_text=tts_text,
                        s2_dir=s2_dir,
                        speed_code="full_turn",
                        play_sr=play_sr,
                        vol=vol,
                        cache_enabled=cache_enabled,
                        s2_cache_key=s2_cache_key,
                        cancel_check=cancel_fn,
                        on_playback_start=_on_oneshot_start,
                        emit_stereo=stream.write,
                        trailing_silence_sec=0.12,
                        backend_override=ephemeral_s2_backend,
                    )
                    if got_any or cancel_fn():
                        return
                    if not eff_fish_api_key and not self._is_fish_mcp_authenticated():
                        self.log("☁️ [Colab S2-Pro 빈 오디오] 서버가 유효한 오디오를 반환하지 않았습니다 (대체 Fish 키/MCP 미설정).")
                        if sub_bundle is not None and not oneshot_started:
                            self._schedule_subtitle_from_playback(sub_bundle)
                        return
                except ColabS2ProAuthError as exc:
                    self.log(f"🔑 [Colab S2-Pro 인증 오류] {exc} (⚙️ 설정 -> 1단계에서 인증 토큰 확인)")
                    return
                except (ColabS2ProUnavailableError, ColabS2ProError) as exc:
                    if ephemeral_s2_backend is None and not self._colab_s2pro_offline_warned:
                        self._colab_s2pro_offline_warned = True
                        self._schedule_ui(self._refresh_companion_badges)
                        self.log(f"☁️ [Colab S2-Pro 서버 오류] {exc} -> 기존 Fish API/MCP가 있으면 자동 폴백합니다.")
                    else:
                        self.log(f"[Colab S2-Pro 폴백] {exc}")
                    if oneshot_started or cancel_fn() or (not eff_fish_api_key and not self._is_fish_mcp_authenticated()):
                        return

            # 1) Direct Fish MCP path (when MCP primary mode is ON, or fish_api_key is empty & MCP signed in)
            if self._should_prefer_fish_mcp(overrides=ov):
                mcp_dir = self._build_mcp_tts_context(text, clause_rms=0.12, overrides=ov)
                clean_jp = str(mcp_dir.get("clean_text") or "").strip()
                sub_bundle = _make_oneshot_sub_bundle(clean_jp)
                mcp_res = self._synthesize_via_mcp_cached(
                    mcp_dir, cancel_check=cancel_fn, cache_enabled_override=cache_enabled
                )
                if mcp_res is not None:
                    pcm_f32, src_sr, from_cache, n_bytes = mcp_res
                    ttfa = time.time() - t_start
                    vp_lbl = f" 🎭{mcp_dir['voice_persona_label']}" if mcp_dir.get("voice_persona_label") else ""
                    badge = "⚡ 캐시" if from_cache else f"🌐 MCP {n_bytes}B"
                    log_line = f"🔊 [{cname}{vp_lbl} 발화 ({badge}, {ttfa:.2f}s)] {mcp_dir['clean_text']}"
                    self._write_mono_pcm_to_oneshot_stream(
                        pcm_f32,
                        src_sr,
                        stream=stream,
                        play_sr=play_sr,
                        vol=vol,
                        sub_bundle=sub_bundle,
                        log_line=log_line,
                    )
                    return
                if cancel_fn() or not eff_fish_api_key:
                    return

            # 2) Direct Fish Cloud API path (with local TTS disk cache lookup + HTTP 402/429/404/410 MCP fallback)
            headers, payload_bytes, mode_tag, dir_info = self._build_fish_request(
                text, latency_mode="balanced", clause_rms=0.12, overrides=ov
            )
            clean_jp = str(dir_info.get("clean_text") or "").strip()
            sub_bundle = _make_oneshot_sub_bundle(clean_jp)
            api_cache_key = self._compute_tts_cache_key(
                dir_info,
                f"fish_api:{dir_info.get('model_header', 's2.1-pro-free')}",
                str(dir_info.get("tts_text") or ""),
            )
            if cache_enabled:
                try:
                    cached_api = self.tts_cache.get_pcm(api_cache_key)
                except Exception:
                    cached_api = None
                if cached_api is not None:
                    ttfa = time.time() - t_start
                    vp_lbl = f" 🎭{dir_info['voice_persona_label']}" if dir_info.get("voice_persona_label") else ""
                    log_line = f"🔊 [{cname}{vp_lbl} 발화 (⚡ 캐시 0B, {ttfa:.2f}s, {mode_tag})] {dir_info['clean_text']}"
                    self._write_mono_pcm_to_oneshot_stream(
                        cached_api[0],
                        cached_api[1],
                        stream=stream,
                        play_sr=play_sr,
                        vol=vol,
                        sub_bundle=sub_bundle,
                        log_line=log_line,
                    )
                    return

            first_chunk = True
            first_emitted = True
            leftover = b""
            pcm_scale = np.float32(vol / 32768.0)
            resampler = StreamingPolyphaseResampler(FISH_PCM_SR, play_sr) if need_resample else None
            fade_tail_n = max(4, int(play_sr * 0.004))
            tail_hold = np.empty(0, dtype=np.float32)

            tts_url = _validate_fish_api_url(FISH_TTS_URL)
            total_pcm_bytes = 0
            hit_stream_limit = False
            fallback_status: int | None = None
            raw_pcm_chunks: list[bytes] = []

            with self.http_client.stream("POST", tts_url, headers=headers, content=payload_bytes) as resp:
                self.last_http_activity = time.time()
                if resp.status_code != 200:
                    err_body = _read_bounded_httpx_error(resp, MAX_HTTP_ERROR_BYTES)
                    self.log(f"[Fish TTS 오류 HTTP {resp.status_code}] {err_body[:250]}")
                    if resp.status_code in FISH_MCP_FALLBACK_HTTP_STATUSES and self._can_fallback_to_fish_mcp(overrides=ov):
                        fallback_status = int(resp.status_code)
                    else:
                        return
                else:
                    for chunk in resp.iter_bytes(chunk_size=2048):
                        if self.stop_playback_flag.is_set():
                            break
                        if not chunk:
                            continue
                        total_pcm_bytes += len(chunk)
                        if total_pcm_bytes > MAX_TTS_STREAM_BYTES:
                            hit_stream_limit = True
                            self.log(f"[Fish TTS 보안 경고] 단일 스트림 크기 한도({MAX_TTS_STREAM_BYTES} bytes) 초과로 중단했습니다.")
                            break
                        if cache_enabled:
                            raw_pcm_chunks.append(chunk)
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

            if fallback_status is not None:
                if not self._fish_mcp_fallback_logged:
                    self._fish_mcp_fallback_logged = True
                    cs_lbl = "ON" if bool(self.cfg.get("fish_credit_saver_mode", True)) else "OFF"
                    self.log(
                        f"🔄 [Fish Audio MCP 자동 전환] Fish API HTTP {fallback_status} 오류로 공식 MCP 웹 크레딧으로 자동 전환합니다! "
                        f"(크레딧 절약 모드: {cs_lbl})"
                    )
                mcp_res = self._synthesize_via_mcp_cached(
                    dir_info, cancel_check=cancel_fn, cache_enabled_override=cache_enabled
                )
                if mcp_res is not None:
                    pcm_f32, src_sr, from_cache, n_bytes = mcp_res
                    ttfa = time.time() - t_start
                    vp_lbl = f" 🎭{dir_info['voice_persona_label']}" if dir_info.get("voice_persona_label") else ""
                    badge = "⚡ 캐시" if from_cache else f"🌐 MCP 전환 {n_bytes}B"
                    log_line = f"🔊 [{cname}{vp_lbl} 발화 ({badge}, {ttfa:.2f}s)] {dir_info['clean_text']}"
                    self._write_mono_pcm_to_oneshot_stream(
                        pcm_f32,
                        src_sr,
                        stream=stream,
                        play_sr=play_sr,
                        vol=vol,
                        sub_bundle=sub_bundle,
                        log_line=log_line,
                    )
                return

            if not self.stop_playback_flag.is_set():
                tail_f32 = resampler.flush() if resampler is not None else tail_hold
                if tail_f32.size > 0:
                    tail_f32 = _apply_micro_fade_1d(
                        tail_f32, play_sr, fade_in=first_emitted, fade_out=True, fade_ms=4.0
                    )
                    np.clip(tail_f32, -0.985, 0.985, out=tail_f32)
                    stream.write(_mono_to_stereo(tail_f32))
                stream.write(np.zeros((int(play_sr * 0.12), 2), dtype=np.float32))
                if cache_enabled and raw_pcm_chunks and (not hit_stream_limit):
                    try:
                        joined = b"".join(raw_pcm_chunks)
                        even_total = len(joined) & ~1
                        if even_total >= 2:
                            unscaled_f32 = np.frombuffer(joined[:even_total], dtype=np.int16).astype(np.float32) * np.float32(1.0 / 32768.0)
                            self.tts_cache.put_pcm(api_cache_key, unscaled_f32, FISH_PCM_SR)
                    except Exception:
                        pass
        except Exception as exc:
            self.log(f"[재생/레퍼런스 오류] {exc}")
        finally:
            if ephemeral_s2_backend is not None:
                try:
                    ephemeral_s2_backend.close()
                except Exception:
                    pass
            self._close_pa_stream(stream)

    def toggle_bridge(self) -> None:
        if self.companion_running:
            self.toggle_companion_mode()
        cname = self.current_char.get("name_kr", self.current_char_id)
        if self.running:
            with self._companion_session_lock:
                self._audio_session_id += 1
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
        if not self._has_fish_tts_credentials():
            messagebox.showwarning(
                "Fish Audio 인증 필요",
                "먼저 ⚙️ 설정 창에서 Fish Audio API Key를 입력하거나 공식 MCP 웹 크레딧(OAuth)에 로그인해 주세요!",
            )
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
            silence_timeout_sec=_safe_cfg_float(PROACTIVE_TIMEOUT_VALS.get(self.proactive_timeout_var.get(), 18.0), 18.0, 5.0, 3600.0),
        )
        self.tts_executor = ThreadPoolExecutor(max_workers=2)

        with self._companion_session_lock:
            self._audio_session_id += 1
            audio_sid = self._audio_session_id
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
        if self._is_colab_s2pro_enabled():
            self._prewarm_colab_s2pro_current_char()

        self._output_stream_ready.clear()
        threading.Thread(target=self._audio_player_loop, kwargs={"audio_sid": audio_sid}, daemon=True).start()
        threading.Thread(target=self._clause_order_drainer_loop, kwargs={"audio_sid": audio_sid}, daemon=True).start()
        threading.Thread(target=self._tts_prefetch_loop, kwargs={"audio_sid": audio_sid}, daemon=True).start()
        threading.Thread(target=self._stt_loop, kwargs={"audio_sid": audio_sid}, daemon=True).start()
        threading.Thread(target=self._capture_loop, kwargs={"audio_sid": audio_sid}, daemon=True).start()
        threading.Thread(target=self._trigger_player_loop, kwargs={"audio_sid": audio_sid}, daemon=True).start()
        threading.Thread(target=self._watchdog_loop, kwargs={"audio_sid": audio_sid}, daemon=True).start()

    def _abort_bridge_on_device_error(self, err_msg: str) -> None:
        self.log(err_msg)
        with self._companion_session_lock:
            self._audio_session_id += 1
            if self.companion_running:
                self._companion_session_id += 1
        self.running = False
        was_companion = bool(self.companion_running)
        if was_companion:
            self.companion_running = False
            self._companion_session_ready = False
            self._companion_cancel_event.set()
            with self._turn_interrupt_lock:
                self._active_turn_interrupt.set()
                self._drain_companion_turn_queue()
            self._companion_turn_wakeup.set()
            self._abort_active_browser_prompt()
            self._barge_in_ducking = False
            self.mic_stt.stop()
            self.companion_sm.stop_session()
            self.gemini_mgr.invalidate_session()
            try:
                self._finalize_active_memory_session(run_summary=not getattr(self, "_closing", False))
            except Exception:
                pass
        self._ringback_state.stop()
        self.abort_current_playback()
        ex = self.tts_executor
        self.tts_executor = None
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
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

        self._schedule_ui(_reset_ui)

    def _audio_player_loop(
        self,
        *,
        audio_sid: int | None = None,
        companion_sid: int | None = None,
    ) -> None:
        """Keep Stereo OutputStream open with live switching & USB-unplug recovery."""
        stream: sd.OutputStream | None = None
        play_sr = FISH_PCM_SR
        idle_zeros = np.zeros((int(play_sr * 0.008), 2), dtype=np.float32)
        active_lbl = ""

        def _session_still_active() -> bool:
            if not self._is_audio_pipeline_active():
                return False
            if audio_sid is not None and audio_sid != self._audio_session_id:
                return False
            if companion_sid is not None and (not self.companion_running or companion_sid != self._companion_session_id):
                return False
            return True

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

        live_rs_key: tuple[int, int, int] | None = None
        live_rs_l: StreamingPolyphaseResampler | None = None
        live_rs_r: StreamingPolyphaseResampler | None = None

        def _reset_live_resampler() -> None:
            nonlocal live_rs_key, live_rs_l, live_rs_r
            live_rs_key = None
            live_rs_l = None
            live_rs_r = None

        def _flush_live_resampler(target_stream: sd.OutputStream | None, cur_sr: int) -> None:
            nonlocal live_rs_key, live_rs_l, live_rs_r
            if live_rs_l is None or live_rs_key is None or target_stream is None:
                _reset_live_resampler()
                return
            _src_sr, out_sr, rs_gen = live_rs_key
            if out_sr != cur_sr or rs_gen != self.playback_gen_id or self.stop_playback_flag.is_set():
                _reset_live_resampler()
                return
            try:
                fl_l = live_rs_l.flush()
                if live_rs_r is not None:
                    fl_r = live_rs_r.flush()
                    n = min(fl_l.size, fl_r.size)
                    if n > 0:
                        tail_st = np.column_stack([soft_limit_pcm_f32(fl_l[:n]), soft_limit_pcm_f32(fl_r[:n])])
                        target_stream.write(tail_st * 0.25 if self._barge_in_ducking else tail_st)
                elif fl_l.size > 0:
                    tail_st = _mono_to_stereo(soft_limit_pcm_f32(fl_l))
                    target_stream.write(tail_st * 0.25 if self._barge_in_ducking else tail_st)
            finally:
                _reset_live_resampler()

        def _open_primary(lbl: str) -> bool:
            nonlocal stream, play_sr, idle_zeros, active_lbl
            _reset_live_resampler()
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
                    self._output_stream_ready.set()
                    active_lbl = ui_lbl
                    if ui_lbl != lbl:
                        self._schedule_ui(lambda v=ui_lbl: self.out_choice_var.set(v))
                    self.log(f"🎧 [캐릭터 음성 출력 연결] '{open_lbl}' @ {play_sr}Hz 준비 완료")
                    return True
                except Exception as exc:
                    last_exc = exc
            self.log(f"[출력 장치 오류] {last_exc or lbl}")
            return False

        if not _session_still_active():
            self._output_stream_ready.set()
            return
        init_out_lbl = str(self._read_tk_var_safe("out_choice_var", self._live_out_choice) or "")
        if not _open_primary(init_out_lbl):
            self._output_stream_ready.set()
            if _session_still_active():
                self._abort_bridge_on_device_error("[출력 장치 오류] 사용 가능한 스피커/이어폰을 열지 못했습니다.")
            return

        consec_write_errs = 0
        try:
            while _session_still_active():
                desired_lbl = str(self._read_tk_var_safe("out_choice_var", self._live_out_choice) or "")
                if desired_lbl and desired_lbl != active_lbl:
                    if not _open_primary(desired_lbl):
                        if _session_still_active():
                            self._abort_bridge_on_device_error(f"[출력 장치 오류] '{desired_lbl}' 전환 실패")
                        break

                try:
                    q_wait = 0.022 if time.monotonic() < self._active_audio_playing_until else 0.008
                    try:
                        gen_id, raw_chunk = self.pcm_play_queue.get(timeout=q_wait)
                        has_item = True
                    except queue.Empty:
                        has_item = False
                        gen_id, raw_chunk = 0, None

                    if not _session_still_active():
                        break

                    if has_item:
                        if gen_id != self.playback_gen_id or self.stop_playback_flag.is_set():
                            _reset_live_resampler()
                            continue
                        if self._call_ui_state == "calling":
                            self._complete_call_connection_from_audio_thread(
                                stream,
                                play_sr,
                                expected_session_id=companion_sid,
                                expected_audio_sid=audio_sid,
                            )
                        if isinstance(raw_chunk, dict):
                            _flush_live_resampler(stream, play_sr)
                            if raw_chunk.get("type") == "subtitle_start":
                                self._schedule_subtitle_from_playback(raw_chunk)
                            continue
                        if (
                            isinstance(raw_chunk, tuple)
                            and len(raw_chunk) == 2
                            and isinstance(raw_chunk[0], int)
                            and isinstance(raw_chunk[1], np.ndarray)
                        ):
                            chunk_sr, stereo_chunk = raw_chunk
                        else:
                            chunk_sr, stereo_chunk = play_sr, raw_chunk

                        if chunk_sr != play_sr and isinstance(stereo_chunk, np.ndarray) and stereo_chunk.size > 0:
                            if live_rs_key != (chunk_sr, play_sr, gen_id):
                                _flush_live_resampler(stream, play_sr)
                                live_rs_key = (chunk_sr, play_sr, gen_id)
                                live_rs_l = StreamingPolyphaseResampler(chunk_sr, play_sr)
                                live_rs_r = None
                            if stereo_chunk.ndim == 2 and stereo_chunk.shape[1] >= 2:
                                if live_rs_r is None and np.array_equal(stereo_chunk[:, 0], stereo_chunk[:, 1]):
                                    out_m = live_rs_l.process(stereo_chunk[:, 0])
                                    if out_m.size == 0:
                                        continue
                                    stereo_chunk = _mono_to_stereo(soft_limit_pcm_f32(out_m))
                                else:
                                    if live_rs_r is None:
                                        live_rs_r = StreamingPolyphaseResampler(chunk_sr, play_sr)
                                    out_l = live_rs_l.process(stereo_chunk[:, 0])
                                    out_r = live_rs_r.process(stereo_chunk[:, 1])
                                    n_rs = min(out_l.size, out_r.size)
                                    if n_rs <= 0:
                                        continue
                                    stereo_chunk = np.column_stack(
                                        [soft_limit_pcm_f32(out_l[:n_rs]), soft_limit_pcm_f32(out_r[:n_rs])]
                                    )
                            else:
                                out_m = live_rs_l.process(stereo_chunk.reshape(-1))
                                if out_m.size == 0:
                                    continue
                                stereo_chunk = _mono_to_stereo(soft_limit_pcm_f32(out_m))
                        elif live_rs_l is not None:
                            _flush_live_resampler(stream, play_sr)

                        now_mono = time.monotonic()
                        chunk_dur = (
                            float(len(stereo_chunk)) / float(play_sr)
                            if (play_sr > 0 and hasattr(stereo_chunk, "__len__"))
                            else 0.0
                        )
                        self._active_audio_playing_until = max(
                            self._active_audio_playing_until,
                            now_mono + max(0.15, chunk_dur + 0.12),
                        )
                        self.watchdog.on_playback_active(now_mono)
                        if stream is not None:
                            stream.write(stereo_chunk * 0.25 if self._barge_in_ducking else stereo_chunk)
                            self._active_audio_playing_until = max(
                                self._active_audio_playing_until,
                                time.monotonic() + 0.15,
                            )
                            consec_write_errs = 0
                    else:
                        if live_rs_l is not None:
                            _flush_live_resampler(stream, play_sr)
                        if stream is not None:
                            if self._call_ui_state == "calling" and self._ringback_state.is_active:
                                vol = _safe_cfg_float(
                                    self._read_tk_var_safe("vol_var", self._live_vol),
                                    1.0,
                                    _OUTPUT_VOLUME_MIN,
                                    _OUTPUT_VOLUME_MAX,
                                )
                                ring_chunk = self._ringback_state.next_chunk(len(idle_zeros), play_sr, volume=vol)
                                stream.write(ring_chunk)
                                consec_write_errs = 0
                                if (
                                    self.running
                                    and self._mode2_capture_ready
                                    and (time.monotonic() - self._call_started_mono) >= 1.8
                                ):
                                    self._complete_call_connection_from_audio_thread(
                                        stream, play_sr, expected_audio_sid=audio_sid
                                    )
                            else:
                                if self._call_ui_state == "calling" and self.running and self._mode2_capture_ready and (
                                    time.monotonic() - self._call_started_mono
                                ) >= 1.8:
                                    self._complete_call_connection_from_audio_thread(
                                        stream, play_sr, expected_audio_sid=audio_sid
                                    )
                                stream.write(idle_zeros)
                                consec_write_errs = 0
                except Exception as write_exc:
                    if not _session_still_active():
                        break
                    consec_write_errs += 1
                    self.log(f"[출력 스트림 복구 시도 ({consec_write_errs}/5)] {write_exc}")
                    if consec_write_errs >= 5:
                        self._abort_bridge_on_device_error(f"[출력 스트림 오류] 반복 쓰기 실패: {write_exc}")
                        break
                    self._refresh_audio_devices_data_only(reinit_portaudio=False)
                    self._schedule_ui(lambda: self.refresh_audio_devices(reinit_portaudio=False, log_info=False))
                    cur_desired = str(self._read_tk_var_safe("out_choice_var", self._live_out_choice) or "")
                    if cur_desired not in self.out_map and self.primary_outputs:
                        cur_desired = self.primary_outputs[0]
                    if not _open_primary(cur_desired):
                        if _session_still_active():
                            self._abort_bridge_on_device_error(f"[출력 스트림 오류] {write_exc}")
                        break
        finally:
            self._output_stream_ready.set()
            self._close_pa_stream(stream)

    def _surface_tts_failure_ui(
        self,
        clause_pcm_q: queue.Queue[Any],
        sub_bundle: Any,
        *,
        notice_text: str = "",
        is_fatal: bool = False,
        is_cancelled: bool = False,
        suppress_calling_error: bool = False,
    ) -> None:
        """Ensure spoken text still reaches chat/subtitles on TTS failure and surface fatal TTS auth/credit errors visibly."""
        if is_cancelled:
            return
        if sub_bundle is not None:
            clause_pcm_q.put(sub_bundle)
        safe_notice = _redact_secrets(str(notice_text or "").strip(), extra_secrets=self._active_secret_values())
        if is_fatal and safe_notice and not getattr(self, "_tts_error_notice_shown", False):
            self._tts_error_notice_shown = True
            if getattr(self, "_momotalk_char_activated", False):
                def _append_tts_err(msg: str = safe_notice) -> None:
                    self._subtitle_seq += 1
                    self._append_subtitle_entry_fast(
                        SubtitleEntry(
                            entry_id=self._subtitle_seq,
                            char_id="__system__",
                            char_name_kr="음성 합성(TTS) 안내",
                            char_name_jp="",
                            jp_text=msg,
                            kr_text=msg,
                            is_turn_start=True,
                        )
                    )

                self._schedule_ui(_append_tts_err)
            if (not suppress_calling_error) and sub_bundle is None and getattr(self, "_call_ui_state", "") == "calling":
                cur_sid = self._companion_session_id
                cur_p = self._get_ai_provider()
                cur_b = str(self.cfg.get("gemini_backend", "browser"))
                self._schedule_ui(
                    lambda sid=cur_sid, pr=cur_p, b=cur_b, d=safe_notice: self._enter_calling_error_ui(
                        sid,
                        pr,
                        backend=b,
                        title="⚠️ 음성 합성(TTS) 오류",
                        detail=d,
                    )
                )

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
        turn_char: dict[str, Any] | None = None,
        subtitle_text_override: str = "",
        suppress_subtitle: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        with self._active_tts_lock:
            self._active_tts_inflight += 1
        ov = overrides if isinstance(overrides, dict) else None
        ephemeral_s2_backend: ColabS2ProBackend | None = None
        last_sub_bundle: Any = None
        sub_bundle_built = False
        sub_emitted = False
        try:
            def _is_cancelled() -> bool:
                return (
                    (not self._is_audio_pipeline_active())
                    or my_gen != self.playback_gen_id
                    or self.stop_playback_flag.is_set()
                )

            if _is_cancelled():
                return

            if not self._output_stream_ready.is_set():
                self._output_stream_ready.wait(timeout=0.5)
                if _is_cancelled():
                    return

            if ov is not None and "streaming_speed_mode" in ov:
                speed_code = str(ov.get("streaming_speed_mode") or "expressive")
            else:
                speed_lbl = str(self._read_tk_var_safe("speed_mode_var", self._live_speed_mode_lbl) or "")
                speed_code = SPEED_LABEL_TO_CODE.get(speed_lbl, str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive"))
            fish_lat = "normal" if speed_code == "full_turn" else "balanced"
            c = turn_char if isinstance(turn_char, dict) else self.current_char
            cname = c.get("name_kr", c.get("id", self.current_char_id))
            t_req = time.time()
            play_sr = int(getattr(self, "_active_play_sr", FISH_PCM_SR) or FISH_PCM_SR)
            need_resample = play_sr != FISH_PCM_SR
            raw_vol = ov.get("output_volume", self.cfg.get("output_volume", 1.0)) if ov is not None else self.cfg.get("output_volume", 1.0)
            vol = _safe_cfg_float(raw_vol, 1.0, _OUTPUT_VOLUME_MIN, _OUTPUT_VOLUME_MAX)
            cache_enabled = self._is_tts_cache_enabled(overrides=ov)
            eff_fish_api_key = (
                str(ov.get("fish_api_key", "") or "").strip()
                if (ov is not None and "fish_api_key" in ov)
                else str(self.cfg.get("fish_api_key", "") or "").strip()
            )

            def _make_sub_bundle(clean_jp_txt: str) -> dict[str, Any] | None:
                nonlocal last_sub_bundle, sub_bundle_built
                if suppress_subtitle:
                    return None
                if sub_bundle_built:
                    return last_sub_bundle
                sub_src = str(subtitle_text_override or clean_jp_txt or "").strip()
                if not sub_src:
                    return None
                last_sub_bundle = self._prepare_subtitle_bundle(
                    sub_src,
                    c,
                    is_turn_start=bool(is_opening),
                    pre_kr_future=pre_kr_future,
                )
                sub_bundle_built = True
                return last_sub_bundle

            # 0) Dedicated Google Colab Fish Audio S2-Pro Server path (Option B: Free / Unlimited / Zero Congestion)
            if self._is_colab_s2pro_enabled(overrides=ov):
                ephemeral_s2_backend = self._make_ephemeral_colab_backend(ov)
                s2_dir = self._build_colab_s2pro_tts_context(
                    text,
                    clause_rms=clause_rms,
                    voice_persona_key=voice_persona_key,
                    char_override=c,
                    overrides=ov,
                )
                clean_jp = str(s2_dir.get("clean_text") or "").strip()
                tts_text = str(s2_dir.get("tts_text") or clean_jp).strip()
                sub_bundle = _make_sub_bundle(clean_jp)
                s2_cache_key = self._compute_tts_cache_key(s2_dir, "colab_s2pro", tts_text)
                if cache_enabled:
                    try:
                        cached_s2 = self.tts_cache.get_pcm(s2_cache_key)
                    except Exception:
                        cached_s2 = None
                    if cached_s2 is not None:
                        ttfa = time.time() - t_req
                        e2e = time.time() - t_captured
                        vp_lbl = f" 🎭{s2_dir['voice_persona_label']}" if s2_dir.get("voice_persona_label") else ""
                        log_line = (
                            f"🔊 [{cname}{vp_lbl} 발화 #{clause_no} (☁️S2-Pro ⚡캐시 | TTS {ttfa:.2f}s | 총 {e2e:.2f}s)] "
                            f"{s2_dir['clean_text']}"
                        )
                        self._emit_mono_pcm_to_clause_queue(
                            cached_s2[0],
                            cached_s2[1],
                            my_gen=my_gen,
                            play_sr=play_sr,
                            vol=vol,
                            clause_pcm_q=clause_pcm_q,
                            sub_bundle=sub_bundle,
                            log_line=log_line,
                        )
                        sub_emitted = True
                        return

                s2_started = False
                try:
                    def _on_clause_start(cushion_sec: float, buf_sec: float, reached_eof: bool) -> None:
                        nonlocal sub_emitted, s2_started
                        s2_started = True
                        ttfa = time.time() - t_req
                        e2e = time.time() - t_captured
                        act_lbl = f"{s2_dir['s2_tag']} " if s2_dir.get("s2_tag") else ""
                        vp_lbl = f" 🎭{s2_dir['voice_persona_label']}" if s2_dir.get("voice_persona_label") else ""
                        if cushion_sec > 0.0:
                            buf_tag = f" | 적응버퍼 {buf_sec:.1f}s/{cushion_sec:.1f}s{'(EOF)' if reached_eof else ''} {ttfa:.2f}s"
                        else:
                            buf_tag = f" | TTS {ttfa:.2f}s"
                        self.log(
                            f"🔊 [{cname}{vp_lbl} 발화 #{clause_no} (☁️Colab S2-Pro {act_lbl}temp={s2_dir['temperature']:.2f}{buf_tag} | 총 {e2e:.2f}s)] {s2_dir['clean_text']}"
                        )
                        if sub_bundle is not None:
                            clause_pcm_q.put(sub_bundle)
                            sub_emitted = True

                    got_any = self._stream_colab_s2pro_adaptive(
                        tts_text=tts_text,
                        s2_dir=s2_dir,
                        speed_code=speed_code,
                        play_sr=play_sr,
                        vol=vol,
                        cache_enabled=cache_enabled,
                        s2_cache_key=s2_cache_key,
                        cancel_check=_is_cancelled,
                        on_playback_start=_on_clause_start,
                        emit_stereo=lambda arr, _sr=play_sr: clause_pcm_q.put((_sr, arr)),
                        trailing_silence_sec=0.025,
                        backend_override=ephemeral_s2_backend,
                    )
                    if got_any or _is_cancelled():
                        return
                    if not eff_fish_api_key and not self._is_fish_mcp_authenticated():
                        self.log("☁️ [Colab S2-Pro 빈 오디오] 서버가 유효한 오디오를 반환하지 않았습니다 (대체 Fish 키/MCP 미설정).")
                        self._surface_tts_failure_ui(
                            clause_pcm_q,
                            sub_bundle if not sub_emitted else None,
                            notice_text="",
                            is_fatal=False,
                            is_cancelled=_is_cancelled(),
                            suppress_calling_error=suppress_subtitle,
                        )
                        sub_emitted = True
                        return
                except ColabS2ProAuthError as exc:
                    self.log(f"🔑 [Colab S2-Pro 인증 오류] {exc} (⚙️ 설정 -> 1단계에서 인증 토큰 확인)")
                    self._surface_tts_failure_ui(
                        clause_pcm_q,
                        sub_bundle if not sub_emitted else None,
                        notice_text="🔑 Colab S2-Pro 서버 인증 토큰이 올바르지 않습니다. ⚙️ 설정 → 1단계에서 토큰을 확인해 주세요.",
                        is_fatal=True,
                        is_cancelled=_is_cancelled(),
                        suppress_calling_error=suppress_subtitle,
                    )
                    sub_emitted = True
                    return
                except (ColabS2ProUnavailableError, ColabS2ProError) as exc:
                    if ephemeral_s2_backend is None and not self._colab_s2pro_offline_warned:
                        self._colab_s2pro_offline_warned = True
                        self._schedule_ui(self._refresh_companion_badges)
                        self.log(f"☁️ [Colab S2-Pro 서버 오류] {exc} -> 기존 Fish API/MCP가 있으면 자동 폴백합니다.")
                    else:
                        self.log(f"[Colab S2-Pro 폴백] {exc}")
                    if s2_started:
                        return
                    if _is_cancelled() or (not eff_fish_api_key and not self._is_fish_mcp_authenticated()):
                        self._surface_tts_failure_ui(
                            clause_pcm_q,
                            sub_bundle if not sub_emitted else None,
                            notice_text=f"☁️ Colab S2-Pro 서버 연결 오류로 음성을 재생하지 못했습니다 ({exc}). 서버 상태 또는 Fish Audio 키를 확인해 주세요.",
                            is_fatal=True,
                            is_cancelled=_is_cancelled(),
                            suppress_calling_error=suppress_subtitle,
                        )
                        sub_emitted = True
                        return

            # 1) Direct Fish MCP path (when MCP primary mode is ON, or fish_api_key is empty & MCP signed in)
            if self._should_prefer_fish_mcp(overrides=ov):
                mcp_dir = self._build_mcp_tts_context(
                    text,
                    clause_rms=clause_rms,
                    voice_persona_key=voice_persona_key,
                    char_override=c,
                    overrides=ov,
                )
                clean_jp = str(mcp_dir.get("clean_text") or "").strip()
                sub_bundle = _make_sub_bundle(clean_jp)
                mcp_res = self._synthesize_via_mcp_cached(
                    mcp_dir, cancel_check=_is_cancelled, cache_enabled_override=cache_enabled
                )
                if mcp_res is not None:
                    pcm_f32, src_sr, from_cache, n_bytes = mcp_res
                    ttfa = time.time() - t_req
                    e2e = time.time() - t_captured
                    vp_lbl = f" 🎭{mcp_dir['voice_persona_label']}" if mcp_dir.get("voice_persona_label") else ""
                    badge = "⚡ 캐시 0B" if from_cache else f"🌐 MCP {n_bytes}B"
                    log_line = (
                        f"🔊 [{cname}{vp_lbl} 발화 #{clause_no} ({badge} | TTS {ttfa:.2f}s | 총 {e2e:.2f}s)] "
                        f"{mcp_dir['clean_text']}"
                    )
                    self._emit_mono_pcm_to_clause_queue(
                        pcm_f32,
                        src_sr,
                        my_gen=my_gen,
                        play_sr=play_sr,
                        vol=vol,
                        clause_pcm_q=clause_pcm_q,
                        sub_bundle=sub_bundle,
                        log_line=log_line,
                    )
                    sub_emitted = True
                    return
                if _is_cancelled() or not eff_fish_api_key:
                    self._surface_tts_failure_ui(
                        clause_pcm_q,
                        sub_bundle if not sub_emitted else None,
                        notice_text=self._last_mcp_tts_error_msg or "⚠️ Fish Audio MCP 음성 합성에 실패했습니다. ⚙️ 설정에서 인증/크레딧을 확인해 주세요.",
                        is_fatal=True,
                        is_cancelled=_is_cancelled(),
                        suppress_calling_error=suppress_subtitle,
                    )
                    sub_emitted = True
                    return

            # 2) Direct Fish Cloud API path (with local TTS disk cache lookup + HTTP 402/429/404/410 MCP fallback)
            headers, payload_bytes, _, dir_info = self._build_fish_request(
                text,
                latency_mode=fish_lat,
                clause_rms=clause_rms,
                voice_persona_key=voice_persona_key,
                char_override=c,
                overrides=ov,
            )
            clean_jp = str(dir_info.get("clean_text") or "").strip()
            sub_bundle = _make_sub_bundle(clean_jp)
            api_cache_key = self._compute_tts_cache_key(
                dir_info,
                f"fish_api:{dir_info.get('model_header', 's2.1-pro-free')}",
                str(dir_info.get("tts_text") or ""),
            )
            if cache_enabled:
                try:
                    cached_api = self.tts_cache.get_pcm(api_cache_key)
                except Exception:
                    cached_api = None
                if cached_api is not None:
                    ttfa = time.time() - t_req
                    e2e = time.time() - t_captured
                    vp_lbl = f" 🎭{dir_info['voice_persona_label']}" if dir_info.get("voice_persona_label") else ""
                    log_line = (
                        f"🔊 [{cname}{vp_lbl} 발화 #{clause_no} (⚡ 캐시 0B | TTS {ttfa:.2f}s | 총 {e2e:.2f}s)] "
                        f"{dir_info['clean_text']}"
                    )
                    self._emit_mono_pcm_to_clause_queue(
                        cached_api[0],
                        cached_api[1],
                        my_gen=my_gen,
                        play_sr=play_sr,
                        vol=vol,
                        clause_pcm_q=clause_pcm_q,
                        sub_bundle=sub_bundle,
                        log_line=log_line,
                    )
                    sub_emitted = True
                    return

            pcm_scale = np.float32(vol / 32768.0)
            resampler = StreamingPolyphaseResampler(FISH_PCM_SR, play_sr) if need_resample else None
            fade_tail_n = max(4, int(play_sr * 0.004))
            tail_hold = np.empty(0, dtype=np.float32)

            first_chunk = True
            first_emitted = True
            leftover = b""
            tts_url = _validate_fish_api_url(FISH_TTS_URL)
            total_pcm_bytes = 0
            hit_stream_limit = False
            fallback_status: int | None = None
            raw_pcm_chunks: list[bytes] = []

            with self.http_client.stream("POST", tts_url, headers=headers, content=payload_bytes) as resp:
                self.last_http_activity = time.time()
                if resp.status_code != 200:
                    err_body = _read_bounded_httpx_error(resp, MAX_HTTP_ERROR_BYTES)
                    self.log(f"[Fish TTS 오류 HTTP {resp.status_code}] {err_body[:220]}")
                    if resp.status_code in FISH_MCP_FALLBACK_HTTP_STATUSES and self._can_fallback_to_fish_mcp(overrides=ov):
                        fallback_status = int(resp.status_code)
                    else:
                        sc = int(resp.status_code)
                        if sc in (401, 403):
                            tts_notice = f"🔑 Fish Audio API 키가 올바르지 않거나 만료되었습니다 (HTTP {sc}). ⚙️ 설정 → 1단계에서 API 키를 확인해 주세요."
                        elif sc == 402:
                            tts_notice = "💳 Fish Audio 크레딧이 부족합니다 (HTTP 402). fish.audio 잔액을 충전하거나 ⚙️ 설정에서 MCP 웹 크레딧을 연동해 주세요."
                        elif sc == 429:
                            tts_notice = "⏳ Fish Audio 요청 한도(HTTP 429)에 도달했습니다. 잠시 후 다시 말씀하시거나 ⚙️ 설정에서 크레딧·MCP 연동을 확인해 주세요."
                        else:
                            tts_notice = f"⚠️ Fish Audio 음성 합성 오류 (HTTP {sc}): {err_body[:140]}"
                        self._surface_tts_failure_ui(
                            clause_pcm_q,
                            sub_bundle if not sub_emitted else None,
                            notice_text=tts_notice,
                            is_fatal=(sc in (401, 402, 403, 429)),
                            is_cancelled=_is_cancelled(),
                            suppress_calling_error=suppress_subtitle,
                        )
                        sub_emitted = True
                        return
                else:
                    for chunk in resp.iter_bytes(chunk_size=2048):
                        if _is_cancelled():
                            break
                        if not chunk:
                            continue
                        total_pcm_bytes += len(chunk)
                        if total_pcm_bytes > MAX_TTS_STREAM_BYTES:
                            hit_stream_limit = True
                            self.log(f"[Fish TTS 보안 경고] 단일 스트림 크기 한도({MAX_TTS_STREAM_BYTES} bytes) 초과로 중단했습니다.")
                            break
                        if cache_enabled:
                            raw_pcm_chunks.append(chunk)
                        if first_chunk:
                            first_chunk = False
                            ttfa = time.time() - t_req
                            e2e = time.time() - t_captured
                            act_lbl = f"{dir_info['s2_tag']} " if dir_info["s2_tag"] else ""
                            vp_lbl = f" 🎭{dir_info['voice_persona_label']}" if dir_info.get("voice_persona_label") else ""
                            lat_badge = "⚡" if fish_lat == "balanced" else "✨"
                            self.log(
                                f"🔊 [{cname}{vp_lbl} 발화 #{clause_no} ({act_lbl}temp={dir_info['temperature']:.2f} spd={float(dir_info.get('speed', 1.0)):.2f}x | {lat_badge}TTS {ttfa:.2f}s | 총 {e2e:.2f}s)] {dir_info['clean_text']}"
                            )
                            if sub_bundle is not None:
                                clause_pcm_q.put(sub_bundle)
                                sub_emitted = True

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
                        clause_pcm_q.put((int(play_sr), _mono_to_stereo(audio_f32)))

            if fallback_status is not None:
                if not self._fish_mcp_fallback_logged:
                    self._fish_mcp_fallback_logged = True
                    cs_lbl = "ON" if bool(self.cfg.get("fish_credit_saver_mode", True)) else "OFF"
                    self.log(
                        f"🔄 [Fish Audio MCP 자동 전환] Fish API HTTP {fallback_status} 오류로 공식 MCP 웹 크레딧으로 자동 전환합니다! "
                        f"(크레딧 절약 모드: {cs_lbl})"
                    )
                mcp_res = self._synthesize_via_mcp_cached(
                    dir_info, cancel_check=_is_cancelled, cache_enabled_override=cache_enabled
                )
                if mcp_res is not None:
                    pcm_f32, src_sr, from_cache, n_bytes = mcp_res
                    ttfa = time.time() - t_req
                    e2e = time.time() - t_captured
                    vp_lbl = f" 🎭{dir_info['voice_persona_label']}" if dir_info.get("voice_persona_label") else ""
                    badge = "⚡ 캐시 0B" if from_cache else f"🌐 MCP 전환 {n_bytes}B"
                    log_line = (
                        f"🔊 [{cname}{vp_lbl} 발화 #{clause_no} ({badge} | TTS {ttfa:.2f}s | 총 {e2e:.2f}s)] "
                        f"{dir_info['clean_text']}"
                    )
                    self._emit_mono_pcm_to_clause_queue(
                        pcm_f32,
                        src_sr,
                        my_gen=my_gen,
                        play_sr=play_sr,
                        vol=vol,
                        clause_pcm_q=clause_pcm_q,
                        sub_bundle=sub_bundle,
                        log_line=log_line,
                    )
                    sub_emitted = True
                else:
                    self._surface_tts_failure_ui(
                        clause_pcm_q,
                        sub_bundle if not sub_emitted else None,
                        notice_text=self._last_mcp_tts_error_msg
                        or f"⚠️ Fish Audio API(HTTP {fallback_status}) 및 MCP 전환 모두 실패해 음성을 재생하지 못했습니다.",
                        is_fatal=True,
                        is_cancelled=_is_cancelled(),
                        suppress_calling_error=suppress_subtitle,
                    )
                    sub_emitted = True
                return

            if not _is_cancelled():
                tail_f32 = resampler.flush() if resampler is not None else tail_hold
                if tail_f32.size > 0:
                    tail_f32 = _apply_micro_fade_1d(
                        tail_f32, play_sr, fade_in=first_emitted, fade_out=True, fade_ms=4.0
                    )
                    np.clip(tail_f32, -0.985, 0.985, out=tail_f32)
                    clause_pcm_q.put((int(play_sr), _mono_to_stereo(tail_f32)))
                clause_pcm_q.put((int(play_sr), np.zeros((int(play_sr * 0.025), 2), dtype=np.float32)))
                if cache_enabled and raw_pcm_chunks and (not hit_stream_limit):
                    try:
                        joined = b"".join(raw_pcm_chunks)
                        even_total = len(joined) & ~1
                        if even_total >= 2:
                            unscaled_f32 = np.frombuffer(joined[:even_total], dtype=np.int16).astype(np.float32) * np.float32(1.0 / 32768.0)
                            self.tts_cache.put_pcm(api_cache_key, unscaled_f32, FISH_PCM_SR)
                    except Exception:
                        pass
        except Exception as exc:
            self.log(f"[TTS 스트리밍 오류] {exc}")
            if not sub_emitted and last_sub_bundle is not None:
                self._surface_tts_failure_ui(
                    clause_pcm_q,
                    last_sub_bundle,
                    is_fatal=False,
                    is_cancelled=(
                        (not self._is_audio_pipeline_active())
                        or my_gen != self.playback_gen_id
                        or self.stop_playback_flag.is_set()
                    ),
                    suppress_calling_error=suppress_subtitle,
                )
        finally:
            if ephemeral_s2_backend is not None:
                try:
                    ephemeral_s2_backend.close()
                except Exception:
                    pass
            clause_pcm_q.put(None)
            with self._active_tts_lock:
                self._active_tts_inflight = max(0, self._active_tts_inflight - 1)

    def _clause_order_drainer_loop(self, *, audio_sid: int | None = None) -> None:
        """Drain parallel-synthesized clause queues strictly in clause arrival order into pcm_play_queue."""
        while self._is_audio_pipeline_active() and (audio_sid is None or audio_sid == self._audio_session_id):
            try:
                my_gen, _clause_no, clause_pcm_q = self.ordered_clause_queue.get(timeout=0.04)
            except queue.Empty:
                continue

            while self._is_audio_pipeline_active() and (audio_sid is None or audio_sid == self._audio_session_id):
                if my_gen != self.playback_gen_id or self.stop_playback_flag.is_set():
                    break
                try:
                    item = clause_pcm_q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    break
                while self._is_audio_pipeline_active() and (audio_sid is None or audio_sid == self._audio_session_id):
                    if my_gen != self.playback_gen_id or self.stop_playback_flag.is_set():
                        break
                    try:
                        self.pcm_play_queue.put((my_gen, item), timeout=0.05)
                        break
                    except queue.Full:
                        continue

    def _tts_prefetch_loop(self, *, audio_sid: int | None = None) -> None:
        """Submit Clause N+1 to 2-worker pool immediately WHILE Clause N is still streaming/playing!"""
        while self._is_audio_pipeline_active() and (audio_sid is None or audio_sid == self._audio_session_id):
            try:
                raw_item = self.tts_queue.get(timeout=0.04)
            except queue.Empty:
                continue

            enqueued_gen: int | None = None
            turn_char: dict[str, Any] | None = None
            subtitle_text_override = ""
            suppress_subtitle = False
            test_overrides: dict[str, Any] | None = None
            if len(raw_item) >= 12:
                (
                    clause_no,
                    text,
                    t_captured,
                    clause_rms,
                    is_opening,
                    pre_kr_future,
                    voice_persona_key,
                    enqueued_gen,
                    turn_char,
                    subtitle_text_override,
                    suppress_subtitle,
                    test_overrides,
                ) = raw_item[:12]
            elif len(raw_item) >= 11:
                (
                    clause_no,
                    text,
                    t_captured,
                    clause_rms,
                    is_opening,
                    pre_kr_future,
                    voice_persona_key,
                    enqueued_gen,
                    turn_char,
                    subtitle_text_override,
                    suppress_subtitle,
                ) = raw_item[:11]
            elif len(raw_item) >= 9:
                clause_no, text, t_captured, clause_rms, is_opening, pre_kr_future, voice_persona_key, enqueued_gen, turn_char = raw_item[:9]
            elif len(raw_item) == 8:
                clause_no, text, t_captured, clause_rms, is_opening, pre_kr_future, voice_persona_key, enqueued_gen = raw_item[:8]
            elif len(raw_item) == 7:
                clause_no, text, t_captured, clause_rms, is_opening, pre_kr_future, voice_persona_key = raw_item
            elif len(raw_item) == 6:
                clause_no, text, t_captured, clause_rms, is_opening, pre_kr_future = raw_item
                voice_persona_key = ""
            else:
                clause_no, text, t_captured, clause_rms, is_opening = raw_item[:5]
                pre_kr_future = None
                voice_persona_key = ""

            with self._playback_lock:
                if enqueued_gen is None:
                    if self.stop_playback_flag.is_set():
                        continue
                    my_gen = self.playback_gen_id
                elif enqueued_gen != self.playback_gen_id:
                    continue
                else:
                    my_gen = enqueued_gen

            ex = self.tts_executor
            if ex is None:
                continue
            clause_pcm_q: queue.Queue[Any] = queue.Queue()
            self.ordered_clause_queue.put((my_gen, clause_no, clause_pcm_q))
            submit_kwargs: dict[str, Any] = {}
            if subtitle_text_override:
                submit_kwargs["subtitle_text_override"] = subtitle_text_override
            if suppress_subtitle:
                submit_kwargs["suppress_subtitle"] = True
            if isinstance(test_overrides, dict):
                submit_kwargs["overrides"] = test_overrides
            try:
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
                    turn_char,
                    **submit_kwargs,
                )
            except RuntimeError:
                clause_pcm_q.put(None)

    def _trigger_player_loop(self, *, audio_sid: int | None = None) -> None:
        """Keep secondary monitor speaker (ANMITE) DAC awake via ultrasonic pilot and emit cached SAPI cues."""
        active_lbl = ""
        stream: sd.OutputStream | None = None
        out_sr = 48000
        idle_pilot = np.zeros((512, 2), dtype=np.float32)
        chunk_frames = 1024

        def _is_active() -> bool:
            return bool(self.running and (audio_sid is None or audio_sid == self._audio_session_id))

        def _open_for(lbl: str) -> bool:
            nonlocal active_lbl, stream, out_sr, idle_pilot, chunk_frames
            self._close_pa_stream(stream)
            stream = None
            if not lbl:
                active_lbl = ""
                return False
            try:
                wavs = self._get_trigger_wavs()
                first_wav = next(iter(wavs.values()), None)
                sample_sr = int(first_wav[1]) if first_wav is not None and int(first_wav[1] or 0) > 0 else 16000
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
            _open_for(str(self._read_tk_var_safe("trigger_out_choice_var", self._live_trigger_out_choice) or ""))
            while _is_active():
                desired_lbl = str(self._read_tk_var_safe("trigger_out_choice_var", self._live_trigger_out_choice) or "")
                if desired_lbl != active_lbl:
                    _open_for(desired_lbl)

                try:
                    key = self.trigger_play_queue.get(timeout=0.015)
                except queue.Empty:
                    if not _is_active():
                        break
                    if stream is not None:
                        stream.write(idle_pilot)
                    else:
                        time.sleep(0.05)
                    continue

                if stream is None:
                    continue
                wavs = self._get_trigger_wavs()
                entry = wavs.get(key) or wavs.get("nudge_1") or next(iter(wavs.values()), None)
                if entry is None:
                    continue
                audio, src_sr = entry
                pvol_lbl = str(self._read_tk_var_safe("proactive_vol_var", self._live_proactive_vol_lbl) or "")
                vol = _safe_cfg_float(PROACTIVE_VOL_VALS.get(pvol_lbl, self.cfg.get("proactive_trigger_volume", 1.0)), 1.0, 0.0, 2.5)
                sig = np.clip(audio * vol, -0.98, 0.98).astype(np.float32)
                if src_sr > 0 and out_sr > 0 and out_sr != src_sr:
                    g = gcd(src_sr, out_sr)
                    sig = np.clip(_resample_pcm_1d(sig, out_sr // g, src_sr // g), -0.98, 0.98).astype(np.float32)
                stereo = np.column_stack([sig, sig])
                for pos in range(0, len(stereo), chunk_frames):
                    if not _is_active():
                        break
                    stream.write(stereo[pos : pos + chunk_frames])
        except Exception as exc:
            self.log(f"[트리거 출력 오류] {exc}")
        finally:
            self._close_pa_stream(stream)

    def _watchdog_loop(self, *, audio_sid: int | None = None) -> None:
        """Monitor silence after character playback completes and emit proactive cues with away protection."""
        last_ui_ts = 0.0
        last_ui_pair: tuple[str, str] | None = None
        while self.running and (audio_sid is None or audio_sid == self._audio_session_id):
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

    def _capture_loop(self, *, audio_sid: int | None = None) -> None:
        """25ms Capture + Character-Adaptive Breath/Clause Slicer (prevents mid-word Japanese sokuon 'っ' cuts!)."""
        in_lbl = str(self._read_tk_var_safe("in_choice_var", self._live_in_choice) or "")
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
            clause_rms = float(np.percentile(rms_slice, 85)) if rms_slice else 0.0
            self.utterance_queue.put((full, clause_counter, time.time(), clause_rms, is_opening, active_in_sr))

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
                if not np.all(np.isfinite(mono)):
                    mono = np.nan_to_num(mono, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
                rms = float(np.sqrt(float(np.dot(mono, mono)) / mono.size))
                peak = float(np.max(np.abs(mono)))
                if not (math.isfinite(rms) and math.isfinite(peak)):
                    rms = 0.0
                    peak = 0.0
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
            cur_ch = self.current_char
            raw_p = cur_ch.get("persona") if isinstance(cur_ch, dict) else None
            raw_vap = raw_p.get("voice_acting_profile") if isinstance(raw_p, dict) else None
            vap = raw_vap if isinstance(raw_vap, dict) else {}
            char_min_clause = _safe_cfg_float(vap.get("min_clause_sec", 1.42), 1.42, 0.5, 10.0)
            char_end_pause = _safe_cfg_float(vap.get("end_pause_sec", 0.34), 0.34, 0.15, 5.0)

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
            if not (self.running and (audio_sid is None or audio_sid == self._audio_session_id)):
                break
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
                                self._schedule_ui(lambda v=cand_lbl: self.in_choice_var.set(v))
                            while self.running and (audio_sid is None or audio_sid == self._audio_session_id):
                                time.sleep(0.08)
                    finally:
                        self._active_capture_speaking = False
                        self._mode2_capture_ready = False
                        self._dec_pa_stream()
                    break
                except Exception as exc:
                    last_exc = exc
            if opened:
                break

        if not opened and self.running and (audio_sid is None or audio_sid == self._audio_session_id):
            self._abort_bridge_on_device_error(f"[입력 장치 오류] {last_exc or in_lbl}")

    def _get_char_whisper_hint_tokens(self, tok: Any, char_data: dict) -> list[int]:
        cid = char_data.get("id", "")
        if cid in self.hint_token_cache:
            return self.hint_token_cache[cid]
        raw_p = char_data.get("persona") if isinstance(char_data, dict) else None
        raw_vap = raw_p.get("voice_acting_profile") if isinstance(raw_p, dict) else None
        vap = raw_vap if isinstance(raw_vap, dict) else {}
        hint_str = str(vap.get("whisper_hint_jp") or "").strip()
        if not hint_str:
            return []
        try:
            toks = list(tok.encode(" " + hint_str))[:45]
        except Exception:
            toks = []
        self.hint_token_cache[cid] = toks
        return toks

    def _stt_loop(self, *, audio_sid: int | None = None) -> None:
        """Dynamic 4s/6s/8s/12s Mel-Window CUDA INT8 Whisper with Sticky-JA fastpath."""
        try:
            from faster_whisper.tokenizer import Tokenizer
        except Exception as imp_exc:
            self.log(f"[STT 오류] faster-whisper 토크나이저 로드 실패: {imp_exc}")
            return

        while self.running and (audio_sid is None or audio_sid == self._audio_session_id):
            try:
                raw_u = self.utterance_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            if len(raw_u) >= 6:
                raw_audio, clause_no, t_captured, clause_rms, is_opening, src_sr = raw_u[:6]
                src_sr = int(src_sr or WHISPER_SR)
            else:
                raw_audio, clause_no, t_captured, clause_rms, is_opening = raw_u[:5]
                src_sr = WHISPER_SR

            audio_16k = np.asarray(raw_audio, dtype=np.float32)
            if audio_16k.size == 0:
                continue
            if not np.all(np.isfinite(audio_16k)):
                audio_16k = np.nan_to_num(audio_16k, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
            if src_sr != WHISPER_SR and src_sr > 0:
                g = gcd(src_sr, WHISPER_SR)
                audio_16k = _resample_pcm_1d(audio_16k, WHISPER_SR // g, src_sr // g)

            peak = float(np.max(np.abs(audio_16k))) if audio_16k.size else 0.0
            if (not math.isfinite(peak)) or peak < 0.010:
                continue
            raw_peak = peak
            if peak < 0.28:
                audio_16k = np.clip(audio_16k * (0.36 / peak), -1.0, 1.0).astype(np.float32)

            stt_lbl = str(self._read_tk_var_safe("stt_lang_var", self._live_stt_lang_lbl) or "")
            lang_setting = STT_LABEL_TO_CODE.get(stt_lbl, str(self.cfg.get("stt_language", "auto") or "auto"))
            w_model = self.whisper_model
            w_tokenizers = self.tokenizers
            if w_model is None:
                continue
            beam_sz = 3 if getattr(self, "_whisper_is_cuda", False) else 1
            stt_start_gen = self.playback_gen_id
            t0 = time.time()
            self._active_stt_busy = True
            try:
                from src.controller.local_mic_stt import _is_128mel_whisper_model, _text_compression_ratio

                use_std_30s = _is_128mel_whisper_model(w_model, getattr(self, "_whisper_loaded_model", ""))
                trail = np.concatenate([audio_16k, np.zeros(int(WHISPER_SR * 0.15), dtype=np.float32)])
                need_sec = len(trail) / float(WHISPER_SR) + 0.35
                cl = 30 if use_std_30s else next((b for b in (4, 6, 8, 12, 16, 24, 30) if b >= need_sec), 30)
                target_samples = cl * WHISPER_SR
                if len(trail) < target_samples:
                    padded = np.pad(trail, (0, target_samples - len(trail)), mode="constant", constant_values=0.0)
                else:
                    padded = trail[:target_samples]
                feats = w_model.feature_extractor(padded, chunk_length=cl)
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

                enc = w_model.encode(feats)

                use_sticky_ja = lang_setting == "auto" and self._sticky_ja_hits >= 2
                if lang_setting == "auto" and not use_sticky_ja:
                    det_list = w_model.model.detect_language(enc)[0]
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

                def _decode_with_lang(lang_code: str, *, anti_repeat: bool = False):
                    tok_l = w_tokenizers.get(lang_code) or Tokenizer(
                        w_model.hf_tokenizer, True, task="transcribe", language=lang_code
                    )
                    hint_toks = (
                        self._get_char_whisper_hint_tokens(tok_l, c)
                        if (lang_code == "ja" and raw_peak >= 0.018)
                        else []
                    )
                    prompt_l = w_model.get_prompt(tok_l, hint_toks, without_timestamps=(not use_std_30s))
                    supp_l = _resolve_whisper_suppressed_tokens(w_model, tok_l)
                    use_anti_rep = anti_repeat or (not use_std_30s)
                    gen_res = w_model.model.generate(
                        enc,
                        [prompt_l],
                        beam_size=beam_sz,
                        max_length=224 if use_std_30s else 160,
                        suppress_blank=True,
                        suppress_tokens=supp_l,
                        no_repeat_ngram_size=3 if use_anti_rep else 0,
                        repetition_penalty=1.08 if use_anti_rep else 1.0,
                        return_no_speech_prob=True,
                    )[0]
                    return tok_l, gen_res

                tok, res = _decode_with_lang(det_lang)
                if use_sticky_ja and res.no_speech_prob > 0.35:
                    det_list = w_model.model.detect_language(enc)[0]
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

                def _tokens_to_text(tok_obj: Any, gen_obj: Any) -> str:
                    ts_b = getattr(tok_obj, "timestamp_begin", None)
                    if not isinstance(ts_b, int) or isinstance(ts_b, bool):
                        ts_b = None
                    seq_ids = getattr(gen_obj, "sequences_ids", None) or []
                    first_s = seq_ids[0] if seq_ids else []
                    toks_clean = [t for t in first_s if t < tok_obj.eot and (ts_b is None or t < ts_b)]
                    return " ".join(tok_obj.decode(toks_clean).split()).strip()

                text = _tokens_to_text(tok, res)
                orig_cr = _text_compression_ratio(text) if use_std_30s else 0.0
                if use_std_30s and orig_cr > 2.4:
                    tok_r, res_r = _decode_with_lang(det_lang, anti_repeat=True)
                    retry_text = _tokens_to_text(tok_r, res_r)
                    if retry_text and _text_compression_ratio(retry_text) < orig_cr:
                        tok, res, text = tok_r, res_r, retry_text
                dt = time.time() - t0
                if not text or text in HALLUCINATION_PHRASES or contains_trigger_echo(text):
                    continue
                # Guard against prompt-header echo on near-silent breaths
                header_prefix = f"{c.get('school_jp', '')} {c.get('club_jp', '')} {c.get('name_jp', '')}".strip()
                if header_prefix and text in header_prefix:
                    continue
                if len(text) <= 1 and (res.no_speech_prob > 0.28 or text in {"ん", "あ", "え", "."}):
                    continue

                if not (self.running and (audio_sid is None or audio_sid == self._audio_session_id)):
                    break
                self.watchdog.on_vbcable_speech_detected(time.monotonic(), is_new_turn=is_opening)
                self.log(f"📝 [구절 인식 #{clause_no} ({det_lang}/{cl}s, {dt:.2f}s)] {text}")
                with self._playback_lock:
                    if stt_start_gen != self.playback_gen_id:
                        continue
                    self.stop_playback_flag.clear()
                self.tts_queue.put((clause_no, text, t_captured, clause_rms, is_opening, None, "", stt_start_gen))
            except Exception as exc:
                self.log(f"[STT 오류] {exc}")
            finally:
                self._active_stt_busy = False

    # =========================================================================
    # Mode 1: Distributable PC Mic + Mode C Gemini + 5-State Proactive Engine
    # =========================================================================

    def _apply_synced_web_models(
        self,
        models: list[tuple[str, str]],
        tier: str,
        persist: bool = True,
        provider: str | None = None,
    ) -> None:
        p = normalize_ai_provider(provider or self._get_ai_provider())
        if not self._is_valid_synced_web_models(models, p):
            return
        clean_models = [
            (str(it[0]).strip(), str(it[1]).strip())
            for it in models
            if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[0]).strip() and str(it[1]).strip()
        ]
        unlocked_codes = {c for c, _ in clean_models}
        with self._cfg_persist_lock:
            if p == "gemini":
                self._account_web_models = list(clean_models)
                if tier:
                    self._web_account_tier = tier
                    self.cfg["web_account_tier"] = tier
                self.cfg["web_models"] = [[c, l] for c, l in clean_models]
                self.cfg["web_models_schema_version"] = 2
                self._extra_web_models = [(c, l) for c, l in clean_models if c not in GEMINI_BROWSER_MODEL_LABELS]

                cur_bmodel = str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen")
                if cur_bmodel not in unlocked_codes:
                    self.cfg["gemini_browser_model"] = "hydrogen" if "hydrogen" in unlocked_codes else clean_models[0][0]
                cur_sub = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
                if cur_sub.startswith("web:") and cur_sub[4:].strip() not in unlocked_codes:
                    self.cfg["subtitle_engine"] = "auto_inline"
            elif p == "claude":
                self.cfg["claude_web_models"] = [[c, l] for c, l in clean_models]
                if tier:
                    self.cfg["claude_web_account_tier"] = tier
                cur_bmodel = str(self.cfg.get("claude_browser_model", "default") or "default")
                if cur_bmodel not in unlocked_codes:
                    self.cfg["claude_browser_model"] = "default" if "default" in unlocked_codes else clean_models[0][0]
                cur_sub = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
                if cur_sub.startswith("claude_web:") and cur_sub[11:].strip() not in unlocked_codes:
                    self.cfg["subtitle_engine"] = "auto_inline"
            else:
                self.cfg["chatgpt_web_models"] = [[c, l] for c, l in clean_models]
                if tier:
                    self.cfg["chatgpt_web_account_tier"] = tier
                cur_bmodel = str(self.cfg.get("chatgpt_browser_model", "default") or "default")
                if cur_bmodel not in unlocked_codes:
                    self.cfg["chatgpt_browser_model"] = "default" if "default" in unlocked_codes else clean_models[0][0]
                cur_sub = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
                if cur_sub.startswith("chatgpt_web:") and cur_sub[12:].strip() not in unlocked_codes:
                    self.cfg["subtitle_engine"] = "auto_inline"

            if persist:
                save_bridge_config(self.cfg)
        self._schedule_ui(self._refresh_companion_badges)

    def _discover_web_models_async(
        self,
        provider: str | None = None,
        *,
        only_when_idle: bool = False,
    ) -> None:
        if getattr(self, "_closing", False):
            return
        target_p = normalize_ai_provider(provider or self._get_ai_provider())
        with self._web_model_discover_lock:
            if self._web_model_discover_inflight:
                return
            self._web_model_discover_inflight = True

        def _probe() -> None:
            try:
                if getattr(self, "_closing", False):
                    return
                if only_when_idle:
                    if (
                        (not self.companion_running)
                        or self._call_ui_state != "connected"
                        or (not self._companion_turn_queue.empty())
                        or self.companion_sm.get_status().state != CompanionState.IDLE
                    ):
                        return
                if not self._is_browser_provider_active(target_p):
                    return
                try:
                    models, tier = self.gemini_mgr.browser.sync_account_web_models(allow_connect=False)
                except Exception:
                    models, tier = [], ""
                if self._is_valid_synced_web_models(models, target_p):
                    self._apply_synced_web_models(models, tier, persist=True, provider=target_p)
            finally:
                with self._web_model_discover_lock:
                    self._web_model_discover_inflight = False

        threading.Thread(target=_probe, daemon=True, name="discover-web-models").start()

    def _auto_sync_cached_web_models_async(self) -> None:
        """Silently refresh account-tier web models on startup if idle and a saved login profile exists."""
        if (
            getattr(self, "_closing", False)
            or self.companion_running
            or self.running
            or self._call_ui_state != "idle"
        ):
            return
        target_p = self._get_ai_provider()
        if self._is_valid_synced_web_models(self._get_active_account_web_models(target_p), target_p):
            return
        try:
            prof = get_browser_profile_dir() / "Default"
            if not prof.exists():
                return
        except Exception:
            return

        def _probe() -> None:
            if (
                getattr(self, "_closing", False)
                or self.companion_running
                or self.running
                or self._call_ui_state != "idle"
                or self._get_ai_provider() != target_p
            ):
                return
            try:
                if not self._is_browser_provider_active(target_p):
                    self.gemini_mgr.set_provider(target_p, browser_model=self._get_active_browser_model(target_p))
                if (
                    getattr(self, "_closing", False)
                    or self.companion_running
                    or self.running
                    or self._call_ui_state != "idle"
                ):
                    return
                models, tier = self.gemini_mgr.browser.sync_account_web_models(allow_connect=True)
            except Exception:
                models, tier = [], ""
            if self._is_valid_synced_web_models(models, target_p):
                self._apply_synced_web_models(models, tier, persist=True, provider=target_p)
                short = AI_PROVIDER_SHORT_NAMES[target_p]
                tier_txt = f"{tier} 계정" if tier else f"웹 {short} 계정"
                self.log(f"[계정 모델 자동 동기화] {tier_txt} 사용 가능 모델 {len(models)}개 반영 완료.")

        th = threading.Thread(target=_probe, daemon=True, name="auto-sync-web-models")
        self._track_startup_bg_thread(th)
        th.start()

    def sync_companion_models_async(self, ui_cb: Any = None, provider_override: str | None = None) -> None:
        target_p = normalize_ai_provider(provider_override or self._get_ai_provider())
        short = AI_PROVIDER_SHORT_NAMES[target_p]
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
                    if not self._is_browser_provider_active(target_p):
                        self._abort_active_browser_prompt()
                        self.gemini_mgr.set_provider(target_p, browser_model=self._get_active_browser_model(target_p))
                    raw_models, tier = self.gemini_mgr.browser.sync_account_web_models(allow_connect=True)
                    if not self._is_valid_synced_web_models(raw_models, target_p):
                        models = []
                        if tier in ("LOGIN_REQUIRED", "CHALLENGE_REQUIRED", "NO_PICKER", "EMPTY_MENU", "CANCELLED"):
                            reason = tier
                        elif tier.startswith("PROBE_ERR:"):
                            reason = tier
                        else:
                            reason = "EMPTY_MENU"
                    else:
                        models = list(raw_models)
                    if models:
                        self._apply_synced_web_models(models, tier, persist=True, provider=target_p)
                        tier_txt = f"{tier} 계정" if tier else f"웹 {short} 계정"
                        self.log(f"[계정 모델 동기화] {tier_txt} 선택 가능 모델 {len(models)}개를 대화·자막 목록에 반영했습니다.")
                    elif ui_cb is None:
                        if reason == "CHALLENGE_REQUIRED":
                            self.log(f"[계정 모델 동기화 안내] 먼저 전용 브라우저에서 {short} 보안 확인(Cloudflare)을 완료한 뒤 [✅ 확인 완료]를 눌러 주세요.")
                        elif reason == "LOGIN_REQUIRED" or self.gemini_mgr.browser.login_mode_active:
                            self.log(f"[계정 모델 동기화 안내] 먼저 전용 브라우저에서 {short} 로그인을 완료한 뒤 [✅ 로그인 완료 확인]을 눌러 주세요.")
                        else:
                            self.log(f"[계정 모델 동기화 안내] 브라우저 모델 목록을 불러오지 못했습니다 ({reason or '응답 없음'}). 기존 목록을 유지합니다.")
                else:
                    api_key = self._get_active_api_key(target_p)
                    api_name = {"gemini": "Gemini", "claude": "Claude", "chatgpt": "OpenAI"}[target_p]
                    if not api_key:
                        self.log(f"[계정 모델 동기화 안내] 공식 {api_name} API 모드에서는 설정에서 {api_name} API Key를 먼저 입력해 주세요.")
                    else:
                        fetched_raw = self.gemini_mgr.get_api_backend(target_p).list_generate_models(api_key)
                        fetched = self._sanitize_api_model_list(fetched_raw, target_p)
                        if fetched:
                            list_key = {"gemini": "api_models", "claude": "claude_api_models", "chatgpt": "openai_api_models"}[target_p]
                            auto_switch_msg = ""
                            with self._cfg_persist_lock:
                                self.cfg[list_key] = list(fetched)
                                if target_p == "gemini":
                                    self._extra_api_models = list(fetched)
                                    cur_gem_model = self._get_active_api_model("gemini")
                                    if cur_gem_model not in fetched:
                                        best_free = pick_best_gemini_free_model(fetched)
                                        self.cfg["gemini_api_model"] = best_free
                                        auto_switch_msg = (
                                            f"[계정 모델 자동 전환] 기존 Gemini 모델 '{cur_gem_model}'이(가) 계정 사용 가능 목록에 없어 "
                                            f"'{best_free}'(으)로 자동 전환했습니다."
                                        )
                                else:
                                    m_cfg_key = "claude_api_model" if target_p == "claude" else "openai_api_model"
                                    cur_m = self._get_active_api_model(target_p)
                                    if cur_m not in fetched:
                                        pref_ids = [code for code, _lbl in get_provider_api_models(target_p)]
                                        best_m = next((c for c in pref_ids if c in fetched), fetched[0])
                                        self.cfg[m_cfg_key] = best_m
                                        auto_switch_msg = (
                                            f"[계정 모델 자동 전환] 기존 {api_name} 모델 '{cur_m}'이(가) 계정 사용 가능 목록에 없어 "
                                            f"'{best_m}'(으)로 자동 전환했습니다."
                                        )
                                save_bridge_config(self.cfg)
                            if auto_switch_msg:
                                self.log(auto_switch_msg)
                            self.log(f"[계정 모델 동기화] {api_name} API 사용 가능 모델 {len(fetched)}개를 반영했습니다.")
                        elif ui_cb is None:
                            self.log(f"[계정 모델 동기화 안내] {api_name} API에서 사용 가능한 모델 목록을 불러오지 못했습니다.")
            except Exception as exc:
                reason = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
                if ui_cb is None:
                    self.log(f"[계정 모델 동기화 오류] {reason}")
            finally:
                self._schedule_ui(_restore_btn)
                self._schedule_ui(self._refresh_companion_badges)
                if ui_cb is not None:
                    try:
                        ui_cb(models, tier, reason)
                    except Exception:
                        pass

        threading.Thread(target=_worker, daemon=True, name="sync-companion-models").start()

    def _refresh_companion_badges(self) -> None:
        self._extra_api_models = self._sanitize_api_model_list(self.cfg.get("api_models", []), "gemini")
        raw_cfg_web = self.cfg.get("web_models")
        if isinstance(raw_cfg_web, list):
            parsed_web = [
                (str(it[0]).strip(), str(it[1]).strip())
                for it in raw_cfg_web
                if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[0]).strip() and str(it[1]).strip()
            ]
            self._account_web_models = parsed_web
            self._extra_web_models = [(c, l) for c, l in parsed_web if c not in GEMINI_BROWSER_MODEL_LABELS]
        else:
            self._account_web_models = []
            self._extra_web_models = []
        self._web_account_tier = str(self.cfg.get("web_account_tier", "") or "").strip()

        p = self._get_ai_provider()
        short = AI_PROVIDER_SHORT_NAMES[p]
        backend = str(self.cfg.get("gemini_backend", "browser"))
        api_model = self._get_active_api_model(p)
        chat_thinking = str(self.cfg.get("gemini_thinking_level", "low") or "low")
        if p == "gemini":
            browser_model, chat_thinking = split_legacy_browser_model(
                str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"),
                chat_thinking,
            )
            self.cfg["gemini_browser_model"] = browser_model
        else:
            browser_model = self._get_active_browser_model(p)
            if chat_thinking not in GEMINI_THINKING_LEVEL_LABELS:
                chat_thinking = "low"
        self.cfg["gemini_thinking_level"] = chat_thinking
        t_short = GEMINI_THINKING_LEVEL_LABELS.get(chat_thinking, chat_thinking)

        if backend == "api":
            api_name = {"gemini": "Gemini API", "claude": "Claude API", "chatgpt": "OpenAI API"}[p]
            badge = f"⚡ {api_name} ({api_model} · {t_short})"
            known_api = get_provider_api_models(p)
            known_api_labels = dict(known_api)
            known_api_codes = {c for c, _ in known_api}
            for extra in self._get_active_extra_api_models(p):
                if extra not in known_api_codes:
                    known_api.append((extra, extra))
                    known_api_codes.add(extra)
            choices = [lbl for _, lbl in known_api]
            cur_val = known_api_labels.get(api_model, api_model)
            cmb_state = "normal"
        else:
            all_web = self._active_web_models(p)
            all_web_labels = {c: l for c, l in all_web}
            if browser_model not in all_web_labels and all_web:
                def_b = get_provider_default_browser_model(p)
                browser_model = def_b if def_b in all_web_labels else all_web[0][0]
                cfg_key = {"gemini": "gemini_browser_model", "claude": "claude_browser_model", "chatgpt": "chatgpt_browser_model"}[p]
                self.cfg[cfg_key] = browser_model
            b_lbl = all_web_labels.get(browser_model, browser_model)
            active_tier = self._get_active_web_tier(p)
            tier_tag = f"[{active_tier}]" if active_tier else ""
            prov_tag = "" if p == "gemini" else f" {short}"
            badge = f"🌐 전용 브라우저{prov_tag}{tier_tag} ({b_lbl.split('(')[0].strip()} · {t_short})"
            choices = [lbl for _, lbl in all_web]
            cur_val = all_web_labels.get(browser_model, choices[0] if choices else "")
            cmb_state = "readonly"
            self.gemini_mgr.browser.selected_model_code = browser_model
            self.gemini_mgr.browser.selected_thinking_level = chat_thinking

        cur_fish_hdr = _safe_cfg_model_header(self.cfg.get("fish_model_header", "s2.1-pro-free"))
        if self._is_colab_s2pro_enabled():
            tts_badge = (
                f"🐟 Fish {cur_fish_hdr} (S2-Pro 폴백)"
                if getattr(self, "_colab_s2pro_offline_warned", False)
                else "☁️ Colab S2-Pro"
            )
        elif self._should_prefer_fish_mcp():
            tts_badge = "🐟 Fish MCP"
        else:
            tts_badge = f"🐟 Fish ({cur_fish_hdr})"
        self._refresh_colab_gpu_release_button()
        self.companion_backend_badge_var.set(f"{badge}  |  🔊 {tts_badge}")
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

        lo = int(round(_safe_cfg_float(self.cfg.get("idle_ping_min_sec", 45.0), 45.0, 5.0, 3600.0)))
        hi = int(round(_safe_cfg_float(self.cfg.get("idle_ping_max_sec", 75.0), 75.0, float(lo), 3600.0)))
        afk_n = _safe_cfg_int(self.cfg.get("afk_max_consecutive_pings", 2), 2, 1, 20)
        self.companion_range_var.set(f"무음 대기: {lo}~{hi}초 랜덤 · {afk_n}회 무응답 시 AFK")

    def _restart_companion_mic_nonblocking(self, mic_lbl: str) -> None:
        if not self.companion_running or not mic_lbl or mic_lbl not in self.in_map:
            return
        mic_idx, mic_sr = self.in_map[mic_lbl]
        fallback_mics = self._build_mic_fallback_candidates(mic_lbl)
        sid = self._companion_session_id

        def _worker() -> None:
            if not self.companion_running or sid != self._companion_session_id:
                return
            try:
                self.mic_stt.stop()
                if not self.companion_running or sid != self._companion_session_id:
                    return
                self._companion_mic_error = ""
                self.mic_stt.start(mic_idx, mic_sr, mic_lbl, fallback_candidates=fallback_mics)
                self.log(f"🎙️ [마이크 실시간 전환] 입력 장치를 '{mic_lbl}'(으)로 즉시 전환했습니다.")
            except Exception as exc:
                safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
                self.log(f"⚠️ [마이크 전환 오류] {safe_exc}")

        threading.Thread(target=_worker, daemon=True, name="switch-companion-mic").start()

    def _close_browser_nonblocking(self) -> None:
        def _worker() -> None:
            try:
                if hasattr(self.gemini_mgr, "browser") and self.gemini_mgr.browser.is_running():
                    self.gemini_mgr.browser.close()
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="close-browser-on-api-switch").start()

    def open_settings_dialog(self, first_run: bool = False, initial_tab: int = 0) -> None:
        saved_flag = [False]
        prev_provider = self._get_ai_provider()
        prev_backend = str(self.cfg.get("gemini_backend", "browser") or "browser")
        prev_bmodel = self._get_active_browser_model(prev_provider)
        prev_bthink = str(self.cfg.get("gemini_thinking_level", "low"))
        prev_steam_dir = str(self.cfg.get("steam_game_dir", "") or "").strip()
        prev_mic = str(self.mic_choice_var.get() or self.cfg.get("mic_input_device", "") or "").strip()
        prev_whisper_model = str(self.cfg.get("whisper_model", "auto") or "auto").strip().lower()
        prev_s2_enabled = bool(self.cfg.get("colab_s2pro_enabled", False))
        prev_s2_url = str(self.cfg.get("colab_s2pro_url", "") or "").strip()
        prev_s2_key = str(self.cfg.get("colab_s2pro_api_key", "") or "").strip()

        def _on_saved(updated_cfg: dict[str, Any]) -> None:
            saved_flag[0] = True
            with self._cfg_persist_lock:
                self.cfg.update(updated_cfg)
            self._tts_error_notice_shown = False
            new_s2_enabled = bool(self.cfg.get("colab_s2pro_enabled", False))
            new_s2_url = str(self.cfg.get("colab_s2pro_url", "") or "").strip()
            new_s2_key = str(self.cfg.get("colab_s2pro_api_key", "") or "").strip()
            if (new_s2_enabled != prev_s2_enabled) or (new_s2_url != prev_s2_url) or (new_s2_key != prev_s2_key):
                self._colab_s2pro_offline_warned = False
                if getattr(self, "colab_s2pro_backend", None) is not None:
                    self.colab_s2pro_backend.reset_cache()
                if self._is_colab_s2pro_enabled():
                    self._prewarm_colab_s2pro_current_char()
            new_provider = normalize_ai_provider(self.cfg.get("ai_provider", "gemini"))
            new_backend = str(self.cfg.get("gemini_backend", "browser") or "browser")
            self.cfg["ai_provider"] = new_provider
            self.ai_provider_var.set(new_provider)
            self._update_ai_provider_toggle_style()
            self._browser_toggle_var.set(f"🌐 {AI_PROVIDER_SHORT_NAMES[new_provider]} 화면 보기")
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
            self._is_tts_cache_enabled()
            self._fish_mcp_fallback_logged = False
            self._fish_mcp_auth_warned = False
            self._fish_mcp_quota_warned = False
            saved_char_voice = self._get_saved_voice_id(self.current_char_id, reply_lang=rlang) or (
                "" if rlang == "ko" else str(self.cfg.get("fish_reference_id", "") or "")
            )
            self.ref_id_var.set(saved_char_voice)
            self.model_hdr_var.set(str(self.cfg.get("fish_model_header", "s2.1-pro-free") or "s2.1-pro-free"))

            smode = str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive")
            self.speed_mode_var.set(SPEED_MODE_LABELS.get(smode, SPEED_MODE_LABELS["expressive"]))
            self.auto_emotion_var.set(bool(self.cfg.get("auto_emotion_directing", True)))
            sboost = _safe_cfg_float(self.cfg.get("expression_boost_offset", 0.0), 0.0, -0.5, 0.5)
            self.boost_preset_var.set(BOOST_PRESET_LABELS[min(BOOST_PRESET_LABELS.keys(), key=lambda k: abs(k - sboost))])
            self.vol_var.set(_safe_cfg_float(self.cfg.get("output_volume", 1.0), 1.0, _OUTPUT_VOLUME_MIN, _OUTPUT_VOLUME_MAX))

            slang = str(self.cfg.get("stt_language", "auto") or "auto")
            self.stt_lang_var.set(STT_LANG_LABELS.get(slang, STT_LANG_LABELS["auto"]))

            p_out = str(self.cfg.get("primary_output_device", "") or "").strip()
            if p_out and p_out in self.out_map:
                self.out_choice_var.set(p_out)
            m_in = str(self.cfg.get("mic_input_device", "") or "").strip()
            if m_in and is_virtual_loopback_mic_label(m_in):
                self.cfg["mic_input_device"] = ""
                m_in = ""
            if m_in and m_in in self.mic_inputs and m_in in self.in_map:
                self.mic_choice_var.set(m_in)
            t_out = str(self.cfg.get("proactive_trigger_device", "") or "").strip()
            if t_out and t_out in self.out_map:
                self.trigger_out_choice_var.set(t_out)

            self.companion_proactive_var.set(bool(self.cfg.get("companion_proactive_enable", True)))
            self.proactive_enable_var.set(bool(self.cfg.get("proactive_enable", True)))
            ptime = _safe_cfg_float(self.cfg.get("proactive_timeout_sec", 18.0), 18.0, 5.0, 3600.0)
            self.proactive_timeout_var.set(PROACTIVE_TIMEOUT_LABELS[min(PROACTIVE_TIMEOUT_LABELS.keys(), key=lambda k: abs(k - ptime))])
            pvol = _safe_cfg_float(self.cfg.get("proactive_trigger_volume", 1.0), 1.0, 0.0, 2.5)
            self.proactive_vol_var.set(PROACTIVE_VOL_LABELS[min(PROACTIVE_VOL_LABELS.keys(), key=lambda k: abs(k - pvol))])

            for _prov in ("gemini", "claude", "chatgpt"):
                self._get_active_api_model(_prov)
            for _prov, _lkey in (
                ("gemini", "api_models"),
                ("claude", "claude_api_models"),
                ("chatgpt", "openai_api_models"),
            ):
                if isinstance(self.cfg.get(_lkey), list):
                    _clean_list = self._sanitize_api_model_list(self.cfg.get(_lkey), _prov)
                    self.cfg[_lkey] = _clean_list
                    if _prov == "gemini":
                        self._extra_api_models = list(_clean_list)
            raw_web = self.cfg.get("web_models")
            if isinstance(raw_web, list):
                parsed_web = [
                    (str(it[0]).strip(), str(it[1]).strip())
                    for it in raw_web
                    if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[0]).strip() and str(it[1]).strip()
                ]
                self._account_web_models = parsed_web
                self._extra_web_models = [(c, l) for c, l in parsed_web if c not in GEMINI_BROWSER_MODEL_LABELS]
            else:
                self._account_web_models = []
                self._extra_web_models = []
            self._web_account_tier = str(self.cfg.get("web_account_tier", "") or "").strip()

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
            self._sync_subtitle_thinking_widget()

            try:
                if hasattr(self, "mode_notebook"):
                    self.mode_notebook.select(1 if self.cfg.get("bridge_mode") == "gemini_live_audio" else 0)
            except Exception:
                pass

            active_new_bmodel = self._get_active_browser_model(new_provider)
            if new_provider != prev_provider or new_backend != prev_backend:
                if new_provider != prev_provider:
                    self.log(f"🤖 [AI 엔진 변경] {AI_PROVIDER_LABELS[new_provider]} 설정이 적용되었습니다.")
                if new_backend != prev_backend:
                    b_lbl = "공식 API" if new_backend == "api" else "전용 브라우저"
                    self.log(f"🔄 [AI 연결 방식 변경] {AI_PROVIDER_SHORT_NAMES[new_provider]} {b_lbl} 모드로 전환합니다.")
                if new_backend == "api" and prev_backend == "browser":
                    self._close_browser_nonblocking()
                    if self.companion_running:
                        self._companion_session_ready = True
                        self._switch_browser_provider_nonblocking(
                            new_provider,
                            active_new_bmodel,
                            relaunch_call=True,
                        )
                    elif new_provider != prev_provider:
                        self._switch_browser_provider_nonblocking(
                            new_provider,
                            active_new_bmodel,
                            relaunch_call=False,
                        )
                else:
                    self._switch_browser_provider_nonblocking(
                        new_provider,
                        active_new_bmodel,
                        relaunch_call=bool(self.companion_running),
                    )
            elif new_backend == "browser" and (
                active_new_bmodel != prev_bmodel or new_bthink != prev_bthink
            ):
                self.gemini_mgr.browser.set_chat_model_async(active_new_bmodel, new_bthink)
            self._sync_calling_quick_bar()
            if new_provider != prev_provider:
                self._on_ai_provider_changed_for_screen_vision(new_provider)
            elif self._sv_available:
                self._refresh_screen_vision_bar_ui()
            self.watchdog.configure(
                enabled=bool(self.proactive_enable_var.get()),
                silence_timeout_sec=_safe_cfg_float(self.cfg.get("proactive_timeout_sec", 18.0), 18.0, 5.0, 3600.0),
            )
            self.companion_sm.configure(
                proactive_enabled=bool(self.companion_proactive_var.get()),
                min_idle_sec=_safe_cfg_float(self.cfg.get("idle_ping_min_sec", 45.0), 45.0, 5.0, 3600.0),
                max_idle_sec=_safe_cfg_float(self.cfg.get("idle_ping_max_sec", 75.0), 75.0, 5.0, 3600.0),
                max_consecutive_pings=_safe_cfg_int(self.cfg.get("afk_max_consecutive_pings", 2), 2, 1, 20),
            )
            self._sync_mic_stt_config()
            new_whisper_model = str(self.cfg.get("whisper_model", "auto") or "auto").strip().lower()
            if new_whisper_model != prev_whisper_model:
                self.log(f"🔄 [Whisper 모델 변경] '{prev_whisper_model}' → '{new_whisper_model}' 백그라운드 전환을 시작합니다...")
                self._preload_whisper_async()
            if (
                self.companion_running
                and m_in
                and m_in != prev_mic
                and m_in in self.mic_inputs
                and m_in in self.in_map
                and not is_virtual_loopback_mic_label(m_in)
            ):
                self._restart_companion_mic_nonblocking(m_in)
            self._refresh_persona_summary_ui()
            self._refresh_companion_badges()
            self._refresh_momotalk_header_and_cards()
            if getattr(self, "_momotalk_sort_mode", "default") == "heart" or bool(getattr(self, "_roster_voice_only", False)):
                self._refresh_character_filter_list(keep_current=True)
            else:
                self._render_roster_visible(force_all=True)
            self._on_active_persona_potentially_changed("설정/커스텀 프롬프트 변경")
            self.log("[환경 설정 적용] %LOCALAPPDATA%\\VoiceCompanionData\\config.json 저장 및 실시간 반영 완료.")

        for _prov in ("gemini", "claude", "chatgpt"):
            self._get_active_api_model(_prov)
        self._extra_api_models = self._sanitize_api_model_list(self.cfg.get("api_models", []), "gemini")
        dlg_kwargs: dict[str, Any] = {
            "on_saved": _on_saved,
            "first_run": first_run,
            "on_open_logs": self.open_status_log_window,
            "extra_api_models": self._extra_api_models,
            "extra_web_models": self._extra_web_models,
            "web_models": self._account_web_models or None,
            "on_sync_web_models": self.sync_companion_models_async,
            "app": self,
            "initial_tab": initial_tab,
            "on_reset_defaults": self.apply_reset_settings_result,
            "on_reset_browser_profile": self.perform_browser_profile_reset,
            "on_screen_vision_saved": self.apply_screen_vision_config,
        }
        try:
            dlg = SettingsDialog(self.root, self.cfg, **dlg_kwargs)
        except TypeError:
            dlg_kwargs.pop("on_screen_vision_saved", None)
            try:
                dlg = SettingsDialog(self.root, self.cfg, **dlg_kwargs)
            except TypeError:
                dlg_kwargs.pop("on_reset_browser_profile", None)
                dlg = SettingsDialog(self.root, self.cfg, **dlg_kwargs)

        def _on_settings_destroyed(ev: Any) -> None:
            if getattr(ev, "widget", None) is not dlg or saved_flag[0] or getattr(self, "_closing", False):
                return
            try:
                cfg_p = self._get_ai_provider()
                if self.gemini_mgr.browser.provider != cfg_p:
                    self._switch_browser_provider_nonblocking(
                        cfg_p,
                        self._get_active_browser_model(cfg_p),
                        relaunch_call=False,
                    )
            except Exception:
                pass

        try:
            dlg.bind("<Destroy>", _on_settings_destroyed, add="+")
        except Exception:
            pass

    def apply_reset_settings_result(self, result: SettingsResetResult) -> None:
        """Apply a `SettingsResetResult` cleanly to all live UI variables, caches, and subsystems (preserving Fish key & Gemini login)."""
        if self.companion_running or self.running or self._call_ui_state != "idle":
            self.stop_momotalk_call()
        self.abort_current_playback()

        prev_whisper_model = str(self.cfg.get("whisper_model", "auto") or "auto").strip().lower()
        with self._cfg_persist_lock:
            self.cfg.clear()
            self.cfg.update(deepcopy(result.config))

        try:
            self.fish_mcp_backend.logout()
        except Exception:
            pass
        self._fish_mcp_fallback_logged = False
        self._fish_mcp_auth_warned = False
        self._fish_mcp_quota_warned = False
        self._tts_error_notice_shown = False
        self._colab_s2pro_offline_warned = False
        if getattr(self, "colab_s2pro_backend", None) is not None:
            self.colab_s2pro_backend.reset_cache()
        self._sub_api_no_key_warned = False
        self._kr_fallback_warned.clear()
        self._recent_tts_jp_lines.clear()
        self._recent_tts_turn_openers.clear()
        self._sticky_ja_hits = 0
        with self._zero_shot_cache_lock:
            self._zero_shot_wav_cache.clear()

        self._extra_api_models = self._sanitize_api_model_list(self.cfg.get("api_models", []), "gemini")
        self._account_web_models = []
        self._extra_web_models = []
        self._web_account_tier = ""

        init_cid = str(self.cfg.get("selected_character_id", "Aru") or "Aru")
        if init_cid not in self.characters:
            init_cid = "Aru" if "Aru" in self.characters else next(iter(self.characters), "Aru")
        init_eid = (
            init_cid
            if init_cid in self.momotalk_entry_map
            else (self.momotalk_entries[0]["entry_id"] if self.momotalk_entries else init_cid)
        )
        self.current_char_id = init_cid
        self.current_momotalk_entry_id = init_eid

        self.school_filter_var.set("전체 학원")
        self.search_var.set("")
        self.temporary_call_var.set(bool(self.cfg.get("temporary_call_mode", False)))
        self._update_ephemeral_toggle_style()

        new_provider = normalize_ai_provider(self.cfg.get("ai_provider", "gemini"))
        self.cfg["ai_provider"] = new_provider
        self.ai_provider_var.set(new_provider)
        self._update_ai_provider_toggle_style()
        self._browser_toggle_var.set(f"🌐 {AI_PROVIDER_SHORT_NAMES[new_provider]} 화면 보기")

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
        self.model_hdr_var.set(_safe_cfg_model_header(self.cfg.get("fish_model_header", "s2.1-pro-free")))

        slang = str(self.cfg.get("stt_language", "auto") or "auto")
        self.stt_lang_var.set(STT_LANG_LABELS.get(slang, STT_LANG_LABELS["auto"]))
        smode = str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive")
        self.speed_mode_var.set(SPEED_MODE_LABELS.get(smode, SPEED_MODE_LABELS["expressive"]))
        self.auto_emotion_var.set(bool(self.cfg.get("auto_emotion_directing", True)))
        sboost = _safe_cfg_float(self.cfg.get("expression_boost_offset", 0.0), 0.0, -0.5, 0.5)
        self.boost_preset_var.set(BOOST_PRESET_LABELS[min(BOOST_PRESET_LABELS.keys(), key=lambda k: abs(k - sboost))])
        self.pause_sec_var.set(_safe_cfg_float(self.cfg.get("pause_threshold_sec", 0.33), 0.33, 0.1, 3.0))
        self.vol_var.set(_safe_cfg_float(self.cfg.get("output_volume", 1.0), 1.0, _OUTPUT_VOLUME_MIN, _OUTPUT_VOLUME_MAX))

        self.companion_proactive_var.set(bool(self.cfg.get("companion_proactive_enable", True)))
        self.proactive_enable_var.set(bool(self.cfg.get("proactive_enable", True)))
        ptime = _safe_cfg_float(self.cfg.get("proactive_timeout_sec", 18.0), 18.0, 5.0, 3600.0)
        self.proactive_timeout_var.set(PROACTIVE_TIMEOUT_LABELS[min(PROACTIVE_TIMEOUT_LABELS.keys(), key=lambda k: abs(k - ptime))])
        pvol = _safe_cfg_float(self.cfg.get("proactive_trigger_volume", 1.0), 1.0, 0.1, 2.0)
        self.proactive_vol_var.set(PROACTIVE_VOL_LABELS[min(PROACTIVE_VOL_LABELS.keys(), key=lambda k: abs(k - pvol))])

        sub_mode = str(self.cfg.get("subtitle_mode", "jp_kr") or "jp_kr")
        if sub_mode not in SUBTITLE_MODES:
            sub_mode = "jp_kr"
        self.subtitle_mode_var.set(sub_mode)
        self._update_subtitle_mode_buttons_style()
        self._apply_subtitle_font_size(
            _safe_cfg_int(self.cfg.get("subtitle_font_size", 11), 11, 9, 18),
            persist=False,
        )

        new_bmodel, new_bthink = split_legacy_browser_model(
            str(self.cfg.get("gemini_browser_model", "hydrogen") or "hydrogen"),
            str(self.cfg.get("gemini_thinking_level", "low") or "low"),
        )
        self.cfg["gemini_browser_model"] = new_bmodel
        self.cfg["gemini_thinking_level"] = new_bthink
        self.companion_thinking_var.set(GEMINI_THINKING_LEVEL_LABELS.get(new_bthink, GEMINI_THINKING_LEVEL_LABELS["low"]))

        sub_choices = self._subtitle_choices()
        if hasattr(self, "cmb_subtitle_engine") and self.cmb_subtitle_engine is not None:
            self.cmb_subtitle_engine.configure(values=[lbl for _, lbl in sub_choices])
        self.subtitle_engine_var.set(
            resolve_subtitle_engine_label(str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline"), sub_choices)
        )
        st_code = str(self.cfg.get("subtitle_thinking_level", "low") or "low")
        self.subtitle_thinking_var.set(GEMINI_THINKING_LEVEL_LABELS.get(st_code, GEMINI_THINKING_LEVEL_LABELS["low"]))
        self._sync_subtitle_thinking_widget()

        try:
            if hasattr(self, "mode_notebook"):
                self.mode_notebook.select(1 if self.cfg.get("bridge_mode") == "gemini_live_audio" else 0)
        except Exception:
            pass

        self.out_choice_var.set("")
        self.mic_choice_var.set("")
        self.in_choice_var.set("")
        self.trigger_out_choice_var.set("")
        self.refresh_audio_devices(reinit_portaudio=False, log_info=False)

        self._is_tts_cache_enabled()
        self.watchdog.configure(
            enabled=bool(self.proactive_enable_var.get()),
            silence_timeout_sec=_safe_cfg_float(self.cfg.get("proactive_timeout_sec", 18.0), 18.0, 5.0, 3600.0),
        )
        self.companion_sm.configure(
            proactive_enabled=bool(self.companion_proactive_var.get()),
            min_idle_sec=_safe_cfg_float(self.cfg.get("idle_ping_min_sec", 45.0), 45.0, 5.0, 3600.0),
            max_idle_sec=_safe_cfg_float(self.cfg.get("idle_ping_max_sec", 75.0), 75.0, 5.0, 3600.0),
            max_consecutive_pings=_safe_cfg_int(self.cfg.get("afk_max_consecutive_pings", 2), 2, 1, 20),
        )
        self._sync_mic_stt_config()
        self.mic_stt.set_character_context(self.current_char)
        new_whisper_model = str(self.cfg.get("whisper_model", "auto") or "auto").strip().lower()
        if new_whisper_model != prev_whisper_model:
            self._preload_whisper_async()

        clear_steam_voice_availability_cache()
        if hasattr(self, "momotalk_assets") and self.momotalk_assets is not None:
            self.momotalk_assets.set_ba_root_hint(None)
            self._start_momotalk_assets_index_async()

        # Reset Gemini runtime target without touching BrowserProfile (login cookies preserved!)
        self.gemini_mgr.invalidate_session()
        self.gemini_mgr.browser.selected_model_code = new_bmodel
        self.gemini_mgr.browser.selected_thinking_level = new_bthink
        if not self._try_fast_set_provider_if_idle(new_provider, new_bmodel):
            self._switch_browser_provider_nonblocking(new_provider, new_bmodel, relaunch_call=False)
        if hasattr(self.gemini_mgr, "reload_selectors"):
            def _reload_sel() -> None:
                try:
                    self.gemini_mgr.reload_selectors()
                except Exception:
                    pass

            threading.Thread(target=_reload_sel, daemon=True, name="reload-selectors-reset").start()

        self._clear_subtitle_translator_context()
        self._sync_calling_quick_bar()
        self._refresh_persona_summary_ui()
        self._refresh_companion_badges()
        self._refresh_character_filter_list(keep_current=True)
        self._refresh_momotalk_header_and_cards()
        self._last_active_persona_key = self._resolve_active_persona_for_gemini()

        fish_st = "유지됨" if result.fish_api_key_preserved else "저장된 키 없음"
        gem_st = "유지됨(BrowserProfile 보존)" if result.browser_profile_exists else "유지됨(웹 로그인 기본 모드)"
        bak_part = f" | 백업: {result.backup_dir}" if result.backup_dir is not None else ""
        self.log(f"🔄 [설정 기본값 복원 완료] Fish API 키: {fish_st} | AI 브라우저 로그인 세션: {gem_st}{bak_part}")

    def perform_browser_profile_reset(self) -> tuple[bool, str]:
        """Gracefully close the AI browser and clear BrowserProfile cookies/cache without touching Fish key or memories."""
        if self.companion_running or self.running or self._call_ui_state != "idle":
            self.stop_momotalk_call()
        self.abort_current_playback()
        self._abort_active_browser_prompt()
        self.gemini_mgr.invalidate_session()
        try:
            close_thr = threading.Thread(target=self.gemini_mgr.browser.close, daemon=True, name="browser-close-reset-profile")
            close_thr.start()
            close_thr.join(timeout=3.0)
        except Exception:
            pass
        ok, msg = _call_reset_browser_profile()
        if ok:
            self._account_web_models = []
            self._extra_web_models = []
            self._web_account_tier = ""
            with self._cfg_persist_lock:
                for k in ("web_models", "claude_web_models", "chatgpt_web_models"):
                    self.cfg[k] = []
                for k in ("web_account_tier", "claude_web_account_tier", "chatgpt_web_account_tier"):
                    self.cfg[k] = ""
                save_bridge_config(self.cfg)
            self._refresh_companion_badges()
            self.log(f"🌐 [브라우저 프로필 초기화 완료] {msg}")
        else:
            self.log(f"⚠️ [브라우저 프로필 초기화 실패] {msg}")
        return ok, msg

    def reset_browser_profile_ui(self, parent: Any = None) -> tuple[bool, str] | None:
        """Confirm and clear AI dedicated BrowserProfile (Gemini/Claude/ChatGPT web cookies/cache)."""
        owner = parent if parent is not None else self.root
        confirm_msg = (
            "AI 전용 브라우저의 로그인 쿠키·캐시 폴더(BrowserProfile)를 초기화합니다.\n"
            "Cloudflare 무한 반복, 로그인 오류, 프로필 잠금 오류 해결에 효과적입니다.\n\n"
            "【유지되는 항목】\n"
            "• ✅ Fish Audio API 키 및 모든 앱 설정\n"
            "• ✅ 대화 기억 노트 및 선생님 프로필 (memory)\n"
            "• ✅ 평소 사용하시는 일반 Chrome / Edge 브라우저\n\n"
            "【초기화되는 항목】\n"
            "• 🌐 전용 브라우저 내 AI 웹 로그인 세션 (Gemini · Claude · ChatGPT)\n"
            "  (다음 통화 시 해당 AI 웹 계정에 다시 한 번 로그인하시면 됩니다.)\n\n"
            "지금 브라우저 프로필을 초기화하시겠습니까?"
        )
        if not messagebox.askyesno("🌐 브라우저 로그인 프로필 초기화", confirm_msg, parent=owner):
            return None
        ok, msg = self.perform_browser_profile_reset()
        if ok:
            messagebox.showinfo("🌐 브라우저 프로필 초기화 완료", msg, parent=owner)
        else:
            messagebox.showwarning("브라우저 프로필 초기화 실패", msg, parent=owner)
        return ok, msg

    def reset_settings_to_defaults_ui(self, parent: Any = None) -> SettingsResetResult | None:
        """Confirm and restore all user settings to defaults while preserving Fish Audio API key & AI browser login."""
        owner = parent if parent is not None else self.root
        confirm_msg = (
            "사용자 설정 오류나 버그가 발생한 경우 모든 앱 설정을 초기 기본값으로 복원합니다.\n\n"
            "【유지되는 항목】\n"
            "• ✅ Fish Audio API 키\n"
            "• ✅ AI 전용 브라우저 로그인 세션 (Gemini·Claude·ChatGPT · BrowserProfile)\n"
            "• ✅ 대화 기억 노트 및 선생님 프로필 (memory)\n\n"
            "【기본값으로 초기화되는 항목】\n"
            "• 🔁 AI 엔진(제미니 웹 기본)·모델·추론·자막·프롬프트·보이스 링크·오디오 장치·음량·선톡 타이머\n"
            "• 🔁 기타 API 키 (Gemini/Claude/OpenAI API 키, Fish MCP OAuth, Colab S2-Pro 토큰)\n"
            "• 🔁 사용자 selectors.json 편집본 및 실시간 RVC 설정\n\n"
            "💾 복원 전 현재 설정은 로컬 backups 폴더에 자동 백업됩니다.\n"
            "정말 기본값으로 복원하시겠습니까?"
        )
        if not messagebox.askyesno("🔄 설정 기본값 복원", confirm_msg, parent=owner):
            return None

        live_snapshot = deepcopy(self.cfg)
        typed_fish_key = str(self._read_tk_var_safe("api_key_var", "") or "").strip()
        if typed_fish_key:
            live_snapshot["fish_api_key"] = typed_fish_key

        try:
            result = reset_settings_to_defaults(
                app_root=ROOT_DIR,
                current_cfg=live_snapshot,
                reason="manual_reset",
                preserve_fish_api_key=True,
                reset_rvc_config=True,
                reset_user_selectors=True,
            )
        except Exception as exc:
            safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
            messagebox.showerror("기본값 복원 실패", safe_exc, parent=owner)
            return None

        self.apply_reset_settings_result(result)
        fish_status = "유지됨" if result.fish_api_key_preserved else "저장된 키 없음"
        gemini_status = (
            "유지됨 (BrowserProfile 보존)"
            if result.browser_profile_exists
            else "유지됨 (기본 웹 로그인 모드)"
        )
        summary_lines = [
            "설정을 기본값으로 안전하게 복원했습니다.",
            "",
            f"• Fish Audio API 키: {fish_status}",
            f"• AI 브라우저 로그인 세션: {gemini_status}",
        ]
        if result.backup_dir is not None:
            summary_lines.append(f"• 자동 백업 경로: {result.backup_dir}")
        if result.warnings:
            summary_lines.append("")
            summary_lines.append("참고: " + "; ".join(result.warnings[:3]))
        messagebox.showinfo("🔄 기본값 복원 완료", "\n".join(summary_lines), parent=owner)
        return result

    def _on_companion_proactive_toggled(self) -> None:
        self.save_settings_ui(silent=True)

    def _resolve_active_persona_for_gemini(self, *, include_variant_block: bool = True) -> tuple[str, bool]:
        """Return (persona_prompt, use_japanese_lock)."""
        rlang = self._get_reply_language()
        is_ko = rlang == "ko"
        coach_on = bool(
            self._read_tk_var_safe("jp_beginner_coach_var", bool(self.cfg.get("jp_beginner_lover_mode", False)))
        ) and (not is_ko)
        if bool(self.cfg.get("use_custom_persona", False)):
            custom = str(self.cfg.get("custom_persona_prompt") or "").strip()
            if is_ko:
                return f"{custom}\n\n[답변 언어 설정: 한국어]\n반드시 자연스러운 한국어로만 대답하세요.", False
            if coach_on:
                return build_jp_beginner_lover_coach_prompt(None, custom), True
            return custom, False
        c = self.current_char
        cfg_vcode = str(self.cfg.get("persona_prompt_variant", "official") or "official")
        pvar_lbl = self._read_tk_var_safe(
            "persona_variant_var",
            PERSONA_VARIANT_LABELS.get(cfg_vcode, PERSONA_VARIANT_LABELS["official"]),
        )
        vcode = PERSONA_LABEL_TO_CODE.get(str(pvar_lbl), cfg_vcode if cfg_vcode in PERSONA_VARIANT_LABELS else "official")
        prompt, _ = resolve_voice_persona_prompt(
            c,
            vcode,
            include_enhancement=include_variant_block,
            include_dna=True,
            reply_lang=rlang,
        )
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
            ko_guide = build_korean_voice_acting_guide(c, persona_variant=vcode, include_samples=True)
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
            cur_short = AI_PROVIDER_SHORT_NAMES[self._get_ai_provider()]
            self.log(f"🔄 [{reason_kr}] 이전 대화 기록과 섞이지 않도록 새 {cur_short} 채팅방을 열고 첫인사를 다시 시작합니다.")
            self.reset_companion_conversation_async()
        elif self.companion_running:
            # Character/variant/language changed while browser boot/login is still in progress:
            # sync memory session & context immediately so Turn 1 never leaks the old student's memories!
            self._clear_subtitle_translator_context()
            self.gemini_mgr.invalidate_session()
            cur_base_cid, cur_track = self._get_active_memory_key()
            cur_rlang = self._get_reply_language()
            cur_vcode = (
                "custom"
                if bool(self.cfg.get("use_custom_persona", False))
                else PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")
            )
            with self._active_memory_session_lock:
                active_sess = dict(self._active_memory_session) if self._active_memory_session else None
            if (not active_sess) or (
                active_sess.get("base_cid") != cur_base_cid
                or active_sess.get("track") != cur_track
                or active_sess.get("persona_variant") != cur_vcode
                or active_sess.get("reply_language") != cur_rlang
            ):
                self._finalize_active_memory_session(run_summary=True)
                self._start_active_memory_session()
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
        recent_lines = list(self._recent_tts_jp_lines)[-4:]
        if not recent_lines:
            return False
        norm_lines = [self._normalize_echo_text(ln) for ln in recent_lines]
        norm_lines = [ln for ln in norm_lines if ln]
        if not norm_lines:
            return False
        # Short 2-3 char replies (e.g. "はい", "うん", "大丈夫") are frequent real user answers;
        # only classify as echo if they match an entire recent short AI clause, not an arbitrary substring.
        if len(norm_in) < 4:
            return any(norm_in == ln for ln in norm_lines)
        recent_blob = "".join(norm_lines)
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
            self._abort_active_browser_prompt()
            self.log(f"🎙️ [끼어들기 반영] 캐릭터 발화·생성 중단 후 새 발화 즉시 전송: {clean}")

        merged = ""
        enqueued = False
        with self._turn_interrupt_lock:
            confirmed = self.companion_sm.on_user_speech_confirmed(now)
            if confirmed:
                confirmed_seq = self.companion_sm.current_turn_seq
                pending_users: list[str] = []
                while True:
                    try:
                        q_kind, q_payload = self._companion_turn_queue.get_nowait()
                        if q_kind == "user" and str(q_payload).strip():
                            pending_users.append(str(q_payload).strip())
                    except queue.Empty:
                        break
                merged = " ".join([*pending_users, clean]).strip()
                enqueued = self._enqueue_companion_turn("user", merged, revert_turn_seq=confirmed_seq)
        if confirmed:
            if enqueued:
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
                if self._screen_watcher is not None:
                    try:
                        self._screen_watcher.mark_turn_activity()
                    except Exception:
                        pass
            else:
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
        self._schedule_ui(
            lambda m=msg: (
                self.companion_state_var.set(f"⚠️ {m}"),
                self.lbl_companion_state.config(fg="#dc2626"),
            )
        )

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
            if "wdm-ks" not in lbl.lower()
            and not is_virtual_loopback_mic_label(lbl)
            and _norm_dev_key(lbl) == target_key
        ]
        same_dev.sort(key=lambda item: (_api_rank(item[2]), item[0]))
        for idx, sr, lbl in same_dev:
            if idx not in seen_idx:
                seen_idx.add(idx)
                candidates.append((idx, int(sr), lbl))

        for lbl in self.mic_inputs:
            if is_virtual_loopback_mic_label(lbl):
                continue
            if lbl in self.in_map:
                idx, sr = self.in_map[lbl]
                if idx not in seen_idx:
                    seen_idx.add(idx)
                    candidates.append((idx, int(sr), lbl))

        candidates.append((None, 44100, "Windows 기본 마이크"))
        return candidates

    def toggle_gemini_browser_window(
        self,
        provider_override: str | None = None,
        *,
        force_visible: bool | None = None,
    ) -> None:
        provider = normalize_ai_provider(provider_override or self._get_ai_provider())
        short = AI_PROVIDER_SHORT_NAMES[provider]
        is_active_app_provider = provider == self._get_ai_provider()

        def _worker() -> None:
            # Lock-free fast path: when browser is already running for this provider,
            # show/toggle HWND visibility directly without acquiring _io_lock via set_provider().
            if self._is_browser_provider_active(provider):
                new_vis = bool(force_visible) if force_visible is not None else (not self.gemini_mgr.browser.window_visible)
                self.gemini_mgr.browser.set_window_visible(new_vis)
                lbl = f"🌐 {short} 화면 숨기기" if new_vis else f"🌐 {short} 화면 보기"
                if is_active_app_provider:
                    self._schedule_ui(lambda l=lbl: self._browser_toggle_var.set(l))
                self.log(f"[전용 브라우저] {short} 창 {'표시' if new_vis else '백그라운드 숨김'} 전환 완료.")
                return

            if not self._is_browser_provider_active(provider):
                self._abort_active_browser_prompt()
                self.gemini_mgr.set_provider(provider, browser_model=self._get_active_browser_model(provider))
            if self.gemini_mgr.browser.is_running():
                new_vis = bool(force_visible) if force_visible is not None else (not self.gemini_mgr.browser.window_visible)
                self.gemini_mgr.browser.set_window_visible(new_vis)
                lbl = f"🌐 {short} 화면 숨기기" if new_vis else f"🌐 {short} 화면 보기"
                if is_active_app_provider:
                    self._schedule_ui(lambda l=lbl: self._browser_toggle_var.set(l))
                self.log(f"[전용 브라우저] {short} 창 {'표시' if new_vis else '백그라운드 숨김'} 전환 완료.")
            else:
                site_url = {"gemini": "gemini.google.com", "claude": "claude.ai", "chatgpt": "chatgpt.com"}[provider]
                self.log(f"[전용 브라우저] {site_url} 세션을 여는 중입니다...")
                ok, reason = self._ensure_browser_ready_and_check_login(show_if_ready=True)
                if ok:
                    if is_active_app_provider:
                        self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 숨기기"))
                    self.log(f"[전용 브라우저] {short} 로그인 세션 확인 완료 (표시 상태).")
                    if not self.companion_running:
                        self._discover_web_models_async(provider=provider)
                elif reason == "CHALLENGE_REQUIRED":
                    if is_active_app_provider:
                        self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 숨기기"))
                    self.log(f"[{short} 보안 확인 필요] 열린 브라우저 창에서 Cloudflare 사람 확인을 완료한 뒤 [✅ 확인 완료]를 눌러 주세요.")
                elif reason == "LOGIN_REQUIRED":
                    if is_active_app_provider:
                        self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 숨기기"))
                    self.log(f"[{short} 로그인 필요] 열린 브라우저 창에서 로그인 후 [✅ 로그인 완료 확인]을 눌러 주세요.")
                elif reason not in ("CANCELLED", "ABORTED"):
                    safe_r = _redact_secrets(str(reason), extra_secrets=self._active_secret_values())
                    self.log(f"[전용 브라우저 오류] {safe_r}")

        threading.Thread(target=_worker, daemon=True, name="toggle-browser-win").start()

    def confirm_browser_login_async(self, provider_override: str | None = None) -> None:
        provider = normalize_ai_provider(provider_override or self._get_ai_provider())
        short = AI_PROVIDER_SHORT_NAMES[provider]
        is_active_app_provider = provider == self._get_ai_provider()

        with self._login_confirm_lock:
            if self._login_confirm_inflight:
                self.log(f"[로그인 확인] 이미 {short} 로그인 검증이 진행 중입니다. 잠시만 기다려 주세요.")
                return
            self._login_confirm_inflight = True

        session_id = self._companion_session_id
        was_companion_running = bool(self.companion_running)
        session_cancel_ev = self._companion_cancel_event if was_companion_running else None
        if was_companion_running and is_active_app_provider and self._call_ui_state == "calling":
            self._schedule_ui(
                lambda sid=session_id, s=short: self._set_calling_stage(
                    f"🔄 {s} 로그인/보안 상태를 확인하는 중...",
                    fg="#FDE68A",
                    session_id=sid,
                )
            )

        def _worker() -> None:
            try:
                self.log(f"[로그인 확인] {short} 로그인 세션을 검증하고 백그라운드로 전환합니다...")
                # Cancel any in-flight turn & abort active browser prompt BEFORE acquiring _io_lock
                if self.companion_running and is_active_app_provider:
                    self._cancel_active_companion_turn(drain_queue=True, abort_browser=True)
                else:
                    self._abort_active_browser_prompt()

                if self._closing:
                    return
                if was_companion_running and (
                    (not self.companion_running)
                    or session_id != self._companion_session_id
                    or (session_cancel_ev is not None and session_cancel_ev.is_set())
                ):
                    self.log(f"[로그인 확인 중단] 통화가 종료되어 {short} 브라우저 재시작을 취소했습니다.")
                    return

                self.gemini_mgr.set_provider(provider, browser_model=self._get_active_browser_model(provider))
                if was_companion_running and (
                    (not self.companion_running)
                    or session_id != self._companion_session_id
                    or (session_cancel_ev is not None and session_cancel_ev.is_set())
                ):
                    self.log(f"[로그인 확인 중단] 통화가 종료되어 {short} 브라우저 재시작을 취소했습니다.")
                    return
                complete_fn = (
                    self.gemini_mgr.complete_interactive_login
                    if hasattr(self.gemini_mgr, "complete_interactive_login")
                    else self.gemini_mgr.browser.complete_interactive_login
                )
                try:
                    ok, reason = complete_fn(cancel_event=session_cancel_ev)
                except TypeError:
                    ok, reason = complete_fn()
                if self._closing:
                    return

                if ok:
                    if is_active_app_provider:
                        self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 보기"))
                    self.log("[로그인 완료] 세션이 %LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile 에 안전하게 저장되었습니다!")
                    if was_companion_running:
                        if (
                            self.companion_running
                            and session_id == self._companion_session_id
                            and self._get_ai_provider() == provider
                        ):
                            self._schedule_ui(lambda: self._set_calling_login_required(False))
                            self._schedule_ui(self._clear_calling_error)
                            self._companion_session_ready = True
                            if self._call_ui_state == "calling":
                                self._calling_prime_started_mono = time.monotonic()
                                self._calling_delay_hint_shown = False
                                self._schedule_ui(
                                    lambda sid=session_id, s=short: self._set_calling_stage(
                                        f"🧠 {s} 첫인사 응답 기다리는 중...",
                                        session_id=sid,
                                    )
                                )
                                self._ringback_state.resume()
                                with self._turn_interrupt_lock:
                                    self._drain_companion_turn_queue()
                                    self.companion_sm.on_opening_greeting_started(time.monotonic())
                                    prime_seq = self.companion_sm.current_turn_seq
                                    self._enqueue_companion_turn("prime", "", revert_turn_seq=prime_seq)
                            else:
                                self.gemini_mgr.invalidate_session()
                                self.log(f"✅ [{short} 복구 완료] 브라우저 세션이 복구되었습니다. 계속 대화하실 수 있습니다.")
                        else:
                            self.log(f"[로그인 완료] 통화가 이미 종료되어 {short} 첫인사를 생략하고 세션을 보관했습니다.")
                    else:
                        self._schedule_ui(lambda: self._set_calling_login_required(False))
                        if not self.companion_running:
                            self._discover_web_models_async(provider=provider)
                elif reason not in ("CANCELLED", "ABORTED"):
                    safe_reason = _redact_secrets(str(reason), extra_secrets=self._active_secret_values())
                    is_challenge = reason == "CHALLENGE_REQUIRED" or "challenge" in safe_reason.lower()
                    if (
                        was_companion_running
                        and self.companion_running
                        and session_id == self._companion_session_id
                        and self._get_ai_provider() == provider
                    ):
                        self.gemini_mgr.browser.set_window_visible(True)
                        self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 숨기기"))
                        self._schedule_ui(
                            lambda sid=session_id, pr=provider, k=("challenge" if is_challenge else "login"): self._enter_auth_recovery_ui(
                                sid,
                                pr,
                                kind=k,
                            )
                        )
                    if is_challenge:
                        self.log(
                            f"[보안 확인 미완료] 아직 {short} 보안 확인(Cloudflare) 화면입니다 ({safe_reason}). 브라우저 창에서 사람 확인을 마친 뒤 다시 눌러 주세요."
                        )
                    else:
                        self.log(
                            f"[로그인 미완료] 아직 {short} 로그인 화면입니다 ({safe_reason}). 브라우저 창에서 로그인을 마친 뒤 다시 눌러 주세요."
                        )
            except Exception as exc:
                safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
                self.log(f"[로그인 확인 오류] {safe_exc}")
            finally:
                with self._login_confirm_lock:
                    self._login_confirm_inflight = False

        threading.Thread(target=_worker, daemon=True, name="confirm-browser-login").start()

    def _drain_companion_turn_queue(self) -> None:
        while not self._companion_turn_queue.empty():
            try:
                self._companion_turn_queue.get_nowait()
            except queue.Empty:
                break

    def _enqueue_companion_turn(
        self,
        kind: str,
        payload: Any,
        *,
        revert_turn_seq: int | None = None,
    ) -> bool:
        try:
            self._companion_turn_queue.put_nowait((kind, payload))
            self._companion_turn_wakeup.set()
            return True
        except queue.Full:
            if revert_turn_seq is not None:
                try:
                    self.companion_sm.on_turn_finished(
                        time.monotonic(),
                        expected_turn_seq=revert_turn_seq,
                    )
                except Exception:
                    pass
            return False

    def reset_companion_conversation_async(self) -> None:
        provider = self._get_ai_provider()
        backend = str(self.cfg.get("gemini_backend", "browser"))
        self._clear_subtitle_translator_context()
        if not self.companion_running:
            self.gemini_mgr.invalidate_session()
            self._abort_active_browser_prompt()
            threading.Thread(
                target=lambda b=backend, p=provider: self.gemini_mgr.reset_conversation(b, provider=p),
                daemon=True,
                name="gemini-reset-chat",
            ).start()
            self.log("[이번 통화 새 주제로] 저장된 장기 기억은 유지한 채 다음 시작 시 새 채팅방에서 시작합니다.")
            return
        self._cancel_active_companion_turn(drain_queue=True, abort_browser=True)
        self._recent_tts_jp_lines.clear()
        self._recent_tts_turn_openers.clear()
        self.gemini_mgr.invalidate_session()
        cur_base_cid, cur_track = self._get_active_memory_key()
        cur_rlang = self._get_reply_language()
        cur_vcode = (
            "custom"
            if bool(self.cfg.get("use_custom_persona", False))
            else PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get(), "official")
        )
        with self._active_memory_session_lock:
            active_sess = dict(self._active_memory_session) if self._active_memory_session else None
        if active_sess and (
            active_sess.get("base_cid") != cur_base_cid
            or active_sess.get("track") != cur_track
            or active_sess.get("persona_variant") != cur_vcode
            or active_sess.get("reply_language") != cur_rlang
        ):
            self._finalize_active_memory_session(run_summary=True)
            self._start_active_memory_session()
        elif not bool(self.temporary_call_var.get()):
            mem_ctx, _ = _call_build_memory_context_block(
                self.memory_mgr,
                base_cid=cur_base_cid,
                track=cur_track,
                char_data=None if cur_base_cid == "__custom__" else self.current_char,
                current_variant=cur_vcode,
                reply_lang=cur_rlang,
            )
            self._active_call_memory_context = mem_ctx
        if backend == "api":
            self.gemini_mgr.reset_conversation(backend, provider=provider)
        self.log("🔄 [이번 통화 새 주제로] 저장된 장기 기억은 유지한 채 현재 통화만 새 화제로 다시 시작합니다.")
        with self._turn_interrupt_lock:
            self.companion_sm.on_opening_greeting_started(time.monotonic())
            prime_seq = self.companion_sm.current_turn_seq
            self._enqueue_companion_turn("prime", "", revert_turn_seq=prime_seq)

    def retry_companion_call_async(self) -> None:
        """Retry connecting / greeting from the calling error state without losing the active call session."""
        if not self.companion_running:
            self.start_momotalk_call()
            return
        if self._call_ui_state != "calling":
            self.reset_companion_conversation_async()
            return
        session_id = self._companion_session_id
        cancel_ev = self._companion_cancel_event
        provider = self._get_ai_provider()
        short = AI_PROVIDER_SHORT_NAMES[provider]
        backend = str(self.cfg.get("gemini_backend", "browser"))

        self._clear_calling_error()
        self._set_calling_login_required(False)
        self._calling_delay_hint_shown = False
        self._cancel_active_companion_turn(drain_queue=True, abort_browser=True)
        self.gemini_mgr.invalidate_session()
        self._ringback_state.start()

        if backend == "browser" and not self._companion_session_ready:
            self._calling_prime_started_mono = 0.0
            self._set_calling_stage(f"🌐 {short} 전용 브라우저 다시 연결 중...", session_id=session_id)
            self._launch_companion_boot_thread(
                session_id=session_id,
                cancel_ev=cancel_ev,
                provider=provider,
                backend=backend,
            )
            return

        if backend == "api":
            self.gemini_mgr.reset_conversation(backend, provider=provider)
        self._calling_prime_started_mono = time.monotonic()
        self._set_calling_stage(
            f"🧠 {short} 첫인사 응답 다시 기다리는 중..." if backend == "browser" else f"⚡ {short} API 첫인사 응답 다시 기다리는 중...",
            session_id=session_id,
        )
        self.log(f"🔄 [{short} 다시 시도] 첫인사 연결을 다시 시도합니다...")
        with self._turn_interrupt_lock:
            self.companion_sm.on_opening_greeting_started(time.monotonic())
            prime_seq = self.companion_sm.current_turn_seq
            self._enqueue_companion_turn("prime", "", revert_turn_seq=prime_seq)

    def _launch_companion_boot_thread(
        self,
        *,
        session_id: int,
        cancel_ev: threading.Event,
        provider: str,
        backend: str,
    ) -> None:
        short = AI_PROVIDER_SHORT_NAMES[provider]

        def _boot_gemini() -> None:
            if self._closing or (not self.companion_running) or session_id != self._companion_session_id or cancel_ev.is_set():
                return
            if backend == "browser":
                self.log(f"[모드 1 시작] 전용 브라우저 {short} 세션을 준비 중입니다...")
                self._schedule_ui(
                    lambda sid=session_id, s=short: self._set_calling_stage(
                        f"🌐 {s} 전용 브라우저 준비 중...",
                        session_id=sid,
                    )
                )
                try:
                    self.gemini_mgr.set_provider(provider, browser_model=self._get_active_browser_model(provider))
                except Exception as exc:
                    if session_id != self._companion_session_id or cancel_ev.is_set() or not self.companion_running:
                        return
                    safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
                    self.log(f"[{short} 브라우저 오류] {safe_exc}")
                    self._ringback_state.stop()
                    self._schedule_ui(
                        lambda sid=session_id, pr=provider, d=safe_exc: self._enter_calling_error_ui(
                            sid,
                            pr,
                            backend="browser",
                            title=f"⚠️ {AI_PROVIDER_SHORT_NAMES[pr]} 브라우저 시작 실패",
                            detail=d,
                        )
                    )
                    return

                if self._closing or (not self.companion_running) or session_id != self._companion_session_id or cancel_ev.is_set():
                    return

                ok, reason = self._ensure_browser_ready_and_check_login(
                    show_if_ready=False,
                    cancel_event=cancel_ev,
                )
                if self._closing or (not self.companion_running) or session_id != self._companion_session_id or cancel_ev.is_set():
                    return
                if not ok:
                    if reason in ("CANCELLED", "ABORTED"):
                        return
                    if reason in ("LOGIN_REQUIRED", "CHALLENGE_REQUIRED"):
                        auth_kind = "challenge" if reason == "CHALLENGE_REQUIRED" else "login"
                        self._ringback_state.pause()
                        self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 숨기기"))
                        self._schedule_ui(
                            lambda sid=session_id, pr=provider, k=auth_kind: self._enter_auth_recovery_ui(
                                sid,
                                pr,
                                kind=k,
                            )
                        )
                        if auth_kind == "challenge":
                            self.log(
                                f"🛡️ [{short} 보안 확인 안내] 열린 브라우저 창에서 Cloudflare 사람 확인을 완료한 뒤 [✅ 확인 완료] 버튼을 눌러 주세요!"
                            )
                        else:
                            self.log(
                                f"🔑 [{short} 로그인 안내] 열린 브라우저 창에서 {short} 로그인을 완료한 뒤 [✅ 로그인 확인] 버튼을 눌러 주세요!"
                            )
                    else:
                        safe_reason = _redact_secrets(str(reason), extra_secrets=self._active_secret_values())
                        self.log(f"[{short} 브라우저 오류] {safe_reason}")
                        self._ringback_state.stop()
                        self._schedule_ui(
                            lambda sid=session_id, pr=provider, d=safe_reason: self._enter_calling_error_ui(
                                sid,
                                pr,
                                backend="browser",
                                title=f"⚠️ {AI_PROVIDER_SHORT_NAMES[pr]} 브라우저 연결 실패",
                                detail=d,
                            )
                        )
                    return
                self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 보기"))
                self._schedule_ui(lambda: self._set_calling_login_required(False))

            if (
                (not self._closing)
                and self.companion_running
                and session_id == self._companion_session_id
                and (not cancel_ev.is_set())
            ):
                self._companion_session_ready = True
                self._calling_prime_started_mono = time.monotonic()
                self._calling_delay_hint_shown = False
                self._schedule_ui(
                    lambda sid=session_id, s=short, b=backend: self._set_calling_stage(
                        f"🧠 {s} 첫인사 응답 기다리는 중..." if b == "browser" else f"⚡ {s} API 첫인사 응답 기다리는 중...",
                        session_id=sid,
                    )
                )
                self._schedule_pending_memory_recovery_async()
                with self._turn_interrupt_lock:
                    self._drain_companion_turn_queue()
                    self.companion_sm.on_opening_greeting_started(time.monotonic())
                    prime_seq = self.companion_sm.current_turn_seq
                    self._enqueue_companion_turn("prime", "", revert_turn_seq=prime_seq)

        threading.Thread(target=_boot_gemini, daemon=True, name=f"boot-companion-{session_id}").start()

    def trigger_manual_companion_ping_async(self) -> None:
        if not self.companion_running:
            messagebox.showinfo("모드 1 대기 중", "먼저 [▶️ AI 보이스 대화 시작]을 눌러 대화를 시작해 주세요.")
            return
        short = AI_PROVIDER_SHORT_NAMES[self._get_ai_provider()]
        with self._turn_interrupt_lock:
            triggered = self.companion_sm.trigger_manual_ping(time.monotonic())
            if triggered:
                ping_seq = self.companion_sm.current_turn_seq
                enqueued = self._enqueue_companion_turn("ping", IDLE_PING_TOKEN, revert_turn_seq=ping_seq)
            else:
                enqueued = False
        if triggered:
            if enqueued:
                self.log(f"💬 [수동 선톡 테스트] {short}에게 '[IDLE_PING]'을 전송합니다!")
            else:
                self.log("[수동 선톡 대기] 대화 대기열이 가득 차 있어 잠시 후 다시 시도해 주세요.")
        else:
            st = self.companion_sm.get_status().kr_label
            self.log(f"[수동 선톡 대기] 현재 상태({st})가 완료된 후 다시 시도해 주세요.")

    def toggle_companion_mode(self) -> None:
        if self.companion_running:
            stop_short = AI_PROVIDER_SHORT_NAMES[self._get_ai_provider()]
            with self._companion_session_lock:
                self._companion_session_id += 1
                self._audio_session_id += 1
            self.companion_running = False
            self._companion_session_ready = False
            self._companion_mic_error = ""
            self._companion_cancel_event.set()
            with self._turn_interrupt_lock:
                self._active_turn_interrupt.set()
                self._drain_companion_turn_queue()
            self._companion_turn_wakeup.set()
            self._abort_active_browser_prompt()
            self._barge_in_ducking = False
            self._recent_tts_jp_lines.clear()
            self._recent_tts_turn_openers.clear()
            self._clear_subtitle_translator_context()
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
            self.log(f"[모드 1 중지] PC 마이크 + {stop_short} 보이스 대화를 종료했습니다.")
            return

        if self.running:
            self.toggle_bridge()

        self.refresh_audio_devices(reinit_portaudio=True, log_info=False)
        self.save_settings_ui(silent=True)

        if not self._has_fish_tts_credentials():
            messagebox.showwarning(
                "Fish Audio 인증 필요",
                "먼저 ⚙️ 설정 창에서 Fish Audio API Key를 입력하거나 공식 MCP 웹 크레딧(OAuth)에 로그인해 주세요.",
            )
            self.open_settings_dialog(first_run=True, initial_tab=1)
            return

        provider = self._get_ai_provider()
        short = AI_PROVIDER_SHORT_NAMES[provider]
        backend = str(self.cfg.get("gemini_backend", "browser"))
        if backend == "api" and not self._get_active_api_key(provider):
            api_lbl = {"gemini": "Gemini", "claude": "Claude", "chatgpt": "OpenAI(ChatGPT)"}[provider]
            messagebox.showwarning(
                f"{api_lbl} API Key 필요",
                f"공식 {api_lbl} API 모드용 {api_lbl} API Key를 입력하거나 전용 브라우저 모드로 전환해 주세요.",
            )
            self.open_settings_dialog(first_run=False, initial_tab=1)
            return

        mic_lbl = self.mic_choice_var.get().strip()
        if (
            not mic_lbl
            or mic_lbl.startswith("(")
            or is_virtual_loopback_mic_label(mic_lbl)
            or mic_lbl not in self.in_map
        ):
            messagebox.showwarning(
                "마이크 장치 필요",
                "PC에 연결된 물리 마이크 입력 장치를 찾을 수 없습니다.\n마이크 연결 후 [🔄 장치 새로고침]을 누르거나 [모드 2: Gemini Live 오디오 브릿지]를 이용해 주세요.",
            )
            return

        self._drain_companion_turn_queue()

        self.abort_current_playback()
        self.stop_playback_flag.clear()
        with self._companion_session_lock:
            self._companion_session_id += 1
            self._audio_session_id += 1
            session_id = self._companion_session_id
            audio_sid = self._audio_session_id
        session_cancel_ev = threading.Event()
        self._companion_cancel_event = session_cancel_ev
        with self._turn_interrupt_lock:
            self._active_turn_interrupt = threading.Event()
        self._barge_in_ducking = False
        self._recent_tts_jp_lines.clear()
        self._recent_tts_turn_openers.clear()
        self._clear_subtitle_translator_context()
        self._tts_error_notice_shown = False
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
        self.log(f"💡 [안내] {short} 브라우저 안의 마이크 버튼을 누를 필요 없이, 이 프로그램이 PC 마이크를 상시 연속 감지합니다.")

        self._output_stream_ready.clear()
        threading.Thread(
            target=self._audio_player_loop,
            kwargs={"audio_sid": audio_sid, "companion_sid": session_id},
            daemon=True,
        ).start()
        threading.Thread(
            target=self._clause_order_drainer_loop,
            kwargs={"audio_sid": audio_sid},
            daemon=True,
        ).start()
        threading.Thread(
            target=self._tts_prefetch_loop,
            kwargs={"audio_sid": audio_sid},
            daemon=True,
        ).start()
        threading.Thread(
            target=self._companion_turn_worker,
            kwargs={"session_id": session_id, "session_cancel_ev": session_cancel_ev},
            daemon=True,
        ).start()
        threading.Thread(
            target=self._companion_watchdog_loop,
            kwargs={"session_id": session_id},
            daemon=True,
        ).start()

        mic_idx, mic_sr = self.in_map[mic_lbl]
        fallback_mics = self._build_mic_fallback_candidates(mic_lbl)
        self._sync_mic_stt_config()
        self.mic_stt.set_character_context(self.current_char)
        try:
            self.mic_stt.start(mic_idx, mic_sr, mic_lbl, fallback_candidates=fallback_mics)
        except Exception as exc:
            safe_exc = _redact_secrets(str(exc), extra_secrets=self._active_secret_values())
            self._abort_bridge_on_device_error(f"[마이크 시작 오류] {safe_exc}")
            return

        if self._is_colab_s2pro_enabled():
            self._prewarm_colab_s2pro_current_char()

        self._launch_companion_boot_thread(
            session_id=session_id,
            cancel_ev=session_cancel_ev,
            provider=provider,
            backend=backend,
        )

    def _wait_for_audio_drain_and_idle(
        self,
        expected_turn_seq: int | None = None,
        interrupt_event: threading.Event | None = None,
        session_id: int | None = None,
    ) -> None:
        """Wait until all prefetched Fish Audio clauses finish playing before returning to IDLE/AFK."""
        deadline = time.monotonic() + 90.0
        while (
            self.companion_running
            and (session_id is None or session_id == self._companion_session_id)
            and time.monotonic() < deadline
        ):
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
        if session_id is not None and session_id != self._companion_session_id:
            return
        new_state = self.companion_sm.on_turn_finished(time.monotonic(), expected_turn_seq=expected_turn_seq)
        if new_state == CompanionState.AFK:
            self.log("⏸ [자리 비움(AFK) 전환] 연속 무응답 선톡 한도에 도달해 타이머를 일시 정지합니다. 말을 걸면 즉시 재개됩니다.")

    def _classify_companion_error(
        self,
        provider: str,
        backend: str,
        exc: Exception | str,
    ) -> tuple[str, str, str]:
        """Classify companion error into (category, scrubbed_err, concise_summary)."""
        p = normalize_ai_provider(provider)
        short = AI_PROVIDER_SHORT_NAMES[p]
        secrets = self._active_secret_values()
        safe_err = _redact_secrets(str(exc or ""), extra_secrets=secrets)
        low = safe_err.lower()
        compact = " ".join(safe_err.split())
        if len(compact) > 120:
            compact = compact[:117] + "..."

        if any(
            tok in low
            for tok in (
                "[challenge_required]",
                "challenge_required",
                "cloudflare",
                "turnstile",
                "just a moment",
                "human verification",
                "보안 확인",
            )
        ):
            return (
                "challenge",
                safe_err,
                f"{short} 보안 확인(Cloudflare 사람 확인)이 필요합니다. 브라우저 창에서 완료 후 [✅ 확인 완료]를 눌러 주세요.",
            )
        if (
            "[login_required]" in low
            or "login_required" in low
            or (
                backend == "browser"
                and any(tok in low for tok in ("로그인", "unauthenticated", "sign in", "세션 만료", "session expired"))
            )
        ):
            return (
                "login",
                safe_err,
                f"{short} 로그인이 필요하거나 세션이 만료되었습니다. 브라우저 창에서 로그인 후 [✅ 확인 완료]를 눌러 주세요.",
            )
        if (
            isinstance(exc, TimeoutError)
            or "[response_timeout]" in low
            or "response_timeout" in low
            or "timeout" in low
            or "timed out" in low
            or "시간 초과" in safe_err
        ):
            hint = (
                f"{short} 응답이 시간 내에 시작되지 않았습니다. [🌐 {short} 창 보기]로 팝업·로그인을 확인하거나 [🔄 다시 시도]를 눌러 주세요."
                if backend == "browser"
                else f"{short} API 응답 시간이 초과되었습니다. 네트워크 상태나 추론 설정을 확인한 뒤 [🔄 다시 시도]를 눌러 주세요."
            )
            return "timeout", safe_err, hint
        if "[provider_error]" in low or "provider_error" in low:
            return "provider_error", safe_err, compact or f"{short} 서비스 오류 또는 사용량 제한이 감지되었습니다."
        return "other", safe_err, compact or f"{short} 응답 생성 중 오류가 발생했습니다."

    def _format_companion_error_notice(self, provider: str, backend: str, exc: Exception) -> tuple[str, str]:
        """Redact secrets from `exc` and craft a concise, actionable Korean subtitle notice."""
        p = normalize_ai_provider(provider)
        short = AI_PROVIDER_SHORT_NAMES[p]
        api_name = {"gemini": "Gemini", "claude": "Claude", "chatgpt": "OpenAI"}[p]
        def_model_hint = get_provider_default_api_model(p)
        secrets = self._active_secret_values()
        safe_err = _redact_secrets(str(exc or ""), extra_secrets=secrets)
        low = safe_err.lower()
        compact = " ".join(safe_err.split())
        if len(compact) > 100:
            compact = compact[:97] + "..."

        if any(
            tok in low
            for tok in (
                "[challenge_required]",
                "challenge_required",
                "cloudflare",
                "turnstile",
                "just a moment",
                "human verification",
                "보안 확인",
            )
        ):
            notice = (
                f"{short} 전용 브라우저에서 보안 확인(Cloudflare 사람 확인)이 필요합니다. "
                f"열린 브라우저 창에서 보안 확인을 마친 뒤 [✅ 확인 완료]를 눌러 주세요."
            )
        elif (
            "[login_required]" in low
            or "login_required" in low
            or (
                backend == "browser"
                and any(tok in low for tok in ("로그인", "unauthenticated", "sign in", "세션 만료", "session expired"))
            )
        ):
            notice = (
                f"{short} 전용 브라우저 로그인이 필요하거나 세션이 만료되었습니다. "
                f"브라우저 창에서 로그인을 마친 뒤 [✅ 확인 완료]를 누르거나 ⚙️ 설정에서 API 모드로 전환해 주세요."
            )
        elif any(
            tok in low
            for tok in (
                "api_key_invalid",
                "api key not valid",
                "invalid api key",
                "invalid_api_key",
                "incorrect api key",
                "authentication_error",
                "unauthorized",
                "http 401",
            )
        ):
            notice = (
                f"{api_name} API 키가 올바르지 않거나 만료되었습니다. "
                f"⚙️ 설정에서 API 키를 확인하거나 무료 전용 브라우저 모드로 전환해 주세요."
            )
        elif any(
            tok in low
            for tok in (
                "429",
                "529",
                "overloaded",
                "quota",
                "rate limit",
                "rate_limit",
                "resource_exhausted",
                "billing",
                "credit",
                "한도",
                "결제",
                "유료",
            )
        ):
            if backend == "browser":
                notice = (
                    f"{short} 웹 사용량 제한 또는 일시적 혼잡이 감지되었습니다. "
                    f"잠시 후 [🔄 다시 시도]하거나 브라우저 창 상태를 확인해 주세요."
                )
            else:
                notice = (
                    f"{api_name} 호출 한도(쿼터)에 도달했거나 현재 계정에서 사용할 수 없는 모델입니다. "
                    f"잠시 후 다시 시도하거나 ⚙️ 설정에서 기본 모델({def_model_hint} 등) 또는 전용 브라우저 모드로 전환해 주세요."
                )
        elif "[provider_error]" in low or "provider_error" in low:
            notice = (
                f"{short} 서비스 오류 또는 메시지 제한이 발생했습니다. "
                f"브라우저 창 메시지를 확인한 뒤 [🔄 다시 시도]해 주세요."
            )
        elif any(tok in low for tok in ("api key", "http 403", "permission_denied", "forbidden")):
            notice = (
                f"{api_name} API 접근 권한이 거부되었습니다. "
                f"⚙️ 설정에서 API 키 권한을 확인하거나 무료 전용 브라우저 모드로 전환해 주세요."
            )
        elif any(
            tok in low
            for tok in (
                "유효하지 않은",
                "invalid",
                "model not found",
                "http 404",
                "404",
                "not_found",
                "deprecated",
                "unsupported",
            )
        ):
            notice = (
                f"{short} 모델을 불러오지 못했습니다. "
                f"⚙️ 설정에서 [계정 모델 동기화]를 실행하거나 기본 모델로 변경해 주세요."
            )
        elif (
            isinstance(exc, TimeoutError)
            or "[response_timeout]" in low
            or "response_timeout" in low
            or any(
                tok in low
                for tok in (
                    "timeout",
                    "timed out",
                    "시간 초과",
                    "connection",
                    "network",
                    "dns",
                    "ssl",
                    "cdp",
                    "websocket",
                    "브라우저",
                )
            )
        ):
            if backend == "browser":
                notice = (
                    f"{short} 브라우저 응답이 시간 내에 도착하지 않았거나 일시적인 네트워크·연결 지연이 발생했습니다. "
                    f"[🌐 {short} 창 보기]로 로그인·보안 팝업·오류를 확인하거나 [🔄 다시 시도]를 눌러 주세요."
                )
            else:
                notice = (
                    f"{short} 연결이 지연되거나 일시적인 네트워크 오류가 발생했습니다. "
                    f"잠시 후 다시 시도하거나 네트워크 연결을 확인해 주세요."
                )
        else:
            notice = (
                f"{short} 응답 생성 중 오류가 발생했습니다. "
                f"⚙️ 설정 또는 상태 로그에서 원인을 확인해 주세요."
            )

        if compact:
            notice = f"{notice} (상세: {compact})"
        notice = _redact_secrets(notice, extra_secrets=secrets)
        return safe_err, notice

    def _companion_turn_worker(
        self,
        *,
        session_id: int | None = None,
        session_cancel_ev: threading.Event | None = None,
    ) -> None:
        base_cancel_ev = session_cancel_ev if session_cancel_ev is not None else self._companion_cancel_event
        while self.companion_running and (session_id is None or session_id == self._companion_session_id):
            if base_cancel_ev.is_set():
                break
            item: tuple[str, str] | None = None
            with self._turn_interrupt_lock:
                try:
                    item = self._companion_turn_queue.get_nowait()
                except queue.Empty:
                    self._companion_turn_wakeup.clear()
                    item = None
                if item is not None:
                    if (
                        (not self.companion_running)
                        or (session_id is not None and session_id != self._companion_session_id)
                        or base_cancel_ev.is_set()
                    ):
                        break
                    cur_turn_cancel = threading.Event()
                    self._active_turn_interrupt = cur_turn_cancel
                    with self._playback_lock:
                        self.stop_playback_flag.clear()
                        turn_playback_gen = self.playback_gen_id
                    my_turn_seq = self.companion_sm.current_turn_seq
            if item is None:
                self._companion_turn_wakeup.wait(timeout=0.02)
                continue

            kind, payload = item
            combined_cancel = _CombinedCancelEvent(base_cancel_ev, cur_turn_cancel)
            turn_char = self.current_char

            provider = self._get_ai_provider()
            short = AI_PROVIDER_SHORT_NAMES[provider]
            backend = str(self.cfg.get("gemini_backend", "browser"))
            active_api_key = self._get_active_api_key(provider)
            active_api_model = self._get_active_api_model(provider)
            browser_model = self._get_active_browser_model(provider)
            chat_thinking = str(self.cfg.get("gemini_thinking_level", "low") or "low")
            if chat_thinking not in GEMINI_THINKING_LEVEL_LABELS:
                chat_thinking = "low"
            sub_engine = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
            if sub_engine == "auto_llm":
                sub_engine = "auto_inline"
            sub_mode = str(self._read_tk_var_safe("subtitle_mode_var", self.cfg.get("subtitle_mode", "jp_kr")) or "jp_kr")
            reply_lang = self._get_reply_language()
            use_ko_lock = reply_lang == "ko"
            jp_coach_on = bool(
                self._read_tk_var_safe("jp_beginner_coach_var", bool(self.cfg.get("jp_beginner_lover_mode", False)))
            ) and (not use_ko_lock)
            use_custom_persona = bool(self.cfg.get("use_custom_persona", False))
            cfg_vcode = str(self.cfg.get("persona_prompt_variant", "official") or "official")
            pvar_lbl = self._read_tk_var_safe(
                "persona_variant_var",
                PERSONA_VARIANT_LABELS.get(cfg_vcode, PERSONA_VARIANT_LABELS["official"]),
            )
            vcode = "official" if use_custom_persona else PERSONA_LABEL_TO_CODE.get(
                str(pvar_lbl),
                cfg_vcode if cfg_vcode in PERSONA_VARIANT_LABELS else "official",
            )
            persona_prompt, use_jp_lock = self._resolve_active_persona_for_gemini(include_variant_block=False)
            custom_prompt_raw = str(self.cfg.get("custom_persona_prompt") or "").strip() if use_custom_persona else ""
            bilingual_inline = bool(use_jp_lock and (not use_ko_lock) and sub_mode != "jp_only" and sub_engine == "auto_inline")
            variant_directive = (
                ""
                if use_custom_persona
                else _call_build_fanon_enhancement_block(turn_char, vcode, reply_lang=reply_lang)
            )
            persona_anchor = _call_build_per_turn_persona_anchor(
                turn_char,
                vcode,
                reply_lang=reply_lang,
                use_custom=use_custom_persona,
                recent_spoken_lines=list(self._recent_tts_jp_lines),
                recent_turn_openers=list(self._recent_tts_turn_openers),
                bilingual_inline=bilingual_inline,
                custom_prompt=custom_prompt_raw,
            )
            active_ent = self.momotalk_entry_map.get(getattr(self, "current_momotalk_entry_id", ""))
            costume_kr_turn = (
                str(active_ent.get("costume_label_kr") or "").strip()
                if (active_ent is not None and active_ent.get("base_cid") == turn_char.get("id") and active_ent.get("costume_label_kr"))
                else ""
            )
            persona_refresh_block = _call_build_long_turn_persona_refresh_block(
                turn_char,
                vcode,
                reply_lang,
                bilingual_inline=bilingual_inline,
                use_custom_persona=use_custom_persona,
                custom_prompt=custom_prompt_raw,
                costume_label_kr=costume_kr_turn,
            )
            llm_temp = 0.84 if use_custom_persona else get_llm_temperature_for_variant(vcode)
            ko_style_guide = (
                build_character_ko_style_guide(turn_char, jp_beginner_coach=jp_coach_on, persona_variant=vcode)
                if use_jp_lock
                else ""
            )
            t_start = time.time()

            emitted_tts_any = False
            turn_recorded_opener = False
            turn_spoken_parts: list[str] = []
            turn_kr_futures: list[Future[str] | None] = []
            reply = ""
            prime_err: Exception | None = None

            def _enqueue_companion_tts(
                sentence: str,
                is_first: bool,
                kr_fut: Future[str] | None = None,
            ) -> None:
                nonlocal emitted_tts_any, turn_recorded_opener
                if (
                    (not self.companion_running)
                    or (session_id is not None and session_id != self._companion_session_id)
                    or combined_cancel.is_set()
                ):
                    return
                voice_key, stripped_sent = resolve_sentence_voice_persona(turn_char, sentence)
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
                if first_flag and not turn_recorded_opener:
                    self._recent_tts_turn_openers.append(clean_sent)
                    turn_recorded_opener = True
                self.companion_sm.on_speaking_started(time.monotonic())

                speed_lbl_cur = str(self._read_tk_var_safe("speed_mode_var", self._live_speed_mode_lbl) or "")
                speed_code_cur = SPEED_LABEL_TO_CODE.get(
                    speed_lbl_cur,
                    str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive"),
                )
                split_tail = ""
                head_sent = clean_sent
                if (
                    first_flag
                    and speed_code_cur != "full_turn"
                    and self._is_colab_s2pro_enabled()
                    and self.colab_s2pro_backend.should_fast_split_opening_clause()
                ):
                    head_sent, split_tail = split_s2pro_opening_clause(clean_sent)

                if split_tail:
                    # Submit short Clause #1a + parallel Clause #1b immediately so SGLang-Omni runs batch=2!
                    self._companion_clause_seq += 1
                    self.tts_queue.put(
                        (
                            self._companion_clause_seq,
                            head_sent,
                            t_start,
                            0.14,
                            True,
                            kr_fut,
                            voice_key,
                            turn_playback_gen,
                            turn_char,
                            clean_sent,
                            False,
                        )
                    )
                    self._companion_clause_seq += 1
                    self.tts_queue.put(
                        (
                            self._companion_clause_seq,
                            split_tail,
                            t_start,
                            0.14,
                            False,
                            None,
                            voice_key,
                            turn_playback_gen,
                            turn_char,
                            "",
                            True,
                        )
                    )
                else:
                    self._companion_clause_seq += 1
                    self.tts_queue.put(
                        (
                            self._companion_clause_seq,
                            clean_sent,
                            t_start,
                            0.14,
                            first_flag,
                            kr_fut,
                            voice_key,
                            turn_playback_gen,
                            turn_char,
                        )
                    )

            def _on_sentence(sentence: str, is_first: bool) -> None:
                _enqueue_companion_tts(sentence, is_first)

            def _on_sentence_pair(jp_sent: str, kr_fut: Future[str], is_first: bool) -> None:
                _enqueue_companion_tts(jp_sent, is_first, kr_fut=kr_fut)

            mem_ctx = str(getattr(self, "_active_call_memory_context", "") or "")

            try:
                if kind == "prime":
                    self._calling_prime_started_mono = time.monotonic()
                    self._calling_delay_hint_shown = False
                    if self._call_ui_state == "calling":
                        self._schedule_ui(
                            lambda sid=session_id, s=short, b=backend: self._set_calling_stage(
                                f"🧠 {s} 첫인사 응답 기다리는 중..." if b == "browser" else f"⚡ {s} API 첫인사 응답 기다리는 중...",
                                session_id=sid,
                            )
                        )
                    self.log(f"🧠 [새 대화 시작 · {short}] 캐릭터 페르소나({vcode})와 [IDLE_PING] 시스템 규칙을 전송합니다...")
                    reply = self.gemini_mgr.prime_and_greet(
                        backend_mode=backend,
                        api_key=active_api_key,
                        api_model=active_api_model,
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
                        provider=provider,
                        persona_variant=vcode,
                        variant_directive=variant_directive,
                        temperature=llm_temp,
                    )
                else:
                    if kind == "ping":
                        self.log(f"🤖 [선톡 발동] 침묵 감지 → {short}에게 '[IDLE_PING]' 전송 중...")

                    sv_frame: Any = None
                    sv_mode = "active_chat"
                    sv_image_bytes: bytes | None = None
                    sv_enable_study_filter = False
                    outgoing_user_text = ""

                    if kind in ("screen_react", "study_checkin", "instant_look"):
                        p_dict = payload if isinstance(payload, dict) else {}
                        sv_frame = p_dict.get("frame")
                        base_prompt = str(p_dict.get("prompt_hint") or "").strip()
                        if not base_prompt:
                            if kind == "instant_look":
                                base_prompt = "지금 내가 보고 있는 화면을 봐 줘!"
                            elif kind == "study_checkin":
                                base_prompt = "[STUDY_STUCK_CHECKIN]"
                            else:
                                base_prompt = "[SCREEN_CHANGE_REACT]"
                        outgoing_user_text = base_prompt
                        if provider == "gemini" and self._sv_available:
                            try:
                                from src.screen_vision import build_screen_turn_addon

                                sv_mode = str(
                                    self._screen_watcher.mode
                                    if self._screen_watcher is not None
                                    else self._sv_cfg.get("mode", "active_chat")
                                )
                                frame_bytes = (
                                    bytes(sv_frame.jpeg_bytes)
                                    if (
                                        sv_frame is not None
                                        and getattr(sv_frame, "jpeg_bytes", None)
                                        and not getattr(sv_frame, "blocked_by_privacy", False)
                                    )
                                    else b""
                                )
                                screen_addon = build_screen_turn_addon(
                                    mode=sv_mode,
                                    trigger_kind=kind,
                                    active_title=str(getattr(sv_frame, "active_title", "") or ""),
                                    has_image=bool(frame_bytes),
                                    deduplicated=bool(getattr(sv_frame, "deduplicated", False)),
                                    privacy_paused=bool(getattr(sv_frame, "blocked_by_privacy", False)),
                                    reply_lang=reply_lang,
                                    bilingual_inline=bilingual_inline,
                                )
                                outgoing_user_text = f"{screen_addon}\n\n{base_prompt}".strip()
                                sv_image_bytes = frame_bytes or None
                                sv_enable_study_filter = bool(sv_mode == "econ_study" and kind != "study_checkin")
                            except Exception as sv_exc:
                                self.log(f"⚠️ [화면 공유 프롬프트 조합 실패] {sv_exc}")
                    else:
                        outgoing_user_text = str(payload or "")
                        if (
                            provider == "gemini"
                            and self._sv_available
                            and self._screen_watcher is not None
                            and self._screen_watcher.enabled
                            and (not self._screen_watcher.paused)
                        ):
                            try:
                                from src.screen_vision import build_screen_turn_addon

                                sv_frame = self._screen_watcher.capture_for_user_turn()
                                if sv_frame is not None:
                                    sv_mode = str(self._screen_watcher.mode)
                                    frame_bytes = (
                                        bytes(sv_frame.jpeg_bytes)
                                        if (
                                            getattr(sv_frame, "jpeg_bytes", None)
                                            and not getattr(sv_frame, "blocked_by_privacy", False)
                                        )
                                        else b""
                                    )
                                    screen_addon = build_screen_turn_addon(
                                        mode=sv_mode,
                                        trigger_kind=kind,
                                        active_title=str(getattr(sv_frame, "active_title", "") or ""),
                                        has_image=bool(frame_bytes),
                                        deduplicated=bool(getattr(sv_frame, "deduplicated", False)),
                                        privacy_paused=bool(getattr(sv_frame, "blocked_by_privacy", False)),
                                        reply_lang=reply_lang,
                                        bilingual_inline=bilingual_inline,
                                    )
                                    outgoing_user_text = f"{screen_addon}\n\n{outgoing_user_text}".strip()
                                    sv_image_bytes = frame_bytes or None
                                    sv_enable_study_filter = bool(sv_mode == "econ_study")
                            except Exception as sv_exc:
                                self.log(f"⚠️ [화면 공유 턴 캡처 오류] {sv_exc}")

                    send_kwargs: dict[str, Any] = {
                        "backend_mode": backend,
                        "api_key": active_api_key,
                        "api_model": active_api_model,
                        "persona_prompt": persona_prompt,
                        "use_japanese_lock": use_jp_lock,
                        "user_text": outgoing_user_text,
                        "on_sentence": _on_sentence,
                        "cancel_event": combined_cancel,
                        "browser_model": browser_model,
                        "thinking_level": chat_thinking,
                        "bilingual_inline": bilingual_inline,
                        "ko_style_guide": ko_style_guide,
                        "on_sentence_pair": _on_sentence_pair if bilingual_inline else None,
                        "jp_beginner_coach": jp_coach_on,
                        "use_korean_lock": use_ko_lock,
                        "memory_context": mem_ctx,
                        "provider": provider,
                        "persona_variant": vcode,
                        "variant_directive": variant_directive,
                        "persona_anchor": persona_anchor,
                        "persona_refresh_block": persona_refresh_block,
                        "temperature": llm_temp,
                    }
                    if sv_image_bytes is not None or sv_enable_study_filter:
                        send_kwargs["image_bytes"] = sv_image_bytes
                        send_kwargs["image_mime"] = "image/jpeg"
                        send_kwargs["enable_study_visual_filter"] = sv_enable_study_filter
                        send_kwargs["study_visual_cb"] = (
                            (lambda spec, ch=turn_char: self._on_study_visual_received(spec, turn_char=ch))
                            if sv_enable_study_filter
                            else None
                        )
                    try:
                        reply = self.gemini_mgr.send_message(**send_kwargs)
                    except TypeError as te:
                        if "unexpected keyword argument" not in str(te):
                            raise
                        for opt_k in (
                            "persona_refresh_block",
                            "image_bytes",
                            "image_mime",
                            "enable_study_visual_filter",
                            "study_visual_cb",
                        ):
                            send_kwargs.pop(opt_k, None)
                        reply = self.gemini_mgr.send_message(**send_kwargs)
                if reply and (not combined_cancel.is_set()):
                    self.log(f"💬 [{short} 응답] {reply}")
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
                                ck = self.subtitle_translator._cache_key(
                                    p,
                                    turn_char,
                                    persona_variant=vcode,
                                    jp_beginner_coach=jp_coach_on,
                                )
                                cv = self.subtitle_translator._cache_get(ck)
                                if cv:
                                    cached_kr.append(cv)
                            except Exception:
                                pass
                        if cached_kr:
                            kr_joined = " ".join(cached_kr).strip()
                    clean_reply_fallback = str(reply or "")
                    if self._sv_available and "[STUDY_VISUAL]" in clean_reply_fallback.upper():
                        try:
                            from src.screen_vision import strip_study_visual_blocks

                            stripped_res = strip_study_visual_blocks(clean_reply_fallback)
                            clean_reply_fallback = (
                                str(stripped_res[0] or "")
                                if isinstance(stripped_res, tuple)
                                else str(stripped_res or "")
                            )
                        except Exception:
                            pass
                    if use_ko_lock:
                        jp_save = spoken_joined or strip_voice_persona_tags(clean_reply_fallback)
                        kr_save = jp_save
                    else:
                        jp_save = spoken_joined or strip_voice_persona_tags(clean_reply_fallback)
                        kr_save = kr_joined
                    self._record_memory_turn(
                        role="assistant",
                        jp_text=jp_save,
                        kr_text=kr_save,
                        is_proactive_ping=(kind in ("ping", "screen_react", "study_checkin")),
                    )
            except Exception as exc:
                prime_err = exc
                still_mine = (
                    (not combined_cancel.is_set())
                    and self.companion_running
                    and (session_id is None or session_id == self._companion_session_id)
                )
                if still_mine:
                    err_kind, _cls_safe, short_reason = self._classify_companion_error(provider, backend, exc)
                    safe_err, user_notice = self._format_companion_error_notice(provider, backend, exc)
                    self.log(f"[{short} 연동 오류] {safe_err}")

                    if kind == "prime" and self._call_ui_state == "calling":
                        if err_kind in ("login", "challenge") and backend == "browser":
                            self._ringback_state.pause()
                            self._companion_session_ready = False
                            try:
                                self.gemini_mgr.browser.set_window_visible(True)
                            except Exception:
                                pass
                            self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 숨기기"))
                            cur_sid = self._companion_session_id if session_id is None else session_id
                            self._schedule_ui(
                                lambda sid=cur_sid, pr=provider, k=err_kind, d=short_reason: self._enter_auth_recovery_ui(
                                    sid,
                                    pr,
                                    kind=k,
                                    detail=d,
                                )
                            )
                        else:
                            self._ringback_state.stop()
                            err_title = (
                                f"⏱️ {short} 첫인사 응답 시간 초과"
                                if err_kind == "timeout"
                                else f"⚠️ {short} 첫인사 연결 실패"
                            )
                            cur_sid = self._companion_session_id if session_id is None else session_id
                            self._schedule_ui(
                                lambda sid=cur_sid, pr=provider, b=backend, t=err_title, d=user_notice: self._enter_calling_error_ui(
                                    sid,
                                    pr,
                                    backend=b,
                                    title=t,
                                    detail=d,
                                )
                            )
                    else:
                        if getattr(self, "_momotalk_char_activated", False):
                            def _append_err_sub(notice_txt: str = user_notice, prov_short: str = short) -> None:
                                self._subtitle_seq += 1
                                self._append_subtitle_entry_fast(
                                    SubtitleEntry(
                                        entry_id=self._subtitle_seq,
                                        char_id="__system__",
                                        char_name_kr=f"{prov_short} 연결 안내",
                                        char_name_jp="",
                                        jp_text=notice_txt,
                                        kr_text=notice_txt,
                                        is_turn_start=True,
                                    )
                                )

                            self._schedule_ui(_append_err_sub)
                        if err_kind in ("login", "challenge") and backend == "browser":
                            self._companion_session_ready = False
                            try:
                                self.gemini_mgr.browser.set_window_visible(True)
                            except Exception:
                                pass
                            self._schedule_ui(lambda s=short: self._browser_toggle_var.set(f"🌐 {s} 화면 숨기기"))
                            cur_sid = self._companion_session_id if session_id is None else session_id
                            self._schedule_ui(
                                lambda sid=cur_sid, pr=provider, k=err_kind, d=short_reason: self._enter_auth_recovery_ui(
                                    sid,
                                    pr,
                                    kind=k,
                                    detail=d,
                                )
                            )
            finally:
                still_mine = (
                    (not combined_cancel.is_set())
                    and self.companion_running
                    and (session_id is None or session_id == self._companion_session_id)
                )
                prime_ok = bool(emitted_tts_any or (reply and str(reply).strip()))
                if kind == "prime" and still_mine:
                    if prime_err is None and prime_ok:
                        if self._call_ui_state == "calling":
                            self._ringback_state.stop()
                            cur_sid = self._companion_session_id if session_id is None else session_id
                            self._schedule_ui(lambda sid=cur_sid: self._on_call_connected_ui(expected_session_id=sid))
                    elif prime_err is None and (not prime_ok):
                        self.log(f"[{short} 응답 비어 있음] 첫인사 응답이 비어 있어 통화를 연결하지 않았습니다.")
                        if self._call_ui_state == "calling":
                            self._ringback_state.stop()
                            empty_detail = (
                                f"{short} 첫인사 응답을 받지 못했습니다. [🌐 {short} 창 보기]로 로그인·보안 확인·팝업을 확인하거나 [🔄 다시 시도]를 눌러 주세요."
                                if backend == "browser"
                                else f"{short} API 응답이 비어 있습니다. 모델/추론 설정을 확인하거나 [🔄 다시 시도]를 눌러 주세요."
                            )
                            cur_sid = self._companion_session_id if session_id is None else session_id
                            self._schedule_ui(
                                lambda sid=cur_sid, pr=provider, b=backend, d=empty_detail: self._enter_calling_error_ui(
                                    sid,
                                    pr,
                                    backend=b,
                                    title=f"⚠️ {AI_PROVIDER_SHORT_NAMES[pr]} 첫인사 응답 없음",
                                    detail=d,
                                )
                            )
                        elif getattr(self, "_momotalk_char_activated", False):
                            empty_sub = f"{short}에서 빈 응답이 반환되었습니다. 잠시 후 다시 말씀하시거나 [🔄] 버튼으로 대화를 새로 시작해 주세요."
                            self._schedule_ui(
                                lambda msg=empty_sub, ps=short: (
                                    setattr(self, "_subtitle_seq", self._subtitle_seq + 1),
                                    self._append_subtitle_entry_fast(
                                        SubtitleEntry(
                                            entry_id=self._subtitle_seq,
                                            char_id="__system__",
                                            char_name_kr=f"{ps} 연결 안내",
                                            char_name_jp="",
                                            jp_text=msg,
                                            kr_text=msg,
                                            is_turn_start=True,
                                        )
                                    ),
                                )
                            )
                self._wait_for_audio_drain_and_idle(
                    expected_turn_seq=my_turn_seq,
                    interrupt_event=cur_turn_cancel,
                    session_id=session_id,
                )
                if self._screen_watcher is not None:
                    try:
                        self._screen_watcher.mark_turn_activity()
                    except Exception:
                        pass
                if (
                    kind == "prime"
                    and prime_err is None
                    and prime_ok
                    and backend == "browser"
                    and (not combined_cancel.is_set())
                    and self.companion_running
                    and (session_id is None or session_id == self._companion_session_id)
                    and not self._is_valid_synced_web_models(self._get_active_account_web_models(provider), provider)
                ):
                    self._discover_web_models_async(provider=provider, only_when_idle=True)

    def _companion_watchdog_loop(self, *, session_id: int | None = None) -> None:
        last_ui = 0.0
        last_ui_pair: tuple[str, str] | None = None
        while self.companion_running and (session_id is None or session_id == self._companion_session_id):
            now = time.monotonic()
            if (
                self._companion_session_ready
                and (not self._companion_mic_error)
                and self._call_ui_state == "connected"
            ):
                with self._turn_interrupt_lock:
                    if self.companion_sm.tick(now):
                        ping_seq = self.companion_sm.current_turn_seq
                        self._enqueue_companion_turn("ping", IDLE_PING_TOKEN, revert_turn_seq=ping_seq)

            if (now - last_ui) >= 0.25:
                last_ui = now
                if self._companion_mic_error:
                    msg = f"⚠️ {self._companion_mic_error}"
                    col = "#dc2626"
                elif not self._companion_session_ready:
                    cur_short = AI_PROVIDER_SHORT_NAMES[self._get_ai_provider()]
                    if self.gemini_mgr.browser.login_mode_active:
                        msg = f"🔑 {cur_short} 로그인/보안 확인 대기 중 ([✅ 확인 완료] 클릭)"
                        col = "#d97706"
                    else:
                        msg = f"🔄 {cur_short} 세션 준비 중..."
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
                        cur_short = AI_PROVIDER_SHORT_NAMES[self._get_ai_provider()]
                        msg = f"🧠 {cur_short} 응답 중 (THINKING)"
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
        with self._companion_session_lock:
            self._companion_session_id += 1
            self._audio_session_id += 1
        with self._whisper_lock:
            self._whisper_load_gen += 1
        self.running = False
        self.companion_running = False
        self._stop_keepalive_event.set()
        try:
            self._cleanup_screen_vision()
        except Exception:
            pass
        try:
            self._companion_cancel_event.set()
        except Exception:
            pass
        try:
            with self._turn_interrupt_lock:
                self._active_turn_interrupt.set()
                self._drain_companion_turn_queue()
            self._companion_turn_wakeup.set()
        except Exception:
            pass
        self._abort_active_browser_prompt()
        try:
            self._finalize_active_memory_session(run_summary=False)
        except Exception:
            pass
        self._stop_momotalk_asset_worker(join_timeout=2.0)
        self._cancel_pending_after_timers()
        try:
            self._ringback_state.stop()
        except Exception:
            pass
        try:
            self.abort_current_playback()
        except Exception:
            pass
        try:
            ex = getattr(self, "tts_executor", None)
            self.tts_executor = None
            if ex is not None:
                ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self.mic_stt.stop()
        except Exception:
            pass
        try:
            # Run browser/client teardown with a bounded join so closing the app never hangs on _io_lock
            close_thr = threading.Thread(target=self.gemini_mgr.close, daemon=True, name="gemini-close-exit")
            close_thr.start()
            close_thr.join(timeout=2.5)
        except Exception:
            pass
        try:
            self.subtitle_translator.close()
        except Exception:
            pass
        try:
            if getattr(self, "fish_mcp_backend", None) is not None:
                self.fish_mcp_backend.close()
        except Exception:
            pass
        try:
            if getattr(self, "colab_s2pro_backend", None) is not None:
                self.colab_s2pro_backend.close()
        except Exception:
            pass
        try:
            with self._http_client_lock:
                hc = self._http_client
                self._http_client = None
            if hc is not None:
                hc.close()
        except Exception:
            pass
        try:
            bg_lock = getattr(self, "_startup_bg_threads_lock", None)
            if bg_lock is not None:
                with bg_lock:
                    bg_threads = list(getattr(self, "_startup_bg_threads", ()))
                    self._startup_bg_threads.clear()
            else:
                bg_threads = []
            cur_t = threading.current_thread()
            join_deadline = time.monotonic() + 0.8
            for th in bg_threads:
                is_alive_fn = getattr(th, "is_alive", None)
                join_fn = getattr(th, "join", None)
                if th is None or th is cur_t or not callable(is_alive_fn) or not is_alive_fn():
                    continue
                rem = join_deadline - time.monotonic()
                if rem <= 0.0:
                    break
                if callable(join_fn):
                    try:
                        join_fn(timeout=min(0.25, rem))
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            ui_q = getattr(self, "_ui_queue", None)
            if ui_q is not None:
                while True:
                    try:
                        ui_q.get_nowait()
                    except queue.Empty:
                        break
        except Exception:
            pass
        try:
            atexit.unregister(self._cleanup_on_exit)
        except Exception:
            pass
        _remove_bridge_pid_file()

    def _maybe_prompt_colab_gpu_release_on_close(self) -> None:
        """
        On interactive window close, if Colab S2-Pro is enabled with a configured URL+token
        and a fast bounded liveness check (<=2.0s) confirms the server is currently online,
        offer 1-click remote GPU shutdown before exiting. Never prompts or blocks in headless/test
        mode or when Colab is disabled/offline.
        """
        if getattr(self, "_closing", False):
            return
        if os.environ.get("VOICE_COMPANION_HEADLESS", "").strip() == "1":
            return
        if not self._is_colab_s2pro_enabled():
            return
        if not str(self.cfg.get("colab_s2pro_api_key", "") or "").strip():
            return
        root = getattr(self, "root", None)
        if root is None:
            return
        try:
            if (not root.winfo_exists()) or (not root.winfo_viewable()):
                return
        except Exception:
            return

        backend = getattr(self, "colab_s2pro_backend", None)
        if backend is None:
            return

        # Strictly bounded <=2.0s liveness probe so offline/unreachable tunnels never stall app exit
        reachable = [False]

        def _probe() -> None:
            try:
                ok, _rtt = backend.ping_health(timeout=2.0)
                reachable[0] = bool(ok)
            except Exception:
                reachable[0] = False

        probe_th = threading.Thread(target=_probe, daemon=True, name="colab-exit-probe")
        probe_th.start()
        probe_th.join(timeout=2.2)
        if not reachable[0]:
            return

        try:
            should_release = messagebox.askyesno(
                "Colab GPU 반납 확인",
                "Colab S2-Pro GPU 서버가 아직 켜져 있습니다.\n"
                "지금 원격으로 종료하고 GPU를 반납할까요?\n"
                "('아니요'를 선택하면 30분 미사용 시 자동 반납됩니다)",
                parent=root,
            )
        except Exception:
            return
        if not should_release:
            return

        # Bounded shutdown request so app exit remains snappy even if tunnel tears down immediately
        def _shutdown() -> None:
            try:
                self.colab_s2pro_shutdown_gpu(unassign=True, timeout=6.0)
            except Exception:
                pass

        sd_th = threading.Thread(target=_shutdown, daemon=True, name="colab-exit-shutdown")
        sd_th.start()
        sd_th.join(timeout=6.5)

    def _on_app_closing(self) -> None:
        self._maybe_prompt_colab_gpu_release_on_close()
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
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
        user32.EnumWindows.restype = ctypes.c_int
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.IsWindowVisible.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_int
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_int

        def _enum_cb(hwnd: int, _lparam: int) -> int:
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value in (APP_DISPLAY_NAME, "MomoTalk") or APP_DISPLAY_NAME in buf.value or "실시간 보이스 브릿지" in buf.value:
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    found_ref[0] = True
                    return 0
            return 1

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    except Exception:
        pass
    return found_ref[0]


def _get_bridge_pid_file() -> Path:
    return get_cache_dir() / "bridge.pid"


def _norm_exe_path(path_str: str) -> str:
    raw = str(path_str or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(str(Path(raw).resolve()))
    except OSError:
        return os.path.normcase(str(Path(raw)))


def _get_win32_process_image_path(proc_handle: Any) -> str:
    """Return the resolved Win32 executable image path via QueryFullProcessImageNameW, or '' on failure."""
    if os.name != "nt" or not proc_handle:
        return ""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        buf = ctypes.create_unicode_buffer(4096)
        size = ctypes.c_uint32(len(buf))
        if k32.QueryFullProcessImageNameW(proc_handle, 0, buf, ctypes.byref(size)) and buf.value:
            try:
                return str(Path(buf.value).resolve())
            except OSError:
                return str(Path(buf.value))
    except Exception:
        pass
    return ""


def _get_current_process_image_path() -> str:
    """Return the actual OS image path of this process (matching QueryFullProcessImageNameW in Windows .venv)."""
    if os.name == "nt":
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            img = _get_win32_process_image_path(k32.GetCurrentProcess())
            if img:
                return img
        except Exception:
            pass
    if sys.executable:
        try:
            return str(Path(sys.executable).resolve())
        except OSError:
            return str(Path(sys.executable))
    return ""


def _get_win32_process_creation_time_100ns(proc_handle: Any) -> int | None:
    """Return a Windows process creation timestamp (64-bit FILETIME in 100ns ticks), or None on failure."""
    if os.name != "nt" or not proc_handle:
        return None
    try:
        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        ]
        k32.GetProcessTimes.restype = ctypes.c_int
        ct, et, kt, ut = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if k32.GetProcessTimes(proc_handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
            return (int(ct.dwHighDateTime) << 32) | int(ct.dwLowDateTime)
    except Exception:
        pass
    return None


def _get_current_process_creation_time_100ns() -> int | None:
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        return _get_win32_process_creation_time_100ns(k32.GetCurrentProcess())
    except Exception:
        return None


def _parse_bridge_pid_record(raw: str) -> tuple[int | None, int | None, str]:
    """Parse `bridge.pid` supporting both JSON `{pid, create_time_100ns, exe}` and legacy plain-PID files."""
    s = str(raw or "").strip()
    if not s:
        return None, None, ""
    if s.isdigit():
        return int(s), None, ""
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                pid_val = obj.get("pid")
                ct_val = obj.get("create_time_100ns")
                exe_val = str(obj.get("exe") or "").strip()
                pid_int = int(pid_val) if isinstance(pid_val, int) and not isinstance(pid_val, bool) else None
                ct_int = int(ct_val) if isinstance(ct_val, int) and not isinstance(ct_val, bool) and ct_val > 0 else None
                return pid_int, ct_int, exe_val
        except Exception:
            return None, None, ""
    return None, None, ""


def _remove_bridge_pid_file() -> None:
    try:
        pid_file = _get_bridge_pid_file()
        if pid_file.exists() and not pid_file.is_symlink():
            rec_pid, _rec_ct, _rec_exe = _parse_bridge_pid_record(pid_file.read_text(encoding="utf-8"))
            if rec_pid == os.getpid():
                pid_file.unlink(missing_ok=True)
    except Exception:
        pass


def _write_bridge_pid_file() -> None:
    try:
        pid_file = _get_bridge_pid_file()
        if pid_file.is_symlink():
            return
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": int(os.getpid()),
            "create_time_100ns": _get_current_process_creation_time_100ns(),
            "exe": _get_current_process_image_path(),
        }
        pid_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        atexit.register(_remove_bridge_pid_file)
    except Exception:
        pass


def _terminate_stale_windowless_bridges() -> None:
    """Terminate a stale windowless previous bridge process recorded in bridge.pid using native Win32 API.

    Verifies process image name AND creation timestamp (preventing accidental termination of an
    unrelated Python process if Windows recycled a stale PID).
    """
    if os.name != "nt":
        return
    try:
        pid_file = _get_bridge_pid_file()
        if not pid_file.is_file() or pid_file.is_symlink():
            return
        stale_pid, recorded_ctime, recorded_exe = _parse_bridge_pid_record(pid_file.read_text(encoding="utf-8"))
        if stale_pid is None or stale_pid <= 0 or stale_pid == os.getpid():
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
            buf = ctypes.create_unicode_buffer(4096)
            size = ctypes.c_uint32(len(buf))
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return
            img_path = Path(buf.value)
            if img_path.name.lower() not in ("python.exe", "pythonw.exe"):
                return
            if recorded_ctime is not None:
                actual_ctime = _get_win32_process_creation_time_100ns(handle)
                if actual_ctime is None or actual_ctime != recorded_ctime:
                    return
                if recorded_exe and _norm_exe_path(str(img_path)) != _norm_exe_path(recorded_exe):
                    return
            else:
                # Legacy plain-PID file without creation timestamp: only terminate if running from this project's .venv
                try:
                    if not img_path.resolve().is_relative_to((ROOT_DIR / ".venv").resolve()):
                        return
                except OSError:
                    return
            k32.TerminateProcess(handle, 1)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        pass


def _offer_startup_settings_reset_recovery(err_text: str) -> bool:
    """Ask the user whether to restore default settings (keeping Fish key & Gemini login) after a fatal startup error."""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        user32.MessageBoxW.restype = ctypes.c_int
        lines = [ln.strip() for ln in _redact_secrets(str(err_text or "")).splitlines() if ln.strip()]
        short_err = lines[-1][:220] if lines else ""
        body = (
            "앱 시작 중 오류가 발생했습니다. 손상되거나 잘못된 사용자 설정이 원인일 수 있습니다.\n\n"
            "【유지】 Fish Audio API 키 · AI 브라우저 로그인 세션(BrowserProfile) · 대화 기억\n"
            "【초기화】 나머지 앱 설정 · 사용자 selectors.json · RVC 설정 (자동 백업 후 초기화)\n\n"
            "설정을 기본값으로 복원하고 다시 시작하시겠습니까?"
        )
        if short_err:
            body += f"\n\n(오류 요약: {short_err})"
        res = int(user32.MessageBoxW(0, body, "보이스 브릿지 시작 오류 — 기본값 복원", 0x04 | 0x30))
        return res == 6  # IDYES
    except Exception:
        return False


def main(argv: list[str] | None = None) -> None:
    global _SINGLE_INSTANCE_MUTEX
    cli_args = list(sys.argv[1:] if argv is None else argv)
    try:
        if "--reset-browser-only" in cli_args or "--reset-browser" in cli_args:
            _call_reset_browser_profile()
            if "--reset-browser-only" in cli_args and "--reset-settings" not in cli_args:
                return

        if "--reset-settings-only" in cli_args or "--reset-settings" in cli_args:
            reset_settings_to_defaults(
                app_root=ROOT_DIR,
                reason="cli_reset",
                preserve_fish_api_key=True,
                reset_rvc_config=True,
                reset_user_selectors=True,
            )
            if "--reset-settings-only" in cli_args:
                return

        if os.name == "nt":
            try:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
                k32.CreateMutexW.restype = ctypes.c_void_p
                k32.CloseHandle.argtypes = [ctypes.c_void_p]
                k32.CloseHandle.restype = ctypes.c_int
                _SINGLE_INSTANCE_MUTEX = k32.CreateMutexW(
                    None, False, "Local\\BlueArchive_AruFishBridge_v4_SingleInstance"
                )
                if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                    if _focus_existing_bridge_window():
                        if _SINGLE_INSTANCE_MUTEX:
                            k32.CloseHandle(_SINGLE_INSTANCE_MUTEX)
                            _SINGLE_INSTANCE_MUTEX = None
                        return
                    if _SINGLE_INSTANCE_MUTEX:
                        k32.CloseHandle(_SINGLE_INSTANCE_MUTEX)
                        _SINGLE_INSTANCE_MUTEX = None
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

        colab_requested = "--colab-s2pro" in cli_args
        root: tk.Tk | None = None
        try:
            root = tk.Tk()
            AruFishBridgeApp(root, enable_colab_on_startup=colab_requested)
        except Exception:
            first_tb = traceback.format_exc()
            try:
                if root is not None:
                    root.destroy()
            except Exception:
                pass
            if _offer_startup_settings_reset_recovery(first_tb):
                try:
                    reset_settings_to_defaults(
                        app_root=ROOT_DIR,
                        reason="startup_error_recovery",
                        preserve_fish_api_key=True,
                        reset_rvc_config=True,
                        reset_user_selectors=True,
                    )
                except Exception as reset_exc:
                    _report_fatal_startup_error(
                        "보이스 브릿지 설정 복원 오류",
                        f"설정 기본값 복원 중 오류가 발생했습니다: {reset_exc}\n\n[최초 시작 오류]\n{first_tb}",
                    )
                    raise
                root = tk.Tk()
                AruFishBridgeApp(root, enable_colab_on_startup=colab_requested)
            else:
                raise
        root.mainloop()
    except Exception:
        _report_fatal_startup_error("보이스 브릿지 실행 오류", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
