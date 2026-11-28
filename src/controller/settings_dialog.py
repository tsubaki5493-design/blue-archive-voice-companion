"""
First-Run & Preferences Settings Dialog (%LOCALAPPDATA%\\VoiceCompanionData\\config.json)
----------------------------------------------------------------------------------------
MomoTalk-styled tabbed configuration window isolating all personal credentials in
%LOCALAPPDATA% while housing:
  - Tab 0: 프롬프트 해석 버전 (공식 원작 vs 2차창작 4종) & 커스텀 페르소나 지시문
  - Tab 1: 1단계 — 스팀 음성(.molru) 추출 & Fish Audio / Gemini 연동 인증
  - Tab 2: 2단계 — 캐릭터별 Voice Reference ID, 합성 엔진, 자동 감정 디렉팅
  - Tab 3: 3단계 — 통화 모드(Mode 1 / Mode 2), 오디오 장치, 모델 · 자막 · 선톡 타이머
"""

from __future__ import annotations

import importlib
import math
import os
import queue
import re
import subprocess
import threading
import tkinter as tk
import urllib.parse
import webbrowser
from copy import deepcopy
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Callable

import pyperclip

from src.controller.gemini_connector import (
    AI_PROVIDER_LABELS,
    AI_PROVIDER_SHORT_NAMES,
    CHATGPT_API_MODELS,
    CLAUDE_API_MODELS,
    GEMINI_API_MODELS,
    GEMINI_BROWSER_MODELS,
    GEMINI_THINKING_LEVEL_LABELS,
    GEMINI_THINKING_LEVELS,
    OfficialClaudeApiBackend,
    OfficialGeminiApiBackend,
    OfficialOpenAIApiBackend,
    coerce_safe_api_model_id,
    extract_safe_api_model_id,
    get_provider_api_models,
    get_provider_browser_models,
    get_provider_default_api_model,
    get_provider_default_browser_model,
    normalize_ai_provider,
    pick_best_gemini_free_model,
    split_legacy_browser_model,
)
from src.controller.colab_s2pro_backend import (
    COLAB_S2PRO_NOTEBOOK_URL,
    ColabS2ProBackend,
    build_colab_notebook_url,
    discover_endpoint_from_drive,
    parse_colab_connection_bundle,
    validate_colab_notebook_url,
    validate_colab_server_url,
)
from src.controller.subtitle_translator import (
    get_subtitle_engine_choices,
    resolve_subtitle_engine_label,
)
from src.extractor.ba_molru_extractor import (
    discover_steam_blue_archive_dir,
    is_valid_ba_game_dir,
)
from src.utils.app_paths import (
    ensure_user_selectors_copy,
    get_user_data_dir,
    redact_secrets,
    save_user_config,
)
from src.utils.settings_recovery import (
    SettingsResetResult,
    extract_google_drive_id,
    is_virtual_loopback_mic_label,
    reset_browser_profile,
    reset_settings_to_defaults,
    sanitize_user_config,
)
from src.utils.ui_scroll import (
    ScrollableFrame,
    bind_responsive_wrap,
    fit_window_to_screen,
    make_pinned_footer,
)

# Shared label/value maps (also imported by aru_fish_bridge as single source of truth)
STT_LANG_LABELS: dict[str, str] = {
    "auto": "자동 감지 (일본어·한국어 초고속 판별)",
    "ja": "일본어 전용 (블루아카 일본어 대본용 · 최속)",
    "ko": "한국어 전용",
}
STT_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in STT_LANG_LABELS.items()}

WHISPER_MODEL_LABELS: dict[str, str] = {
    "auto": "⚡ 자동 추천 (GPU: large-v3-turbo / CPU: small · 권장)",
    "large-v3-turbo": "🌟 large-v3-turbo (최고 정확도 · 한/일 특화 · GPU 권장)",
    "medium": "🎯 medium (높은 정확도 · 중간 속도)",
    "small": "⚖️ small (가벼움 · 저사양/CPU 권장)",
    "base": "🪶 base (초경량 · 매우 빠름)",
    "tiny": "🚀 tiny (초고속 · 초저사양 CPU용)",
}
WHISPER_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in WHISPER_MODEL_LABELS.items()}

REPLY_LANG_LABELS: dict[str, str] = {
    "ja": "🇯🇵 일본어",
    "ko": "🇰🇷 한국어",
}

SPEED_MODE_LABELS: dict[str, str] = {
    "expressive": "🎭 캐릭터 감정·억양 최적화 (단어 절단 방지 · 권장)",
    "ultra": "⚡ 초저지연 스트리밍 (빠른 구절 선행 반응)",
    "full_turn": "🎬 완결 대사 통째 합성 (성우 연기·호흡 최대)",
}
SPEED_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in SPEED_MODE_LABELS.items()}

BOOST_PRESET_LABELS: dict[float, str] = {
    0.0: "표준 (캐릭터 고유 텐션 · 권장)",
    0.06: "리액션 강화 (+0.06)",
    0.12: "연기 극대화 (+0.12)",
    -0.06: "차분하게 (-0.06)",
}
BOOST_LABEL_TO_VAL: dict[str, float] = {v: k for k, v in BOOST_PRESET_LABELS.items()}

PERSONA_VARIANT_LABELS: dict[str, str] = {
    "official": "📘 프롬프트1: 공식 원작 해석 (인게임·메인/인연스토리 100% 정사)",
    "fanon_mainstream": "🎭 프롬프트2-A: 2차창작 주류 종합 (밈 티키타카 ↔ 순애 드리프트)",
    "fanon_pure_love": "💖 프롬프트2-B: 2차창작 고습도 순애 · 정실 무브 · 독점욕",
    "fanon_spicy_meme": "🔥 프롬프트2-C: 2차창작 매운맛 개그 · 음해 · 발작버튼 폭주",
    "fanon_optimal_blend": "🌟 프롬프트2-D: 캐릭터별 2차창작 최적 배합 통합본 (맞춤 황금비율 · 추천)",
}
PERSONA_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in PERSONA_VARIANT_LABELS.items()}

PROACTIVE_TIMEOUT_LABELS: dict[float, str] = {
    12.0: "12초 (적극 리드)",
    15.0: "15초",
    18.0: "18초 (권장)",
    25.0: "25초",
    35.0: "35초 (여유)",
}
PROACTIVE_TIMEOUT_VALS: dict[str, float] = {v: k for k, v in PROACTIVE_TIMEOUT_LABELS.items()}

PROACTIVE_VOL_LABELS: dict[float, str] = {
    0.50: "50% (조용함)",
    0.75: "75%",
    1.0: "100% (권장)",
    1.35: "135% (증폭)",
}
PROACTIVE_VOL_VALS: dict[str, float] = {v: k for k, v in PROACTIVE_VOL_LABELS.items()}

FISH_MODEL_HEADERS: list[str] = ["s2.1-pro-free", "s2.1-pro", "s2-pro", "s1"]

# Feature flag: keep on-demand 1-click auto voice generation UI hidden/disabled by default
ENABLE_ON_DEMAND_VOICE_UI: bool = False

AUTO_VOICE_VISIBILITY_LABELS: dict[str, str] = {
    "private": "🔒 비공개 private (Pro 무제한 · Plus 10개 한도 · 기본)",
    "unlist": "🔗 링크공개 unlist (목록 비노출 · 링크 보유자 전용)",
    "public": "🌐 공개 public (Plus 요금제 무제한 · 초상화 자동 첨부)",
}
AUTO_VOICE_VIS_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in AUTO_VOICE_VISIBILITY_LABELS.items()}

AUTO_VOICE_PROFILE_LABELS: dict[str, str] = {
    "plus200": "👑 Plus 최고품질 200초 (210초 한도 · 최대 30클립 · 추천)",
    "pro85": "💎 고품질 85초 (무료 90초 한도 · 최대 14클립)",
    "standard45": "⚡ 표준 45초 (최대 8클립 · 빠른 생성)",
}
AUTO_VOICE_PROF_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in AUTO_VOICE_PROFILE_LABELS.items()}

VAD_PRESETS: dict[str, float] = {
    "민감 (조용한 방 · RMS 0.007)": 0.007,
    "표준 (권장 · RMS 0.010)": 0.010,
    "둔감 (스피커/주변소음 · RMS 0.022)": 0.022,
}


_FISH_URL_RE = re.compile(r"(?:https://(?:www\.)?)?fish\.audio/(?:[a-zA-Z_-]+/)?m/([a-zA-Z0-9_-]{8,64})")
_FISH_HEX_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")
_SAFE_REF_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_API_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_KEY_PASTE_LEN = 512

ALLOWED_EXTERNAL_URLS: frozenset[str] = frozenset(
    {
        "https://fish.audio/app/api-keys/",
        "https://fish.audio/discovery/",
        "https://aistudio.google.com/apikey",
        "https://platform.claude.com/settings/keys",
        "https://platform.openai.com/api-keys",
    }
)
ALLOWED_EXTERNAL_HOSTS: frozenset[str] = frozenset(
    {
        "fish.audio",
        "aistudio.google.com",
        "platform.claude.com",
        "platform.openai.com",
    }
)


def validate_external_https_url(url: str) -> str:
    """Validate `url` strictly against the allowlisted HTTPS endpoints (blocks non-HTTPS, creds, control chars)."""
    s = str(url or "").strip()
    if not s or any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        raise ValueError("Invalid external URL")
    parsed = urllib.parse.urlsplit(s)
    if parsed.scheme != "https":
        raise ValueError(f"Only https:// URLs are permitted (got {parsed.scheme!r})")
    if parsed.username or parsed.password:
        raise ValueError("Embedded URL credentials are not permitted")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_EXTERNAL_HOSTS:
        raise ValueError(f"External URL host is not allowlisted: {host!r}")
    if s not in ALLOWED_EXTERNAL_URLS:
        raise ValueError("External URL is not in strict allowlist")
    return s


def open_safe_external_url(url: str) -> bool:
    """Open only a strictly validated, allowlisted HTTPS URL in the user's browser."""
    safe_url = validate_external_https_url(url)
    return bool(webbrowser.open(safe_url, new=2))


def _validate_user_selectors_path(candidate: Path | None = None) -> Path:
    """Ensure selectors.json resolves to a regular non-symlink file inside `%LOCALAPPDATA%\\VoiceCompanionData`."""
    user_dir = get_user_data_dir().resolve()
    raw_path = Path(candidate) if candidate is not None else ensure_user_selectors_copy()
    if "\x00" in str(raw_path):
        raise ValueError("Invalid selectors.json path")
    if raw_path.is_symlink():
        raise ValueError("Refusing to open symlinked selectors.json")
    resolved = raw_path.resolve()
    if resolved.parent != user_dir or resolved.name.lower() != "selectors.json":
        raise ValueError("selectors.json must reside directly inside user data directory")
    if not resolved.is_file():
        raise FileNotFoundError("selectors.json file does not exist")
    return resolved


def _sanitize_single_line_secret(raw: str) -> str:
    """Strip NUL/CRLF/control characters and bound length for pasted API keys."""
    s = str(raw or "").replace("\x00", "")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    cleaned = "".join(ch for ch in first if ord(ch) >= 32 and ord(ch) != 127)
    return cleaned[:_MAX_KEY_PASTE_LEN]


def _validate_api_model_name(
    raw: str,
    default: str = "gemini-3.5-flash-lite",
    provider: str = "gemini",
) -> str:
    s = str(raw or "").strip()
    if not s:
        return default
    cleaned = extract_safe_api_model_id(s, provider=provider)
    if not cleaned:
        raise ValueError("유효하지 않은 API 모델 이름입니다. 영문/숫자/점/하이픈만 사용해 주세요.")
    return cleaned


def _clean_ref_id(raw: str) -> str:
    s = _sanitize_single_line_secret(raw)
    if not s:
        return ""
    m = _FISH_URL_RE.search(s)
    if m:
        return m.group(1)
    m_hex = _FISH_HEX_RE.search(s)
    if m_hex:
        return m_hex.group(1).lower()
    cand = s.split("?")[0].split("#")[0].strip("/")
    if _SAFE_REF_ID_RE.fullmatch(cand):
        return cand
    return ""


def _build_api_warning_text(provider: str = "gemini") -> str:
    """Build a clear, beginner-friendly Korean warning for AI API mode and API key issuance."""
    p = normalize_ai_provider(provider)
    portal_name = {
        "gemini": "Google AI Studio (Gemini API)",
        "claude": "Anthropic Console (Claude API)",
        "chatgpt": "OpenAI Platform (ChatGPT API)",
    }[p]
    return (
        f"[{portal_name} 이용 · API 키 발급 전 주의사항]\n\n"
        "• 일반/무료 계정은 API 키 없이 기본값인 「🌐 전용 브라우저 연동(웹 로그인)」을 "
        "사용하는 것이 한도 초과(429)나 오류 없이 가장 안정적입니다.\n"
        "• 무료 Google AI Studio 키는 분당/일일 호출 한도(RPM·RPD) 제한으로 "
        "오래 대화하면 응답이 지연·중단될 수 있습니다.\n"
        "• 결제(Billing)가 연결된 Gemini 프로젝트나 Claude/OpenAI API 키는 "
        "사용량에 따라 실제 요금이 청구됩니다.\n"
        "• 발급받은 API 키는 외부에 절대 공유하지 마세요. "
        "(이 앱은 사용자 PC 로컬 암호화 폴더에만 저장합니다.)"
    )


ROOT_DIR = Path(__file__).resolve().parents[2]


def _is_screen_vision_available(project_root: Path | None = None) -> bool:
    """Return True iff the local-only `src.screen_vision` package is present and importable."""
    try:
        root = (project_root if project_root is not None else ROOT_DIR).resolve()
        init_py = root / "src" / "screen_vision" / "__init__.py"
        if not init_py.is_file() or init_py.is_symlink():
            return False
        if not init_py.resolve().is_relative_to(root):
            return False
        mod = importlib.import_module("src.screen_vision")
        importlib.import_module("src.screen_vision.config")
        avail_fn = getattr(mod, "is_screen_vision_available", None)
        if callable(avail_fn):
            return bool(avail_fn())
        return bool(getattr(mod, "SCREEN_VISION_AVAILABLE", True))
    except Exception:
        return False


class SettingsDialog(tk.Toplevel):
    """Modal MomoTalk-themed settings window housing prompt version and Steps 1-3."""

    def __init__(
        self,
        parent: tk.Misc,
        cfg: dict[str, Any],
        on_saved: Callable[[dict[str, Any]], None],
        first_run: bool = False,
        on_open_logs: Callable[[], None] | None = None,
        extra_api_models: list[str] | None = None,
        extra_web_models: list[tuple[str, str]] | None = None,
        web_models: list[tuple[str, str]] | None = None,
        on_sync_web_models: Callable[[Callable[[list[tuple[str, str]], str, str], None]], None] | None = None,
        app: Any = None,
        initial_tab: int = 0,
        on_reset_defaults: Callable[[SettingsResetResult], None] | None = None,
        on_reset_browser_profile: Callable[[], tuple[bool, str]] | None = None,
        on_screen_vision_saved: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(parent)
        sanitized_cfg, _ = sanitize_user_config(cfg if isinstance(cfg, dict) else {})
        self.cfg = deepcopy(sanitized_cfg)
        self.on_saved = on_saved
        self.on_reset_defaults = on_reset_defaults
        self.on_reset_browser_profile = on_reset_browser_profile
        self.on_screen_vision_saved = on_screen_vision_saved
        self.first_run = first_run
        self.on_open_logs = on_open_logs
        self._on_sync_web_models = on_sync_web_models
        self.app = app
        self._sv_available: bool = _is_screen_vision_available()
        self._sv_cfg: dict[str, Any] | None = None
        self._sv_denylist: list[str] = []
        max_tab_idx = 4 if self._sv_available else 3
        self._initial_tab = 1 if (first_run and int(initial_tab) == 0) else max(0, min(max_tab_idx, int(initial_tab)))
        self._ref_trace_token: str | None = None
        self._ref_kr_trace_token: str | None = None
        self._ref_jp_trace_token: str | None = None
        self._ref_active_trace_token: str | None = None
        self._reply_lang_trace_token: str | None = None
        self._syncing_voice_lang: bool = False
        self._syncing_ref_vars: bool = False
        self._syncing_alt_vars: bool = False
        self._alt_voice_trace_tokens: list[tuple[tk.StringVar, str]] = []
        self._deleted_voice_slots: set[tuple[str, str]] = set()
        self._destroyed: bool = False
        self._pending_after_ids: set[str] = set()
        self._ui_dispatch_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._ui_dispatch_after_id: str | None = None
        self._colab_key_shown: bool = False
        self._start_ui_dispatch_pump()

        self._initial_provider = normalize_ai_provider(self.cfg.get("ai_provider", "gemini"))
        self._active_provider_ui = self._initial_provider

        def _parse_web_pairs(raw_list: Any) -> list[tuple[str, str]]:
            out: list[tuple[str, str]] = []
            for item in raw_list or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    c, l = str(item[0]).strip(), str(item[1]).strip()
                    if c and l:
                        out.append((c, l))
            if len(out) == 1 and out[0][0].lower() == "default":
                return []
            return out

        def _clean_extra_api_list(raw_list: Any, provider: str) -> list[str]:
            out: list[str] = []
            seen: set[str] = set()
            for item in raw_list or []:
                cid = extract_safe_api_model_id(item, provider=provider)
                if cid and cid not in seen:
                    seen.add(cid)
                    out.append(cid)
            return out

        gem_extra_raw = extra_api_models if extra_api_models is not None else (self.cfg.get("api_models", []) or [])
        self._provider_extra_api_models: dict[str, list[str]] = {
            "gemini": _clean_extra_api_list(gem_extra_raw, "gemini"),
            "claude": _clean_extra_api_list(self.cfg.get("claude_api_models", []) or [], "claude"),
            "chatgpt": _clean_extra_api_list(self.cfg.get("openai_api_models", []) or [], "chatgpt"),
        }
        self._extra_api_models = list(self._provider_extra_api_models[self._active_provider_ui])
        self._extra_web_models = list(extra_web_models or [])
        gem_web = list(web_models) if web_models else _parse_web_pairs(self.cfg.get("web_models"))
        self._provider_account_web_models: dict[str, list[tuple[str, str]]] = {
            "gemini": gem_web,
            "claude": _parse_web_pairs(self.cfg.get("claude_web_models")),
            "chatgpt": _parse_web_pairs(self.cfg.get("chatgpt_web_models")),
        }
        self._account_web_models: list[tuple[str, str]] = list(
            self._provider_account_web_models[self._active_provider_ui]
        )
        cur_gem_bmodel, cur_tlevel = split_legacy_browser_model(
            str(self.cfg.get("gemini_browser_model", "hydrogen")),
            str(self.cfg.get("gemini_thinking_level", "low")),
        )
        self._provider_browser_model_codes: dict[str, str] = {
            "gemini": cur_gem_bmodel,
            "claude": str(self.cfg.get("claude_browser_model", "default") or "default").strip(),
            "chatgpt": str(self.cfg.get("chatgpt_browser_model", "default") or "default").strip(),
        }
        self._provider_api_model_selected: dict[str, str] = {
            "gemini": coerce_safe_api_model_id(
                self.cfg.get("gemini_api_model"),
                provider="gemini",
                default="gemini-3.5-flash-lite",
            ),
            "claude": coerce_safe_api_model_id(
                self.cfg.get("claude_api_model"),
                provider="claude",
                default="claude-sonnet-5-5",
            ),
            "chatgpt": coerce_safe_api_model_id(
                self.cfg.get("openai_api_model"),
                provider="chatgpt",
                default="gpt-5.4-mini",
            ),
        }
        self._refresh_provider_model_catalogs(self._active_provider_ui)

        title_prefix = "초기 설정 안내" if first_run else "MomoTalk Live 환경 설정"
        self.title(f"⚙️ {title_prefix} — 프롬프트 해석 · 1단계~3단계 · 개인정보 격리")
        self._dlg_w = 900
        self._dlg_h = 700
        self._tab_scrollers: list[ScrollableFrame] = []
        self._tab_scroller_by_widget: dict[str, ScrollableFrame] = {}
        self._current_build_tab_idx: int = 0
        self._searchable_sections: list[dict[str, Any]] = []
        self._search_matches: list[dict[str, Any]] = []
        self._search_match_idx: int = -1
        self._search_debounce_id: str | None = None
        self._search_trace_token: str | None = None
        self._suppress_tab_scroll_top: bool = False
        self._wrap_refreshers: list[Callable[[], None]] = []
        self.search_var = tk.StringVar(value="")
        self.configure(bg="#F3F5F8")
        self.transient(parent)
        try:
            self.grab_set()
        except tk.TclError:
            pass

        self._show_fish_key = False
        self._show_gem_key = False
        self._show_claude_key = False
        self._show_openai_key = False

        # Persona / Prompt version vars
        saved_pvar = str(self.cfg.get("persona_prompt_variant", "official") or "official")
        self.persona_variant_var = tk.StringVar(
            value=PERSONA_VARIANT_LABELS.get(saved_pvar, PERSONA_VARIANT_LABELS["official"])
        )
        self.proactive_prompt_var = tk.BooleanVar(value=bool(self.cfg.get("proactive_prompt_append", True)))
        saved_rlang = "ko" if str(self.cfg.get("reply_language", "ja") or "ja").strip().lower() == "ko" else "ja"
        self._voice_editor_lang: str = saved_rlang
        self.reply_language_var = tk.StringVar(value=saved_rlang)
        self.jp_beginner_coach_var = tk.BooleanVar(value=bool(self.cfg.get("jp_beginner_lover_mode", False)))
        self.use_custom_persona_var = tk.BooleanVar(value=bool(self.cfg.get("use_custom_persona", False)))

        # Step 1: API keys, Fish MCP OAuth / Credit-Saver / Cache, Steam path & auth vars
        self.fish_key_var = tk.StringVar(value=str(self.cfg.get("fish_api_key", "")))
        self.fish_mcp_fallback_var = tk.BooleanVar(value=bool(self.cfg.get("fish_mcp_fallback_enabled", True)))
        self.fish_mcp_primary_var = tk.BooleanVar(value=bool(self.cfg.get("fish_mcp_primary_mode", False)))
        self.fish_credit_saver_var = tk.BooleanVar(value=bool(self.cfg.get("fish_credit_saver_mode", True)))
        self.fish_tts_cache_var = tk.BooleanVar(value=bool(self.cfg.get("fish_tts_cache_enabled", True)))
        self.colab_s2pro_enabled_var = tk.BooleanVar(value=bool(self.cfg.get("colab_s2pro_enabled", False)))
        self.colab_s2pro_url_var = tk.StringVar(value=str(self.cfg.get("colab_s2pro_url", "") or ""))
        self.colab_s2pro_key_var = tk.StringVar(value=str(self.cfg.get("colab_s2pro_api_key", "") or ""))
        self.colab_s2pro_drive_folder_var = tk.StringVar(
            value=str(self.cfg.get("colab_s2pro_drive_folder_id", "") or "")
        )
        self.colab_s2pro_notebook_id_var = tk.StringVar(
            value=str(self.cfg.get("colab_s2pro_notebook_id", "") or "")
        )
        self.colab_s2pro_endpoint_file_var = tk.StringVar(
            value=str(self.cfg.get("colab_s2pro_endpoint_file", "") or "")
        )
        self.colab_s2pro_status_var = tk.StringVar(
            value="☁️ 연결 대기 중 (Colab 서버 실행 후 '연결 정보 붙여넣기' 또는 'Drive 자동 가져오기' 클릭)"
            if not str(self.cfg.get("colab_s2pro_url", "") or "").strip()
            else f"☁️ 설정됨 ({str(self.cfg.get('colab_s2pro_url', '')).strip()[:42]})"
        )
        self._fish_mcp_busy: bool = False
        self._fish_mcp_login_in_progress: bool = False
        self._fish_mcp_refresh_in_progress: bool = False
        self._fish_mcp_login_seq: int = 0
        self._fish_mcp_cancelled_seq: int = 0
        self._fish_mcp_live_status: str = ""
        self._fish_mcp_attempt_cancel_event: Any = None
        self._fish_mcp_prompt_open: bool = False
        self.steam_game_dir_var = tk.StringVar(value=str(self.cfg.get("steam_game_dir", "") or ""))
        self.ai_provider_var = tk.StringVar(value=self._active_provider_ui)
        saved_backend = str(self.cfg.get("gemini_backend", "browser") or "browser").strip().lower()
        if saved_backend not in ("browser", "api"):
            saved_backend = "browser"
        self.gemini_backend_var = tk.StringVar(value=saved_backend)
        self._last_backend_ui: str = saved_backend
        self.gemini_key_var = tk.StringVar(value=str(self.cfg.get("gemini_api_key", "")))
        self.claude_key_var = tk.StringVar(value=str(self.cfg.get("claude_api_key", "")))
        self.openai_key_var = tk.StringVar(value=str(self.cfg.get("openai_api_key", "")))

        # Step 2: Per-character Voice & Directing vars
        saved_auto_vis = str(self.cfg.get("auto_voice_visibility", "private") or "private").strip().lower()
        self.auto_voice_vis_var = tk.StringVar(
            value=AUTO_VOICE_VISIBILITY_LABELS.get(saved_auto_vis, AUTO_VOICE_VISIBILITY_LABELS["private"])
        )
        saved_auto_prof = str(self.cfg.get("auto_voice_quality_profile", "plus200") or "plus200").strip().lower()
        self.auto_voice_prof_var = tk.StringVar(
            value=AUTO_VOICE_PROFILE_LABELS.get(saved_auto_prof, AUTO_VOICE_PROFILE_LABELS["plus200"])
        )
        cid = self._current_character_id()
        raw_saved_jp = self.cfg.get("saved_voices")
        raw_saved_kr = self.cfg.get("saved_voices_kr")
        saved_jp = raw_saved_jp if isinstance(raw_saved_jp, dict) else {}
        saved_kr = raw_saved_kr if isinstance(raw_saved_kr, dict) else {}
        init_ref_kr = str(saved_kr.get(cid) or "")
        init_ref_jp = str(saved_jp.get(cid) or ("" if saved_rlang == "ko" else self.cfg.get("fish_reference_id", "")) or "")
        self.ref_id_kr_var = tk.StringVar(value=init_ref_kr)
        self.ref_id_jp_var = tk.StringVar(value=init_ref_jp)
        self.ref_id_var = tk.StringVar(value=(init_ref_kr if saved_rlang == "ko" else init_ref_jp))
        # Dual-voice alt persona slots (e.g. Kisaki -> Kisaki_Kiki), populated in _rebuild_alt_voice_controls()
        self._alt_voice_specs: list[tuple[str, str, str]] = []  # (alt_key, slot_id, label_kr)
        self._alt_voice_vars: dict[str, tk.StringVar] = {}
        self._alt_voice_kr_vars: dict[str, tk.StringVar] = {}
        self._alt_voice_jp_vars: dict[str, tk.StringVar] = {}
        self._alt_voice_status_labels: dict[tuple[str, str], tk.Label] = {}
        init_hdr = str(self.cfg.get("fish_model_header", "s2.1-pro-free") or "s2.1-pro-free")
        if init_hdr not in FISH_MODEL_HEADERS:
            init_hdr = "s2.1-pro-free"
        self.fish_model_hdr_var = tk.StringVar(value=init_hdr)
        saved_speed = str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive")
        self.speed_mode_var = tk.StringVar(
            value=SPEED_MODE_LABELS.get(saved_speed, SPEED_MODE_LABELS["expressive"])
        )
        self.auto_emotion_var = tk.BooleanVar(value=bool(self.cfg.get("auto_emotion_directing", True)))
        saved_boost = self._safe_float(self.cfg.get("expression_boost_offset", 0.0), 0.0, -0.25, 0.25)
        closest_boost = min(BOOST_PRESET_LABELS.keys(), key=lambda k: abs(k - saved_boost))
        self.boost_preset_var = tk.StringVar(value=BOOST_PRESET_LABELS[closest_boost])
        self.vol_var = tk.DoubleVar(value=self._safe_float(self.cfg.get("output_volume", 1.0), 1.0, 0.10, 2.00))

        # Step 3: Call mode, Devices, Models, Subtitles, Proactive timers
        saved_cbm = str(self.cfg.get("call_bridge_mode", "") or "").strip()
        if saved_cbm not in ("mode1", "mode2"):
            saved_cbm = "mode2" if str(self.cfg.get("bridge_mode", "pc_mic")) == "gemini_live_audio" else "mode1"
        self.call_bridge_mode_var = tk.StringVar(value=saved_cbm)

        app_out_raw = app.out_choice_var.get().strip() if app is not None and hasattr(app, "out_choice_var") else ""
        if app_out_raw.startswith("("):
            app_out_raw = ""
        self.primary_out_var = tk.StringVar(
            value=(app_out_raw or str(self.cfg.get("primary_output_device", "") or ""))
        )
        app_mic_raw = app.mic_choice_var.get().strip() if app is not None and hasattr(app, "mic_choice_var") else ""
        if app_mic_raw.startswith("(") or is_virtual_loopback_mic_label(app_mic_raw):
            app_mic_raw = ""
        cfg_mic_raw = str(self.cfg.get("mic_input_device", "") or "").strip()
        if cfg_mic_raw.startswith("(") or is_virtual_loopback_mic_label(cfg_mic_raw):
            cfg_mic_raw = ""
        self.mic_input_var = tk.StringVar(value=(app_mic_raw or cfg_mic_raw))
        self.vb_in_var = tk.StringVar(
            value=(app.in_choice_var.get() if app is not None and hasattr(app, "in_choice_var") else "")
        )
        app_trig_raw = (
            app.trigger_out_choice_var.get().strip()
            if app is not None and hasattr(app, "trigger_out_choice_var")
            else ""
        )
        if app_trig_raw.startswith("("):
            app_trig_raw = ""
        self.proactive_trig_dev_var = tk.StringVar(
            value=(app_trig_raw or str(self.cfg.get("proactive_trigger_device", "") or ""))
        )

        saved_lang = str(self.cfg.get("stt_language", "auto") or "auto")
        self.stt_lang_var = tk.StringVar(value=STT_LANG_LABELS.get(saved_lang, STT_LANG_LABELS["auto"]))

        saved_wm = str(self.cfg.get("whisper_model", "auto") or "auto").strip().lower()
        if saved_wm == "turbo":
            saved_wm = "large-v3-turbo"
        self.whisper_model_var = tk.StringVar(
            value=WHISPER_MODEL_LABELS.get(saved_wm, WHISPER_MODEL_LABELS["auto"])
        )

        cur_vad = self._safe_float(self.cfg.get("mic_vad_rms_threshold", 0.010), 0.010, 0.001, 0.25)
        closest_vad_lbl = min(VAD_PRESETS.keys(), key=lambda k: abs(VAD_PRESETS[k] - cur_vad))
        self.vad_preset_var = tk.StringVar(value=closest_vad_lbl)

        self.gemini_model_var = tk.StringVar(value=self._provider_api_model_selected[self._active_provider_ui])
        cur_bmodel = self._provider_browser_model_codes[self._active_provider_ui]
        default_bcode = get_provider_default_browser_model(self._active_provider_ui)
        self.gemini_browser_model_var = tk.StringVar(
            value=self._browser_lbl_by_code.get(
                cur_bmodel,
                self._browser_lbl_by_code.get(default_bcode, self._all_browser_models[0][1] if self._all_browser_models else ""),
            )
        )
        self.gemini_thinking_var = tk.StringVar(
            value=GEMINI_THINKING_LEVEL_LABELS.get(cur_tlevel, GEMINI_THINKING_LEVEL_LABELS["low"])
        )
        cur_sub_eng = str(self.cfg.get("subtitle_engine", "auto_inline"))
        self.subtitle_engine_var = tk.StringVar(
            value=resolve_subtitle_engine_label(cur_sub_eng, self._all_sub_choices)
        )
        cur_sub_tlevel = str(self.cfg.get("subtitle_thinking_level", "low"))
        if cur_sub_tlevel not in GEMINI_THINKING_LEVEL_LABELS:
            cur_sub_tlevel = "low"
        self.subtitle_thinking_var = tk.StringVar(
            value=GEMINI_THINKING_LEVEL_LABELS[cur_sub_tlevel]
        )

        self.companion_proactive_var = tk.BooleanVar(value=bool(self.cfg.get("companion_proactive_enable", True)))
        min_idle_val = self._safe_float(self.cfg.get("idle_ping_min_sec", 45.0), 45.0, 5.0, 3600.0)
        max_idle_val = self._safe_float(self.cfg.get("idle_ping_max_sec", 75.0), max(min_idle_val, 75.0), min_idle_val, 7200.0)
        afk_val = self._safe_int(self.cfg.get("afk_max_consecutive_pings", 2), 2, 1, 99)
        self.min_idle_var = tk.StringVar(value=str(int(round(min_idle_val))))
        self.max_idle_var = tk.StringVar(value=str(int(round(max_idle_val))))
        self.afk_count_var = tk.StringVar(value=str(afk_val))

        self.proactive_enable_var = tk.BooleanVar(value=bool(self.cfg.get("proactive_enable", True)))
        saved_ptime = self._safe_float(self.cfg.get("proactive_timeout_sec", 18.0), 18.0, 3.0, 300.0)
        closest_ptime = min(PROACTIVE_TIMEOUT_LABELS.keys(), key=lambda k: abs(k - saved_ptime))
        self.proactive_timeout_var = tk.StringVar(value=PROACTIVE_TIMEOUT_LABELS[closest_ptime])
        saved_pvol = self._safe_float(self.cfg.get("proactive_trigger_volume", 1.0), 1.0, 0.1, 2.0)
        closest_pvol = min(PROACTIVE_VOL_LABELS.keys(), key=lambda k: abs(k - saved_pvol))
        self.proactive_vol_var = tk.StringVar(value=PROACTIVE_VOL_LABELS[closest_pvol])

        try:
            self._reply_lang_trace_token = self.reply_language_var.trace_add(
                "write", self._on_reply_language_var_written
            )
        except Exception:
            self._reply_lang_trace_token = None
        try:
            self._ref_kr_trace_token = self.ref_id_kr_var.trace_add("write", self._on_ref_id_kr_written)
        except Exception:
            self._ref_kr_trace_token = None
        try:
            self._ref_jp_trace_token = self.ref_id_jp_var.trace_add("write", self._on_ref_id_jp_written)
        except Exception:
            self._ref_jp_trace_token = None
        try:
            self._ref_active_trace_token = self.ref_id_var.trace_add("write", self._on_ref_id_active_written)
        except Exception:
            self._ref_active_trace_token = None

        if self._sv_available:
            self._init_screen_vision_vars()

        self._build_ui()
        self._bind_app_live_sync()
        self._bind_dialog_shortcuts()
        self.update_idletasks()
        try:
            w, h, _px, _py = fit_window_to_screen(
                self,
                preferred_w=900,
                preferred_h=700,
                min_w=720,
                min_h=480,
                max_screen_ratio=0.88,
                center_on=parent,
            )
            self._dlg_w, self._dlg_h = w, h
        except Exception:
            pass
        if 0 <= self._initial_tab <= (4 if self._sv_available else 3):
            try:
                self.notebook.select(self._initial_tab)
            except Exception:
                pass
        self._refresh_all_tab_scrollers(defer=True)
        self.focus_force()
        if self.first_run and hasattr(self, "ent_fish_key"):
            try:
                self.ent_fish_key.focus_set()
            except Exception:
                pass

    @staticmethod
    def _safe_float(raw: Any, default: float, min_val: float, max_val: float) -> float:
        if raw is None or isinstance(raw, bool):
            return default
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(v):
            return default
        return max(min_val, min(max_val, v))

    @staticmethod
    def _safe_int(raw: Any, default: int, min_val: int, max_val: int) -> int:
        if raw is None or isinstance(raw, bool):
            return default
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(v):
            return default
        return max(min_val, min(max_val, int(round(v))))

    def _start_ui_dispatch_pump(self) -> None:
        """Start the single Tk main-thread pump that drains worker-thread UI callbacks safely."""
        if self._destroyed or self._ui_dispatch_after_id is not None:
            return
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            self._ui_dispatch_after_id = self.after(30, self._drain_ui_dispatch_queue)
        except Exception:
            self._ui_dispatch_after_id = None

    def _drain_ui_dispatch_queue(self) -> None:
        old_aid = self._ui_dispatch_after_id
        self._ui_dispatch_after_id = None
        if old_aid is not None:
            try:
                self.after_cancel(old_aid)
            except Exception:
                pass
        if self._destroyed:
            return
        try:
            if not self.winfo_exists():
                self._destroyed = True
                return
        except Exception:
            self._destroyed = True
            return

        while True:
            if self._destroyed:
                return
            try:
                if not self.winfo_exists():
                    self._destroyed = True
                    return
            except Exception:
                self._destroyed = True
                return
            try:
                cb = self._ui_dispatch_queue.get_nowait()
            except queue.Empty:
                break
            try:
                cb()
            except Exception:
                pass

        if not self._destroyed:
            try:
                if self.winfo_exists():
                    self._ui_dispatch_after_id = self.after(30, self._drain_ui_dispatch_queue)
            except Exception:
                self._ui_dispatch_after_id = None

    def _enqueue_ui(self, callback: Callable[[], None]) -> None:
        """Thread-safe worker-to-Tk dispatch: never calls `self.after()` off the Tk main thread."""
        if self._destroyed:
            return
        if threading.current_thread() is threading.main_thread():
            self._schedule_after(0, callback)
            return
        try:
            self._ui_dispatch_queue.put(callback)
        except Exception:
            pass

    def _schedule_after(self, delay_ms: int, callback: Callable[[], None]) -> str | None:
        """Schedule a Tk `after()` callback tracked for guaranteed cancellation on `destroy()`.

        Strictly mutates `self._pending_after_ids` and calls `self.after()` on the Tk main thread only;
        off-thread calls are routed through the thread-safe `_ui_dispatch_queue`.
        """
        if self._destroyed:
            return None
        if threading.current_thread() is not threading.main_thread():
            if delay_ms <= 0:
                self._enqueue_ui(callback)
            else:
                self._enqueue_ui(lambda d=delay_ms, c=callback: self._schedule_after(d, c))
            return None

        token_box: list[str | None] = [None]

        def _wrapped() -> None:
            tok = token_box[0]
            if tok is not None:
                self._pending_after_ids.discard(tok)
            if self._destroyed:
                return
            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            callback()

        try:
            aid = self.after(delay_ms, _wrapped)
            if isinstance(aid, str) and aid:
                token_box[0] = aid
                self._pending_after_ids.add(aid)
            return aid if isinstance(aid, str) else None
        except Exception:
            return None

    @staticmethod
    def _voice_ref_badge_style(raw_val: str) -> tuple[str, str, str]:
        """Return `(label_text, bg_hex, fg_hex)` for a Voice Reference ID status pill."""
        raw = str(raw_val or "").strip()
        if not raw:
            return ("⬜ 미등록", "#E2E8F0", "#475569")
        if _clean_ref_id(raw):
            return ("✅ 등록됨", "#DCFCE7", "#166534")
        return ("⚠️ 형식오류", "#FEF3C7", "#B45309")

    def _clear_alt_voice_traces(self) -> None:
        for var, tok in list(self._alt_voice_trace_tokens):
            try:
                var.trace_remove("write", tok)
            except Exception:
                pass
        self._alt_voice_trace_tokens.clear()

    def _refresh_voice_ref_status_ui(self) -> None:
        """Refresh KR/JP registration badges, active reply-language row highlight, and hint text on Tab 2."""
        rlang = self._get_reply_lang()
        for var_name, lbl_name in (
            ("ref_id_kr_var", "lbl_ref_kr_status"),
            ("ref_id_jp_var", "lbl_ref_jp_status"),
        ):
            lbl = getattr(self, lbl_name, None)
            var = getattr(self, var_name, None)
            if lbl is not None and var is not None:
                try:
                    txt, bg, fg = self._voice_ref_badge_style(var.get())
                    lbl.configure(text=txt, bg=bg, fg=fg)
                except Exception:
                    pass

        row_kr = getattr(self, "_row_ref_kr", None)
        lbl_kr = getattr(self, "_lbl_ref_kr_title", None)
        row_jp = getattr(self, "_row_ref_jp", None)
        lbl_jp = getattr(self, "_lbl_ref_jp_title", None)
        if row_kr is not None and row_jp is not None:
            try:
                if rlang == "ko":
                    kr_bg, kr_bd, kr_fg = "#F0FDF4", "#4ADE80", "#0F766E"
                    jp_bg, jp_bd, jp_fg = "#F8FAFC", "#E2E8F0", "#334155"
                else:
                    kr_bg, kr_bd, kr_fg = "#F8FAFC", "#E2E8F0", "#334155"
                    jp_bg, jp_bd, jp_fg = "#FFF5F7", "#FB7390", "#BE123C"
                row_kr.configure(bg=kr_bg, highlightbackground=kr_bd)
                if lbl_kr is not None:
                    lbl_kr.configure(bg=kr_bg, fg=kr_fg)
                row_jp.configure(bg=jp_bg, highlightbackground=jp_bd)
                if lbl_jp is not None:
                    lbl_jp.configure(bg=jp_bg, fg=jp_fg)
            except Exception:
                pass

        lbl_hint = getattr(self, "lbl_voice_ref_hint", None)
        if lbl_hint is not None:
            try:
                active_lang_txt = "🇰🇷 한국어(KR)" if rlang == "ko" else "🇯🇵 일본어(JP)"
                hint_txt = (
                    f"• 위(KR) / 아래(JP) 칸에서 한국어·일본어 보이스 등록 여부를 한눈에 확인하고 각각 수정·저장할 수 있습니다. "
                    f"(현재 답변 언어: {active_lang_txt})"
                )
                lbl_hint.configure(text=hint_txt)
            except Exception:
                pass

        for (slot_id, lang_key), badge in list(self._alt_voice_status_labels.items()):
            src_map = self._alt_voice_kr_vars if lang_key == "kr" else self._alt_voice_jp_vars
            alt_v = src_map.get(slot_id)
            if alt_v is None:
                continue
            try:
                txt, bg, fg = self._voice_ref_badge_style(alt_v.get())
                badge.configure(text=txt, bg=bg, fg=fg)
            except Exception:
                pass

    def _on_ref_id_kr_written(self, *_args: object) -> None:
        self._refresh_voice_ref_status_ui()
        if self._syncing_ref_vars:
            return
        if self._voice_editor_lang == "ko":
            self._syncing_ref_vars = True
            try:
                self.ref_id_var.set(self.ref_id_kr_var.get())
            finally:
                self._syncing_ref_vars = False

    def _on_ref_id_jp_written(self, *_args: object) -> None:
        self._refresh_voice_ref_status_ui()
        if self._syncing_ref_vars:
            return
        if self._voice_editor_lang != "ko":
            self._syncing_ref_vars = True
            try:
                self.ref_id_var.set(self.ref_id_jp_var.get())
            finally:
                self._syncing_ref_vars = False

    def _on_ref_id_active_written(self, *_args: object) -> None:
        if self._syncing_ref_vars:
            return
        self._syncing_ref_vars = True
        try:
            val = self.ref_id_var.get()
            if self._voice_editor_lang == "ko":
                self.ref_id_kr_var.set(val)
            else:
                self.ref_id_jp_var.set(val)
        finally:
            self._syncing_ref_vars = False
        self._refresh_voice_ref_status_ui()

    def _sync_active_voice_mirrors(self, lang: str) -> None:
        """Keep backwards-compatible single active-language mirror vars aligned with the dual KR/JP vars."""
        self._syncing_ref_vars = True
        try:
            self.ref_id_var.set(self.ref_id_kr_var.get() if lang == "ko" else self.ref_id_jp_var.get())
        finally:
            self._syncing_ref_vars = False

        self._syncing_alt_vars = True
        try:
            src_map = self._alt_voice_kr_vars if lang == "ko" else self._alt_voice_jp_vars
            for _ak, slot_id, _lbl in self._alt_voice_specs:
                act_var = self._alt_voice_vars.get(slot_id)
                src_var = src_map.get(slot_id)
                if act_var is not None and src_var is not None:
                    act_var.set(src_var.get())
        finally:
            self._syncing_alt_vars = False

    def _stash_voice_vars_for_lang(self, lang: str) -> None:
        """Persist current Tab 2 voice vars into `self.cfg` under `lang`'s bucket (ja/ko) without losing edits."""
        bucket = "saved_voices_kr" if lang == "ko" else "saved_voices"
        cid = self._current_character_id()
        if not isinstance(self.cfg.get(bucket), dict):
            self.cfg[bucket] = {}
        src_ref_var = self.ref_id_kr_var if lang == "ko" else self.ref_id_jp_var
        raw_ref = src_ref_var.get().strip()
        clean_ref = _clean_ref_id(raw_ref)
        if clean_ref:
            self.cfg[bucket][cid] = clean_ref
            self._deleted_voice_slots.discard((bucket, cid))
        elif not raw_ref and cid:
            self.cfg[bucket].pop(cid, None)
            self._deleted_voice_slots.add((bucket, cid))
        alt_src = self._alt_voice_kr_vars if lang == "ko" else self._alt_voice_jp_vars
        for _ak, slot_id, _lbl in self._alt_voice_specs:
            alt_var = alt_src.get(slot_id)
            if alt_var is None:
                continue
            raw_alt = alt_var.get().strip()
            clean_alt = _clean_ref_id(raw_alt)
            if clean_alt:
                self.cfg[bucket][slot_id] = clean_alt
                self._deleted_voice_slots.discard((bucket, slot_id))
            elif not raw_alt:
                self.cfg[bucket].pop(slot_id, None)
                self._deleted_voice_slots.add((bucket, slot_id))

    def _load_voice_vars_for_lang(self, lang: str) -> None:
        """Load BOTH KR and JP Tab 2 voice vars from `self.cfg` and align active-language mirrors to `lang`."""
        raw_kr = self.cfg.get("saved_voices_kr")
        raw_jp = self.cfg.get("saved_voices")
        saved_kr = raw_kr if isinstance(raw_kr, dict) else {}
        saved_jp = raw_jp if isinstance(raw_jp, dict) else {}
        cid = self._current_character_id()
        loaded_kr = str(saved_kr.get(cid) or "")
        loaded_jp = str(saved_jp.get(cid) or "")
        self._syncing_ref_vars = True
        try:
            self.ref_id_kr_var.set(loaded_kr)
            self.ref_id_jp_var.set(loaded_jp)
            self.ref_id_var.set(loaded_kr if lang == "ko" else loaded_jp)
        finally:
            self._syncing_ref_vars = False

        self._syncing_alt_vars = True
        try:
            for _ak, slot_id, _lbl in self._alt_voice_specs:
                v_kr = str(saved_kr.get(slot_id) or "")
                v_jp = str(saved_jp.get(slot_id) or "")
                if slot_id in self._alt_voice_kr_vars:
                    self._alt_voice_kr_vars[slot_id].set(v_kr)
                if slot_id in self._alt_voice_jp_vars:
                    self._alt_voice_jp_vars[slot_id].set(v_jp)
                if slot_id in self._alt_voice_vars:
                    self._alt_voice_vars[slot_id].set(v_kr if lang == "ko" else v_jp)
        finally:
            self._syncing_alt_vars = False
        self._refresh_voice_ref_status_ui()

    def _on_reply_language_var_written(self, *_args: object) -> None:
        """Keep Tab 2 active-language highlight & legacy mirror vars synchronized with reply language (ja vs ko)."""
        if self._syncing_voice_lang:
            return
        new_lang = "ko" if self.reply_language_var.get() == "ko" else "ja"
        old_lang = self._voice_editor_lang
        self._syncing_voice_lang = True
        try:
            if new_lang == "ko" and self.jp_beginner_coach_var.get():
                self.jp_beginner_coach_var.set(False)
            if old_lang != new_lang:
                self._voice_editor_lang = new_lang
                self._sync_active_voice_mirrors(new_lang)
            self._refresh_voice_ref_status_ui()
        finally:
            self._syncing_voice_lang = False

    def _refresh_provider_model_catalogs(self, provider: str) -> None:
        p = normalize_ai_provider(provider)
        acct_web = list(self._provider_account_web_models.get(p) or [])
        self._account_web_models = acct_web
        base_web = acct_web if acct_web else get_provider_browser_models(p)
        base_web_codes = {c for c, _ in base_web}
        extra_web = self._extra_web_models if p == "gemini" else []
        self._all_browser_models = base_web + [(c, l) for c, l in extra_web if c not in base_web_codes]
        self._browser_lbl_by_code = dict(self._all_browser_models)
        self._extra_api_models = list(self._provider_extra_api_models.get(p) or [])
        self._all_sub_choices = get_subtitle_engine_choices(
            self._extra_api_models,
            extra_web,
            web_models=acct_web or None,
            provider=p,
        )

    def _build_api_model_dropdown_values(self, provider: str) -> list[str]:
        """Build clean, deduplicated API model dropdown IDs (prioritizing fetched/free-compatible models)."""
        p = normalize_ai_provider(provider)
        presets = [code for code, _ in get_provider_api_models(p)]
        extras = list(self._provider_extra_api_models.get(p) or [])
        if not extras:
            return presets
        extra_set = set(extras)
        ordered = [m for m in presets if m in extra_set]
        ordered.extend(m for m in extras if m not in ordered)
        ordered.extend(m for m in presets if m not in ordered)
        return ordered

    def _current_character_id(self) -> str:
        if self.app is not None and getattr(self.app, "current_char_id", None):
            return str(self.app.current_char_id)
        return str(self.cfg.get("selected_character_id") or "Aru")

    def _current_character_title(self) -> str:
        if self.app is not None and hasattr(self.app, "characters"):
            cid = self._current_character_id()
            ch = (self.app.characters or {}).get(cid) or {}
            if ch:
                base = f"{ch.get('name_kr', cid)} ({ch.get('name_jp', cid)}) · {ch.get('school', '')}"
                eid = getattr(self.app, "current_momotalk_entry_id", "")
                emap = getattr(self.app, "momotalk_entry_map", {}) or {}
                ent = emap.get(eid)
                if ent and ent.get("is_costume_variant"):
                    return f"{ent.get('name_kr', eid)} · {base}"
                return base
        return self._current_character_id()

    def _bind_app_live_sync(self) -> None:
        if self.app is None or not hasattr(self.app, "ref_id_var"):
            return

        def _on_app_ref_change(*_args: object) -> None:
            try:
                if not self.winfo_exists():
                    return
                new_val = self.app.ref_id_var.get().strip()
                if not new_val:
                    return
                app_rlang = self._voice_editor_lang
                if hasattr(self.app, "_get_reply_language"):
                    app_rlang = "ko" if self.app._get_reply_language() == "ko" else "ja"
                elif hasattr(self.app, "reply_language_var"):
                    app_rlang = "ko" if self.app.reply_language_var.get() == "ko" else "ja"
                target_var = self.ref_id_kr_var if app_rlang == "ko" else self.ref_id_jp_var
                if new_val != target_var.get().strip():
                    target_var.set(new_val)
            except Exception:
                pass

        try:
            self._ref_trace_token = self.app.ref_id_var.trace_add("write", _on_app_ref_change)
        except Exception:
            self._ref_trace_token = None

    def destroy(self) -> None:
        self._destroyed = True
        if getattr(self, "_ui_dispatch_after_id", None) is not None:
            try:
                self.after_cancel(self._ui_dispatch_after_id)  # type: ignore[arg-type]
            except Exception:
                pass
            self._ui_dispatch_after_id = None
        dq = getattr(self, "_ui_dispatch_queue", None)
        if dq is not None:
            while True:
                try:
                    dq.get_nowait()
                except queue.Empty:
                    break
        if getattr(self, "_search_debounce_id", None) is not None:
            try:
                self.after_cancel(self._search_debounce_id)  # type: ignore[arg-type]
            except Exception:
                pass
            self._search_debounce_id = None
        if getattr(self, "_search_trace_token", None):
            try:
                self.search_var.trace_remove("write", self._search_trace_token)  # type: ignore[arg-type]
            except Exception:
                pass
            self._search_trace_token = None
        for aid in list(self._pending_after_ids):
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self._pending_after_ids.clear()
        attempt_ev = getattr(self, "_fish_mcp_attempt_cancel_event", None)
        if attempt_ev is not None:
            try:
                attempt_ev.set()
            except Exception:
                pass
            self._fish_mcp_attempt_cancel_event = None
        if getattr(self, "_fish_mcp_login_in_progress", False):
            self._fish_mcp_login_in_progress = False
            self._fish_mcp_busy = False
            self._fish_mcp_login_seq += 1
            if self.app is not None and hasattr(self.app, "fish_mcp_cancel_login"):
                try:
                    self.app.fish_mcp_cancel_login()
                except Exception:
                    pass
        if self._reply_lang_trace_token:
            try:
                self.reply_language_var.trace_remove("write", self._reply_lang_trace_token)
            except Exception:
                pass
            self._reply_lang_trace_token = None
        for var_attr, tok_attr in (
            ("ref_id_kr_var", "_ref_kr_trace_token"),
            ("ref_id_jp_var", "_ref_jp_trace_token"),
            ("ref_id_var", "_ref_active_trace_token"),
        ):
            tok = getattr(self, tok_attr, None)
            var = getattr(self, var_attr, None)
            if tok and var is not None:
                try:
                    var.trace_remove("write", tok)
                except Exception:
                    pass
                setattr(self, tok_attr, None)
        self._clear_alt_voice_traces()
        if self.app is not None and self._ref_trace_token and hasattr(self.app, "ref_id_var"):
            try:
                self.app.ref_id_var.trace_remove("write", self._ref_trace_token)
            except Exception:
                pass
            self._ref_trace_token = None
        self._show_fish_key = False
        self._show_gem_key = False
        self._show_claude_key = False
        self._show_openai_key = False
        self._colab_key_shown = False
        super().destroy()

    def _bind_dialog_shortcuts(self) -> None:
        """Bind convenient, non-conflicting keyboard shortcuts (Ctrl+S, Ctrl+F, Esc)."""
        for seq in ("<Control-s>", "<Control-S>"):
            self.bind(seq, self._on_shortcut_save, add="+")
        for seq in ("<Control-f>", "<Control-F>"):
            self.bind(seq, self._on_shortcut_search, add="+")
        self.bind("<Escape>", self._on_shortcut_escape, add="+")

    def _on_shortcut_save(self, _event: Any = None) -> str:
        self._save_and_close()
        return "break"

    def _on_shortcut_search(self, _event: Any = None) -> str:
        ent = getattr(self, "ent_settings_search", None)
        if ent is not None:
            try:
                ent.focus_set()
                ent.select_range(0, tk.END)
            except Exception:
                pass
        return "break"

    def _on_shortcut_escape(self, _event: Any = None) -> str | None:
        try:
            focused = self.focus_get()
        except Exception:
            focused = None
        # Do not hijack Esc while a Combobox dropdown popdown is open
        if focused is not None:
            try:
                if ".popdown" in str(focused).lower():
                    return None
                if str(focused.winfo_class()) == "TCombobox":
                    pop = focused.tk.call("ttk::combobox::PopdownWindow", str(focused))
                    if pop and int(focused.tk.call("winfo", "ismapped", pop)):
                        return None
            except Exception:
                pass
        ent = getattr(self, "ent_settings_search", None)
        if ent is not None and focused is ent and self.search_var.get().strip():
            self._clear_settings_search()
            return "break"
        self.destroy()
        return "break"

    def _wrap(self, widget: tk.Misc, *, pad: int = 32, min_wrap: int = 220, container: tk.Misc | None = None) -> None:
        """Attach responsive Korean text wrapping to `widget` and track its refresher."""
        refresher = bind_responsive_wrap(widget, pad=pad, min_wrap=min_wrap, container=container)
        self._wrap_refreshers.append(refresher)

    def _refresh_tab_scroll(self, tab_idx: int | None = None, *, defer: bool = False) -> None:
        """Refresh scrollregion on the specified (or currently active) tab's ScrollableFrame."""
        if not self._tab_scrollers:
            return
        idx = tab_idx
        if idx is None:
            try:
                sel = self.notebook.select()
                sf = self._tab_scroller_by_widget.get(str(sel))
                if sf is not None:
                    sf.refresh_scrollregion(defer=defer)
                return
            except Exception:
                return
        if 0 <= idx < len(self._tab_scrollers):
            self._tab_scrollers[idx].refresh_scrollregion(defer=defer)

    def _refresh_all_tab_scrollers(self, *, defer: bool = False) -> None:
        for sf in self._tab_scrollers:
            try:
                sf.refresh_scrollregion(defer=defer)
            except Exception:
                pass

    def _on_notebook_tab_changed(self, _event: Any = None) -> None:
        try:
            sel = str(self.notebook.select())
        except Exception:
            return
        sf = self._tab_scroller_by_widget.get(sel)
        if sf is None:
            return
        sf.refresh_scrollregion(defer=False)
        sf.refresh_scrollregion(defer=True)
        if not self._suppress_tab_scroll_top:
            sf.scroll_to_top()

    def _collect_card_search_text(self, card: tk.Misc, title: str, keywords: str) -> str:
        parts = [title, keywords]
        stack: list[Any] = [card]
        while stack:
            cur = stack.pop()
            try:
                for ch in cur.winfo_children():
                    try:
                        t = str(ch.cget("text") or "").strip()
                        if t:
                            parts.append(t)
                    except Exception:
                        pass
                    stack.append(ch)
            except Exception:
                pass
        return " ".join(parts).casefold()

    def _on_search_query_changed(self, *_args: object) -> None:
        if self._destroyed:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self._search_debounce_id is not None:
            try:
                self.after_cancel(self._search_debounce_id)
            except Exception:
                pass
            self._search_debounce_id = None
        try:
            self._search_debounce_id = self.after(90, self._run_settings_search)
        except Exception:
            self._search_debounce_id = None

    def _clear_search_highlights(self) -> None:
        for sec in self._searchable_sections:
            card = sec.get("card")
            if card is not None:
                try:
                    card.configure(highlightbackground="#E2E8F0", highlightthickness=1)
                except Exception:
                    pass

    def _clear_settings_search(self) -> None:
        self.search_var.set("")
        self._search_matches = []
        self._search_match_idx = -1
        self._clear_search_highlights()
        lbl = getattr(self, "lbl_search_status", None)
        if lbl is not None:
            try:
                lbl.configure(text="Ctrl+F 검색 · Ctrl+S 저장 · Esc 닫기", bg="#F8FAFC", fg="#64748B")
            except Exception:
                pass

    def _run_settings_search(self, *, jump: bool = True) -> None:
        old_deb = self._search_debounce_id
        self._search_debounce_id = None
        if old_deb is not None:
            try:
                self.after_cancel(old_deb)
            except Exception:
                pass
        if self._destroyed:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        q = self.search_var.get().strip().casefold()
        self._clear_search_highlights()
        lbl = getattr(self, "lbl_search_status", None)
        if not q:
            self._search_matches = []
            self._search_match_idx = -1
            if lbl is not None:
                lbl.configure(text="Ctrl+F 검색 · Ctrl+S 저장 · Esc 닫기", bg="#F8FAFC", fg="#64748B")
            return

        tokens = [tok for tok in q.split() if tok]
        matches: list[dict[str, Any]] = []
        for sec in self._searchable_sections:
            hay = sec.get("haystack")
            if not hay:
                hay = self._collect_card_search_text(sec["card"], sec["title"], sec["keywords"])
                sec["haystack"] = hay
            if all(tok in hay for tok in tokens):
                matches.append(sec)

        self._search_matches = matches
        if not matches:
            self._search_match_idx = -1
            if lbl is not None:
                lbl.configure(text="일치하는 설정 항목이 없습니다", bg="#FEF2F2", fg="#B91C1C")
            return

        for m in matches:
            try:
                m["card"].configure(highlightbackground="#F9A8B8", highlightthickness=2)
            except Exception:
                pass
        self._search_match_idx = 0
        if jump:
            self._focus_search_match(0)

    def _focus_search_match(self, idx: int) -> None:
        if not self._search_matches:
            return
        idx = idx % len(self._search_matches)
        self._search_match_idx = idx
        sec = self._search_matches[idx]
        for i, m in enumerate(self._search_matches):
            try:
                m["card"].configure(
                    highlightbackground="#FB7390" if i == idx else "#F9A8B8",
                    highlightthickness=2,
                )
            except Exception:
                pass
        tab_idx = int(sec["tab_idx"])
        self._suppress_tab_scroll_top = True
        try:
            self.notebook.select(tab_idx)
        except Exception:
            pass
        finally:
            self._suppress_tab_scroll_top = False

        if 0 <= tab_idx < len(self._tab_scrollers):
            sf = self._tab_scrollers[tab_idx]
            sf.refresh_scrollregion(defer=False)
            sf.scroll_to_widget(sec["card"], pad=12)

        lbl = getattr(self, "lbl_search_status", None)
        if lbl is not None:
            short_title = sec["title"]
            if len(short_title) > 26:
                short_title = short_title[:25] + "…"
            lbl.configure(
                text=f"✓ {idx + 1}/{len(self._search_matches)} · {short_title}",
                bg="#FFF1F4",
                fg="#BE123C",
            )

    def _next_search_match(self, _event: Any = None) -> str:
        if not self._search_matches:
            self._run_settings_search(jump=True)
        else:
            self._focus_search_match(self._search_match_idx + 1)
        return "break"

    def _build_ui(self) -> None:
        # MomoTalk coral-pink header banner (pack right buttons FIRST so they never clip)
        banner = tk.Frame(self, bg="#FB7390", padx=14, pady=9)
        banner.pack(side="top", fill="x")

        b_right = tk.Frame(banner, bg="#FB7390")
        b_right.pack(side="right", padx=(8, 0))
        tk.Button(
            b_right,
            text="📂 데이터 폴더",
            command=self._open_user_data_dir,
            bg="#374254",
            fg="#FFFFFF",
            activebackground="#2A3446",
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=4,
            cursor="hand2",
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            b_right,
            text="🧩 selectors.json",
            command=self._edit_selectors_json,
            bg="#FFFFFF",
            fg="#D94B6A",
            activebackground="#FFEAF0",
            activeforeground="#D94B6A",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=4,
            cursor="hand2",
        ).pack(side="left")

        b_left = tk.Frame(banner, bg="#FB7390")
        b_left.pack(side="left", fill="x", expand=True)
        tk.Label(
            b_left,
            text="💬 MomoTalk Live 환경 설정  ·  프롬프트 해석 & 1~3단계 통합 설정",
            fg="#FFFFFF",
            bg="#FB7390",
            font=("Malgun Gothic", 11, "bold"),
            anchor="w",
        ).pack(anchor="w", fill="x")
        lbl_sec_path = tk.Label(
            b_left,
            text=f"🔒 개인 API 키 및 로그인 세션은 로컬 격리 폴더 ({get_user_data_dir()}) 에만 저장됩니다.",
            fg="#FFF1F4",
            bg="#FB7390",
            font=("Malgun Gothic", 8, "bold"),
            anchor="w",
            justify="left",
        )
        lbl_sec_path.pack(anchor="w", fill="x", pady=(2, 0))
        self._wrap(lbl_sec_path, pad=12, min_wrap=240, container=b_left)

        # Quick settings search / jump bar
        search_bar = tk.Frame(self, bg="#F8FAFC", padx=14, pady=6)
        search_bar.pack(side="top", fill="x")
        tk.Frame(self, bg="#E2E8F0", height=1).pack(side="top", fill="x")
        tk.Label(
            search_bar,
            text="🔍 설정 빠른 찾기:",
            bg="#F8FAFC",
            fg="#334155",
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="left", padx=(0, 6))
        self.ent_settings_search = tk.Entry(
            search_bar,
            textvariable=self.search_var,
            font=("Malgun Gothic", 9),
            bg="#FFFFFF",
            fg="#0F172A",
            relief="solid",
            bd=1,
            width=26,
        )
        self.ent_settings_search.pack(side="left", fill="x", expand=True, ipady=2, padx=(0, 4))
        self.ent_settings_search.bind("<Return>", self._next_search_match)
        try:
            self._search_trace_token = self.search_var.trace_add("write", self._on_search_query_changed)
        except Exception:
            self._search_trace_token = None
        tk.Button(
            search_bar,
            text="다음",
            command=self._next_search_match,
            bg="#FFE4EC",
            fg="#BE123C",
            activebackground="#FECDD3",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            search_bar,
            text="×",
            command=self._clear_settings_search,
            bg="#E2E8F0",
            fg="#475569",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=6,
            pady=2,
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))
        self.lbl_search_status = tk.Label(
            search_bar,
            text="Ctrl+F 검색 · Ctrl+S 저장 · Esc 닫기",
            bg="#F8FAFC",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            padx=6,
            pady=2,
        )
        self.lbl_search_status.pack(side="right")

        # Pack pinned footer FIRST with side="bottom" OUTSIDE the scroll area so buttons are ALWAYS reachable
        ftr = make_pinned_footer(self, bg="#FFFFFF", border_color="#DCE3EE", padx=14, pady=(8, 10), separator=True)
        self._footer_frame = ftr
        if self.on_open_logs is not None:
            tk.Button(
                ftr,
                text="📋 상태 로그",
                command=self._handle_open_logs,
                bg="#E2E8F0",
                fg="#334155",
                relief="flat",
                font=("Malgun Gothic", 9, "bold"),
                padx=10,
                pady=6,
                cursor="hand2",
            ).pack(side="left", padx=(0, 6))
        self.btn_reset_defaults = tk.Button(
            ftr,
            text="🔄 기본값 복원",
            command=self._reset_to_defaults_clicked,
            bg="#FEF3C7",
            fg="#92400E",
            activebackground="#FDE68A",
            activeforeground="#78350F",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.btn_reset_defaults.pack(side="left")
        self.btn_reset_browser = tk.Button(
            ftr,
            text="🌐 브라우저 초기화",
            command=self._reset_browser_profile_clicked,
            bg="#E0F2FE",
            fg="#0369A1",
            activebackground="#BAE6FD",
            activeforeground="#075985",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.btn_reset_browser.pack(side="left", padx=(6, 0))
        self.btn_save_close = tk.Button(
            ftr,
            text="💾 설정 저장 및 적용",
            command=self._save_and_close,
            bg="#FB7390",
            fg="#FFFFFF",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Malgun Gothic", 10, "bold"),
            padx=16,
            pady=6,
            cursor="hand2",
        )
        self.btn_save_close.pack(side="right", padx=(8, 0))
        self.btn_close = tk.Button(
            ftr,
            text="닫기",
            command=self.destroy,
            bg="#E2E8F0",
            fg="#1E293B",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=12,
            pady=6,
            cursor="hand2",
        )
        self.btn_close.pack(side="right")

        body = tk.Frame(self, bg="#F3F5F8")
        body.pack(side="top", fill="both", expand=True, padx=10, pady=(8, 6))

        # Style notebook tabs softly in MomoTalk palette
        style = ttk.Style(self)
        try:
            style.configure("Momo.TNotebook", background="#F3F5F8", borderwidth=0)
            style.configure("Momo.TNotebook.Tab", padding=(9, 6), font=("Malgun Gothic", 9, "bold"))
        except Exception:
            pass

        self.notebook = ttk.Notebook(body, style="Momo.TNotebook")
        self.notebook.pack(side="top", fill="both", expand=True)

        def _add_scrollable_tab(tab_idx: int, label_text: str) -> ScrollableFrame:
            sf = ScrollableFrame(
                self.notebook,
                bg="#F3F5F8",
                padx=8,
                pady=8,
                bottom_pad=28,
                auto_scroll_on_focus=True,
            )
            self.notebook.add(sf, text=label_text)
            self._tab_scrollers.append(sf)
            self._tab_scroller_by_widget[str(sf)] = sf
            return sf

        tab_prompt = _add_scrollable_tab(0, " 🎭 프롬프트·언어 ")
        tab_step1 = _add_scrollable_tab(1, " 🔑 1단계: 음성·인증 ")
        tab_step2 = _add_scrollable_tab(2, " 🎤 2단계: 보이스·디렉팅 ")
        tab_step3 = _add_scrollable_tab(3, " 🎧 3단계: 통화·오디오·자막 ")

        self._current_build_tab_idx = 0
        self._build_tab_prompt(tab_prompt.inner)
        self._current_build_tab_idx = 1
        self._build_tab_step1(tab_step1.inner)
        self._current_build_tab_idx = 2
        self._build_tab_step2(tab_step2.inner)
        self._current_build_tab_idx = 3
        self._build_tab_step3(tab_step3.inner)
        if self._sv_available:
            tab_sv = _add_scrollable_tab(4, " 🖥️ 화면 공유·공부 ")
            self._current_build_tab_idx = 4
            self._build_tab_screen_vision(tab_sv.inner)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed, add="+")

    def _card(self, parent: tk.Misc, title: str, *, keywords: str = "") -> tk.LabelFrame:
        card = tk.LabelFrame(
            parent,
            text=f" {title} ",
            font=("Malgun Gothic", 9, "bold"),
            bg="#FFFFFF",
            fg="#2A3446",
            padx=12,
            pady=9,
            bd=1,
            relief="groove",
            highlightbackground="#E2E8F0",
            highlightthickness=1,
        )
        card.pack(fill="x", pady=(0, 8))
        self._searchable_sections.append(
            {
                "tab_idx": self._current_build_tab_idx,
                "card": card,
                "title": title,
                "keywords": keywords,
                "haystack": "",
            }
        )
        return card

    # ------------------------------------------------------------------ Tab 0
    def _build_tab_prompt(self, parent: tk.Frame) -> None:
        c_interp = self._card(
            parent,
            "0. 프롬프트 해석 버전 선택 (공식 원작 정사 vs 2차창작 4종)",
            keywords="프롬프트 원작 2차창작 페르소나 선톡 RP",
        )

        r_char = tk.Frame(c_interp, bg="#FFFFFF")
        r_char.pack(fill="x", pady=(0, 6))
        tk.Label(
            r_char,
            text="현재 선택 학생:",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        self.lbl_tab0_char_title = tk.Label(
            r_char,
            text=f"  {self._current_character_title()}",
            bg="#FFF1F4",
            fg="#D94B6A",
            font=("Malgun Gothic", 9, "bold"),
            padx=8,
            pady=3,
        )
        self.lbl_tab0_char_title.pack(side="left", padx=(6, 0))

        r_var = tk.Frame(c_interp, bg="#FFFFFF")
        r_var.pack(fill="x", pady=3)
        tk.Label(
            r_var,
            text="프롬프트 해석 버전:",
            width=18,
            anchor="w",
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        self.cmb_persona_variant = ttk.Combobox(
            r_var,
            textvariable=self.persona_variant_var,
            values=list(PERSONA_VARIANT_LABELS.values()),
            state="readonly",
            width=40,
        )
        self.cmb_persona_variant.pack(side="left", fill="x", expand=True)

        chk_pro = tk.Checkbutton(
            c_interp,
            text="🎭 선톡 유도 지시문(선생님 침묵 시 먼저 말걸기) 프롬프트에 자동 포함",
            variable=self.proactive_prompt_var,
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9),
            justify="left",
            anchor="w",
        )
        chk_pro.pack(anchor="w", fill="x", pady=(6, 4))

        r_btns = tk.Frame(c_interp, bg="#FFFFFF")
        r_btns.pack(fill="x", pady=(4, 2))
        btn_state = "normal" if self.app is not None else "disabled"
        tk.Button(
            r_btns,
            text="① 통화 프롬프트 복사 (선택 버전)",
            command=lambda: self._run_app_method("copy_jp_prompt", sync_variant=True),
            bg="#374254",
            fg="#FFFFFF",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=9,
            pady=5,
            cursor="hand2",
            state=btn_state,
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            r_btns,
            text="② 공식 심층 RP 복사",
            command=lambda: self._run_app_method("copy_rp_chat_prompt"),
            bg="#EFF6FF",
            fg="#1D4ED8",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=9,
            pady=5,
            cursor="hand2",
            state=btn_state,
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            r_btns,
            text="③ 공식 vs 2차창작 5종 상세보기",
            command=lambda: self._run_app_method("open_persona_inspector", release_grab=True),
            bg="#FFF1F4",
            fg="#D94B6A",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=9,
            pady=5,
            cursor="hand2",
            state=btn_state,
        ).pack(side="left")

        c_lang = self._card(
            parent,
            "🌐 학생 답변 언어 선택 (모모톡 상단 일본어/한국어 버튼과 연동)",
            keywords="답변 언어 일본어 한국어 더빙",
        )

        def _on_dlg_reply_lang_changed() -> None:
            self._on_reply_language_var_written()

        def _on_dlg_coach_changed() -> None:
            if self.jp_beginner_coach_var.get() and self.reply_language_var.get() != "ja":
                self.reply_language_var.set("ja")

        tk.Radiobutton(
            c_lang,
            text="🇯🇵 일본어 답변 (일본어 원음 · 기본)",
            variable=self.reply_language_var,
            value="ja",
            command=_on_dlg_reply_lang_changed,
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        ).pack(anchor="w", fill="x")
        tk.Radiobutton(
            c_lang,
            text="🇰🇷 한국어 답변 (한국어 대화 · 한국어 더빙 보이스)",
            variable=self.reply_language_var,
            value="ko",
            command=_on_dlg_reply_lang_changed,
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        ).pack(anchor="w", fill="x", pady=(2, 0))

        c_coach = self._card(
            parent,
            "🌸 서툰 일본어 눈높이 배려 모드 (페르소나 설정과 별개 ON/OFF)",
            keywords="일본어 초보 눈높이 배려 코치",
        )
        chk_coach = tk.Checkbutton(
            c_coach,
            text="🌸 일본어가 서툰 한국인 상대에게 눈높이를 맞춰 자연스럽게 대화하기 (과도한 선생·교정 말투 없음)",
            variable=self.jp_beginner_coach_var,
            command=_on_dlg_coach_changed,
            bg="#FFFFFF",
            fg="#BE185D",
            font=("Malgun Gothic", 9, "bold"),
            justify="left",
            anchor="w",
        )
        chk_coach.pack(anchor="w", fill="x")
        self._wrap(chk_coach, pad=44, min_wrap=260, container=c_coach)
        lbl_coach_desc = tk.Label(
            c_coach,
            text=(
                "• 선택한 캐릭터의 고유 페르소나·말투(반말/존댓말)·공식/2차창작 버전은 100% 그대로 유지한 채, "
                "실제 일본인이 서툰 한국인과 대화할 때처럼 쉬운 일상 어휘와 편한 문장으로 맞춰 주며 문맥으로 자연스럽게 알아듣습니다."
            ),
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            justify="left",
            anchor="w",
        )
        lbl_coach_desc.pack(anchor="w", fill="x", padx=(22, 0), pady=(3, 0))
        self._wrap(lbl_coach_desc, pad=54, min_wrap=240, container=c_coach)

        c_per = self._card(
            parent,
            "캐릭터 페르소나 사용 방식 (블루 아카이브 196인 DB vs 직접 작성 커스텀)",
            keywords="커스텀 페르소나 지시문 직접 작성 196인 DB",
        )

        rb_db = tk.Radiobutton(
            c_per,
            text="🎓 모모톡에서 선택한 블루 아카이브 196인 캐릭터 DB + 일본어 감정 디렉팅 사용 (권장)",
            variable=self.use_custom_persona_var,
            value=False,
            bg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            justify="left",
            anchor="w",
        )
        rb_db.pack(anchor="w", fill="x")
        self._wrap(rb_db, pad=44, min_wrap=260, container=c_per)
        rb_custom = tk.Radiobutton(
            c_per,
            text="✍️ 아래에 직접 입력한 커스텀 캐릭터 페르소나 지시문 사용 (자유 캐릭터/한국어 대화용)",
            variable=self.use_custom_persona_var,
            value=True,
            bg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            fg="#7C2D12",
            justify="left",
            anchor="w",
        )
        rb_custom.pack(anchor="w", fill="x", pady=(2, 6))
        self._wrap(rb_custom, pad=44, min_wrap=260, container=c_per)

        self.txt_persona = scrolledtext.ScrolledText(c_per, height=7, font=("Malgun Gothic", 9), wrap="word")
        self.txt_persona.pack(fill="x", expand=False)
        self.txt_persona.insert("1.0", str(self.cfg.get("custom_persona_prompt", "")))

    # ------------------------------------------------------------------ Tab 1
    def _build_tab_step1(self, parent: tk.Frame) -> None:
        if self.first_run:
            guide = tk.Frame(
                parent,
                bg="#FFF1F4",
                highlightbackground="#FB7390",
                highlightthickness=1,
                padx=12,
                pady=9,
            )
            guide.pack(fill="x", pady=(0, 8))
            tk.Label(
                guide,
                text="🚀 처음 시작하기 — 딱 2단계면 바로 통화할 수 있어요!",
                bg="#FFF1F4",
                fg="#BE123C",
                font=("Malgun Gothic", 10, "bold"),
                anchor="w",
            ).pack(fill="x")
            lbl_guide_body = tk.Label(
                guide,
                text=(
                    "① 아래 [🔑 키 발급]으로 Fish Audio API 키를 입력하거나, API 키가 없다면 [🔐 웹 크레딧 로그인 (공식 MCP OAuth)]으로 연결하세요.\n"
                    "② [💾 저장] 버튼(또는 하단 [💾 설정 저장 및 적용]) 클릭 → 학생 선택 후 [통화 시작]!\n"
                    "💡 사전 등록된 공개 보이스가 이미 연결되어 있어 별도의 음성 추출 없이 바로 통화할 수 있습니다."
                ),
                bg="#FFF1F4",
                fg="#881337",
                font=("Malgun Gothic", 8),
                justify="left",
                anchor="w",
            )
            lbl_guide_body.pack(fill="x", pady=(3, 0))
            self._wrap(lbl_guide_body, pad=32, min_wrap=240, container=guide)

        c_steam = self._card(
            parent,
            "1-A. Fish Audio 연결 (API 키 또는 공식 MCP 웹 크레딧) & 스팀 음성 추출 (선택)",
            keywords="Fish Audio API 키 스팀 음성 추출 wav 보이스 탐색",
        )

        # Keep Fish API Key entry and "🔑 키 발급" button as direct children of r_fk (grid layout)
        r_fk = tk.Frame(c_steam, bg="#FFFFFF")
        r_fk.pack(fill="x", pady=(0, 6))
        r_fk.columnconfigure(1, weight=1)
        tk.Label(
            r_fk, text="Fish Audio API Key:", width=18, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.ent_fish_key = tk.Entry(r_fk, textvariable=self.fish_key_var, show="*", width=24, font=("Consolas", 9))
        self.ent_fish_key.grid(row=0, column=1, sticky="ew", padx=(0, 5), pady=(0, 4))
        self.ent_fish_key.bind("<Return>", lambda _e: self._save_and_close())
        tk.Button(r_fk, text="👁", command=self._toggle_fish_eye, bg="#E2E8F0", width=3, cursor="hand2").grid(
            row=0, column=2, padx=(0, 4), pady=(0, 4)
        )
        tk.Button(
            r_fk, text="📋 붙여넣기", command=lambda: self._paste_into(self.fish_key_var), bg="#DCFCE7", cursor="hand2"
        ).grid(row=0, column=3, padx=(0, 4), pady=(0, 4))
        tk.Button(
            r_fk,
            text="💾 저장",
            command=self._save_and_close,
            bg="#FB7390",
            fg="#FFFFFF",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=9,
            pady=2,
            cursor="hand2",
        ).grid(row=0, column=4, pady=(0, 4))

        tk.Button(
            r_fk,
            text="🔑 키 발급",
            command=lambda: self._open_external_url("https://fish.audio/app/api-keys/"),
            bg="#DBEAFE",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=2,
            cursor="hand2",
        ).grid(row=1, column=1, sticky="w", padx=(0, 6))
        tk.Button(
            r_fk,
            text="🔊 보이스 탐색",
            command=lambda: self._open_external_url("https://fish.audio/discovery/"),
            bg="#F1F5F9",
            font=("Malgun Gothic", 8),
            padx=8,
            pady=2,
            cursor="hand2",
        ).grid(row=1, column=2, columnspan=3, sticky="w")

        r_ext = tk.Frame(c_steam, bg="#FFFFFF")
        r_ext.pack(fill="x", pady=(4, 2))
        tk.Button(
            r_ext,
            text="📂 스팀 음성 추출·레퍼런스 열기",
            command=lambda: self._run_app_method("open_reference_folder", release_grab=True),
            bg="#374254",
            fg="#FFFFFF",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=10,
            pady=4,
            cursor="hand2",
            state="normal" if self.app is not None else "disabled",
        ).pack(side="right", padx=(8, 0))
        lbl_ext = tk.Label(
            r_ext,
            text="(선택) 스팀 블루아카에서 직접 캐릭터 .wav 보이스를 추출할 때만 사용:",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            justify="left",
            anchor="w",
        )
        lbl_ext.pack(side="left", fill="x", expand=True)
        self._wrap(lbl_ext, pad=220, min_wrap=180, container=r_ext)

        if ENABLE_ON_DEMAND_VOICE_UI:
            r_st = tk.Frame(c_steam, bg="#FFFFFF")
            r_st.pack(fill="x", pady=(4, 4))
            tk.Label(r_st, text="스팀 블루아카 폴더:", width=18, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
                side="left"
            )
            self.ent_steam_dir = tk.Entry(r_st, textvariable=self.steam_game_dir_var, width=28, font=("Consolas", 8))
            self.ent_steam_dir.pack(side="left", fill="x", expand=True, padx=(0, 6))
            tk.Button(
                r_st,
                text="📁 폴더 지정",
                command=self._browse_steam_game_dir,
                bg="#F1F5F9",
                fg="#334155",
                font=("Malgun Gothic", 8, "bold"),
            ).pack(side="left", padx=(0, 4))
            tk.Button(
                r_st,
                text="🔄 자동 감지",
                command=self._auto_detect_steam_game_dir,
                bg="#E0F2FE",
                fg="#0369A1",
                font=("Malgun Gothic", 8, "bold"),
            ).pack(side="left")
            self.lbl_steam_dir_status = tk.Label(
                c_steam,
                text="",
                bg="#FFFFFF",
                fg="#64748B",
                font=("Malgun Gothic", 8),
                anchor="w",
                justify="left",
            )
            self.lbl_steam_dir_status.pack(fill="x", pady=(0, 6))
            self._wrap(self.lbl_steam_dir_status, pad=32, min_wrap=220, container=c_steam)
            self._refresh_steam_dir_status_label()

        c_mcp = self._card(
            parent,
            "1-A2. Fish Audio 공식 MCP 웹 크레딧 (무료 8,000 B/월) · 절약 모드 · 로컬 캐시",
            keywords="MCP OAuth 웹 크레딧 무료 절약 캐시",
        )
        mcp_btn_state = "normal" if self.app is not None else "disabled"

        r_mcp_pill = tk.Frame(c_mcp, bg="#FFFFFF")
        r_mcp_pill.pack(fill="x", pady=(0, 5))
        self.lbl_fish_mcp_status = tk.Label(
            r_mcp_pill,
            text="미연결",
            bg="#F1F5F9",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=3,
            anchor="w",
            justify="left",
        )
        self.lbl_fish_mcp_status.pack(side="left", fill="x", expand=True)
        self._wrap(self.lbl_fish_mcp_status, pad=24, min_wrap=220, container=r_mcp_pill)

        r_mcp_st = tk.Frame(c_mcp, bg="#FFFFFF")
        r_mcp_st.pack(fill="x", pady=(0, 6))
        self.btn_fish_mcp_login = tk.Button(
            r_mcp_st,
            text="웹 크레딧 로그인 (공식 MCP OAuth)",
            command=self._fish_mcp_login_async,
            bg="#DBEAFE",
            fg="#1D4ED8",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=4,
            cursor="hand2",
            state=mcp_btn_state,
        )
        self.btn_fish_mcp_login.pack(side="left", padx=(0, 6))
        self.btn_fish_mcp_refresh = tk.Button(
            r_mcp_st,
            text="잔액 새로고침",
            command=self._fish_mcp_refresh_balance_async,
            bg="#ECFEFF",
            fg="#0E7490",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=4,
            cursor="hand2",
            state="disabled",
        )
        self.btn_fish_mcp_refresh.pack(side="left", padx=(0, 6))
        self.btn_fish_mcp_logout = tk.Button(
            r_mcp_st,
            text="연결 해제",
            command=self._fish_mcp_logout_clicked,
            bg="#FEE2E2",
            fg="#B91C1C",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=4,
            cursor="hand2",
            state="disabled",
        )
        self.btn_fish_mcp_logout.pack(side="left")

        r_mcp_chk1 = tk.Frame(c_mcp, bg="#FFFFFF")
        r_mcp_chk1.pack(fill="x", pady=(1, 2))
        self.chk_fish_mcp_fallback = tk.Checkbutton(
            r_mcp_chk1,
            text="API 실패 시 웹 크레딧(MCP) 자동 전환",
            variable=self.fish_mcp_fallback_var,
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 8, "bold"),
        )
        self.chk_fish_mcp_fallback.pack(side="left", padx=(0, 14))
        self.chk_fish_mcp_primary = tk.Checkbutton(
            r_mcp_chk1,
            text="웹 크레딧(MCP) 우선 사용",
            variable=self.fish_mcp_primary_var,
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 8),
        )
        self.chk_fish_mcp_primary.pack(side="left")

        r_mcp_chk2 = tk.Frame(c_mcp, bg="#FFFFFF")
        r_mcp_chk2.pack(fill="x", pady=(1, 0))
        self.chk_fish_credit_saver = tk.Checkbutton(
            r_mcp_chk2,
            text="크레딧 절약 모드 (짧은 영어 태그 사용)",
            variable=self.fish_credit_saver_var,
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 8, "bold"),
        )
        self.chk_fish_credit_saver.pack(side="left", padx=(0, 14))
        self.chk_fish_tts_cache = tk.Checkbutton(
            r_mcp_chk2,
            text="로컬 TTS 캐시 사용",
            variable=self.fish_tts_cache_var,
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 8),
        )
        self.chk_fish_tts_cache.pack(side="left", padx=(0, 10))
        self.btn_clear_tts_cache = tk.Button(
            r_mcp_chk2,
            text="캐시 비우기 (0.0 MB)",
            command=self._clear_tts_cache_clicked,
            bg="#F1F5F9",
            fg="#334155",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=2,
            cursor="hand2",
            state=mcp_btn_state,
        )
        self.btn_clear_tts_cache.pack(side="right")

        self._refresh_fish_mcp_ui_state()
        self._refresh_tts_cache_button_label()
        # Automatically sync live web credit balance when opening Settings while already signed in
        if (
            self.app is not None
            and hasattr(self.app, "fish_mcp_refresh_balance_blocking")
            and self._get_fish_mcp_connection_status()[0]
            and not self._has_cached_fish_mcp_balance()
        ):
            self._schedule_after(120, lambda: self._fish_mcp_refresh_balance_async(silent=True))

        c_s2 = self._card(
            parent,
            "1-A+. 🚀 선택 기능 · Google Colab Fish Audio S2-Pro 전용 서버 (기본 OFF · 2_코랩_실행하기.bat)",
            keywords="코랩 Colab S2-Pro GPU 서버 Drive 노트북 터널 반납",
        )

        lbl_s2_intro = tk.Label(
            c_s2,
            text=(
                "Colab(A100/L4)에서 오픈소스 Fish Audio S2-Pro를 단독 실행해 기계음 없는 44.1kHz 스튜디오 원음을 무료·무제한 스트리밍합니다.\n"
                "PC에 추출된 158명 블루 아카이브 원음·대본이 자동 업로드·캐싱되므로 별도 모델 등록 없이 전 캐릭터 즉시 사용 가능합니다."
            ),
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 8),
            justify="left",
            anchor="w",
        )
        lbl_s2_intro.pack(anchor="w", fill="x", pady=(0, 6))
        self._wrap(lbl_s2_intro, pad=36, min_wrap=240, container=c_s2)

        r_s2_en = tk.Frame(c_s2, bg="#FFFFFF")
        r_s2_en.pack(fill="x", pady=(0, 5))
        self.chk_colab_s2pro = tk.Checkbutton(
            r_s2_en,
            text="Colab S2-Pro 전용 서버 우선 사용 (기본 OFF · 기본값은 Fish Audio s2.1-pro-free · 서버 꺼지면 자동 폴백)",
            variable=self.colab_s2pro_enabled_var,
            bg="#FFFFFF",
            fg="#0369A1",
            font=("Malgun Gothic", 8, "bold"),
            justify="left",
            anchor="w",
        )
        self.chk_colab_s2pro.pack(side="left", fill="x", expand=True)
        self._wrap(self.chk_colab_s2pro, pad=44, min_wrap=240, container=c_s2)

        r_s2_url = tk.Frame(c_s2, bg="#FFFFFF")
        r_s2_url.pack(fill="x", pady=(0, 4))
        tk.Label(
            r_s2_url, text="서버 주소(URL):", bg="#FFFFFF", fg="#0F172A", font=("Malgun Gothic", 8, "bold"), width=13, anchor="w"
        ).pack(side="left")
        self.ent_colab_s2pro_url = tk.Entry(
            r_s2_url,
            textvariable=self.colab_s2pro_url_var,
            font=("Consolas", 9),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="solid",
            bd=1,
        )
        self.ent_colab_s2pro_url.pack(side="left", fill="x", expand=True, ipady=2)

        r_s2_key = tk.Frame(c_s2, bg="#FFFFFF")
        r_s2_key.pack(fill="x", pady=(0, 6))
        tk.Label(
            r_s2_key, text="인증 토큰(Key):", bg="#FFFFFF", fg="#0F172A", font=("Malgun Gothic", 8, "bold"), width=13, anchor="w"
        ).pack(side="left")
        self.ent_colab_s2pro_key = tk.Entry(
            r_s2_key,
            textvariable=self.colab_s2pro_key_var,
            show="•",
            font=("Consolas", 9),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="solid",
            bd=1,
        )
        self.ent_colab_s2pro_key.pack(side="left", fill="x", expand=True, ipady=2)
        self._colab_key_shown = False

        def _toggle_colab_key() -> None:
            self._colab_key_shown = not self._colab_key_shown
            self.ent_colab_s2pro_key.configure(show="" if self._colab_key_shown else "•")

        tk.Button(
            r_s2_key,
            text="👁",
            command=_toggle_colab_key,
            bg="#F1F5F9",
            fg="#334155",
            relief="flat",
            font=("Segoe UI Emoji", 8),
            padx=6,
            pady=1,
            cursor="hand2",
        ).pack(side="left", padx=(4, 0))

        r_s2_drive = tk.Frame(c_s2, bg="#FFFFFF")
        r_s2_drive.pack(fill="x", pady=(0, 6))
        tk.Label(
            r_s2_drive, text="Drive 폴더 ID:", bg="#FFFFFF", fg="#475569", font=("Malgun Gothic", 8), width=13, anchor="w"
        ).pack(side="left")
        self.ent_colab_s2pro_folder_id = tk.Entry(
            r_s2_drive,
            textvariable=self.colab_s2pro_drive_folder_var,
            font=("Consolas", 8),
            bg="#F8FAFC",
            fg="#334155",
            relief="solid",
            bd=1,
            width=18,
        )
        self.ent_colab_s2pro_folder_id.pack(side="left", fill="x", expand=True, ipady=1, padx=(0, 8))
        tk.Label(
            r_s2_drive, text="노트북 ID:", bg="#FFFFFF", fg="#475569", font=("Malgun Gothic", 8), anchor="w"
        ).pack(side="left", padx=(0, 4))
        self.ent_colab_s2pro_notebook_id = tk.Entry(
            r_s2_drive,
            textvariable=self.colab_s2pro_notebook_id_var,
            font=("Consolas", 8),
            bg="#F8FAFC",
            fg="#334155",
            relief="solid",
            bd=1,
            width=18,
        )
        self.ent_colab_s2pro_notebook_id.pack(side="left", fill="x", expand=True, ipady=1)

        lbl_s2_note = tk.Label(
            c_s2,
            text="※ 선택 입력: 개인 Google Drive 폴더 ID / Colab 노트북 ID를 저장해 두면 2_코랩_실행하기.bat 에서 원클릭 자동 연결됩니다 (공개 배포 기본값: 비어 있음).",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 7),
            justify="left",
            anchor="w",
        )
        lbl_s2_note.pack(anchor="w", fill="x", pady=(0, 6))
        self._wrap(lbl_s2_note, pad=36, min_wrap=240, container=c_s2)

        def _open_colab_nb_safe() -> None:
            try:
                raw_nb = self.colab_s2pro_notebook_id_var.get().strip()
                clean_nb = extract_google_drive_id(raw_nb, kind="notebook")
                if raw_nb and not clean_nb:
                    self.colab_s2pro_status_var.set(
                        "⚠️ Colab 노트북 ID 형식이 올바르지 않습니다. ID 또는 https://colab.research.google.com/drive/... 주소를 입력해 주세요."
                    )
                    return
                if clean_nb and clean_nb != raw_nb:
                    self.colab_s2pro_notebook_id_var.set(clean_nb)
                safe_nb = validate_colab_notebook_url(build_colab_notebook_url(clean_nb))
                webbrowser.open(safe_nb, new=2)
            except Exception as exc:
                self.colab_s2pro_status_var.set(
                    f"⚠️ Colab 노트북 주소 오류: {redact_secrets(str(exc), self._current_secrets())}"
                )

        r_s2_btn = tk.Frame(c_s2, bg="#FFFFFF")
        r_s2_btn.pack(fill="x", pady=(0, 4))
        tk.Button(
            r_s2_btn,
            text="☁️ Colab 서버 노트북 열기",
            command=_open_colab_nb_safe,
            bg="#E0F2FE",
            fg="#0369A1",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=4,
            cursor="hand2",
        ).pack(side="left", padx=(0, 6))
        self.btn_colab_s2pro_paste = tk.Button(
            r_s2_btn,
            text="📋 연결 정보 붙여넣기",
            command=self._colab_s2pro_paste_bundle,
            bg="#FEF3C7",
            fg="#92400E",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=4,
            cursor="hand2",
        )
        self.btn_colab_s2pro_paste.pack(side="left", padx=(0, 6))
        self.btn_colab_s2pro_auto = tk.Button(
            r_s2_btn,
            text="🔄 Drive에서 주소 자동 가져오기",
            command=self._colab_s2pro_auto_discover_async,
            bg="#DCFCE7",
            fg="#166534",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=4,
            cursor="hand2",
        )
        self.btn_colab_s2pro_auto.pack(side="left", padx=(0, 6))

        r_s2_btn2 = tk.Frame(c_s2, bg="#FFFFFF")
        r_s2_btn2.pack(fill="x", pady=(0, 4))
        self.btn_colab_s2pro_test = tk.Button(
            r_s2_btn2,
            text="🧪 연결 테스트",
            command=self._colab_s2pro_test_health_async,
            bg="#F1F5F9",
            fg="#0F172A",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=4,
            cursor="hand2",
        )
        self.btn_colab_s2pro_test.pack(side="left", padx=(0, 6))
        self.btn_colab_s2pro_shutdown = tk.Button(
            r_s2_btn2,
            text="🛑 코랩 GPU 반납",
            command=self._colab_s2pro_shutdown_gpu_async,
            bg="#FFE4E6",
            fg="#BE123C",
            activebackground="#FECDD3",
            activeforeground="#9F1239",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=10,
            pady=4,
            cursor="hand2",
        )
        self.btn_colab_s2pro_shutdown.pack(side="left")

        lbl_s2_status = tk.Label(
            c_s2,
            textvariable=self.colab_s2pro_status_var,
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 8, "bold"),
            justify="left",
            anchor="w",
        )
        lbl_s2_status.pack(anchor="w", fill="x", pady=(2, 0))
        self._wrap(lbl_s2_status, pad=36, min_wrap=240, container=c_s2)

        c_gem = self._card(
            parent,
            "1-B. AI 대화 엔진 선택 (제미니 · 클로드 · 챗지피티) & 로그인 인증",
            keywords="AI 제미니 Gemini 클로드 Claude 챗지피티 ChatGPT OpenAI 브라우저 로그인 API 키",
        )

        r_prov = tk.Frame(c_gem, bg="#FFFFFF")
        r_prov.pack(fill="x", pady=(0, 6))
        tk.Label(
            r_prov,
            text="사용할 AI:",
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=(0, 8))
        for p_code, p_fg in (("gemini", "#0F766E"), ("claude", "#C2410C"), ("chatgpt", "#1D4ED8")):
            tk.Radiobutton(
                r_prov,
                text=AI_PROVIDER_LABELS[p_code],
                variable=self.ai_provider_var,
                value=p_code,
                command=self._on_ai_provider_ui_changed,
                bg="#FFFFFF",
                fg=p_fg,
                font=("Malgun Gothic", 9, "bold"),
            ).pack(side="left", padx=(0, 12))

        self.rb_backend_browser = tk.Radiobutton(
            c_gem,
            text="🌐 전용 브라우저 연동 — gemini.google.com 웹 로그인 (기본 · 권장 · 무료 계정 안정)",
            variable=self.gemini_backend_var,
            value="browser",
            command=self._on_backend_mode_changed,
            bg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            fg="#0F766E",
            justify="left",
            anchor="w",
        )
        self.rb_backend_browser.pack(anchor="w", fill="x")
        self._wrap(self.rb_backend_browser, pad=44, min_wrap=240, container=c_gem)
        self.rb_backend_api = tk.Radiobutton(
            c_gem,
            text="⚡ 공식 Gemini API 연동 — Google AI Studio 키 (고급 사용자용 · 한도/과금 주의)",
            variable=self.gemini_backend_var,
            value="api",
            command=self._on_backend_mode_changed,
            bg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            fg="#B45309",
            justify="left",
            anchor="w",
        )
        self.rb_backend_api.pack(anchor="w", fill="x", pady=(2, 6))
        self._wrap(self.rb_backend_api, pad=44, min_wrap=240, container=c_gem)

        self.lbl_browser_default_hint = tk.Label(
            c_gem,
            text=(
                "✅ 기본 권장 모드: 별도의 AI API 키가 필요하지 않습니다!\n"
                "• 아래 [🌐 제미니 창 보기/숨기기] 또는 통화 시작 시 열리는 전용 웹 창에서 "
                "한 번만 로그인하면 한도 초과(429)·과금 걱정 없이 안정적으로 대화할 수 있습니다."
            ),
            bg="#ECFDF5",
            fg="#065F46",
            highlightbackground="#A7F3D0",
            highlightthickness=1,
            font=("Malgun Gothic", 8, "bold"),
            justify="left",
            anchor="w",
            padx=10,
            pady=6,
        )
        self._wrap(self.lbl_browser_default_hint, pad=54, min_wrap=240, container=c_gem)

        self.lbl_api_warning_banner = tk.Label(
            c_gem,
            text=(
                "⚠️ 공식 API 모드 주의: 일반/무료 계정은 기본값인 웹 로그인 모드가 가장 안정적입니다.\n"
                "• 무료 Google AI Studio 키는 분당/일일 한도(RPM·RPD)로 지연·중단될 수 있으며, "
                "결제(Billing) 연결 Gemini 및 Claude/OpenAI API 키는 사용량에 따라 요금이 청구됩니다. "
                "발급 키는 외부 공유 금지(로컬 암호화 저장)."
            ),
            bg="#FEF3C7",
            fg="#92400E",
            highlightbackground="#F59E0B",
            highlightthickness=1,
            font=("Malgun Gothic", 8, "bold"),
            justify="left",
            anchor="w",
            padx=10,
            pady=6,
        )
        self._wrap(self.lbl_api_warning_banner, pad=54, min_wrap=240, container=c_gem)

        self._api_key_rows_host = tk.Frame(c_gem, bg="#FFFFFF")

        # Gemini API Key row (always instantiated with show="*")
        self._row_gem_key = tk.Frame(self._api_key_rows_host, bg="#FFFFFF")
        tk.Label(self._row_gem_key, text="Gemini API Key (선택):", width=19, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9)).pack(
            side="left"
        )
        self.ent_gem_key = tk.Entry(self._row_gem_key, textvariable=self.gemini_key_var, show="*", width=20, font=("Consolas", 9))
        self.ent_gem_key.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.ent_gem_key.bind("<Return>", lambda _e: self._save_and_close())
        tk.Button(self._row_gem_key, text="👁", command=self._toggle_gem_eye, bg="#E2E8F0", width=3, cursor="hand2").pack(side="left", padx=(0, 4))
        tk.Button(self._row_gem_key, text="📋", command=lambda: self._paste_into(self.gemini_key_var), bg="#DCFCE7", width=3, cursor="hand2").pack(
            side="left", padx=(0, 4)
        )
        self.btn_fetch_api_models = tk.Button(
            self._row_gem_key,
            text="🔄 API 모델 조회",
            command=lambda: self._fetch_api_models_async("gemini"),
            bg="#E0F2FE",
            fg="#0369A1",
            cursor="hand2",
        )
        self.btn_fetch_api_models.pack(side="left", padx=(0, 4))
        self.btn_gem_key_portal = tk.Button(
            self._row_gem_key,
            text="🔑 키 발급",
            command=lambda: self._open_api_key_portal_with_warning("gemini", "https://aistudio.google.com/apikey"),
            bg="#DBEAFE",
            cursor="hand2",
        )
        self.btn_gem_key_portal.pack(side="left")

        # Claude API Key row (always instantiated with show="*")
        self._row_claude_key = tk.Frame(self._api_key_rows_host, bg="#FFFFFF")
        tk.Label(self._row_claude_key, text="Claude API Key (선택):", width=19, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9)).pack(
            side="left"
        )
        self.ent_claude_key = tk.Entry(self._row_claude_key, textvariable=self.claude_key_var, show="*", width=20, font=("Consolas", 9))
        self.ent_claude_key.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.ent_claude_key.bind("<Return>", lambda _e: self._save_and_close())
        tk.Button(self._row_claude_key, text="👁", command=self._toggle_claude_eye, bg="#E2E8F0", width=3, cursor="hand2").pack(side="left", padx=(0, 4))
        tk.Button(self._row_claude_key, text="📋", command=lambda: self._paste_into(self.claude_key_var), bg="#DCFCE7", width=3, cursor="hand2").pack(
            side="left", padx=(0, 4)
        )
        self.btn_fetch_claude_models = tk.Button(
            self._row_claude_key,
            text="🔄 API 모델 조회",
            command=lambda: self._fetch_api_models_async("claude"),
            bg="#FFEDD5",
            fg="#9A3412",
            cursor="hand2",
        )
        self.btn_fetch_claude_models.pack(side="left", padx=(0, 4))
        self.btn_claude_key_portal = tk.Button(
            self._row_claude_key,
            text="🔑 키 발급",
            command=lambda: self._open_api_key_portal_with_warning("claude", "https://platform.claude.com/settings/keys"),
            bg="#FFEDD5",
            cursor="hand2",
        )
        self.btn_claude_key_portal.pack(side="left")

        # OpenAI / ChatGPT API Key row (always instantiated with show="*")
        self._row_openai_key = tk.Frame(self._api_key_rows_host, bg="#FFFFFF")
        tk.Label(self._row_openai_key, text="OpenAI API Key (선택):", width=19, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9)).pack(
            side="left"
        )
        self.ent_openai_key = tk.Entry(self._row_openai_key, textvariable=self.openai_key_var, show="*", width=20, font=("Consolas", 9))
        self.ent_openai_key.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.ent_openai_key.bind("<Return>", lambda _e: self._save_and_close())
        tk.Button(self._row_openai_key, text="👁", command=self._toggle_openai_eye, bg="#E2E8F0", width=3, cursor="hand2").pack(side="left", padx=(0, 4))
        tk.Button(self._row_openai_key, text="📋", command=lambda: self._paste_into(self.openai_key_var), bg="#DCFCE7", width=3, cursor="hand2").pack(
            side="left", padx=(0, 4)
        )
        self.btn_fetch_openai_models = tk.Button(
            self._row_openai_key,
            text="🔄 API 모델 조회",
            command=lambda: self._fetch_api_models_async("chatgpt"),
            bg="#E0F2FE",
            fg="#1E40AF",
            cursor="hand2",
        )
        self.btn_fetch_openai_models.pack(side="left", padx=(0, 4))
        self.btn_openai_key_portal = tk.Button(
            self._row_openai_key,
            text="🔑 키 발급",
            command=lambda: self._open_api_key_portal_with_warning("chatgpt", "https://platform.openai.com/api-keys"),
            bg="#DBEAFE",
            cursor="hand2",
        )
        self.btn_openai_key_portal.pack(side="left")

        self._row_br_controls = tk.Frame(c_gem, bg="#FFFFFF")
        r_br = self._row_br_controls
        btn_state = "normal" if self.app is not None else "disabled"
        self.lbl_br_login = tk.Label(
            r_br,
            text="전용 브라우저 로그인 관리:",
            bg="#FFFFFF",
            fg="#475569",
            font=("Malgun Gothic", 9, "bold"),
        )
        self.lbl_br_login.pack(side="left")
        self.btn_br_toggle = tk.Button(
            r_br,
            text="🌐 제미니 창 보기/숨기기",
            command=self._open_active_provider_browser,
            bg="#E0F2FE",
            fg="#0369A1",
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            state=btn_state,
        )
        self.btn_br_toggle.pack(side="left", padx=(10, 6))
        self.btn_br_login_confirm = tk.Button(
            r_br,
            text="✅ 로그인 완료 확인",
            command=self._confirm_active_provider_login,
            bg="#DCFCE7",
            fg="#166534",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=4,
            cursor="hand2",
            state=btn_state,
        )
        self.btn_br_login_confirm.pack(side="left")
        self._apply_provider_step1_ui(self._active_provider_ui)

    def _rebuild_alt_voice_controls(self) -> None:
        """Dynamically rebuild Tab 2 dual-voice controls (showing KR top / JP bottom) for the active character."""
        self._clear_alt_voice_traces()
        self._alt_voice_status_labels.clear()
        container = getattr(self, "_alt_voice_container", None)
        if container is not None:
            for child in list(container.winfo_children()):
                try:
                    child.destroy()
                except Exception:
                    pass

        cid = self._current_character_id()
        rlang = self._get_reply_lang()
        raw_kr = self.cfg.get("saved_voices_kr")
        raw_jp = self.cfg.get("saved_voices")
        saved_kr = raw_kr if isinstance(raw_kr, dict) else {}
        saved_jp = raw_jp if isinstance(raw_jp, dict) else {}

        cur_ch = getattr(self.app, "current_char", None) if self.app is not None else None
        if (not isinstance(cur_ch, dict) or str(cur_ch.get("id") or "") != cid) and self.app is not None:
            chars_map = getattr(self.app, "characters", None)
            if isinstance(chars_map, dict) and isinstance(chars_map.get(cid), dict):
                cur_ch = chars_map[cid]

        new_specs: list[tuple[str, str, str]] = []
        new_vars: dict[str, tk.StringVar] = {}
        new_kr_vars: dict[str, tk.StringVar] = {}
        new_jp_vars: dict[str, tk.StringVar] = {}
        if isinstance(cur_ch, dict) and isinstance(cur_ch.get("alt_voice_personas"), dict):
            for ak, spec in cur_ch["alt_voice_personas"].items():
                if not isinstance(spec, dict):
                    continue
                slot_id = str(spec.get("voice_slot_id") or f"{cid}_{ak.capitalize()}").strip()
                lbl_kr = str(spec.get("label_kr") or ak).strip()
                new_specs.append((str(ak), slot_id, lbl_kr))
                init_kr = str(saved_kr.get(slot_id) or "")
                init_jp = str(saved_jp.get(slot_id) or "")
                v_kr = tk.StringVar(master=self, value=init_kr)
                v_jp = tk.StringVar(master=self, value=init_jp)
                v_act = tk.StringVar(master=self, value=(init_kr if rlang == "ko" else init_jp))
                new_kr_vars[slot_id] = v_kr
                new_jp_vars[slot_id] = v_jp
                new_vars[slot_id] = v_act

                def _make_alt_lang_cb(sid: str, is_kr_slot: bool) -> Callable[..., None]:
                    def _cb(*_a: object) -> None:
                        self._refresh_voice_ref_status_ui()
                        if self._syncing_alt_vars:
                            return
                        if (self._voice_editor_lang == "ko") == is_kr_slot:
                            src = self._alt_voice_kr_vars.get(sid) if is_kr_slot else self._alt_voice_jp_vars.get(sid)
                            dst = self._alt_voice_vars.get(sid)
                            if src is not None and dst is not None:
                                self._syncing_alt_vars = True
                                try:
                                    dst.set(src.get())
                                finally:
                                    self._syncing_alt_vars = False

                    return _cb

                def _make_alt_act_cb(sid: str) -> Callable[..., None]:
                    def _cb(*_a: object) -> None:
                        if self._syncing_alt_vars:
                            return
                        src = self._alt_voice_vars.get(sid)
                        dst = (
                            self._alt_voice_kr_vars.get(sid)
                            if self._voice_editor_lang == "ko"
                            else self._alt_voice_jp_vars.get(sid)
                        )
                        if src is not None and dst is not None:
                            self._syncing_alt_vars = True
                            try:
                                dst.set(src.get())
                            finally:
                                self._syncing_alt_vars = False
                        self._refresh_voice_ref_status_ui()

                    return _cb

                for var_obj, cb_fn in (
                    (v_kr, _make_alt_lang_cb(slot_id, True)),
                    (v_jp, _make_alt_lang_cb(slot_id, False)),
                    (v_act, _make_alt_act_cb(slot_id)),
                ):
                    try:
                        tok = var_obj.trace_add("write", cb_fn)
                        self._alt_voice_trace_tokens.append((var_obj, tok))
                    except Exception:
                        pass

        self._alt_voice_specs = new_specs
        self._alt_voice_vars = new_vars
        self._alt_voice_kr_vars = new_kr_vars
        self._alt_voice_jp_vars = new_jp_vars

        if container is None:
            return
        if not self._alt_voice_specs:
            try:
                container.pack_forget()
            except Exception:
                pass
            self._refresh_tab_scroll(2, defer=True)
            return

        container.pack(fill="x", pady=(2, 0))
        btn_state = "normal" if self.app is not None else "disabled"
        for ak, slot_id, lbl_kr in self._alt_voice_specs:
            alt_kr_var = self._alt_voice_kr_vars[slot_id]
            alt_jp_var = self._alt_voice_jp_vars[slot_id]
            alt_act_var = self._alt_voice_vars[slot_id]
            r_alt = tk.Frame(container, bg="#FFF7FA", padx=8, pady=5, highlightbackground="#FBCFE8", highlightthickness=1)
            r_alt.pack(fill="x", pady=(6, 0))

            r_alt_hdr = tk.Frame(r_alt, bg="#FFF7FA")
            r_alt_hdr.pack(fill="x", pady=(0, 4))
            tk.Label(
                r_alt_hdr,
                text=f"🎭 보조 보이스 [{lbl_kr}] ({slot_id})",
                bg="#FFF7FA",
                fg="#BE185D",
                font=("Malgun Gothic", 8, "bold"),
                anchor="w",
            ).pack(side="left")
            tk.Button(
                r_alt_hdr,
                text="🔊 테스트" if ENABLE_ON_DEMAND_VOICE_UI else "🔊 보조 보이스 테스트",
                command=lambda k=ak, s=slot_id, v=alt_act_var: self._test_alt_voice(k, s, v),
                bg="#FCE7F3",
                fg="#9D174D",
                font=("Malgun Gothic", 8, "bold"),
                relief="flat",
                padx=7 if ENABLE_ON_DEMAND_VOICE_UI else 8,
                pady=1,
                state=btn_state,
            ).pack(side="right")
            if ENABLE_ON_DEMAND_VOICE_UI:
                tk.Button(
                    r_alt_hdr,
                    text="✨ 자동 생성",
                    command=lambda k=ak, s=slot_id: self._auto_create_alt_voice(k, s),
                    bg="#FFF1F4",
                    fg="#D94B6A",
                    font=("Malgun Gothic", 8, "bold"),
                    relief="flat",
                    padx=7,
                    pady=1,
                    state=btn_state,
                ).pack(side="right", padx=(0, 4))

            for lang_key, lang_label, lang_var in (
                ("kr", "🇰🇷 KR:", alt_kr_var),
                ("jp", "🇯🇵 JP:", alt_jp_var),
            ):
                sub_row = tk.Frame(r_alt, bg="#FFF7FA")
                sub_row.pack(fill="x", pady=(0, 2 if lang_key == "kr" else 0))
                tk.Label(
                    sub_row,
                    text=lang_label,
                    width=8,
                    anchor="w",
                    bg="#FFF7FA",
                    fg="#9D174D",
                    font=("Malgun Gothic", 8, "bold"),
                ).pack(side="left")
                b_txt, b_bg, b_fg = self._voice_ref_badge_style(lang_var.get())
                lbl_badge = tk.Label(
                    sub_row,
                    text=b_txt,
                    width=9,
                    anchor="center",
                    bg=b_bg,
                    fg=b_fg,
                    font=("Malgun Gothic", 8, "bold"),
                    padx=3,
                    pady=1,
                )
                lbl_badge.pack(side="left", padx=(0, 6))
                self._alt_voice_status_labels[(slot_id, lang_key)] = lbl_badge
                tk.Entry(sub_row, textvariable=lang_var, width=30, font=("Consolas", 8)).pack(
                    side="left", fill="x", expand=True, padx=(0, 5)
                )
                tk.Button(
                    sub_row,
                    text="📋 붙여넣기",
                    command=lambda v=lang_var: self._paste_into(v, clean_ref=True),
                    bg="#DCFCE7",
                    fg="#166534",
                    font=("Malgun Gothic", 8),
                    relief="flat",
                    padx=6,
                    pady=1,
                ).pack(side="left")

            lbl_alt_note = tk.Label(
                container,
                text=(
                    f"  └ 대화 중 `[VOICE:{ak}]` 연출 시 자동으로 이 보이스({slot_id})로 전환됩니다. "
                    "(일괄 자동화 실행 시 자동 추출·업로드 · 미연결 시에도 로컬 Zero-Shot 자동 작동)"
                ),
                bg="#FFFFFF",
                fg="#9D174D",
                font=("Malgun Gothic", 8),
                anchor="w",
                justify="left",
            )
            lbl_alt_note.pack(anchor="w", fill="x", pady=(2, 0))
            self._wrap(lbl_alt_note, pad=36, min_wrap=220, container=container)
        self._refresh_tab_scroll(2, defer=True)

    # ------------------------------------------------------------------ Tab 2
    def _build_tab_step2(self, parent: tk.Frame) -> None:
        c_voice = self._card(
            parent,
            f"2-A. 캐릭터별 Voice Reference ID 설정 — [{self._current_character_title()}]",
            keywords="보이스 Voice Reference ID KR JP 보조 보이스 검색 테스트 일괄 자동화",
        )
        self._card_voice_step2 = c_voice

        # Top row: KR Voice Reference ID
        self._row_ref_kr = tk.Frame(
            c_voice,
            bg="#F8FAFC",
            padx=8,
            pady=5,
            highlightbackground="#E2E8F0",
            highlightthickness=1,
        )
        self._row_ref_kr.pack(fill="x", pady=(2, 4))
        self._lbl_ref_kr_title = tk.Label(
            self._row_ref_kr,
            text="🇰🇷 KR Voice Reference ID:",
            width=21,
            anchor="w",
            bg="#F8FAFC",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
        )
        self._lbl_ref_kr_title.pack(side="left")
        self.lbl_ref_kr_status = tk.Label(
            self._row_ref_kr,
            text="⬜ 미등록",
            width=9,
            anchor="center",
            bg="#E2E8F0",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            padx=4,
            pady=2,
        )
        self.lbl_ref_kr_status.pack(side="left", padx=(0, 6))
        self.ent_ref_kr = tk.Entry(self._row_ref_kr, textvariable=self.ref_id_kr_var, width=22, font=("Consolas", 9))
        self.ent_ref_kr.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(
            self._row_ref_kr,
            text="📋 URL 붙여넣기",
            command=lambda: self._paste_into(self.ref_id_kr_var, clean_ref=True),
            bg="#DCFCE7",
            fg="#166534",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="left")

        # Bottom row: JP Voice Reference ID
        self._row_ref_jp = tk.Frame(
            c_voice,
            bg="#F8FAFC",
            padx=8,
            pady=5,
            highlightbackground="#E2E8F0",
            highlightthickness=1,
        )
        self._row_ref_jp.pack(fill="x", pady=(0, 3))
        self._lbl_ref_jp_title = tk.Label(
            self._row_ref_jp,
            text="🇯🇵 JP Voice Reference ID:",
            width=21,
            anchor="w",
            bg="#F8FAFC",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
        )
        self._lbl_ref_jp_title.pack(side="left")
        self.lbl_ref_jp_status = tk.Label(
            self._row_ref_jp,
            text="⬜ 미등록",
            width=9,
            anchor="center",
            bg="#E2E8F0",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            padx=4,
            pady=2,
        )
        self.lbl_ref_jp_status.pack(side="left", padx=(0, 6))
        self.ent_ref_jp = tk.Entry(self._row_ref_jp, textvariable=self.ref_id_jp_var, width=22, font=("Consolas", 9))
        self.ent_ref_jp.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(
            self._row_ref_jp,
            text="📋 URL 붙여넣기",
            command=lambda: self._paste_into(self.ref_id_jp_var, clean_ref=True),
            bg="#DCFCE7",
            fg="#166534",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="left")

        # 2x2 action button grid so all 4 buttons fit cleanly at any dialog width
        r_act = tk.Frame(c_voice, bg="#FFFFFF")
        r_act.pack(fill="x", pady=(6, 2))
        r_act.columnconfigure(0, weight=1, uniform="voice_act")
        r_act.columnconfigure(1, weight=1, uniform="voice_act")
        btn_state = "normal" if self.app is not None else "disabled"
        tk.Button(
            r_act,
            text="🔍 인기 보이스 검색",
            command=lambda: self._run_app_method("open_voice_search_dialog", release_grab=True),
            bg="#EFF6FF",
            fg="#1D4ED8",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=5,
            cursor="hand2",
            state=btn_state,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        tk.Button(
            r_act,
            text="✨ 스팀 원본 업로드 · 전용 모델 자동 생성",
            command=self._auto_create_current_voice,
            bg="#FFF1F4",
            fg="#D94B6A",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=5,
            cursor="hand2",
            state=btn_state,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))
        tk.Button(
            r_act,
            text="🔊 시그니처 대사 테스트",
            command=self._test_current_voice,
            bg="#ECFDF5",
            fg="#047857",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=5,
            cursor="hand2",
            state=btn_state,
        ).grid(row=1, column=0, sticky="ew", padx=(0, 4))
        tk.Button(
            r_act,
            text="🚀 전 캐릭터 일괄 자동화 (JP/KR)",
            command=self._open_batch_voice_dialog,
            bg="#FB7390",
            fg="#FFFFFF",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=11,
            pady=5,
            cursor="hand2",
            state=btn_state,
        ).grid(row=1, column=1, sticky="ew", padx=(4, 0))

        self.lbl_voice_ref_hint = tk.Label(
            c_voice,
            text="",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            anchor="w",
            justify="left",
        )
        self.lbl_voice_ref_hint.pack(anchor="w", fill="x", pady=(5, 4 if ENABLE_ON_DEMAND_VOICE_UI else 0))
        self._wrap(self.lbl_voice_ref_hint, pad=36, min_wrap=240, container=c_voice)

        if ENABLE_ON_DEMAND_VOICE_UI:
            r_auto_opt = tk.Frame(c_voice, bg="#F8FAFC", padx=8, pady=5, highlightbackground="#E2E8F0", highlightthickness=1)
            r_auto_opt.pack(fill="x", pady=(2, 2))
            tk.Label(
                r_auto_opt,
                text="원클릭 생성 공개범위:",
                bg="#F8FAFC",
                fg="#334155",
                font=("Malgun Gothic", 8, "bold"),
            ).pack(side="left", padx=(0, 5))
            ttk.Combobox(
                r_auto_opt,
                textvariable=self.auto_voice_vis_var,
                values=list(AUTO_VOICE_VISIBILITY_LABELS.values()),
                width=26,
                state="readonly",
            ).pack(side="left", fill="x", expand=True, padx=(0, 10))
            tk.Label(
                r_auto_opt,
                text="음질:",
                bg="#F8FAFC",
                fg="#334155",
                font=("Malgun Gothic", 8, "bold"),
            ).pack(side="left", padx=(0, 4))
            ttk.Combobox(
                r_auto_opt,
                textvariable=self.auto_voice_prof_var,
                values=list(AUTO_VOICE_PROFILE_LABELS.values()),
                width=20,
                state="readonly",
            ).pack(side="left")

        # Dual-voice alt persona controls (e.g. Kisaki <-> Kiki real-time sentence voice switching)
        self._alt_voice_container = tk.Frame(c_voice, bg="#FFFFFF")
        self._rebuild_alt_voice_controls()
        self._refresh_voice_ref_status_ui()

        c_dir = self._card(
            parent,
            "2-B. Fish Audio 합성 엔진 · 응답 속도 · 감정/억양 디렉팅",
            keywords="합성 엔진 스트리밍 속도 음량 볼륨 감정 억양 디렉팅 텐션",
        )

        r_eng1 = tk.Frame(c_dir, bg="#FFFFFF")
        r_eng1.pack(fill="x", pady=3)
        tk.Label(r_eng1, text="음성 합성 엔진(model):", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        ttk.Combobox(
            r_eng1,
            textvariable=self.fish_model_hdr_var,
            values=FISH_MODEL_HEADERS,
            width=22,
            state="readonly",
        ).pack(side="left")
        tk.Label(
            r_eng1,
            text=" (기본값: s2.1-pro-free)",
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="left", padx=(6, 0))

        r_eng2 = tk.Frame(c_dir, bg="#FFFFFF")
        r_eng2.pack(fill="x", pady=3)
        tk.Label(r_eng2, text="스트리밍 모드:", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        ttk.Combobox(
            r_eng2,
            textvariable=self.speed_mode_var,
            values=list(SPEED_MODE_LABELS.values()),
            width=36,
            state="readonly",
        ).pack(side="left", fill="x", expand=True)

        r_vol = tk.Frame(c_dir, bg="#FFFFFF")
        r_vol.pack(fill="x", pady=(6, 3))
        tk.Label(r_vol, text="캐릭터 출력 음량:", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        self.lbl_vol_pct = tk.Label(
            r_vol,
            text=f"{int(round(self.vol_var.get() * 100))}%",
            width=6,
            bg="#FFFFFF",
            fg="#D94B6A",
            font=("Malgun Gothic", 9, "bold"),
        )
        self.lbl_vol_pct.pack(side="left")
        tk.Scale(
            r_vol,
            from_=0.10,
            to=2.00,
            resolution=0.05,
            orient="horizontal",
            variable=self.vol_var,
            showvalue=False,
            bg="#FFFFFF",
            highlightthickness=0,
            length=260,
            command=lambda v: self.lbl_vol_pct.configure(text=f"{int(round(float(v) * 100))}%"),
        ).pack(side="left", fill="x", expand=True, padx=(4, 8))

        r_boost = tk.Frame(c_dir, bg="#FFFFFF")
        r_boost.pack(fill="x", pady=3)
        tk.Label(r_boost, text="연기 텐션/억양 강도:", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        ttk.Combobox(
            r_boost,
            textvariable=self.boost_preset_var,
            values=list(BOOST_PRESET_LABELS.values()),
            width=32,
            state="readonly",
        ).pack(side="left", fill="x", expand=True)

        chk_emo = tk.Checkbutton(
            c_dir,
            text="🎭 문맥 · 대사 분석 기반 자동 감정/억양 디렉팅 (v4 엔진 + 정밀패치 스파이크/속삭임 적용)",
            variable=self.auto_emotion_var,
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 9, "bold"),
            justify="left",
            anchor="w",
        )
        chk_emo.pack(anchor="w", fill="x", pady=(6, 0))
        self._wrap(chk_emo, pad=44, min_wrap=240, container=c_dir)

    # ------------------------------------------------------------------ Tab 3
    def _build_tab_step3(self, parent: tk.Frame) -> None:
        c_mode = self._card(
            parent,
            "3-A. AI 보이스 통화 방식 (Mode 1 vs Mode 2) & 오디오 장치",
            keywords="통화 방식 Mode 1 Mode 2 마이크 출력 장치 이어폰 스피커 VB-Cable STT VAD Whisper 음성인식",
        )

        # Stack Mode 1 / Mode 2 vertically so long Korean labels never clip on the right
        rb_m1 = tk.Radiobutton(
            c_mode,
            text="🎤 Mode 1: PC 마이크 직통 AI 컴패니언 통화 (VB-Cable 불필요 · 권장)",
            variable=self.call_bridge_mode_var,
            value="mode1",
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 9, "bold"),
            justify="left",
            anchor="w",
        )
        rb_m1.pack(anchor="w", fill="x")
        self._wrap(rb_m1, pad=44, min_wrap=240, container=c_mode)
        rb_m2 = tk.Radiobutton(
            c_mode,
            text="🎧 Mode 2: VB-Cable 라이브 오디오 캡처 브릿지 (크롬 Live 등)",
            variable=self.call_bridge_mode_var,
            value="mode2",
            bg="#FFFFFF",
            fg="#1D4ED8",
            font=("Malgun Gothic", 9, "bold"),
            justify="left",
            anchor="w",
        )
        rb_m2.pack(anchor="w", fill="x", pady=(2, 6))
        self._wrap(rb_m2, pad=44, min_wrap=240, container=c_mode)

        out_vals = list(getattr(self.app, "primary_outputs", []) or [])
        mic_vals = list(getattr(self.app, "mic_inputs", []) or [])
        in_vals = [lbl for _, lbl, _ in (getattr(self.app, "inputs", []) or [])]
        trig_vals = list(getattr(self.app, "trigger_outputs", []) or [])

        r_dev1 = tk.Frame(c_mode, bg="#FFFFFF")
        r_dev1.pack(fill="x", pady=2)
        tk.Label(r_dev1, text="출력 장치 (이어폰/스피커):", width=22, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        self.cmb_set_out = ttk.Combobox(r_dev1, textvariable=self.primary_out_var, values=out_vals, width=28, state="readonly")
        self.cmb_set_out.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Button(
            r_dev1,
            text="🔄 장치 새로고침",
            command=self._refresh_app_devices,
            bg="#F1F5F9",
            fg="#334155",
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            state="normal" if self.app is not None else "disabled",
        ).pack(side="left")

        if self.app is not None and hasattr(self.app, "lbl_gpu_status") and self.app.lbl_gpu_status is not None:
            try:
                gpu_txt = str(self.app.lbl_gpu_status.cget("text") or "").strip()
            except Exception:
                gpu_txt = ""
            if gpu_txt:
                r_gpu = tk.Frame(c_mode, bg="#FFFFFF")
                r_gpu.pack(fill="x", pady=(1, 3))
                tk.Label(r_gpu, text="", width=22, bg="#FFFFFF").pack(side="left")
                self.lbl_settings_gpu = tk.Label(
                    r_gpu,
                    text=gpu_txt,
                    bg="#0F172A",
                    fg="#86EFAC" if "준비" in gpu_txt or "CUDA" in gpu_txt else "#FDE047",
                    font=("Malgun Gothic", 8, "bold"),
                    padx=8,
                    pady=2,
                )
                self.lbl_settings_gpu.pack(side="left")

        r_dev2 = tk.Frame(c_mode, bg="#FFFFFF")
        r_dev2.pack(fill="x", pady=2)
        tk.Label(r_dev2, text="PC 마이크 (Mode 1):", width=22, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        self.cmb_set_mic = ttk.Combobox(r_dev2, textvariable=self.mic_input_var, values=mic_vals, width=28, state="readonly")
        self.cmb_set_mic.pack(side="left", fill="x", expand=True)

        r_dev3 = tk.Frame(c_mode, bg="#FFFFFF")
        r_dev3.pack(fill="x", pady=2)
        tk.Label(r_dev3, text="VB-Cable 입력 (Mode 2):", width=22, anchor="w", bg="#FFFFFF").pack(side="left")
        self.cmb_set_vbin = ttk.Combobox(r_dev3, textvariable=self.vb_in_var, values=in_vals, width=28, state="readonly")
        self.cmb_set_vbin.pack(side="left", fill="x", expand=True)

        r_dev4a = tk.Frame(c_mode, bg="#FFFFFF")
        r_dev4a.pack(fill="x", pady=2)
        tk.Label(r_dev4a, text="STT 음성인식 언어:", width=22, anchor="w", bg="#FFFFFF").pack(side="left")
        ttk.Combobox(
            r_dev4a,
            textvariable=self.stt_lang_var,
            values=list(STT_LANG_LABELS.values()),
            width=28,
            state="readonly",
        ).pack(side="left", fill="x", expand=True)

        r_dev4b = tk.Frame(c_mode, bg="#FFFFFF")
        r_dev4b.pack(fill="x", pady=2)
        tk.Label(r_dev4b, text="VAD 마이크 민감도:", width=22, anchor="w", bg="#FFFFFF").pack(side="left")
        ttk.Combobox(
            r_dev4b,
            textvariable=self.vad_preset_var,
            values=list(VAD_PRESETS.keys()),
            width=28,
            state="readonly",
        ).pack(side="left", fill="x", expand=True)

        r_dev5 = tk.Frame(c_mode, bg="#FFFFFF")
        r_dev5.pack(fill="x", pady=2)
        tk.Label(r_dev5, text="Whisper 음성인식 모델:", width=22, anchor="w", bg="#FFFFFF").pack(side="left")
        self.cmb_whisper_model = ttk.Combobox(
            r_dev5,
            textvariable=self.whisper_model_var,
            values=list(WHISPER_MODEL_LABELS.values()),
            width=30,
            state="readonly",
        )
        self.cmb_whisper_model.pack(side="left", fill="x", expand=True)

        c_mod = self._card(
            parent,
            "3-B. AI 대화 모델 & 실시간 대사 자막 엔진",
            keywords="대화 모델 브라우저 웹 모델 동기화 추론 공식 API 자막 번역 엔진 트리거",
        )
        api_vals = self._build_api_model_dropdown_values(self._active_provider_ui)

        rm0 = tk.Frame(c_mod, bg="#FFFFFF")
        rm0.pack(fill="x", pady=(0, 6))
        self.btn_sync_web_models = tk.Button(
            rm0,
            text="🔄 웹 모델 동기화",
            command=self._sync_web_models_async,
            bg="#ECFEFF",
            fg="#0E7490",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=3,
            cursor="hand2",
        )
        self.btn_sync_web_models.pack(side="right", padx=(8, 0))
        self.lbl_step3_provider = tk.Label(
            rm0,
            text=f"현재 선택된 AI 엔진: {AI_PROVIDER_LABELS[self._active_provider_ui]}  (1단계 탭 또는 메인 상단에서 즉시 변경 가능)",
            bg="#FFF1F4",
            fg="#BE123C",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=3,
            anchor="w",
            justify="left",
        )
        self.lbl_step3_provider.pack(side="left", fill="x", expand=True)
        self._wrap(self.lbl_step3_provider, pad=160, min_wrap=200, container=rm0)

        rm1 = tk.Frame(c_mod, bg="#FFFFFF")
        rm1.pack(fill="x", pady=2)
        tk.Label(rm1, text="브라우저 대화 모델:", width=17, anchor="w", bg="#FFFFFF").pack(side="left")
        self.cmb_browser_model = ttk.Combobox(
            rm1,
            textvariable=self.gemini_browser_model_var,
            values=[lbl for _, lbl in self._all_browser_models],
            width=22,
            state="readonly",
        )
        self.cmb_browser_model.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Label(rm1, text="추론:", bg="#FFFFFF").pack(side="left")
        self.cmb_chat_thinking = ttk.Combobox(
            rm1,
            textvariable=self.gemini_thinking_var,
            values=[lbl for _, lbl in GEMINI_THINKING_LEVELS],
            width=18,
            state="readonly",
        )
        self.cmb_chat_thinking.pack(side="left", padx=(4, 0))

        rm2 = tk.Frame(c_mod, bg="#FFFFFF")
        rm2.pack(fill="x", pady=2)
        tk.Label(rm2, text="공식 API 대화 모델:", width=17, anchor="w", bg="#FFFFFF").pack(side="left")
        self.cmb_api_model = ttk.Combobox(
            rm2,
            textvariable=self.gemini_model_var,
            values=api_vals,
            width=22,
            state="normal",
        )
        self.cmb_api_model.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Label(rm2, text="트리거(M2):", bg="#FFFFFF").pack(side="left")
        self.cmb_set_trig = ttk.Combobox(
            rm2, textvariable=self.proactive_trig_dev_var, values=trig_vals, width=18, state="readonly"
        )
        self.cmb_set_trig.pack(side="left", padx=(4, 0))

        rm3 = tk.Frame(c_mod, bg="#FFFFFF")
        rm3.pack(fill="x", pady=2)
        tk.Label(rm3, text="대사 자막 모델:", width=17, anchor="w", bg="#FFFFFF").pack(side="left")
        self.cmb_sub_engine = ttk.Combobox(
            rm3,
            textvariable=self.subtitle_engine_var,
            values=[lbl for _, lbl in self._all_sub_choices],
            width=22,
            state="readonly",
        )
        self.cmb_sub_engine.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.cmb_sub_engine.bind("<<ComboboxSelected>>", lambda _e: self._sync_sub_thinking_state())
        tk.Label(rm3, text="자막 추론:", bg="#FFFFFF").pack(side="left")
        self.cmb_sub_thinking = ttk.Combobox(
            rm3,
            textvariable=self.subtitle_thinking_var,
            values=[lbl for _, lbl in GEMINI_THINKING_LEVELS],
            width=18,
            state="readonly",
        )
        self.cmb_sub_thinking.pack(side="left", padx=(4, 0))
        self._sync_sub_thinking_state()

        c_tm = self._card(
            parent,
            "3-C. 양방향 무음 선톡 타이머 & AFK 설정",
            keywords="선톡 타이머 무음 대기 시간 AFK 자동 유도",
        )
        rt1 = tk.Frame(c_tm, bg="#FFFFFF")
        rt1.pack(fill="x", pady=2)
        tk.Checkbutton(
            rt1,
            text="Mode 1 랜덤 무음 선톡",
            variable=self.companion_proactive_var,
            bg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=(0, 10))
        tk.Label(rt1, text="대기 범위(초):", bg="#FFFFFF").pack(side="left")
        tk.Entry(rt1, textvariable=self.min_idle_var, width=5, justify="center").pack(side="left", padx=(4, 2))
        tk.Label(rt1, text="~", bg="#FFFFFF").pack(side="left")
        tk.Entry(rt1, textvariable=self.max_idle_var, width=5, justify="center").pack(side="left", padx=(2, 10))
        tk.Label(rt1, text="AFK 전환:", bg="#FFFFFF").pack(side="left")
        tk.Entry(rt1, textvariable=self.afk_count_var, width=4, justify="center").pack(side="left", padx=(4, 2))
        tk.Label(rt1, text="회 무응답", bg="#FFFFFF").pack(side="left")

        rt2 = tk.Frame(c_tm, bg="#FFFFFF")
        rt2.pack(fill="x", pady=(4, 0))
        tk.Checkbutton(
            rt2,
            text="Mode 2 자동 선톡 유도",
            variable=self.proactive_enable_var,
            bg="#FFFFFF",
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=(0, 8))
        ttk.Combobox(
            rt2,
            textvariable=self.proactive_timeout_var,
            values=list(PROACTIVE_TIMEOUT_LABELS.values()),
            width=14,
            state="readonly",
        ).pack(side="left", padx=(0, 6))
        ttk.Combobox(
            rt2,
            textvariable=self.proactive_vol_var,
            values=list(PROACTIVE_VOL_LABELS.values()),
            width=14,
            state="readonly",
        ).pack(side="left")

    # ------------------------------------------------------------------ Tab 4 (Local-Only Screen Vision)
    def _init_screen_vision_vars(self) -> None:
        from src.screen_vision.config import load_screen_vision_config

        sv_cfg = load_screen_vision_config()
        self._sv_cfg = deepcopy(sv_cfg)
        self._sv_denylist = list(sv_cfg.get("privacy_denylist") or [])

        mode_val = "econ_study" if str(sv_cfg.get("mode", "active_chat")) == "econ_study" else "active_chat"
        self.sv_mode_var = tk.StringVar(value=mode_val)
        self.sv_auto_react_var = tk.BooleanVar(value=bool(sv_cfg.get("active_chat_auto_react", True)))
        self.sv_threshold_var = tk.StringVar(
            value=str(self._safe_float(sv_cfg.get("active_chat_change_threshold", 12.0), 12.0, 3.0, 45.0))
        )
        self.sv_cooldown_var = tk.StringVar(
            value=str(int(round(self._safe_float(sv_cfg.get("active_chat_cooldown_sec", 14.0), 14.0, 5.0, 120.0))))
        )
        self.sv_stuck_enabled_var = tk.BooleanVar(value=bool(sv_cfg.get("study_stuck_enabled", True)))
        self.sv_stuck_sec_var = tk.StringVar(
            value=str(int(round(self._safe_float(sv_cfg.get("study_stuck_sec", 210.0), 210.0, 45.0, 900.0))))
        )
        self.sv_board_auto_open_var = tk.BooleanVar(value=bool(sv_cfg.get("study_board_auto_open", True)))
        self.sv_dedup_var = tk.BooleanVar(value=bool(sv_cfg.get("dedup_identical_turns", True)))
        self.sv_privacy_auto_pause_var = tk.BooleanVar(value=bool(sv_cfg.get("privacy_auto_pause", True)))
        self.sv_deny_entry_var = tk.StringVar(value="")
        self.sv_hotkeys_enabled_var = tk.BooleanVar(value=bool(sv_cfg.get("hotkeys_enabled", True)))

    def _build_tab_screen_vision(self, parent: tk.Frame) -> None:
        c_preset = self._card(
            parent,
            "4-A. 화면 공유 기본 모드 · 전송 최적화 (Gemini 전용 · 앱 시작 시 항상 OFF)",
            keywords="화면 공유 프리셋 적극적 대화 공부 경제학 중복 첨부",
        )

        tk.Label(
            c_preset,
            text="기본 프리셋 모드:",
            anchor="w",
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w", pady=(0, 2))
        rb_sv_chat = tk.Radiobutton(
            c_preset,
            text="🎮 적극적 대화 (화면 변화에 즉각 리액션 · 1~2문장 티키타카)",
            variable=self.sv_mode_var,
            value="active_chat",
            bg="#FFFFFF",
            fg="#1D4ED8",
            font=("Malgun Gothic", 9, "bold"),
            justify="left",
            anchor="w",
        )
        rb_sv_chat.pack(anchor="w", fill="x", padx=(8, 0))
        self._wrap(rb_sv_chat, pad=52, min_wrap=240, container=c_preset)
        rb_sv_study = tk.Radiobutton(
            c_preset,
            text="📚 공부 모드 (수식·그래프 보드 연동)",
            variable=self.sv_mode_var,
            value="econ_study",
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 9, "bold"),
            justify="left",
            anchor="w",
        )
        rb_sv_study.pack(anchor="w", fill="x", padx=(8, 0), pady=(2, 4))
        self._wrap(rb_sv_study, pad=52, min_wrap=240, container=c_preset)

        chk_dedup = tk.Checkbutton(
            c_preset,
            text="♻️ 직전 턴과 화면이 같으면 중복 첨부 생략 (토큰·응답 속도 절약)",
            variable=self.sv_dedup_var,
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9),
            justify="left",
            anchor="w",
        )
        chk_dedup.pack(anchor="w", fill="x", pady=(2, 0))
        self._wrap(chk_dedup, pad=44, min_wrap=240, container=c_preset)

        c_modes = self._card(
            parent,
            "4-B. 🎮 대화 모드 자동 반응 & 📚 공부 모드 설정",
            keywords="자동 리액션 민감도 쿨다운 막힘 감지 학습 보드",
        )

        r_chat = tk.Frame(c_modes, bg="#FFFFFF")
        r_chat.pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            r_chat,
            text="🎮 대화 모드: 화면 변화 자동 리액션",
            variable=self.sv_auto_react_var,
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=(0, 12))
        tk.Label(r_chat, text="변화 민감도(3~45):", bg="#FFFFFF", fg="#334155", font=("Malgun Gothic", 8)).pack(side="left")
        tk.Entry(r_chat, textvariable=self.sv_threshold_var, width=6, justify="center").pack(side="left", padx=(4, 10))
        tk.Label(r_chat, text="쿨다운(5~120초):", bg="#FFFFFF", fg="#334155", font=("Malgun Gothic", 8)).pack(side="left")
        tk.Entry(r_chat, textvariable=self.sv_cooldown_var, width=6, justify="center").pack(side="left", padx=(4, 0))

        r_study = tk.Frame(c_modes, bg="#FFFFFF")
        r_study.pack(fill="x", pady=(2, 2))
        tk.Checkbutton(
            r_study,
            text="📚 공부 모드: 막힘 감지 체크인",
            variable=self.sv_stuck_enabled_var,
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=(0, 12))
        tk.Label(r_study, text="정지 감지(45~900초):", bg="#FFFFFF", fg="#334155", font=("Malgun Gothic", 8)).pack(side="left")
        tk.Entry(r_study, textvariable=self.sv_stuck_sec_var, width=6, justify="center").pack(side="left", padx=(4, 0))

        tk.Checkbutton(
            c_modes,
            text="📊 학습 보드 자동 열기",
            variable=self.sv_board_auto_open_var,
            bg="#FFFFFF",
            fg="#0369A1",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        ).pack(anchor="w", fill="x", pady=(2, 0))

        c_priv = self._card(
            parent,
            "4-C. 🔒 프라이버시 자동 보호 & 제외 창/앱 목록 (100% RAM 캡처 · 디스크 저장 없음)",
            keywords="프라이버시 보호 제외 앱 키워드 은행 결제 암호 관리자",
        )

        chk_priv = tk.Checkbutton(
            c_priv,
            text="🔒 민감한 창(암호 관리자·은행·결제·로그인 등) 감지 시 화면 공유 자동 일시정지",
            variable=self.sv_privacy_auto_pause_var,
            bg="#FFFFFF",
            fg="#92400E",
            font=("Malgun Gothic", 9, "bold"),
            justify="left",
            anchor="w",
        )
        chk_priv.pack(anchor="w", fill="x")
        self._wrap(chk_priv, pad=44, min_wrap=240, container=c_priv)
        lbl_priv_tip = tk.Label(
            c_priv,
            text="💡 기본 내장 보호: 1Password · Bitwarden · KeePass · Windows 보안 · 공동/금융인증서 · 은행/결제/로그인 창은 목록에 없어도 자동 차단됩니다.",
            bg="#FFFBEB",
            fg="#92400E",
            font=("Malgun Gothic", 8),
            padx=8,
            pady=4,
            anchor="w",
            justify="left",
        )
        lbl_priv_tip.pack(fill="x", pady=(4, 5))
        self._wrap(lbl_priv_tip, pad=48, min_wrap=240, container=c_priv)

        r_deny_in = tk.Frame(c_priv, bg="#FFFFFF")
        r_deny_in.pack(fill="x", pady=(0, 4))
        tk.Label(r_deny_in, text="추가 제외 앱/키워드:", bg="#FFFFFF", fg="#0F172A", font=("Malgun Gothic", 8, "bold")).pack(
            side="left", padx=(0, 6)
        )
        self.ent_sv_deny = tk.Entry(r_deny_in, textvariable=self.sv_deny_entry_var, font=("Malgun Gothic", 9))
        self.ent_sv_deny.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.ent_sv_deny.bind("<Return>", lambda _e: self._sv_add_denylist_item())
        tk.Button(
            r_deny_in,
            text="추가",
            command=self._sv_add_denylist_item,
            bg="#DBEAFE",
            fg="#1D4ED8",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=10,
            pady=2,
            cursor="hand2",
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            r_deny_in,
            text="선택 삭제",
            command=self._sv_remove_denylist_selected,
            bg="#FEE2E2",
            fg="#B91C1C",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=2,
            cursor="hand2",
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            r_deny_in,
            text="초기화",
            command=self._sv_clear_denylist,
            bg="#F1F5F9",
            fg="#475569",
            relief="flat",
            font=("Malgun Gothic", 8, "bold"),
            padx=9,
            pady=2,
            cursor="hand2",
        ).pack(side="left")

        r_deny_list = tk.Frame(c_priv, bg="#FFFFFF")
        r_deny_list.pack(fill="x")
        self.lst_sv_denylist = tk.Listbox(
            r_deny_list,
            height=3,
            selectmode="extended",
            font=("Malgun Gothic", 8),
            bg="#F8FAFC",
            fg="#1E293B",
            relief="solid",
            bd=1,
        )
        sb_deny = ttk.Scrollbar(r_deny_list, orient="vertical", command=self.lst_sv_denylist.yview)
        self.lst_sv_denylist.configure(yscrollcommand=sb_deny.set)
        self.lst_sv_denylist.pack(side="left", fill="x", expand=True)
        sb_deny.pack(side="right", fill="y")
        self._sv_refresh_denylist_widget()

        c_hk = self._card(
            parent,
            "4-D. ⌨️ 전역 단축키 (게임·PDF·브라우저 위에서 즉시 제어)",
            keywords="단축키 Ctrl Shift S V B 미니 자막 바",
        )
        tk.Checkbutton(
            c_hk,
            text="⌨️ 전역 단축키 활성화 (Windows 공식 RegisterHotKey 사용)",
            variable=self.sv_hotkeys_enabled_var,
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        ).pack(anchor="w", fill="x")
        lbl_hk_list = tk.Label(
            c_hk,
            text="• Ctrl+Shift+S : 화면 공유 일시정지 / 전환    • Ctrl+Shift+V : 지금 화면 봐! (즉시 캡처)    • Ctrl+Shift+B : 항상 위 미니 자막 바 토글",
            bg="#F1F5F9",
            fg="#334155",
            font=("Consolas", 8, "bold"),
            padx=8,
            pady=4,
            anchor="w",
            justify="left",
        )
        lbl_hk_list.pack(fill="x", pady=(4, 0))
        self._wrap(lbl_hk_list, pad=48, min_wrap=240, container=c_hk)

    def _sv_refresh_denylist_widget(self) -> None:
        lb = getattr(self, "lst_sv_denylist", None)
        if lb is None:
            return
        try:
            lb.delete(0, tk.END)
            for item in self._sv_denylist:
                lb.insert(tk.END, item)
        except Exception:
            pass

    def _sv_add_denylist_item(self) -> None:
        raw = self.sv_deny_entry_var.get().strip()
        if not raw:
            return
        cleaned = "".join(ch for ch in raw if ord(ch) >= 32 or ch == "\t").strip()[:128]
        if not cleaned:
            return
        if all(existing.casefold() != cleaned.casefold() for existing in self._sv_denylist):
            self._sv_denylist.append(cleaned)
            self._sv_refresh_denylist_widget()
        self.sv_deny_entry_var.set("")

    def _sv_remove_denylist_selected(self) -> None:
        lb = getattr(self, "lst_sv_denylist", None)
        if lb is None:
            return
        sel = sorted((int(i) for i in lb.curselection()), reverse=True)
        if not sel:
            return
        for idx in sel:
            if 0 <= idx < len(self._sv_denylist):
                self._sv_denylist.pop(idx)
        self._sv_refresh_denylist_widget()

    def _sv_clear_denylist(self) -> None:
        self._sv_denylist.clear()
        self._sv_refresh_denylist_widget()

    # ------------------------------------------------------------------ Helpers
    def _push_live_app_vars(self, push_key_ref: bool = False, sync_variant: bool = False) -> None:
        if self.app is None:
            return
        if sync_variant and hasattr(self.app, "persona_variant_var"):
            new_var_label = self.persona_variant_var.get()
            old_var_label = self.app.persona_variant_var.get()
            self.app.persona_variant_var.set(new_var_label)
            if new_var_label != old_var_label:
                if hasattr(self.app, "_refresh_momotalk_header_and_cards"):
                    self.app._refresh_momotalk_header_and_cards()
                if hasattr(self.app, "_on_active_persona_potentially_changed"):
                    self.app._on_active_persona_potentially_changed("페르소나 해석 버전 변경")
        if hasattr(self.app, "proactive_prompt_var"):
            self.app.proactive_prompt_var.set(bool(self.proactive_prompt_var.get()))
        if hasattr(self.app, "reply_language_var"):
            rlang_val = "ko" if self.reply_language_var.get() == "ko" else "ja"
            self.app.reply_language_var.set(rlang_val)
            if hasattr(self.app, "cfg") and isinstance(self.app.cfg, dict):
                self.app.cfg["reply_language"] = rlang_val
            if hasattr(self.app, "_update_reply_language_toggle_style"):
                self.app._update_reply_language_toggle_style()
        if hasattr(self.app, "jp_beginner_coach_var"):
            self.app.jp_beginner_coach_var.set(bool(self.jp_beginner_coach_var.get()))
            if hasattr(self.app, "cfg") and isinstance(self.app.cfg, dict):
                self.app.cfg["jp_beginner_lover_mode"] = bool(self.jp_beginner_coach_var.get())
            if hasattr(self.app, "_update_jp_coach_toggle_style"):
                self.app._update_jp_coach_toggle_style()
        if push_key_ref:
            if hasattr(self.app, "cfg") and isinstance(self.app.cfg, dict):
                self.app.cfg["steam_game_dir"] = self.steam_game_dir_var.get().strip()
                self.app.cfg["auto_voice_visibility"] = AUTO_VOICE_VIS_LABEL_TO_CODE.get(
                    self.auto_voice_vis_var.get().strip(), "private"
                )
                self.app.cfg["auto_voice_quality_profile"] = AUTO_VOICE_PROF_LABEL_TO_CODE.get(
                    self.auto_voice_prof_var.get().strip(), "plus200"
                )
                self.app.cfg["fish_mcp_fallback_enabled"] = bool(self.fish_mcp_fallback_var.get())
                self.app.cfg["fish_mcp_primary_mode"] = bool(self.fish_mcp_primary_var.get())
                self.app.cfg["fish_credit_saver_mode"] = bool(self.fish_credit_saver_var.get())
                self.app.cfg["fish_tts_cache_enabled"] = bool(self.fish_tts_cache_var.get())
            if hasattr(self.app, "api_key_var"):
                self.app.api_key_var.set(self.fish_key_var.get().strip())
            cid = self._current_character_id()
            rlang = self._get_reply_lang()
            clean_kr = _clean_ref_id(self.ref_id_kr_var.get())
            clean_jp = _clean_ref_id(self.ref_id_jp_var.get())
            if hasattr(self.app, "cfg") and isinstance(self.app.cfg, dict) and cid:
                if clean_kr:
                    self.app.cfg.setdefault("saved_voices_kr", {})[cid] = clean_kr
                if clean_jp:
                    self.app.cfg.setdefault("saved_voices", {})[cid] = clean_jp
            if hasattr(self.app, "ref_id_var"):
                self.app.ref_id_var.set(clean_kr if rlang == "ko" else clean_jp)
            if hasattr(self.app, "model_hdr_var"):
                self.app.model_hdr_var.set(self.fish_model_hdr_var.get())
            if hasattr(self.app, "speed_mode_var"):
                self.app.speed_mode_var.set(self.speed_mode_var.get())
                if hasattr(self.app, "cfg") and isinstance(self.app.cfg, dict):
                    self.app.cfg["streaming_speed_mode"] = SPEED_LABEL_TO_CODE.get(
                        self.speed_mode_var.get().strip(), "expressive"
                    )
            if hasattr(self.app, "auto_emotion_var"):
                self.app.auto_emotion_var.set(bool(self.auto_emotion_var.get()))
            if hasattr(self.app, "boost_preset_var"):
                self.app.boost_preset_var.set(self.boost_preset_var.get())
            if hasattr(self.app, "vol_var"):
                vol_val = float(self.vol_var.get())
                self.app.vol_var.set(vol_val)
                if hasattr(self.app, "cfg") and isinstance(self.app.cfg, dict):
                    self.app.cfg["output_volume"] = vol_val
            if hasattr(self.app, "out_choice_var") and self.primary_out_var.get():
                self.app.out_choice_var.set(self.primary_out_var.get())

    def _run_app_method(
        self,
        method_name: str,
        *,
        release_grab: bool = False,
        push_key_ref: bool = False,
        sync_variant: bool = False,
    ) -> None:
        if self.app is None or not hasattr(self.app, method_name):
            return
        self._push_live_app_vars(push_key_ref=push_key_ref, sync_variant=sync_variant)
        if release_grab:
            try:
                self.grab_release()
            except Exception:
                pass
        getattr(self.app, method_name)()

    def _refresh_app_devices(self) -> None:
        if self.app is None or not hasattr(self.app, "refresh_devices"):
            return
        self.app.refresh_devices()
        out_vals = list(getattr(self.app, "primary_outputs", []) or [])
        mic_vals = list(getattr(self.app, "mic_inputs", []) or [])
        in_vals = [lbl for _, lbl, _ in (getattr(self.app, "inputs", []) or [])]
        trig_vals = list(getattr(self.app, "trigger_outputs", []) or [])
        self.cmb_set_out.configure(values=out_vals)
        self.cmb_set_mic.configure(values=mic_vals)
        self.cmb_set_vbin.configure(values=in_vals)
        self.cmb_set_trig.configure(values=trig_vals)
        if hasattr(self.app, "out_choice_var") and self.app.out_choice_var.get():
            self.primary_out_var.set(self.app.out_choice_var.get())
        if hasattr(self.app, "mic_choice_var") and self.app.mic_choice_var.get():
            m_val = self.app.mic_choice_var.get().strip()
            self.mic_input_var.set("" if (m_val.startswith("(") or is_virtual_loopback_mic_label(m_val)) else m_val)
        if hasattr(self.app, "in_choice_var") and self.app.in_choice_var.get():
            self.vb_in_var.set(self.app.in_choice_var.get())
        if hasattr(self.app, "trigger_out_choice_var") and self.app.trigger_out_choice_var.get():
            self.proactive_trig_dev_var.set(self.app.trigger_out_choice_var.get())

    def _apply_provider_step1_ui(self, provider: str) -> None:
        p = normalize_ai_provider(provider)
        short = AI_PROVIDER_SHORT_NAMES[p]
        if p == "claude":
            br_txt = "🌐 전용 브라우저 연동 — claude.ai 웹 로그인 (기본 · 권장 · 무료 계정 안정)"
            api_txt = "⚡ 공식 Claude API 연동 — Anthropic API 키 (고급 사용자용 · 한도/과금 주의)"
        elif p == "chatgpt":
            br_txt = "🌐 전용 브라우저 연동 — chatgpt.com 웹 로그인 (기본 · 권장 · 무료 계정 안정)"
            api_txt = "⚡ 공식 ChatGPT API 연동 — OpenAI API 키 (고급 사용자용 · 한도/과금 주의)"
        else:
            br_txt = "🌐 전용 브라우저 연동 — gemini.google.com 웹 로그인 (기본 · 권장 · 무료 계정 안정)"
            api_txt = "⚡ 공식 Gemini API 연동 — Google AI Studio 키 (고급 사용자용 · 한도/과금 주의)"
        if hasattr(self, "rb_backend_browser"):
            self.rb_backend_browser.configure(text=br_txt)
        if hasattr(self, "rb_backend_api"):
            self.rb_backend_api.configure(text=api_txt)
        if hasattr(self, "btn_br_toggle"):
            self.btn_br_toggle.configure(text=f"🌐 {short} 창 보기/숨기기")
        if hasattr(self, "btn_br_login_confirm"):
            self.btn_br_login_confirm.configure(text=f"✅ {short} 로그인 완료 확인")
        self._sync_backend_mode_ui()

    def _sync_backend_mode_ui(self) -> None:
        mode = (self.gemini_backend_var.get() or "browser").strip().lower()
        if mode not in ("browser", "api"):
            mode = "browser"
        p = normalize_ai_provider(self.ai_provider_var.get())
        short = AI_PROVIDER_SHORT_NAMES[p]

        if hasattr(self, "lbl_browser_default_hint"):
            self.lbl_browser_default_hint.configure(
                text=(
                    "✅ 기본 권장 모드: 별도의 AI API 키가 필요하지 않습니다!\n"
                    f"• 아래 [🌐 {short} 창 보기/숨기기] 또는 통화 시작 시 열리는 전용 웹 창에서 "
                    "한 번만 로그인하면 한도 초과(429)·과금 걱정 없이 안정적으로 대화할 수 있습니다."
                )
            )

        for row_attr, p_key in (
            ("_row_gem_key", "gemini"),
            ("_row_claude_key", "claude"),
            ("_row_openai_key", "chatgpt"),
        ):
            row = getattr(self, row_attr, None)
            if row is not None:
                row.pack_forget()
                if p_key == p:
                    row.pack(fill="x")

        for attr in ("lbl_browser_default_hint", "lbl_api_warning_banner", "_api_key_rows_host", "_row_br_controls"):
            w = getattr(self, attr, None)
            if w is not None:
                w.pack_forget()

        if mode == "api":
            if hasattr(self, "lbl_api_warning_banner"):
                self.lbl_api_warning_banner.pack(fill="x", pady=(2, 6))
            if hasattr(self, "_api_key_rows_host"):
                self._api_key_rows_host.pack(fill="x", pady=(0, 2))
        else:
            if hasattr(self, "lbl_browser_default_hint"):
                self.lbl_browser_default_hint.pack(fill="x", pady=(2, 6))
            if hasattr(self, "_row_br_controls"):
                self._row_br_controls.pack(fill="x", pady=(2, 0))
        self._refresh_tab_scroll(1, defer=True)

    def _on_backend_mode_changed(self) -> None:
        new_mode = (self.gemini_backend_var.get() or "browser").strip().lower()
        if new_mode not in ("browser", "api"):
            new_mode = "browser"
            self.gemini_backend_var.set("browser")
        prev_mode = getattr(self, "_last_backend_ui", "browser")
        if new_mode == "api" and prev_mode != "api":
            provider = normalize_ai_provider(self.ai_provider_var.get())
            warn_msg = _build_api_warning_text(provider) + "\n\n그래도 공식 API 키 모드로 전환하시겠습니까?"
            proceed = messagebox.askyesno(
                "⚠️ 공식 API 모드 · 키 발급 주의 안내",
                warn_msg,
                parent=self,
            )
            if self._destroyed or not self.winfo_exists():
                return
            if not proceed:
                self.gemini_backend_var.set("browser")
                self._last_backend_ui = "browser"
                self._sync_backend_mode_ui()
                return
        self._last_backend_ui = new_mode
        self._sync_backend_mode_ui()

    def _stash_active_provider_tab3_choices(self) -> None:
        prev_p = self._active_provider_ui
        bmodel_by_lbl = {lbl: code for code, lbl in self._all_browser_models}
        cur_b_code = bmodel_by_lbl.get(
            self.gemini_browser_model_var.get().strip(),
            self._provider_browser_model_codes.get(prev_p, get_provider_default_browser_model(prev_p)),
        )
        self._provider_browser_model_codes[prev_p] = cur_b_code
        typed_api = self.gemini_model_var.get().strip()
        if not typed_api:
            self._provider_api_model_selected[prev_p] = get_provider_default_api_model(prev_p)
            return
        cleaned_api = extract_safe_api_model_id(typed_api, provider=prev_p)
        if cleaned_api:
            self._provider_api_model_selected[prev_p] = cleaned_api
        elif (self.gemini_backend_var.get().strip() or "browser") == "api":
            self._provider_api_model_selected[prev_p] = typed_api
        else:
            self._provider_api_model_selected[prev_p] = coerce_safe_api_model_id(
                self._provider_api_model_selected.get(prev_p),
                provider=prev_p,
            )

    def _on_ai_provider_ui_changed(self) -> None:
        new_p = normalize_ai_provider(self.ai_provider_var.get())
        if new_p != self._active_provider_ui:
            self._stash_active_provider_tab3_choices()
            self._active_provider_ui = new_p
        self._apply_provider_step1_ui(new_p)
        self._refresh_provider_model_catalogs(new_p)

        if hasattr(self, "lbl_step3_provider"):
            self.lbl_step3_provider.configure(
                text=f"현재 선택된 AI 엔진: {AI_PROVIDER_LABELS[new_p]}  (1단계 탭 또는 메인 상단에서 즉시 변경 가능)"
            )
        if hasattr(self, "cmb_browser_model"):
            self.cmb_browser_model.configure(values=[lbl for _, lbl in self._all_browser_models])
            b_code = self._provider_browser_model_codes.get(new_p, get_provider_default_browser_model(new_p))
            def_b = get_provider_default_browser_model(new_p)
            self.gemini_browser_model_var.set(
                self._browser_lbl_by_code.get(
                    b_code,
                    self._browser_lbl_by_code.get(def_b, self._all_browser_models[0][1] if self._all_browser_models else ""),
                )
            )
        if hasattr(self, "cmb_api_model"):
            api_vals = self._build_api_model_dropdown_values(new_p)
            self.cmb_api_model.configure(values=api_vals)
            self.gemini_model_var.set(
                self._provider_api_model_selected.get(new_p, get_provider_default_api_model(new_p))
            )
        if hasattr(self, "cmb_sub_engine"):
            self.cmb_sub_engine.configure(values=[lbl for _, lbl in self._all_sub_choices])
            valid_sub_lbls = {lbl for _, lbl in self._all_sub_choices}
            if self.subtitle_engine_var.get().strip() not in valid_sub_lbls:
                self.subtitle_engine_var.set(resolve_subtitle_engine_label("auto_inline", self._all_sub_choices))
            self._sync_sub_thinking_state()

    def _sync_selected_provider_to_app(self) -> None:
        if self.app is None:
            return
        new_p = normalize_ai_provider(self.ai_provider_var.get())
        if hasattr(self.app, "set_ai_provider"):
            try:
                self.app.set_ai_provider(new_p, save=False, restart_call=False)
                return
            except Exception:
                pass
        if hasattr(self.app, "cfg") and isinstance(self.app.cfg, dict):
            self.app.cfg["ai_provider"] = new_p
        mgr = getattr(self.app, "gemini_mgr", None)
        if mgr is not None:
            import threading

            def _bg_switch() -> None:
                try:
                    if hasattr(mgr, "abort_active_prompt"):
                        mgr.abort_active_prompt()
                    mgr.set_provider(new_p)
                except Exception:
                    pass

            threading.Thread(target=_bg_switch, daemon=True, name="settings-switch-provider").start()

    def _open_active_provider_browser(self) -> None:
        if self.app is None or not hasattr(self.app, "toggle_gemini_browser_window"):
            return
        new_p = normalize_ai_provider(self.ai_provider_var.get())
        try:
            self.app.toggle_gemini_browser_window(provider_override=new_p)
        except TypeError:
            self._sync_selected_provider_to_app()
            self._run_app_method("toggle_gemini_browser_window")

    def _confirm_active_provider_login(self) -> None:
        if self.app is None or not hasattr(self.app, "confirm_browser_login_async"):
            return
        new_p = normalize_ai_provider(self.ai_provider_var.get())
        try:
            self.app.confirm_browser_login_async(provider_override=new_p)
        except TypeError:
            self._sync_selected_provider_to_app()
            self._run_app_method("confirm_browser_login_async")

    def _toggle_fish_eye(self) -> None:
        self._show_fish_key = not self._show_fish_key
        self.ent_fish_key.config(show="" if self._show_fish_key else "*")

    def _toggle_gem_eye(self) -> None:
        self._show_gem_key = not self._show_gem_key
        self.ent_gem_key.config(show="" if self._show_gem_key else "*")

    def _toggle_claude_eye(self) -> None:
        self._show_claude_key = not self._show_claude_key
        self.ent_claude_key.config(show="" if self._show_claude_key else "*")

    def _toggle_openai_eye(self) -> None:
        self._show_openai_key = not self._show_openai_key
        self.ent_openai_key.config(show="" if self._show_openai_key else "*")

    def _current_secrets(self) -> tuple[str, ...]:
        import json as _json

        secrets: list[str] = []
        for var in (
            self.fish_key_var,
            self.gemini_key_var,
            self.claude_key_var,
            self.openai_key_var,
            self.colab_s2pro_key_var,
        ):
            try:
                val = var.get().strip()
                if val:
                    secrets.append(val)
            except Exception:
                pass
        for cfg_src in (self.cfg, getattr(self.app, "cfg", None) if self.app is not None else None):
            if not isinstance(cfg_src, dict):
                continue
            raw_oauth = cfg_src.get("fish_mcp_oauth_data")
            if not raw_oauth:
                continue
            try:
                oauth_obj = raw_oauth if isinstance(raw_oauth, dict) else _json.loads(str(raw_oauth))
                if isinstance(oauth_obj, dict):
                    stack: list[Any] = [oauth_obj]
                    while stack:
                        cur = stack.pop()
                        if isinstance(cur, dict):
                            for ok, ov in cur.items():
                                if isinstance(ov, (dict, list)):
                                    stack.append(ov)
                                elif str(ok).lower() in ("access_token", "refresh_token", "client_secret", "id_token"):
                                    sv = str(ov or "").strip()
                                    if len(sv) >= 6 and sv not in secrets:
                                        secrets.append(sv)
                        elif isinstance(cur, list):
                            stack.extend(cur)
            except Exception:
                pass
        return tuple(secrets)

    def _get_fish_mcp_connection_status(self) -> tuple[bool, str]:
        connected = False
        status_txt = "미연결"
        if self.app is not None and hasattr(self.app, "get_fish_mcp_ui_status"):
            try:
                connected, status_txt = self.app.get_fish_mcp_ui_status()
            except Exception:
                connected, status_txt = False, "미연결"
        else:
            raw_oauth = str(self.cfg.get("fish_mcp_oauth_data") or "").strip()
            if raw_oauth and ("access_token" in raw_oauth or "refresh_token" in raw_oauth):
                connected = True
                status_txt = "연결됨"
        return bool(connected), str(status_txt or ("연결됨" if connected else "미연결"))

    def _has_cached_fish_mcp_balance(self) -> bool:
        if self.app is not None and hasattr(self.app, "fish_mcp_has_cached_balance"):
            try:
                return bool(self.app.fish_mcp_has_cached_balance())
            except Exception:
                return False
        backend = getattr(self.app, "fish_mcp_backend", None) if self.app is not None else None
        if backend is not None and hasattr(backend, "get_cached_balance"):
            try:
                bal = backend.get_cached_balance()
                return isinstance(bal, dict) and bal.get("remaining_credits") is not None
            except Exception:
                return False
        return False

    def _refresh_fish_mcp_ui_state(self) -> None:
        if not hasattr(self, "lbl_fish_mcp_status"):
            return
        connected, status_txt = self._get_fish_mcp_connection_status()
        has_app = self.app is not None
        login_busy = bool(getattr(self, "_fish_mcp_login_in_progress", False))
        refresh_busy = bool(getattr(self, "_fish_mcp_refresh_in_progress", False))
        self._fish_mcp_busy = login_busy or refresh_busy

        if login_busy:
            live_txt = str(getattr(self, "_fish_mcp_live_status", "") or "").strip()
            self.lbl_fish_mcp_status.configure(
                text=live_txt or "브라우저에서 Fish Audio 로그인 대기 중...",
                bg="#FEF3C7",
                fg="#92400E",
            )
        elif refresh_busy:
            if connected and self._has_cached_fish_mcp_balance():
                self.lbl_fish_mcp_status.configure(text=status_txt, bg="#ECFDF5", fg="#047857")
            else:
                self.lbl_fish_mcp_status.configure(
                    text="크레딧 잔액 동기화 중...",
                    bg="#E0F2FE",
                    fg="#0369A1",
                )
        elif connected:
            self.lbl_fish_mcp_status.configure(text=status_txt, bg="#ECFDF5", fg="#047857")
        else:
            self.lbl_fish_mcp_status.configure(text=status_txt or "미연결", bg="#F1F5F9", fg="#475569")

        if hasattr(self, "btn_fish_mcp_login"):
            if login_busy:
                cancelling = (
                    getattr(self, "_fish_mcp_cancelled_seq", 0) == getattr(self, "_fish_mcp_login_seq", 0)
                    and getattr(self, "_fish_mcp_login_seq", 0) > 0
                )
                self.btn_fish_mcp_login.configure(
                    state=("disabled" if cancelling else "normal"),
                    text=("취소 중..." if cancelling else "로그인 취소"),
                    bg="#FEE2E2",
                    fg="#B91C1C",
                )
            else:
                self.btn_fish_mcp_login.configure(
                    state=("disabled" if (not has_app or refresh_busy) else "normal"),
                    text=("계정 재로그인" if connected else "웹 크레딧 로그인 (공식 MCP OAuth)"),
                    bg="#DBEAFE",
                    fg="#1D4ED8",
                )
        if hasattr(self, "btn_fish_mcp_refresh"):
            self.btn_fish_mcp_refresh.configure(
                state=("normal" if (has_app and connected and not login_busy and not refresh_busy) else "disabled"),
                text=("조회 중..." if refresh_busy else "잔액 새로고침"),
            )
        if hasattr(self, "btn_fish_mcp_logout"):
            self.btn_fish_mcp_logout.configure(
                state=("normal" if (has_app and connected and not login_busy and not refresh_busy) else "disabled")
            )

    def _refresh_tts_cache_button_label(self) -> None:
        if not hasattr(self, "btn_clear_tts_cache"):
            return
        size_mb = 0.0
        entry_count = 0
        if self.app is not None and hasattr(self.app, "get_tts_cache_stats"):
            try:
                stats = self.app.get_tts_cache_stats()
                if isinstance(stats, dict):
                    total_bytes = int(stats.get("total_bytes") or stats.get("size_bytes") or 0)
                    entry_count = int(stats.get("file_count") or stats.get("entry_count") or stats.get("count") or 0)
                    size_mb = total_bytes / (1024.0 * 1024.0)
            except Exception:
                pass
        if entry_count > 0:
            self.btn_clear_tts_cache.configure(text=f"캐시 비우기 ({size_mb:.1f} MB · {entry_count}개)")
        else:
            self.btn_clear_tts_cache.configure(text=f"캐시 비우기 ({size_mb:.1f} MB)")

    def _fish_mcp_cancel_login(self) -> None:
        if not self._fish_mcp_login_in_progress:
            return
        cancel_seq = self._fish_mcp_login_seq
        self._fish_mcp_cancelled_seq = cancel_seq
        self._fish_mcp_live_status = "로그인 취소 중..."
        attempt_ev = getattr(self, "_fish_mcp_attempt_cancel_event", None)
        if attempt_ev is not None:
            try:
                attempt_ev.set()
            except Exception:
                pass
        self._refresh_fish_mcp_ui_state()
        if self.app is not None and hasattr(self.app, "fish_mcp_cancel_login"):
            try:
                self.app.fish_mcp_cancel_login()
            except Exception:
                pass

        # Guarantee UI unblocks promptly even if a backend/mock ignores cancel_login
        def _ensure_unblocked() -> None:
            if not self.winfo_exists():
                return
            if self._fish_mcp_login_in_progress and self._fish_mcp_login_seq == cancel_seq:
                self._fish_mcp_login_in_progress = False
                self._fish_mcp_live_status = ""
                self._fish_mcp_login_seq += 1
                self._refresh_fish_mcp_ui_state()

        self._schedule_after(1500, _ensure_unblocked)

    def _fish_mcp_login_async(self) -> None:
        import inspect
        import threading

        if not self.winfo_exists():
            return
        if self.app is None or not hasattr(self.app, "fish_mcp_login_blocking"):
            return
        if self._fish_mcp_login_in_progress:
            self._fish_mcp_cancel_login()
            return
        if self._fish_mcp_refresh_in_progress or getattr(self, "_fish_mcp_prompt_open", False):
            return

        # If already signed in, let the user sync credits without re-authorizing, or explicitly re-login
        if self._get_fish_mcp_connection_status()[0]:
            self._fish_mcp_prompt_open = True
            try:
                choice = messagebox.askyesnocancel(
                    "이미 Fish Audio 계정에 연결됨",
                    (
                        "이미 Fish Audio 공식 MCP 계정에 연결되어 있습니다.\n\n"
                        "• [예(Y)]: 기존 연결을 유지하고 크레딧 잔액만 즉시 동기화 (재인증 불필요)\n"
                        "• [아니오(N)]: 브라우저에서 다시 로그인 / 다른 계정으로 전환\n"
                        "• [취소]: 작업 취소"
                    ),
                    parent=self,
                )
            finally:
                self._fish_mcp_prompt_open = False
            # Re-check widget & concurrency state after modal nested event loop returns
            if not self.winfo_exists() or self._fish_mcp_login_in_progress or self._fish_mcp_refresh_in_progress:
                return
            if choice is None:
                return
            if choice is True:
                self._fish_mcp_refresh_balance_async(silent=False)
                return

        self._fish_mcp_login_seq += 1
        my_seq = self._fish_mcp_login_seq
        attempt_cancel_ev = threading.Event()
        self._fish_mcp_attempt_cancel_event = attempt_cancel_ev
        self._fish_mcp_login_in_progress = True
        self._fish_mcp_live_status = "브라우저에서 Fish Audio 로그인 대기 중..."
        self._refresh_fish_mcp_ui_state()

        # Snapshot secrets on the Tk main thread — never touch Tk vars/widgets from worker threads
        secrets_snapshot = self._current_secrets()
        state_lock = threading.Lock()
        state_box: dict[str, Any] = {
            "status": "",
            "done": False,
            "ok": False,
            "msg": "",
        }

        def _on_status(status_text: str) -> None:
            safe_txt = redact_secrets(str(status_text or "").strip(), secrets_snapshot)
            if not safe_txt:
                return
            with state_lock:
                state_box["status"] = safe_txt

        def _worker() -> None:
            ok = False
            msg = ""
            try:
                if attempt_cancel_ev.is_set():
                    ok = False
                    msg = "OAuth 로그인이 취소되었습니다."
                else:
                    fn = self.app.fish_mcp_login_blocking
                    call_kwargs: dict[str, Any] = {}
                    try:
                        sig = inspect.signature(fn)
                        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                        if has_var_kw or "status_cb" in sig.parameters:
                            call_kwargs["status_cb"] = _on_status
                        if has_var_kw or "cancel_check" in sig.parameters:
                            call_kwargs["cancel_check"] = attempt_cancel_ev.is_set
                    except Exception:
                        call_kwargs = {}
                    ok, msg = fn(**call_kwargs)
            except Exception as exc:
                ok = False
                msg = redact_secrets(str(exc), secrets_snapshot)
            finally:
                with state_lock:
                    state_box["ok"] = bool(ok)
                    state_box["msg"] = str(msg or "")
                    state_box["done"] = True

        def _poll() -> None:
            if not self.winfo_exists() or self._fish_mcp_login_seq != my_seq:
                return
            with state_lock:
                cur_status = str(state_box["status"] or "")
                is_done = bool(state_box["done"])
                ok = bool(state_box["ok"])
                msg = str(state_box["msg"] or "")

            if not is_done:
                if (
                    cur_status
                    and cur_status != self._fish_mcp_live_status
                    and self._fish_mcp_cancelled_seq != my_seq
                ):
                    self._fish_mcp_live_status = cur_status
                    self.lbl_fish_mcp_status.configure(text=cur_status, bg="#FEF3C7", fg="#92400E")
                self._schedule_after(60, _poll)
                return

            was_user_cancel = self._fish_mcp_cancelled_seq == my_seq
            try:
                self._fish_mcp_login_in_progress = False
                self._fish_mcp_live_status = ""
                if self._fish_mcp_attempt_cancel_event is attempt_cancel_ev:
                    self._fish_mcp_attempt_cancel_event = None
                if isinstance(getattr(self.app, "cfg", None), dict):
                    self.cfg["fish_mcp_oauth_data"] = self.app.cfg.get("fish_mcp_oauth_data", "")
            finally:
                self._refresh_fish_mcp_ui_state()

            if was_user_cancel and not ok:
                return
            safe_msg = redact_secrets(msg, self._current_secrets())
            if ok:
                messagebox.showinfo(
                    "Fish Audio MCP 연결 완료",
                    safe_msg or "Fish Audio 공식 MCP 웹 크레딧 계정 연결이 완료되었습니다!",
                    parent=self,
                )
            else:
                messagebox.showwarning(
                    "Fish Audio MCP 로그인 실패",
                    safe_msg or "로그인을 완료하지 못했습니다. 다시 시도해 주세요.",
                    parent=self,
                )

        threading.Thread(target=_worker, daemon=True, name="fish-mcp-oauth-login").start()
        self._schedule_after(60, _poll)

    def _fish_mcp_refresh_balance_async(self, *, silent: bool = False) -> None:
        import threading

        if not self.winfo_exists():
            return
        if self.app is None or not hasattr(self.app, "fish_mcp_refresh_balance_blocking"):
            return
        if (
            self._fish_mcp_login_in_progress
            or self._fish_mcp_refresh_in_progress
            or getattr(self, "_fish_mcp_prompt_open", False)
        ):
            return
        self._fish_mcp_refresh_in_progress = True
        self._refresh_fish_mcp_ui_state()

        secrets_snapshot = self._current_secrets()
        state_lock = threading.Lock()
        state_box: dict[str, Any] = {"done": False, "ok": False, "msg": ""}

        def _worker() -> None:
            ok = False
            msg = ""
            try:
                ok, msg = self.app.fish_mcp_refresh_balance_blocking()
            except Exception as exc:
                ok = False
                msg = redact_secrets(str(exc), secrets_snapshot)
            finally:
                with state_lock:
                    state_box["ok"] = bool(ok)
                    state_box["msg"] = str(msg or "")
                    state_box["done"] = True

        def _poll() -> None:
            if not self.winfo_exists():
                return
            with state_lock:
                is_done = bool(state_box["done"])
                ok = bool(state_box["ok"])
                msg = str(state_box["msg"] or "")
            if not is_done:
                self._schedule_after(60, _poll)
                return

            try:
                self._fish_mcp_refresh_in_progress = False
                if isinstance(getattr(self.app, "cfg", None), dict):
                    self.cfg["fish_mcp_oauth_data"] = self.app.cfg.get("fish_mcp_oauth_data", "")
            finally:
                self._refresh_fish_mcp_ui_state()
            if not ok and msg:
                if silent:
                    if self._get_fish_mcp_connection_status()[0] and not self._has_cached_fish_mcp_balance():
                        self.lbl_fish_mcp_status.configure(
                            text="연결됨 · 잔액 조회 실패 (새로고침 클릭)",
                            bg="#FEF3C7",
                            fg="#92400E",
                        )
                else:
                    messagebox.showwarning(
                        "잔액 조회 실패",
                        redact_secrets(msg, self._current_secrets()),
                        parent=self,
                    )

        threading.Thread(target=_worker, daemon=True, name="fish-mcp-balance-refresh").start()
        self._schedule_after(60, _poll)

    def _fish_mcp_logout_clicked(self) -> None:
        if self.app is None or not hasattr(self.app, "fish_mcp_logout"):
            return
        if self._fish_mcp_login_in_progress or self._fish_mcp_refresh_in_progress:
            return
        if not messagebox.askyesno(
            "Fish Audio MCP 연결 해제",
            "저장된 Fish Audio 공식 MCP OAuth 로그인 토큰을 삭제하고 연결을 해제할까요?",
            parent=self,
        ):
            return
        if self._destroyed or not self.winfo_exists():
            return
        try:
            self.app.fish_mcp_logout()
            self.cfg["fish_mcp_oauth_data"] = ""
        except Exception as exc:
            messagebox.showwarning(
                "연결 해제 오류",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )
        self._refresh_fish_mcp_ui_state()

    def _clear_tts_cache_clicked(self) -> None:
        if self.app is None or not hasattr(self.app, "clear_tts_cache"):
            return
        try:
            removed = int(self.app.clear_tts_cache() or 0)
            self._refresh_tts_cache_button_label()
            messagebox.showinfo(
                "로컬 TTS 캐시 비우기 완료",
                f"로컬 TTS 캐시 {removed}개 항목을 삭제했습니다.",
                parent=self,
            )
        except Exception as exc:
            messagebox.showwarning(
                "캐시 삭제 오류",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )

    def _open_external_url(self, url: str) -> None:
        try:
            open_safe_external_url(url)
        except Exception as exc:
            messagebox.showerror(
                "링크 열기 차단",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )

    def _open_api_key_portal_with_warning(self, provider: str, url: str) -> None:
        p = normalize_ai_provider(provider)
        warn_msg = (
            _build_api_warning_text(p)
            + "\n\n위 주의사항을 확인했으며 API 키 발급 페이지를 열까요?"
        )
        confirmed = messagebox.askokcancel(
            "⚠️ AI API 키 발급 전 주의 안내",
            warn_msg,
            parent=self,
        )
        if not confirmed:
            return
        self._open_external_url(url)

    def _paste_into(self, var: tk.StringVar, clean_ref: bool = False) -> None:
        try:
            raw_clip = pyperclip.paste() or ""
        except Exception as exc:
            messagebox.showwarning(
                "클립보드 읽기 실패",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )
            return
        raw = _sanitize_single_line_secret(raw_clip)
        if not raw:
            messagebox.showwarning("클립보드 비어 있음", "먼저 복사한 값을 클립보드에 담아 주세요.", parent=self)
            return
        if clean_ref:
            cleaned = _clean_ref_id(raw)
            if not cleaned:
                messagebox.showwarning(
                    "유효하지 않은 Voice ID",
                    "올바른 Fish Audio 보이스 URL 또는 32자리 ID를 복사해 주세요.",
                    parent=self,
                )
                return
            var.set(cleaned)
        else:
            var.set(raw)

    def _open_user_data_dir(self) -> None:
        try:
            p = get_user_data_dir()
            if "\x00" in str(p) or p.is_symlink():
                raise ValueError("Refusing to open invalid or symlinked user data directory")
            resolved = p.resolve()
            if not resolved.is_dir() or not resolved.is_absolute():
                raise ValueError("User data directory is not accessible")
            if os.name == "nt":
                if str(resolved).startswith(("/", "-")):
                    raise ValueError("Invalid Windows directory path")
                os.startfile(str(resolved), "explore")  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(
                "폴더 열기 실패",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )

    def _handle_open_logs(self) -> None:
        if self.on_open_logs is None:
            return
        try:
            self.grab_release()
        except Exception:
            pass
        self.on_open_logs()

    def _sync_sub_thinking_state(self) -> None:
        sub_eng_by_lbl = {lbl: code for code, lbl in self._all_sub_choices}
        code = sub_eng_by_lbl.get(self.subtitle_engine_var.get().strip(), "auto_inline")
        if hasattr(self, "cmb_sub_thinking") and self.cmb_sub_thinking is not None:
            self.cmb_sub_thinking.configure(state="disabled" if code == "fast_nmt" else "readonly")

    def _sync_web_models_async(self) -> None:
        if self._on_sync_web_models is None:
            messagebox.showinfo("안내", "전용 브라우저 로그인 완료 후 자동으로 계정 모델이 동기화됩니다.", parent=self)
            return
        target_p = normalize_ai_provider(self.ai_provider_var.get())
        self.btn_sync_web_models.configure(state="disabled", text="동기화 중...")

        def _on_done(models: list[tuple[str, str]], tier: str, err: str) -> None:
            def _apply() -> None:
                if self._destroyed or not self.winfo_exists():
                    return
                self.btn_sync_web_models.configure(state="normal", text="🔄 웹 모델 동기화")
                clean_models = [
                    (str(it[0]).strip(), str(it[1]).strip())
                    for it in (models or [])
                    if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[0]).strip() and str(it[1]).strip()
                ]
                is_default_only = len(clean_models) <= 1 and (
                    not clean_models or clean_models[0][0].lower() == "default"
                )
                if err and (not clean_models or is_default_only):
                    messagebox.showwarning(
                        "웹 모델 동기화 안내",
                        redact_secrets(str(err), self._current_secrets()),
                        parent=self,
                    )
                    return
                if not clean_models or is_default_only:
                    messagebox.showinfo(
                        "동기화 안내",
                        "현재 계정에서 모델 선택 메뉴를 읽지 못했습니다. 기존 모델 목록을 유지합니다.",
                        parent=self,
                    )
                    return
                self._provider_account_web_models[target_p] = list(clean_models)
                if tier:
                    tier_key = {
                        "gemini": "web_account_tier",
                        "claude": "claude_web_account_tier",
                        "chatgpt": "chatgpt_web_account_tier",
                    }[target_p]
                    self.cfg[tier_key] = tier
                if self._active_provider_ui == target_p:
                    self._refresh_provider_model_catalogs(target_p)
                    self.cmb_browser_model.configure(values=[lbl for _, lbl in self._all_browser_models])
                    if self.gemini_browser_model_var.get().strip() not in set(self._browser_lbl_by_code.values()):
                        def_code = get_provider_default_browser_model(target_p)
                        fallback_code = def_code if def_code in self._browser_lbl_by_code else self._all_browser_models[0][0]
                        self.gemini_browser_model_var.set(self._browser_lbl_by_code[fallback_code])
                    self.cmb_sub_engine.configure(values=[lbl for _, lbl in self._all_sub_choices])
                    valid_sub_lbls = {lbl for _, lbl in self._all_sub_choices}
                    if self.subtitle_engine_var.get().strip() not in valid_sub_lbls:
                        self.subtitle_engine_var.set(resolve_subtitle_engine_label("auto_inline", self._all_sub_choices))
                    self._sync_sub_thinking_state()
                short = AI_PROVIDER_SHORT_NAMES[target_p]
                tier_txt = f"{tier} 계정" if tier else f"웹 {short} 계정"
                messagebox.showinfo("동기화 완료", f"{tier_txt} 사용 가능 모델 {len(clean_models)}개를 불러왔습니다.", parent=self)

            self._enqueue_ui(_apply)

        try:
            self._on_sync_web_models(_on_done, provider_override=target_p)  # type: ignore[call-arg]
        except TypeError:
            try:
                self._on_sync_web_models(_on_done)
            except Exception as exc:
                if not self._destroyed and self.winfo_exists():
                    self.btn_sync_web_models.configure(state="normal", text="🔄 웹 모델 동기화")
                    messagebox.showwarning(
                        "웹 모델 동기화 오류",
                        redact_secrets(str(exc), self._current_secrets()),
                        parent=self,
                    )
        except Exception as exc:
            if not self._destroyed and self.winfo_exists():
                self.btn_sync_web_models.configure(state="normal", text="🔄 웹 모델 동기화")
                messagebox.showwarning(
                    "웹 모델 동기화 오류",
                    redact_secrets(str(exc), self._current_secrets()),
                    parent=self,
                )

    def _fetch_api_models_async(self, provider: str | None = None) -> None:
        import threading

        target_p = normalize_ai_provider(provider or self.ai_provider_var.get())
        key_var = {
            "gemini": self.gemini_key_var,
            "claude": self.claude_key_var,
            "chatgpt": self.openai_key_var,
        }[target_p]
        btn_widget = {
            "gemini": self.btn_fetch_api_models,
            "claude": self.btn_fetch_claude_models,
            "chatgpt": self.btn_fetch_openai_models,
        }[target_p]
        api_name = {"gemini": "Gemini", "claude": "Claude", "chatgpt": "OpenAI"}[target_p]
        key = key_var.get().strip()
        if not key:
            messagebox.showinfo(f"{api_name} API Key 필요", f"먼저 {api_name} API Key를 입력한 뒤 모델 목록을 조회해 주세요.", parent=self)
            return
        btn_widget.configure(state="disabled", text="조회 중...")

        def _worker() -> None:
            err_msg = ""
            fetched: list[str] = []
            try:
                api: Any = (
                    OfficialClaudeApiBackend()
                    if target_p == "claude"
                    else (OfficialOpenAIApiBackend() if target_p == "chatgpt" else OfficialGeminiApiBackend())
                )
                try:
                    fetched = api.list_generate_models(key)
                finally:
                    api.close()
            except Exception as exc:
                err_msg = redact_secrets(str(exc), (key,))

            def _done() -> None:
                if self._destroyed or not self.winfo_exists():
                    return
                btn_widget.configure(state="normal", text="🔄 API 모델 조회")
                if err_msg:
                    messagebox.showwarning(
                        "모델 조회 실패",
                        redact_secrets(err_msg, self._current_secrets()),
                        parent=self,
                    )
                    return
                if not fetched:
                    messagebox.showinfo("조회 완료", f"사용 가능한 {api_name} 모델을 찾지 못했습니다.", parent=self)
                    return
                clean_fetched: list[str] = []
                seen_f: set[str] = set()
                for m in fetched:
                    cid = extract_safe_api_model_id(m, provider=target_p)
                    if cid and cid not in seen_f:
                        seen_f.add(cid)
                        clean_fetched.append(cid)
                if not clean_fetched:
                    messagebox.showinfo("조회 완료", f"사용 가능한 {api_name} 모델을 찾지 못했습니다.", parent=self)
                    return
                self._provider_extra_api_models[target_p] = list(clean_fetched)
                raw_cur = (
                    self.gemini_model_var.get().strip()
                    if self._active_provider_ui == target_p
                    else self._provider_api_model_selected.get(target_p, "")
                )
                cur = extract_safe_api_model_id(raw_cur, provider=target_p)
                fetched_set = set(clean_fetched)
                if (not cur) or (cur not in fetched_set):
                    if target_p == "gemini":
                        selected_model = pick_best_gemini_free_model(clean_fetched, preferred=cur)
                    else:
                        preset_codes = [code for code, _ in get_provider_api_models(target_p)]
                        selected_model = next((m for m in preset_codes if m in fetched_set), clean_fetched[0])
                    self._provider_api_model_selected[target_p] = selected_model
                    if self._active_provider_ui == target_p:
                        self.gemini_model_var.set(selected_model)
                    selected_notice = f"\n\n자동 선택된 권장 모델: {selected_model}"
                else:
                    self._provider_api_model_selected[target_p] = cur
                    if self._active_provider_ui == target_p:
                        self.gemini_model_var.set(cur)
                    selected_notice = f"\n\n현재 선택된 모델: {cur}"
                if self._active_provider_ui == target_p:
                    self._refresh_provider_model_catalogs(target_p)
                    api_vals = self._build_api_model_dropdown_values(target_p)
                    self.cmb_api_model.configure(values=api_vals)
                    self.cmb_sub_engine.configure(values=[lbl for _, lbl in self._all_sub_choices])
                messagebox.showinfo(
                    "조회 완료",
                    f"{api_name} API 모델 {len(clean_fetched)}개를 불러왔습니다.{selected_notice}",
                    parent=self,
                )

            self._enqueue_ui(_done)

        threading.Thread(target=_worker, daemon=True, name=f"fetch-{target_p}-models").start()

    def _edit_selectors_json(self) -> None:
        try:
            p = _validate_user_selectors_path(ensure_user_selectors_copy())
            if os.name == "nt":
                sys_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows")
                notepad = sys_root / "System32" / "notepad.exe"
                if not notepad.is_file():
                    raise FileNotFoundError("Windows notepad.exe를 찾을 수 없습니다.")
                subprocess.Popen([str(notepad), str(p)], shell=False)
        except Exception as exc:
            messagebox.showerror(
                "selectors.json 열기 실패",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )

    def _get_reply_lang(self) -> str:
        return "ko" if self.reply_language_var.get() == "ko" else "ja"

    def _refresh_steam_dir_status_label(self) -> None:
        if not hasattr(self, "lbl_steam_dir_status"):
            return
        typed = self.steam_game_dir_var.get().strip()
        if typed:
            if is_valid_ba_game_dir(typed):
                self.lbl_steam_dir_status.configure(text=f"✅ 지정된 스팀 폴더 확인됨: {typed}", fg="#047857")
            else:
                self.lbl_steam_dir_status.configure(
                    text="⚠️ 지정한 경로에서 BlueArchive_Data 리소스를 찾지 못했습니다. 폴더를 다시 확인해 주세요.",
                    fg="#B45309",
                )
            return
        found = discover_steam_blue_archive_dir()
        if found is not None:
            self.lbl_steam_dir_status.configure(text=f"✅ 자동 감지됨: {found}", fg="#047857")
        else:
            self.lbl_steam_dir_status.configure(
                text="ℹ️ 스팀 블루 아카이브 미감지 (설치되어 있다면 [📁 폴더 지정]으로 한 번만 등록해 주세요)",
                fg="#64748B",
            )

    def _browse_steam_game_dir(self) -> None:
        picked = filedialog.askdirectory(title="스팀 BlueArchive 설치 폴더 선택", parent=self)
        if not picked:
            return
        from pathlib import Path

        p = Path(picked)
        if p.name.lower() == "bluearchive_data":
            p = p.parent
        self.steam_game_dir_var.set(str(p))
        self._refresh_steam_dir_status_label()

    def _auto_detect_steam_game_dir(self) -> None:
        found = discover_steam_blue_archive_dir(use_cache=False)
        if found is not None:
            self.steam_game_dir_var.set(str(found))
            self._refresh_steam_dir_status_label()
            messagebox.showinfo("스팀 폴더 감지 완료", f"블루 아카이브 설치 폴더를 찾았습니다!\n{found}", parent=self)
        else:
            self._refresh_steam_dir_status_label()
            messagebox.showwarning(
                "자동 감지 실패",
                "스팀 블루 아카이브 설치 폴더를 자동으로 찾지 못했습니다.\n[📁 폴더 지정]을 눌러 직접 선택해 주세요.",
                parent=self,
            )

    def _sync_voice_vars_from_app_cfg(self) -> None:
        """Pull newly linked KR & JP main/alt voice IDs from `self.app.cfg` without erasing unsaved user edits."""
        if self._destroyed or not self.winfo_exists() or self.app is None or not isinstance(getattr(self.app, "cfg", None), dict):
            return
        app_jp = dict(self.app.cfg.get("saved_voices") or {})
        app_kr = dict(self.app.cfg.get("saved_voices_kr") or {})
        self.cfg["saved_voices"] = app_jp
        self.cfg["saved_voices_kr"] = app_kr
        cur_cid = self._current_character_id()
        cur_rlang = self._get_reply_lang()
        latest_kr = str(app_kr.get(cur_cid) or "").strip()
        latest_jp = str(app_jp.get(cur_cid) or "").strip()
        if latest_kr:
            self._deleted_voice_slots.discard(("saved_voices_kr", cur_cid))
            if latest_kr != self.ref_id_kr_var.get().strip():
                self.ref_id_kr_var.set(latest_kr)
        if latest_jp:
            self._deleted_voice_slots.discard(("saved_voices", cur_cid))
            if latest_jp != self.ref_id_jp_var.get().strip():
                self.ref_id_jp_var.set(latest_jp)
        latest_active = latest_kr if cur_rlang == "ko" else latest_jp
        if latest_active:
            self.cfg["fish_reference_id"] = latest_active
            if hasattr(self.app, "ref_id_var"):
                try:
                    self.app.ref_id_var.set(latest_active)
                except Exception:
                    pass
        for _ak, slot_id, _lbl in self._alt_voice_specs:
            alt_kr = str(app_kr.get(slot_id) or "").strip()
            alt_jp = str(app_jp.get(slot_id) or "").strip()
            if alt_kr:
                self._deleted_voice_slots.discard(("saved_voices_kr", slot_id))
                if slot_id in self._alt_voice_kr_vars:
                    self._alt_voice_kr_vars[slot_id].set(alt_kr)
            if alt_jp:
                self._deleted_voice_slots.discard(("saved_voices", slot_id))
                if slot_id in self._alt_voice_jp_vars:
                    self._alt_voice_jp_vars[slot_id].set(alt_jp)
        self._refresh_voice_ref_status_ui()

    def _auto_create_current_voice(self) -> None:
        if self.app is None:
            return
        if not hasattr(self.app, "auto_generate_current_student_voice_async"):
            self._run_app_method("auto_create_aru_model_async", push_key_ref=True)
            return
        self._push_live_app_vars(push_key_ref=True)

        def _on_done(res: dict[str, Any]) -> None:
            if not isinstance(res, dict) or not res.get("ok"):
                return
            self._enqueue_ui(self._sync_voice_vars_from_app_cfg)

        self.app.auto_generate_current_student_voice_async(on_done=_on_done)

    def _auto_create_alt_voice(self, alt_key: str, slot_id: str) -> None:
        if self.app is None or not hasattr(self.app, "auto_generate_current_student_voice_async"):
            return
        self._push_live_app_vars(push_key_ref=True)
        fallback_lang_tag = "KR" if self._get_reply_lang() == "ko" else "JP"

        def _on_alt_done(res: dict[str, Any]) -> None:
            if not res.get("ok"):
                return
            mid = str(res.get("model_id") or "").strip()
            if not mid:
                return
            is_kr = str(res.get("lang") or fallback_lang_tag).strip().upper() == "KR"

            def _upd() -> None:
                if self._destroyed or not self.winfo_exists():
                    return
                bucket = "saved_voices_kr" if is_kr else "saved_voices"
                self.cfg.setdefault(bucket, {})[slot_id] = mid
                target_map = self._alt_voice_kr_vars if is_kr else self._alt_voice_jp_vars
                if slot_id in target_map:
                    target_map[slot_id].set(mid)

            self._enqueue_ui(_upd)

        self.app.auto_generate_current_student_voice_async(
            alt_voice_key=alt_key,
            on_done=_on_alt_done,
        )

    def _collect_voice_test_overrides(
        self,
        *,
        alt_slot_id: str = "",
        alt_var: tk.StringVar | None = None,
    ) -> dict[str, Any] | None:
        """Build an ephemeral settings override dict for Voice Test without mutating `self.app.cfg` or `config.json`."""
        rlang = self._get_reply_lang()
        cid = self._current_character_id()
        colab_enabled = bool(self.colab_s2pro_enabled_var.get())
        colab_url_raw = self.colab_s2pro_url_var.get().strip()
        colab_url = ""
        if colab_url_raw:
            try:
                colab_url = validate_colab_server_url(colab_url_raw, resolve_dns=False)
            except ValueError as exc:
                if colab_enabled:
                    messagebox.showwarning("Colab S2-Pro 서버 주소 오류", str(exc), parent=self)
                    return None
                colab_url = ""

        clean_kr = _clean_ref_id(self.ref_id_kr_var.get())
        clean_jp = _clean_ref_id(self.ref_id_jp_var.get())
        saved_voices_override: dict[str, str] = {}
        saved_voices_kr_override: dict[str, str] = {}
        if cid:
            if clean_jp:
                saved_voices_override[cid] = clean_jp
            if clean_kr:
                saved_voices_kr_override[cid] = clean_kr
        if alt_slot_id:
            for target_map, is_kr_bucket, var_map in (
                (saved_voices_kr_override, True, self._alt_voice_kr_vars),
                (saved_voices_override, False, self._alt_voice_jp_vars),
            ):
                v = var_map.get(alt_slot_id)
                if v is None and alt_var is not None and ((rlang == "ko") == is_kr_bucket):
                    v = alt_var
                if v is None:
                    continue
                clean_alt = _clean_ref_id(v.get())
                target_map[alt_slot_id] = clean_alt  # empty string signals explicit clear in ephemeral test

        raw_hdr = self.fish_model_hdr_var.get().strip()
        return {
            "reply_language": rlang,
            "persona_variant": PERSONA_LABEL_TO_CODE.get(self.persona_variant_var.get().strip(), "official"),
            "jp_beginner_lover_mode": False if rlang == "ko" else bool(self.jp_beginner_coach_var.get()),
            "use_custom_persona": bool(self.use_custom_persona_var.get()),
            "fish_api_key": _sanitize_single_line_secret(self.fish_key_var.get()),
            "fish_reference_id": clean_kr if rlang == "ko" else clean_jp,
            "saved_voices_override": saved_voices_override,
            "saved_voices_kr_override": saved_voices_kr_override,
            "fish_model_header": raw_hdr if raw_hdr in FISH_MODEL_HEADERS else "s2.1-pro-free",
            "fish_mcp_fallback_enabled": bool(self.fish_mcp_fallback_var.get()),
            "fish_mcp_primary_mode": bool(self.fish_mcp_primary_var.get()),
            "fish_credit_saver_mode": bool(self.fish_credit_saver_var.get()),
            "fish_tts_cache_enabled": bool(self.fish_tts_cache_var.get()),
            "colab_s2pro_enabled": bool(colab_enabled and colab_url),
            "colab_s2pro_url": colab_url,
            "colab_s2pro_api_key": _sanitize_single_line_secret(self.colab_s2pro_key_var.get()),
            "steam_game_dir": self.steam_game_dir_var.get().strip(),
            "streaming_speed_mode": SPEED_LABEL_TO_CODE.get(self.speed_mode_var.get().strip(), "expressive"),
            "auto_emotion_directing": bool(self.auto_emotion_var.get()),
            "expression_boost_offset": float(BOOST_LABEL_TO_VAL.get(self.boost_preset_var.get().strip(), 0.0)),
            "output_volume": self._safe_float(self.vol_var.get(), 1.0, 0.10, 2.00),
            "primary_output_device": self.primary_out_var.get().strip(),
        }

    def _test_current_voice(self) -> None:
        if self.app is None or not hasattr(self.app, "test_aru_tts_async"):
            return
        overrides = self._collect_voice_test_overrides()
        if overrides is None:
            return
        try:
            self.app.test_aru_tts_async(overrides=overrides)
        except TypeError:
            self.app.test_aru_tts_async()

    def _test_alt_voice(self, alt_key: str, slot_id: str, alt_var: tk.StringVar | None = None) -> None:
        if self.app is None or not hasattr(self.app, "test_alt_voice_tts_async"):
            return
        overrides = self._collect_voice_test_overrides(alt_slot_id=slot_id, alt_var=alt_var)
        if overrides is None:
            return
        try:
            self.app.test_alt_voice_tts_async(alt_key, overrides=overrides)
        except TypeError:
            self.app.test_alt_voice_tts_async(alt_key)

    def _open_batch_voice_dialog(self) -> None:
        if self.app is None:
            return
        from src.controller.fish_batch_dialog import FishBatchDialog

        # Push latest typed API key so batch dialog can use it immediately without needing Save first
        typed_key = _sanitize_single_line_secret(self.fish_key_var.get())
        if typed_key:
            self.cfg["fish_api_key"] = typed_key
            self.app.cfg["fish_api_key"] = typed_key

        persist_cb = getattr(self.app, "_persist_batch_voice_link_locked", None)

        def _sync_cfg_from_app_on_main() -> None:
            """Keep SettingsDialog's local cfg copy & dual KR/JP ref vars synchronized so Save never overwrites batch links."""
            self._sync_voice_vars_from_app_cfg()

        def _on_linked(cid: str, lang_up: str, mid: str) -> None:
            bucket = "saved_voices_kr" if str(lang_up or "").upper() == "KR" else "saved_voices"

            def _apply_app_ui() -> None:
                if not callable(persist_cb) and isinstance(getattr(self.app, "cfg", None), dict):
                    self.app.cfg.setdefault(bucket, {})[cid] = mid
                if (
                    getattr(self.app, "_momotalk_sort_mode", "default") == "heart"
                    or bool(getattr(self.app, "_roster_voice_only", False))
                ):
                    self.app._refresh_character_filter_list(keep_current=True)
                else:
                    self.app._render_roster_visible(force_all=True)
                self.app._refresh_momotalk_header_and_cards()
                _sync_cfg_from_app_on_main()

            if hasattr(self.app, "_schedule_ui"):
                try:
                    self.app._schedule_ui(_apply_app_ui)
                    return
                except Exception:
                    pass
            # Fallback when app has no _schedule_ui (e.g. lightweight test mocks)
            def _fallback_main() -> None:
                if not callable(persist_cb) and isinstance(getattr(self.app, "cfg", None), dict):
                    self.app.cfg.setdefault(bucket, {})[cid] = mid
                _sync_cfg_from_app_on_main()

            self._enqueue_ui(_fallback_main)

        def _on_done() -> None:
            try:
                _sync_cfg_from_app_on_main()
                self.app._refresh_momotalk_header_and_cards()
                self.app._render_roster_visible(force_all=True)
            except Exception:
                pass

        cfg_for_batch = (
            self.app._snapshot_cfg_locked()
            if hasattr(self.app, "_snapshot_cfg_locked")
            else self.app.cfg
        )
        FishBatchDialog(
            self,
            cfg=cfg_for_batch,
            characters=self.app.characters,
            api_key_getter=lambda: _sanitize_single_line_secret(self.fish_key_var.get()),
            on_model_linked=_on_linked,
            on_finished=_on_done,
            persist_voice_link_cb=persist_cb if callable(persist_cb) else None,
        )

    def _colab_s2pro_paste_bundle(self) -> None:
        raw_clip = ""
        try:
            raw_clip = str(self.clipboard_get() or "")
        except Exception:
            try:
                import pyperclip

                raw_clip = str(pyperclip.paste() or "")
            except Exception:
                raw_clip = ""

        try:
            parsed_url, parsed_key = parse_colab_connection_bundle(raw_clip)
        except ValueError as exc:
            self.colab_s2pro_status_var.set(f"⚠️ {redact_secrets(str(exc), self._current_secrets())}")
            return

        self.colab_s2pro_url_var.set(parsed_url)
        if parsed_key:
            self.colab_s2pro_key_var.set(parsed_key)
        self.colab_s2pro_enabled_var.set(True)
        key_note = "URL + 인증 토큰" if parsed_key else "URL"
        self.colab_s2pro_status_var.set(f"📋 클립보드에서 {key_note} 자동 입력 완료 — 연결 테스트 진행 중...")
        self._colab_s2pro_test_health_async()

    def _colab_s2pro_auto_discover_async(self) -> None:
        raw_folder = self.colab_s2pro_drive_folder_var.get().strip()
        folder_id = extract_google_drive_id(raw_folder, kind="folder")
        if raw_folder and not folder_id:
            self.colab_s2pro_status_var.set(
                "⚠️ Drive 폴더 ID 형식이 올바르지 않습니다. 폴더 ID 또는 https://drive.google.com/drive/folders/... 주소를 입력해 주세요."
            )
            return
        if folder_id and folder_id != raw_folder:
            self.colab_s2pro_drive_folder_var.set(folder_id)
        self.colab_s2pro_status_var.set("🔄 Google Drive에서 실행 중인 Colab S2-Pro 서버 주소를 찾는 중...")
        ep_file = self.colab_s2pro_endpoint_file_var.get().strip()
        secrets_snapshot = self._current_secrets()
        try:
            self.btn_colab_s2pro_auto.configure(state="disabled")
        except Exception:
            pass

        def _worker() -> None:
            info: dict[str, Any] | None = None
            err_msg = ""
            try:
                info = discover_endpoint_from_drive(
                    timeout_sec=12.0,
                    endpoint_file=ep_file or None,
                    drive_folder_id=folder_id or None,
                )
            except Exception as exc:
                err_msg = redact_secrets(str(exc), secrets_snapshot)

            def _apply() -> None:
                if self._destroyed or not self.winfo_exists():
                    return
                try:
                    self.btn_colab_s2pro_auto.configure(state="normal")
                except Exception:
                    pass
                if err_msg:
                    self.colab_s2pro_status_var.set(f"❌ Drive 자동 조회 오류: {err_msg}")
                    return
                if not info or not info.get("url"):
                    self.colab_s2pro_status_var.set(
                        "⚠️ Drive에서 실행 중인 서버를 찾지 못했습니다. Colab 출력의 1줄 연결 정보를 복사해 [📋 연결 정보 붙여넣기]를 눌러 주세요."
                    )
                    return
                self.colab_s2pro_url_var.set(str(info.get("url") or "").strip())
                if info.get("api_key"):
                    self.colab_s2pro_key_var.set(str(info.get("api_key") or "").strip())
                self.colab_s2pro_enabled_var.set(True)
                gpu_lbl = str(info.get("gpu") or "Colab GPU")
                self.colab_s2pro_status_var.set(f"✅ Drive 자동 불러오기 성공 ({gpu_lbl}) — 연결 테스트 진행 중...")
                self._colab_s2pro_test_health_async()

            self._enqueue_ui(_apply)

        threading.Thread(target=_worker, daemon=True, name="colab-s2pro-discover").start()

    def _colab_s2pro_test_health_async(self) -> None:
        if self._destroyed or not self.winfo_exists():
            return
        url = self.colab_s2pro_url_var.get().strip()
        key = self.colab_s2pro_key_var.get().strip()
        if not url:
            self.colab_s2pro_status_var.set("⚠️ 먼저 Colab S2-Pro 서버 주소(https://....trycloudflare.com)를 입력해 주세요.")
            return
        self.colab_s2pro_status_var.set("🧪 Colab S2-Pro 서버 연결 및 응답 속도 확인 중...")
        secrets_snapshot = self._current_secrets()
        try:
            self.btn_colab_s2pro_test.configure(state="disabled")
        except Exception:
            pass

        def _worker() -> None:
            ok = False
            msg = ""
            try:
                if self.app is not None and hasattr(self.app, "colab_s2pro_check_health"):
                    ok, msg, _rtt, _refs = self.app.colab_s2pro_check_health(override_url=url, override_key=key)
                else:
                    tmp_backend = ColabS2ProBackend(url_getter=lambda: url, api_key_getter=lambda: key)
                    try:
                        ok, msg, _rtt, _refs = tmp_backend.check_health()
                    finally:
                        tmp_backend.close()
            except Exception as exc:
                ok = False
                msg = f"연결 확인 오류: {exc}"
            safe_msg = redact_secrets(str(msg or ""), secrets_snapshot)

            def _apply() -> None:
                if self._destroyed or not self.winfo_exists():
                    return
                try:
                    self.btn_colab_s2pro_test.configure(state="normal")
                except Exception:
                    pass
                prefix = "✅" if ok else "❌"
                self.colab_s2pro_status_var.set(f"{prefix} {safe_msg}")
                if ok and self.app is not None and hasattr(self.app, "_prewarm_colab_s2pro_current_char"):
                    try:
                        self.app._prewarm_colab_s2pro_current_char(override_url=url, override_key=key)
                    except Exception:
                        pass

            self._enqueue_ui(_apply)

        threading.Thread(target=_worker, daemon=True, name="colab-s2pro-health").start()

    def _colab_s2pro_shutdown_gpu_async(self) -> None:
        if self._destroyed or not self.winfo_exists():
            return
        url = self.colab_s2pro_url_var.get().strip()
        key = self.colab_s2pro_key_var.get().strip()
        if not url:
            self.colab_s2pro_status_var.set("⚠️ 반납할 Colab S2-Pro 서버 주소가 입력되어 있지 않습니다.")
            return
        if not key:
            self.colab_s2pro_status_var.set(
                "⚠️ 인증 토큰(Key)이 없어 원격 GPU 반납을 실행할 수 없습니다. Colab [2단계 셀]을 직접 실행해 주세요."
            )
            return

        if not messagebox.askyesno(
            "Colab GPU 원격 반납",
            "현재 실행 중인 Colab S2-Pro GPU 서버를 원격으로 종료하고 GPU를 반납할까요?\n\n"
            "(다음에 다시 쓰려면 2_코랩_실행하기.bat 또는 Colab 1단계 셀을 실행하면 됩니다.)",
            parent=self,
        ):
            return
        if self._destroyed or not self.winfo_exists():
            return

        self.colab_s2pro_status_var.set("🛑 Colab S2-Pro 서버 원격 종료 및 GPU 반납을 요청하는 중...")
        secrets_snapshot = self._current_secrets()
        try:
            self.btn_colab_s2pro_shutdown.configure(state="disabled")
        except Exception:
            pass

        def _worker() -> None:
            ok = False
            msg = ""
            try:
                if self.app is not None and hasattr(self.app, "colab_s2pro_shutdown_gpu"):
                    ok, msg = self.app.colab_s2pro_shutdown_gpu(unassign=True, override_url=url, override_key=key)
                else:
                    tmp_backend = ColabS2ProBackend(url_getter=lambda: url, api_key_getter=lambda: key)
                    try:
                        ok, msg = tmp_backend.request_remote_shutdown(unassign=True)
                    finally:
                        tmp_backend.close()
            except Exception as exc:
                ok = False
                msg = f"원격 GPU 반납 요청 오류: {exc}"
            safe_msg = redact_secrets(str(msg or ""), secrets_snapshot)

            def _apply() -> None:
                if self._destroyed or not self.winfo_exists():
                    return
                try:
                    self.btn_colab_s2pro_shutdown.configure(state="normal")
                except Exception:
                    pass
                prefix = "✅" if ok else "❌"
                self.colab_s2pro_status_var.set(f"{prefix} {safe_msg}")
                if ok:
                    self.colab_s2pro_enabled_var.set(False)
                try:
                    if ok:
                        messagebox.showinfo("Colab GPU 반납 완료", safe_msg, parent=self)
                    else:
                        messagebox.showwarning("Colab GPU 반납 안내", safe_msg, parent=self)
                except Exception:
                    pass

            self._enqueue_ui(_apply)

        threading.Thread(target=_worker, daemon=True, name="colab-s2pro-shutdown").start()

    def _reload_from_cfg(self, new_cfg: dict[str, Any]) -> None:
        """Refresh all SettingsDialog internal state, Tk variables, and widgets in-place from `new_cfg`."""
        sanitized, _ = sanitize_user_config(new_cfg if isinstance(new_cfg, dict) else {})
        self.cfg = deepcopy(sanitized)
        self._deleted_voice_slots.clear()

        def _parse_web_pairs(raw_list: Any) -> list[tuple[str, str]]:
            out: list[tuple[str, str]] = []
            for item in raw_list or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    c, l = str(item[0]).strip(), str(item[1]).strip()
                    if c and l:
                        out.append((c, l))
            if len(out) == 1 and out[0][0].lower() == "default":
                return []
            return out

        def _clean_extra_api_list(raw_list: Any, provider: str) -> list[str]:
            out: list[str] = []
            seen: set[str] = set()
            for item in raw_list or []:
                cid = extract_safe_api_model_id(item, provider=provider)
                if cid and cid not in seen:
                    seen.add(cid)
                    out.append(cid)
            return out

        new_provider = normalize_ai_provider(self.cfg.get("ai_provider", "gemini"))
        self._active_provider_ui = new_provider
        self._extra_web_models = []
        self._provider_extra_api_models = {
            "gemini": _clean_extra_api_list(self.cfg.get("api_models", []), "gemini"),
            "claude": _clean_extra_api_list(self.cfg.get("claude_api_models", []), "claude"),
            "chatgpt": _clean_extra_api_list(self.cfg.get("openai_api_models", []), "chatgpt"),
        }
        self._provider_account_web_models = {
            "gemini": _parse_web_pairs(self.cfg.get("web_models")),
            "claude": _parse_web_pairs(self.cfg.get("claude_web_models")),
            "chatgpt": _parse_web_pairs(self.cfg.get("chatgpt_web_models")),
        }
        cur_gem_bmodel, cur_tlevel = split_legacy_browser_model(
            str(self.cfg.get("gemini_browser_model", "hydrogen")),
            str(self.cfg.get("gemini_thinking_level", "low")),
        )
        self._provider_browser_model_codes = {
            "gemini": cur_gem_bmodel,
            "claude": str(self.cfg.get("claude_browser_model", "default") or "default").strip(),
            "chatgpt": str(self.cfg.get("chatgpt_browser_model", "default") or "default").strip(),
        }
        self._provider_api_model_selected = {
            "gemini": coerce_safe_api_model_id(
                self.cfg.get("gemini_api_model"), provider="gemini", default="gemini-3.5-flash-lite"
            ),
            "claude": coerce_safe_api_model_id(
                self.cfg.get("claude_api_model"), provider="claude", default="claude-sonnet-5-5"
            ),
            "chatgpt": coerce_safe_api_model_id(
                self.cfg.get("openai_api_model"), provider="chatgpt", default="gpt-5.4-mini"
            ),
        }
        self._refresh_provider_model_catalogs(new_provider)

        # Tab 0: Prompt interpretation & custom persona
        pvar = str(self.cfg.get("persona_prompt_variant", "official") or "official")
        self.persona_variant_var.set(PERSONA_VARIANT_LABELS.get(pvar, PERSONA_VARIANT_LABELS["official"]))
        self.proactive_prompt_var.set(bool(self.cfg.get("proactive_prompt_append", True)))
        rlang = "ko" if str(self.cfg.get("reply_language", "ja") or "ja").strip().lower() == "ko" else "ja"
        self._syncing_voice_lang = True
        try:
            self._voice_editor_lang = rlang
            self.reply_language_var.set(rlang)
            self.jp_beginner_coach_var.set(False if rlang == "ko" else bool(self.cfg.get("jp_beginner_lover_mode", False)))
        finally:
            self._syncing_voice_lang = False
        self.use_custom_persona_var.set(bool(self.cfg.get("use_custom_persona", False)))
        if hasattr(self, "txt_persona") and self.txt_persona is not None:
            self.txt_persona.delete("1.0", tk.END)
            self.txt_persona.insert("1.0", str(self.cfg.get("custom_persona_prompt", "")))

        # Tab 1: Credentials, Fish MCP, Colab S2-Pro, AI engine
        self._show_fish_key = False
        self._show_gem_key = False
        self._show_claude_key = False
        self._show_openai_key = False
        self._colab_key_shown = False
        if hasattr(self, "ent_fish_key"):
            self.ent_fish_key.config(show="*")
        if hasattr(self, "ent_gem_key"):
            self.ent_gem_key.config(show="*")
        if hasattr(self, "ent_claude_key"):
            self.ent_claude_key.config(show="*")
        if hasattr(self, "ent_openai_key"):
            self.ent_openai_key.config(show="*")
        if hasattr(self, "ent_colab_s2pro_key"):
            self.ent_colab_s2pro_key.configure(show="•")

        self.fish_key_var.set(str(self.cfg.get("fish_api_key", "") or ""))
        self.fish_mcp_fallback_var.set(bool(self.cfg.get("fish_mcp_fallback_enabled", True)))
        self.fish_mcp_primary_var.set(bool(self.cfg.get("fish_mcp_primary_mode", False)))
        self.fish_credit_saver_var.set(bool(self.cfg.get("fish_credit_saver_mode", True)))
        self.fish_tts_cache_var.set(bool(self.cfg.get("fish_tts_cache_enabled", True)))
        self.colab_s2pro_enabled_var.set(bool(self.cfg.get("colab_s2pro_enabled", False)))
        colab_url = str(self.cfg.get("colab_s2pro_url", "") or "").strip()
        self.colab_s2pro_url_var.set(colab_url)
        self.colab_s2pro_key_var.set(str(self.cfg.get("colab_s2pro_api_key", "") or ""))
        self.colab_s2pro_drive_folder_var.set(str(self.cfg.get("colab_s2pro_drive_folder_id", "") or ""))
        self.colab_s2pro_notebook_id_var.set(str(self.cfg.get("colab_s2pro_notebook_id", "") or ""))
        self.colab_s2pro_endpoint_file_var.set(str(self.cfg.get("colab_s2pro_endpoint_file", "") or ""))
        self.colab_s2pro_status_var.set(
            "☁️ 연결 대기 중 (Colab 서버 실행 후 '연결 정보 붙여넣기' 또는 'Drive 자동 가져오기' 클릭)"
            if not colab_url
            else f"☁️ 설정됨 ({colab_url[:42]})"
        )
        self.steam_game_dir_var.set(str(self.cfg.get("steam_game_dir", "") or ""))
        self._refresh_steam_dir_status_label()

        self.ai_provider_var.set(new_provider)
        new_backend = str(self.cfg.get("gemini_backend", "browser") or "browser").strip().lower()
        if new_backend not in ("browser", "api"):
            new_backend = "browser"
        self.gemini_backend_var.set(new_backend)
        self._last_backend_ui = new_backend
        self.gemini_key_var.set(str(self.cfg.get("gemini_api_key", "") or ""))
        self.claude_key_var.set(str(self.cfg.get("claude_api_key", "") or ""))
        self.openai_key_var.set(str(self.cfg.get("openai_api_key", "") or ""))
        self._apply_provider_step1_ui(new_provider)
        self._refresh_fish_mcp_ui_state()
        self._refresh_tts_cache_button_label()

        # Tab 2: Voice IDs & Directing (sync active character title & dual-voice slots first!)
        if hasattr(self, "lbl_tab0_char_title") and self.lbl_tab0_char_title is not None:
            try:
                self.lbl_tab0_char_title.configure(text=f"  {self._current_character_title()}")
            except Exception:
                pass
        if hasattr(self, "_card_voice_step2") and self._card_voice_step2 is not None:
            try:
                self._card_voice_step2.configure(
                    text=f" 2-A. 캐릭터별 Voice Reference ID 설정 — [{self._current_character_title()}] "
                )
            except Exception:
                pass
        self._rebuild_alt_voice_controls()
        auto_vis = str(self.cfg.get("auto_voice_visibility", "private") or "private").strip().lower()
        self.auto_voice_vis_var.set(AUTO_VOICE_VISIBILITY_LABELS.get(auto_vis, AUTO_VOICE_VISIBILITY_LABELS["private"]))
        auto_prof = str(self.cfg.get("auto_voice_quality_profile", "plus200") or "plus200").strip().lower()
        self.auto_voice_prof_var.set(AUTO_VOICE_PROFILE_LABELS.get(auto_prof, AUTO_VOICE_PROFILE_LABELS["plus200"]))
        self._load_voice_vars_for_lang(rlang)
        hdr = str(self.cfg.get("fish_model_header", "s2.1-pro-free") or "s2.1-pro-free")
        if hdr not in FISH_MODEL_HEADERS:
            hdr = "s2.1-pro-free"
        self.fish_model_hdr_var.set(hdr)
        smode = str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive")
        self.speed_mode_var.set(SPEED_MODE_LABELS.get(smode, SPEED_MODE_LABELS["expressive"]))
        self.auto_emotion_var.set(bool(self.cfg.get("auto_emotion_directing", True)))
        sboost = self._safe_float(self.cfg.get("expression_boost_offset", 0.0), 0.0, -0.25, 0.25)
        self.boost_preset_var.set(BOOST_PRESET_LABELS[min(BOOST_PRESET_LABELS.keys(), key=lambda k: abs(k - sboost))])
        vol = self._safe_float(self.cfg.get("output_volume", 1.0), 1.0, 0.10, 2.00)
        self.vol_var.set(vol)
        if hasattr(self, "lbl_vol_pct") and self.lbl_vol_pct is not None:
            self.lbl_vol_pct.configure(text=f"{int(round(vol * 100))}%")

        # Tab 3: Call mode, Devices, Models, Subtitles, Proactive timers
        cbm = str(self.cfg.get("call_bridge_mode", "mode1") or "mode1").strip()
        if cbm not in ("mode1", "mode2"):
            cbm = "mode2" if str(self.cfg.get("bridge_mode", "pc_mic")) == "gemini_live_audio" else "mode1"
        self.call_bridge_mode_var.set(cbm)
        self.primary_out_var.set(str(self.cfg.get("primary_output_device", "") or ""))
        reloaded_mic = str(self.cfg.get("mic_input_device", "") or "").strip()
        if reloaded_mic.startswith("(") or is_virtual_loopback_mic_label(reloaded_mic):
            reloaded_mic = ""
        self.mic_input_var.set(reloaded_mic)
        self.proactive_trig_dev_var.set(str(self.cfg.get("proactive_trigger_device", "") or ""))

        slang = str(self.cfg.get("stt_language", "auto") or "auto")
        self.stt_lang_var.set(STT_LANG_LABELS.get(slang, STT_LANG_LABELS["auto"]))
        wm_code = str(self.cfg.get("whisper_model", "auto") or "auto").strip().lower()
        if wm_code == "turbo":
            wm_code = "large-v3-turbo"
        self.whisper_model_var.set(WHISPER_MODEL_LABELS.get(wm_code, WHISPER_MODEL_LABELS["auto"]))
        cur_vad = self._safe_float(self.cfg.get("mic_vad_rms_threshold", 0.010), 0.010, 0.001, 0.25)
        self.vad_preset_var.set(min(VAD_PRESETS.keys(), key=lambda k: abs(VAD_PRESETS[k] - cur_vad)))

        if hasattr(self, "lbl_step3_provider"):
            self.lbl_step3_provider.configure(
                text=f"현재 선택된 AI 엔진: {AI_PROVIDER_LABELS[new_provider]}  (1단계 탭 또는 메인 상단에서 즉시 변경 가능)"
            )
        cur_bmodel = self._provider_browser_model_codes[new_provider]
        def_b = get_provider_default_browser_model(new_provider)
        if hasattr(self, "cmb_browser_model"):
            self.cmb_browser_model.configure(values=[lbl for _, lbl in self._all_browser_models])
        self.gemini_browser_model_var.set(
            self._browser_lbl_by_code.get(
                cur_bmodel,
                self._browser_lbl_by_code.get(def_b, self._all_browser_models[0][1] if self._all_browser_models else ""),
            )
        )
        self.gemini_thinking_var.set(GEMINI_THINKING_LEVEL_LABELS.get(cur_tlevel, GEMINI_THINKING_LEVEL_LABELS["low"]))
        if hasattr(self, "cmb_api_model"):
            self.cmb_api_model.configure(values=self._build_api_model_dropdown_values(new_provider))
        self.gemini_model_var.set(self._provider_api_model_selected[new_provider])

        cur_sub_eng = str(self.cfg.get("subtitle_engine", "auto_inline") or "auto_inline")
        if hasattr(self, "cmb_sub_engine"):
            self.cmb_sub_engine.configure(values=[lbl for _, lbl in self._all_sub_choices])
        self.subtitle_engine_var.set(resolve_subtitle_engine_label(cur_sub_eng, self._all_sub_choices))
        cur_sub_tlevel = str(self.cfg.get("subtitle_thinking_level", "low") or "low")
        if cur_sub_tlevel not in GEMINI_THINKING_LEVEL_LABELS:
            cur_sub_tlevel = "low"
        self.subtitle_thinking_var.set(GEMINI_THINKING_LEVEL_LABELS[cur_sub_tlevel])
        self._sync_sub_thinking_state()

        self.companion_proactive_var.set(bool(self.cfg.get("companion_proactive_enable", True)))
        lo = self._safe_float(self.cfg.get("idle_ping_min_sec", 45.0), 45.0, 5.0, 3600.0)
        hi = self._safe_float(self.cfg.get("idle_ping_max_sec", 75.0), max(lo, 75.0), lo, 7200.0)
        afk_n = self._safe_int(self.cfg.get("afk_max_consecutive_pings", 2), 2, 1, 99)
        self.min_idle_var.set(str(int(round(lo))))
        self.max_idle_var.set(str(int(round(hi))))
        self.afk_count_var.set(str(afk_n))

        self.proactive_enable_var.set(bool(self.cfg.get("proactive_enable", True)))
        ptime = self._safe_float(self.cfg.get("proactive_timeout_sec", 18.0), 18.0, 3.0, 300.0)
        self.proactive_timeout_var.set(PROACTIVE_TIMEOUT_LABELS[min(PROACTIVE_TIMEOUT_LABELS.keys(), key=lambda k: abs(k - ptime))])
        pvol = self._safe_float(self.cfg.get("proactive_trigger_volume", 1.0), 1.0, 0.1, 2.0)
        self.proactive_vol_var.set(PROACTIVE_VOL_LABELS[min(PROACTIVE_VOL_LABELS.keys(), key=lambda k: abs(k - pvol))])
        self._refresh_all_tab_scrollers(defer=True)

    def _reset_browser_profile_clicked(self) -> None:
        """Confirm and clear ONLY `%LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile`."""
        confirm_msg = (
            "AI 전용 브라우저의 로그인 쿠키·캐시(BrowserProfile)만 깨끗하게 초기화합니다.\n"
            "Cloudflare 무한 확인, 로그인 만료/계정 꼬임, 브라우저 프로필 잠금 오류 해결에 유용합니다.\n\n"
            "【유지되는 항목】\n"
            "• ✅ Fish Audio API 키 및 모든 앱 설정 (config.json)\n"
            "• ✅ 대화 기억 노트 및 선생님 프로필 (memory)\n\n"
            "【초기화되는 항목】\n"
            "• 🌐 AI 전용 브라우저 로그인 세션·쿠키·캐시 (Gemini · Claude · ChatGPT)\n\n"
            "초기화 후 다음 통화를 시작할 때 새 브라우저 창에서 다시 로그인하면 됩니다.\n"
            "정말 브라우저 프로필을 초기화하시겠습니까?"
        )
        if not messagebox.askyesno("🌐 브라우저 프로필 초기화", confirm_msg, parent=self):
            return
        if not self.winfo_exists():
            return

        try:
            if self.on_reset_browser_profile is not None:
                ok, msg = self.on_reset_browser_profile()
            else:
                ok, msg = reset_browser_profile()
        except Exception as exc:
            ok, msg = False, str(exc)

        safe_msg = redact_secrets(str(msg or ""), self._current_secrets())
        if ok:
            messagebox.showinfo("🌐 브라우저 초기화 완료", safe_msg, parent=self)
        else:
            messagebox.showwarning("🌐 브라우저 초기화 실패", safe_msg, parent=self)

    def _reset_to_defaults_clicked(self) -> None:
        """Confirm and restore all settings to safe defaults while preserving Fish Audio API key & AI browser logins."""
        confirm_msg = (
            "사용자 설정 오류나 버그가 발생한 경우 모든 앱 설정을 초기 기본값으로 복원합니다.\n\n"
            "【유지되는 항목】\n"
            "• ✅ Fish Audio API 키\n"
            "• ✅ AI 전용 브라우저 로그인 세션 (Gemini·Claude·ChatGPT)\n"
            "• ✅ 대화 기억 노트 및 선생님 프로필 (memory)\n\n"
            "【기본값으로 초기화되는 항목】\n"
            "• 🔁 AI 엔진(제미니 웹 기본)·모델·추론·자막·프롬프트·보이스 링크·오디오 장치·음량·선톡 타이머\n"
            "• 🔁 기타 API 키 (Gemini/Claude/OpenAI API 키, Fish MCP OAuth, Colab S2-Pro 토큰)\n"
            "• 🔁 사용자 selectors.json 편집본 및 실시간 RVC 설정\n\n"
            "💾 복원 전 현재 설정은 로컬 backups 폴더에 자동 백업됩니다.\n"
            "정말 기본값으로 복원하시겠습니까?"
        )
        if not messagebox.askyesno("🔄 설정 기본값 복원", confirm_msg, parent=self):
            return
        if not self.winfo_exists():
            return

        if getattr(self, "_fish_mcp_login_in_progress", False):
            self._fish_mcp_cancel_login()

        live_snapshot = deepcopy(self.cfg)
        typed_fish_key = _sanitize_single_line_secret(self.fish_key_var.get())
        if typed_fish_key:
            live_snapshot["fish_api_key"] = typed_fish_key

        try:
            result = reset_settings_to_defaults(
                current_cfg=live_snapshot,
                reason="manual_reset",
                preserve_fish_api_key=True,
                reset_rvc_config=True,
                reset_user_selectors=True,
            )
        except Exception as exc:
            messagebox.showerror(
                "기본값 복원 실패",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )
            return

        if self.on_reset_defaults is not None:
            self.on_reset_defaults(result)
        else:
            self.on_saved(deepcopy(result.config))
        if not self.winfo_exists():
            return
        self._reload_from_cfg(result.config)

        # Re-sync refreshed default audio devices if live app repopulated them
        if self.app is not None:
            try:
                if hasattr(self.app, "out_choice_var"):
                    v_out = self.app.out_choice_var.get().strip()
                    if v_out and not v_out.startswith("("):
                        self.primary_out_var.set(v_out)
                if hasattr(self.app, "mic_choice_var"):
                    v_mic = self.app.mic_choice_var.get().strip()
                    if v_mic and not v_mic.startswith("(") and not is_virtual_loopback_mic_label(v_mic):
                        self.mic_input_var.set(v_mic)
                if hasattr(self.app, "trigger_out_choice_var"):
                    v_trig = self.app.trigger_out_choice_var.get().strip()
                    if v_trig and not v_trig.startswith("("):
                        self.proactive_trig_dev_var.set(v_trig)
            except Exception:
                pass
        self._refresh_fish_mcp_ui_state()
        self._refresh_tts_cache_button_label()

        fish_status = "유지됨" if result.fish_api_key_preserved else "저장된 키 없음"
        browser_status = (
            "유지됨 (BrowserProfile 보존)"
            if result.browser_profile_exists
            else "유지됨 (기본 웹 로그인 모드)"
        )
        lines = [
            "설정을 기본값으로 안전하게 복원했습니다.",
            "",
            f"• Fish Audio API 키: {fish_status}",
            f"• AI 전용 브라우저 로그인 세션 (Gemini·Claude·ChatGPT): {browser_status}",
        ]
        if result.backup_dir is not None:
            lines.append(f"• 이전 설정 백업: {result.backup_dir.name}")
        if result.warnings:
            lines.append("")
            lines.append("⚠️ 참고 사항:")
            for w in result.warnings:
                lines.append(f"  - {redact_secrets(w, self._current_secrets())}")
        messagebox.showinfo("기본값 복원 완료", "\n".join(lines), parent=self)

    def _save_and_close(self) -> None:
        try:
            raw_min = float(self.min_idle_var.get().strip())
            raw_max = float(self.max_idle_var.get().strip())
            raw_afk = int(self.afk_count_var.get().strip())
        except ValueError:
            messagebox.showerror("입력 오류", "선톡 무음 대기 시간과 AFK 횟수는 숫자로 입력해 주세요.", parent=self)
            return
        if not (math.isfinite(raw_min) and math.isfinite(raw_max)):
            messagebox.showerror("입력 오류", "선톡 무음 대기 시간은 유효한 유한 숫자(5~7200초)로 입력해 주세요.", parent=self)
            return
        min_s = max(5.0, min(3600.0, raw_min))
        max_s = max(min_s, min(7200.0, raw_max))
        afk_n = max(1, min(99, raw_afk))

        # Ensure Tab 2 voice slots reflect the active reply language BEFORE validating & copying self.cfg
        rlang = "ko" if self.reply_language_var.get() == "ko" else "ja"
        if self._voice_editor_lang != rlang:
            self._on_reply_language_var_written()

        fish_key = _sanitize_single_line_secret(self.fish_key_var.get())
        raw_ref_kr_input = self.ref_id_kr_var.get().strip()
        ref_id_kr = _clean_ref_id(raw_ref_kr_input)
        if raw_ref_kr_input and not ref_id_kr:
            messagebox.showerror(
                "유효하지 않은 KR Voice Reference ID",
                "KR Voice Reference ID 칸에 올바른 Fish Audio 보이스 URL 또는 영문/숫자 ID를 입력하거나 비워 주세요.",
                parent=self,
            )
            return
        raw_ref_jp_input = self.ref_id_jp_var.get().strip()
        ref_id_jp = _clean_ref_id(raw_ref_jp_input)
        if raw_ref_jp_input and not ref_id_jp:
            messagebox.showerror(
                "유효하지 않은 JP Voice Reference ID",
                "JP Voice Reference ID 칸에 올바른 Fish Audio 보이스 URL 또는 영문/숫자 ID를 입력하거나 비워 주세요.",
                parent=self,
            )
            return
        cleaned_alt_voices_kr: dict[str, str] = {}
        cleaned_alt_voices_jp: dict[str, str] = {}
        for _ak, slot_id, lbl_kr in self._alt_voice_specs:
            raw_alt_kr = self._alt_voice_kr_vars[slot_id].get().strip()
            alt_clean_kr = _clean_ref_id(raw_alt_kr)
            if raw_alt_kr and not alt_clean_kr:
                messagebox.showerror(
                    "유효하지 않은 보조 보이스 ID (KR)",
                    f"보조 보이스 [{lbl_kr}] (KR) 칸에 올바른 Fish Audio 보이스 URL 또는 영문/숫자 ID를 입력하거나 비워 주세요.",
                    parent=self,
                )
                return
            cleaned_alt_voices_kr[slot_id] = alt_clean_kr

            raw_alt_jp = self._alt_voice_jp_vars[slot_id].get().strip()
            alt_clean_jp = _clean_ref_id(raw_alt_jp)
            if raw_alt_jp and not alt_clean_jp:
                messagebox.showerror(
                    "유효하지 않은 보조 보이스 ID (JP)",
                    f"보조 보이스 [{lbl_kr}] (JP) 칸에 올바른 Fish Audio 보이스 URL 또는 영문/숫자 ID를 입력하거나 비워 주세요.",
                    parent=self,
                )
                return
            cleaned_alt_voices_jp[slot_id] = alt_clean_jp
        ai_provider = normalize_ai_provider(self.ai_provider_var.get())
        raw_backend = (self.gemini_backend_var.get() or "browser").strip().lower()
        gem_backend = "api" if raw_backend == "api" else "browser"
        gem_key = _sanitize_single_line_secret(self.gemini_key_var.get())
        claude_key = _sanitize_single_line_secret(self.claude_key_var.get())
        openai_key = _sanitize_single_line_secret(self.openai_key_var.get())

        active_key = {"gemini": gem_key, "claude": claude_key, "chatgpt": openai_key}[ai_provider]
        active_api_name = {"gemini": "Gemini", "claude": "Claude", "chatgpt": "OpenAI(ChatGPT)"}[ai_provider]

        if gem_backend == "api" and not active_key:
            messagebox.showwarning(
                f"{active_api_name} API Key 필요",
                f"공식 {active_api_name} API 모드를 선택하셨습니다. {active_api_name} API Key를 입력하거나 전용 브라우저 모드를 선택해 주세요.",
                parent=self,
            )
            return

        self._stash_active_provider_tab3_choices()
        provider_defaults = {
            "gemini": "gemini-3.5-flash-lite",
            "claude": "claude-sonnet-5-5",
            "chatgpt": "gpt-5.4-mini",
        }
        resolved_api_models: dict[str, str] = {}
        try:
            for p_code, p_default in provider_defaults.items():
                raw_model = self._provider_api_model_selected.get(p_code, p_default)
                if gem_backend == "api" and p_code == ai_provider:
                    resolved_api_models[p_code] = _validate_api_model_name(
                        raw_model,
                        default=p_default,
                        provider=p_code,
                    )
                else:
                    resolved_api_models[p_code] = coerce_safe_api_model_id(
                        raw_model,
                        provider=p_code,
                        default=p_default,
                    )
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc), parent=self)
            return
        gem_api_model = resolved_api_models["gemini"]
        claude_api_model = resolved_api_models["claude"]
        openai_api_model = resolved_api_models["chatgpt"]

        colab_url_raw = self.colab_s2pro_url_var.get().strip()
        colab_enabled = bool(self.colab_s2pro_enabled_var.get())
        if colab_url_raw:
            try:
                colab_url_raw = validate_colab_server_url(colab_url_raw, resolve_dns=False)
            except ValueError as exc:
                if colab_enabled:
                    messagebox.showwarning("Colab S2-Pro 서버 주소 오류", str(exc), parent=self)
                    return
                colab_url_raw = ""
                self.colab_s2pro_url_var.set("")

        raw_drive_folder = self.colab_s2pro_drive_folder_var.get().strip()
        clean_drive_folder = extract_google_drive_id(raw_drive_folder, kind="folder")
        if raw_drive_folder and not clean_drive_folder:
            messagebox.showwarning(
                "Drive 폴더 ID 오류",
                "Google Drive 폴더 ID 형식이 올바르지 않습니다.\n"
                "8~128자의 영문·숫자·`_`·`-` ID 또는 공식 폴더 주소(https://drive.google.com/drive/folders/...)를 입력해 주세요.",
                parent=self,
            )
            return
        if clean_drive_folder and clean_drive_folder != raw_drive_folder:
            self.colab_s2pro_drive_folder_var.set(clean_drive_folder)

        raw_notebook_id = self.colab_s2pro_notebook_id_var.get().strip()
        clean_notebook_id = extract_google_drive_id(raw_notebook_id, kind="notebook")
        if raw_notebook_id and not clean_notebook_id:
            messagebox.showwarning(
                "Colab 노트북 ID 오류",
                "Google Colab 노트북 ID 형식이 올바르지 않습니다.\n"
                "8~128자의 영문·숫자·`_`·`-` ID 또는 공식 노트북 주소(https://colab.research.google.com/drive/...)를 입력해 주세요.",
                parent=self,
            )
            return
        if clean_notebook_id and clean_notebook_id != raw_notebook_id:
            self.colab_s2pro_notebook_id_var.set(clean_notebook_id)

        # Build an isolated candidate config so failed disk saves never mutate live state
        save_cfg = deepcopy(self.cfg)
        if self.app is not None and isinstance(getattr(self.app, "cfg", None), dict):
            app_cfg = self.app.cfg
            # Merge live non-dialog keys & concurrent voice links added while SettingsDialog was open
            for live_key in (
                "selected_momotalk_entry_id",
                "temporary_call_mode",
                "subtitle_mode",
                "subtitle_font_size",
                "fish_mcp_oauth_data",
            ):
                if live_key in app_cfg:
                    save_cfg[live_key] = deepcopy(app_cfg[live_key])
            for v_bucket in ("saved_voices", "saved_voices_kr"):
                app_vmap = app_cfg.get(v_bucket)
                if isinstance(app_vmap, dict):
                    merged_vmap = dict(app_vmap)
                    dlg_vmap = save_cfg.get(v_bucket)
                    if isinstance(dlg_vmap, dict):
                        merged_vmap.update(dlg_vmap)
                    for del_bucket, del_slot in self._deleted_voice_slots:
                        if del_bucket == v_bucket:
                            merged_vmap.pop(del_slot, None)
                    save_cfg[v_bucket] = merged_vmap

        # Tab 0: Prompt interpretation & custom persona
        save_cfg["persona_prompt_variant"] = PERSONA_LABEL_TO_CODE.get(
            self.persona_variant_var.get().strip(), "official"
        )
        save_cfg["proactive_prompt_append"] = bool(self.proactive_prompt_var.get())
        save_cfg["reply_language"] = rlang
        save_cfg["jp_beginner_lover_mode"] = False if rlang == "ko" else bool(self.jp_beginner_coach_var.get())
        save_cfg["use_custom_persona"] = bool(self.use_custom_persona_var.get())
        save_cfg["custom_persona_prompt"] = self.txt_persona.get("1.0", tk.END).strip()

        # Tab 1 & 2: Fish Audio, MCP web credits, Colab S2-Pro, Credit-Saver, Disk Cache, Steam dir & directing
        save_cfg["fish_api_key"] = fish_key
        save_cfg["fish_mcp_fallback_enabled"] = bool(self.fish_mcp_fallback_var.get())
        save_cfg["fish_mcp_primary_mode"] = bool(self.fish_mcp_primary_var.get())
        save_cfg["fish_credit_saver_mode"] = bool(self.fish_credit_saver_var.get())
        save_cfg["fish_tts_cache_enabled"] = bool(self.fish_tts_cache_var.get())
        save_cfg["colab_s2pro_enabled"] = colab_enabled and bool(colab_url_raw)
        save_cfg["colab_s2pro_url"] = colab_url_raw
        save_cfg["colab_s2pro_api_key"] = _sanitize_single_line_secret(self.colab_s2pro_key_var.get())
        save_cfg["colab_s2pro_drive_folder_id"] = clean_drive_folder
        save_cfg["colab_s2pro_notebook_id"] = clean_notebook_id
        save_cfg["colab_s2pro_endpoint_file"] = self.colab_s2pro_endpoint_file_var.get().strip()
        save_cfg["steam_game_dir"] = self.steam_game_dir_var.get().strip()
        save_cfg["auto_voice_visibility"] = AUTO_VOICE_VIS_LABEL_TO_CODE.get(
            self.auto_voice_vis_var.get().strip(), "private"
        )
        save_cfg["auto_voice_quality_profile"] = AUTO_VOICE_PROF_LABEL_TO_CODE.get(
            self.auto_voice_prof_var.get().strip(), "plus200"
        )
        save_cfg["fish_reference_id"] = ref_id_kr if rlang == "ko" else ref_id_jp
        cid = self._current_character_id()
        for voice_bucket, main_ref, alt_map in (
            ("saved_voices_kr", ref_id_kr, cleaned_alt_voices_kr),
            ("saved_voices", ref_id_jp, cleaned_alt_voices_jp),
        ):
            if not isinstance(save_cfg.get(voice_bucket), dict):
                save_cfg[voice_bucket] = {}
            if main_ref:
                save_cfg[voice_bucket][cid] = main_ref
            elif cid:
                save_cfg[voice_bucket].pop(cid, None)
            for _ak, slot_id, _lbl in self._alt_voice_specs:
                alt_clean = alt_map.get(slot_id, "")
                if alt_clean:
                    save_cfg[voice_bucket][slot_id] = alt_clean
                else:
                    save_cfg[voice_bucket].pop(slot_id, None)
        raw_hdr = self.fish_model_hdr_var.get().strip()
        save_cfg["fish_model_header"] = raw_hdr if raw_hdr in FISH_MODEL_HEADERS else "s2.1-pro-free"
        save_cfg["streaming_speed_mode"] = SPEED_LABEL_TO_CODE.get(
            self.speed_mode_var.get().strip(), "expressive"
        )
        save_cfg["auto_emotion_directing"] = bool(self.auto_emotion_var.get())
        save_cfg["expression_boost_offset"] = float(
            BOOST_LABEL_TO_VAL.get(self.boost_preset_var.get().strip(), 0.0)
        )
        save_cfg["output_volume"] = self._safe_float(self.vol_var.get(), 1.0, 0.10, 2.00)

        # Tab 1 & 3: Multi-provider AI (Gemini / Claude / ChatGPT), subtitles, bridge mode, devices, proactive timers
        think_by_lbl = {lbl: code for code, lbl in GEMINI_THINKING_LEVELS}
        sub_eng_by_lbl = {lbl: code for code, lbl in self._all_sub_choices}

        save_cfg["ai_provider"] = ai_provider
        save_cfg["gemini_backend"] = gem_backend
        save_cfg["gemini_api_key"] = gem_key
        save_cfg["gemini_api_model"] = gem_api_model
        save_cfg["gemini_browser_model"] = self._provider_browser_model_codes.get("gemini", "hydrogen") or "hydrogen"
        save_cfg["claude_api_key"] = claude_key
        save_cfg["claude_api_model"] = claude_api_model
        save_cfg["claude_browser_model"] = self._provider_browser_model_codes.get("claude", "default") or "default"
        save_cfg["openai_api_key"] = openai_key
        save_cfg["openai_api_model"] = openai_api_model
        save_cfg["chatgpt_browser_model"] = self._provider_browser_model_codes.get("chatgpt", "default") or "default"
        save_cfg["gemini_thinking_level"] = think_by_lbl.get(self.gemini_thinking_var.get().strip(), "low")
        save_cfg["subtitle_engine"] = sub_eng_by_lbl.get(self.subtitle_engine_var.get().strip(), "auto_inline")
        save_cfg["subtitle_thinking_level"] = think_by_lbl.get(self.subtitle_thinking_var.get().strip(), "low")
        save_cfg["api_models"] = list(self._provider_extra_api_models.get("gemini") or [])
        save_cfg["claude_api_models"] = list(self._provider_extra_api_models.get("claude") or [])
        save_cfg["openai_api_models"] = list(self._provider_extra_api_models.get("chatgpt") or [])
        save_cfg["web_models"] = [[c, l] for c, l in (self._provider_account_web_models.get("gemini") or [])]
        save_cfg["claude_web_models"] = [[c, l] for c, l in (self._provider_account_web_models.get("claude") or [])]
        save_cfg["chatgpt_web_models"] = [[c, l] for c, l in (self._provider_account_web_models.get("chatgpt") or [])]
        save_cfg["web_models_schema_version"] = 2

        cbm = "mode2" if self.call_bridge_mode_var.get().strip() == "mode2" else "mode1"
        save_cfg["call_bridge_mode"] = cbm
        save_cfg["bridge_mode"] = "gemini_live_audio" if cbm == "mode2" else "pc_mic"

        for dev_var, dev_key in (
            (self.primary_out_var, "primary_output_device"),
            (self.mic_input_var, "mic_input_device"),
            (self.proactive_trig_dev_var, "proactive_trigger_device"),
        ):
            dev_val = dev_var.get().strip()
            if dev_val.startswith("(") or (dev_key == "mic_input_device" and is_virtual_loopback_mic_label(dev_val)):
                save_cfg[dev_key] = ""
            elif dev_val:
                save_cfg[dev_key] = dev_val
            elif self.app is not None:
                save_cfg[dev_key] = ""

        if self.app is not None and hasattr(self.app, "in_choice_var") and self.vb_in_var.get().strip():
            self.app.in_choice_var.set(self.vb_in_var.get().strip())

        st_lang = STT_LABEL_TO_CODE.get(self.stt_lang_var.get().strip(), "auto")
        save_cfg["stt_language"] = st_lang
        coach_active = bool(save_cfg.get("jp_beginner_lover_mode", False)) and rlang == "ja"
        save_cfg["mic_stt_language"] = (
            st_lang if st_lang in ("ja", "ko") else ("ja_beginner_bi" if coach_active else "ko_auto")
        )
        save_cfg["whisper_model"] = WHISPER_LABEL_TO_CODE.get(self.whisper_model_var.get().strip(), "auto")
        save_cfg["mic_vad_rms_threshold"] = float(VAD_PRESETS.get(self.vad_preset_var.get(), 0.010))

        save_cfg["companion_proactive_enable"] = bool(self.companion_proactive_var.get())
        save_cfg["idle_ping_min_sec"] = min_s
        save_cfg["idle_ping_max_sec"] = max_s
        save_cfg["afk_max_consecutive_pings"] = afk_n

        save_cfg["proactive_enable"] = bool(self.proactive_enable_var.get())
        save_cfg["proactive_timeout_sec"] = float(
            PROACTIVE_TIMEOUT_VALS.get(self.proactive_timeout_var.get().strip(), 18.0)
        )
        save_cfg["proactive_trigger_volume"] = float(
            PROACTIVE_VOL_VALS.get(self.proactive_vol_var.get().strip(), 1.0)
        )

        sv_to_save: dict[str, Any] | None = None
        if self._sv_available and self._sv_cfg is not None:
            try:
                raw_sv_thresh = float(self.sv_threshold_var.get().strip())
                raw_sv_cool = float(self.sv_cooldown_var.get().strip())
                raw_sv_stuck = float(self.sv_stuck_sec_var.get().strip())
            except ValueError:
                messagebox.showerror(
                    "입력 오류",
                    "화면 공유 민감도, 쿨다운, 정지 감지 시간은 숫자로 입력해 주세요.",
                    parent=self,
                )
                return
            if not (math.isfinite(raw_sv_thresh) and math.isfinite(raw_sv_cool) and math.isfinite(raw_sv_stuck)):
                messagebox.showerror(
                    "입력 오류",
                    "화면 공유 숫자 설정은 유효한 유한 숫자로 입력해 주세요.",
                    parent=self,
                )
                return
            from src.screen_vision.config import sanitize_screen_vision_config, save_screen_vision_config

            cand_sv = deepcopy(self._sv_cfg)
            if self.app is not None and isinstance(getattr(self.app, "_sv_cfg", None), dict):
                live_sv = self.app._sv_cfg
                for live_k in (
                    "target_kind",
                    "target_monitor_index",
                    "target_window_title",
                    "target_window_process",
                    "mini_overlay_enabled",
                    "mini_overlay_x",
                    "mini_overlay_y",
                    "mini_overlay_alpha",
                ):
                    if live_k in live_sv:
                        cand_sv[live_k] = deepcopy(live_sv[live_k])
            cand_sv["mode"] = "econ_study" if self.sv_mode_var.get().strip() == "econ_study" else "active_chat"
            cand_sv["active_chat_auto_react"] = bool(self.sv_auto_react_var.get())
            cand_sv["active_chat_change_threshold"] = max(3.0, min(45.0, raw_sv_thresh))
            cand_sv["active_chat_cooldown_sec"] = max(5.0, min(120.0, raw_sv_cool))
            cand_sv["study_stuck_enabled"] = bool(self.sv_stuck_enabled_var.get())
            cand_sv["study_stuck_sec"] = max(45.0, min(900.0, raw_sv_stuck))
            cand_sv["study_board_auto_open"] = bool(self.sv_board_auto_open_var.get())
            cand_sv["dedup_identical_turns"] = bool(self.sv_dedup_var.get())
            cand_sv["privacy_auto_pause"] = bool(self.sv_privacy_auto_pause_var.get())
            cand_sv["privacy_denylist"] = list(self._sv_denylist)
            cand_sv["hotkeys_enabled"] = bool(self.sv_hotkeys_enabled_var.get())
            sv_to_save = sanitize_screen_vision_config(cand_sv)

        try:
            save_user_config(save_cfg)
        except Exception as exc:
            messagebox.showerror(
                "설정 저장 실패",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )
            return

        if sv_to_save is not None:
            try:
                if not save_screen_vision_config(sv_to_save):
                    raise OSError("screen_vision_config.json 파일을 저장하지 못했습니다.")
                self._sv_cfg = deepcopy(sv_to_save)
            except Exception as exc:
                messagebox.showerror(
                    "화면 공유 설정 저장 실패",
                    redact_secrets(str(exc), self._current_secrets()),
                    parent=self,
                )
                return

        self.cfg = save_cfg
        self.on_saved(self.cfg)
        if sv_to_save is not None:
            if self.on_screen_vision_saved is not None:
                try:
                    self.on_screen_vision_saved(deepcopy(sv_to_save))
                except Exception:
                    pass
            elif self.app is not None and hasattr(self.app, "apply_screen_vision_config"):
                try:
                    self.app.apply_screen_vision_config(deepcopy(sv_to_save))
                except Exception:
                    pass
        self.destroy()
