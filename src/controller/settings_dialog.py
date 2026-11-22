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

import os
import re
import subprocess
import tkinter as tk
import urllib.parse
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Callable

import pyperclip

from src.controller.gemini_connector import (
    GEMINI_API_MODELS,
    GEMINI_BROWSER_MODELS,
    GEMINI_THINKING_LEVEL_LABELS,
    GEMINI_THINKING_LEVELS,
    OfficialGeminiApiBackend,
    split_legacy_browser_model,
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

# Shared label/value maps (also imported by aru_fish_bridge as single source of truth)
STT_LANG_LABELS: dict[str, str] = {
    "auto": "자동 감지 (일본어·한국어 초고속 판별)",
    "ja": "일본어 전용 (블루아카 일본어 대본용 · 최속)",
    "ko": "한국어 전용",
}
STT_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in STT_LANG_LABELS.items()}

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
    "pro85": "💎 고품질 85초 (최대 14클립 · 추천)",
    "standard45": "⚡ 표준 45초 (최대 8클립 · 빠른 생성)",
}
AUTO_VOICE_PROF_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in AUTO_VOICE_PROFILE_LABELS.items()}

VAD_PRESETS: dict[str, float] = {
    "민감 (조용한 방 · RMS 0.007)": 0.007,
    "표준 (권장 · RMS 0.012)": 0.012,
    "둔감 (스피커/주변소음 · RMS 0.022)": 0.022,
}


_FISH_URL_RE = re.compile(r"(?:https://(?:www\.)?)?fish\.audio/(?:[a-zA-Z_-]+/)?m/([a-zA-Z0-9_-]{8,64})")
_FISH_HEX_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")
_SAFE_REF_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_API_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MAX_KEY_PASTE_LEN = 512

ALLOWED_EXTERNAL_URLS: frozenset[str] = frozenset(
    {
        "https://fish.audio/app/api-keys/",
        "https://fish.audio/discovery/",
        "https://aistudio.google.com/apikey",
    }
)
ALLOWED_EXTERNAL_HOSTS: frozenset[str] = frozenset(
    {
        "fish.audio",
        "aistudio.google.com",
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


def _validate_api_model_name(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return "gemini-3.5-flash-lite"
    if ".." in s or not _SAFE_API_MODEL_RE.fullmatch(s):
        raise ValueError("유효하지 않은 Gemini API 모델 이름입니다. 영문/숫자/점/하이픈만 사용해 주세요.")
    return s


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
    ) -> None:
        super().__init__(parent)
        self.cfg = dict(cfg)
        self.on_saved = on_saved
        self.first_run = first_run
        self.on_open_logs = on_open_logs
        self._on_sync_web_models = on_sync_web_models
        self.app = app
        self._initial_tab = 1 if (first_run and int(initial_tab) == 0) else max(0, min(3, int(initial_tab)))
        self._ref_trace_token: str | None = None

        self._extra_api_models = [
            str(m).strip()
            for m in (extra_api_models if extra_api_models is not None else (self.cfg.get("api_models", []) or []))
            if str(m).strip()
        ]
        self._extra_web_models = list(extra_web_models or [])
        cfg_web_raw = self.cfg.get("web_models") or []
        cfg_web = [
            (str(item[0]).strip(), str(item[1]).strip())
            for item in cfg_web_raw
            if isinstance(item, (list, tuple)) and len(item) >= 2 and str(item[0]).strip() and str(item[1]).strip()
        ]
        self._account_web_models: list[tuple[str, str]] = list(web_models) if web_models else cfg_web
        base_web = list(self._account_web_models) if self._account_web_models else list(GEMINI_BROWSER_MODELS)
        base_web_codes = {c for c, _ in base_web}
        self._all_browser_models = base_web + [
            (c, l) for c, l in self._extra_web_models if c not in base_web_codes
        ]
        self._browser_lbl_by_code = dict(self._all_browser_models)
        self._all_sub_choices = get_subtitle_engine_choices(
            self._extra_api_models,
            self._extra_web_models,
            web_models=self._account_web_models or None,
        )

        title_prefix = "초기 설정 안내" if first_run else "MomoTalk 환경 설정"
        self.title(f"⚙️ {title_prefix} — 프롬프트 해석 · 1단계~3단계 · 개인정보 격리")
        self.geometry("860x760")
        self.minsize(780, 680)
        self.configure(bg="#F3F5F8")
        self.transient(parent)
        self.grab_set()

        self._show_fish_key = False
        self._show_gem_key = False

        # Persona / Prompt version vars
        saved_pvar = str(self.cfg.get("persona_prompt_variant", "official") or "official")
        self.persona_variant_var = tk.StringVar(
            value=PERSONA_VARIANT_LABELS.get(saved_pvar, PERSONA_VARIANT_LABELS["official"])
        )
        self.proactive_prompt_var = tk.BooleanVar(value=bool(self.cfg.get("proactive_prompt_append", True)))
        saved_rlang = "ko" if str(self.cfg.get("reply_language", "ja") or "ja").strip().lower() == "ko" else "ja"
        self.reply_language_var = tk.StringVar(value=saved_rlang)
        self.jp_beginner_coach_var = tk.BooleanVar(value=bool(self.cfg.get("jp_beginner_lover_mode", False)))
        self.use_custom_persona_var = tk.BooleanVar(value=bool(self.cfg.get("use_custom_persona", False)))

        # Step 1: API keys, Steam path & auth vars
        self.fish_key_var = tk.StringVar(value=str(self.cfg.get("fish_api_key", "")))
        self.steam_game_dir_var = tk.StringVar(value=str(self.cfg.get("steam_game_dir", "") or ""))
        self.gemini_backend_var = tk.StringVar(value=str(self.cfg.get("gemini_backend", "browser")))
        self.gemini_key_var = tk.StringVar(value=str(self.cfg.get("gemini_api_key", "")))

        # Step 2: Per-character Voice & Directing vars
        saved_auto_vis = str(self.cfg.get("auto_voice_visibility", "private") or "private").strip().lower()
        self.auto_voice_vis_var = tk.StringVar(
            value=AUTO_VOICE_VISIBILITY_LABELS.get(saved_auto_vis, AUTO_VOICE_VISIBILITY_LABELS["private"])
        )
        saved_auto_prof = str(self.cfg.get("auto_voice_quality_profile", "pro85") or "pro85").strip().lower()
        self.auto_voice_prof_var = tk.StringVar(
            value=AUTO_VOICE_PROFILE_LABELS.get(saved_auto_prof, AUTO_VOICE_PROFILE_LABELS["pro85"])
        )
        cid = self._current_character_id()
        saved_map = (self.cfg.get("saved_voices_kr") if saved_rlang == "ko" else self.cfg.get("saved_voices")) or {}
        init_ref = str(saved_map.get(cid) or ("" if saved_rlang == "ko" else self.cfg.get("fish_reference_id", "")) or "")
        self.ref_id_var = tk.StringVar(value=init_ref)
        # Dual-voice alt persona slots (e.g. Kisaki -> Kisaki_Kiki)
        self._alt_voice_specs: list[tuple[str, str, str]] = []  # (alt_key, slot_id, label_kr)
        self._alt_voice_vars: dict[str, tk.StringVar] = {}
        cur_ch = getattr(app, "current_char", None) if app is not None else None
        if isinstance(cur_ch, dict) and isinstance(cur_ch.get("alt_voice_personas"), dict):
            for ak, spec in cur_ch["alt_voice_personas"].items():
                if not isinstance(spec, dict):
                    continue
                slot_id = str(spec.get("voice_slot_id") or f"{cid}_{ak.capitalize()}").strip()
                lbl_kr = str(spec.get("label_kr") or ak).strip()
                self._alt_voice_specs.append((str(ak), slot_id, lbl_kr))
                self._alt_voice_vars[slot_id] = tk.StringVar(value=str(saved_map.get(slot_id) or ""))
        self.fish_model_hdr_var = tk.StringVar(value=str(self.cfg.get("fish_model_header", "s2.1-pro-free") or "s2.1-pro-free"))
        saved_speed = str(self.cfg.get("streaming_speed_mode", "expressive") or "expressive")
        self.speed_mode_var = tk.StringVar(
            value=SPEED_MODE_LABELS.get(saved_speed, SPEED_MODE_LABELS["expressive"])
        )
        self.auto_emotion_var = tk.BooleanVar(value=bool(self.cfg.get("auto_emotion_directing", True)))
        saved_boost = float(self.cfg.get("expression_boost_offset", 0.0) or 0.0)
        closest_boost = min(BOOST_PRESET_LABELS.keys(), key=lambda k: abs(k - saved_boost))
        self.boost_preset_var = tk.StringVar(value=BOOST_PRESET_LABELS[closest_boost])
        self.vol_var = tk.DoubleVar(value=float(self.cfg.get("output_volume", 1.0) or 1.0))

        # Step 3: Call mode, Devices, Models, Subtitles, Proactive timers
        saved_cbm = str(self.cfg.get("call_bridge_mode", "") or "").strip()
        if saved_cbm not in ("mode1", "mode2"):
            saved_cbm = "mode2" if str(self.cfg.get("bridge_mode", "pc_mic")) == "gemini_live_audio" else "mode1"
        self.call_bridge_mode_var = tk.StringVar(value=saved_cbm)

        self.primary_out_var = tk.StringVar(
            value=(app.out_choice_var.get() if app is not None and hasattr(app, "out_choice_var") else str(self.cfg.get("primary_output_device", "")))
        )
        self.mic_input_var = tk.StringVar(
            value=(app.mic_choice_var.get() if app is not None and hasattr(app, "mic_choice_var") else str(self.cfg.get("mic_input_device", "")))
        )
        self.vb_in_var = tk.StringVar(
            value=(app.in_choice_var.get() if app is not None and hasattr(app, "in_choice_var") else "")
        )
        self.proactive_trig_dev_var = tk.StringVar(
            value=(
                app.trigger_out_choice_var.get()
                if app is not None and hasattr(app, "trigger_out_choice_var")
                else str(self.cfg.get("proactive_trigger_device", ""))
            )
        )

        saved_lang = str(self.cfg.get("stt_language", "auto") or "auto")
        self.stt_lang_var = tk.StringVar(value=STT_LANG_LABELS.get(saved_lang, STT_LANG_LABELS["auto"]))

        cur_vad = float(self.cfg.get("mic_vad_rms_threshold", 0.012) or 0.012)
        closest_vad_lbl = min(VAD_PRESETS.keys(), key=lambda k: abs(VAD_PRESETS[k] - cur_vad))
        self.vad_preset_var = tk.StringVar(value=closest_vad_lbl)

        self.gemini_model_var = tk.StringVar(value=str(self.cfg.get("gemini_api_model", "gemini-3.5-flash-lite")))
        cur_bmodel, cur_tlevel = split_legacy_browser_model(
            str(self.cfg.get("gemini_browser_model", "hydrogen")),
            str(self.cfg.get("gemini_thinking_level", "low")),
        )
        self.gemini_browser_model_var = tk.StringVar(
            value=self._browser_lbl_by_code.get(cur_bmodel, self._browser_lbl_by_code.get("hydrogen", ""))
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
        self.min_idle_var = tk.StringVar(value=str(int(round(float(self.cfg.get("idle_ping_min_sec", 45.0))))))
        self.max_idle_var = tk.StringVar(value=str(int(round(float(self.cfg.get("idle_ping_max_sec", 75.0))))))
        self.afk_count_var = tk.StringVar(value=str(int(self.cfg.get("afk_max_consecutive_pings", 2))))

        self.proactive_enable_var = tk.BooleanVar(value=bool(self.cfg.get("proactive_enable", True)))
        saved_ptime = float(self.cfg.get("proactive_timeout_sec", 18.0) or 18.0)
        closest_ptime = min(PROACTIVE_TIMEOUT_LABELS.keys(), key=lambda k: abs(k - saved_ptime))
        self.proactive_timeout_var = tk.StringVar(value=PROACTIVE_TIMEOUT_LABELS[closest_ptime])
        saved_pvol = float(self.cfg.get("proactive_trigger_volume", 1.0) or 1.0)
        closest_pvol = min(PROACTIVE_VOL_LABELS.keys(), key=lambda k: abs(k - saved_pvol))
        self.proactive_vol_var = tk.StringVar(value=PROACTIVE_VOL_LABELS[closest_pvol])

        self._build_ui()
        self._bind_app_live_sync()
        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + max(20, (parent.winfo_width() - 860) // 2)
            py = parent.winfo_rooty() + max(10, (parent.winfo_height() - 760) // 2)
            self.geometry(f"+{px}+{py}")
        except Exception:
            pass
        if 0 <= self._initial_tab < 4:
            try:
                self.notebook.select(self._initial_tab)
            except Exception:
                pass
        self.focus_force()
        if self.first_run and hasattr(self, "ent_fish_key"):
            try:
                self.ent_fish_key.focus_set()
            except Exception:
                pass

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
                if new_val and new_val != self.ref_id_var.get().strip():
                    self.ref_id_var.set(new_val)
            except Exception:
                pass

        try:
            self._ref_trace_token = self.app.ref_id_var.trace_add("write", _on_app_ref_change)
        except Exception:
            self._ref_trace_token = None

    def destroy(self) -> None:
        if self.app is not None and self._ref_trace_token and hasattr(self.app, "ref_id_var"):
            try:
                self.app.ref_id_var.trace_remove("write", self._ref_trace_token)
            except Exception:
                pass
            self._ref_trace_token = None
        self._show_fish_key = False
        self._show_gem_key = False
        super().destroy()

    def _build_ui(self) -> None:
        # MomoTalk coral-pink header banner
        banner = tk.Frame(self, bg="#FB7390", padx=16, pady=10)
        banner.pack(fill="x")
        b_left = tk.Frame(banner, bg="#FB7390")
        b_left.pack(side="left", fill="x", expand=True)
        tk.Label(
            b_left,
            text="💬 MomoTalk 환경 설정  ·  프롬프트 해석 & 1~3단계 통합 설정",
            fg="#FFFFFF",
            bg="#FB7390",
            font=("Malgun Gothic", 12, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            b_left,
            text=f"🔒 개인 API 키 및 로그인 세션은 로컬 격리 폴더 ({get_user_data_dir()}) 에만 저장됩니다.",
            fg="#FFF1F4",
            bg="#FB7390",
            font=("Malgun Gothic", 8, "bold"),
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        b_right = tk.Frame(banner, bg="#FB7390")
        b_right.pack(side="right")
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

        body = tk.Frame(self, bg="#F3F5F8", padx=14, pady=10)
        body.pack(fill="both", expand=True)

        # Style notebook tabs softly in MomoTalk palette
        style = ttk.Style(self)
        try:
            style.configure("Momo.TNotebook", background="#F3F5F8", borderwidth=0)
            style.configure("Momo.TNotebook.Tab", padding=(12, 6), font=("Malgun Gothic", 9, "bold"))
        except Exception:
            pass

        self.notebook = ttk.Notebook(body, style="Momo.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        tab_prompt = tk.Frame(self.notebook, bg="#F3F5F8", padx=6, pady=8)
        tab_step1 = tk.Frame(self.notebook, bg="#F3F5F8", padx=6, pady=8)
        tab_step2 = tk.Frame(self.notebook, bg="#F3F5F8", padx=6, pady=8)
        tab_step3 = tk.Frame(self.notebook, bg="#F3F5F8", padx=6, pady=8)

        self.notebook.add(tab_prompt, text=" 🎭 프롬프트 해석 버전 ")
        self.notebook.add(tab_step1, text=" 🔑 1단계: 스팀음성·인증 ")
        self.notebook.add(tab_step2, text=" 🎤 2단계: 보이스·디렉팅 ")
        self.notebook.add(tab_step3, text=" 🎧 3단계: 통화모드·오디오·자막 ")

        self._build_tab_prompt(tab_prompt)
        self._build_tab_step1(tab_step1)
        self._build_tab_step2(tab_step2)
        self._build_tab_step3(tab_step3)

        # Footer buttons
        ftr = tk.Frame(body, bg="#F3F5F8", pady=4)
        ftr.pack(fill="x", pady=(8, 0))
        if self.on_open_logs is not None:
            tk.Button(
                ftr,
                text="📋 상태 로그 보기",
                command=self._handle_open_logs,
                bg="#E2E8F0",
                fg="#334155",
                relief="flat",
                font=("Malgun Gothic", 9, "bold"),
                padx=12,
                pady=7,
                cursor="hand2",
            ).pack(side="left")
        tk.Button(
            ftr,
            text="💾 설정 저장 및 적용",
            command=self._save_and_close,
            bg="#FB7390",
            fg="#FFFFFF",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Malgun Gothic", 10, "bold"),
            padx=18,
            pady=7,
            cursor="hand2",
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            ftr,
            text="닫기",
            command=self.destroy,
            bg="#E2E8F0",
            fg="#1E293B",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=14,
            pady=7,
            cursor="hand2",
        ).pack(side="right")

    def _card(self, parent: tk.Misc, title: str) -> tk.LabelFrame:
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
        )
        card.pack(fill="x", pady=(0, 8))
        return card

    # ------------------------------------------------------------------ Tab 0
    def _build_tab_prompt(self, parent: tk.Frame) -> None:
        c_interp = self._card(parent, "0. 프롬프트 해석 버전 선택 (공식 원작 정사 vs 2차창작 4종)")

        r_char = tk.Frame(c_interp, bg="#FFFFFF")
        r_char.pack(fill="x", pady=(0, 6))
        tk.Label(
            r_char,
            text="현재 선택 학생:",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        tk.Label(
            r_char,
            text=f"  {self._current_character_title()}",
            bg="#FFF1F4",
            fg="#D94B6A",
            font=("Malgun Gothic", 9, "bold"),
            padx=8,
            pady=3,
        ).pack(side="left", padx=(6, 0))

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
            width=64,
        )
        self.cmb_persona_variant.pack(side="left", fill="x", expand=True)

        tk.Checkbutton(
            c_interp,
            text="🎭 선톡 유도 지시문(선생님 침묵 시 먼저 말걸기) 프롬프트에 자동 포함",
            variable=self.proactive_prompt_var,
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9),
        ).pack(anchor="w", pady=(6, 4))

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
            padx=10,
            pady=5,
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
            padx=10,
            pady=5,
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
            padx=10,
            pady=5,
            state=btn_state,
        ).pack(side="left")

        c_lang = self._card(parent, "🌐 학생 답변 언어 선택 (모모톡 상단 일본어/한국어 버튼과 연동)")
        r_lang = tk.Frame(c_lang, bg="#FFFFFF")
        r_lang.pack(fill="x")

        def _on_dlg_reply_lang_changed() -> None:
            if self.reply_language_var.get() == "ko" and self.jp_beginner_coach_var.get():
                self.jp_beginner_coach_var.set(False)

        def _on_dlg_coach_changed() -> None:
            if self.jp_beginner_coach_var.get() and self.reply_language_var.get() != "ja":
                self.reply_language_var.set("ja")

        tk.Radiobutton(
            r_lang,
            text="🇯🇵 일본어 답변 (일본어 원음 · 기본)",
            variable=self.reply_language_var,
            value="ja",
            command=_on_dlg_reply_lang_changed,
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=(0, 16))
        tk.Radiobutton(
            r_lang,
            text="🇰🇷 한국어 답변 (한국어 대화 · 한국어 더빙 보이스)",
            variable=self.reply_language_var,
            value="ko",
            command=_on_dlg_reply_lang_changed,
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")

        c_coach = self._card(parent, "🌸 서툰 일본어 눈높이 배려 모드 (페르소나 설정과 별개 ON/OFF)")
        tk.Checkbutton(
            c_coach,
            text="🌸 일본어가 서툰 한국인 상대에게 눈높이를 맞춰 자연스럽게 대화하기 (과도한 선생·교정 말투 없음)",
            variable=self.jp_beginner_coach_var,
            command=_on_dlg_coach_changed,
            bg="#FFFFFF",
            fg="#BE185D",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            c_coach,
            text=(
                "• 선택한 캐릭터의 고유 페르소나·말투(반말/존댓말)·공식/2차창작 버전은 100% 그대로 유지한 채,\n"
                "  실제 일본인이 서툰 한국인과 대화할 때처럼 쉬운 일상 어휘와 편한 문장으로 맞춰 주며 문맥으로 자연스럽게 알아듣습니다."
            ),
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            justify="left",
            anchor="w",
        ).pack(anchor="w", padx=(22, 0), pady=(2, 0))

        c_per = tk.LabelFrame(
            parent,
            text=" 캐릭터 페르소나 사용 방식 (블루 아카이브 196인 DB vs 직접 작성 커스텀) ",
            font=("Malgun Gothic", 9, "bold"),
            bg="#FFFFFF",
            fg="#2A3446",
            padx=12,
            pady=9,
            bd=1,
            relief="groove",
        )
        c_per.pack(fill="both", expand=True)

        tk.Radiobutton(
            c_per,
            text="🎓 모모톡에서 선택한 블루 아카이브 196인 캐릭터 DB + 일본어 감정 디렉팅 사용 (권장)",
            variable=self.use_custom_persona_var,
            value=False,
            bg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w")
        tk.Radiobutton(
            c_per,
            text="✍️ 아래에 직접 입력한 커스텀 캐릭터 페르소나 지시문 사용 (자유 캐릭터/한국어 대화용)",
            variable=self.use_custom_persona_var,
            value=True,
            bg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            fg="#7C2D12",
        ).pack(anchor="w", pady=(2, 4))

        self.txt_persona = scrolledtext.ScrolledText(c_per, height=6, font=("Malgun Gothic", 9), wrap="word")
        self.txt_persona.pack(fill="both", expand=True)
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
            tk.Label(
                guide,
                text=(
                    "① 아래 [🔑 키 발급]을 눌러 본인 Fish Audio API 키를 복사한 뒤 [📋 붙여넣기] 클릭\n"
                    "② 우측 하단 [💾 설정 저장 및 적용] 클릭 → 왼쪽 학생 선택 후 [통화 시작] 누르기 (구글 제미니 로그인 창 자동 실행)\n"
                    "💡 사전 등록된 공개 보이스가 이미 연결되어 있어 별도의 음성 추출 없이 바로 통화할 수 있습니다."
                ),
                bg="#FFF1F4",
                fg="#881337",
                font=("Malgun Gothic", 8),
                justify="left",
                anchor="w",
            ).pack(fill="x", pady=(3, 0))

        c_steam = self._card(parent, "1-A. Fish Audio API 키 연결 (필수) & 스팀 음성 추출 (선택)")

        r_fk = tk.Frame(c_steam, bg="#FFFFFF")
        r_fk.pack(fill="x", pady=(0, 8))
        tk.Label(r_fk, text="Fish Audio API Key:", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        self.ent_fish_key = tk.Entry(r_fk, textvariable=self.fish_key_var, show="*", width=38, font=("Consolas", 9))
        self.ent_fish_key.pack(side="left", padx=(0, 6))
        tk.Button(r_fk, text="👁", command=self._toggle_fish_eye, bg="#E2E8F0", width=3).pack(side="left", padx=(0, 4))
        tk.Button(r_fk, text="📋 붙여넣기", command=lambda: self._paste_into(self.fish_key_var), bg="#DCFCE7").pack(
            side="left", padx=(0, 4)
        )
        tk.Button(
            r_fk,
            text="🔑 키 발급",
            command=lambda: self._open_external_url("https://fish.audio/app/api-keys/"),
            bg="#DBEAFE",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=(0, 4))
        tk.Button(
            r_fk,
            text="🔊 보이스 탐색",
            command=lambda: self._open_external_url("https://fish.audio/discovery/"),
            bg="#F1F5F9",
        ).pack(side="left")

        r_ext = tk.Frame(c_steam, bg="#FFFFFF")
        r_ext.pack(fill="x", pady=(2, 4))
        tk.Label(
            r_ext,
            text="(선택) 스팀 블루아카에서 직접 캐릭터 .wav 보이스를 추출할 때만 사용:",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
        ).pack(side="left")
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
            state="normal" if self.app is not None else "disabled",
        ).pack(side="right")

        if ENABLE_ON_DEMAND_VOICE_UI:
            r_st = tk.Frame(c_steam, bg="#FFFFFF")
            r_st.pack(fill="x", pady=(0, 6))
            tk.Label(r_st, text="스팀 블루아카 폴더:", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
                side="left"
            )
            self.ent_steam_dir = tk.Entry(r_st, textvariable=self.steam_game_dir_var, width=38, font=("Consolas", 8))
            self.ent_steam_dir.pack(side="left", padx=(0, 6))
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
            )
            self.lbl_steam_dir_status.pack(fill="x", pady=(0, 6))
            self._refresh_steam_dir_status_label()

        c_gem = self._card(parent, "1-B. 제미니(Gemini) 연동 방식 & 로그인 인증")
        tk.Radiobutton(
            c_gem,
            text="🌐 전용 브라우저 연동 (gemini.google.com — 구글 로그인 유지 · API 키 불필요 · 기본)",
            variable=self.gemini_backend_var,
            value="browser",
            bg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            fg="#0F766E",
        ).pack(anchor="w")
        tk.Radiobutton(
            c_gem,
            text="⚡ 공식 Gemini API 연동 (Google AI Studio 키 · 초저지연 스트리밍 · 브라우저 창 없음)",
            variable=self.gemini_backend_var,
            value="api",
            bg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            fg="#1E40AF",
        ).pack(anchor="w", pady=(2, 6))

        rg = tk.Frame(c_gem, bg="#FFFFFF")
        rg.pack(fill="x", pady=2)
        tk.Label(rg, text="Gemini API Key (선택):", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9)).pack(
            side="left"
        )
        self.ent_gem_key = tk.Entry(rg, textvariable=self.gemini_key_var, show="*", width=31, font=("Consolas", 9))
        self.ent_gem_key.pack(side="left", padx=(0, 4))
        tk.Button(rg, text="👁", command=self._toggle_gem_eye, bg="#E2E8F0", width=3).pack(side="left", padx=(0, 4))
        tk.Button(rg, text="📋", command=lambda: self._paste_into(self.gemini_key_var), bg="#DCFCE7", width=3).pack(
            side="left", padx=(0, 4)
        )
        self.btn_fetch_api_models = tk.Button(
            rg,
            text="🔄 API 모델 조회",
            command=self._fetch_api_models_async,
            bg="#E0F2FE",
            fg="#0369A1",
        )
        self.btn_fetch_api_models.pack(side="left", padx=(0, 4))
        tk.Button(
            rg,
            text="🔑 무료 발급",
            command=lambda: self._open_external_url("https://aistudio.google.com/apikey"),
            bg="#DBEAFE",
        ).pack(side="left")

        r_br = tk.Frame(c_gem, bg="#FFFFFF")
        r_br.pack(fill="x", pady=(8, 0))
        btn_state = "normal" if self.app is not None else "disabled"
        tk.Label(
            r_br,
            text="전용 브라우저 구글 로그인 관리:",
            bg="#FFFFFF",
            fg="#475569",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        tk.Button(
            r_br,
            text="🌐 제미니 창 보기/숨기기",
            command=lambda: self._run_app_method("toggle_gemini_browser_window"),
            bg="#E0F2FE",
            fg="#0369A1",
            relief="flat",
            padx=10,
            pady=4,
            state=btn_state,
        ).pack(side="left", padx=(10, 6))
        tk.Button(
            r_br,
            text="✅ 구글 로그인 완료 확인",
            command=lambda: self._run_app_method("confirm_browser_login_async"),
            bg="#DCFCE7",
            fg="#166534",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=4,
            state=btn_state,
        ).pack(side="left")

    # ------------------------------------------------------------------ Tab 2
    def _build_tab_step2(self, parent: tk.Frame) -> None:
        c_voice = self._card(parent, f"2-A. 캐릭터별 Voice Reference ID 설정 — [{self._current_character_title()}]")

        r_ref = tk.Frame(c_voice, bg="#FFFFFF")
        r_ref.pack(fill="x", pady=3)
        tk.Label(r_ref, text="Voice Reference ID:", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        tk.Entry(r_ref, textvariable=self.ref_id_var, width=42, font=("Consolas", 9)).pack(side="left", padx=(0, 6))
        tk.Button(
            r_ref,
            text="📋 URL 붙여넣기",
            command=lambda: self._paste_into(self.ref_id_var, clean_ref=True),
            bg="#DCFCE7",
        ).pack(side="left")

        r_act = tk.Frame(c_voice, bg="#FFFFFF")
        r_act.pack(fill="x", pady=(6, 2))
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
            state=btn_state,
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            r_act,
            text="✨ 스팀 원본 업로드 · 전용 모델 자동 생성",
            command=lambda: self._run_app_method("auto_create_aru_model_async", push_key_ref=True),
            bg="#FFF1F4",
            fg="#D94B6A",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=5,
            state=btn_state,
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            r_act,
            text="🔊 시그니처 대사 테스트",
            command=lambda: self._run_app_method("test_aru_tts_async", push_key_ref=True),
            bg="#ECFDF5",
            fg="#047857",
            relief="flat",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=5,
            state=btn_state,
        ).pack(side="left", padx=(0, 6))
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
        ).pack(side="left")

        tk.Label(
            c_voice,
            text=(
                "• 모모톡 메인 화면에서 목소리 링크가 없는 학생을 누르면 버튼 하나로 자동 생성·연결할 수 있습니다."
                if ENABLE_ON_DEMAND_VOICE_UI
                else "• 상단 답변 언어(🇯🇵 일본어 / 🇰🇷 한국어)에 맞춰 스팀 원음(.molru)과 공식 대본을 자동 추출해 Fish Audio 보이스를 생성·저장합니다."
            ),
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            anchor="w",
        ).pack(anchor="w", pady=(4, 4 if ENABLE_ON_DEMAND_VOICE_UI else 0))

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
                width=34,
                state="readonly",
            ).pack(side="left", padx=(0, 10))
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
                width=25,
                state="readonly",
            ).pack(side="left")

        # Dual-voice alt persona controls (e.g. Kisaki <-> Kiki real-time sentence voice switching)
        for ak, slot_id, lbl_kr in self._alt_voice_specs:
            alt_var = self._alt_voice_vars[slot_id]
            r_alt = tk.Frame(c_voice, bg="#FFF7FA", padx=8, pady=5, highlightbackground="#FBCFE8", highlightthickness=1)
            r_alt.pack(fill="x", pady=(6, 0))
            tk.Label(
                r_alt,
                text=f"🎭 보조 보이스 [{lbl_kr}]:",
                bg="#FFF7FA",
                fg="#BE185D",
                font=("Malgun Gothic", 8, "bold"),
                anchor="w",
            ).pack(side="left", padx=(0, 6))
            tk.Entry(r_alt, textvariable=alt_var, width=26 if ENABLE_ON_DEMAND_VOICE_UI else 30, font=("Consolas", 8)).pack(
                side="left", padx=(0, 5)
            )
            tk.Button(
                r_alt,
                text="📋 붙여넣기",
                command=lambda v=alt_var: self._paste_into(v, clean_ref=True),
                bg="#DCFCE7",
                font=("Malgun Gothic", 8),
                relief="flat",
                padx=6,
                pady=2,
            ).pack(side="left", padx=(0, 4 if ENABLE_ON_DEMAND_VOICE_UI else 5))
            if ENABLE_ON_DEMAND_VOICE_UI:
                tk.Button(
                    r_alt,
                    text="✨ 자동 생성",
                    command=lambda k=ak, s=slot_id: self._auto_create_alt_voice(k, s),
                    bg="#FFF1F4",
                    fg="#D94B6A",
                    font=("Malgun Gothic", 8, "bold"),
                    relief="flat",
                    padx=7,
                    pady=2,
                    state=btn_state,
                ).pack(side="left", padx=(0, 4))
            tk.Button(
                r_alt,
                text="🔊 테스트" if ENABLE_ON_DEMAND_VOICE_UI else "🔊 보조 보이스 테스트",
                command=lambda k=ak, s=slot_id, v=alt_var: self._test_alt_voice(k, s, v),
                bg="#FCE7F3",
                fg="#9D174D",
                font=("Malgun Gothic", 8, "bold"),
                relief="flat",
                padx=7 if ENABLE_ON_DEMAND_VOICE_UI else 8,
                pady=2,
                state=btn_state,
            ).pack(side="left")
            tk.Label(
                c_voice,
                text=(
                    f"  └ 대화 중 `[VOICE:{ak}]` 연출 시 자동으로 이 보이스({slot_id})로 전환됩니다. "
                    "(일괄 자동화 실행 시 자동 추출·업로드 · 미연결 시에도 로컬 Zero-Shot 자동 작동)"
                ),
                bg="#FFFFFF",
                fg="#9D174D",
                font=("Malgun Gothic", 8),
                anchor="w",
            ).pack(anchor="w", pady=(2, 0))

        c_dir = self._card(parent, "2-B. Fish Audio 합성 엔진 · 응답 속도 · 감정/억양 디렉팅")

        r_eng = tk.Frame(c_dir, bg="#FFFFFF")
        r_eng.pack(fill="x", pady=3)
        tk.Label(r_eng, text="음성 합성 엔진(model):", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        ttk.Combobox(
            r_eng,
            textvariable=self.fish_model_hdr_var,
            values=FISH_MODEL_HEADERS,
            width=18,
            state="readonly",
        ).pack(side="left", padx=(0, 14))
        tk.Label(r_eng, text="스트리밍 모드:", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(side="left")
        ttk.Combobox(
            r_eng,
            textvariable=self.speed_mode_var,
            values=list(SPEED_MODE_LABELS.values()),
            width=40,
            state="readonly",
        ).pack(side="left", padx=(6, 0))

        r_vol = tk.Frame(c_dir, bg="#FFFFFF")
        r_vol.pack(fill="x", pady=(6, 3))
        tk.Label(r_vol, text="캐릭터 출력 음량:", width=20, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        self.lbl_vol_pct = tk.Label(r_vol, text=f"{int(round(self.vol_var.get() * 100))}%", width=6, bg="#FFFFFF", fg="#D94B6A", font=("Malgun Gothic", 9, "bold"))
        self.lbl_vol_pct.pack(side="left")
        tk.Scale(
            r_vol,
            from_=0.2,
            to=1.5,
            resolution=0.05,
            orient="horizontal",
            variable=self.vol_var,
            showvalue=False,
            bg="#FFFFFF",
            highlightthickness=0,
            length=240,
            command=lambda v: self.lbl_vol_pct.configure(text=f"{int(round(float(v) * 100))}%"),
        ).pack(side="left", padx=(4, 16))
        tk.Label(r_vol, text="연기 텐션/억양 강도:", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(side="left")
        ttk.Combobox(
            r_vol,
            textvariable=self.boost_preset_var,
            values=list(BOOST_PRESET_LABELS.values()),
            width=26,
            state="readonly",
        ).pack(side="left", padx=(6, 0))

        tk.Checkbutton(
            c_dir,
            text="🎭 문맥 · 대사 분석 기반 자동 감정/억양 디렉팅 (v4 엔진 + 정밀패치 스파이크/속삭임 적용)",
            variable=self.auto_emotion_var,
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------------ Tab 3
    def _build_tab_step3(self, parent: tk.Frame) -> None:
        c_mode = self._card(parent, "3-A. AI 보이스 통화 방식 (Mode 1 vs Mode 2) & 오디오 장치")

        r_m = tk.Frame(c_mode, bg="#FFFFFF")
        r_m.pack(fill="x", pady=(0, 6))
        tk.Radiobutton(
            r_m,
            text="🎤 Mode 1: PC 마이크 직통 AI 컴패니언 통화 (VB-Cable 불필요 · 권장)",
            variable=self.call_bridge_mode_var,
            value="mode1",
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left", padx=(0, 14))
        tk.Radiobutton(
            r_m,
            text="🎧 Mode 2: VB-Cable 라이브 오디오 캡처 브릿지 (크롬 Live 등)",
            variable=self.call_bridge_mode_var,
            value="mode2",
            bg="#FFFFFF",
            fg="#1D4ED8",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")

        out_vals = list(getattr(self.app, "primary_outputs", []) or [])
        mic_vals = list(getattr(self.app, "mic_inputs", []) or [])
        in_vals = [lbl for _, lbl, _ in (getattr(self.app, "inputs", []) or [])]
        trig_vals = list(getattr(self.app, "trigger_outputs", []) or [])

        r_dev1 = tk.Frame(c_mode, bg="#FFFFFF")
        r_dev1.pack(fill="x", pady=2)
        tk.Label(r_dev1, text="출력 장치 (이어폰/스피커):", width=22, anchor="w", bg="#FFFFFF", font=("Malgun Gothic", 9, "bold")).pack(
            side="left"
        )
        self.cmb_set_out = ttk.Combobox(r_dev1, textvariable=self.primary_out_var, values=out_vals, width=42, state="readonly")
        self.cmb_set_out.pack(side="left", padx=(0, 8))
        tk.Button(
            r_dev1,
            text="🔄 장치 새로고침",
            command=self._refresh_app_devices,
            bg="#F1F5F9",
            fg="#334155",
            relief="flat",
            padx=8,
            pady=2,
            state="normal" if self.app is not None else "disabled",
        ).pack(side="left", padx=(0, 8))
        if self.app is not None and hasattr(self.app, "lbl_gpu_status") and self.app.lbl_gpu_status is not None:
            try:
                gpu_txt = str(self.app.lbl_gpu_status.cget("text") or "").strip()
            except Exception:
                gpu_txt = ""
            if gpu_txt:
                self.lbl_settings_gpu = tk.Label(
                    r_dev1,
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
        self.cmb_set_mic = ttk.Combobox(r_dev2, textvariable=self.mic_input_var, values=mic_vals, width=42, state="readonly")
        self.cmb_set_mic.pack(side="left", padx=(0, 8))

        r_dev3 = tk.Frame(c_mode, bg="#FFFFFF")
        r_dev3.pack(fill="x", pady=2)
        tk.Label(r_dev3, text="VB-Cable 입력 (Mode 2):", width=22, anchor="w", bg="#FFFFFF").pack(side="left")
        self.cmb_set_vbin = ttk.Combobox(r_dev3, textvariable=self.vb_in_var, values=in_vals, width=42, state="readonly")
        self.cmb_set_vbin.pack(side="left", padx=(0, 8))

        r_dev4 = tk.Frame(c_mode, bg="#FFFFFF")
        r_dev4.pack(fill="x", pady=2)
        tk.Label(r_dev4, text="STT 언어 / VAD 민감도:", width=22, anchor="w", bg="#FFFFFF").pack(side="left")
        ttk.Combobox(
            r_dev4,
            textvariable=self.stt_lang_var,
            values=list(STT_LANG_LABELS.values()),
            width=30,
            state="readonly",
        ).pack(side="left", padx=(0, 6))
        ttk.Combobox(
            r_dev4,
            textvariable=self.vad_preset_var,
            values=list(VAD_PRESETS.keys()),
            width=26,
            state="readonly",
        ).pack(side="left")

        c_mod = self._card(parent, "3-B. 제미니 대화 모델 & 실시간 대사 자막 엔진")
        api_vals = [code for code, _ in GEMINI_API_MODELS]
        for m in self._extra_api_models:
            if m and m not in api_vals:
                api_vals.append(m)

        rm1 = tk.Frame(c_mod, bg="#FFFFFF")
        rm1.pack(fill="x", pady=2)
        tk.Label(rm1, text="브라우저 대화 모델:", width=17, anchor="w", bg="#FFFFFF").pack(side="left")
        self.cmb_browser_model = ttk.Combobox(
            rm1,
            textvariable=self.gemini_browser_model_var,
            values=[lbl for _, lbl in self._all_browser_models],
            width=28,
            state="readonly",
        )
        self.cmb_browser_model.pack(side="left", padx=(0, 8))
        tk.Label(rm1, text="추론:", bg="#FFFFFF").pack(side="left")
        self.cmb_chat_thinking = ttk.Combobox(
            rm1,
            textvariable=self.gemini_thinking_var,
            values=[lbl for _, lbl in GEMINI_THINKING_LEVELS],
            width=19,
            state="readonly",
        )
        self.cmb_chat_thinking.pack(side="left", padx=(4, 6))
        self.btn_sync_web_models = tk.Button(
            rm1,
            text="🔄 웹 모델 동기화",
            command=self._sync_web_models_async,
            bg="#ECFEFF",
            fg="#0E7490",
            font=("Malgun Gothic", 8, "bold"),
        )
        self.btn_sync_web_models.pack(side="left")

        rm2 = tk.Frame(c_mod, bg="#FFFFFF")
        rm2.pack(fill="x", pady=2)
        tk.Label(rm2, text="공식 API 대화 모델:", width=17, anchor="w", bg="#FFFFFF").pack(side="left")
        self.cmb_api_model = ttk.Combobox(
            rm2,
            textvariable=self.gemini_model_var,
            values=api_vals,
            width=28,
            state="normal",
        )
        self.cmb_api_model.pack(side="left", padx=(0, 8))
        tk.Label(rm2, text="트리거 장치(M2):", bg="#FFFFFF").pack(side="left")
        self.cmb_set_trig = ttk.Combobox(rm2, textvariable=self.proactive_trig_dev_var, values=trig_vals, width=24, state="readonly")
        self.cmb_set_trig.pack(side="left", padx=(4, 0))

        rm3 = tk.Frame(c_mod, bg="#FFFFFF")
        rm3.pack(fill="x", pady=2)
        tk.Label(rm3, text="대사 자막 모델:", width=17, anchor="w", bg="#FFFFFF").pack(side="left")
        self.cmb_sub_engine = ttk.Combobox(
            rm3,
            textvariable=self.subtitle_engine_var,
            values=[lbl for _, lbl in self._all_sub_choices],
            width=28,
            state="readonly",
        )
        self.cmb_sub_engine.pack(side="left", padx=(0, 8))
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

        c_tm = self._card(parent, "3-C. 양방향 무음 선톡 타이머 & AFK 설정")
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

    # ------------------------------------------------------------------ Helpers
    def _push_live_app_vars(self, push_key_ref: bool = False, sync_variant: bool = False) -> None:
        if self.app is None:
            return
        if sync_variant and hasattr(self.app, "persona_variant_var"):
            self.app.persona_variant_var.set(self.persona_variant_var.get())
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
                    self.auto_voice_prof_var.get().strip(), "pro85"
                )
            if hasattr(self.app, "api_key_var"):
                self.app.api_key_var.set(self.fish_key_var.get().strip())
            if hasattr(self.app, "ref_id_var"):
                self.app.ref_id_var.set(_clean_ref_id(self.ref_id_var.get()))
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
            self.mic_input_var.set(self.app.mic_choice_var.get())
        if hasattr(self.app, "in_choice_var") and self.app.in_choice_var.get():
            self.vb_in_var.set(self.app.in_choice_var.get())
        if hasattr(self.app, "trigger_out_choice_var") and self.app.trigger_out_choice_var.get():
            self.proactive_trig_dev_var.set(self.app.trigger_out_choice_var.get())

    def _toggle_fish_eye(self) -> None:
        self._show_fish_key = not self._show_fish_key
        self.ent_fish_key.config(show="" if self._show_fish_key else "*")

    def _toggle_gem_eye(self) -> None:
        self._show_gem_key = not self._show_gem_key
        self.ent_gem_key.config(show="" if self._show_gem_key else "*")

    def _current_secrets(self) -> tuple[str, ...]:
        secrets: list[str] = []
        for var in (self.fish_key_var, self.gemini_key_var):
            try:
                val = var.get().strip()
                if val:
                    secrets.append(val)
            except Exception:
                pass
        return tuple(secrets)

    def _open_external_url(self, url: str) -> None:
        try:
            open_safe_external_url(url)
        except Exception as exc:
            messagebox.showerror(
                "링크 열기 차단",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )

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
        self.btn_sync_web_models.configure(state="disabled", text="동기화 중...")

        def _on_done(models: list[tuple[str, str]], tier: str, err: str) -> None:
            def _apply() -> None:
                if not self.winfo_exists():
                    return
                self.btn_sync_web_models.configure(state="normal", text="🔄 웹 모델 동기화")
                if err and not models:
                    messagebox.showwarning(
                        "웹 모델 동기화 안내",
                        redact_secrets(str(err), self._current_secrets()),
                        parent=self,
                    )
                    return
                if not models:
                    messagebox.showinfo("동기화 완료", "현재 계정에서 선택 가능한 웹 모델을 찾지 못했습니다.", parent=self)
                    return
                self._account_web_models = list(models)
                if tier:
                    self.cfg["web_account_tier"] = tier
                base_codes = {c for c, _ in models}
                self._all_browser_models = list(models) + [
                    (c, l) for c, l in self._extra_web_models if c not in base_codes
                ]
                self._browser_lbl_by_code = dict(self._all_browser_models)
                self.cmb_browser_model.configure(values=[lbl for _, lbl in self._all_browser_models])
                if self.gemini_browser_model_var.get().strip() not in set(self._browser_lbl_by_code.values()):
                    fallback_code = "hydrogen" if "hydrogen" in self._browser_lbl_by_code else self._all_browser_models[0][0]
                    self.gemini_browser_model_var.set(self._browser_lbl_by_code[fallback_code])
                self._all_sub_choices = get_subtitle_engine_choices(
                    self._extra_api_models,
                    self._extra_web_models,
                    web_models=self._account_web_models or None,
                )
                self.cmb_sub_engine.configure(values=[lbl for _, lbl in self._all_sub_choices])
                valid_sub_lbls = {lbl for _, lbl in self._all_sub_choices}
                if self.subtitle_engine_var.get().strip() not in valid_sub_lbls:
                    self.subtitle_engine_var.set(resolve_subtitle_engine_label("auto_inline", self._all_sub_choices))
                self._sync_sub_thinking_state()
                tier_txt = f"{tier} 계정" if tier else "웹 제미니 계정"
                messagebox.showinfo("동기화 완료", f"{tier_txt} 사용 가능 모델 {len(models)}개를 불러왔습니다.", parent=self)

            try:
                self.after(0, _apply)
            except Exception:
                pass

        self._on_sync_web_models(_on_done)

    def _fetch_api_models_async(self) -> None:
        import threading

        key = self.gemini_key_var.get().strip()
        if not key:
            messagebox.showinfo("Gemini API Key 필요", "먼저 Gemini API Key를 입력한 뒤 모델 목록을 조회해 주세요.", parent=self)
            return
        self.btn_fetch_api_models.configure(state="disabled", text="조회 중...")

        def _worker() -> None:
            err_msg = ""
            fetched: list[str] = []
            try:
                api = OfficialGeminiApiBackend()
                try:
                    fetched = api.list_generate_models(key)
                finally:
                    api.close()
            except Exception as exc:
                err_msg = redact_secrets(str(exc), (key,))

            def _done() -> None:
                if not self.winfo_exists():
                    return
                self.btn_fetch_api_models.configure(state="normal", text="🔄 API 모델 조회")
                if err_msg:
                    messagebox.showwarning(
                        "모델 조회 실패",
                        redact_secrets(err_msg, self._current_secrets()),
                        parent=self,
                    )
                    return
                if not fetched:
                    messagebox.showinfo("조회 완료", "사용 가능한 Gemini 모델을 찾지 못했습니다.", parent=self)
                    return
                self._extra_api_models = list(fetched)
                api_vals = list(dict.fromkeys([code for code, _ in GEMINI_API_MODELS] + fetched))
                self.cmb_api_model.configure(values=api_vals)
                self._all_sub_choices = get_subtitle_engine_choices(
                    self._extra_api_models,
                    self._extra_web_models,
                    web_models=self._account_web_models or None,
                )
                self.cmb_sub_engine.configure(values=[lbl for _, lbl in self._all_sub_choices])
                messagebox.showinfo("조회 완료", f"Gemini API 모델 {len(fetched)}개를 불러왔습니다.", parent=self)

            try:
                self.after(0, _done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="fetch-gem-models").start()

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

    def _auto_create_alt_voice(self, alt_key: str, slot_id: str) -> None:
        if self.app is None or not hasattr(self.app, "auto_generate_current_student_voice_async"):
            return
        self._push_live_app_vars(push_key_ref=True)

        def _on_alt_done(res: dict[str, Any]) -> None:
            if not res.get("ok"):
                return
            mid = str(res.get("model_id") or "").strip()
            if not mid:
                return

            def _upd() -> None:
                if not self.winfo_exists():
                    return
                bucket = "saved_voices_kr" if self._get_reply_lang() == "ko" else "saved_voices"
                self.cfg.setdefault(bucket, {})[slot_id] = mid
                if slot_id in self._alt_voice_vars:
                    self._alt_voice_vars[slot_id].set(mid)

            try:
                self.after(0, _upd)
            except Exception:
                pass

        self.app.auto_generate_current_student_voice_async(
            alt_voice_key=alt_key,
            on_done=_on_alt_done,
        )

    def _test_alt_voice(self, alt_key: str, slot_id: str, alt_var: tk.StringVar) -> None:
        if self.app is None or not hasattr(self.app, "test_alt_voice_tts_async"):
            return
        self._push_live_app_vars(push_key_ref=True)
        rlang = self._get_reply_lang()
        bucket = "saved_voices_kr" if rlang == "ko" else "saved_voices"
        clean_alt = _clean_ref_id(alt_var.get())
        if clean_alt:
            self.cfg.setdefault(bucket, {})[slot_id] = clean_alt
            self.app.cfg.setdefault(bucket, {})[slot_id] = clean_alt
        else:
            self.cfg.setdefault(bucket, {}).pop(slot_id, None)
            self.app.cfg.setdefault(bucket, {}).pop(slot_id, None)
        self.app.test_alt_voice_tts_async(alt_key)

    def _open_batch_voice_dialog(self) -> None:
        if self.app is None:
            return
        from src.controller.fish_batch_dialog import FishBatchDialog

        # Push latest typed API key so batch dialog can use it immediately without needing Save first
        typed_key = self.fish_key_var.get().strip()
        if typed_key:
            self.cfg["fish_api_key"] = typed_key
            self.app.cfg["fish_api_key"] = typed_key

        def _sync_cfg_from_app_on_main() -> None:
            """Keep SettingsDialog's local cfg copy & ref_id_var synchronized so Save never overwrites batch links."""
            if not self.winfo_exists():
                return
            self.cfg["saved_voices"] = dict(self.app.cfg.get("saved_voices") or {})
            self.cfg["saved_voices_kr"] = dict(self.app.cfg.get("saved_voices_kr") or {})
            cur_cid = self._current_character_id()
            cur_rlang = self._get_reply_lang()
            cur_bucket = "saved_voices_kr" if cur_rlang == "ko" else "saved_voices"
            latest_id = str((self.cfg.get(cur_bucket) or {}).get(cur_cid) or "").strip()
            if latest_id:
                self.ref_id_var.set(latest_id)
                self.cfg["fish_reference_id"] = latest_id
                if hasattr(self.app, "ref_id_var"):
                    try:
                        self.app.ref_id_var.set(latest_id)
                    except Exception:
                        pass
            for _ak, slot_id, _lbl in self._alt_voice_specs:
                alt_latest = str((self.cfg.get(cur_bucket) or {}).get(slot_id) or "").strip()
                if alt_latest and slot_id in self._alt_voice_vars:
                    self._alt_voice_vars[slot_id].set(alt_latest)

        def _on_linked(cid: str, lang_up: str, mid: str) -> None:
            bucket = "saved_voices_kr" if lang_up == "KR" else "saved_voices"
            self.app.cfg.setdefault(bucket, {})[cid] = mid
            # All Tk variable / SettingsDialog cfg mutations MUST run on the Tk main thread
            try:
                self.after(0, _sync_cfg_from_app_on_main)
            except Exception:
                pass
            try:
                self.app._schedule_ui(
                    lambda: (
                        self.app._refresh_character_filter_list(keep_current=True)
                        if getattr(self.app, "_momotalk_sort_mode", "default") == "heart"
                        else self.app._render_roster_visible(force_all=True),
                        self.app._refresh_momotalk_header_and_cards(),
                    )
                )
            except Exception:
                pass

        def _on_done() -> None:
            try:
                _sync_cfg_from_app_on_main()
                self.app._refresh_momotalk_header_and_cards()
                self.app._render_roster_visible(force_all=True)
            except Exception:
                pass

        FishBatchDialog(
            self,
            cfg=self.app.cfg,
            characters=self.app.characters,
            api_key_getter=lambda: self.fish_key_var.get().strip(),
            on_model_linked=_on_linked,
            on_finished=_on_done,
        )

    def _save_and_close(self) -> None:
        try:
            min_s = max(5.0, float(self.min_idle_var.get().strip()))
            max_s = max(min_s, float(self.max_idle_var.get().strip()))
            afk_n = max(1, int(self.afk_count_var.get().strip()))
        except ValueError:
            messagebox.showerror("입력 오류", "선톡 무음 대기 시간과 AFK 횟수는 숫자로 입력해 주세요.", parent=self)
            return

        fish_key = _sanitize_single_line_secret(self.fish_key_var.get())
        ref_id = _clean_ref_id(self.ref_id_var.get())
        gem_backend = self.gemini_backend_var.get().strip() or "browser"
        gem_key = _sanitize_single_line_secret(self.gemini_key_var.get())

        if gem_backend == "api" and not gem_key:
            messagebox.showwarning(
                "Gemini API Key 필요",
                "공식 Gemini API 모드를 선택하셨습니다. Gemini API Key를 입력하거나 전용 브라우저 모드를 선택해 주세요.",
                parent=self,
            )
            return

        try:
            gem_api_model = _validate_api_model_name(self.gemini_model_var.get())
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc), parent=self)
            return

        # Tab 0: Prompt interpretation & custom persona
        self.cfg["persona_prompt_variant"] = PERSONA_LABEL_TO_CODE.get(
            self.persona_variant_var.get().strip(), "official"
        )
        self.cfg["proactive_prompt_append"] = bool(self.proactive_prompt_var.get())
        rlang = "ko" if self.reply_language_var.get() == "ko" else "ja"
        self.cfg["reply_language"] = rlang
        self.cfg["jp_beginner_lover_mode"] = False if rlang == "ko" else bool(self.jp_beginner_coach_var.get())
        self.cfg["use_custom_persona"] = bool(self.use_custom_persona_var.get())
        self.cfg["custom_persona_prompt"] = self.txt_persona.get("1.0", tk.END).strip()

        # Tab 1 & 2: Fish Audio, Steam dir & directing
        self.cfg["fish_api_key"] = fish_key
        self.cfg["steam_game_dir"] = self.steam_game_dir_var.get().strip()
        self.cfg["auto_voice_visibility"] = AUTO_VOICE_VIS_LABEL_TO_CODE.get(
            self.auto_voice_vis_var.get().strip(), "private"
        )
        self.cfg["auto_voice_quality_profile"] = AUTO_VOICE_PROF_LABEL_TO_CODE.get(
            self.auto_voice_prof_var.get().strip(), "pro85"
        )
        self.cfg["fish_reference_id"] = ref_id
        cid = self._current_character_id()
        voice_bucket = "saved_voices_kr" if rlang == "ko" else "saved_voices"
        if ref_id:
            self.cfg.setdefault(voice_bucket, {})[cid] = ref_id
        elif cid:
            self.cfg.setdefault(voice_bucket, {}).pop(cid, None)
        for _ak, slot_id, _lbl in self._alt_voice_specs:
            alt_clean = _clean_ref_id(self._alt_voice_vars[slot_id].get())
            if alt_clean:
                self.cfg.setdefault(voice_bucket, {})[slot_id] = alt_clean
            else:
                self.cfg.setdefault(voice_bucket, {}).pop(slot_id, None)
        self.cfg["fish_model_header"] = self.fish_model_hdr_var.get().strip() or "s2.1-pro-free"
        self.cfg["streaming_speed_mode"] = SPEED_LABEL_TO_CODE.get(
            self.speed_mode_var.get().strip(), "expressive"
        )
        self.cfg["auto_emotion_directing"] = bool(self.auto_emotion_var.get())
        self.cfg["expression_boost_offset"] = float(
            BOOST_LABEL_TO_VAL.get(self.boost_preset_var.get().strip(), 0.0)
        )
        self.cfg["output_volume"] = float(self.vol_var.get())

        # Tab 1 & 3: Gemini, subtitles, bridge mode, devices, proactive timers
        bmodel_by_lbl = {lbl: code for code, lbl in self._all_browser_models}
        think_by_lbl = {lbl: code for code, lbl in GEMINI_THINKING_LEVELS}
        sub_eng_by_lbl = {lbl: code for code, lbl in self._all_sub_choices}

        self.cfg["gemini_backend"] = gem_backend
        self.cfg["gemini_api_key"] = gem_key
        self.cfg["gemini_api_model"] = gem_api_model
        self.cfg["gemini_browser_model"] = bmodel_by_lbl.get(self.gemini_browser_model_var.get().strip(), "hydrogen")
        self.cfg["gemini_thinking_level"] = think_by_lbl.get(self.gemini_thinking_var.get().strip(), "low")
        self.cfg["subtitle_engine"] = sub_eng_by_lbl.get(self.subtitle_engine_var.get().strip(), "auto_inline")
        self.cfg["subtitle_thinking_level"] = think_by_lbl.get(self.subtitle_thinking_var.get().strip(), "low")
        self.cfg["api_models"] = list(self._extra_api_models)
        self.cfg["web_models"] = [[c, l] for c, l in self._account_web_models]
        self.cfg["web_models_schema_version"] = 2

        cbm = "mode2" if self.call_bridge_mode_var.get().strip() == "mode2" else "mode1"
        self.cfg["call_bridge_mode"] = cbm
        self.cfg["bridge_mode"] = "gemini_live_audio" if cbm == "mode2" else "pc_mic"

        if self.primary_out_var.get().strip():
            self.cfg["primary_output_device"] = self.primary_out_var.get().strip()
        if self.mic_input_var.get().strip():
            self.cfg["mic_input_device"] = self.mic_input_var.get().strip()
        if self.proactive_trig_dev_var.get().strip():
            self.cfg["proactive_trigger_device"] = self.proactive_trig_dev_var.get().strip()
        if self.app is not None and hasattr(self.app, "in_choice_var") and self.vb_in_var.get().strip():
            self.app.in_choice_var.set(self.vb_in_var.get().strip())

        self.cfg["stt_language"] = STT_LABEL_TO_CODE.get(self.stt_lang_var.get().strip(), "auto")
        self.cfg["mic_vad_rms_threshold"] = float(VAD_PRESETS.get(self.vad_preset_var.get(), 0.012))

        self.cfg["companion_proactive_enable"] = bool(self.companion_proactive_var.get())
        self.cfg["idle_ping_min_sec"] = min_s
        self.cfg["idle_ping_max_sec"] = max_s
        self.cfg["afk_max_consecutive_pings"] = afk_n

        self.cfg["proactive_enable"] = bool(self.proactive_enable_var.get())
        self.cfg["proactive_timeout_sec"] = float(
            PROACTIVE_TIMEOUT_VALS.get(self.proactive_timeout_var.get().strip(), 18.0)
        )
        self.cfg["proactive_trigger_volume"] = float(
            PROACTIVE_VOL_VALS.get(self.proactive_vol_var.get().strip(), 1.0)
        )

        try:
            save_user_config(self.cfg)
        except Exception as exc:
            messagebox.showerror(
                "설정 저장 실패",
                redact_secrets(str(exc), self._current_secrets()),
                parent=self,
            )
            return
        self.on_saved(self.cfg)
        self.destroy()
