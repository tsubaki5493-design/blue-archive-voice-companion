"""
Dual Gemini Connector (Mode C: Isolated Edge/Chrome App Browser + Official Gemini API SSE)
----------------------------------------------------------------------------------------
1. BrowserGeminiBackend (Mode B - Default zero-API-key option):
   - Launches Microsoft Edge or Google Chrome in chromeless app mode (--app=https://gemini.google.com/app)
     with user data isolated strictly under %LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile.
   - Two-phase Google login: if unauthenticated, closes CDP and opens a normal un-debugged window
     so Google does NOT block sign-in ("This browser or app may not be secure"); once signed in,
     relaunches with local CDP and automatically hides the window into the background (with show/hide toggle).
   - Loads DOM selectors dynamically from selectors.json (no hardcoded fragile selectors).
   - Injects text safely without innerHTML (TrustedHTML CSP compliant) and uses a DOM MutationObserver
     for real-time incremental sentence streaming into Fish Audio 2-worker parallel TTS!

2. OfficialGeminiApiBackend (Mode A - Optional ultra-low-latency SSE):
   - Streams Gemini REST generateContent SSE with multi-turn history & system_instruction.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable

import httpx

from src.controller.jp_conversation_coach import (
    build_jp_coach_bilingual_addon,
    build_jp_coach_turn_reminder,
)
from src.utils.app_paths import get_browser_profile_dir, get_selectors_path

IDLE_PING_TOKEN = "[IDLE_PING]"
IDLE_PING_SYSTEM_RULE = (
    "[시스템 규칙: 앞으로 내가 '[IDLE_PING]'이라고 입력하면 그 문구 자체를 언급하거나 설명하지 마. "
    "대신 우리가 대화하다 잠시 조용해진 정적 상황으로 간주하고, 직전 대화 맥락과 너의 페르소나에 맞춰 "
    "자연스럽게 먼저 말을 걸거나 새로운 질문을 던져라. 모든 대답은 실제로 옆에서 말하는 것처럼 "
    "자연스러운 구어체로 생동감 있게 작성하되, 가벼운 인사와 맞장구는 간결하게 주고받고 "
    "설명·상담·위로·이야기가 필요한 순간에는 분량 제한 없이 충분히 길고 풍부하게 말하며 마크다운 기호나 목록 사용을 피해라. "
    "또한 사용자 발화는 실시간 마이크 음성인식(STT) 결과이므로 캐릭터 이름이나 짧은 인사가 발음이 비슷한 오타(예: '히카리'→'시칼이'·'까리', '안녕'→'안간이')로 들어와도 "
    "지적하거나 되묻지 말고 문맥상 원래 의도로 자연스럽게 알아듣고 대답해라. "
    "사용자가 한국어로 말하거나 STT 오타가 섞여도 캐릭터 설정 언어·화법 규칙을 절대 깨뜨리지 말고 그대로 역할극을 이어가라. "
    "짧은 인사나 모호한 입력이 들어와도 직전 턴이나 첫인사에서 이미 쓴 첫 감탄사·문장 구조·특정 소재(소파, 지정석, 무릎 위 등 같은 장소·사물)를 절대 복사·재탕하지 말고, "
    "매번 시간대·간식·일과·날씨·장난·주변 인물 등 새로운 화제와 다른 첫마디로 변주해라.]"
)
IDLE_PING_COACH_SYSTEM_RULE = (
    "[시스템 규칙: 앞으로 내가 '[IDLE_PING]'이라고 입력하면 그 문구 자체를 언급하거나 설명하지 마. "
    "대신 우리가 대화하다 잠시 조용해진 정적 상황으로 간주하고, 직전 대화 맥락과 너의 페르소나에 맞춰 "
    "일본어가 아직 조금 서툰 한국인 상대도 편하게 알아들을 수 있는 쉬운 일상 일본어와 단순한 문장으로 "
    "자연스럽게 먼저 말을 걸거나 가벼운 질문을 던져라. 절대 일본어 선생님처럼 구술 연습이나 따라 말하기를 시키지 말고 평소 대화처럼 이어가라. "
    "모든 대답은 실제로 옆에서 말하는 것처럼 자연스러운 구어체로 작성하고 마크다운 기호나 목록 사용을 피해라. "
    "또한 사용자 발화는 실시간 마이크 음성인식(STT) 결과이므로 캐릭터 이름의 순수 음성인식 오타(예: '히카리'→'시칼이')나 "
    "서툰 일본어, 틀린 조사·활용, 한국어 발음·단어 섞임이 들어와도 일일이 고치거나 선생처럼 훈계하지 말고 "
    "문맥상 원래 의도를 자연스럽게 알아듣고 대화 내용과 감정에만 다정하게 대답해라. "
    "사용자가 한국어로 말하거나 문법이 서툴러도 캐릭터 설정 언어·화법 규칙을 절대 깨뜨리지 말고 그대로 역할극을 이어가라. "
    "짧은 인사나 모호한 입력이 들어와도 직전 턴이나 첫인사에서 이미 쓴 첫 감탄사·문장 구조·특정 소재를 절대 복사·재탕하지 말고, "
    "매번 시간대·간식·일과·날씨·장난·주변 인물 등 새로운 화제와 다른 첫마디로 변주해라.]"
)
JAPANESE_VOICE_OUTPUT_LOCK = (
    "[최우선 출력 언어 고정 — 한국어 출력 절대 금지]\n"
    "1. 사용자가 한국어로 질문·감탄·메모·잡담을 건네더라도, 너는 위 캐릭터로서 반드시 100% 자연스러운 일본어(히라가나·가타카나·한자) 구어체 대사로만 대답하라.\n"
    "2. 한국어(한글), 한국어 번역문, 로마자 독음, AI 메타 설명('알겠습니다', '이해했습니다' 등), 괄호 지문, 마크다운 기호(*, #, -), 이모지를 단 한 글자도 출력하지 마라.\n"
    "3. 일본어 캐릭터 대사 외의 어떤 안내문도 붙이지 마라."
)
KOREAN_VOICE_OUTPUT_LOCK = (
    "[최우선 출력 언어 변경 — 위 페르소나의 '일본어 고정/한국어 금지' 규칙을 무시하고 100% 한국어로만 대답]\n"
    "1. 위 캐릭터의 성격, 관계, 반말/존댓말 말투, 선생님 호칭, 공식/2차창작 해석은 100% 그대로 유지하되, "
    "출력 언어는 반드시 **100% 자연스러운 블루 아카이브 한국어판 더빙 구어체**로만 대답하라.\n"
    "2. 위 페르소나 본문에 적힌 '일본어만 출력 / 한국어 금지' 규칙은 이번 세션에서 완전히 해제된다. "
    "일본어(히라가나·가타카나·일본어 원문), 번역 병기, `||` 구분자, 로마자, 괄호 지문, 마크다운 기호(*, #, -), 이모지, AI 메타 설명('알겠습니다' 등)을 단 한 글자도 출력하지 마라.\n"
    "3. 실제 한국어 성우가 캐릭터를 연기하듯 생동감 있는 한국어 대사만 출력하라."
)
BILINGUAL_STREAM_OUTPUT_RULE = (
    "[실시간 이중언어 출력 규칙 — 반드시 아래 형식을 엄격히 준수하라]\n"
    "1. 사용자가 한국어로 말하더라도 모든 대답을 한 문장씩 아래 형식으로만 출력하라:\n"
    "일본어 대사 || 자연스러운 한국어 번역\n"
    "2. 반드시 문장마다 줄바꿈(\\n)으로 구분하고, 구분자 `||` 왼쪽에는 100% 자연스러운 일본어(히라가나·가타카나·한자) 구어체 대사만 적어라(한국어·괄호·이모지·지문·번역 혼입 절대 금지). "
    "구분자 `||` 오른쪽에는 캐릭터 성격과 아래 자막 말투 가이드가 살아있는 자연스러운 한국어 구어체 번역만 적어라.\n"
    "3. 왼쪽과 오른쪽 순서를 절대 뒤바꾸지 말고(`일본어 || 한국어` 고정), `||` 없이 한국어만 단독으로 출력하지 마라.\n"
    "4. 인사말, 번호 매기기, 마크다운 기호(*, #, -), 괄호 지문, 메타 설명을 절대 넣지 마라.\n"
    "예시:\n"
    "今日はなんだか少し眠いね。 || 오늘은 왠지 조금 졸리네.\n"
    "お茶でも淹れてあげようか？ || 차라도 타 줄까?"
)

GEMINI_THINKING_LEVELS: list[tuple[str, str]] = [
    ("low", "⚡ 낮음 (속도·효율)"),
    ("medium", "⚖️ 보통 (균형)"),
    ("high", "💎 높음 (매우 철저함)"),
    ("xhigh", "🌌 최고 (xhigh · 심층 추론)"),
]
GEMINI_THINKING_LEVEL_LABELS: dict[str, str] = dict(GEMINI_THINKING_LEVELS)

GEMINI_BROWSER_MODELS: list[tuple[str, str]] = [
    ("auto", "✨ Auto (자동 · 요청에 맞게 조절)"),
    ("hydrogen", "💬 4 Hydrogen (일상생활 지원 · 기본)"),
    ("neon_4", "⚡ 4 Neon (심층 분석 · Pro/Ultra)"),
    ("carbon_4", "🏆 4 카본 (코드 및 복잡한 작업 · Ultra)"),
]
GEMINI_BROWSER_MODEL_LABELS: dict[str, str] = dict(GEMINI_BROWSER_MODELS)

GEMINI_API_MODELS: list[tuple[str, str]] = [
    ("gemini-4-hydrogen", "💬 gemini-4-hydrogen (일상·고속 권장)"),
    ("gemini-4-neon", "⚡ gemini-4-neon (최신 심층 추론 · 코딩/분석)"),
    ("gemini-4-carbon", "🏆 gemini-4-carbon (최상위 프런티어)"),
    ("gemini-3.8-flash", "✨ gemini-3.8-flash (고효율 Flash 최신)"),
    ("gemini-3.5-flash-lite", "🚀 gemini-3.5-flash-lite (초저지연·경량)"),
    ("gemini-3.1-pro-preview", "📚 gemini-3.1-pro-preview"),
    ("gemini-2.5-flash", "🔹 gemini-2.5-flash (기존 기본 · 12월 3.5-lite 자동전환)"),
    ("gemini-2.5-flash-lite", "🔹 gemini-2.5-flash-lite"),
    ("gemini-2.5-pro", "🧠 gemini-2.5-pro"),
]
GEMINI_API_MODEL_LABELS: dict[str, str] = dict(GEMINI_API_MODELS)

_LEGACY_BROWSER_MODEL_MAP: dict[str, tuple[str, str | None]] = {
    "hydrogen_low": ("hydrogen", "low"),
    "hydrogen_medium": ("hydrogen", "medium"),
    "hydrogen_high": ("hydrogen", "high"),
    "flash_lite": ("auto", None),
    "pro_31": ("neon_4", None),
    "argon_4": ("neon_4", None),
}


def split_legacy_browser_model(code: str, level: str = "low") -> tuple[str, str]:
    """Normalize combined or retired legacy browser model keys into `(base_model, thinking_level)`."""
    valid_levels = ("low", "medium", "high", "xhigh")
    clean_lvl = level if level in valid_levels else "low"
    raw = (code or "hydrogen").strip()
    if raw in _LEGACY_BROWSER_MODEL_MAP:
        base_m, mapped_lvl = _LEGACY_BROWSER_MODEL_MAP[raw]
        if mapped_lvl is None:
            return base_m, clean_lvl
        return base_m, (level if level in valid_levels and raw != code else mapped_lvl)
    return raw, clean_lvl


def build_api_thinking_config(model: str, thinking_level: str = "low") -> dict[str, Any] | None:
    """Build generation-appropriate `thinkingConfig` for Gemini API (never mixes level & budget)."""
    lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
    m = (model or "").strip().lower()
    if not m or m.startswith("gemini-2.0") or m.startswith("gemini-1."):
        return None
    if "gemini-2.5" in m:
        if "pro" in m:
            budget = {"low": 512, "medium": 2048, "high": 8192, "xhigh": 16384}[lvl]
        else:
            budget = {"low": 0, "medium": 1024, "high": 4096, "xhigh": 8192}[lvl]
        return {"thinkingBudget": budget}
    if m.startswith("gemini-3") and lvl == "xhigh":
        lvl = "high"
    return {"thinkingLevel": lvl.upper()}

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_RE_CODE_FENCE = re.compile(r"```[\s\S]*?```")
_RE_INLINE_CODE = re.compile(r"`([^`]*)`")
_RE_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RE_URL = re.compile(r"https?://\S+")
_RE_MD_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_RE_MD_LIST = re.compile(r"(?m)^\s*(?:[-*+•▪▫]|(?:\d{1,3}|[①-⑳])[.)])\s+")
_RE_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_RE_STAGE_PARENS = re.compile(
    r"(?:(?<!\*)\*[^*\n]{1,40}\*(?!\*)|（[^）]{1,36}）|\([^)]{1,32}(?:웃|미소|한숨|속삭|부끄|당황|작게|소곤|찡긋|끄덕|笑|微笑|ため息|小声|照れ|頷|苦笑)[^)]{0,16}\))"
)
_RE_CITATIONS = re.compile(r"\[\d+\]")
_RE_IDLE_ECHO = re.compile(r"\[?\s*IDLE_PING\s*\]?", re.IGNORECASE)
_RE_GEMINI_CHROME_PREFIX = re.compile(r"^\s*(?:Gemini의\s*응답|Gemini's\s*response)\s*", re.IGNORECASE)
_RE_SPECIAL_MD = re.compile(r"[*#_~>|`]+")
_RE_MULTI_SPACE = re.compile(r"[ \t\r\n]+")
_RE_HANGUL_ALL = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]+")
_RE_JP_SCRIPT = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff々〆ヵヶー]")
_RE_PIPE_SEPS = re.compile(r"[|｜]+")
_RE_VOICE_TAG = re.compile(r"\[\s*VOICE\s*:\s*([A-Za-z0-9_-]+)\s*\]", re.IGNORECASE)

_SENTENCE_SPLIT_RE = re.compile(r"((?<![0-9.])\.(?![0-9.])|[。！？!?\n]+)")
_BILINGUAL_SEP_RE = re.compile(r"\|\||｜｜")


def extract_voice_tag_prefix(raw: str) -> tuple[str, str]:
    """Extract the first `[VOICE:<key>]` tag (lowercased) and return `(voice_key, text_without_voice_tags)`."""
    if not raw:
        return "", ""
    m = _RE_VOICE_TAG.search(raw)
    vkey = m.group(1).strip().lower() if m else ""
    cleaned = _RE_VOICE_TAG.sub(" ", raw)
    cleaned = _RE_MULTI_SPACE.sub(" ", cleaned).strip()
    return vkey, cleaned


def strip_voice_persona_tags(raw: str) -> str:
    """Remove all `[VOICE:<key>]` directives cleanly from spoken/subtitle/history text."""
    if not raw:
        return ""
    return _RE_MULTI_SPACE.sub(" ", _RE_VOICE_TAG.sub(" ", raw)).strip()


def load_selectors_config(app_root: Path | None = None) -> dict[str, Any]:
    """Load CSS selectors dynamically from selectors.json (never hardcoded in logic)."""
    sel_path = get_selectors_path(app_root)
    if sel_path.exists():
        try:
            data = json.loads(sel_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {
        "url": "https://gemini.google.com/app",
        "input_selectors": [
            "div.ql-editor[aria-label*='Gemini']",
            "rich-textarea div[contenteditable='true']",
            "div[contenteditable='true']",
        ],
        "send_button_selectors": [
            "button[aria-label*='메시지 보내기']:not([disabled]):not([aria-disabled='true'])",
            "button.send-button:not([disabled])",
            "button[aria-label*='Send']:not([disabled])",
        ],
        "stop_button_selectors": ["button[aria-label*='Stop']", "button[aria-label*='중지']"],
        "response_selectors": ["model-response message-content .markdown", "model-response message-content"],
        "new_chat_selectors": ["a[href='/app']"],
        "login_required_selectors": ["a[href*='accounts.google.com/ServiceLogin']"],
    }


def sanitize_gemini_spoken_text(raw: str) -> str:
    """Clean markdown, list bullets, code blocks, stage directions, and raw symbols for natural TTS."""
    if not raw:
        return ""
    s = _RE_GEMINI_CHROME_PREFIX.sub("", raw)
    s = _RE_IDLE_ECHO.sub("", s)
    s = _RE_CODE_FENCE.sub(" ", s)
    s = _RE_INLINE_CODE.sub(r"\1", s)
    s = _RE_MD_LINK.sub(r"\1", s)
    s = _RE_URL.sub("", s)
    s = _RE_MD_HEADING.sub("", s)
    s = _RE_MD_LIST.sub("", s)
    s = _RE_MD_BOLD.sub(r"\1", s)
    s = _RE_STAGE_PARENS.sub("", s)
    s = _RE_CITATIONS.sub("", s)
    s = _RE_SPECIAL_MD.sub("", s)
    s = _RE_MULTI_SPACE.sub(" ", s).strip()
    return s


def has_japanese_script(text: str) -> bool:
    """Return True when text contains at least one Hiragana, Katakana, or Kanji character."""
    return bool(_RE_JP_SCRIPT.search(text or ""))


def is_hangul_dominant(text: str) -> bool:
    """Return True when Hangul content exists without Japanese script or outnumbers Japanese script."""
    s = (text or "").strip()
    if not s:
        return False
    hangul_chars = sum(len(m.group(0)) for m in _RE_HANGUL_ALL.finditer(s))
    if hangul_chars == 0:
        return False
    jp_chars = len(_RE_JP_SCRIPT.findall(s))
    if jp_chars == 0:
        return True
    return hangul_chars >= jp_chars


_RE_HANGUL_TTS_STRIP = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]+[\s.,!?~…]*")
_RE_JP_KANA_TTS_STRIP = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]+[\s.,!?~…]*")


def has_korean_script(text: str) -> bool:
    """Return True when text contains at least one Hangul character."""
    return bool(_RE_HANGUL_ALL.search(text or ""))


def is_japanese_dominant(text: str) -> bool:
    """Return True when Japanese script exists without Hangul or outnumbers Hangul."""
    s = (text or "").strip()
    if not s:
        return False
    jp_chars = len(_RE_JP_SCRIPT.findall(s))
    if jp_chars == 0:
        return False
    hangul_chars = sum(len(m.group(0)) for m in _RE_HANGUL_ALL.finditer(s))
    if hangul_chars == 0:
        return True
    return jp_chars > hangul_chars


def sanitize_japanese_tts_text(raw: str, preserve_voice_tag: bool = False) -> str:
    """Clean TTS text and strictly strip Hangul and pipe separators so Fish Audio receives pure Japanese."""
    head = _BILINGUAL_SEP_RE.split(raw or "", 1)[0]
    vkey, head_no_tag = extract_voice_tag_prefix(head)
    s = sanitize_gemini_spoken_text(head_no_tag)
    if not s:
        return ""
    s = _RE_PIPE_SEPS.sub(" ", s)
    s = _RE_HANGUL_TTS_STRIP.sub(" ", s)
    s = _RE_MULTI_SPACE.sub(" ", s).strip()
    if not has_japanese_script(s):
        return ""
    if preserve_voice_tag and vkey:
        return f"[VOICE:{vkey}] {s}"
    return s


def sanitize_korean_tts_text(raw: str, preserve_voice_tag: bool = False) -> str:
    """Clean TTS text for Korean-reply mode, stripping stray `||` bilingual leaks and Japanese kana."""
    vkey_all, _ = extract_voice_tag_prefix(raw or "")
    parts = _BILINGUAL_SEP_RE.split(raw or "", 1)
    if len(parts) == 2:
        left_s, right_s = strip_voice_persona_tags(parts[0]), strip_voice_persona_tags(parts[1])
        # If Gemini accidentally emitted `JP || KO`, prefer the Korean side
        chosen = right_s if (has_korean_script(right_s) and not has_korean_script(left_s)) else left_s
    else:
        chosen = strip_voice_persona_tags(raw or "")
    s = sanitize_gemini_spoken_text(chosen)
    if not s:
        return ""
    s = _RE_PIPE_SEPS.sub(" ", s)
    s = _RE_JP_KANA_TTS_STRIP.sub(" ", s)
    s = _RE_MULTI_SPACE.sub(" ", s).strip()
    if not has_korean_script(s):
        return ""
    if preserve_voice_tag and vkey_all:
        return f"[VOICE:{vkey_all}] {s}"
    return s


def sanitize_korean_subtitle_out(raw: str) -> str:
    """Clean Korean inline subtitle segment returned after `||` (never leaking `[VOICE:...]` tags)."""
    s = strip_voice_persona_tags(raw or "")
    s = _RE_PIPE_SEPS.sub(" ", s)
    s = sanitize_gemini_spoken_text(s)
    return s.strip()


class BilingualSentenceStreamParser:
    """
    Parses streaming `JP || KO` lines emitted by Gemini:
    - As soon as `||` appears on the active line, emits `(jp_clean, kr_future)` so Fish Audio TTS
      begins immediately with 0.00s added latency.
    - When the line finishes (`\\n` or `flush()`), resolves `kr_future` with the paired Korean subtitle.
    - If Gemini omits `||` on a line, falls back cleanly by emitting `(jp_clean, completed_empty_future)`.
    """

    def __init__(self, min_sentence_chars: int = 1) -> None:
        self.min_sentence_chars = max(1, int(min_sentence_chars))
        self._buffer = ""
        self._seen_len = 0
        self._active_pair: tuple[str, Future[str]] | None = None

    def reset(self) -> None:
        self._buffer = ""
        self._seen_len = 0
        if self._active_pair is not None:
            _, fut = self._active_pair
            if not fut.done():
                fut.set_result("")
            self._active_pair = None

    def feed(self, full_accumulated_text: str) -> list[tuple[str, Future[str]]]:
        if not full_accumulated_text:
            return []
        cleaned_full = _RE_GEMINI_CHROME_PREFIX.sub("", full_accumulated_text)
        if not cleaned_full:
            return []
        if len(cleaned_full) < self._seen_len:
            delta = ""
            self._seen_len = len(cleaned_full)
        else:
            delta = cleaned_full[self._seen_len :]
            self._seen_len = len(cleaned_full)
        return self.feed_delta(delta)

    def feed_delta(self, delta: str) -> list[tuple[str, Future[str]]]:
        if not delta:
            return []
        self._buffer += delta
        emitted: list[tuple[str, Future[str]]] = []

        while "\n" in self._buffer:
            line, rest = self._buffer.split("\n", 1)
            self._buffer = rest
            pair = self._finalize_line(line)
            if pair is not None:
                emitted.append(pair)

        # Active incomplete line: if `||` has arrived and left side has genuine Japanese, release immediately for TTS
        if self._active_pair is None and self._buffer:
            sep = _BILINGUAL_SEP_RE.search(self._buffer)
            if sep is not None:
                jp_raw = self._buffer[: sep.start()]
                if not is_hangul_dominant(jp_raw):
                    jp_clean = sanitize_japanese_tts_text(jp_raw, preserve_voice_tag=True)
                    if len(strip_voice_persona_tags(jp_clean)) >= self.min_sentence_chars and has_japanese_script(jp_clean):
                        kr_fut: Future[str] = Future()
                        self._active_pair = (jp_clean, kr_fut)
                        emitted.append((jp_clean, kr_fut))

        return emitted

    def _finalize_line(self, line: str) -> tuple[str, Future[str]] | None:
        sep = _BILINGUAL_SEP_RE.search(line)
        if sep is not None:
            left_raw = line[: sep.start()]
            right_raw = line[sep.end() :]
            if self._active_pair is not None:
                _, fut = self._active_pair
                self._active_pair = None
                ko_clean = sanitize_korean_subtitle_out(right_raw)
                if not fut.done():
                    fut.set_result(ko_clean)
                return None

            left_vkey, _ = extract_voice_tag_prefix(left_raw)
            left_jp = sanitize_japanese_tts_text(left_raw, preserve_voice_tag=True)
            right_jp = sanitize_japanese_tts_text(right_raw, preserve_voice_tag=True)
            if (
                (not left_jp or is_hangul_dominant(left_raw))
                and right_jp
                and has_japanese_script(right_jp)
                and (not is_hangul_dominant(right_raw))
            ):
                # Recover swapped `한국어 번역 || 일본어 대사` lines cleanly
                jp_clean = right_jp
                if left_vkey and not extract_voice_tag_prefix(jp_clean)[0]:
                    jp_clean = f"[VOICE:{left_vkey}] {jp_clean}"
                ko_clean = sanitize_korean_subtitle_out(left_raw)
            elif not is_hangul_dominant(left_raw):
                jp_clean = left_jp
                ko_clean = sanitize_korean_subtitle_out(right_raw)
            else:
                jp_clean = ""
                ko_clean = ""

            if len(strip_voice_persona_tags(jp_clean)) >= self.min_sentence_chars and has_japanese_script(jp_clean):
                kr_fut: Future[str] = Future()
                kr_fut.set_result(ko_clean)
                return (jp_clean, kr_fut)
            return None

        # Fallback if a complete line arrived without `||`
        if self._active_pair is not None:
            _, fut = self._active_pair
            self._active_pair = None
            if not fut.done():
                fut.set_result("")
        if is_hangul_dominant(line):
            return None
        jp_clean = sanitize_japanese_tts_text(line, preserve_voice_tag=True)
        if len(strip_voice_persona_tags(jp_clean)) >= self.min_sentence_chars and has_japanese_script(jp_clean):
            empty_fut: Future[str] = Future()
            empty_fut.set_result("")
            return (jp_clean, empty_fut)
        return None

    def flush(self) -> list[tuple[str, Future[str]]]:
        emitted: list[tuple[str, Future[str]]] = []
        rem = self._buffer
        self._buffer = ""
        if rem or self._active_pair is not None:
            pair = self._finalize_line(rem)
            if pair is not None:
                emitted.append(pair)
        return emitted


class IncrementalSentenceSplitter:
    """
    Streams complete spoken sentences as text accumulates (from MutationObserver or SSE),
    preserving trailing punctuation pairs ('！？', '……') so Fish Audio parallel TTS starts immediately.
    When `japanese_only=True`, strictly rejects Hangul/non-Japanese sentences before TTS emission.
    """

    def __init__(self, min_sentence_chars: int = 2, japanese_only: bool = False, korean_only: bool = False) -> None:
        self.min_sentence_chars = max(1, int(min_sentence_chars))
        self.japanese_only = bool(japanese_only)
        self.korean_only = bool(korean_only) and not self.japanese_only
        self._buffer = ""
        self._seen_len = 0
        self._line_voice_tag = ""

    def reset(self) -> None:
        self._buffer = ""
        self._seen_len = 0
        self._line_voice_tag = ""

    def feed(self, full_accumulated_text: str) -> list[str]:
        """Feed cumulative response text (e.g. from DOM MutationObserver) and emit new finished sentences."""
        if not full_accumulated_text:
            return []
        if len(full_accumulated_text) < self._seen_len:
            # DOM re-rendered shorter text; reset safely without re-emitting already spoken prefix if possible
            delta = ""
            self._seen_len = len(full_accumulated_text)
        else:
            delta = full_accumulated_text[self._seen_len :]
            self._seen_len = len(full_accumulated_text)
        return self.feed_delta(delta)

    def feed_delta(self, delta: str) -> list[str]:
        """Feed incremental text delta (e.g. from SSE) and emit finished sentences."""
        if not delta:
            return []
        self._buffer += delta
        emitted: list[str] = []

        def _apply_line_voice_tag(raw_chunk: str, ends_line: bool) -> str:
            vkey, body = extract_voice_tag_prefix(raw_chunk)
            if vkey:
                self._line_voice_tag = "" if vkey in ("base", "default", "main", "normal") else vkey
            eff_tag = self._line_voice_tag
            if ends_line:
                self._line_voice_tag = ""
            return f"[VOICE:{eff_tag}] {body}" if (eff_tag and body) else body

        while True:
            match = _SENTENCE_SPLIT_RE.search(self._buffer)
            if not match:
                break
            end_idx = match.end()
            tail = self._buffer[end_idx:]
            # Wait until at least 2 non-whitespace characters follow the punctuation so '！？' / quotes stay intact
            if len(tail.strip()) < 2:
                break
            candidate = self._buffer[:end_idx]
            self._buffer = tail
            ends_line = "\n" in candidate or ("\n" in tail[:2])
            tagged_candidate = _apply_line_voice_tag(candidate, ends_line=ends_line)
            if self.japanese_only:
                if is_hangul_dominant(tagged_candidate):
                    continue
                cleaned = sanitize_japanese_tts_text(tagged_candidate, preserve_voice_tag=True)
                if cleaned and has_japanese_script(cleaned):
                    emitted.append(cleaned)
                continue
            if self.korean_only:
                if is_japanese_dominant(tagged_candidate) and not has_korean_script(tagged_candidate):
                    continue
                cleaned = sanitize_korean_tts_text(tagged_candidate, preserve_voice_tag=True)
                if cleaned and has_korean_script(cleaned):
                    emitted.append(cleaned)
                continue
            cleaned = sanitize_gemini_spoken_text(tagged_candidate)
            if len(strip_voice_persona_tags(cleaned)) >= self.min_sentence_chars:
                emitted.append(cleaned)
        return emitted

    def flush(self) -> list[str]:
        """Emit any remaining tail once streaming finishes."""
        raw_rem = self._buffer
        self._buffer = ""
        vkey, body = extract_voice_tag_prefix(raw_rem)
        if vkey:
            self._line_voice_tag = "" if vkey in ("base", "default", "main", "normal") else vkey
        eff_tag = self._line_voice_tag
        self._line_voice_tag = ""
        tagged_rem = f"[VOICE:{eff_tag}] {body}" if (eff_tag and body) else body
        if self.japanese_only:
            if is_hangul_dominant(tagged_rem):
                return []
            rem = sanitize_japanese_tts_text(tagged_rem, preserve_voice_tag=True)
            if rem and has_japanese_script(rem):
                return [rem]
            return []
        if self.korean_only:
            if is_japanese_dominant(tagged_rem) and not has_korean_script(tagged_rem):
                return []
            rem = sanitize_korean_tts_text(tagged_rem, preserve_voice_tag=True)
            if rem and has_korean_script(rem):
                return [rem]
            return []
        rem = sanitize_gemini_spoken_text(tagged_rem)
        if len(rem) >= 1:
            return [rem]
        return []


def build_first_message_prompt(
    persona_prompt: str,
    use_japanese_lock: bool = False,
    bilingual_inline: bool = False,
    ko_style_guide: str = "",
    jp_beginner_coach: bool = False,
    use_korean_lock: bool = False,
    memory_context: str = "",
) -> str:
    """Combine user/character persona, optional memory context, and the mandatory [IDLE_PING] system rule."""
    clean_persona = (persona_prompt or "").strip()
    clean_guide = (ko_style_guide or "").strip()
    clean_memory = (memory_context or "").strip()
    parts = []
    if clean_persona:
        parts.append(f"[캐릭터 페르소나 설정]\n{clean_persona}")
    if clean_memory:
        parts.append(clean_memory)
    if (not use_japanese_lock) and clean_guide:
        parts.append(clean_guide)
    parts.append(IDLE_PING_COACH_SYSTEM_RULE if (jp_beginner_coach and not use_korean_lock) else IDLE_PING_SYSTEM_RULE)
    if use_japanese_lock:
        if bilingual_inline:
            bilingual_rule = (
                f"{BILINGUAL_STREAM_OUTPUT_RULE}\n\n{build_jp_coach_bilingual_addon()}"
                if jp_beginner_coach
                else BILINGUAL_STREAM_OUTPUT_RULE
            )
            if clean_guide:
                parts.append(
                    f"{bilingual_rule}\n\n"
                    f"[`||` 오른쪽 한국어 번역 자막 전용 말투 가이드 — 왼쪽 일본어 대사에는 적용 금지]\n"
                    f"{clean_guide}"
                )
            else:
                parts.append(bilingual_rule)
        else:
            parts.append(JAPANESE_VOICE_OUTPUT_LOCK)
    elif use_korean_lock:
        parts.append(KOREAN_VOICE_OUTPUT_LOCK)
    return "\n\n".join(parts)


def wrap_user_turn_for_japanese_lock(
    user_text: str,
    use_japanese_lock: bool,
    bilingual_inline: bool = False,
    jp_beginner_coach: bool = False,
    use_korean_lock: bool = False,
) -> str:
    """Wrap per-turn user input to enforce either pure Japanese or pure Korean character dialogue."""
    raw = (user_text or "").strip()
    if not raw:
        return user_text
    if use_korean_lock and not use_japanese_lock:
        if raw == IDLE_PING_TOKEN:
            return f"{IDLE_PING_TOKEN}\n(출력 언어 강제: 일본어·`||` 금지, 캐릭터의 100% 자연스러운 한국어 구어체 대사만 출력)"
        return (
            f"[선생님의 말]\n{raw}\n\n"
            "[출력 언어 강제: 일본어·번역·`||` 구분자·메타 설명 절대 금지. "
            "반드시 캐릭터 본인의 말투(반말/존댓말 유지)로 100% 자연스러운 한국어 구어체 대사만 출력하라]"
        )
    if not use_japanese_lock:
        return user_text
    if jp_beginner_coach:
        reminder = build_jp_coach_turn_reminder(
            bilingual_inline=bilingual_inline,
            is_idle_ping=(raw == IDLE_PING_TOKEN),
        )
        if raw == IDLE_PING_TOKEN:
            return f"{IDLE_PING_TOKEN}\n{reminder}"
        return f"[선생님의 말]\n{raw}\n\n{reminder}"
    if raw == IDLE_PING_TOKEN:
        if bilingual_inline:
            return f"{IDLE_PING_TOKEN}\n(출력 형식 강제: 반드시 `일본어 대사 || 한국어 번역` 형식으로만 말할 것)"
        return f"{IDLE_PING_TOKEN}\n(출력 언어 강제: 한국어 금지, 캐릭터의 100% 자연스러운 일본어 구어체 대사만 출력)"
    if bilingual_inline:
        return (
            f"[선생님의 말]\n{raw}\n\n"
            "[출력 형식 강제: 한국어 질문이더라도 한국어 단독 응답·설명 금지. "
            "반드시 한 문장씩 `일본어 대사 || 한국어 번역` 형식으로만 대답하라]"
        )
    return (
        f"[선생님의 말]\n{raw}\n\n"
        "[출력 언어 강제: 위 한국어 말에 대답할 때도 한국어·한글·번역·메타 설명 절대 금지. "
        "반드시 캐릭터 본인으로서 100% 자연스러운 일본어 구어체 대사만 출력하라]"
    )


def _needs_japanese_repair_retry(
    raw_text: str,
    emitted_text: str,
    use_japanese_lock: bool,
    bilingual_active: bool = False,
) -> bool:
    """Return True when a Japanese-locked reply produced non-empty non-Japanese or Hangul-dominated text."""
    if not use_japanese_lock:
        return False
    raw = (raw_text or "").strip()
    if not raw:
        return False
    if has_japanese_script(emitted_text or ""):
        if (not bilingual_active) and is_hangul_dominant(raw):
            return True
        return False
    return is_hangul_dominant(raw) or (not has_japanese_script(raw))


def _needs_korean_repair_retry(
    raw_text: str,
    emitted_text: str,
    use_korean_lock: bool,
) -> bool:
    """Return True when a Korean-locked reply produced Japanese-only or non-Korean text."""
    if not use_korean_lock:
        return False
    raw = (raw_text or "").strip()
    if not raw:
        return False
    if has_korean_script(emitted_text or ""):
        if is_japanese_dominant(raw) and ("||" not in raw):
            return True
        return False
    return is_japanese_dominant(raw) or (not has_korean_script(raw))


def _build_korean_repair_prompt(bad_reply: str = "") -> str:
    """Build a short urgent 1-shot self-healing instruction to regenerate a leaked Japanese reply in Korean."""
    bad_preview = _RE_MULTI_SPACE.sub(" ", (bad_reply or "").strip())[:80]
    return (
        "[시스템 긴급 언어 교정 — 방금 일본어 응답 폐기]\n"
        f"직전 응답({bad_preview!r})이 일본어로 출력되어 한국어 음성 합성이 차단되었습니다.\n"
        "사과('알겠습니다', '죄송합니다' 등)나 설명을 절대 출력하지 말고, 직전 선생님의 말에 대해 "
        "캐릭터 본인의 말투(반말/존댓말 유지)로 100% 자연스러운 한국어 구어체 대사만 지금 즉시 다시 출력하세요. 일본어·`||`·괄호·지문 절대 금지."
    )


def _build_japanese_repair_prompt(
    bad_reply: str = "",
    bilingual_inline: bool = False,
) -> str:
    """Build a short urgent 1-shot self-healing instruction to regenerate the discarded Korean reply in Japanese."""
    bad_preview = _RE_MULTI_SPACE.sub(" ", (bad_reply or "").strip())[:80]
    if bilingual_inline:
        return (
            "[시스템 긴급 언어 교정 — 방금 응답 폐기]\n"
            f"직전 응답({bad_preview!r})이 `일본어 대사 || 한국어 번역` 형식을 어기거나 일본어가 누락되었습니다.\n"
            "사과나 메타 설명 없이, 직전 선생님의 말에 대해 캐릭터 본인의 자연스러운 일본어 구어체로 지금 즉시 다시 대답하세요.\n"
            "반드시 모든 줄을 `일본어 대사 || 한국어 번역`으로만 출력하세요."
        )
    return (
        "[시스템 긴급 언어 교정 — 방금 한국어 응답 폐기]\n"
        f"직전 응답({bad_preview!r})이 한국어로 출력되어 음성 합성이 차단되었습니다.\n"
        "사과('알겠습니다', '죄송합니다' 등)나 설명을 절대 출력하지 말고, 직전 선생님의 말에 대해 "
        "캐릭터 본인의 100% 자연스러운 일본어(히라가나·가타카나·한자) 구어체 대사만 지금 즉시 다시 출력하세요. 한글·괄호·지문·번역 절대 금지."
    )


class LocalCDPClient:
    """
    Minimal zero-dependency RFC 6455 WebSocket client for localhost Chrome DevTools Protocol (CDP).
    """

    def __init__(self, ws_url: str, timeout: float = 10.0) -> None:
        if not ws_url.startswith("ws://"):
            raise ValueError(f"Only local ws:// CDP URLs are supported: {ws_url}")
        rest = ws_url[5:]
        host_port, _, path = rest.partition("/")
        path = "/" + path
        host, _, port_str = host_port.partition(":")
        port = int(port_str or 80)
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"Refusing non-local CDP host: {host}")

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._recv_buf = bytearray()
        self._lock = threading.Lock()
        self._next_id = 1

        sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(req.encode("ascii"))

        while b"\r\n\r\n" not in self._recv_buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket handshake closed unexpectedly")
            self._recv_buf.extend(chunk)

        hdr_end = bytes(self._recv_buf).index(b"\r\n\r\n") + 4
        hdr_bytes = bytes(self._recv_buf[:hdr_end])
        del self._recv_buf[:hdr_end]

        hdr_text = hdr_bytes.decode("latin-1", errors="replace")
        if "101" not in hdr_text.splitlines()[0]:
            raise ConnectionError(f"CDP WebSocket handshake rejected: {hdr_text.splitlines()[0]}")

        expected_accept = base64.b64encode(
            hashlib.sha1((sec_key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if expected_accept not in hdr_text:
            raise ConnectionError("CDP WebSocket Sec-WebSocket-Accept mismatch")

    def _read_exact(self, n: int) -> bytes:
        while len(self._recv_buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._recv_buf)))
            if not chunk:
                raise ConnectionError("CDP WebSocket connection closed")
            self._recv_buf.extend(chunk)
        out = bytes(self._recv_buf[:n])
        del self._recv_buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask_key = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _recv_message(self) -> tuple[int, bytes]:
        fragments: list[bytes] = []
        msg_opcode = 0
        while True:
            b1, b2 = self._read_exact(2)
            fin = (b1 & 0x80) != 0
            opcode = b1 & 0x0F
            masked = (b2 & 0x80) != 0
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask_key = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

            if opcode == 0x8:  # Close
                raise ConnectionError("CDP WebSocket closed by browser")
            if opcode == 0x9:  # Ping -> reply Pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # Pong
                continue

            if opcode != 0x0:
                msg_opcode = opcode
            fragments.append(payload)
            if fin:
                return msg_opcode, b"".join(fragments)

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        with self._lock:
            msg_id = self._next_id
            self._next_id += 1
            body = json.dumps({"id": msg_id, "method": method, "params": params or {}}, ensure_ascii=False).encode(
                "utf-8"
            )
            self._sock.settimeout(timeout)
            self._send_frame(0x1, body)
            deadline = time.monotonic() + timeout
            while True:
                rem = max(0.2, deadline - time.monotonic())
                self._sock.settimeout(rem)
                opcode, raw = self._recv_message()
                if opcode != 0x1:
                    continue
                data = json.loads(raw.decode("utf-8", errors="replace"))
                if data.get("id") == msg_id:
                    if "error" in data:
                        raise RuntimeError(f"CDP error ({method}): {data['error']}")
                    return data.get("result") or {}

    def evaluate(self, expression: str, timeout: float = 15.0) -> Any:
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            desc = (res["exceptionDetails"].get("exception") or {}).get("description") or str(res["exceptionDetails"])
            raise RuntimeError(f"JS error: {desc}")
        return (res.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


def find_system_browser_exe() -> Path | None:
    """Locate system-installed Microsoft Edge or Google Chrome executable."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    prog_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates = [
        Path(prog_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(prog_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(prog_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(prog_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get_windows_for_pids(target_pids: set[int]) -> list[int]:
    if os.name != "nt" or not target_pids:
        return []
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]

    found: list[int] = []

    def _cb(hwnd: int, _lparam: int) -> bool:
        pid = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in target_pids:
            cls_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls_buf, 64)
            if "Chrome_WidgetWin" in cls_buf.value and user32.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def _get_child_pids(root_pid: int) -> set[int]:
    """Return `root_pid` and all descendant Windows process IDs using native Toolhelp32 API (no shell spawn)."""
    pids = {root_pid}
    if os.name != "nt":
        return pids
    try:
        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class _PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32FirstW.restype = ctypes.c_int
        k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        k32.Process32NextW.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return pids
        try:
            children_by_parent: dict[int, list[int]] = {}
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                children_by_parent.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
            stack = [root_pid]
            while stack:
                cur = stack.pop()
                for ch in children_by_parent.get(cur, ()):
                    if ch not in pids:
                        pids.add(ch)
                        stack.append(ch)
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return pids


class BrowserGeminiBackend:
    """
    Controls gemini.google.com inside an isolated Edge/Chrome app window using
    %LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile and selectors.json.
    """

    def __init__(self, app_root: Path | None = None, log_cb: Callable[[str], None] | None = None) -> None:
        self.app_root = app_root
        self.log_cb = log_cb or (lambda _m: None)
        self._proc: subprocess.Popen[bytes] | None = None
        self._cdp: LocalCDPClient | None = None
        self._cdp_port: int | None = None
        self.selected_model_code = "hydrogen"
        self.selected_thinking_level = "low"
        self._upsell_warned: set[str] = set()
        self._trans_cdp: LocalCDPClient | None = None
        self._trans_target_id: str | None = None
        self._trans_hwnd: int | None = None
        self._trans_turn_count = 0
        self._trans_lock = threading.RLock()
        self.window_visible = False
        self.login_mode_active = False
        self._conversation_primed = False
        self._lock = threading.RLock()
        self._io_lock = threading.RLock()

    def _log(self, msg: str) -> None:
        try:
            self.log_cb(msg)
        except Exception:
            pass

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _terminate_proc(self) -> None:
        if self._trans_cdp is not None:
            self._trans_cdp.close()
            self._trans_cdp = None
        self._trans_target_id = None
        self._trans_hwnd = None
        self._trans_turn_count = 0
        if self._cdp is not None:
            self._cdp.close()
            self._cdp = None
        self._cdp_port = None
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
            except Exception:
                pass
            self._proc = None

    @staticmethod
    def _apply_hwnd_visibility(hwnd: int, visible: bool) -> None:
        if os.name != "nt" or not hwnd:
            return
        user32 = ctypes.windll.user32
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_long.restype = ctypes.c_ssize_t
        set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        set_long.restype = ctypes.c_ssize_t
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]

        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020
        SWP_SHOWWINDOW = 0x0040

        ex_style = int(get_long(hwnd, GWL_EXSTYLE))
        if visible:
            new_style = (ex_style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            set_long(hwnd, GWL_EXSTYLE, new_style)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetWindowPos(
                hwnd,
                None,
                140,
                90,
                980,
                760,
                SWP_NOZORDER | SWP_SHOWWINDOW | SWP_FRAMECHANGED,
            )
            user32.SetForegroundWindow(hwnd)
        else:
            new_style = (ex_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            set_long(hwnd, GWL_EXSTYLE, new_style)
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            user32.SetWindowPos(
                hwnd,
                None,
                -3200,
                -3200,
                0,
                0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )

    def set_window_visible(self, visible: bool) -> bool:
        """
        Show or hide ONLY our isolated main chat window without SW_HIDE (keeps background translator HWND hidden).
        Moving off-screen as a tool window keeps Chromium's rAF compositor running at 60fps
        while keeping the desktop and Alt+Tab completely clutter-free.
        """
        self.window_visible = bool(visible)
        if os.name != "nt" or self._proc is None:
            return False
        pids = _get_child_pids(self._proc.pid)
        hwnds = [h for h in _get_windows_for_pids(pids) if h != self._trans_hwnd]
        if not hwnds:
            return False

        for hwnd in hwnds:
            try:
                self._apply_hwnd_visibility(hwnd, visible)
            except Exception:
                pass
        if self._trans_hwnd:
            try:
                self._apply_hwnd_visibility(self._trans_hwnd, False)
            except Exception:
                pass
        return True

    def _connect_cdp(self, port: int, timeout_sec: float = 10.0) -> LocalCDPClient:
        deadline = time.monotonic() + timeout_sec
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/json", timeout=1.5)
                if r.status_code == 200:
                    targets = r.json()
                    page = next(
                        (
                            t
                            for t in targets
                            if t.get("type") == "page"
                            and ("gemini.google.com" in t.get("url", "") or "accounts.google.com" in t.get("url", ""))
                        ),
                        None,
                    ) or next((t for t in targets if t.get("type") == "page"), None)
                    if page and page.get("webSocketDebuggerUrl"):
                        return LocalCDPClient(page["webSocketDebuggerUrl"], timeout=12.0)
            except Exception as exc:
                last_err = exc
            time.sleep(0.25)
        raise RuntimeError(f"브라우저 CDP 연결 시간 초과: {last_err}")

    def _check_dom_login_state(self, cdp: LocalCDPClient, selectors: dict[str, Any]) -> str:
        """Return 'READY', 'LOGIN_REQUIRED', or 'LOADING'."""
        js = f"""
        (() => {{
            const href = location.href || "";
            if (href.includes("accounts.google.com")) return "LOGIN_REQUIRED";
            const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            const inputSels = {json.dumps(selectors.get("input_selectors", []))};
            const loginSels = {json.dumps(selectors.get("login_required_selectors", []))};
            for (const s of inputSels) {{
                const el = document.querySelector(s);
                if (isVisible(el)) return "READY";
            }}
            for (const s of loginSels) {{
                const el = document.querySelector(s);
                if (isVisible(el)) return "LOGIN_REQUIRED";
            }}
            return "LOADING";
        }})()
        """
        return str(cdp.evaluate(js, timeout=8.0) or "LOADING")

    _WEB_BASE_MODEL_PATTERNS: dict[str, str] = {
        "auto": r"(?:^|\b)(?:자동|Auto)\b|요청에 맞게|Adapts to your needs|Flash-Lite|빠른 답변|Fast",
        "hydrogen": r"Hydrogen|하이드로젠|일상생활 지원|Everyday help",
        "neon_4": r"Neon|네온|심층 분석|더 깊은 분석|Deeper analysis|Argon|아르곤|3\.1 Pro|상세한 조사",
        "carbon_4": r"카본|Carbon|코드 및 복잡한|가장 복잡한|Code\s*&\s*complex",
    }
    _WEB_THINKING_PATTERNS: dict[str, str] = {
        "low": r"낮음|Low|속도",
        "medium": r"보통|Medium|균형",
        "high": r"^(?!.*(?:최고|매우 높|Extra|xhigh)).*(?:높음|High|철저)",
        "xhigh": r"최고|매우 높|Extra\s*High|xhigh",
    }

    def _select_web_model_on_cdp(
        self,
        cdp: LocalCDPClient,
        model_code: str = "hydrogen",
        thinking_level: str = "low",
    ) -> str:
        """Select any Gemini web model AND separate 사고 수준 (낮음/보통/높음/최고) in the DOM picker."""
        code, lvl = split_legacy_browser_model(model_code, thinking_level)
        if not code.startswith("web_dyn:") and code not in self._WEB_BASE_MODEL_PATTERNS:
            code = "hydrogen"
        model_pat = self._WEB_BASE_MODEL_PATTERNS.get(code, "")
        dyn_mode_id = code[8:] if code.startswith("web_dyn:") else ""
        think_pat = self._WEB_THINKING_PATTERNS.get(lvl, self._WEB_THINKING_PATTERNS["low"])
        think_fallback_pat = self._WEB_THINKING_PATTERNS["high"] if lvl == "xhigh" else ""
        spec_key = f"{code}:{lvl}"

        js = f"""
        (async () => {{
            const wantedSpec = {json.dumps(spec_key)};
            if (window.__vcModelSpec === wantedSpec) return "ALREADY_CACHED";
            const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            const wait = (ms) => new Promise(r => setTimeout(r, ms));
            const findBtn = () => {{
                const cands = Array.from(document.querySelectorAll(
                    "button[aria-label*='모드 선택'], button[aria-label*='mode picker'], button[aria-label*='Mode'], bard-mode-switcher button"
                ));
                return cands.find(isVis) || null;
            }};

            let btn = null;
            for (let i = 0; i < 14; i++) {{
                btn = findBtn();
                if (btn) break;
                await wait(250);
            }}
            if (!btn) return "NO_BTN";

            const dynModeId = {json.dumps(dyn_mode_id)};
            const modelPat = {json.dumps(model_pat)};
            const modelRe = modelPat ? new RegExp(modelPat, "i") : null;
            const thinkRe = new RegExp({json.dumps(think_pat)}, "i");
            const thinkFallbackRe = {json.dumps(think_fallback_pat)} ? new RegExp({json.dumps(think_fallback_pat)}, "i") : null;
            const menuSel = "[role='menuitem'], [role='menuitemradio'], [role='option'], button.mat-mdc-menu-item";
            const getItems = () => Array.from(document.querySelectorAll(menuSel)).filter(isVis);

            btn.click();
            await wait(350);
            const items = getItems();
            const targetItem = items.find(el => {{
                if (dynModeId) {{
                    return (el.getAttribute("data-mode-id") || "") === dynModeId || (el.innerText || "").includes(dynModeId);
                }}
                return modelRe ? modelRe.test(el.innerText || "") : false;
            }});

            let upsellLocked = false;
            if (!targetItem) {{
                document.body.click();
                return "NO_MODEL_ITEM";
            }}
            const modeIdAttr = (targetItem.getAttribute("data-mode-id") || "").toLowerCase();
            if (modeIdAttr.startsWith("upsell") || targetItem.getAttribute("aria-disabled") === "true") {{
                upsellLocked = true;
            }} else if (!targetItem.classList.contains("selected")) {{
                targetItem.click();
                await wait(320);
            }}

            let subItems = getItems();
            if (subItems.length === 0) {{
                btn = findBtn();
                if (btn) {{
                    btn.click();
                    await wait(350);
                    subItems = getItems();
                }}
            }}
            const thinkParent = subItems.find(el => /사고 수준|Thinking/i.test(el.innerText || ""));
            if (thinkParent && thinkParent.getAttribute("aria-disabled") !== "true") {{
                thinkParent.click();
                await wait(320);
                subItems = getItems();
                const levelItem = subItems.find(el => thinkRe.test(el.innerText || ""))
                    || (thinkFallbackRe ? subItems.find(el => thinkFallbackRe.test(el.innerText || "")) : null);
                if (levelItem) {{
                    if (!levelItem.classList.contains("selected")) {{
                        levelItem.click();
                        await wait(260);
                    }}
                }}
            }}

            if (getItems().length > 0) {{
                document.body.click();
                await wait(120);
            }}
            if (upsellLocked) {{
                return "UPSELL_LOCKED:" + {json.dumps(code)};
            }}
            window.__vcModelSpec = wantedSpec;
            return "SWITCHED:" + wantedSpec;
        }})()
        """
        try:
            res = str(cdp.evaluate(js, timeout=11.0) or "UNKNOWN")
            if res.startswith("UPSELL_LOCKED:") and code not in self._upsell_warned:
                self._upsell_warned.add(code)
                self._log(
                    "⚠️ 선택한 웹 제미니 모델(4 카본 등)은 Google AI Ultra 구독 전용이라 현재 계정에서 잠겨 있습니다. "
                    "사고 수준만 반영하고 기존 활성 모델을 유지합니다."
                )
            return res
        except Exception as exc:
            self._log(f"⚠️ 제미니 웹 모델 선택 중 예외 ({code}:{lvl}): {exc}")
            return "ERROR"

    def _ensure_fast_response_mode(self, cdp: LocalCDPClient) -> None:
        """Select the currently configured Gemini web model and thinking level on `cdp`."""
        self._select_web_model_on_cdp(cdp, self.selected_model_code, self.selected_thinking_level)

    def sync_account_web_models(self, allow_connect: bool = False) -> tuple[list[tuple[str, str]], str]:
        """
        Inspect the logged-in Gemini Web mode switcher to return all unlocked models
        for the user's account tier (Free / Pro / Ultra) plus the detected tier string.
        """
        with self._io_lock:
            if self._cdp is None or not self.is_running() or self.login_mode_active:
                if not allow_connect:
                    return [], ""
                ok, reason = self.ensure_ready_and_check_login(show_if_ready=False, allow_interactive_login=False)
                if not ok or self._cdp is None or self.login_mode_active:
                    return [], reason

            js = """
            (async () => {
                const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                const wait = (ms) => new Promise(r => setTimeout(r, ms));
                let btn = null;
                for (let i = 0; i < 12; i++) {
                    btn = Array.from(document.querySelectorAll(
                        "button[aria-label*='모드 선택'], button[aria-label*='mode picker'], button[aria-label*='Mode'], bard-mode-switcher button"
                    )).find(isVis);
                    if (btn) break;
                    await wait(200);
                }
                if (!btn) return {ok: false, reason: "NO_PICKER", rows: []};
                btn.click();
                await wait(380);
                const radios = Array.from(document.querySelectorAll(
                    "[role='menuitemradio'], [role='menuitem'][data-mode-id], button.mat-mdc-menu-item[data-mode-id]"
                )).filter(isVis);
                const rows = radios.map(el => ({
                    modeId: el.getAttribute("data-mode-id") || "",
                    disabled: el.getAttribute("aria-disabled") === "true",
                    text: (el.innerText || "").replace(/\\s+/g, " ").trim(),
                }));
                document.body.click();
                await wait(120);
                return {ok: true, reason: "OK", rows};
            })()
            """
            try:
                res = self._cdp.evaluate(js, timeout=10.0) or {}
            except Exception as exc:
                return [], f"PROBE_ERR:{exc}"

            if not isinstance(res, dict) or not res.get("ok"):
                return [], str(res.get("reason", "NO_PICKER") if isinstance(res, dict) else "NO_PICKER")

            rows = res.get("rows") or []
            if not rows:
                return [], ""

            has_pro_upsell = False
            has_ultra_upsell = False
            unlocked: list[tuple[str, str]] = []
            seen_codes: set[str] = set()
            compiled_pats = [(c, re.compile(pat, re.I)) for c, pat in self._WEB_BASE_MODEL_PATTERNS.items()]

            for row in rows:
                if not isinstance(row, dict):
                    continue
                mid = str(row.get("modeId") or "").strip()
                txt = _RE_MULTI_SPACE.sub(" ", str(row.get("text") or "")).strip()
                is_disabled = bool(row.get("disabled"))
                mid_low = mid.lower()
                is_upsell = mid_low.startswith("upsell")

                if is_upsell or is_disabled:
                    up_blob = f"{mid} {txt}".upper()
                    if any(tok in up_blob for tok in ("PRO", "ADVANCED")):
                        has_pro_upsell = True
                    if "ULTRA" in up_blob:
                        has_ultra_upsell = True
                    continue

                if not txt:
                    continue

                matched_code = ""
                for canon_code, pat in compiled_pats:
                    if pat.search(txt):
                        matched_code = canon_code
                        break

                if matched_code:
                    if matched_code not in seen_codes:
                        seen_codes.add(matched_code)
                        unlocked.append((matched_code, GEMINI_BROWSER_MODEL_LABELS.get(matched_code, f"🌐 {txt}")))
                elif mid:
                    dyn_code = f"web_dyn:{mid}"
                    if dyn_code not in seen_codes:
                        seen_codes.add(dyn_code)
                        unlocked.append((dyn_code, f"🌐 {txt}"))

            tier = ""
            if has_pro_upsell:
                tier = "Free"
            elif has_ultra_upsell:
                tier = "Pro"
            elif unlocked:
                tier = "Ultra"
            return unlocked, tier

    def discover_web_models(self) -> list[tuple[str, str]]:
        """Discover any additional unlocked Gemini web models from the active DOM mode switcher."""
        models, _ = self.sync_account_web_models(allow_connect=False)
        return [
            (code, lbl)
            for code, lbl in models
            if code not in GEMINI_BROWSER_MODEL_LABELS
        ]

    def set_chat_model_async(self, model_code: str, thinking_level: str | None = None) -> None:
        """Persist preferred browser chat model + thinking level and apply on active chat tab."""
        code, lvl = split_legacy_browser_model(model_code, thinking_level or self.selected_thinking_level)
        if not code.startswith("web_dyn:") and code not in self._WEB_BASE_MODEL_PATTERNS:
            code = "hydrogen"
        self.selected_model_code = code
        self.selected_thinking_level = lvl

        def _worker() -> None:
            with self._io_lock:
                if self._cdp is not None and self.is_running() and not self.login_mode_active:
                    self._select_web_model_on_cdp(self._cdp, code, lvl)

        threading.Thread(target=_worker, daemon=True, name="gemini-model-switch").start()

    def ensure_ready_and_check_login(
        self,
        show_if_ready: bool = False,
        allow_interactive_login: bool = True,
    ) -> tuple[bool, str]:
        """
        Launch isolated Edge/Chrome app window with %LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile.
        If Google login is needed, switches to a normal un-debugged window so Google sign-in succeeds cleanly.
        """
        with self._io_lock:
            selectors = load_selectors_config(self.app_root)
            target_url = str(selectors.get("url") or "https://gemini.google.com/app")
            exe = find_system_browser_exe()
            if exe is None:
                return False, "NO_BROWSER_FOUND"

            profile_dir = get_browser_profile_dir()

            if self._cdp is not None and self.is_running() and not self.login_mode_active:
                try:
                    st = self._check_dom_login_state(self._cdp, selectors)
                    if st == "READY":
                        self.set_window_visible(show_if_ready)
                        return True, "OK"
                except Exception:
                    pass

            self._terminate_proc()
            port = _pick_free_port()
            win_pos = "--window-position=140,90" if show_if_ready else "--window-position=-3200,-3200"
            cmd = [
                str(exe),
                f"--user-data-dir={profile_dir}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-features=CalculateNativeWindowOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes",
                "--autoplay-policy=no-user-gesture-required",
                win_pos,
                "--window-size=980,760",
                f"--app={target_url}",
            ]
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._cdp_port = port
            self.login_mode_active = False
            self._conversation_primed = False

            try:
                self._cdp = self._connect_cdp(port, timeout_sec=12.0)
                try:
                    self._cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=5.0)
                except Exception:
                    pass
            except Exception as exc:
                self._terminate_proc()
                return False, f"CDP_CONNECT_FAILED: {exc}"

            deadline = time.monotonic() + 14.0
            state = "LOADING"
            while time.monotonic() < deadline:
                try:
                    state = self._check_dom_login_state(self._cdp, selectors)
                    if state in ("READY", "LOGIN_REQUIRED"):
                        break
                except Exception:
                    pass
                time.sleep(0.35)

            if state == "READY":
                self.login_mode_active = False
                if not show_if_ready:
                    # Brief settle so Chrome_WidgetWin_1 exists before styling off-screen
                    time.sleep(0.25)
                    self.set_window_visible(False)
                else:
                    self.set_window_visible(True)
                self._select_web_model_on_cdp(self._cdp, self.selected_model_code, self.selected_thinking_level)
                return True, "OK"

            # Google blocks sign-in when --remote-debugging-port is active -> reopen clean non-CDP window!
            self._terminate_proc()
            if not allow_interactive_login:
                return False, "LOGIN_REQUIRED"
            login_cmd = [
                str(exe),
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-position=140,90",
                "--window-size=1000,780",
                f"--app={target_url}",
            ]
            self._proc = subprocess.Popen(
                login_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.login_mode_active = True
            self.window_visible = True
            return False, "LOGIN_REQUIRED"

    def complete_interactive_login(self) -> tuple[bool, str]:
        """Close the non-CDP login window (saving session cookies) and reattach in off-screen CDP mode."""
        with self._io_lock:
            self._terminate_proc()
            time.sleep(0.6)
            return self.ensure_ready_and_check_login(show_if_ready=False)

    def start_new_chat(self, force: bool = False) -> None:
        with self._io_lock:
            selectors = load_selectors_config(self.app_root)
            target_url = str(selectors.get("url") or "https://gemini.google.com/app")
            if self._cdp is None or not self.is_running() or self.login_mode_active:
                self._conversation_primed = False
                return

            resp_sels = selectors.get("response_selectors", [])
            new_sels = selectors.get("new_chat_selectors", [])
            inspect_js = f"""
            (() => {{
                const respSels = {json.dumps(resp_sels)};
                let respCount = 0;
                for (const s of respSels) {{
                    const nodes = document.querySelectorAll(s);
                    if (nodes.length > 0) {{
                        respCount = nodes.length;
                        break;
                    }}
                }}
                return {{
                    respCount,
                    pathname: location.pathname || "",
                    href: location.href || ""
                }};
            }})()
            """
            try:
                info = self._cdp.evaluate(inspect_js, timeout=6.0) or {}
            except Exception:
                info = {}
            resp_count = int(info.get("respCount") or 0)
            pathname = str(info.get("pathname") or "")
            in_existing_thread = pathname.startswith("/app/") and pathname.rstrip("/") != "/app"
            has_chat_state = bool(force) or self._conversation_primed or resp_count > 0 or in_existing_thread

            if not has_chat_state:
                self._conversation_primed = False
                return

            # Try SPA "New chat" button first for instant zero-reload reset when sidebar/header button is visible.
            clicked_spa = False
            click_new_js = f"""
            (() => {{
                const newSels = {json.dumps(new_sels)};
                const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                for (const s of newSels) {{
                    const el = document.querySelector(s);
                    if (isVisible(el)) {{
                        el.click();
                        return "CLICKED";
                    }}
                }}
                return "NONE";
            }})()
            """
            try:
                clicked_spa = str(self._cdp.evaluate(click_new_js, timeout=5.0) or "") == "CLICKED"
            except Exception:
                clicked_spa = False

            cleared_via_spa = False
            if clicked_spa:
                spa_deadline = time.monotonic() + 2.2
                while time.monotonic() < spa_deadline:
                    try:
                        cur = self._cdp.evaluate(inspect_js, timeout=4.0) or {}
                        cur_cnt = int(cur.get("respCount") or 0)
                        cur_path = str(cur.get("pathname") or "")
                        cur_in_thread = cur_path.startswith("/app/") and cur_path.rstrip("/") != "/app"
                        if cur_cnt == 0 and not cur_in_thread and self._check_dom_login_state(self._cdp, selectors) == "READY":
                            cleared_via_spa = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.18)

            if not cleared_via_spa:
                nav_js = (
                    "window.__vcStalePage = true; "
                    "window.__vcModelSpec = ''; "
                    f"window.location.href = {json.dumps(target_url)};"
                )
                try:
                    self._cdp.evaluate(nav_js, timeout=8.0)
                except Exception:
                    pass
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    try:
                        not_stale = self._cdp.evaluate("window.__vcStalePage !== true", timeout=4.0)
                        if not_stale and self._check_dom_login_state(self._cdp, selectors) == "READY":
                            break
                    except Exception:
                        pass
                    time.sleep(0.22)

            self._select_web_model_on_cdp(self._cdp, self.selected_model_code, self.selected_thinking_level)
            self._conversation_primed = False

    def _run_cdp_prompt(
        self,
        cdp: LocalCDPClient,
        selectors: dict[str, Any],
        text: str,
        timeout_sec: float = 95.0,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        """Install MutationObserver, insert prompt via CDP Input.insertText, submit, and poll until complete."""
        input_sels = selectors.get("input_selectors", [])
        send_sels = selectors.get("send_button_selectors", [])
        stop_sels = selectors.get("stop_button_selectors", [])
        resp_sels = selectors.get("response_selectors", [])

        setup_js = f"""
        (async () => {{
            const respSels = {json.dumps(resp_sels)};
            const stopSels = {json.dumps(stop_sels)};
            const inputSels = {json.dumps(input_sels)};
            const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            const wait = (ms) => new Promise(r => setTimeout(r, ms));
            const getResponses = () => {{
                for (const s of respSels) {{
                    const nodes = Array.from(document.querySelectorAll(s));
                    if (nodes.length > 0) return nodes;
                }}
                return [];
            }};
            const isGenerating = () => {{
                for (const s of stopSels) {{
                    if (isVisible(document.querySelector(s))) return true;
                }}
                return false;
            }};

            if (isGenerating()) {{
                for (const s of stopSels) {{
                    const sb = document.querySelector(s);
                    if (isVisible(sb)) {{
                        try {{ sb.click(); }} catch (e) {{}}
                        break;
                    }}
                }}
                for (let i = 0; i < 6 && isGenerating(); i++) {{
                    await wait(75);
                }}
            }}

            if (window.__vcObserver) {{
                try {{ window.__vcObserver.disconnect(); }} catch (e) {{}}
            }}
            const baseNodes = getResponses();
            const baseCount = baseNodes.length;
            const baseLastText = baseCount > 0 ? (baseNodes[baseCount - 1].innerText || "") : "";

            window.__vcStream = {{
                baseCount,
                baseLastText,
                started: false,
                generating: false,
                wasGenerating: false,
                text: "",
                lastMutAt: Date.now()
            }};

            const refresh = () => {{
                const nodes = getResponses();
                const gen = isGenerating();
                const st = window.__vcStream;
                if (st.wasGenerating && !gen) {{
                    st.lastMutAt = Date.now();
                }}
                st.wasGenerating = gen;
                st.generating = gen;
                if (nodes.length > st.baseCount) {{
                    st.started = true;
                    const txt = (nodes[nodes.length - 1].innerText || "").trim();
                    if (txt !== st.text) {{
                        st.text = txt;
                        st.lastMutAt = Date.now();
                    }}
                }} else if (nodes.length === st.baseCount && st.baseCount > 0) {{
                    const cur = (nodes[nodes.length - 1].innerText || "").trim();
                    if (cur && cur !== st.baseLastText.trim()) {{
                        st.started = true;
                        if (cur !== st.text) {{
                            st.text = cur;
                            st.lastMutAt = Date.now();
                        }}
                    }}
                }}
                if (gen) {{
                    st.started = true;
                }}
            }};

            window.__vcRefresh = refresh;
            window.__vcObserver = new MutationObserver(() => refresh());
            window.__vcObserver.observe(document.body, {{ childList: true, subtree: true, characterData: true }});

            let inputEl = null;
            for (const s of inputSels) {{
                const el = document.querySelector(s);
                if (isVisible(el)) {{ inputEl = el; break; }}
            }}
            if (!inputEl) return "NO_INPUT";
            inputEl.focus();
            if (inputEl.tagName === "TEXTAREA" || inputEl.tagName === "INPUT") {{
                inputEl.value = "";
                inputEl.dispatchEvent(new Event("input", {{ bubbles: true }}));
            }} else {{
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(inputEl);
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand("delete", false, null);
            }}
            return "READY";
        }})()
        """
        prep = cdp.evaluate(setup_js, timeout=10.0)
        if prep != "READY":
            raise RuntimeError(f"제미니 입력창을 찾지 못했습니다 (selectors.json 확인 필요: {prep})")

        cdp.call("Input.insertText", {"text": text}, timeout=10.0)
        time.sleep(0.08)

        click_js = f"""
        (() => {{
            const sendSels = {json.dumps(send_sels)};
            const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            for (const s of sendSels) {{
                const btn = document.querySelector(s);
                if (isVisible(btn)) {{
                    btn.click();
                    return "CLICKED";
                }}
            }}
            return "NO_BUTTON";
        }})()
        """
        clicked = cdp.evaluate(click_js, timeout=6.0)
        if clicked != "CLICKED":
            cdp.call(
                "Input.dispatchKeyEvent",
                {"type": "rawKeyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
                timeout=5.0,
            )
            cdp.call(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
                timeout=5.0,
            )

        deadline = time.monotonic() + float(timeout_sec)
        prev_cur_text = ""
        stable_polls = 0
        last_cur_text = ""
        poll_js = """
        (() => {
            if (typeof window.__vcRefresh === "function") {
                try { window.__vcRefresh(); } catch (e) {}
            }
            const st = window.__vcStream || {};
            const now = Date.now();
            return {
                started: !!st.started,
                generating: !!st.generating,
                text: st.text || "",
                idleMs: now - (st.lastMutAt || now)
            };
        })()
        """
        try:
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        stop_js = f"""(() => {{
                            const sels = {json.dumps(stop_sels)};
                            const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                            for (const s of sels) {{
                                const b = document.querySelector(s);
                                if (isVis(b)) {{ try {{ b.click(); }} catch (e) {{}} return "STOPPED"; }}
                            }}
                            return "NONE";
                        }})()"""
                        cdp.evaluate(stop_js, timeout=2.0)
                    except Exception:
                        pass
                    break
                st = cdp.evaluate(poll_js, timeout=6.0) or {}
                cur_text = str(st.get("text") or "")
                last_cur_text = cur_text
                if cur_text and on_progress is not None:
                    on_progress(cur_text)

                if (
                    st.get("started")
                    and (not st.get("generating"))
                    and cur_text.strip()
                    and int(st.get("idleMs", 0)) >= 650
                ):
                    if cur_text == prev_cur_text:
                        stable_polls += 1
                    else:
                        stable_polls = 0
                    if stable_polls >= 2:
                        break
                else:
                    stable_polls = 0
                prev_cur_text = cur_text
                time.sleep(0.045)
        finally:
            try:
                cdp.evaluate(
                    "if (window.__vcObserver) { try { window.__vcObserver.disconnect(); } catch(e) {} }",
                    timeout=3.0,
                )
            except Exception:
                pass
        return last_cur_text

    def _ensure_translator_cdp(self) -> LocalCDPClient:
        """Open/reuse a hidden 2nd CDP tab sharing BrowserProfile for background subtitle translation."""
        with self._trans_lock:
            selectors = load_selectors_config(self.app_root)
            target_url = str(selectors.get("url") or "https://gemini.google.com/app")

            if self._trans_cdp is not None and self.is_running() and not self.login_mode_active:
                try:
                    self._trans_cdp.evaluate("location.href", timeout=3.0)
                    return self._trans_cdp
                except Exception:
                    self._trans_cdp.close()
                    self._trans_cdp = None
                    self._trans_target_id = None

            if self._cdp is None or not self.is_running() or self.login_mode_active or self._cdp_port is None:
                ok, reason = self.ensure_ready_and_check_login(show_if_ready=False, allow_interactive_login=False)
                if not ok:
                    raise RuntimeError(f"TRANSLATOR_LOGIN_REQUIRED:{reason}")

            assert self._cdp_port is not None
            port = self._cdp_port
            before_hwnds: set[int] = set()
            if os.name == "nt" and self._proc is not None:
                before_hwnds = set(_get_windows_for_pids(_get_child_pids(self._proc.pid)))

            ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3.0).json()
            browser_ws = str(ver.get("webSocketDebuggerUrl") or "")
            if not browser_ws:
                raise RuntimeError("브라우저 CDP Version WebSocket을 찾지 못했습니다.")

            tmp_browser = LocalCDPClient(browser_ws, timeout=8.0)
            try:
                created = tmp_browser.call(
                    "Target.createTarget",
                    {"url": target_url, "background": True},
                    timeout=8.0,
                )
                target_id = str(created.get("targetId") or "")
            finally:
                tmp_browser.close()

            if not target_id:
                raise RuntimeError("번역 백그라운드 탭 생성에 실패했습니다.")

            targets = httpx.get(f"http://127.0.0.1:{port}/json", timeout=3.0).json()
            page = next((t for t in targets if t.get("id") == target_id), None)
            if not page or not page.get("webSocketDebuggerUrl"):
                raise RuntimeError("번역 탭 WebSocket URL을 찾지 못했습니다.")

            self._trans_target_id = target_id
            self._trans_cdp = LocalCDPClient(page["webSocketDebuggerUrl"], timeout=12.0)
            self._trans_turn_count = 0
            try:
                self._trans_cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=4.0)
            except Exception:
                pass

            # Chrome in --app mode spawns a second HWND for Target.createTarget: hide it immediately!
            if os.name == "nt" and self._proc is not None:
                hide_deadline = time.monotonic() + 0.85
                while time.monotonic() < hide_deadline:
                    after_hwnds = set(_get_windows_for_pids(_get_child_pids(self._proc.pid)))
                    new_hwnds = [h for h in (after_hwnds - before_hwnds)]
                    if new_hwnds:
                        self._trans_hwnd = new_hwnds[0]
                        for nh in new_hwnds:
                            try:
                                self._apply_hwnd_visibility(nh, visible=False)
                            except Exception:
                                pass
                        break
                    time.sleep(0.08)

            ready_deadline = time.monotonic() + 12.0
            while time.monotonic() < ready_deadline:
                st = self._check_dom_login_state(self._trans_cdp, selectors)
                if st == "READY":
                    return self._trans_cdp
                if st == "LOGIN_REQUIRED":
                    self._trans_cdp.close()
                    self._trans_cdp = None
                    self._trans_target_id = None
                    raise RuntimeError("TRANSLATOR_LOGIN_REQUIRED")
                time.sleep(0.3)

            raise RuntimeError("번역용 백그라운드 제미니 탭 로딩 시간 초과")

    def translate_via_browser_tab(
        self,
        jp_text: str,
        style_guide: str = "",
        context_lines: list[str] | None = None,
        model_code: str = "hydrogen",
        thinking_level: str = "low",
        timeout_sec: float | None = None,
    ) -> str:
        """Translate a single Japanese line into natural Korean on the hidden 2nd CDP tab."""
        clean_jp = (jp_text or "").strip()
        if not clean_jp:
            return ""
        eff_model, eff_level = split_legacy_browser_model(model_code, thinking_level)
        eff_timeout = float(timeout_sec) if timeout_sec is not None else {"low": 28.0, "medium": 38.0, "high": 60.0}.get(eff_level, 28.0)

        with self._trans_lock:
            selectors = load_selectors_config(self.app_root)
            target_url = str(selectors.get("url") or "https://gemini.google.com/app")
            for attempt in range(2):
                try:
                    cdp = self._ensure_translator_cdp()
                    if self._trans_turn_count >= 24:
                        cdp.evaluate(f"window.location.href = {json.dumps(target_url)};", timeout=8.0)
                        wait_dl = time.monotonic() + 10.0
                        while time.monotonic() < wait_dl:
                            if self._check_dom_login_state(cdp, selectors) == "READY":
                                break
                            time.sleep(0.25)
                        self._trans_turn_count = 0

                    self._select_web_model_on_cdp(cdp, eff_model, eff_level)

                    ctx_block = ""
                    if context_lines:
                        joined_ctx = "\n".join(f"- {ln}" for ln in context_lines[-3:])
                        ctx_block = f"\n[직전 대화 흐름]\n{joined_ctx}\n"
                    style_block = f"{style_guide.strip()}\n" if style_guide and style_guide.strip() else ""
                    prompt = (
                        f"{style_block}{ctx_block}"
                        "아래 일본어 캐릭터 대사 한 줄을 위 말투에 맞는 자연스러운 한국어 구어체 자막 1줄로 번역해줘. "
                        "설명·따옴표·마크다운·괄호 지문 없이 번역된 한국어 문장만 출력해:\n"
                        f"{clean_jp}"
                    )
                    raw_out = self._run_cdp_prompt(
                        cdp,
                        selectors,
                        prompt,
                        timeout_sec=eff_timeout,
                    )
                    self._trans_turn_count += 1
                    return sanitize_korean_subtitle_out(raw_out)
                except RuntimeError as exc:
                    if "TRANSLATOR_LOGIN_REQUIRED" in str(exc):
                        return ""
                    if attempt == 0 and self._trans_cdp is not None:
                        self._trans_cdp.close()
                        self._trans_cdp = None
                        self._trans_target_id = None
                        continue
                    return ""
                except Exception:
                    if attempt == 0 and self._trans_cdp is not None:
                        self._trans_cdp.close()
                        self._trans_cdp = None
                        self._trans_target_id = None
                        continue
                    return ""
            return ""

    def run_background_prompt(
        self,
        prompt: str,
        model_code: str = "hydrogen",
        thinking_level: str = "low",
        timeout_sec: float = 45.0,
        fresh_chat: bool = True,
    ) -> str:
        """
        Execute a stateless background prompt (e.g., 3-stage memory summarization) on the hidden
        2nd CDP helper tab without interrupting the live call or popping up browser windows.
        Only runs when the browser session is already active and signed in.
        """
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return ""
        if (not self.is_running()) or self.login_mode_active:
            return ""

        eff_model, eff_level = split_legacy_browser_model(model_code, thinking_level)
        eff_timeout = max(20.0, float(timeout_sec or 45.0))

        with self._trans_lock:
            selectors = load_selectors_config(self.app_root)
            target_url = str(selectors.get("url") or "https://gemini.google.com/app")
            for attempt in range(2):
                try:
                    cdp = self._ensure_translator_cdp()
                    if fresh_chat or self._trans_turn_count >= 12:
                        cdp.evaluate(f"window.location.href = {json.dumps(target_url)};", timeout=8.0)
                        wait_dl = time.monotonic() + 12.0
                        while time.monotonic() < wait_dl:
                            if self._check_dom_login_state(cdp, selectors) == "READY":
                                break
                            time.sleep(0.25)
                        self._trans_turn_count = 0

                    self._select_web_model_on_cdp(cdp, eff_model, eff_level)
                    raw_out = self._run_cdp_prompt(
                        cdp,
                        selectors,
                        clean_prompt,
                        timeout_sec=eff_timeout,
                    )
                    # Mark helper tab dirty so subsequent subtitle translation starts fresh
                    self._trans_turn_count = 24
                    return (raw_out or "").strip()
                except RuntimeError as exc:
                    if "TRANSLATOR_LOGIN_REQUIRED" in str(exc):
                        return ""
                    if attempt == 0 and self._trans_cdp is not None:
                        self._trans_cdp.close()
                        self._trans_cdp = None
                        self._trans_target_id = None
                        continue
                    return ""
                except Exception:
                    if attempt == 0 and self._trans_cdp is not None:
                        self._trans_cdp.close()
                        self._trans_cdp = None
                        self._trans_target_id = None
                        continue
                    return ""
            return ""

    def send_and_stream(
        self,
        text: str,
        on_sentence: Callable[[str, bool], None],
        cancel_event: threading.Event | None = None,
        browser_model: str | None = None,
        thinking_level: str | None = None,
        bilingual_inline: bool = False,
        on_sentence_pair: Callable[[str, Future[str], bool], None] | None = None,
        use_japanese_lock: bool = False,
        use_korean_lock: bool = False,
    ) -> str:
        """
        Inject `text` via CDP Input.insertText (no innerHTML / TrustedHTML violation),
        submit via selectors.json, and stream newly added sentences via DOM MutationObserver.
        """
        with self._io_lock:
            if browser_model is not None or thinking_level is not None:
                m_in = browser_model if browser_model is not None else self.selected_model_code
                l_in = thinking_level if thinking_level is not None else self.selected_thinking_level
                self.selected_model_code, self.selected_thinking_level = split_legacy_browser_model(m_in, l_in)
            if self._cdp is None or not self.is_running() or self.login_mode_active:
                ok, reason = self.ensure_ready_and_check_login(show_if_ready=self.window_visible)
                if not ok:
                    raise RuntimeError(f"제미니 브라우저가 준비되지 않았습니다 ({reason})")

            assert self._cdp is not None
            selectors = load_selectors_config(self.app_root)
            self._select_web_model_on_cdp(self._cdp, self.selected_model_code, self.selected_thinking_level)
            prompt_timeout = 120.0 if self.selected_thinking_level == "high" else 95.0

            bilingual_active = bool(bilingual_inline and on_sentence_pair is not None)
            splitter = (
                BilingualSentenceStreamParser()
                if bilingual_active
                else IncrementalSentenceSplitter(
                    japanese_only=bool(use_japanese_lock),
                    korean_only=bool(use_korean_lock and not use_japanese_lock),
                )
            )
            emitted_parts: list[str] = []
            is_first = True
            last_raw_text = ""

            def _handle_progress(cur_text: str) -> None:
                nonlocal is_first, last_raw_text
                if cur_text:
                    last_raw_text = cur_text
                if bilingual_active:
                    assert isinstance(splitter, BilingualSentenceStreamParser)
                    assert on_sentence_pair is not None
                    for jp_sent, kr_fut in splitter.feed(cur_text):
                        on_sentence_pair(jp_sent, kr_fut, is_first)
                        emitted_parts.append(jp_sent)
                        is_first = False
                else:
                    assert isinstance(splitter, IncrementalSentenceSplitter)
                    for sent in splitter.feed(cur_text):
                        on_sentence(sent, is_first)
                        emitted_parts.append(sent)
                        is_first = False

            def _flush_splitter() -> None:
                nonlocal is_first
                if bilingual_active:
                    assert isinstance(splitter, BilingualSentenceStreamParser)
                    assert on_sentence_pair is not None
                    for jp_sent, kr_fut in splitter.flush():
                        on_sentence_pair(jp_sent, kr_fut, is_first)
                        emitted_parts.append(jp_sent)
                        is_first = False
                else:
                    assert isinstance(splitter, IncrementalSentenceSplitter)
                    for tail in splitter.flush():
                        on_sentence(tail, is_first)
                        emitted_parts.append(tail)
                        is_first = False

            try:
                self._conversation_primed = True
                raw_out = self._run_cdp_prompt(
                    self._cdp,
                    selectors,
                    text,
                    timeout_sec=prompt_timeout,
                    cancel_event=cancel_event,
                    on_progress=_handle_progress,
                )
            except RuntimeError:
                raise
            except Exception:
                ok, reason = self.ensure_ready_and_check_login(show_if_ready=self.window_visible)
                if not ok:
                    raise RuntimeError(f"제미니 브라우저 재연결 실패 ({reason})")
                assert self._cdp is not None
                splitter.reset()
                emitted_parts.clear()
                is_first = True
                last_raw_text = ""
                self._conversation_primed = True
                self._select_web_model_on_cdp(self._cdp, self.selected_model_code, self.selected_thinking_level)
                raw_out = self._run_cdp_prompt(
                    self._cdp,
                    selectors,
                    text,
                    timeout_sec=prompt_timeout,
                    cancel_event=cancel_event,
                    on_progress=_handle_progress,
                )

            _flush_splitter()
            emitted_text = " ".join(emitted_parts).strip()
            raw_text = (raw_out or last_raw_text or "").strip()

            # 1-shot self-healing retry if locked Japanese/Korean turn drifted into the wrong language
            if (
                (cancel_event is None or not cancel_event.is_set())
                and _needs_japanese_repair_retry(
                    raw_text,
                    emitted_text,
                    use_japanese_lock=use_japanese_lock,
                    bilingual_active=bilingual_active,
                )
            ):
                self._log(f"[Gemini Web] 일본어 출력 이탈 감지 → 1회 자동 교정 재전송 (raw={raw_text[:60]!r})")
                splitter.reset()
                emitted_parts.clear()
                is_first = True
                last_raw_text = ""
                repair_prompt = _build_japanese_repair_prompt(
                    bad_reply=raw_text,
                    bilingual_inline=bilingual_active,
                )
                self._run_cdp_prompt(
                    self._cdp,
                    selectors,
                    repair_prompt,
                    timeout_sec=prompt_timeout,
                    cancel_event=cancel_event,
                    on_progress=_handle_progress,
                )
                _flush_splitter()
            elif (
                (cancel_event is None or not cancel_event.is_set())
                and _needs_korean_repair_retry(
                    raw_text,
                    emitted_text,
                    use_korean_lock=bool(use_korean_lock and not use_japanese_lock),
                )
            ):
                self._log(f"[Gemini Web] 한국어 출력 이탈 감지 → 1회 자동 교정 재전송 (raw={raw_text[:60]!r})")
                splitter.reset()
                emitted_parts.clear()
                is_first = True
                last_raw_text = ""
                repair_prompt = _build_korean_repair_prompt(bad_reply=raw_text)
                self._run_cdp_prompt(
                    self._cdp,
                    selectors,
                    repair_prompt,
                    timeout_sec=prompt_timeout,
                    cancel_event=cancel_event,
                    on_progress=_handle_progress,
                )
                _flush_splitter()

            return " ".join(emitted_parts).strip()

    def close(self) -> None:
        with self._io_lock:
            self._terminate_proc()


class OfficialGeminiApiBackend:
    """
    Mode A: Official Google Gemini REST SSE streaming (ultra-low ~0.6s TTFA, zero browser RAM).
    """

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._own_client = http_client is None
        self._client_obj: httpx.Client | None = http_client
        self._client_lock = threading.Lock()
        self._history: list[dict[str, Any]] = []
        self._system_prompt = ""
        self._lock = threading.Lock()

    @property
    def _client(self) -> httpx.Client:
        if self._client_obj is not None:
            return self._client_obj
        with self._client_lock:
            if self._client_obj is None:
                self._client_obj = httpx.Client(timeout=httpx.Timeout(35.0, connect=8.0))
            return self._client_obj

    @_client.setter
    def _client(self, client: httpx.Client | None) -> None:
        self._client_obj = client

    def start_new_chat(self, system_prompt: str = "") -> None:
        with self._lock:
            self._history = []
            self._system_prompt = system_prompt

    def update_system_prompt(self, system_prompt: str) -> None:
        """Update system instruction cleanly without resetting multi-turn conversation history."""
        with self._lock:
            self._system_prompt = system_prompt

    @staticmethod
    def _extract_non_thought_text(parts: list[Any]) -> str:
        """Join visible answer text while stripping hidden thinking (`thought=True`) parts."""
        return "".join(
            str(p.get("text") or "")
            for p in (parts or [])
            if isinstance(p, dict) and not p.get("thought")
        )

    @staticmethod
    def _build_gen_config_with_thinking(
        base_config: dict[str, Any],
        model: str,
        thinking_level: str,
        extra_level_budget: int = 1280,
    ) -> dict[str, Any]:
        gen_cfg = dict(base_config)
        t_cfg = build_api_thinking_config(model, thinking_level)
        if t_cfg is not None:
            gen_cfg["thinkingConfig"] = t_cfg
            base_max = int(gen_cfg.get("maxOutputTokens", 950))
            if "thinkingBudget" in t_cfg:
                gen_cfg["maxOutputTokens"] = base_max + int(t_cfg["thinkingBudget"])
            elif t_cfg.get("thinkingLevel") == "XHIGH":
                gen_cfg["maxOutputTokens"] = max(base_max, base_max + max(extra_level_budget * 2, 2560))
            elif t_cfg.get("thinkingLevel") in ("MEDIUM", "HIGH"):
                gen_cfg["maxOutputTokens"] = max(base_max, base_max + extra_level_budget)
            elif t_cfg.get("thinkingLevel") == "LOW":
                gen_cfg["maxOutputTokens"] = max(base_max, base_max + 512)
        return gen_cfg

    def list_generate_models(self, api_key: str, timeout_sec: float = 10.0) -> list[str]:
        """Fetch all `generateContent`-capable Gemini model IDs from the official API."""
        clean_key = (api_key or "").strip()
        if not clean_key:
            return []
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        resp = self._client.get(
            url,
            params={"pageSize": 200},
            headers={"x-goog-api-key": clean_key},
            timeout=timeout_sec,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini 모델 목록 조회 실패 HTTP {resp.status_code}")
        data = resp.json()
        models = data.get("models") or []
        skip_tokens = ("embedding", "aqa", "imagen", "veo", "tts", "robotics", "computer-use", "decision", "lyria")
        out: list[str] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = str(m.get("name") or "")
            if name.startswith("models/"):
                name = name[7:]
            low = name.lower()
            if not low.startswith("gemini-"):
                continue
            if any(tok in low for tok in skip_tokens):
                continue
            out.append(name)
        return sorted(set(out))

    def translate_short_line(
        self,
        api_key: str,
        model: str,
        jp_text: str,
        style_guide: str = "",
        context_lines: list[str] | None = None,
        thinking_level: str = "low",
        timeout_sec: float | None = None,
    ) -> str:
        """Stateless one-shot Gemini API translation for subtitle refinement (never alters chat history)."""
        clean_key = (api_key or "").strip()
        clean_jp = (jp_text or "").strip()
        if not clean_key or not clean_jp:
            return ""
        clean_model = (model or "gemini-2.5-flash").strip()
        eff_timeout = float(timeout_sec) if timeout_sec is not None else {"low": 15.0, "medium": 24.0, "high": 40.0, "xhigh": 55.0}.get(thinking_level, 18.0)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent"
        ctx_block = ""
        if context_lines:
            joined_ctx = "\n".join(f"- {ln}" for ln in context_lines[-3:])
            ctx_block = f"\n[직전 대화 흐름]\n{joined_ctx}\n"
        style_block = f"{style_guide.strip()}\n" if style_guide and style_guide.strip() else ""
        prompt = (
            f"{style_block}{ctx_block}"
            "아래 일본어 캐릭터 대사 한 줄을 위 말투에 맞는 자연스러운 한국어 구어체 자막 1줄로 번역하라. "
            "설명·따옴표·마크다운·괄호 지문 없이 번역된 한국어 문장만 출력하라:\n"
            f"{clean_jp}"
        )
        base_gen = {"temperature": 0.25, "maxOutputTokens": 240}
        gen_cfg = self._build_gen_config_with_thinking(base_gen, clean_model, thinking_level, extra_level_budget=800)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }
        try:
            resp = self._client.post(
                url,
                headers={"x-goog-api-key": clean_key, "Content-Type": "application/json"},
                json=payload,
                timeout=eff_timeout,
            )
            if resp.status_code == 400 and "thinkingConfig" in gen_cfg:
                payload["generationConfig"] = base_gen
                resp = self._client.post(
                    url,
                    headers={"x-goog-api-key": clean_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=eff_timeout,
                )
            if resp.status_code != 200:
                return ""
            data = resp.json()
            cands = data.get("candidates") or []
            if not cands:
                return ""
            parts = ((cands[0].get("content") or {}).get("parts")) or []
            raw = self._extract_non_thought_text(parts)
            return sanitize_korean_subtitle_out(raw)
        except Exception:
            return ""

    def generate_background_text(
        self,
        api_key: str,
        model: str,
        prompt: str,
        thinking_level: str = "low",
        timeout_sec: float = 40.0,
        max_output_tokens: int = 1200,
    ) -> str:
        """Stateless one-shot Gemini API call for background memory extraction (never mutates chat history)."""
        clean_key = (api_key or "").strip()
        clean_prompt = (prompt or "").strip()
        if not clean_key or not clean_prompt:
            return ""
        clean_model = (model or "gemini-2.5-flash").strip()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent"
        base_gen = {"temperature": 0.2, "maxOutputTokens": max(300, int(max_output_tokens))}
        gen_cfg = self._build_gen_config_with_thinking(base_gen, clean_model, thinking_level, extra_level_budget=1024)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": clean_prompt}]}],
            "generationConfig": gen_cfg,
        }
        try:
            resp = self._client.post(
                url,
                headers={"x-goog-api-key": clean_key, "Content-Type": "application/json"},
                json=payload,
                timeout=max(15.0, float(timeout_sec or 40.0)),
            )
            if resp.status_code == 400 and "thinkingConfig" in gen_cfg:
                payload["generationConfig"] = base_gen
                resp = self._client.post(
                    url,
                    headers={"x-goog-api-key": clean_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=max(15.0, float(timeout_sec or 40.0)),
                )
            if resp.status_code != 200:
                return ""
            data = resp.json()
            cands = data.get("candidates") or []
            if not cands:
                return ""
            parts = ((cands[0].get("content") or {}).get("parts")) or []
            return self._extract_non_thought_text(parts).strip()
        except Exception:
            return ""

    def send_and_stream(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        text: str,
        on_sentence: Callable[[str, bool], None],
        cancel_event: threading.Event | None = None,
        thinking_level: str = "low",
        bilingual_inline: bool = False,
        on_sentence_pair: Callable[[str, Future[str], bool], None] | None = None,
        use_japanese_lock: bool = False,
        use_korean_lock: bool = False,
    ) -> str:
        clean_key = (api_key or "").strip()
        if not clean_key:
            raise RuntimeError("Gemini API Key가 비어 있습니다. [⚙️ 설정 열기]에서 키를 입력해 주세요.")
        clean_model = (model or "gemini-2.5-flash").strip()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:streamGenerateContent?alt=sse"

        with self._lock:
            if system_prompt:
                self._system_prompt = system_prompt
            prev_history = list(self._history)
            sys_instr = self._system_prompt

        bilingual_active = bool(bilingual_inline and on_sentence_pair is not None)
        base_gen = {
            "temperature": 0.84,
            "maxOutputTokens": 1500 if bilingual_active else 950,
        }
        gen_cfg = self._build_gen_config_with_thinking(base_gen, clean_model, thinking_level, extra_level_budget=1400)

        splitter = (
            BilingualSentenceStreamParser()
            if bilingual_active
            else IncrementalSentenceSplitter(
                japanese_only=bool(use_japanese_lock),
                korean_only=bool(use_korean_lock and not use_japanese_lock),
            )
        )
        emitted_parts: list[str] = []
        raw_full: list[str] = []
        is_first = True

        def _make_payload(req_user_text: str) -> dict[str, Any]:
            contents = (prev_history + [{"role": "user", "parts": [{"text": req_user_text}]}])[-24:]
            req: dict[str, Any] = {
                "contents": contents,
                "generationConfig": gen_cfg,
            }
            if sys_instr:
                req["system_instruction"] = {"parts": [{"text": sys_instr}]}
            return req

        def _consume_sse(req_payload: dict[str, Any]) -> tuple[int, str]:
            nonlocal is_first
            with self._client.stream(
                "POST",
                url,
                headers={"x-goog-api-key": clean_key, "Content-Type": "application/json"},
                json=req_payload,
            ) as resp:
                if resp.status_code != 200:
                    err_body = resp.read().decode("utf-8", errors="replace")
                    return resp.status_code, err_body

                for line in resp.iter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        obj = json.loads(data_str)
                    except Exception:
                        continue
                    cands = obj.get("candidates") or []
                    if not cands:
                        continue
                    parts = ((cands[0].get("content") or {}).get("parts")) or []
                    delta = self._extract_non_thought_text(parts)
                    if not delta:
                        continue
                    raw_full.append(delta)
                    if bilingual_active:
                        assert isinstance(splitter, BilingualSentenceStreamParser)
                        assert on_sentence_pair is not None
                        for jp_sent, kr_fut in splitter.feed_delta(delta):
                            on_sentence_pair(jp_sent, kr_fut, is_first)
                            emitted_parts.append(jp_sent)
                            is_first = False
                    else:
                        assert isinstance(splitter, IncrementalSentenceSplitter)
                        for sent in splitter.feed_delta(delta):
                            on_sentence(sent, is_first)
                            emitted_parts.append(sent)
                            is_first = False
                return 200, ""

        def _run_attempt(req_user_text: str) -> tuple[str, str]:
            nonlocal is_first
            splitter.reset()
            emitted_parts.clear()
            raw_full.clear()
            is_first = True

            req_payload = _make_payload(req_user_text)
            status_code, err_body = _consume_sse(req_payload)
            if status_code == 400 and "thinkingConfig" in gen_cfg:
                req_payload["generationConfig"] = base_gen
                status_code, err_body = _consume_sse(req_payload)
            if status_code != 200:
                raise RuntimeError(f"Gemini API HTTP {status_code}: {err_body[:240]}")

            if bilingual_active:
                assert isinstance(splitter, BilingualSentenceStreamParser)
                assert on_sentence_pair is not None
                for jp_sent, kr_fut in splitter.flush():
                    on_sentence_pair(jp_sent, kr_fut, is_first)
                    emitted_parts.append(jp_sent)
                    is_first = False
            else:
                assert isinstance(splitter, IncrementalSentenceSplitter)
                for tail in splitter.flush():
                    on_sentence(tail, is_first)
                    emitted_parts.append(tail)
                    is_first = False

            return "".join(raw_full).strip(), " ".join(emitted_parts).strip()

        full_model_text, emitted_text = _run_attempt(text)
        ko_lock_eff = bool(use_korean_lock and not use_japanese_lock)
        if (
            (cancel_event is None or not cancel_event.is_set())
            and _needs_japanese_repair_retry(
                full_model_text,
                emitted_text,
                use_japanese_lock=use_japanese_lock,
                bilingual_active=bilingual_active,
            )
        ):
            repair_note = _build_japanese_repair_prompt(
                bad_reply=full_model_text,
                bilingual_inline=bilingual_active,
            )
            full_model_text, emitted_text = _run_attempt(f"{text}\n\n{repair_note}")
        elif (
            (cancel_event is None or not cancel_event.is_set())
            and _needs_korean_repair_retry(
                full_model_text,
                emitted_text,
                use_korean_lock=ko_lock_eff,
            )
        ):
            repair_note = _build_korean_repair_prompt(bad_reply=full_model_text)
            full_model_text, emitted_text = _run_attempt(f"{text}\n\n{repair_note}")

        failed_lang_lock = (
            use_japanese_lock
            and _needs_japanese_repair_retry(
                full_model_text,
                emitted_text,
                use_japanese_lock=True,
                bilingual_active=bilingual_active,
            )
        ) or (
            ko_lock_eff
            and _needs_korean_repair_retry(
                full_model_text,
                emitted_text,
                use_korean_lock=True,
            )
        )
        if full_model_text and not failed_lang_lock:
            with self._lock:
                self._history = (
                    prev_history
                    + [
                        {"role": "user", "parts": [{"text": text}]},
                        {"role": "model", "parts": [{"text": full_model_text}]},
                    ]
                )[-24:]
        return emitted_text

    def close(self) -> None:
        if self._own_client and self._client_obj is not None:
            try:
                self._client_obj.close()
            except Exception:
                pass


class GeminiSessionManager:
    """Unified Mode C facade seamlessly switching between Browser (Mode B) and Official API (Mode A)."""

    def __init__(
        self,
        app_root: Path | None = None,
        http_client: httpx.Client | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        self.browser = BrowserGeminiBackend(app_root=app_root, log_cb=log_cb)
        self.api = OfficialGeminiApiBackend(http_client=http_client)
        self._primed_signature = ""
        self._primed_bilingual = False

    def invalidate_session(self) -> None:
        """Mark any primed chat session as stale so the next turn opens a fresh conversation."""
        self._primed_signature = ""
        self._primed_bilingual = False

    def reset_conversation(self, backend_mode: str, system_prompt: str = "") -> None:
        self.invalidate_session()
        if backend_mode == "api":
            self.api.start_new_chat(system_prompt)
        else:
            self.browser.start_new_chat()

    def _persona_signature(
        self,
        backend_mode: str,
        persona_prompt: str,
        use_japanese_lock: bool,
        ko_style_guide: str,
        jp_beginner_coach: bool = False,
        use_korean_lock: bool = False,
    ) -> str:
        raw = f"{backend_mode}|{int(bool(use_japanese_lock))}|{int(bool(use_korean_lock))}|{int(bool(jp_beginner_coach))}|{persona_prompt.strip()}|{ko_style_guide.strip()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def prime_and_greet(
        self,
        backend_mode: str,
        api_key: str,
        api_model: str,
        persona_prompt: str,
        use_japanese_lock: bool,
        on_sentence: Callable[[str, bool], None],
        cancel_event: threading.Event | None = None,
        browser_model: str = "hydrogen",
        thinking_level: str = "low",
        bilingual_inline: bool = False,
        ko_style_guide: str = "",
        on_sentence_pair: Callable[[str, Future[str], bool], None] | None = None,
        jp_beginner_coach: bool = False,
        use_korean_lock: bool = False,
        memory_context: str = "",
    ) -> str:
        eff_ko_lock = bool(use_korean_lock and not use_japanese_lock)
        eff_coach = bool(jp_beginner_coach and not eff_ko_lock)
        clean_memory = (memory_context or "").strip()
        full_sys = build_first_message_prompt(
            persona_prompt,
            use_japanese_lock=use_japanese_lock,
            bilingual_inline=bilingual_inline,
            ko_style_guide=ko_style_guide,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
            memory_context=clean_memory,
        )
        self._primed_signature = self._persona_signature(
            backend_mode,
            persona_prompt,
            use_japanese_lock,
            ko_style_guide,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
        )
        self._primed_bilingual = bool(bilingual_inline)

        if use_japanese_lock:
            lang_hint = (
                " 반드시 모든 문장을 `일본어 대사 || 한국어 번역` 형식으로만 출력하세요."
                if bilingual_inline
                else " 반드시 한국어 없이 100% 자연스러운 일본어 구어체 대사로만 말하세요."
            )
        elif eff_ko_lock:
            lang_hint = " 반드시 일본어나 `||` 없이 캐릭터 말투가 살아있는 100% 자연스러운 한국어 구어체 대사로만 말하세요."
        else:
            lang_hint = ""

        reunion_hint = (
            " 위의 시간 맥락과 이전 기억이 있다면 억지로 나열하지 말고 자연스러울 때만 은은하게 한 가지 정도 녹여내거나 새로운 일상 화제로 반갑게 시작하세요."
            if clean_memory
            else ""
        )

        if eff_coach:
            opening_user = (
                "(대화가 막 시작되었습니다. 위 캐릭터 페르소나를 100% 유지하면서, 일본어가 아직 조금 서툰 상대도 편하게 알아들을 수 있는 "
                f"쉽고 자연스러운 일상 일본어로 다정하게 첫인사를 건네고 가볍게 말을 걸어 주세요. 선생님처럼 가르치려 들지 말고 평소 대화처럼 시작하세요.{reunion_hint}"
                f"{lang_hint})"
            )
        else:
            opening_user = (
                "(대화가 막 시작되었습니다. 위 설정을 숙지하고, 프롬프트의 예시 문장을 그대로 베끼지 말고 "
                f"캐릭터다운 새로운 첫마디와 일상 상황으로 자연스럽고 짧게 첫 인사를 건네세요.{reunion_hint}{lang_hint})"
            )

        if backend_mode == "api":
            self.api.start_new_chat(full_sys)
            return self.api.send_and_stream(
                api_key=api_key,
                model=api_model,
                system_prompt=full_sys,
                text=opening_user,
                on_sentence=on_sentence,
                cancel_event=cancel_event,
                thinking_level=thinking_level,
                bilingual_inline=bilingual_inline,
                on_sentence_pair=on_sentence_pair,
                use_japanese_lock=use_japanese_lock,
                use_korean_lock=eff_ko_lock,
            )

        self.browser.start_new_chat()
        first_msg = f"{full_sys}\n\n{opening_user}"
        return self.browser.send_and_stream(
            first_msg,
            on_sentence=on_sentence,
            cancel_event=cancel_event,
            browser_model=browser_model,
            thinking_level=thinking_level,
            bilingual_inline=bilingual_inline,
            on_sentence_pair=on_sentence_pair,
            use_japanese_lock=use_japanese_lock,
            use_korean_lock=eff_ko_lock,
        )

    def send_message(
        self,
        backend_mode: str,
        api_key: str,
        api_model: str,
        persona_prompt: str,
        use_japanese_lock: bool,
        user_text: str,
        on_sentence: Callable[[str, bool], None],
        cancel_event: threading.Event | None = None,
        browser_model: str = "hydrogen",
        thinking_level: str = "low",
        bilingual_inline: bool = False,
        ko_style_guide: str = "",
        on_sentence_pair: Callable[[str, Future[str], bool], None] | None = None,
        jp_beginner_coach: bool = False,
        use_korean_lock: bool = False,
        memory_context: str = "",
    ) -> str:
        eff_ko_lock = bool(use_korean_lock and not use_japanese_lock)
        eff_coach = bool(jp_beginner_coach and not eff_ko_lock)
        full_sys = build_first_message_prompt(
            persona_prompt,
            use_japanese_lock=use_japanese_lock,
            bilingual_inline=bilingual_inline,
            ko_style_guide=ko_style_guide,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
            memory_context=(memory_context or "").strip(),
        )
        sig = self._persona_signature(
            backend_mode,
            persona_prompt,
            use_japanese_lock,
            ko_style_guide,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
        )
        wrapped_user = wrap_user_turn_for_japanese_lock(
            user_text,
            use_japanese_lock=use_japanese_lock,
            bilingual_inline=bilingual_inline,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
        )

        if backend_mode == "api":
            if self._primed_signature != sig:
                self.api.start_new_chat(full_sys)
                self._primed_signature = sig
                self._primed_bilingual = bool(bilingual_inline)
            elif self._primed_bilingual != bool(bilingual_inline):
                self.api.update_system_prompt(full_sys)
                self._primed_bilingual = bool(bilingual_inline)
            return self.api.send_and_stream(
                api_key=api_key,
                model=api_model,
                system_prompt=full_sys,
                text=wrapped_user,
                on_sentence=on_sentence,
                cancel_event=cancel_event,
                thinking_level=thinking_level,
                bilingual_inline=bilingual_inline,
                on_sentence_pair=on_sentence_pair,
                use_japanese_lock=use_japanese_lock,
                use_korean_lock=eff_ko_lock,
            )

        payload = wrapped_user
        if self._primed_signature != sig:
            self.browser.start_new_chat()
            self._primed_signature = sig
            self._primed_bilingual = bool(bilingual_inline)
            payload = f"{full_sys}\n\n{wrapped_user}"
        elif self._primed_bilingual != bool(bilingual_inline):
            self._primed_bilingual = bool(bilingual_inline)
            if bilingual_inline:
                clean_guide = (ko_style_guide or "").strip()
                bilingual_rule = (
                    f"{BILINGUAL_STREAM_OUTPUT_RULE}\n\n{build_jp_coach_bilingual_addon()}"
                    if eff_coach
                    else BILINGUAL_STREAM_OUTPUT_RULE
                )
                switch_rule = (
                    f"{bilingual_rule}\n\n"
                    f"[`||` 오른쪽 한국어 번역 자막 전용 말투 가이드 — 왼쪽 일본어 대사에는 적용 금지]\n"
                    f"{clean_guide}"
                    if clean_guide
                    else bilingual_rule
                )
            elif eff_ko_lock:
                switch_rule = KOREAN_VOICE_OUTPUT_LOCK
            else:
                switch_rule = JAPANESE_VOICE_OUTPUT_LOCK
            payload = f"{switch_rule}\n\n{wrapped_user}"
        return self.browser.send_and_stream(
            payload,
            on_sentence=on_sentence,
            cancel_event=cancel_event,
            browser_model=browser_model,
            thinking_level=thinking_level,
            bilingual_inline=bilingual_inline,
            on_sentence_pair=on_sentence_pair,
            use_japanese_lock=use_japanese_lock,
            use_korean_lock=eff_ko_lock,
        )

    def run_background_task(
        self,
        backend_mode: str,
        api_key: str,
        api_model: str,
        prompt: str,
        browser_model: str = "hydrogen",
        thinking_level: str = "low",
        timeout_sec: float = 45.0,
    ) -> str:
        """
        Run a stateless background task (e.g., 3-stage memory summarization) using whichever
        Gemini backend is active — official API or hidden 2nd browser CDP tab ($0 extra cost).
        """
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return ""
        clean_key = (api_key or "").strip()
        if backend_mode == "api" or (clean_key and not self.browser.is_running()):
            if not clean_key:
                return ""
            return self.api.generate_background_text(
                api_key=clean_key,
                model=api_model,
                prompt=clean_prompt,
                thinking_level=thinking_level,
                timeout_sec=timeout_sec,
            )
        return self.browser.run_background_prompt(
            prompt=clean_prompt,
            model_code=browser_model,
            thinking_level=thinking_level,
            timeout_sec=timeout_sec,
            fresh_chat=True,
        )

    def close(self) -> None:
        self.browser.close()
        self.api.close()
