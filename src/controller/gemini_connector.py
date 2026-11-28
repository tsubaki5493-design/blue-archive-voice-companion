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
import hmac
import json
import math
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
from urllib.parse import urlsplit

import httpx

from src.controller.jp_conversation_coach import (
    build_jp_coach_bilingual_addon,
    build_jp_coach_turn_reminder,
)
from src.controller.persona_enhancer import (
    CHARACTER_DNA_HEADER_PREFIX,
    CHARACTER_DNA_HEADER_PREFIX_KO,
    build_variant_opening_instruction,
    get_llm_temperature_for_variant,
)
from src.utils import app_paths as _app_paths
from src.utils.app_paths import get_browser_profile_dir, get_selectors_path

IDLE_PING_TOKEN = "[IDLE_PING]"
IDLE_PING_SYSTEM_RULE = (
    "[시스템 규칙: 앞으로 내가 '[IDLE_PING]'이라고 입력하면 그 문구 자체를 언급하거나 설명하지 마. "
    "대신 우리가 대화하다 잠시 조용해진 정적 상황으로 간주하고, 직전 대화 맥락과 너의 페르소나에 맞춰 "
    "자연스럽게 먼저 말을 걸거나 새로운 질문을 던져라. 모든 대답은 실제 사람과 전화로 티키타카하듯 "
    "자연스러운 구어체로 작성하되, 평상시 일상 대화·인사·장난은 짧은 1~2문장(한 턴에 한 박자만 치고 바로 상대에게 말 넘기기, "
    "혼자 3~4문장씩 길게 읊거나 '보케+자폭+수습+새 화제'를 한 번에 몰아넣기 금지)으로 경쾌하게 주고받고, "
    "사용자가 진지한 설명·상담·위로·긴 이야기를 요청한 순간에만 분량 제한 없이 충분히 길고 풍부하게 말하며 마크다운 기호나 목록 사용을 피해라. "
    "또한 사용자 발화는 실시간 마이크 음성인식(STT) 결과이므로 캐릭터 이름이나 짧은 인사가 발음이 비슷한 오타(예: '히카리'→'시칼이'·'까리', '안녕'→'안간이')로 들어와도 "
    "지적하거나 되묻지 말고 문맥상 원래 의도로 자연스럽게 알아듣고 대답해라. "
    "사용자가 한국어로 말하거나 STT 오타가 섞여도 캐릭터 설정 언어·화법 규칙을 절대 깨뜨리지 말고 그대로 역할극을 이어가라. "
    "짧은 인사나 모호한 입력이 들어와도 직전 턴이나 첫인사에서 이미 쓴 첫 감탄사·문장 구조·특정 소재(소파, 지정석, 무릎 위 등 같은 장소·사물)를 절대 복사·재탕하지 말고, "
    "매번 시간대·간식·일과·날씨·장난·주변 인물 등 새로운 화제와 다른 첫마디로 변주해라.]"
)
IDLE_PING_COACH_SYSTEM_RULE = (
    "[시스템 규칙: 앞으로 내가 '[IDLE_PING]'이라고 입력하면 그 문구 자체를 언급하거나 설명하지 마. "
    "대신 우리가 대화하다 잠시 조용해진 정적 상황으로 간주하고, 직전 대화 맥락과 너의 페르소나에 맞춰 "
    "일본어가 아직 조금 서툰 한국인 상대도 편하게 알아들을 수 있는 쉬운 일상 일본어와 짧은 1~2문장으로 "
    "자연스럽게 먼저 말을 걸거나 가벼운 질문을 던져라. 절대 일본어 선생님처럼 구술 연습이나 따라 말하기를 시키지 말고 평소 대화처럼 짧게 티키타카 이어가라. "
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
    "3. 실제 한국어 성우가 전화 통화로 티키타카하듯 평상시엔 짧은 1~2문장으로 생동감 있게 주고받고, 깊은 상담·설명 요청 시에만 자연스럽게 길게 말하라."
)
BILINGUAL_STREAM_OUTPUT_RULE = (
    "[실시간 이중언어 출력 규칙 — 반드시 아래 형식을 엄격히 준수하라]\n"
    "1. 사용자가 한국어로 말하더라도 모든 대답을 한 문장씩 아래 형식으로만 출력하라:\n"
    "일본어 대사 || 자연스러운 한국어 번역\n"
    "2. 반드시 문장마다 줄바꿈(\\n)으로 구분하고, 구분자 `||` 왼쪽에는 100% 자연스러운 일본어(히라가나·가타카나·한자) 구어체 대사만 적어라(한국어·괄호·이모지·지문·번역 혼입 절대 금지). "
    "구분자 `||` 오른쪽에는 캐릭터 성격과 아래 자막 말투 가이드가 살아있는 자연스러운 한국어 구어체 번역만 적어라.\n"
    "3. 왼쪽과 오른쪽 순서를 절대 뒤바꾸지 말고(`일본어 || 한국어` 고정), `||` 없이 한국어만 단독으로 출력하지 마라.\n"
    "4. 평상시 대화는 실제 전화 티키타카처럼 짧은 1~2줄(1~2문장, 한 줄을 쉼표로 길게 늘이지 말 것)로 템포 있게 주고받고, 진지한 상담·자세한 설명 요청 시에만 3줄 이상으로 늘려라.\n"
    "5. 인사말, 번호 매기기, 마크다운 기호(*, #, -), 괄호 지문, 메타 설명을 절대 넣지 마라.\n"
    "출력 형식 예: <현재 페르소나의 짧은 일본어 구어체 1문장> || <그 문장의 자연스러운 한국어 번역>"
)

AI_PROVIDERS: tuple[str, ...] = ("gemini", "claude", "chatgpt")

AI_PROVIDER_LABELS: dict[str, str] = {
    "gemini": "✨ 제미니 (Gemini)",
    "claude": "🧡 클로드 (Claude)",
    "chatgpt": "💬 챗지피티 (ChatGPT)",
}

AI_PROVIDER_SHORT_NAMES: dict[str, str] = {
    "gemini": "제미니",
    "claude": "클로드",
    "chatgpt": "챗지피티",
}


def normalize_ai_provider(provider: Any) -> str:
    """Normalize an AI provider string into `'gemini'`, `'claude'`, or `'chatgpt'`."""
    raw = str(provider or "gemini").strip().lower()
    if raw in ("claude", "anthropic"):
        return "claude"
    if raw in ("chatgpt", "openai", "gpt"):
        return "chatgpt"
    return "gemini"


GEMINI_THINKING_LEVELS: list[tuple[str, str]] = [
    ("low", "⚡ 낮음 (속도·효율)"),
    ("medium", "⚖️ 보통 (균형)"),
    ("high", "💎 높음 (매우 철저함)"),
    ("xhigh", "🌌 최고 (xhigh · 심층 추론)"),
]
GEMINI_THINKING_LEVEL_LABELS: dict[str, str] = dict(GEMINI_THINKING_LEVELS)

GEMINI_BROWSER_MODELS: list[tuple[str, str]] = [
    ("auto", "✨ Auto (자동 · 무료 계정 지원)"),
    ("hydrogen", "💬 4 Hydrogen (일상생활 지원 · 무료/기본)"),
    ("neon_4", "⚡ 4 Neon (심층 분석 · Pro/Ultra)"),
    ("carbon_4", "🏆 4 카본 (코드 및 복잡한 작업 · Ultra)"),
]
GEMINI_BROWSER_MODEL_LABELS: dict[str, str] = dict(GEMINI_BROWSER_MODELS)

GEMINI_FREE_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
)

GEMINI_API_MODELS: list[tuple[str, str]] = [
    ("gemini-3.5-flash-lite", "🚀 gemini-3.5-flash-lite (무료 계정 지원 · 초저지연 기본)"),
    ("gemini-3.8-flash", "✨ gemini-3.8-flash (무료 계정 지원 · 고효율 Flash 최신)"),
    ("gemini-2.5-flash-lite", "🔹 gemini-2.5-flash-lite (무료 계정 지원 · 경량)"),
    ("gemini-2.5-flash", "🔹 gemini-2.5-flash (무료/기존 호환 · 안정)"),
    ("gemini-4-hydrogen", "💬 gemini-4-hydrogen (유료 API · 일상/고속)"),
    ("gemini-4-neon", "⚡ gemini-4-neon (유료 API · 심층 추론)"),
    ("gemini-4-carbon", "🏆 gemini-4-carbon (유료 API · 최상위)"),
    ("gemini-3.1-pro-preview", "📚 gemini-3.1-pro-preview (유료/프리뷰)"),
    ("gemini-2.5-pro", "🧠 gemini-2.5-pro (고급 추론)"),
]
GEMINI_API_MODEL_LABELS: dict[str, str] = dict(GEMINI_API_MODELS)

CLAUDE_BROWSER_MODELS: list[tuple[str, str]] = [
    ("default", "🧡 현재 웹 선택 모델 (기본 · 계정 기본값 유지)"),
    ("haiku", "⚡ Claude Haiku (초고속 대화)"),
    ("sonnet", "✨ Claude Sonnet (속도·연기 균형 권장)"),
    ("opus", "💎 Claude Opus (심층 연기 · Pro/Max)"),
    ("fable", "🏆 Claude Fable (최상위 추론 · Max)"),
]
CLAUDE_BROWSER_MODEL_LABELS: dict[str, str] = dict(CLAUDE_BROWSER_MODELS)

CLAUDE_API_MODELS: list[tuple[str, str]] = [
    ("claude-sonnet-5-5", "✨ claude-sonnet-5-5 (속도·연기 균형 권장)"),
    ("claude-haiku-5-5", "🚀 claude-haiku-5-5 (초고속·경량)"),
    ("claude-opus-5-5", "💎 claude-opus-5-5 (심층 연기·복잡 맥락)"),
    ("claude-fable-5-2", "🏆 claude-fable-5-2 (최상위 프런티어)"),
    ("claude-sonnet-4-6", "🔹 claude-sonnet-4-6 (균형 모델)"),
    ("claude-haiku-4-5", "🔹 claude-haiku-4-5 (고속 경량)"),
    ("claude-opus-4-6", "🧠 claude-opus-4-6 (심층 모델)"),
]
CLAUDE_API_MODEL_LABELS: dict[str, str] = dict(CLAUDE_API_MODELS)

CHATGPT_BROWSER_MODELS: list[tuple[str, str]] = [
    ("default", "💬 현재 웹 선택 모델 (기본 · 계정 기본값 유지)"),
    ("auto", "✨ Auto / Instant (자동 · 빠른 응답)"),
    ("mini", "⚡ Mini / Fast (초고속 대화)"),
    ("thinking", "🧠 Thinking (심층 추론)"),
    ("pro", "🏆 Pro (최상위 추론 · Pro)"),
]
CHATGPT_BROWSER_MODEL_LABELS: dict[str, str] = dict(CHATGPT_BROWSER_MODELS)

CHATGPT_API_MODELS: list[tuple[str, str]] = [
    ("gpt-5.4-mini", "🚀 gpt-5.4-mini (고속·균형 권장)"),
    ("gpt-5.4-nano", "⚡ gpt-5.4-nano (초저지연·경량)"),
    ("gpt-5.4", "✨ gpt-5.4 (고품질 연기·대화)"),
    ("gpt-6-luna", "🌙 gpt-6-luna (GPT-6 경량 고속)"),
    ("gpt-6.2-sol", "☀️ gpt-6.2-sol (GPT-6 균형)"),
    ("gpt-6.2-astra", "🌌 gpt-6.2-astra (GPT-6 최상위)"),
    ("gpt-5-mini", "🔹 gpt-5-mini (기존 고속)"),
    ("gpt-4.1-mini", "🔹 gpt-4.1-mini (기존 경량)"),
    ("gpt-4o-mini", "🔹 gpt-4o-mini (기존 호환)"),
]
CHATGPT_API_MODEL_LABELS: dict[str, str] = dict(CHATGPT_API_MODELS)


def get_provider_browser_models(provider: str) -> list[tuple[str, str]]:
    p = normalize_ai_provider(provider)
    if p == "claude":
        return list(CLAUDE_BROWSER_MODELS)
    if p == "chatgpt":
        return list(CHATGPT_BROWSER_MODELS)
    return list(GEMINI_BROWSER_MODELS)


def get_provider_api_models(provider: str) -> list[tuple[str, str]]:
    p = normalize_ai_provider(provider)
    if p == "claude":
        return list(CLAUDE_API_MODELS)
    if p == "chatgpt":
        return list(CHATGPT_API_MODELS)
    return list(GEMINI_API_MODELS)


def get_provider_default_api_model(provider: str) -> str:
    p = normalize_ai_provider(provider)
    if p == "claude":
        return "claude-sonnet-5-5"
    if p == "chatgpt":
        return "gpt-5.4-mini"
    return "gemini-3.5-flash-lite"


def get_provider_default_browser_model(provider: str) -> str:
    p = normalize_ai_provider(provider)
    if p in ("claude", "chatgpt"):
        return "default"
    return "hydrogen"

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
        return base_m, mapped_lvl
    return raw, clean_lvl


def _coerce_finite_float(value: Any, default: float, *, low: float, high: float) -> float:
    """Coerce numeric input safely into a finite float clamped to `[low, high]`."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    return max(low, min(high, num))


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
    r"(?:"
    r"(?<!\*)\*[^*\n]{1,40}\*(?!\*)"
    r"|（[^）]{1,36}）"
    r"|\([^)]{0,24}(?:"
    r"웃|미소|한숨|속삭|부끄|당황|작게|소곤|찡긋|끄덕|고개|머뭇|중얼|피식|킥킥|헛기침|어깨|살짝|조용히|작은\s*목소리"
    r"|にっこり|にやり|ふふ|くす|ぺこり|首を傾げ|頷|うなず|照れ|赤面|頬|ため息|小声|ささや|呟|つぶや|じっと|目を逸ら|苦笑|微笑|笑|咳払|慌て|拗ね|膨れ|ぽつり|震え|泣|涙|驚|目を見開|肩をすくめ|手を振|頭を抱え|小さく|そっと|静かに"
    r"|chuckles?|laughs?|giggles?|sighs?|whispers?|blushes?|smiles?|smirks?|grins?|nods?|tilts?\s+head|pouts?|stammers?|mumbles?|mutters?|coughs?|clears?\s+throat|shrugs?|waves?|softly|quietly|shyly|nervously|teasingly|playfully"
    r")[^)]{0,24}\)"
    r")",
    re.IGNORECASE,
)
_RE_CITATIONS = re.compile(r"\[\d+\]")
_RE_IDLE_ECHO = re.compile(r"\[?\s*IDLE_PING\s*\]?", re.IGNORECASE)
_RE_GEMINI_CHROME_PREFIX = re.compile(
    r"^\s*(?:Gemini의\s*응답|Gemini's\s*response|Gemini\s*の\s*回答\s*:?"
    r"|ChatGPT의\s*말\s*:?|ChatGPT\s*답변\s*:?|ChatGPT\s*said\s*:?|ChatGPT\s*の\s*回答\s*:?|ChatGPT\s*:"
    r"|Claude의\s*응답|Claude's\s*response|Claude\s*の\s*(?:応答|回答)\s*:?)\s*",
    re.IGNORECASE,
)
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


_ALLOWED_IMAGE_MIMES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})
_MAX_VISION_IMAGE_BYTES = 6 * 1024 * 1024
_RE_STUDY_VISUAL_FALLBACK = re.compile(r"\[STUDY_VISUAL\][\s\S]*?(?:\[/STUDY_VISUAL\]|$)", re.IGNORECASE)


def _normalize_vision_image(
    image_bytes: bytes | None,
    image_mime: str = "image/jpeg",
) -> tuple[bytes | None, str]:
    """Validate and normalize optional RAM-only screen image bytes and MIME type."""
    if image_bytes is None:
        return None, "image/jpeg"
    if isinstance(image_bytes, (bytearray, memoryview)):
        raw = bytes(image_bytes)
    elif isinstance(image_bytes, bytes):
        raw = image_bytes
    else:
        return None, "image/jpeg"
    if not raw or len(raw) > _MAX_VISION_IMAGE_BYTES:
        return None, "image/jpeg"
    mime = str(image_mime or "image/jpeg").strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in _ALLOWED_IMAGE_MIMES:
        return None, "image/jpeg"
    return raw, mime


def _create_study_visual_filter(enabled: bool) -> Any:
    """Lazily instantiate optional local-only StudyVisualStreamFilter when enabled and installed."""
    if not enabled:
        return None
    try:
        from src.screen_vision.prompts import StudyVisualStreamFilter

        return StudyVisualStreamFilter()
    except ImportError:
        return None
    except Exception:
        return None


def _strip_study_visual_for_storage(text: str) -> str:
    """Remove `[STUDY_VISUAL]...[/STUDY_VISUAL]` blocks before history storage or language-lock checks."""
    raw = str(text or "")
    if not raw:
        return ""
    if "[study_visual]" not in raw.lower():
        return raw.strip()
    try:
        from src.screen_vision.prompts import strip_study_visual_blocks

        cleaned, _ = strip_study_visual_blocks(raw)
        return str(cleaned or "").strip()
    except Exception:
        return _RE_STUDY_VISUAL_FALLBACK.sub("", raw).strip()


def _dispatch_study_visuals(
    study_visual_cb: Callable[[dict[str, Any]], None] | None,
    visuals: list[dict[str, Any]] | None,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """Safely invoke `study_visual_cb(spec)` for each parsed Economics Study Board dict."""
    if study_visual_cb is None or not visuals:
        return
    for vis in visuals:
        if not isinstance(vis, dict):
            continue
        try:
            study_visual_cb(vis)
        except Exception as exc:
            if log_fn is not None:
                try:
                    log_fn(f"⚠️ [StudyVisual] 콜백 처리 중 오류 무시: {type(exc).__name__}")
                except Exception:
                    pass


DEFAULT_GEMINI_URL = "https://gemini.google.com/app"
DEFAULT_CLAUDE_URL = "https://claude.ai/new"
DEFAULT_CHATGPT_URL = "https://chatgpt.com/"

DEFAULT_PROVIDER_URLS: dict[str, str] = {
    "gemini": DEFAULT_GEMINI_URL,
    "claude": DEFAULT_CLAUDE_URL,
    "chatgpt": DEFAULT_CHATGPT_URL,
}

_ALLOWED_GEMINI_WEB_HOSTS: tuple[str, ...] = ("gemini.google.com",)
_ALLOWED_GEMINI_LOGIN_HOSTS: tuple[str, ...] = ("gemini.google.com", "accounts.google.com")
_ALLOWED_GEMINI_API_HOSTS: tuple[str, ...] = ("generativelanguage.googleapis.com",)

_ALLOWED_CLAUDE_WEB_HOSTS: tuple[str, ...] = ("claude.ai",)
_ALLOWED_CLAUDE_LOGIN_HOSTS: tuple[str, ...] = (
    "claude.ai",
    "claude.com",
    "accounts.claude.ai",
    "accounts.google.com",
    "appleid.apple.com",
    "login.microsoftonline.com",
    "challenges.cloudflare.com",
)
_ALLOWED_CLAUDE_API_HOSTS: tuple[str, ...] = ("api.anthropic.com",)

_ALLOWED_CHATGPT_WEB_HOSTS: tuple[str, ...] = ("chatgpt.com", "chat.openai.com")
_ALLOWED_CHATGPT_LOGIN_HOSTS: tuple[str, ...] = (
    "chatgpt.com",
    "chat.openai.com",
    "auth.openai.com",
    "auth0.openai.com",
    "accounts.google.com",
    "appleid.apple.com",
    "login.microsoftonline.com",
    "challenges.cloudflare.com",
)
_ALLOWED_OPENAI_API_HOSTS: tuple[str, ...] = ("api.openai.com",)

_ALLOWED_PROVIDER_WEB_HOSTS: dict[str, tuple[str, ...]] = {
    "gemini": _ALLOWED_GEMINI_WEB_HOSTS,
    "claude": _ALLOWED_CLAUDE_WEB_HOSTS,
    "chatgpt": _ALLOWED_CHATGPT_WEB_HOSTS,
}
_ALLOWED_PROVIDER_LOGIN_HOSTS: dict[str, tuple[str, ...]] = {
    "gemini": _ALLOWED_GEMINI_LOGIN_HOSTS,
    "claude": _ALLOWED_CLAUDE_LOGIN_HOSTS,
    "chatgpt": _ALLOWED_CHATGPT_LOGIN_HOSTS,
}

_MAX_SELECTORS_FILE_BYTES = 256 * 1024
_MAX_SELECTOR_COUNT = 32
_MAX_SELECTOR_LEN = 512

_MAX_CDP_HANDSHAKE_BYTES = 16 * 1024
_MAX_CDP_CONTROL_FRAME_BYTES = 125
_DEFAULT_MAX_CDP_FRAME_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_CDP_MESSAGE_BYTES = 8 * 1024 * 1024
_MAX_CDP_FRAGMENTS = 256
_MAX_CDP_SEND_BYTES = 4 * 1024 * 1024
_MAX_CDP_HTTP_JSON_BYTES = 2 * 1024 * 1024

_MAX_GEMINI_API_JSON_BYTES = 4 * 1024 * 1024
_MAX_GEMINI_API_ERROR_BYTES = 8 * 1024
_MAX_GEMINI_SSE_LINE_CHARS = 512 * 1024
_MAX_GEMINI_SSE_TOTAL_CHARS = 2 * 1024 * 1024

_RE_CTRL_OR_WS = re.compile(r"[\x00-\x20\x7f]")
_RE_CTRL_NON_SPACE = re.compile(r"[\x00-\x1f\x7f]")
_RE_SAFE_GEMINI_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_RE_REDACT_DPAPI = re.compile(r"dpapi:[A-Za-z0-9+/=]{16,}")
_RE_REDACT_AIZA = re.compile(r"AIza[0-9A-Za-z\-_]{20,}")
_RE_REDACT_SK = re.compile(r"\bsk-[0-9A-Za-z\-_]{16,}\b")
_RE_REDACT_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._\-~+/]+=*")
_RE_REDACT_HEADER_SECRET = re.compile(r"(?i)((?:x-goog-api-key|x-api-key|authorization)\s*[:=]\s*['\"]?)[^\s'\",;}\]]+")
_RE_REDACT_QUERY_SECRET = re.compile(r"(?i)([?&](?:key|api_key|apikey|token|access_token)=)[^&\s'\"<>]+")
_RE_REDACT_JSON_SECRET = re.compile(
    r'(?i)("(?:fish_api_key|gemini_api_key|claude_api_key|openai_api_key|api_key)"\s*:\s*")[^"]+(")'
)


def _redact_secrets_fallback(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Fallback secret scrubber active even before app_paths.redact_secrets is imported."""
    out = str(text or "")
    for sec in extra_secrets:
        s = str(sec or "").strip()
        if s and len(s) >= 4:
            out = out.replace(s, "[REDACTED]")
    out = _RE_REDACT_DPAPI.sub("dpapi:[REDACTED]", out)
    out = _RE_REDACT_AIZA.sub("[REDACTED_GEMINI_KEY]", out)
    out = _RE_REDACT_SK.sub("[REDACTED_API_KEY]", out)
    out = _RE_REDACT_BEARER.sub(r"\1[REDACTED]", out)
    out = _RE_REDACT_HEADER_SECRET.sub(r"\1[REDACTED]", out)
    out = _RE_REDACT_QUERY_SECRET.sub(r"\1[REDACTED]", out)
    out = _RE_REDACT_JSON_SECRET.sub(r"\1[REDACTED]\2", out)
    return out


def _scrub_secrets(text: Any, extra_secrets: tuple[str, ...] = ()) -> str:
    """Scrub API keys, tokens, and DPAPI blobs using app_paths.redact_secrets with local fallback."""
    raw = str(text or "")
    fn = getattr(_app_paths, "redact_secrets", None)
    if callable(fn):
        try:
            raw = str(fn(raw, extra_secrets=extra_secrets))
        except TypeError:
            try:
                raw = str(fn(raw))
            except Exception:
                pass
        except Exception:
            pass
    return _redact_secrets_fallback(raw, extra_secrets=extra_secrets)


def is_valid_provider_url(url: Any, provider: str = "gemini") -> bool:
    """Return True strictly for safe HTTPS URLs on the selected provider's allowlisted web host."""
    p = normalize_ai_provider(provider)
    allowed_hosts = _ALLOWED_PROVIDER_WEB_HOSTS.get(p, _ALLOWED_GEMINI_WEB_HOSTS)
    if not isinstance(url, str):
        return False
    raw = url.strip()
    if not raw or len(raw) > 512 or _RE_CTRL_OR_WS.search(raw) or "\\" in raw:
        return False
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    if (parsed.hostname or "").lower() not in allowed_hosts:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if port not in (None, 443):
        return False
    if parsed.path and (
        not parsed.path.startswith("/")
        or "/../" in parsed.path
        or parsed.path.endswith("/..")
        or parsed.path == "/.."
    ):
        return False
    return True


def validate_provider_url(url: Any, provider: str = "gemini", fallback: str | None = None) -> str:
    """Validate a provider web URL strictly against its allowlisted HTTPS domain, falling back safely."""
    p = normalize_ai_provider(provider)
    eff_fallback = fallback if fallback is not None else DEFAULT_PROVIDER_URLS[p]
    if is_valid_provider_url(url, provider=p):
        return str(url).strip()
    return eff_fallback


def is_valid_gemini_url(url: Any) -> bool:
    """Return True strictly for safe `https://gemini.google.com/...` URLs."""
    return is_valid_provider_url(url, provider="gemini")


def validate_gemini_url(url: Any, fallback: str = DEFAULT_GEMINI_URL) -> str:
    """Validate a Gemini web URL strictly against `https://gemini.google.com/...`, falling back safely."""
    return validate_provider_url(url, provider="gemini", fallback=fallback)


def _validate_https_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    """Enforce strict HTTPS-only URL to an explicitly allowlisted host."""
    raw = str(url or "").strip()
    if not raw or len(raw) > 2048 or _RE_CTRL_OR_WS.search(raw):
        raise ValueError("Invalid HTTPS URL")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid HTTPS URL") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("Refusing non-HTTPS outbound URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Refusing credentials in outbound URL")
    if (parsed.hostname or "").lower() not in allowed_hosts:
        raise ValueError(f"Refusing untrusted outbound host: {parsed.hostname!r}")
    if port not in (None, 443):
        raise ValueError(f"Refusing non-standard HTTPS port: {port!r}")
    return raw


_RE_UNSAFE_MODEL_CHARS = re.compile(r"(?:\.\.|[/\\?#&=;<>\x00-\x1f\x7f])")
_RE_PROVIDER_MODEL_TOKENS: dict[str, re.Pattern[str]] = {
    "gemini": re.compile(r"\b(gemini-[A-Za-z0-9][A-Za-z0-9._-]{1,120})\b"),
    "claude": re.compile(r"\b(claude-[A-Za-z0-9][A-Za-z0-9._-]{1,120})\b"),
    "chatgpt": re.compile(r"\b((?:gpt-[A-Za-z0-9]|o[1-9])[A-Za-z0-9._-]{1,120})\b"),
}
_GEMINI_NON_CHAT_TOKENS: tuple[str, ...] = (
    "embedding",
    "aqa",
    "imagen",
    "veo",
    "tts",
    "robotics",
    "computer-use",
    "decision",
    "lyria",
    "native-audio",
    "-image",
    "image-generation",
    "deep-research",
)


def extract_safe_api_model_id(raw: Any, provider: str = "gemini") -> str:
    """
    Extract a validated API model identifier from raw config/UI input (exact ID, `models/<id>`,
    or known UI display label such as `🚀 gemini-3.5-flash-lite (...)`).
    Returns `""` for empty, cross-provider/browser labels, or any path/query injection attempt.
    """
    p = normalize_ai_provider(provider)
    s = str(raw or "").strip()
    if not s:
        return ""
    for code, lbl in get_provider_api_models(p):
        if s == code or s == lbl:
            return code
    if s.lower().startswith("models/"):
        tail = s[7:].strip()
        if tail and ".." not in tail and _RE_SAFE_GEMINI_MODEL.fullmatch(tail):
            return tail
        return ""
    if ".." not in s and _RE_SAFE_GEMINI_MODEL.fullmatch(s):
        return s
    if _RE_UNSAFE_MODEL_CHARS.search(s):
        return ""
    pat = _RE_PROVIDER_MODEL_TOKENS.get(p)
    if pat is not None:
        m = pat.search(s)
        if m:
            cand = m.group(1).strip()
            if cand and ".." not in cand and _RE_SAFE_GEMINI_MODEL.fullmatch(cand):
                return cand
    return ""


def coerce_safe_api_model_id(raw: Any, provider: str = "gemini", default: str | None = None) -> str:
    """Return a guaranteed-safe API model ID for `provider`, falling back to `default` on corruption."""
    p = normalize_ai_provider(provider)
    eff_default = default if (default and _RE_SAFE_GEMINI_MODEL.fullmatch(default)) else get_provider_default_api_model(p)
    extracted = extract_safe_api_model_id(raw, provider=p)
    return extracted or eff_default


def pick_best_gemini_free_model(available_models: list[str], preferred: str = "") -> str:
    """Choose the best free-tier-compatible Gemini text model from `available_models`."""
    clean_avail: list[str] = []
    seen: set[str] = set()
    for m in available_models or []:
        cid = extract_safe_api_model_id(m, provider="gemini")
        if not cid or cid in seen:
            continue
        low = cid.lower()
        if not low.startswith("gemini-"):
            continue
        if any(tok in low for tok in _GEMINI_NON_CHAT_TOKENS):
            continue
        seen.add(cid)
        clean_avail.append(cid)
    if not clean_avail:
        return get_provider_default_api_model("gemini")
    pref = extract_safe_api_model_id(preferred, provider="gemini")
    if pref and pref in seen:
        return pref
    for cand in GEMINI_FREE_FALLBACK_MODELS:
        if cand in seen:
            return cand
    flash_lites = sorted([m for m in clean_avail if "flash-lite" in m.lower()], reverse=True)
    if flash_lites:
        return flash_lites[0]
    flashes = sorted([m for m in clean_avail if "flash" in m.lower()], reverse=True)
    if flashes:
        return flashes[0]
    return clean_avail[0]


def _validate_api_model_id(model: str, default: str = "gemini-3.5-flash-lite", provider_label: str = "AI") -> str:
    """Validate an API model identifier before interpolating into REST paths or payloads."""
    clean = (model or default).strip()
    if clean.lower().startswith("models/"):
        clean = clean[7:].strip()
    if not clean or ".." in clean or not _RE_SAFE_GEMINI_MODEL.fullmatch(clean):
        raise ValueError(f"Invalid {provider_label} API model identifier")
    return clean


def _validate_gemini_model_id(model: str) -> str:
    """Validate a Gemini API model identifier before interpolating into REST paths."""
    return _validate_api_model_id(model, default="gemini-3.5-flash-lite", provider_label="Gemini")


_RE_API_KEY_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _validate_api_key_header(api_key: Any, provider_label: str) -> str:
    """Validate and trim an API key before sending it in HTTP headers (blocks header injection and non-ASCII key leaks)."""
    clean_key = str(api_key or "").strip()
    if not clean_key:
        raise RuntimeError(f"{provider_label} API Key가 비어 있습니다. [⚙️ 설정 열기]에서 키를 입력해 주세요.")
    if _RE_API_KEY_CTRL.search(clean_key) or not clean_key.isascii() or len(clean_key) > 4096:
        raise RuntimeError(
            f"{provider_label} API Key 형식이 올바르지 않습니다. 영문·숫자·기호로 된 유효한 키만 공백·줄바꿈 없이 입력해 주세요."
        )
    return clean_key


def _js_literal(value: Any) -> str:
    """Safely serialize Python data into an ASCII-escaped JavaScript literal."""
    return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")


def _is_trusted_page_url(url: Any, allowed_hosts: tuple[str, ...]) -> bool:
    if not isinstance(url, str):
        return False
    raw = url.strip()
    if not raw or _RE_CTRL_OR_WS.search(raw):
        return False
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and (parsed.hostname or "").lower() in allowed_hosts


_DEFAULT_SELECTORS: dict[str, Any] = {
    "url": DEFAULT_GEMINI_URL,
    "input_selectors": [
        "div.ql-editor[aria-label*='Gemini']",
        "rich-textarea div.ql-editor[contenteditable='true']",
        "rich-textarea div[contenteditable='true']",
        "div[role='textbox'][contenteditable='true']",
        "div[contenteditable='true']",
        "textarea",
    ],
    "send_button_selectors": [
        "button[aria-label*='메시지 보내기']:not([disabled]):not([aria-disabled='true'])",
        "button.send-button:not([disabled]):not([aria-disabled='true'])",
        "button[aria-label*='전송']:not([disabled]):not([aria-disabled='true'])",
        "button[aria-label*='보내기']:not([disabled]):not([aria-disabled='true'])",
        "button[aria-label*='Send']:not([disabled]):not([aria-disabled='true'])",
        "button[aria-label*='送信']:not([disabled]):not([aria-disabled='true'])",
        "send-button button:not([disabled])",
    ],
    "stop_button_selectors": [
        "button[aria-label*='대답 생성 중지']",
        "button[aria-label*='Stop response']",
        "button[aria-label*='Stop generating']",
        "input-area-v2 button[aria-label*='중지']",
        "input-area-v2 button[aria-label*='Stop']",
        "input-area-v2 button[aria-label*='停止']",
        "form button[aria-label*='중지']",
        "form button[aria-label*='Stop']",
        "form button[aria-label*='停止']",
        ".input-area button[aria-label*='중지']",
        ".input-area button[aria-label*='Stop']",
    ],
    "response_selectors": [
        "model-response message-content .markdown",
        "model-response .model-response-text",
        "model-response message-content",
        "message-content .markdown",
        "model-response structured-content-container",
        "model-response .response-content",
    ],
    "new_chat_selectors": [
        "a[aria-label*='새 채팅']",
        "button[aria-label*='새 채팅']",
        "a[aria-label*='New chat']",
        "button[aria-label*='New chat']",
        "a[href='/app']",
    ],
    "login_required_selectors": [
        "a[href*='accounts.google.com/ServiceLogin']",
        "a[aria-label*='로그인']",
        "a[aria-label*='Sign in']",
        "button[aria-label*='로그인']",
        "button[aria-label*='Sign in']",
    ],
}

_DEFAULT_CLAUDE_SELECTORS: dict[str, Any] = {
    "url": DEFAULT_CLAUDE_URL,
    "input_selectors": [
        "div[data-testid='chat-input'][contenteditable='true']",
        "div.ProseMirror[contenteditable='true'][role='textbox']",
        "div.ProseMirror[contenteditable='true']",
        "fieldset div[contenteditable='true']",
        "div[role='textbox'][contenteditable='true']",
        "textarea",
    ],
    "send_button_selectors": [
        "button[aria-label='Send message']:not([disabled]):not([aria-disabled='true'])",
        "button[aria-label='메시지 보내기']:not([disabled]):not([aria-disabled='true'])",
        "button[data-testid='send-button']:not([disabled]):not([aria-disabled='true'])",
        "fieldset button[aria-label*='Send']:not([aria-label*='feedback' i]):not([disabled]):not([aria-disabled='true'])",
        "form button[aria-label*='Send']:not([aria-label*='feedback' i]):not([disabled]):not([aria-disabled='true'])",
        "fieldset button[aria-label*='보내기']:not([aria-label*='피드백']):not([disabled]):not([aria-disabled='true'])",
        "fieldset button[aria-label*='전송']:not([aria-label*='피드백']):not([disabled]):not([aria-disabled='true'])",
        "button[aria-label*='메시지 보내기']:not([disabled]):not([aria-disabled='true'])",
        "button[aria-label*='送信']:not([disabled]):not([aria-disabled='true'])",
    ],
    "stop_button_selectors": [
        "button[data-testid='stop-button']",
        "button[aria-label='Stop response']",
        "button[aria-label='Stop generating']",
        "button[aria-label='응답 중지']",
        "button[aria-label='생성 중지']",
        "fieldset button[aria-label*='Stop']",
        "form button[aria-label*='Stop']",
        "div[role='button'][aria-label*='Stop response']",
        "fieldset button[aria-label*='중지']",
        "fieldset button[aria-label*='停止']",
    ],
    "response_selectors": [
        ".font-claude-response",
        ".font-claude-message",
        "[data-testid='assistant-message']",
        "div[data-test-render-count] .font-claude-response",
    ],
    "new_chat_selectors": [
        "a[href='/new']",
        "a[aria-label*='New chat']",
        "button[aria-label*='New chat']",
        "a[aria-label*='새 채팅']",
        "button[aria-label*='새 채팅']",
    ],
    "login_required_selectors": [
        "input[type='email']",
        "input[name='email']",
        "button[data-testid='login-button']",
        "button[data-testid*='login']",
        "a[href*='/login']",
        "a[href*='/signup']",
        "button[aria-label*='Continue with Google']",
        "button[aria-label*='Log in']",
        "button[aria-label*='Sign in']",
    ],
}

_DEFAULT_CHATGPT_SELECTORS: dict[str, Any] = {
    "url": DEFAULT_CHATGPT_URL,
    "input_selectors": [
        "div.ProseMirror[data-composer-markdown][contenteditable='true']",
        "div[data-composer-markdown][contenteditable='true']",
        "#prompt-textarea",
        "div#prompt-textarea[contenteditable='true']",
        "div.ProseMirror#prompt-textarea",
        "#mobile-composer-prompt",
        "textarea[name='prompt']",
        "div[role='textbox'][contenteditable='true']",
        "textarea[data-id='root']",
        "textarea",
    ],
    "send_button_selectors": [
        "button[data-testid='send-button']:not([disabled]):not([aria-disabled='true']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "#composer-submit-button:not([disabled]):not([aria-disabled='true']):not([data-testid='stop-button']):not([data-testid='composer-speech-button']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "button[data-testid='composer-submit-button']:not([disabled]):not([aria-disabled='true']):not([data-testid='stop-button']):not([data-testid='composer-speech-button']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "button[data-composer-submit]:not([disabled]):not([aria-disabled='true']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "button[aria-label*='Send prompt']:not([disabled]):not([aria-disabled='true']):not([data-testid='stop-button']):not([data-testid='composer-speech-button']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "button[aria-label*='프롬프트 보내기']:not([disabled]):not([aria-disabled='true']):not([data-testid='stop-button']):not([data-testid='composer-speech-button']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "button[aria-label*='메시지 보내기']:not([disabled]):not([aria-disabled='true']):not([data-testid='stop-button']):not([data-testid='composer-speech-button']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "button[aria-label*='보내기']:not([disabled]):not([aria-disabled='true']):not([data-testid='stop-button']):not([data-testid='composer-speech-button']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "button[aria-label*='Send']:not([disabled]):not([aria-disabled='true']):not([data-testid='stop-button']):not([data-testid='composer-speech-button']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "button[aria-label*='送信']:not([disabled]):not([aria-disabled='true']):not([data-testid='stop-button']):not([data-testid='composer-speech-button']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
        "form button[type='submit'][aria-label]:not([disabled]):not([aria-disabled='true']):not([data-testid='stop-button']):not([data-testid='composer-speech-button']):not([data-stop-generating]):not([data-visually-disabled]):not([data-composer-voice-start])",
    ],
    "stop_button_selectors": [
        "button[data-testid='stop-button']",
        "button[data-stop-generating]",
        "button[aria-label*='Stop streaming']",
        "button[aria-label*='Stop generating']",
        "button[aria-label*='스트리밍 중지']",
        "button[aria-label*='생성 중단']",
        "button[aria-label*='생성 중지']",
        "form button[aria-label*='중단']",
        "form button[aria-label*='중지']",
        "form button[aria-label*='Stop']",
        "form button[aria-label*='停止']",
        "[data-composer-footer-responsive] button[aria-label*='중단']",
        "[data-composer-footer-responsive] button[aria-label*='중지']",
        "[data-composer-footer-responsive] button[aria-label*='Stop']",
        "[data-composer-footer-responsive] button[aria-label*='停止']",
        "[data-testid='composer-background'] button[aria-label*='중단']",
        "[data-testid='composer-background'] button[aria-label*='중지']",
        "[data-testid='composer-background'] button[aria-label*='Stop']",
    ],
    "response_selectors": [
        "[data-content-search-unit-key$=':assistant'] [data-markdown-text-style='assistant-message']",
        "[data-chatgpt-search-unit-key$=':assistant'] [data-markdown-text-style='assistant-message']",
        "[data-markdown-text-style='assistant-message']",
        "[data-response-body] [data-dil-response]",
        "[data-dil-response]",
        "[data-message-author-role='assistant'] .markdown",
        "[data-message-role='assistant'] [data-assistant-markdown]",
        "[data-message-author-role='assistant'] .prose",
        "article[data-turn='assistant'] .markdown",
        "[data-message-role='assistant'] [data-assistant-markdown-root]",
        "[data-content-search-unit-key$=':assistant']",
        "[data-chatgpt-search-unit-key$=':assistant']",
        "[data-response-body]",
        "[data-message-author-role='assistant']",
        "[data-message-role='assistant']",
        "article[data-turn='assistant']",
    ],
    "new_chat_selectors": [
        "a[data-testid='create-new-chat-button']",
        "button[data-testid='create-new-chat-button']",
        "button[aria-label='새 채팅']",
        "button[aria-label='New chat']",
        "a[aria-label*='New chat']:not([href*='/c/']):not([data-interactive-row-link])",
        "button[aria-label*='New chat']:not([aria-label*=' in ' i])",
        "a[aria-label*='새 채팅']:not([href*='/c/']):not([data-interactive-row-link])",
        "button[aria-label*='새 채팅']:not([aria-label*='에서 새 채팅'])",
    ],
    "login_required_selectors": [
        "form[data-logged-out]",
        "form[action*='/unauth-']",
        "[data-header-auth-actions]",
        "button[data-mobile-auth-entry-action='login']",
        "form[action*='/auth/login']",
        "a[href*='/auth/login']",
        "button[data-testid*='login']",
        "a[data-testid*='login']",
        "button[data-testid='signup-button']",
        "a[href*='auth.openai.com']",
    ],
}

_DEFAULT_PROVIDER_SELECTORS: dict[str, dict[str, Any]] = {
    "gemini": _DEFAULT_SELECTORS,
    "claude": _DEFAULT_CLAUDE_SELECTORS,
    "chatgpt": _DEFAULT_CHATGPT_SELECTORS,
}

_SELECTOR_LIST_KEYS: tuple[str, ...] = (
    "input_selectors",
    "send_button_selectors",
    "stop_button_selectors",
    "response_selectors",
    "new_chat_selectors",
    "login_required_selectors",
)


def _sanitize_selector_list(raw: Any, fallback: list[str]) -> list[str]:
    if not isinstance(raw, list):
        return list(fallback)
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        clean = item.strip()
        if not clean or len(clean) > _MAX_SELECTOR_LEN or _RE_CTRL_NON_SPACE.search(clean):
            continue
        out.append(clean)
        if len(out) >= _MAX_SELECTOR_COUNT:
            break
    return out if out else list(fallback)


_UNSCOPED_GENERIC_STOP_SELS = frozenset(
    {
        "button[aria-label*='stop']",
        'button[aria-label*="stop"]',
        "button[aria-label*='중지']",
        'button[aria-label*="중지"]',
        "button[aria-label*='중단']",
        'button[aria-label*="중단"]',
        "button[aria-label*='停止']",
        'button[aria-label*="停止"]',
    }
)


def _merge_default_selectors(provider: str, key: str, user_list: list[str], default_list: list[str]) -> list[str]:
    """
    Deep-merge critical default selectors into a user-overridden selector list while
    stripping legacy/hazardous overrides that break 2026 ChatGPT Octane or Claude DOM.
    """
    filtered: list[str] = []
    seen: set[str] = set()
    for sel in user_list:
        low = sel.lower()
        # Drop legacy Claude streaming-attr or bare markdown-block selectors that break Turn 2+ baseCount tracking
        if provider == "claude" and key == "response_selectors":
            if "data-is-streaming" in low:
                continue
            if low in (
                ".standard-markdown",
                "div.standard-markdown",
                ".progressive-markdown",
                "div.progressive-markdown",
            ) or (".standard-markdown" in low and ".font-claude-response" not in low):
                continue
        # Drop legacy unscoped Claude Send/보내기/전송 selectors that can click "Send feedback" / "피드백 보내기"
        if provider == "claude" and key == "send_button_selectors":
            if low.startswith("button[aria-label*='send']") and "feedback" not in low:
                continue
            if (
                low.startswith(("button[aria-label*='보내기']", 'button[aria-label*="보내기"]', "button[aria-label*='전송']", 'button[aria-label*="전송"]'))
                and "피드백" not in low
            ):
                continue
        # Drop bare submit / unguarded ChatGPT send selectors that click cookie banners or Voice/Stop
        if provider == "chatgpt" and key == "send_button_selectors":
            if low in ("button[type='submit']", 'button[type="submit"]'):
                continue
            if "data-testid='send-button'" in low or 'data-testid="send-button"' in low:
                if "data-stop-generating" not in low or "data-visually-disabled" not in low:
                    continue
            if ("composer-submit-button" in low or "aria-label*=" in low) and "data-visually-disabled" not in low:
                continue
        # Drop unscoped generic Stop/중지/중단/停止 selectors on ChatGPT & Gemini that can false-match unrelated buttons
        if provider in ("chatgpt", "gemini") and key == "stop_button_selectors":
            if low in _UNSCOPED_GENERIC_STOP_SELS:
                continue
        if sel not in seen:
            seen.add(sel)
            filtered.append(sel)

    missing_defaults = [s for s in default_list if s not in seen]
    if not missing_defaults:
        return filtered[:_MAX_SELECTOR_COUNT] if filtered else list(default_list)[:_MAX_SELECTOR_COUNT]

    if not filtered:
        return list(default_list)[:_MAX_SELECTOR_COUNT]

    if key == "input_selectors" and "textarea" in filtered:
        # Insert specific missing defaults (e.g. #mobile-composer-prompt) BEFORE bare `textarea`
        ta_idx = filtered.index("textarea")
        merged = filtered[:ta_idx] + [s for s in missing_defaults if s != "textarea"] + filtered[ta_idx:]
    elif provider == "claude" and key == "response_selectors":
        # Ensure `.font-claude-response` always sits at the front ahead of custom/catch-all selectors
        if ".font-claude-response" in missing_defaults:
            merged = [".font-claude-response"] + filtered + [s for s in missing_defaults if s != ".font-claude-response"]
        else:
            merged = filtered + missing_defaults
    elif provider == "chatgpt" and key == "response_selectors":
        # Keep specific markdown selectors before broad assistant container catch-alls
        broad_set = {
            "[data-content-search-unit-key$=':assistant']",
            "[data-chatgpt-search-unit-key$=':assistant']",
            "[data-response-body]",
            "[data-message-author-role='assistant']",
            "[data-message-role='assistant']",
            "article[data-turn='assistant']",
        }
        insert_idx = next((i for i, s in enumerate(filtered) if s in broad_set), len(filtered))
        specific_missing = [s for s in missing_defaults if s not in broad_set]
        broad_missing = [s for s in missing_defaults if s in broad_set]
        merged = filtered[:insert_idx] + specific_missing + filtered[insert_idx:] + broad_missing
    else:
        merged = filtered + missing_defaults

    dedup: list[str] = []
    seen_final: set[str] = set()
    for s in merged:
        if s not in seen_final:
            seen_final.add(s)
            dedup.append(s)
            if len(dedup) >= _MAX_SELECTOR_COUNT:
                break
    return dedup


def _try_read_selectors_dict(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists() and path.is_file() and path.stat().st_size <= _MAX_SELECTORS_FILE_BYTES:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def load_selectors_config(app_root: Path | None = None, provider: str = "gemini") -> dict[str, Any]:
    """Load, strictly validate, and deep-merge CSS selectors and provider URL from selectors.json."""
    p = normalize_ai_provider(provider)
    defaults = _DEFAULT_PROVIDER_SELECTORS[p]
    sel_path = get_selectors_path(app_root)
    loaded = _try_read_selectors_dict(sel_path)
    if loaded is None:
        # User selectors.json missing/corrupt/oversized -> fall back to bundled config/selectors.json before hardcoded defaults
        bundled_root = app_root if app_root is not None else Path(__file__).resolve().parents[2]
        bundled_path = bundled_root / "config" / "selectors.json"
        try:
            same_file = bundled_path.resolve() == sel_path.resolve()
        except Exception:
            same_file = False
        if not same_file:
            loaded = _try_read_selectors_dict(bundled_path)
    if loaded is None:
        loaded = {}

    if p == "gemini":
        section = loaded
    else:
        providers_map = loaded.get("providers")
        section = providers_map.get(p, {}) if isinstance(providers_map, dict) else {}
        if not isinstance(section, dict):
            section = {}

    sanitized: dict[str, Any] = {
        "url": validate_provider_url(section.get("url"), provider=p, fallback=DEFAULT_PROVIDER_URLS[p]),
    }
    for key in _SELECTOR_LIST_KEYS:
        user_list = _sanitize_selector_list(section.get(key), defaults[key])
        sanitized[key] = _merge_default_selectors(p, key, user_list, defaults[key])
    return sanitized


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
_RE_JP_AND_KANJI_TTS_STRIP = re.compile(
    r"[\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff々〆ヵヶ]+"
    r"(?:[\s、。！？〜ー・]*[\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff々〆ヵヶ]+)*"
    r"[、。！？〜ー・]*(?:[!?,.\s]+(?=\s|[\uac00-\ud7a3]|$))?"
)
_RE_KO_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?])")
_RE_KO_DUP_PUNCT = re.compile(r"([,])(?:\s*[,])+")
_RE_KO_LEADING_PUNCT = re.compile(r"^[\s,.;:!?、。！？・]+")
_RE_KO_TO_JP_BOUNDARY = re.compile(
    r"(?:\[VOICE:[A-Za-z0-9_-]{1,32}\]\s*)?(?:(?<=\s)…+\s*)?[「『（(]?[\u3040-\u30ff\u4e00-\u9fff]"
)


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
    """Clean TTS text for Korean-reply mode, stripping stray `||` bilingual leaks, JP kana/kanji, and orphaned JP punct."""
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
    s = _RE_JP_AND_KANJI_TTS_STRIP.sub(" ", s)
    s = s.replace("、", ", ").replace("。", ". ").replace("！", "!").replace("？", "?")
    s = _RE_MULTI_SPACE.sub(" ", s)
    s = _RE_KO_SPACE_BEFORE_PUNCT.sub(r"\1", s)
    s = _RE_KO_DUP_PUNCT.sub(r"\1", s)
    s = _RE_KO_LEADING_PUNCT.sub("", s)
    s = s.rstrip(" ,、・").strip()
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


_RE_INLINE_MD_DRIFT = re.compile(r"[*_`~]+")


def _extract_cumulative_dom_delta(prev_full: str, new_full: str) -> str:
    """
    Compute the newly appended suffix between two cumulative DOM `innerText` snapshots,
    staying resilient when ChatGPT/Claude markdown conversion (e.g. `**word**` -> `<strong>word</strong>`)
    shrinks or shifts earlier characters mid-stream.
    """
    if not new_full:
        return ""
    if not prev_full:
        return new_full
    if new_full.startswith(prev_full):
        return new_full[len(prev_full) :]
    if prev_full.startswith(new_full):
        return ""

    # Find longest common prefix
    max_lcp = min(len(prev_full), len(new_full))
    lcp = 0
    while lcp < max_lcp and prev_full[lcp] == new_full[lcp]:
        lcp += 1

    # Anchor using trailing markdown-stripped substring of prev_full so formatting collapses never skip/duplicate chars
    prev_tail_clean = _RE_INLINE_MD_DRIFT.sub("", prev_full)
    if prev_tail_clean:
        search_start = max(0, lcp - 16)
        max_anchor = min(28, len(prev_tail_clean))
        min_anchor = 2 if len(prev_tail_clean) >= 2 else 1
        for k in range(max_anchor, min_anchor - 1, -1):
            anchor = prev_tail_clean[-k:]
            if not anchor.strip():
                continue
            pos = new_full.rfind(anchor, search_start)
            if pos != -1:
                return new_full[pos + len(anchor) :]

    # Fallback when full prefix changed unexpectedly without shrinking
    if len(new_full) > len(prev_full):
        return new_full[len(prev_full) :]
    return ""


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
        self._last_seen_full = ""
        self._active_pair: tuple[str, Future[str]] | None = None

    def reset(self) -> None:
        self._buffer = ""
        self._seen_len = 0
        self._last_seen_full = ""
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
        delta = _extract_cumulative_dom_delta(self._last_seen_full, cleaned_full)
        self._last_seen_full = cleaned_full
        self._seen_len = len(cleaned_full)
        return self.feed_delta(delta)

    @staticmethod
    def _split_ko_then_jp_segment(seg: str) -> tuple[str, str, int]:
        """Split an intermediate `KO_1 JP_2` segment (between two `||` separators) into `(ko_part, jp_part, jp_start_idx)`."""
        if not seg:
            return "", "", 0
        hangul_matches = list(_RE_HANGUL_ALL.finditer(seg))
        if not hangul_matches:
            return "", seg, 0
        last_h_end = hangul_matches[-1].end()
        tail = seg[last_h_end:]
        m_jp = _RE_KO_TO_JP_BOUNDARY.search(tail)
        if m_jp is None:
            return seg, "", len(seg)
        jp_start = last_h_end + m_jp.start()
        return seg[:jp_start], seg[jp_start:], jp_start

    def feed_delta(self, delta: str) -> list[tuple[str, Future[str]]]:
        if not delta:
            return []
        self._buffer += delta
        emitted: list[tuple[str, Future[str]]] = []

        while "\n" in self._buffer:
            line, rest = self._buffer.split("\n", 1)
            self._buffer = rest
            emitted.extend(self._finalize_line_pairs(line))

        # Active incomplete line: emit each JP sentence as soon as its `||` arrives,
        # and if multiple `JP || KO` pairs share one line without newlines, split & emit sequentially.
        while self._buffer:
            seps = list(_BILINGUAL_SEP_RE.finditer(self._buffer))
            if not seps:
                break
            if self._active_pair is None:
                sep = seps[0]
                jp_raw = self._buffer[: sep.start()]
                if not is_hangul_dominant(jp_raw):
                    jp_clean = sanitize_japanese_tts_text(jp_raw, preserve_voice_tag=True)
                    if len(strip_voice_persona_tags(jp_clean)) >= self.min_sentence_chars and has_japanese_script(jp_clean):
                        kr_fut: Future[str] = Future()
                        self._active_pair = (jp_clean, kr_fut)
                        emitted.append((jp_clean, kr_fut))
                    else:
                        break
                else:
                    break
            if len(seps) >= 2 and self._active_pair is not None:
                between = self._buffer[seps[0].end() : seps[1].start()]
                ko_part, jp_next, jp_offset = self._split_ko_then_jp_segment(between)
                if jp_next and has_japanese_script(jp_next):
                    _, fut = self._active_pair
                    self._active_pair = None
                    if not fut.done():
                        fut.set_result(sanitize_korean_subtitle_out(ko_part))
                    self._buffer = self._buffer[seps[0].end() + jp_offset :]
                    continue
            break

        return emitted

    def _build_completed_pair(self, left_raw: str, right_raw: str) -> tuple[str, Future[str]] | None:
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

    def _finalize_line_pairs(self, line: str) -> list[tuple[str, Future[str]]]:
        seps = list(_BILINGUAL_SEP_RE.finditer(line))
        if len(seps) >= 2:
            segments: list[str] = []
            prev_end = 0
            for m in seps:
                segments.append(line[prev_end : m.start()])
                prev_end = m.end()
            segments.append(line[prev_end:])

            results: list[tuple[str, Future[str]]] = []
            cur_left = segments[0]
            for seg_idx in range(1, len(segments)):
                raw_mid = segments[seg_idx]
                if seg_idx < len(segments) - 1:
                    ko_part, next_left, _ = self._split_ko_then_jp_segment(raw_mid)
                else:
                    ko_part, next_left = raw_mid, ""

                if seg_idx == 1 and self._active_pair is not None:
                    _, fut = self._active_pair
                    self._active_pair = None
                    if not fut.done():
                        fut.set_result(sanitize_korean_subtitle_out(ko_part))
                else:
                    pair = self._build_completed_pair(cur_left, ko_part)
                    if pair is not None:
                        results.append(pair)
                cur_left = next_left
            return results

        if len(seps) == 1:
            sep = seps[0]
            left_raw = line[: sep.start()]
            right_raw = line[sep.end() :]
            if self._active_pair is not None:
                _, fut = self._active_pair
                self._active_pair = None
                ko_clean = sanitize_korean_subtitle_out(right_raw)
                if not fut.done():
                    fut.set_result(ko_clean)
                return []

            pair = self._build_completed_pair(left_raw, right_raw)
            return [pair] if pair is not None else []

        # Fallback if a complete line arrived without `||`
        if self._active_pair is not None:
            _, fut = self._active_pair
            self._active_pair = None
            if not fut.done():
                fut.set_result("")
        if is_hangul_dominant(line):
            return []
        jp_clean = sanitize_japanese_tts_text(line, preserve_voice_tag=True)
        if len(strip_voice_persona_tags(jp_clean)) >= self.min_sentence_chars and has_japanese_script(jp_clean):
            empty_fut: Future[str] = Future()
            empty_fut.set_result("")
            return [(jp_clean, empty_fut)]
        return []

    def _finalize_line(self, line: str) -> tuple[str, Future[str]] | None:
        pairs = self._finalize_line_pairs(line)
        return pairs[0] if pairs else None

    def flush(self) -> list[tuple[str, Future[str]]]:
        emitted: list[tuple[str, Future[str]]] = []
        rem = self._buffer
        self._buffer = ""
        if rem or self._active_pair is not None:
            emitted.extend(self._finalize_line_pairs(rem))
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
        self._last_seen_full = ""
        self._line_voice_tag = ""

    def reset(self) -> None:
        self._buffer = ""
        self._seen_len = 0
        self._last_seen_full = ""
        self._line_voice_tag = ""

    def feed(self, full_accumulated_text: str) -> list[str]:
        """Feed cumulative response text (e.g. from DOM MutationObserver) and emit new finished sentences."""
        if not full_accumulated_text:
            return []
        cleaned_full = _RE_GEMINI_CHROME_PREFIX.sub("", full_accumulated_text)
        if not cleaned_full:
            return []
        delta = _extract_cumulative_dom_delta(self._last_seen_full, cleaned_full)
        self._last_seen_full = cleaned_full
        self._seen_len = len(cleaned_full)
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
            v_tag, v_body = extract_voice_tag_prefix(tagged_candidate)
            cleaned_body = sanitize_gemini_spoken_text(v_body)
            cleaned = f"[VOICE:{v_tag}] {cleaned_body}" if (v_tag and cleaned_body) else cleaned_body
            if len(cleaned_body) >= self.min_sentence_chars:
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
        rem_body = sanitize_gemini_spoken_text(body)
        rem = f"[VOICE:{eff_tag}] {rem_body}" if (eff_tag and rem_body) else rem_body
        if len(rem_body) >= 1:
            return [rem]
        return []


def _is_character_dna_already_embedded(clean_persona: str, clean_dna: str) -> bool:
    """Return True if `clean_dna` is already contained inside `clean_persona` to prevent duplicate injection."""
    if not clean_dna:
        return True
    if not clean_persona:
        return False
    if clean_dna in clean_persona:
        return True
    first_line = clean_dna.splitlines()[0].strip()
    if first_line and first_line in clean_persona:
        return True
    for hdr in (CHARACTER_DNA_HEADER_PREFIX, CHARACTER_DNA_HEADER_PREFIX_KO):
        if hdr and (hdr in clean_dna) and (hdr in clean_persona):
            return True
    return False


def build_first_message_prompt(
    persona_prompt: str,
    use_japanese_lock: bool = False,
    bilingual_inline: bool = False,
    ko_style_guide: str = "",
    jp_beginner_coach: bool = False,
    use_korean_lock: bool = False,
    memory_context: str = "",
    variant_directive: str = "",
    character_dna_block: str = "",
) -> str:
    """Combine user/character persona, optional Canon DNA block, memory context, high-recency variant directive, and output rules."""
    clean_persona = (persona_prompt or "").strip()
    clean_dna = (character_dna_block or "").strip()
    clean_guide = (ko_style_guide or "").strip()
    clean_memory = (memory_context or "").strip()
    clean_variant_dir = (variant_directive or "").strip()
    parts = []
    if clean_persona:
        parts.append(f"[캐릭터 페르소나 설정]\n{clean_persona}")
    if clean_dna and not _is_character_dna_already_embedded(clean_persona, clean_dna):
        parts.append(clean_dna)
    if clean_memory:
        parts.append(clean_memory)
    if (not use_japanese_lock) and clean_guide:
        parts.append(clean_guide)
    parts.append(IDLE_PING_COACH_SYSTEM_RULE if (jp_beginner_coach and not use_korean_lock) else IDLE_PING_SYSTEM_RULE)
    if clean_variant_dir:
        # Place 2-A~2-D variant contrast rules near the high-recency tail so boilerplate never buries them
        parts.append(clean_variant_dir)
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


_IDLE_PING_SILENCE_CUE = (
    "(무음 감지: 선생님이 잠시 말이 없으니 캐릭터 본인의 성격과 말투로 먼저 자연스럽게 말을 걸어라. `[IDLE_PING]` 단어는 절대 말하지 마라.)"
)


def _strip_turn_wrapper_for_history(text: str) -> str:
    """Extract the clean user utterance from a wrapped per-turn prompt so API history never duplicates boilerplate."""
    raw = (text or "").strip()
    if not raw:
        return ""
    if raw.startswith(IDLE_PING_TOKEN):
        return IDLE_PING_TOKEN
    prefix = "[선생님의 말]\n"
    if raw.startswith(prefix):
        body = raw[len(prefix) :]
        markers = (
            "\n\n[출력 ",
            "\n\n[일본어 눈높이 배려",
            "\n\n[커스텀 페르소나 유지",
            "\n\n[🌸",
            "\n\n[페르소나",
            "\n\n[캐릭터",
            "\n\n[VOICE",
            "\n\n(출력 ",
            "\n\n(무음 ",
        )
        earliest = -1
        for marker in markers:
            idx = body.find(marker)
            if idx != -1 and (earliest == -1 or idx < earliest):
                earliest = idx
        if earliest != -1:
            body = body[:earliest]
        cleaned = body.strip()
        if cleaned:
            return cleaned
    return raw


def wrap_user_turn_for_japanese_lock(
    user_text: str,
    use_japanese_lock: bool,
    bilingual_inline: bool = False,
    jp_beginner_coach: bool = False,
    use_korean_lock: bool = False,
    persona_anchor: str = "",
) -> str:
    """Wrap per-turn user input to enforce output language AND keep the active persona anchored."""
    raw = (user_text or "").strip()
    if not raw:
        return user_text
    anchor = (persona_anchor or "").strip()
    anchor_suffix = f"\n{anchor}" if anchor else ""
    is_idle = (raw == IDLE_PING_TOKEN) or (IDLE_PING_TOKEN in raw)

    if use_korean_lock and not use_japanese_lock:
        if is_idle:
            return (
                f"{raw}\n"
                f"{_IDLE_PING_SILENCE_CUE}\n"
                f"(출력 언어 강제: 일본어·`||` 금지, 캐릭터의 100% 자연스러운 한국어 구어체 대사만 출력){anchor_suffix}"
            )
        return (
            f"[선생님의 말]\n{raw}\n\n"
            "[출력 언어 강제: 일본어·번역·`||` 구분자·메타 설명 절대 금지. "
            f"반드시 캐릭터 본인의 말투(반말/존댓말 유지)로 100% 자연스러운 한국어 구어체 대사만 출력하라]{anchor_suffix}"
        )
    if not use_japanese_lock:
        if is_idle:
            return f"{raw}\n{_IDLE_PING_SILENCE_CUE}{anchor_suffix}"
        if anchor:
            return f"[선생님의 말]\n{raw}\n\n{anchor}"
        return user_text
    if jp_beginner_coach:
        reminder = build_jp_coach_turn_reminder(
            bilingual_inline=bilingual_inline,
            is_idle_ping=is_idle,
        )
        if is_idle:
            return f"{raw}\n{reminder}{anchor_suffix}"
        return f"[선생님의 말]\n{raw}\n\n{reminder}{anchor_suffix}"
    if is_idle:
        if bilingual_inline:
            return (
                f"{raw}\n"
                f"{_IDLE_PING_SILENCE_CUE}\n"
                f"(출력 형식 강제: 반드시 `일본어 대사 || 한국어 번역` 형식으로만 말할 것){anchor_suffix}"
            )
        return (
            f"{raw}\n"
            f"{_IDLE_PING_SILENCE_CUE}\n"
            f"(출력 언어 강제: 한국어 금지, 캐릭터의 100% 자연스러운 일본어 구어체 대사만 출력){anchor_suffix}"
        )
    if bilingual_inline:
        return (
            f"[선생님의 말]\n{raw}\n\n"
            "[출력 형식 강제: 한국어 질문이더라도 한국어 단독 응답·설명 금지. "
            f"반드시 한 문장씩 `일본어 대사 || 한국어 번역` 형식으로만 대답하라]{anchor_suffix}"
        )
    return (
        f"[선생님의 말]\n{raw}\n\n"
        "[출력 언어 강제: 위 한국어 말에 대답할 때도 한국어·한글·번역·메타 설명 절대 금지. "
        f"반드시 캐릭터 본인으로서 100% 자연스러운 일본어 구어체 대사만 출력하라]{anchor_suffix}"
    )


def _needs_japanese_repair_retry(
    raw_text: str,
    emitted_text: str,
    use_japanese_lock: bool,
    bilingual_active: bool = False,
) -> bool:
    """Return True when a Japanese-locked reply produced non-empty non-Japanese or Hangul-dominated text
    AND nothing valid in Japanese has been emitted to TTS yet (preventing spliced double audio).
    """
    _ = bilingual_active
    if not use_japanese_lock:
        return False
    raw = (raw_text or "").strip()
    if not raw:
        return False
    emitted_clean = strip_voice_persona_tags(emitted_text or "").strip()
    if emitted_clean and has_japanese_script(emitted_clean) and not is_hangul_dominant(emitted_clean):
        return False
    return is_hangul_dominant(raw) or (not has_japanese_script(raw))


def _needs_korean_repair_retry(
    raw_text: str,
    emitted_text: str,
    use_korean_lock: bool,
) -> bool:
    """Return True when a Korean-locked reply produced Japanese-only or non-Korean text
    AND nothing valid in Korean has been emitted to TTS yet (preventing spliced double audio).
    """
    if not use_korean_lock:
        return False
    raw = (raw_text or "").strip()
    if not raw:
        return False
    emitted_clean = strip_voice_persona_tags(emitted_text or "").strip()
    if emitted_clean and has_korean_script(emitted_clean) and not is_japanese_dominant(emitted_clean):
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
    Connects strictly to loopback without an Origin header and enforces strict frame/message bounds.
    """

    MAX_HANDSHAKE_BYTES = _MAX_CDP_HANDSHAKE_BYTES
    MAX_CONTROL_FRAME_BYTES = _MAX_CDP_CONTROL_FRAME_BYTES
    DEFAULT_MAX_FRAME_BYTES = _DEFAULT_MAX_CDP_FRAME_BYTES
    DEFAULT_MAX_MESSAGE_BYTES = _DEFAULT_MAX_CDP_MESSAGE_BYTES
    MAX_FRAGMENTS = _MAX_CDP_FRAGMENTS
    MAX_SEND_BYTES = _MAX_CDP_SEND_BYTES

    def __init__(
        self,
        ws_url: str,
        timeout: float = 10.0,
        max_frame_bytes: int | None = None,
        max_message_bytes: int | None = None,
    ) -> None:
        if not isinstance(ws_url, str) or len(ws_url) > 2048 or _RE_CTRL_OR_WS.search(ws_url):
            raise ValueError("Invalid CDP WebSocket URL")
        try:
            parsed = urlsplit(ws_url)
            parsed_port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid CDP WebSocket URL") from exc
        if parsed.scheme != "ws":
            raise ValueError(f"Only local ws:// CDP URLs are supported: {parsed.scheme!r}")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("Refusing credentials or fragment in CDP WebSocket URL")
        host = (parsed.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"Refusing non-local CDP host: {host}")
        port = parsed_port if parsed_port is not None else 80
        if not (1 <= port <= 65535):
            raise ValueError(f"Invalid CDP port: {port}")

        req_path = parsed.path or "/"
        if parsed.query:
            req_path = f"{req_path}?{parsed.query}"
        if not req_path.startswith("/") or not all(0x21 <= ord(ch) <= 0x7E for ch in req_path):
            raise ValueError("Invalid CDP WebSocket request path")

        self.max_message_bytes = max(1024, int(max_message_bytes or self.DEFAULT_MAX_MESSAGE_BYTES))
        self.max_frame_bytes = min(self.max_message_bytes, max(1024, int(max_frame_bytes or self.DEFAULT_MAX_FRAME_BYTES)))

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._recv_buf = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._next_id = 1

        try:
            sec_key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            # Deliberately omit Origin header so Chromium/Edge CDP accepts local non-browser
            # clients without enabling wildcard remote-allow-origins.
            req = (
                f"GET {req_path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {sec_key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            self._sock.sendall(req.encode("ascii"))

            while b"\r\n\r\n" not in self._recv_buf:
                if len(self._recv_buf) >= self.MAX_HANDSHAKE_BYTES:
                    raise ConnectionError("CDP WebSocket handshake header exceeds safety limit")
                chunk = self._sock.recv(min(4096, self.MAX_HANDSHAKE_BYTES - len(self._recv_buf)))
                if not chunk:
                    raise ConnectionError("CDP WebSocket handshake closed unexpectedly")
                self._recv_buf.extend(chunk)

            hdr_end = bytes(self._recv_buf).index(b"\r\n\r\n") + 4
            if hdr_end > self.MAX_HANDSHAKE_BYTES:
                raise ConnectionError("CDP WebSocket handshake header exceeds safety limit")
            hdr_bytes = bytes(self._recv_buf[:hdr_end])
            del self._recv_buf[:hdr_end]

            hdr_lines = hdr_bytes.decode("latin-1", errors="replace").splitlines()
            if not hdr_lines:
                raise ConnectionError("Empty CDP WebSocket handshake response")
            status_parts = hdr_lines[0].split(" ", 2)
            if len(status_parts) < 2 or not status_parts[0].upper().startswith("HTTP/1.") or status_parts[1] != "101":
                raise ConnectionError(f"CDP WebSocket handshake rejected: {hdr_lines[0][:120]}")

            resp_headers: dict[str, str] = {}
            for line in hdr_lines[1:]:
                if ":" in line:
                    hk, hv = line.split(":", 1)
                    resp_headers[hk.strip().lower()] = hv.strip()

            expected_accept = base64.b64encode(
                hashlib.sha1((sec_key + _WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            actual_accept = resp_headers.get("sec-websocket-accept", "")
            if not hmac.compare_digest(actual_accept, expected_accept):
                raise ConnectionError("CDP WebSocket Sec-WebSocket-Accept mismatch")
        except Exception:
            try:
                self._sock.close()
            except Exception:
                pass
            raise

    def _read_exact(self, n: int) -> bytes:
        if n < 0 or n > self.max_frame_bytes:
            self.close()
            raise ValueError(f"CDP WebSocket frame length {n} exceeds limit {self.max_frame_bytes}")
        while len(self._recv_buf) < n:
            needed = n - len(self._recv_buf)
            try:
                chunk = self._sock.recv(min(65536, max(4096, needed)))
            except (TimeoutError, socket.timeout):
                # Mid-frame timeout desynchronizes WebSocket framing; close so callers reconnect cleanly.
                self.close()
                raise
            if not chunk:
                self.close()
                raise ConnectionError("CDP WebSocket connection closed")
            self._recv_buf.extend(chunk)
        out = bytes(self._recv_buf[:n])
        del self._recv_buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        length = len(payload)
        if opcode >= 0x8 and length > self.MAX_CONTROL_FRAME_BYTES:
            raise ValueError(f"CDP WebSocket control frame too large: {length}")
        if length > self.MAX_SEND_BYTES:
            raise ValueError(f"CDP WebSocket outbound payload exceeds limit: {length}")
        mask_key = os.urandom(4)
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
        total_bytes = 0
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

            if opcode >= 0x8:
                if (not fin) or length > self.MAX_CONTROL_FRAME_BYTES:
                    self.close()
                    raise ValueError(f"Invalid CDP WebSocket control frame (fin={fin}, len={length})")
            else:
                if length > self.max_frame_bytes:
                    self.close()
                    raise ValueError(f"CDP WebSocket frame exceeds max size ({length} > {self.max_frame_bytes})")
                if total_bytes + length > self.max_message_bytes:
                    self.close()
                    raise ValueError(
                        f"CDP WebSocket message exceeds max size ({total_bytes + length} > {self.max_message_bytes})"
                    )
                if len(fragments) >= self.MAX_FRAGMENTS:
                    self.close()
                    raise ValueError("CDP WebSocket message exceeds maximum fragment count")

            mask_key = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

            if opcode == 0x8:  # Close
                self.close()
                raise ConnectionError("CDP WebSocket closed by browser")
            if opcode == 0x9:  # Ping -> reply Pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # Pong
                continue

            if opcode == 0x0:
                if not fragments:
                    self.close()
                    raise ValueError("Unexpected CDP WebSocket continuation frame")
            else:
                if fragments:
                    self.close()
                    raise ValueError("Interleaved CDP WebSocket data frame during fragmentation")
                msg_opcode = opcode

            fragments.append(payload)
            total_bytes += length
            if fin:
                return msg_opcode, b"".join(fragments)

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise ConnectionError(f"CDP WebSocket is closed ({method})")
            msg_id = self._next_id
            self._next_id += 1
            body = json.dumps({"id": msg_id, "method": method, "params": params or {}}, ensure_ascii=False).encode(
                "utf-8"
            )
            self._sock.settimeout(timeout)
            try:
                self._send_frame(0x1, body)
            except Exception:
                self.close()
                raise
            deadline = time.monotonic() + timeout
            while True:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    self.close()
                    raise TimeoutError(f"CDP call timed out ({method})")
                self._sock.settimeout(rem)
                opcode, raw = self._recv_message()
                if opcode != 0x1:
                    continue
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("id") == msg_id:
                    if "error" in data:
                        raise RuntimeError(_scrub_secrets(f"CDP error ({method}): {data['error']}"))
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
            raise RuntimeError(_scrub_secrets(f"JS error: {desc}"))
        return (res.get("result") or {}).get("value")

    def close(self) -> None:
        self._closed = True
        acquired = False
        try:
            acquired = self._lock.acquire(blocking=False)
            if acquired:
                try:
                    self._sock.settimeout(0.5)
                    self._send_frame(0x8, b"")
                except Exception:
                    pass
        finally:
            if acquired:
                self._lock.release()
        try:
            self._sock.close()
        except Exception:
            pass


def find_system_browser_exe() -> Path | None:
    """Locate system-installed Microsoft Edge or Google Chrome executable (resistant to CWD planting)."""
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
        if c.is_file():
            return c.resolve()

    cwd_resolved = Path.cwd().resolve()
    safe_path_entries: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip()
        if not entry or entry in (".", ".."):
            continue
        try:
            if Path(entry).resolve() == cwd_resolved:
                continue
        except OSError:
            continue
        safe_path_entries.append(entry)
    sanitized_path = os.pathsep.join(safe_path_entries)
    if not sanitized_path:
        return None
    for name in ("msedge.exe" if os.name == "nt" else "msedge", "chrome.exe" if os.name == "nt" else "chrome"):
        found = shutil.which(name, path=sanitized_path)
        if found:
            fp = Path(found).resolve()
            if fp.is_file() and (os.name != "nt" or fp.suffix.lower() == ".exe"):
                return fp
    return None


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_USER32_LOCK = threading.Lock()
_USER32_DLL: Any = None
_WNDENUMPROC: Any = None


def _get_private_user32() -> tuple[Any, Any]:
    """Return a lazily initialized private WinDLL('user32') instance without mutating global ctypes.windll.user32."""
    global _USER32_DLL, _WNDENUMPROC
    if _USER32_DLL is not None and _WNDENUMPROC is not None:
        return _USER32_DLL, _WNDENUMPROC
    with _USER32_LOCK:
        if _USER32_DLL is None or _WNDENUMPROC is None:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
            u32.EnumWindows.argtypes = [enum_proc, ctypes.c_void_p]
            u32.EnumWindows.restype = ctypes.c_int
            u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            u32.GetWindowThreadProcessId.restype = ctypes.c_uint32
            u32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
            u32.GetClassNameW.restype = ctypes.c_int
            u32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
            u32.GetWindowTextLengthW.restype = ctypes.c_int
            get_long = getattr(u32, "GetWindowLongPtrW", u32.GetWindowLongW)
            set_long = getattr(u32, "SetWindowLongPtrW", u32.SetWindowLongW)
            get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            set_long.restype = ctypes.c_ssize_t
            u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u32.ShowWindow.restype = ctypes.c_int
            u32.IsIconic.argtypes = [ctypes.c_void_p]
            u32.IsIconic.restype = ctypes.c_int
            u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            u32.SetForegroundWindow.restype = ctypes.c_int
            u32.SetWindowPos.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            u32.SetWindowPos.restype = ctypes.c_int
            u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
            u32.PostMessageW.restype = ctypes.c_int
            _USER32_DLL = u32
            _WNDENUMPROC = enum_proc
        return _USER32_DLL, _WNDENUMPROC


def _get_windows_for_pids(target_pids: set[int]) -> list[int]:
    if os.name != "nt" or not target_pids:
        return []
    user32, wnd_enum_proc = _get_private_user32()
    found: list[int] = []

    def _cb(hwnd: int, _lparam: int) -> int:
        pid = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in target_pids:
            cls_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls_buf, 64)
            if "Chrome_WidgetWin" in cls_buf.value and user32.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
        return 1

    user32.EnumWindows(wnd_enum_proc(_cb), 0)
    return found


def _request_graceful_browser_close(proc: Any, timeout_sec: float = 1.8) -> bool:
    """
    Send WM_CLOSE to browser top-level windows on Windows so Chromium flushes
    BrowserProfile SQLite cookies/session WAL cleanly before process exit.
    Returns True if the process exited cleanly before `timeout_sec`.
    """
    if os.name != "nt" or proc is None:
        return False
    try:
        if proc.poll() is not None:
            return True
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return False
        hwnds = _get_windows_for_pids(_get_child_pids(pid))
        if not hwnds:
            return False
        user32, _ = _get_private_user32()
        WM_CLOSE = 0x0010
        for hwnd in hwnds:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        try:
            proc.wait(timeout=max(0.2, float(timeout_sec)))
            return proc.poll() is not None
        except subprocess.TimeoutExpired:
            return False
    except Exception:
        return False


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

    def __init__(
        self,
        app_root: Path | None = None,
        log_cb: Callable[[str], None] | None = None,
        provider: str = "gemini",
    ) -> None:
        self.app_root = app_root
        self.log_cb = log_cb or (lambda _m: None)
        self.provider = normalize_ai_provider(provider)
        self._active_browser_provider = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._cdp: LocalCDPClient | None = None
        self._cdp_port: int | None = None
        self.selected_model_code = get_provider_default_browser_model(self.provider)
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
        self._thread_turn_count = 0
        self._main_stream_active = False
        self._abort_event = threading.Event()
        self._lock = threading.RLock()
        self._io_lock = threading.RLock()

    def get_thread_turn_count(self) -> int:
        return int(self._thread_turn_count)

    def reset_thread_turn_count(self) -> None:
        self._thread_turn_count = 0

    def _provider_name_kr(self) -> str:
        return AI_PROVIDER_SHORT_NAMES.get(self.provider, "AI")

    def _load_active_selectors(self) -> dict[str, Any]:
        if self.provider == "gemini":
            return load_selectors_config(self.app_root)
        return load_selectors_config(self.app_root, provider=self.provider)

    def reload_selectors(self) -> dict[str, Any]:
        """Reload selectors from `app_paths.get_selectors_path()` without touching BrowserProfile."""
        with self._io_lock:
            self._upsell_warned.clear()
            return self._load_active_selectors()

    def _is_in_existing_thread(self, pathname: str) -> bool:
        p = str(pathname or "").strip()
        if self.provider == "claude":
            return p.startswith("/chat/")
        if self.provider == "chatgpt":
            return p.startswith(("/c/", "/uc/")) or "/c/" in p
        return p.startswith("/app/") and p.rstrip("/") != "/app"

    def abort_active_prompt(self) -> None:
        """Cooperatively signal any in-flight browser prompt, boot, navigation, or model sync to abort promptly."""
        self._abort_event.set()

    def _is_aborted(self, cancel_event: threading.Event | None = None) -> bool:
        return self._abort_event.is_set() or (cancel_event is not None and cancel_event.is_set())

    def is_provider_active(self, provider: str) -> bool:
        """Lock-free check whether the isolated browser is currently running for `provider`."""
        return self.provider == normalize_ai_provider(provider) and self.is_running()

    def set_provider(self, provider: str, model_code: str | None = None) -> bool:
        """Switch active web provider (`gemini` | `claude` | `chatgpt`), fast-pathing without `_io_lock` when unchanged."""
        new_p = normalize_ai_provider(provider)
        if new_p == self.provider:
            if model_code:
                self.selected_model_code = str(model_code).strip()
            return False
        self.abort_active_prompt()
        with self._io_lock:
            changed = new_p != self.provider
            if changed:
                if self.is_running() or self._cdp is not None:
                    self._terminate_proc(graceful=True)
                self.provider = new_p
                self._active_browser_provider = ""
                self.login_mode_active = False
                self._conversation_primed = False
                self._thread_turn_count = 0
                self._upsell_warned.clear()
                self.selected_model_code = (
                    str(model_code).strip() if model_code else get_provider_default_browser_model(new_p)
                )
            elif model_code:
                self.selected_model_code = str(model_code).strip()
            self._abort_event.clear()
            return changed

    def _log(self, msg: str) -> None:
        try:
            self.log_cb(_scrub_secrets(msg))
        except Exception:
            pass

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _close_translator_target(self) -> None:
        """Close the hidden 2nd CDP tab via `Target.closeTarget` and tear down its WebSocket without leaking HWNDs."""
        with self._trans_lock:
            trans_cdp = self._trans_cdp
            target_id = self._trans_target_id
            port = self._cdp_port
            self._trans_cdp = None
            self._trans_target_id = None
            self._trans_hwnd = None
            self._trans_turn_count = 0
            if trans_cdp is not None:
                try:
                    trans_cdp.close()
                except Exception:
                    pass
            if target_id and port and self.is_running():
                try:
                    ver = self._cdp_http_get_json(port, "/json/version", timeout_sec=1.5)
                    browser_ws_raw = str((ver if isinstance(ver, dict) else {}).get("webSocketDebuggerUrl") or "")
                    if browser_ws_raw:
                        browser_ws = self._validate_cdp_ws_for_port(browser_ws_raw, port)
                        tmp = LocalCDPClient(browser_ws, timeout=3.0)
                        try:
                            tmp.call("Target.closeTarget", {"targetId": target_id}, timeout=3.0)
                        finally:
                            tmp.close()
                except Exception:
                    pass

    def _terminate_proc(self, *, graceful: bool = False) -> None:
        with self._trans_lock:
            self._close_translator_target()
        if self._cdp is not None:
            try:
                self._cdp.close()
            except Exception:
                pass
            self._cdp = None
        self._cdp_port = None
        if self._proc is not None:
            proc = self._proc
            try:
                if proc.poll() is None:
                    exited = _request_graceful_browser_close(proc, timeout_sec=1.8) if graceful else False
                    if (not exited) and proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3.0)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            try:
                                proc.wait(timeout=2.0)
                            except Exception:
                                pass
            except Exception:
                pass
            self._proc = None

    @staticmethod
    def _apply_hwnd_visibility(hwnd: int, visible: bool) -> None:
        if os.name != "nt" or not hwnd:
            return
        user32, _ = _get_private_user32()
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)

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
        proc = self._proc
        if os.name != "nt" or proc is None:
            return False
        pids = _get_child_pids(proc.pid)
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

    @staticmethod
    def _cdp_http_get_json(port: int, endpoint: str, timeout_sec: float = 3.0) -> Any:
        """Query local 127.0.0.1 CDP JSON endpoint with proxy bypass, no redirects, and bounded size."""
        port_i = int(port)
        if not (1 <= port_i <= 65535):
            raise ValueError(f"Invalid CDP port: {port_i}")
        if endpoint not in ("/json", "/json/version"):
            raise ValueError(f"Unexpected CDP endpoint: {endpoint!r}")
        url = f"http://127.0.0.1:{port_i}{endpoint}"
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_sec, connect=min(timeout_sec, 2.0)),
        ) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"CDP HTTP {resp.status_code}")
                cl_raw = resp.headers.get("content-length")
                if cl_raw is not None:
                    try:
                        if int(cl_raw) > _MAX_CDP_HTTP_JSON_BYTES:
                            raise RuntimeError("CDP HTTP JSON response exceeds safety limit")
                    except ValueError:
                        pass
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > _MAX_CDP_HTTP_JSON_BYTES:
                        raise RuntimeError("CDP HTTP JSON response exceeds safety limit")
                return json.loads(buf.decode("utf-8", errors="replace"))

    @staticmethod
    def _validate_cdp_ws_for_port(ws_url: Any, expected_port: int) -> str:
        if not isinstance(ws_url, str) or _RE_CTRL_OR_WS.search(ws_url):
            raise ValueError("Invalid CDP WebSocket URL from browser")
        try:
            parsed = urlsplit(ws_url)
            parsed_port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid CDP WebSocket URL from browser") from exc
        if parsed.scheme != "ws" or (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Refusing non-local CDP WebSocket target")
        if parsed_port != int(expected_port):
            raise ValueError("Refusing CDP WebSocket port mismatch")
        return ws_url

    def _connect_cdp(
        self,
        port: int,
        timeout_sec: float = 10.0,
        cancel_event: threading.Event | None = None,
    ) -> LocalCDPClient:
        deadline = time.monotonic() + timeout_sec
        last_err: Exception | None = None
        allowed_login_hosts = _ALLOWED_PROVIDER_LOGIN_HOSTS.get(self.provider, _ALLOWED_GEMINI_LOGIN_HOSTS)
        while time.monotonic() < deadline:
            if self._is_aborted(cancel_event):
                raise InterruptedError("CDP_CONNECT_CANCELLED")
            proc = self._proc
            if proc is not None:
                exit_code = proc.poll()
                if exit_code is not None:
                    raise RuntimeError(
                        f"[BROWSER_PROFILE_LOCKED] 전용 브라우저가 시작 직후 종료되었습니다 (code={exit_code}). "
                        "이미 열린 AI 전용 브라우저 창이 프로필(BrowserProfile)을 잠그고 있을 수 있습니다. "
                        "열린 AI 브라우저 창을 모두 닫거나 ⚙️ 설정 → 🌐 브라우저 초기화를 실행해 주세요."
                    )
            try:
                targets = self._cdp_http_get_json(port, "/json", timeout_sec=1.5)
                if isinstance(targets, list):
                    page = next(
                        (
                            t
                            for t in targets
                            if isinstance(t, dict)
                            and t.get("type") == "page"
                            and _is_trusted_page_url(t.get("url", ""), allowed_login_hosts)
                        ),
                        None,
                    ) or next((t for t in targets if isinstance(t, dict) and t.get("type") == "page"), None)
                    if page and page.get("webSocketDebuggerUrl"):
                        ws_url = self._validate_cdp_ws_for_port(page["webSocketDebuggerUrl"], port)
                        return LocalCDPClient(ws_url, timeout=12.0)
            except Exception as exc:
                last_err = exc
            time.sleep(0.2)
        raise RuntimeError(_scrub_secrets(f"브라우저 CDP 연결 시간 초과: {last_err}"))

    def _check_dom_login_state(self, cdp: LocalCDPClient, selectors: dict[str, Any]) -> str:
        """Return 'READY', 'LOGIN_REQUIRED', 'CHALLENGE_REQUIRED', or 'LOADING' for the active AI provider."""
        provider = self.provider
        allowed_web = list(_ALLOWED_PROVIDER_WEB_HOSTS.get(provider, _ALLOWED_GEMINI_WEB_HOSTS))
        login_hosts = [
            h
            for h in _ALLOWED_PROVIDER_LOGIN_HOSTS.get(provider, _ALLOWED_GEMINI_LOGIN_HOSTS)
            if h not in allowed_web and h != "challenges.cloudflare.com"
        ]
        is_gemini = provider == "gemini"
        is_claude = provider == "claude"
        is_chatgpt = provider == "chatgpt"
        check_login_first = provider in ("claude", "chatgpt")
        login_paths = (
            ["/login", "/onboarding", "/signup", "/oauth", "/magic-link", "/verify"]
            if provider == "claude"
            else (["/auth", "/login"] if provider == "chatgpt" else [])
        )
        account_avatar_sels = [
            "a[aria-label*='Google 계정']",
            "a[aria-label*='Google Account']",
            "button[aria-label*='Google 계정']",
            "button[aria-label*='Google Account']",
            "img.gb_O",
            "a[href*='SignOutOptions']",
        ]
        js = f"""
        (() => {{
            const proto = (location.protocol || "").toLowerCase();
            const host = (location.hostname || "").toLowerCase();
            const pathname = (location.pathname || "").toLowerCase();
            const allowedWeb = {_js_literal(allowed_web)};
            const loginHosts = {_js_literal(login_hosts)};
            const loginPaths = {_js_literal(login_paths)};
            const isGemini = {_js_literal(is_gemini)};
            const isClaude = {_js_literal(is_claude)};
            const isChatGpt = {_js_literal(is_chatgpt)};
            const checkLoginFirst = {_js_literal(check_login_first)};
            const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            const titleLow = (document.title || "").toLowerCase();
            const hasCfChallenge = () => {{
                if (host === "challenges.cloudflare.com") return true;
                if (
                    titleLow.includes("just a moment") ||
                    titleLow.includes("attention required") ||
                    titleLow.includes("잠시만 기다려") ||
                    titleLow.includes("잠시만 기다리십시오") ||
                    titleLow.includes("사람인지 확인")
                ) {{
                    return true;
                }}
                const cfSels = [
                    "#challenge-running",
                    "#challenge-stage",
                    "#turnstile-wrapper",
                    "iframe[src*='challenges.cloudflare.com']",
                    "form#challenge-form",
                    "div.cf-browser-verification"
                ];
                for (const s of cfSels) {{
                    try {{
                        const el = document.querySelector(s);
                        if (el && (isVisible(el) || s === "#challenge-running" || s === "#challenge-stage")) return true;
                    }} catch (e) {{}}
                }}
                return false;
            }};
            if (hasCfChallenge()) return "CHALLENGE_REQUIRED";
            if (loginHosts.includes(host)) return "LOGIN_REQUIRED";
            if (proto !== "https:" || !allowedWeb.includes(host)) return "LOADING";
            for (const lp of loginPaths) {{
                if (pathname === lp || pathname.startsWith(lp.endsWith("/") ? lp : (lp + "/"))) return "LOGIN_REQUIRED";
            }}
            if (checkLoginFirst && document.readyState === "loading") {{
                return "LOADING";
            }}
            const inputSels = {_js_literal(selectors.get("input_selectors", []))};
            const loginSels = {_js_literal(selectors.get("login_required_selectors", []))};
            const avatarSels = {_js_literal(account_avatar_sels)};
            const hasBlockingAuthModal = () => {{
                if (!checkLoginFirst) return false;
                try {{
                    if (isChatGpt) {{
                        const mobAuth = document.querySelector("#mobile-auth-dialog");
                        if (isVisible(mobAuth)) return true;
                    }}
                    const dialogs = Array.from(document.querySelectorAll("[role='dialog'][aria-modal='true'], [role='alertdialog']")).filter(isVisible);
                    const authModalRe = /log\\s*in|sign\\s*in|sign\\s*up|continue with google|continue with email|terms of service|privacy policy|age verification|verify your phone|18 years|로그인|회원가입|계정으로 계속|이용약관|개인정보|전화번호 인증|연령 확인/i;
                    for (const dlg of dialogs) {{
                        if (dlg.querySelector("input[type='email'], input[name='email'], input[type='password']")) return true;
                        const dtxt = (dlg.innerText || "").replace(/\\s+/g, " ").trim();
                        if (dtxt && authModalRe.test(dtxt)) return true;
                    }}
                }} catch (e) {{}}
                return false;
            }};
            const hasLogin = () => {{
                for (const s of loginSels) {{
                    try {{
                        const el = document.querySelector(s);
                        if (isVisible(el)) return true;
                    }} catch (e) {{}}
                }}
                if (isChatGpt) {{
                    try {{
                        const ctaRe = /^(?:로그인|무료로\\s*회원가입|회원가입|log\\s*in|sign\\s*in|sign\\s*up(?:\\s*for\\s*free)?)$/i;
                        const nodes = Array.from(document.querySelectorAll("button, a")).filter(isVisible);
                        for (const n of nodes) {{
                            const t = (n.innerText || n.textContent || "").replace(/\\s+/g, " ").trim();
                            if (t && t.length <= 36 && ctaRe.test(t)) return true;
                        }}
                    }} catch (e) {{}}
                }} else if (isClaude) {{
                    try {{
                        const ctaRe = /^(?:log\\s*in|sign\\s*in|sign\\s*up|continue\\s+with\\s+google|continue\\s+with\\s+email|continue\\s+with\\s+apple|로그인|회원가입|google로\\s*계속|이메일로\\s*계속)$/i;
                        const nodes = Array.from(document.querySelectorAll("button, a")).filter(isVisible);
                        for (const n of nodes) {{
                            const t = (n.innerText || n.textContent || "").replace(/\\s+/g, " ").trim();
                            if (t && t.length <= 44 && ctaRe.test(t)) return true;
                        }}
                    }} catch (e) {{}}
                }}
                if (hasBlockingAuthModal()) return true;
                return false;
            }};
            const hasAccountAvatar = () => {{
                for (const s of avatarSels) {{
                    try {{
                        const el = document.querySelector(s);
                        if (isVisible(el)) return true;
                    }} catch (e) {{}}
                }}
                return false;
            }};
            const hasInput = () => {{
                for (const s of inputSels) {{
                    try {{
                        const el = document.querySelector(s);
                        if (isVisible(el)) return true;
                    }} catch (e) {{}}
                }}
                return false;
            }};
            if (checkLoginFirst) {{
                if (hasLogin()) return "LOGIN_REQUIRED";
                if (hasInput()) return "READY";
            }} else {{
                if (isGemini && hasLogin() && !hasAccountAvatar()) return "LOGIN_REQUIRED";
                if (hasInput()) return "READY";
                if (hasLogin()) return "LOGIN_REQUIRED";
            }}
            return "LOADING";
        }})()
        """
        return str(cdp.evaluate(js, timeout=8.0) or "LOADING")

    _WEB_BASE_MODEL_PATTERNS: dict[str, str] = {
        "auto": r"(?:^|\b)(?:자동|Auto)\b|요청에 맞게|Adapts to your needs|Flash-Lite|Flash|빠른\s*모드|빠른 답변|Fast",
        "hydrogen": r"Hydrogen|하이드로젠|일상생활 지원|Everyday help",
        "neon_4": r"Neon|네온|심층 분석|더 깊은 분석|Deeper analysis|Argon|아르곤|3\.1 Pro|상세한 조사",
        "carbon_4": r"카본|Carbon|코드 및 복잡한|가장 복잡한|Code\s*&\s*complex",
    }
    _CLAUDE_WEB_MODEL_PATTERNS: dict[str, str] = {
        "haiku": r"Haiku|하이쿠",
        "sonnet": r"Sonnet|소넷",
        "opus": r"Opus|오퍼스",
        "fable": r"Fable|페이블",
    }
    _CHATGPT_WEB_MODEL_PATTERNS: dict[str, str] = {
        "auto": r"(?:^|\b)(?:Auto|Instant|자동)\b|빠른",
        "mini": r"Mini|Fast|Nano|Luna",
        "thinking": r"Thinking|사고|추론|Sol",
        "pro": r"\bPro\b|Astra",
    }
    _WEB_THINKING_PATTERNS: dict[str, str] = {
        "low": r"낮음|Low|속도",
        "medium": r"보통|Medium|균형",
        "high": r"^(?!.*(?:최고|매우 높|Extra|xhigh)).*(?:높음|High|철저)",
        "xhigh": r"최고|매우 높|Extra\s*High|xhigh",
    }

    @staticmethod
    def _provider_picker_config(provider: str) -> tuple[list[str], str, str]:
        """Return `(picker_selectors, fallback_include_re, fallback_exclude_re)` for Claude or ChatGPT."""
        if provider == "claude":
            return (
                [
                    "button[data-testid='model-selector-dropdown']",
                    "fieldset button[aria-haspopup='menu']",
                    "form button[aria-haspopup='menu']",
                    "button[aria-label*='Select model']",
                    "button[aria-label*='모델 선택']",
                ],
                r"\b(?:Sonnet|Opus|Haiku|Fable|Claude\s*\d)\b",
                r"upgrade|업그레이드|\bpro\b|\bmax\b|plan|구독|logo|home|새\s*채팅|new\s*chat|로그인|log\s*in|sign\s*in",
            )
        # ChatGPT: NEVER include broad `button[aria-label*='ChatGPT']` (matches prompt suggestion chips!)
        return (
            [
                "button[data-testid='model-switcher-dropdown-button']",
                "button[aria-label*='Model selector']",
                "button[aria-label*='모델 선택']",
                "header button[aria-haspopup='menu']",
                "header button[aria-haspopup='listbox']",
                "nav button[aria-haspopup='menu']",
            ],
            r"^(?:ChatGPT|GPT|o[1-9]|Auto|Instant|Mini|Thinking)\b",
            r"upgrade|업그레이드|plus|로그인|회원가입|log\s*in|sign\s*in|sign\s*up|뭘\s*할\s*수|what\s+can|help|도움말",
        )

    @staticmethod
    def _cdp_trusted_click(cdp: LocalCDPClient, x: float, y: float) -> None:
        """Dispatch a trusted CDP mouse click at `(x, y)` (required when Radix UI ignores synthetic JS clicks)."""
        px, py = float(x), float(y)
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": px, "y": py, "button": "none", "buttons": 0},
            timeout=4.0,
        )
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": px, "y": py, "button": "left", "buttons": 1, "clickCount": 1},
            timeout=4.0,
        )
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": px, "y": py, "button": "left", "buttons": 0, "clickCount": 1},
            timeout=4.0,
        )

    def _select_provider_web_model_on_cdp(
        self,
        cdp: LocalCDPClient,
        model_code: str,
        thinking_level: str = "low",
    ) -> str:
        """Best-effort web model selector for Claude (`claude.ai`, Radix UI) and ChatGPT (`chatgpt.com`)."""
        code = (model_code or "default").strip()
        p = self.provider
        want_claude_think_off = (p == "claude") and (thinking_level == "low")
        want_claude_think_on = (p == "claude") and (thinking_level in ("medium", "high", "xhigh"))
        if not code or code == "default":
            if p == "claude" and (want_claude_think_off or want_claude_think_on):
                target_pressed = "true" if want_claude_think_off else "false"
                res_tag = "THINKING_OFF" if want_claude_think_off else "THINKING_ON"
                try:
                    cdp.evaluate(
                        f"""(() => {{
                            const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                            const pressed = {_js_literal(target_pressed)};
                            const sels = [
                                `fieldset button[aria-label*='Extended thinking'][aria-pressed='${{pressed}}']`,
                                `form button[aria-label*='Extended thinking'][aria-pressed='${{pressed}}']`,
                                `fieldset button[aria-label*='확장 사고'][aria-pressed='${{pressed}}']`,
                                `form button[aria-label*='확장 사고'][aria-pressed='${{pressed}}']`,
                                `button[data-testid*='extended-thinking'][aria-pressed='${{pressed}}']`
                            ];
                            for (const s of sels) {{
                                try {{
                                    const b = document.querySelector(s);
                                    if (isVis(b)) {{ b.click(); return {_js_literal(res_tag)}; }}
                                }} catch (e) {{}}
                            }}
                            return "NOOP";
                        }})()""",
                        timeout=4.0,
                    )
                except Exception:
                    pass
            return "DEFAULT_KEPT"
        pats = self._CLAUDE_WEB_MODEL_PATTERNS if p == "claude" else self._CHATGPT_WEB_MODEL_PATTERNS
        dyn_text = _RE_CTRL_NON_SPACE.sub("", code[8:]).strip()[:128] if code.startswith("web_dyn:") else ""
        model_pat = pats.get(code, "")
        if not dyn_text and not model_pat:
            return "DEFAULT_KEPT"
        spec_key = f"{p}:{code}:{thinking_level}"
        picker_sels, fallback_btn_re, fallback_excl_re = self._provider_picker_config(p)
        is_claude = p == "claude"

        def _build_js(skip_open: bool = False) -> str:
            return f"""
            (async () => {{
                const wantedSpec = {_js_literal(spec_key)};
                if (window.__vcModelSpec === wantedSpec) return {{status: "ALREADY_CACHED"}};
                const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                const wait = (ms) => new Promise(r => setTimeout(r, ms));
                const isClaude = {_js_literal(is_claude)};
                const skipOpen = {_js_literal(skip_open)};
                const pickerSels = {_js_literal(picker_sels)};
                const fbRe = new RegExp({_js_literal(fallback_btn_re)}, "i");
                const fbExclRe = new RegExp({_js_literal(fallback_excl_re)}, "i");
                const upsellRe = /upgrade|업그레이드|구독|subscribe|pro\\s*전용|max\\s*전용/i;
                const moreRe = /more\\s*models|더\\s*많은\\s*모델|모델\\s*더보기/i;
                const normText = (el) => ((el && (el.innerText || el.textContent)) || "").replace(/\\s+/g, " ").trim();
                const isDisabledItem = (el) => !!(
                    !el ||
                    el.getAttribute("aria-disabled") === "true" ||
                    el.hasAttribute("data-disabled") ||
                    el.disabled === true ||
                    el.hasAttribute("disabled") ||
                    upsellRe.test(normText(el))
                );
                const openTrigger = (el) => {{
                    const r = el.getBoundingClientRect();
                    const cx = r.left + r.width / 2;
                    const cy = r.top + r.height / 2;
                    const pOpts = {{ bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 1, pointerId: 1, pointerType: "mouse", isPrimary: true }};
                    const mOpts = {{ bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 1 }};
                    try {{ el.focus(); }} catch (e) {{}}
                    try {{ el.dispatchEvent(new PointerEvent("pointerdown", pOpts)); }} catch (e) {{}}
                    try {{ el.dispatchEvent(new MouseEvent("mousedown", mOpts)); }} catch (e) {{}}
                    try {{ el.dispatchEvent(new PointerEvent("pointerup", {{ ...pOpts, buttons: 0 }})); }} catch (e) {{}}
                    try {{ el.dispatchEvent(new MouseEvent("mouseup", {{ ...mOpts, buttons: 0 }})); }} catch (e) {{}}
                    try {{ el.click(); }} catch (e) {{}}
                }};
                const closeMenu = async () => {{
                    try {{
                        const evInit = {{ key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true }};
                        if (document.activeElement) document.activeElement.dispatchEvent(new KeyboardEvent("keydown", evInit));
                        document.dispatchEvent(new KeyboardEvent("keydown", evInit));
                    }} catch (e) {{}}
                    await wait(120);
                }};
                const findBtn = () => {{
                    for (const s of pickerSels) {{
                        try {{
                            const els = Array.from(document.querySelectorAll(s)).filter(isVis);
                            for (const el of els) {{
                                const t = normText(el);
                                if (t && fbExclRe.test(t) && !el.matches("button[data-testid='model-selector-dropdown'], button[data-testid='model-switcher-dropdown-button']")) {{
                                    continue;
                                }}
                                return el;
                            }}
                        }} catch (e) {{}}
                    }}
                    // Scoped fallback ONLY (header/composer with aria-haspopup) — never unconstrained body buttons
                    const scopeSel = isClaude
                        ? "fieldset button[aria-haspopup], form button[aria-haspopup], [data-testid*='composer'] button[aria-haspopup]"
                        : "header button[aria-haspopup='menu'], header button[aria-haspopup='listbox']";
                    const btns = Array.from(document.querySelectorAll(scopeSel)).filter(isVis);
                    return btns.find(b => {{
                        const t = normText(b);
                        return t.length > 0 && t.length <= 36 && fbRe.test(t) && !fbExclRe.test(t);
                    }}) || null;
                }};
                let btn = null;
                for (let i = 0; i < 8; i++) {{
                    btn = findBtn();
                    if (btn) break;
                    await wait(200);
                }}
                if (!btn) return {{status: "NO_BTN"}};
                const getItems = () => {{
                    const menuSel = "[role='menu'] [role='menuitem'], [role='menu'] [role='menuitemradio'], [role='listbox'] [role='option'], [data-radix-popper-content-wrapper] [role='menuitem'], [data-radix-popper-content-wrapper] [role='menuitemradio'], [data-radix-popper-content-wrapper] [role='option'], [role='menuitem'], [role='menuitemradio'], [role='option']";
                    const raw = Array.from(document.querySelectorAll(menuSel));
                    const uniq = [];
                    const seen = new Set();
                    for (const el of raw) {{
                        if (seen.has(el)) continue;
                        seen.add(el);
                        if (!isVis(el) || el === btn || el.contains(btn) || btn.contains(el)) continue;
                        uniq.push(el);
                    }}
                    return uniq;
                }};
                if (!skipOpen) {{
                    openTrigger(btn);
                    await wait(320);
                }}
                let items = getItems();
                if (items.length === 0 && !skipOpen) {{
                    const r = btn.getBoundingClientRect();
                    return {{status: "NEED_TRUSTED_CLICK", x: r.left + r.width / 2, y: r.top + r.height / 2}};
                }}
                const dynText = {_js_literal(dyn_text)};
                const modelPat = {_js_literal(model_pat)};
                const modelRe = modelPat ? new RegExp(modelPat, "i") : null;
                const matchTarget = (list) => list.find(el => {{
                    const txt = normText(el);
                    if (!txt || moreRe.test(txt)) return false;
                    if (dynText) return txt.toLowerCase().includes(dynText.toLowerCase());
                    return modelRe ? modelRe.test(txt) : false;
                }});
                let target = matchTarget(items);
                if (!target && isClaude) {{
                    const moreItem = items.find(el => moreRe.test(normText(el)));
                    if (moreItem) {{
                        try {{
                            moreItem.dispatchEvent(new PointerEvent("pointerenter", {{ bubbles: true, pointerType: "mouse" }}));
                            moreItem.dispatchEvent(new PointerEvent("pointermove", {{ bubbles: true, pointerType: "mouse" }}));
                            openTrigger(moreItem);
                        }} catch (e) {{}}
                        await wait(260);
                        items = getItems();
                        target = matchTarget(items);
                    }}
                }}
                if (!target) {{
                    await closeMenu();
                    return {{status: "NO_MODEL_ITEM"}};
                }}
                if (isDisabledItem(target)) {{
                    await closeMenu();
                    return {{status: "UPSELL_LOCKED:" + {_js_literal(code)}}};
                }}
                openTrigger(target);
                await wait(240);
                if ({_js_literal(want_claude_think_off)}) {{
                    try {{
                        const tBtns = document.querySelectorAll(
                            "fieldset button[aria-label*='Extended thinking'][aria-pressed='true'], form button[aria-label*='Extended thinking'][aria-pressed='true'], fieldset button[aria-label*='확장 사고'][aria-pressed='true'], form button[aria-label*='확장 사고'][aria-pressed='true'], button[data-testid*='extended-thinking'][aria-pressed='true']"
                        );
                        for (const tb of tBtns) {{
                            if (isVis(tb)) {{ tb.click(); break; }}
                        }}
                    }} catch (e) {{}}
                }} else if ({_js_literal(want_claude_think_on)}) {{
                    try {{
                        const tBtns = document.querySelectorAll(
                            "fieldset button[aria-label*='Extended thinking'][aria-pressed='false'], form button[aria-label*='Extended thinking'][aria-pressed='false'], fieldset button[aria-label*='확장 사고'][aria-pressed='false'], form button[aria-label*='확장 사고'][aria-pressed='false'], button[data-testid*='extended-thinking'][aria-pressed='false']"
                        );
                        for (const tb of tBtns) {{
                            if (isVis(tb)) {{ tb.click(); break; }}
                        }}
                    }} catch (e) {{}}
                }}
                await closeMenu();
                window.__vcModelSpec = wantedSpec;
                return {{status: "SWITCHED:" + wantedSpec}};
            }})()
            """

        try:
            res = cdp.evaluate(_build_js(skip_open=False), timeout=10.0)
            if isinstance(res, dict) and res.get("status") == "NEED_TRUSTED_CLICK":
                self._cdp_trusted_click(cdp, float(res.get("x", 0)), float(res.get("y", 0)))
                time.sleep(0.32)
                res = cdp.evaluate(_build_js(skip_open=True), timeout=10.0)
            if isinstance(res, dict):
                return str(res.get("status") or "UNKNOWN")
            return str(res or "UNKNOWN")
        except Exception as exc:
            self._log(f"⚠️ {self._provider_name_kr()} 웹 모델 선택 중 예외 ({code}): {_scrub_secrets(exc)}")
            return "ERROR"

    def _select_web_model_on_cdp(
        self,
        cdp: LocalCDPClient,
        model_code: str = "hydrogen",
        thinking_level: str = "low",
    ) -> str:
        """Select web model (and thinking level on Gemini) in the active provider's DOM picker."""
        if self.provider != "gemini":
            return self._select_provider_web_model_on_cdp(cdp, model_code, thinking_level)
        code, lvl = split_legacy_browser_model(model_code, thinking_level)
        if not code.startswith("web_dyn:") and code not in self._WEB_BASE_MODEL_PATTERNS:
            code = "hydrogen"
        model_pat = self._WEB_BASE_MODEL_PATTERNS.get(code, "")
        dyn_mode_id = _RE_CTRL_NON_SPACE.sub("", code[8:]).strip()[:128] if code.startswith("web_dyn:") else ""
        if code.startswith("web_dyn:") and not dyn_mode_id:
            code = "hydrogen"
            model_pat = self._WEB_BASE_MODEL_PATTERNS["hydrogen"]
        think_pat = self._WEB_THINKING_PATTERNS.get(lvl, self._WEB_THINKING_PATTERNS["low"])
        think_fallback_pat = self._WEB_THINKING_PATTERNS["high"] if lvl == "xhigh" else ""
        spec_key = f"{code}:{lvl}"

        js = f"""
        (async () => {{
            const wantedSpec = {_js_literal(spec_key)};
            if (window.__vcModelSpec === wantedSpec) return "ALREADY_CACHED";
            const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            const wait = (ms) => new Promise(r => setTimeout(r, ms));
            const normText = (el) => ((el && (el.innerText || el.textContent)) || "").replace(/\\s+/g, " ").trim();
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
            if (!btn) {{
                window.__vcModelSpec = wantedSpec;
                return "NO_BTN";
            }}

            const dynModeId = {_js_literal(dyn_mode_id)};
            const modelPat = {_js_literal(model_pat)};
            const modelRe = modelPat ? new RegExp(modelPat, "i") : null;
            const autoRe = new RegExp({_js_literal(self._WEB_BASE_MODEL_PATTERNS["auto"])}, "i");
            const hydrogenRe = new RegExp({_js_literal(self._WEB_BASE_MODEL_PATTERNS["hydrogen"])}, "i");
            const thinkRe = new RegExp({_js_literal(think_pat)}, "i");
            const thinkFallbackRe = {_js_literal(think_fallback_pat)} ? new RegExp({_js_literal(think_fallback_pat)}, "i") : null;
            const upsellTextRe = /업그레이드|Upgrade|구독하기|Subscribe|무료 체험|Start trial/i;
            const menuSel = "[role='menuitem'], [role='menuitemradio'], [role='option'], button.mat-mdc-menu-item";
            const getItems = () => Array.from(document.querySelectorAll(menuSel)).filter(isVis);
            const isSelected = (el) => !!(
                el && (
                    el.classList.contains("selected") ||
                    el.getAttribute("aria-checked") === "true" ||
                    el.getAttribute("aria-selected") === "true"
                )
            );
            const isItemUpsell = (el) => {{
                if (!el) return true;
                const mid = (el.getAttribute("data-mode-id") || "").trim().toLowerCase();
                if (mid.startsWith("upsell")) return true;
                if (el.getAttribute("aria-disabled") === "true") return true;
                if (el.disabled === true || el.hasAttribute("disabled")) return true;
                const tid = ((el.getAttribute("data-test-id") || el.getAttribute("data-testid") || "")).toLowerCase();
                if (tid.includes("upsell")) return true;
                try {{
                    if (el.matches && el.matches("a[href*='one.google.com']")) return true;
                    if (el.closest && el.closest("a[href*='one.google.com']")) return true;
                    if (el.querySelector("a[href*='one.google.com'], [data-test-id*='upsell'], [data-testid*='upsell']")) return true;
                    const icons = el.querySelectorAll("mat-icon, .material-icons, .google-symbols");
                    for (const ic of icons) {{
                        const fontIcon = (ic.getAttribute("fonticon") || ic.getAttribute("data-mat-icon-name") || "").trim().toLowerCase();
                        const icTxt = (ic.textContent || "").trim().toLowerCase();
                        const icLbl = (ic.getAttribute("aria-label") || "").toLowerCase();
                        if (
                            fontIcon === "lock" || fontIcon.startsWith("lock_") ||
                            icTxt === "lock" || icTxt === "lock_outline" || icTxt === "lock_person" ||
                            icLbl.includes("lock") || icLbl.includes("잠금")
                        ) {{
                            return true;
                        }}
                    }}
                }} catch (e) {{}}
                if (upsellTextRe.test(normText(el))) return true;
                return false;
            }};
            const isThinkSubmenu = (el) => {{
                const t = normText(el);
                const mid = (el.getAttribute("data-mode-id") || "").trim().toLowerCase();
                if (mid && !mid.includes("think")) return false;
                if (/사고 수준|Thinking\\s*level/i.test(t)) return true;
                return /^Thinking\\b/i.test(t) && !/Flash|Pro|Hydrogen|Neon|Carbon|Auto/i.test(t);
            }};
            const isNonModelItem = (el) => {{
                if (isThinkSubmenu(el)) return true;
                return /설정|Settings|도움말|Help|로그아웃|Sign out|의견 보내기|Send feedback/i.test(normText(el));
            }};
            const closeOverlays = async () => {{
                const dispatchEsc = () => {{
                    try {{
                        const evInit = {{ key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true }};
                        if (document.activeElement) document.activeElement.dispatchEvent(new KeyboardEvent("keydown", evInit));
                        document.dispatchEvent(new KeyboardEvent("keydown", evInit));
                    }} catch (e) {{}}
                }};
                dispatchEsc();
                await wait(100);
                try {{
                    const backdrops = Array.from(document.querySelectorAll(".cdk-overlay-backdrop")).filter(isVis);
                    for (const bd of backdrops) {{
                        try {{ bd.click(); }} catch (e) {{}}
                    }}
                }} catch (e) {{}}
                await wait(120);
                if (getItems().length > 0) {{
                    dispatchEsc();
                    try {{ if (document.body) document.body.click(); }} catch (e) {{}}
                    await wait(120);
                }}
            }};

            btn.click();
            await wait(350);
            const items = getItems();
            if (items.length === 0) {{
                await closeOverlays();
                return "NO_MODEL_ITEM";
            }}
            const hadThinkInInitial = items.some(el => isThinkSubmenu(el) && isVis(el) && !isItemUpsell(el));
            const targetItem = items.find(el => {{
                if (isThinkSubmenu(el)) return false;
                if (dynModeId) {{
                    return (el.getAttribute("data-mode-id") || "") === dynModeId || normText(el).includes(dynModeId);
                }}
                return modelRe ? modelRe.test(normText(el)) : false;
            }});

            let usedFallbackFree = false;
            let upsellLocked = false;
            if (!targetItem || isItemUpsell(targetItem)) {{
                upsellLocked = !!(targetItem && isItemUpsell(targetItem));
                const unlockedItems = items.filter(el => !isItemUpsell(el) && !isNonModelItem(el));
                const fallbackItem =
                    unlockedItems.find(isSelected)
                    || unlockedItems.find(el => autoRe.test(normText(el)))
                    || unlockedItems.find(el => hydrogenRe.test(normText(el)))
                    || unlockedItems[0]
                    || null;
                if (fallbackItem) {{
                    usedFallbackFree = true;
                    if (!isSelected(fallbackItem)) {{
                        fallbackItem.click();
                        await wait(320);
                    }}
                }} else {{
                    await closeOverlays();
                    window.__vcModelSpec = wantedSpec;
                    return upsellLocked ? ("UPSELL_LOCKED:" + {_js_literal(code)}) : "NO_MODEL_ITEM";
                }}
            }} else if (!isSelected(targetItem)) {{
                targetItem.click();
                await wait(320);
            }}

            let subItems = getItems();
            if (subItems.length === 0 && (!usedFallbackFree || hadThinkInInitial)) {{
                btn = findBtn();
                if (btn) {{
                    btn.click();
                    await wait(350);
                    subItems = getItems();
                }}
            }}
            const thinkParent = subItems.find(el => isThinkSubmenu(el) && isVis(el) && !isItemUpsell(el));
            if (thinkParent) {{
                thinkParent.click();
                await wait(320);
                subItems = getItems();
                const levelItem = subItems.find(el => !isItemUpsell(el) && thinkRe.test(normText(el)))
                    || (thinkFallbackRe ? subItems.find(el => !isItemUpsell(el) && thinkFallbackRe.test(normText(el))) : null);
                if (levelItem && !isSelected(levelItem)) {{
                    levelItem.click();
                    await wait(260);
                }}
            }}

            await closeOverlays();
            window.__vcModelSpec = wantedSpec;
            if (usedFallbackFree) {{
                return "FALLBACK_FREE:" + wantedSpec;
            }}
            return "SWITCHED:" + wantedSpec;
        }})()
        """
        try:
            res = str(cdp.evaluate(js, timeout=11.0) or "UNKNOWN")
            if (res.startswith("FALLBACK_FREE:") or res.startswith("UPSELL_LOCKED:")) and code not in self._upsell_warned:
                self._upsell_warned.add(code)
                self._log(
                    f"ℹ️ 선택한 웹 제미니 모델({code})은 현재 Google 계정에서 잠겨 있거나 사용할 수 없어 "
                    "무료/기본 사용 가능 모델로 자동 전환해 대화를 이어갑니다."
                )
            return res
        except Exception as exc:
            self._log(f"⚠️ 제미니 웹 모델 선택 중 예외 ({code}:{lvl}): {_scrub_secrets(exc)}")
            return "ERROR"

    def _ensure_fast_response_mode(self, cdp: LocalCDPClient) -> None:
        """Select the currently configured Gemini web model and thinking level on `cdp`."""
        self._select_web_model_on_cdp(cdp, self.selected_model_code, self.selected_thinking_level)

    def _sync_provider_web_models(self) -> tuple[list[tuple[str, str]], str]:
        """Inspect Claude (`claude.ai`, Radix UI) or ChatGPT (`chatgpt.com`) model picker for unlocked models."""
        assert self._cdp is not None
        p = self.provider
        is_claude = p == "claude"
        picker_sels, fallback_btn_re, fallback_excl_re = self._provider_picker_config(p)

        def _build_sync_js(skip_open: bool = False) -> str:
            return f"""
            (async () => {{
                const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                const wait = (ms) => new Promise(r => setTimeout(r, ms));
                const isClaude = {_js_literal(is_claude)};
                const skipOpen = {_js_literal(skip_open)};
                const pickerSels = {_js_literal(picker_sels)};
                const fbRe = new RegExp({_js_literal(fallback_btn_re)}, "i");
                const fbExclRe = new RegExp({_js_literal(fallback_excl_re)}, "i");
                const upsellRe = /upgrade|업그레이드|구독|subscribe|pro\\s*전용|max\\s*전용/i;
                const moreRe = /more\\s*models|더\\s*많은\\s*모델|모델\\s*더보기/i;
                const normText = (el) => ((el && (el.innerText || el.textContent)) || "").replace(/\\s+/g, " ").trim();
                const openTrigger = (el) => {{
                    const r = el.getBoundingClientRect();
                    const cx = r.left + r.width / 2;
                    const cy = r.top + r.height / 2;
                    const pOpts = {{ bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 1, pointerId: 1, pointerType: "mouse", isPrimary: true }};
                    const mOpts = {{ bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 1 }};
                    try {{ el.focus(); }} catch (e) {{}}
                    try {{ el.dispatchEvent(new PointerEvent("pointerdown", pOpts)); }} catch (e) {{}}
                    try {{ el.dispatchEvent(new MouseEvent("mousedown", mOpts)); }} catch (e) {{}}
                    try {{ el.dispatchEvent(new PointerEvent("pointerup", {{ ...pOpts, buttons: 0 }})); }} catch (e) {{}}
                    try {{ el.dispatchEvent(new MouseEvent("mouseup", {{ ...mOpts, buttons: 0 }})); }} catch (e) {{}}
                    try {{ el.click(); }} catch (e) {{}}
                }};
                const closeMenu = async () => {{
                    try {{
                        const evInit = {{ key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true }};
                        if (document.activeElement) document.activeElement.dispatchEvent(new KeyboardEvent("keydown", evInit));
                        document.dispatchEvent(new KeyboardEvent("keydown", evInit));
                    }} catch (e) {{}}
                    await wait(120);
                }};
                const findBtn = () => {{
                    for (const s of pickerSels) {{
                        try {{
                            const els = Array.from(document.querySelectorAll(s)).filter(isVis);
                            for (const el of els) {{
                                const t = normText(el);
                                if (t && fbExclRe.test(t) && !el.matches("button[data-testid='model-selector-dropdown'], button[data-testid='model-switcher-dropdown-button']")) {{
                                    continue;
                                }}
                                return el;
                            }}
                        }} catch (e) {{}}
                    }}
                    const scopeSel = isClaude
                        ? "fieldset button[aria-haspopup], form button[aria-haspopup], [data-testid*='composer'] button[aria-haspopup]"
                        : "header button[aria-haspopup='menu'], header button[aria-haspopup='listbox']";
                    const btns = Array.from(document.querySelectorAll(scopeSel)).filter(isVis);
                    return btns.find(b => {{
                        const t = normText(b);
                        return t.length > 0 && t.length <= 36 && fbRe.test(t) && !fbExclRe.test(t);
                    }}) || null;
                }};
                let btn = null;
                for (let i = 0; i < 8; i++) {{
                    btn = findBtn();
                    if (btn) break;
                    await wait(200);
                }}
                if (!btn) return {{ok: false, reason: "NO_PICKER", rows: []}};
                const getItems = () => {{
                    const menuSel = "[role='menu'] [role='menuitem'], [role='menu'] [role='menuitemradio'], [role='listbox'] [role='option'], [data-radix-popper-content-wrapper] [role='menuitem'], [data-radix-popper-content-wrapper] [role='menuitemradio'], [data-radix-popper-content-wrapper] [role='option'], [role='menuitem'], [role='menuitemradio'], [role='option']";
                    const raw = Array.from(document.querySelectorAll(menuSel));
                    const uniq = [];
                    const seen = new Set();
                    for (const el of raw) {{
                        if (seen.has(el)) continue;
                        seen.add(el);
                        if (!isVis(el) || el === btn || el.contains(btn) || btn.contains(el)) continue;
                        uniq.push(el);
                    }}
                    return uniq;
                }};
                if (!skipOpen) {{
                    openTrigger(btn);
                    await wait(340);
                }}
                let items = getItems();
                if (items.length === 0 && !skipOpen) {{
                    const r = btn.getBoundingClientRect();
                    return {{ok: false, reason: "NEED_TRUSTED_CLICK", x: r.left + r.width / 2, y: r.top + r.height / 2, rows: []}};
                }}
                const collected = [];
                const seenTxt = new Set();
                const pushRows = (list) => {{
                    for (const el of list) {{
                        const txt = normText(el);
                        if (!txt || seenTxt.has(txt) || moreRe.test(txt)) continue;
                        seenTxt.add(txt);
                        const disabled = (
                            el.getAttribute("aria-disabled") === "true" ||
                            el.hasAttribute("data-disabled") ||
                            el.disabled === true ||
                            el.hasAttribute("disabled") ||
                            upsellRe.test(txt)
                        );
                        collected.push({{disabled, text: txt}});
                    }}
                }};
                pushRows(items);
                if (isClaude) {{
                    const moreItem = items.find(el => moreRe.test(normText(el)));
                    if (moreItem) {{
                        try {{
                            moreItem.dispatchEvent(new PointerEvent("pointerenter", {{ bubbles: true, pointerType: "mouse" }}));
                            moreItem.dispatchEvent(new PointerEvent("pointermove", {{ bubbles: true, pointerType: "mouse" }}));
                            openTrigger(moreItem);
                        }} catch (e) {{}}
                        await wait(260);
                        pushRows(getItems());
                    }}
                }}
                await closeMenu();
                return {{ok: true, reason: "OK", rows: collected}};
            }})()
            """

        try:
            res = self._cdp.evaluate(_build_sync_js(skip_open=False), timeout=10.0) or {}
            if isinstance(res, dict) and res.get("reason") == "NEED_TRUSTED_CLICK":
                self._cdp_trusted_click(self._cdp, float(res.get("x", 0)), float(res.get("y", 0)))
                time.sleep(0.34)
                res = self._cdp.evaluate(_build_sync_js(skip_open=True), timeout=10.0) or {}
        except Exception as exc:
            return [], f"PROBE_ERR:{_scrub_secrets(exc)}"
        if not isinstance(res, dict) or not res.get("ok"):
            return [], str(res.get("reason", "NO_PICKER") if isinstance(res, dict) else "NO_PICKER")

        rows = res.get("rows") or []
        defaults = get_provider_browser_models(p)
        found_models: list[tuple[str, str]] = []
        seen_codes: set[str] = {defaults[0][0]}
        pats = self._CLAUDE_WEB_MODEL_PATTERNS if p == "claude" else self._CHATGPT_WEB_MODEL_PATTERNS
        lbl_map = CLAUDE_BROWSER_MODEL_LABELS if p == "claude" else CHATGPT_BROWSER_MODEL_LABELS
        compiled = [(c, re.compile(pat, re.I)) for c, pat in pats.items()]
        for row in rows:
            if not isinstance(row, dict) or row.get("disabled"):
                continue
            txt = _RE_MULTI_SPACE.sub(" ", str(row.get("text") or "")).strip()[:120]
            if not txt or len(txt) < 2:
                continue
            low = txt.lower()
            if any(
                skip in low
                for skip in (
                    "upgrade",
                    "업그레이드",
                    "log out",
                    "로그아웃",
                    "settings",
                    "설정",
                    "help",
                    "도움말",
                    "more models",
                    "더 많은 모델",
                )
            ):
                continue
            matched = ""
            for canon, pat in compiled:
                if pat.search(txt):
                    matched = canon
                    break
            if matched:
                if matched not in seen_codes:
                    seen_codes.add(matched)
                    found_models.append((matched, lbl_map.get(matched, f"🌐 {txt}")))
            else:
                first_tok = txt.split("\n", 1)[0].strip()[:48]
                dyn_code = f"web_dyn:{first_tok}"
                if dyn_code not in seen_codes:
                    seen_codes.add(dyn_code)
                    found_models.append((dyn_code, f"🌐 {first_tok}"))
        # Never wipe cached models with `[defaults[0]]` alone when the menu yielded zero real models!
        if not found_models:
            return [], "EMPTY_MENU"
        return [defaults[0]] + found_models, ""

    def sync_account_web_models(
        self,
        allow_connect: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> tuple[list[tuple[str, str]], str]:
        """
        Inspect the logged-in AI Web mode switcher to return all unlocked models
        for the user's account tier plus the detected tier string.
        """
        if self._is_aborted(cancel_event):
            return [], "CANCELLED"
        with self._io_lock:
            self._abort_event.clear()
            if self._is_aborted(cancel_event):
                return [], "CANCELLED"
            if self._cdp is None or not self.is_running() or self.login_mode_active:
                if not allow_connect:
                    return [], ""
                ok, reason = self.ensure_ready_and_check_login(
                    show_if_ready=False,
                    allow_interactive_login=False,
                    cancel_event=cancel_event,
                    _clear_abort=False,
                )
                if not ok or self._cdp is None or self.login_mode_active:
                    return [], reason

            if self._is_aborted(cancel_event):
                return [], "CANCELLED"
            if self.provider != "gemini":
                return self._sync_provider_web_models()

            js = """
            (async () => {
                const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                const wait = (ms) => new Promise(r => setTimeout(r, ms));
                const normText = (el) => ((el && (el.innerText || el.textContent)) || "").replace(/\\s+/g, " ").trim();
                const menuSel = "[role='menuitemradio'], [role='menuitem'], [role='option'], button.mat-mdc-menu-item";
                const getItems = () => Array.from(document.querySelectorAll(menuSel)).filter(isVis);
                const upsellTextRe = /업그레이드|Upgrade|구독하기|Subscribe|무료 체험|Start trial/i;
                const isItemUpsell = (el) => {
                    if (!el) return true;
                    const mid = (el.getAttribute("data-mode-id") || "").trim().toLowerCase();
                    if (mid.startsWith("upsell")) return true;
                    if (el.getAttribute("aria-disabled") === "true") return true;
                    if (el.disabled === true || el.hasAttribute("disabled")) return true;
                    const tid = ((el.getAttribute("data-test-id") || el.getAttribute("data-testid") || "")).toLowerCase();
                    if (tid.includes("upsell")) return true;
                    try {
                        if (el.matches && el.matches("a[href*='one.google.com']")) return true;
                        if (el.closest && el.closest("a[href*='one.google.com']")) return true;
                        if (el.querySelector("a[href*='one.google.com'], [data-test-id*='upsell'], [data-testid*='upsell']")) return true;
                        const icons = el.querySelectorAll("mat-icon, .material-icons, .google-symbols");
                        for (const ic of icons) {
                            const fontIcon = (ic.getAttribute("fonticon") || ic.getAttribute("data-mat-icon-name") || "").trim().toLowerCase();
                            const icTxt = (ic.textContent || "").trim().toLowerCase();
                            const icLbl = (ic.getAttribute("aria-label") || "").toLowerCase();
                            if (
                                fontIcon === "lock" || fontIcon.startsWith("lock_") ||
                                icTxt === "lock" || icTxt === "lock_outline" || icTxt === "lock_person" ||
                                icLbl.includes("lock") || icLbl.includes("잠금")
                            ) {
                                return true;
                            }
                        }
                    } catch (e) {}
                    if (upsellTextRe.test(normText(el))) return true;
                    return false;
                };
                const closeOverlays = async () => {
                    const dispatchEsc = () => {
                        try {
                            const evInit = { key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true };
                            if (document.activeElement) document.activeElement.dispatchEvent(new KeyboardEvent("keydown", evInit));
                            document.dispatchEvent(new KeyboardEvent("keydown", evInit));
                        } catch (e) {}
                    };
                    dispatchEsc();
                    await wait(100);
                    try {
                        const backdrops = Array.from(document.querySelectorAll(".cdk-overlay-backdrop")).filter(isVis);
                        for (const bd of backdrops) {
                            try { bd.click(); } catch (e) {}
                        }
                    } catch (e) {}
                    await wait(120);
                    if (getItems().length > 0) {
                        dispatchEsc();
                        try { if (document.body) document.body.click(); } catch (e) {}
                        await wait(120);
                    }
                };
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
                const items = getItems();
                const rows = items.map(el => ({
                    modeId: el.getAttribute("data-mode-id") || "",
                    disabled: el.getAttribute("aria-disabled") === "true" || el.disabled === true || el.hasAttribute("disabled"),
                    upsell: isItemUpsell(el),
                    text: normText(el),
                }));
                await closeOverlays();
                return {ok: true, reason: "OK", rows};
            })()
            """
            try:
                res = self._cdp.evaluate(js, timeout=10.0) or {}
            except Exception as exc:
                return [], f"PROBE_ERR:{_scrub_secrets(exc)}"

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
            skip_non_model_re = re.compile(
                r"사고 수준|Thinking\s*level|설정|Settings|도움말|Help|로그아웃|Sign out|의견 보내기|Send feedback",
                re.I,
            )
            think_prefix_re = re.compile(r"^Thinking\b", re.I)
            model_word_re = re.compile(r"Flash|Pro|Hydrogen|Neon|Carbon|Auto", re.I)
            upgrade_cta_re = re.compile(r"업그레이드|Upgrade|구독하기|Subscribe|무료 체험|Start trial", re.I)

            for row in rows:
                if not isinstance(row, dict):
                    continue
                mid = _RE_CTRL_NON_SPACE.sub("", str(row.get("modeId") or "")).strip()[:128]
                txt = _RE_MULTI_SPACE.sub(" ", str(row.get("text") or "")).strip()[:160]
                is_disabled = bool(row.get("disabled"))
                mid_low = mid.lower()
                is_upsell = bool(row.get("upsell")) or mid_low.startswith("upsell") or bool(upgrade_cta_re.search(txt))

                # Skip non-model submenu/chrome items (thinking level, settings, help, sign out)
                is_non_model_txt = bool(skip_non_model_re.search(txt)) or (
                    bool(think_prefix_re.search(txt)) and not bool(model_word_re.search(txt))
                )
                if (not mid_low or "think" in mid_low) and is_non_model_txt:
                    continue

                if is_upsell or is_disabled:
                    up_blob = f"{mid} {txt}".upper()
                    if "ULTRA" in up_blob or "CARBON" in up_blob or "카본" in txt:
                        has_ultra_upsell = True
                    elif any(tok in up_blob for tok in ("PRO", "ADVANCED", "NEON", "PLUS", "UPGRADE", "TRIAL")) or any(
                        tok in txt for tok in ("업그레이드", "구독", "무료 체험", "네온")
                    ):
                        has_pro_upsell = True
                    else:
                        has_pro_upsell = True
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
                if seen_codes.issubset({"auto", "hydrogen"}):
                    tier = "Free"
                elif "carbon_4" in seen_codes:
                    tier = "Ultra"
                elif "neon_4" in seen_codes:
                    tier = "Pro"
                else:
                    tier = "Ultra"
            return unlocked, tier

    def discover_web_models(self) -> list[tuple[str, str]]:
        """Discover any additional unlocked web models from the active DOM mode switcher."""
        models, _ = self.sync_account_web_models(allow_connect=False)
        base_labels = dict(get_provider_browser_models(self.provider))
        return [
            (code, lbl)
            for code, lbl in models
            if code not in base_labels
        ]

    def set_chat_model_async(self, model_code: str, thinking_level: str | None = None) -> None:
        """Persist preferred browser chat model + thinking level and apply on active chat tab."""
        expected_provider = self.provider
        if expected_provider == "gemini":
            code, lvl = split_legacy_browser_model(model_code, thinking_level or self.selected_thinking_level)
            if not code.startswith("web_dyn:") and code not in self._WEB_BASE_MODEL_PATTERNS:
                code = "hydrogen"
        else:
            lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else self.selected_thinking_level
            code = (model_code or get_provider_default_browser_model(expected_provider)).strip()
        self.selected_model_code = code
        self.selected_thinking_level = lvl

        def _worker() -> None:
            with self._io_lock:
                if self.provider != expected_provider:
                    return
                if self._cdp is not None and self.is_running() and not self.login_mode_active:
                    self._select_web_model_on_cdp(
                        self._cdp,
                        self.selected_model_code,
                        self.selected_thinking_level,
                    )

        threading.Thread(target=_worker, daemon=True, name="gemini-model-switch").start()

    def ensure_ready_and_check_login(
        self,
        show_if_ready: bool = False,
        allow_interactive_login: bool = True,
        cancel_event: threading.Event | None = None,
        *,
        _clear_abort: bool = True,
    ) -> tuple[bool, str]:
        """
        Launch isolated Edge/Chrome app window with %LOCALAPPDATA%\\VoiceCompanionData\\BrowserProfile.
        If web sign-in or Cloudflare verification is needed, switches to a normal un-debugged window
        so the user can complete sign-in / verification directly.
        """
        if cancel_event is not None and cancel_event.is_set():
            return False, "CANCELLED"
        with self._io_lock:
            if _clear_abort:
                self._abort_event.clear()
            if self._is_aborted(cancel_event):
                return False, "CANCELLED"
            selectors = self._load_active_selectors()
            target_url = validate_provider_url(selectors.get("url"), provider=self.provider)
            exe = find_system_browser_exe()
            if exe is None:
                return False, "NO_BROWSER_FOUND"

            profile_dir = get_browser_profile_dir().resolve()

            if (
                self._cdp is not None
                and self.is_running()
                and not self.login_mode_active
                and self._active_browser_provider in ("", self.provider)
            ):
                try:
                    st = self._check_dom_login_state(self._cdp, selectors)
                    if st == "READY":
                        self._active_browser_provider = self.provider
                        self.set_window_visible(show_if_ready)
                        return True, "OK"
                except Exception:
                    pass

            if self._is_aborted(cancel_event):
                return False, "CANCELLED"

            self._terminate_proc(graceful=True)
            port = _pick_free_port()
            win_pos = "--window-position=140,90" if show_if_ready else "--window-position=-3200,-3200"
            cmd = [
                str(exe),
                f"--user-data-dir={profile_dir}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
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
            self._active_browser_provider = self.provider
            self.login_mode_active = False
            self._conversation_primed = False
            self._thread_turn_count = 0

            try:
                self._cdp = self._connect_cdp(port, timeout_sec=12.0, cancel_event=cancel_event)
                try:
                    self._cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=5.0)
                except Exception:
                    pass
            except InterruptedError:
                self._terminate_proc()
                return False, "CANCELLED"
            except Exception as exc:
                self._terminate_proc()
                return False, f"CDP_CONNECT_FAILED: {_scrub_secrets(exc)}"

            deadline = time.monotonic() + 14.0
            state = "LOADING"
            while time.monotonic() < deadline:
                if self._is_aborted(cancel_event):
                    self._terminate_proc()
                    return False, "CANCELLED"
                try:
                    state = self._check_dom_login_state(self._cdp, selectors)
                    if state in ("READY", "LOGIN_REQUIRED", "CHALLENGE_REQUIRED"):
                        break
                except Exception:
                    pass
                time.sleep(0.3)

            if self._is_aborted(cancel_event):
                self._terminate_proc()
                return False, "CANCELLED"

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

            reason_out = "CHALLENGE_REQUIRED" if state == "CHALLENGE_REQUIRED" else "LOGIN_REQUIRED"
            # Reopen clean non-CDP window so Google / Claude / ChatGPT login or Cloudflare human verification works smoothly!
            if not allow_interactive_login:
                self._terminate_proc(graceful=True)
                return False, reason_out
            self._launch_clean_login_window(exe=exe, profile_dir=profile_dir, target_url=target_url)
            return False, reason_out

    def _launch_clean_login_window(
        self,
        *,
        exe: Path | None = None,
        profile_dir: Path | None = None,
        target_url: str | None = None,
    ) -> bool:
        """Close CDP browser gracefully and launch a clean non-CDP window for Google OAuth / Cloudflare Turnstile."""
        self._terminate_proc(graceful=True)
        eff_exe = exe or find_system_browser_exe()
        eff_profile = (profile_dir or get_browser_profile_dir()).resolve()
        eff_url = validate_provider_url(
            target_url if target_url else self._load_active_selectors().get("url"),
            provider=self.provider,
        )
        self._active_browser_provider = self.provider
        self.login_mode_active = True
        self._conversation_primed = False
        self._thread_turn_count = 0
        if eff_exe is None:
            self.window_visible = False
            return False
        login_cmd = [
            str(eff_exe),
            f"--user-data-dir={eff_profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-position=140,90",
            "--window-size=1000,780",
            f"--app={eff_url}",
        ]
        self._proc = subprocess.Popen(
            login_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.window_visible = True
        return True

    def complete_interactive_login(self, cancel_event: threading.Event | None = None) -> tuple[bool, str]:
        """Close the non-CDP login window (saving session cookies) and reattach in off-screen CDP mode."""
        if cancel_event is not None and cancel_event.is_set():
            return False, "CANCELLED"
        self.abort_active_prompt()
        with self._io_lock:
            self._abort_event.clear()
            if self._is_aborted(cancel_event):
                return False, "CANCELLED"
            self._terminate_proc(graceful=True)
            for _ in range(6):
                if self._is_aborted(cancel_event):
                    return False, "CANCELLED"
                time.sleep(0.1)
            return self.ensure_ready_and_check_login(
                show_if_ready=False,
                cancel_event=cancel_event,
                _clear_abort=False,
            )

    def start_new_chat(
        self,
        force: bool = False,
        cancel_event: threading.Event | None = None,
        *,
        _clear_abort: bool = True,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            return
        with self._io_lock:
            if _clear_abort:
                self._abort_event.clear()
            if self._is_aborted(cancel_event):
                return
            selectors = self._load_active_selectors()
            target_url = validate_provider_url(selectors.get("url"), provider=self.provider)
            if self._cdp is None or not self.is_running() or self.login_mode_active:
                self._conversation_primed = False
                self._thread_turn_count = 0
                return

            resp_sels = selectors.get("response_selectors", [])
            new_sels = selectors.get("new_chat_selectors", [])
            inspect_js = f"""
            (() => {{
                const respSels = {_js_literal(resp_sels)};
                let respCount = 0;
                for (const s of respSels) {{
                    try {{
                        const nodes = document.querySelectorAll(s);
                        if (nodes.length > 0) {{
                            respCount = nodes.length;
                            break;
                        }}
                    }} catch (e) {{}}
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
            in_existing_thread = self._is_in_existing_thread(pathname)
            has_chat_state = bool(force) or self._conversation_primed or resp_count > 0 or in_existing_thread

            if not has_chat_state:
                self._conversation_primed = False
                self._thread_turn_count = 0
                return

            # Try SPA "New chat" button first for instant zero-reload reset when sidebar/header button is visible.
            clicked_spa = False
            click_new_js = f"""
            (() => {{
                const newSels = {_js_literal(new_sels)};
                const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                for (const s of newSels) {{
                    try {{
                        const el = document.querySelector(s);
                        if (isVisible(el)) {{
                            el.click();
                            return "CLICKED";
                        }}
                    }} catch (e) {{}}
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
                    if self._is_aborted(cancel_event):
                        return
                    try:
                        cur = self._cdp.evaluate(inspect_js, timeout=4.0) or {}
                        cur_cnt = int(cur.get("respCount") or 0)
                        cur_path = str(cur.get("pathname") or "")
                        cur_in_thread = self._is_in_existing_thread(cur_path)
                        if cur_cnt == 0 and not cur_in_thread and self._check_dom_login_state(self._cdp, selectors) == "READY":
                            cleared_via_spa = True
                            try:
                                self._cdp.evaluate("window.__vcModelSpec = '';", timeout=3.0)
                            except Exception:
                                pass
                            break
                    except Exception:
                        pass
                    time.sleep(0.18)

            if not cleared_via_spa:
                if self._is_aborted(cancel_event):
                    return
                nav_js = (
                    "window.__vcStalePage = true; "
                    "window.__vcModelSpec = ''; "
                    f"window.location.href = {_js_literal(target_url)};"
                )
                try:
                    self._cdp.evaluate(nav_js, timeout=8.0)
                except Exception:
                    pass
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if self._is_aborted(cancel_event):
                        return
                    try:
                        not_stale = self._cdp.evaluate("window.__vcStalePage !== true", timeout=4.0)
                        if not_stale:
                            dom_st = self._check_dom_login_state(self._cdp, selectors)
                            if dom_st in ("READY", "LOGIN_REQUIRED", "CHALLENGE_REQUIRED"):
                                break
                    except Exception:
                        pass
                    time.sleep(0.22)

            if not self._is_aborted(cancel_event):
                self._select_web_model_on_cdp(self._cdp, self.selected_model_code, self.selected_thinking_level)
            self._conversation_primed = False
            self._thread_turn_count = 0

    def has_active_thread(self) -> bool:
        """Return True if the browser tab currently holds an active primed chat thread with responses."""
        with self._io_lock:
            if not self._conversation_primed or self._cdp is None or not self.is_running() or self.login_mode_active:
                return False
            selectors = self._load_active_selectors()
            resp_sels = selectors.get("response_selectors", [])
            inspect_js = f"""
            (() => {{
                const respSels = {_js_literal(resp_sels)};
                let respCount = 0;
                for (const s of respSels) {{
                    try {{
                        const nodes = document.querySelectorAll(s);
                        if (nodes.length > 0) {{
                            respCount = nodes.length;
                            break;
                        }}
                    }} catch (e) {{}}
                }}
                return {{
                    respCount,
                    pathname: location.pathname || ""
                }};
            }})()
            """
            try:
                info = self._cdp.evaluate(inspect_js, timeout=4.0) or {}
            except Exception:
                return True
            resp_count = int(info.get("respCount") or 0)
            pathname = str(info.get("pathname") or "")
            return bool(resp_count > 0 or self._is_in_existing_thread(pathname))

    def _attach_image_via_cdp(
        self,
        cdp: LocalCDPClient,
        input_sels: list[str],
        image_bytes: bytes,
        image_mime: str = "image/jpeg",
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, str]:
        """Attach an in-RAM screen image to Gemini Web via synthetic Clipboard paste / Drag&Drop (zero disk I/O)."""
        if self._is_aborted(cancel_event):
            return False, "CANCELLED"
        norm_bytes, norm_mime = _normalize_vision_image(image_bytes, image_mime)
        if not norm_bytes:
            self._log("⚠️ [Gemini Web] 화면 이미지가 유효하지 않아 텍스트만으로 대화를 진행합니다.")
            return False, "INVALID_IMAGE"

        b64_data = base64.b64encode(norm_bytes).decode("ascii")
        if len(b64_data) > (LocalCDPClient.MAX_SEND_BYTES - 128 * 1024):
            self._log("⚠️ [Gemini Web] 화면 이미지 크기가 CDP 전송 상한을 초과하여 텍스트만으로 대화를 진행합니다.")
            return False, "IMAGE_TOO_LARGE"

        ext = {"image/png": "png", "image/webp": "webp"}.get(norm_mime, "jpg")
        filename = f"screen_capture.{ext}"
        attach_js = f"""
        (async () => {{
            const allowedHosts = {_js_literal(list(_ALLOWED_GEMINI_WEB_HOSTS))};
            if ((location.protocol || "").toLowerCase() !== "https:" || !allowedHosts.includes((location.hostname || "").toLowerCase())) {{
                return "UNTRUSTED_ORIGIN";
            }}
            const inputSels = {_js_literal(list(input_sels or []))};
            const b64Data = {_js_literal(b64_data)};
            const filename = {_js_literal(filename)};
            const mime = {_js_literal(norm_mime)};
            const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            const wait = (ms) => new Promise(r => setTimeout(r, ms));
            let inputEl = null;
            for (const s of inputSels) {{
                try {{
                    const el = document.querySelector(s);
                    if (isVisible(el)) {{ inputEl = el; break; }}
                }} catch (e) {{}}
            }}
            if (!inputEl) return "NO_INPUT";
            let file = null;
            try {{
                const binStr = atob(b64Data);
                const bytes = new Uint8Array(binStr.length);
                for (let i = 0; i < binStr.length; i++) {{
                    bytes[i] = binStr.charCodeAt(i);
                }}
                file = new File([bytes], filename, {{ type: mime }});
            }} catch (e) {{
                return "FILE_BUILD_FAILED";
            }}
            const makeDT = () => {{
                const dt = new DataTransfer();
                dt.items.add(file);
                return dt;
            }};
            const previewSel = "img[src^='blob:'], img[src^='data:'], uploader-file-preview, [class*='image-preview'], [class*='file-preview'], [data-test-id*='image-preview'], [aria-label*='첨부파일 삭제'], [aria-label*='Remove']";
            const busySel = "mat-progress-bar, [role='progressbar'], .uploading, [aria-busy='true']";
            const countPreviews = () => {{
                try {{
                    return Array.from(document.querySelectorAll(previewSel)).filter(isVisible).length;
                }} catch (e) {{
                    return 0;
                }}
            }};
            const isUploading = () => {{
                try {{
                    const scope = inputEl.closest("input-area-v2, .input-area, form") || document;
                    return Array.from(scope.querySelectorAll(busySel)).some(isVisible);
                }} catch (e) {{
                    return false;
                }}
            }};
            const waitUploadSettle = async (maxMs) => {{
                const dl = Date.now() + maxMs;
                while (Date.now() < dl) {{
                    if (!isUploading()) return true;
                    await wait(100);
                }}
                return !isUploading();
            }};
            const basePreviewCount = countPreviews();
            inputEl.focus();
            try {{
                const pasteEv = new ClipboardEvent("paste", {{
                    bubbles: true,
                    cancelable: true,
                    clipboardData: makeDT()
                }});
                inputEl.dispatchEvent(pasteEv);
            }} catch (e) {{}}
            for (let i = 0; i < 15; i++) {{
                await wait(100);
                if (countPreviews() > basePreviewCount) {{
                    await waitUploadSettle(2000);
                    return "ATTACHED_PASTE";
                }}
            }}
            const dropTarget = inputEl.closest("input-area-v2, .input-area, form") || inputEl;
            try {{
                const dt = makeDT();
                for (const evType of ["dragenter", "dragover", "drop"]) {{
                    const de = new DragEvent(evType, {{
                        bubbles: true,
                        cancelable: true,
                        dataTransfer: dt
                    }});
                    dropTarget.dispatchEvent(de);
                }}
            }} catch (e) {{}}
            for (let i = 0; i < 25; i++) {{
                await wait(100);
                if (countPreviews() > basePreviewCount) {{
                    await waitUploadSettle(2500);
                    return "ATTACHED_DROP";
                }}
            }}
            return "NO_PREVIEW";
        }})()
        """
        try:
            res = str(cdp.evaluate(attach_js, timeout=10.0) or "")
        except Exception as exc:
            reason = f"CDP_ERR:{type(exc).__name__}"
            self._log(f"⚠️ [Gemini Web] 화면 이미지 첨부 실패({reason}) — 텍스트만으로 대화를 진행합니다.")
            return False, reason
        if res in ("ATTACHED_PASTE", "ATTACHED_DROP"):
            return True, res
        reason = res or "UNKNOWN"
        self._log(f"⚠️ [Gemini Web] 화면 이미지 첨부 미확인({reason}) — 텍스트만으로 대화를 진행합니다.")
        return False, reason

    def _run_cdp_prompt(
        self,
        cdp: LocalCDPClient,
        selectors: dict[str, Any],
        text: str,
        timeout_sec: float = 95.0,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[str], None] | None = None,
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
    ) -> str:
        """Install MutationObserver, insert prompt safely, submit, and poll with fail-fast diagnostics."""
        input_sels = selectors.get("input_selectors", [])
        send_sels = selectors.get("send_button_selectors", [])
        stop_sels = selectors.get("stop_button_selectors", [])
        resp_sels = selectors.get("response_selectors", [])
        allowed_web_hosts = list(_ALLOWED_PROVIDER_WEB_HOSTS.get(self.provider, _ALLOWED_GEMINI_WEB_HOSTS))
        login_hosts = [
            h
            for h in _ALLOWED_PROVIDER_LOGIN_HOSTS.get(self.provider, _ALLOWED_GEMINI_LOGIN_HOSTS)
            if h not in allowed_web_hosts and h != "challenges.cloudflare.com"
        ]
        is_claude = self.provider == "claude"
        is_chatgpt = self.provider == "chatgpt"
        prov_kr = self._provider_name_kr()

        def _enter_clean_midcall_auth_gate() -> None:
            if cdp is self._cdp:
                try:
                    self._launch_clean_login_window(
                        target_url=validate_provider_url(selectors.get("url"), provider=self.provider)
                    )
                except Exception:
                    self.login_mode_active = True

        def _raise_for_dom_gate(fallback_msg: str, is_timeout: bool = False) -> None:
            dom_st = ""
            try:
                dom_st = self._check_dom_login_state(cdp, selectors)
            except Exception:
                dom_st = ""
            if dom_st == "CHALLENGE_REQUIRED":
                _enter_clean_midcall_auth_gate()
                raise RuntimeError(
                    _scrub_secrets(
                        f"[CHALLENGE_REQUIRED] {prov_kr} 보안 확인(Cloudflare)이 필요합니다. 브라우저 창에서 확인을 완료해 주세요."
                    )
                )
            if dom_st == "LOGIN_REQUIRED":
                _enter_clean_midcall_auth_gate()
                raise RuntimeError(
                    _scrub_secrets(
                        f"[LOGIN_REQUIRED] {prov_kr} 로그인이 필요하거나 세션이 만료되었습니다. 브라우저 창에서 로그인을 완료해 주세요."
                    )
                )
            if is_timeout:
                raise TimeoutError(_scrub_secrets(fallback_msg))
            raise RuntimeError(_scrub_secrets(fallback_msg))

        setup_js = f"""
        (async () => {{
            const allowedHosts = {_js_literal(allowed_web_hosts)};
            if ((location.protocol || "").toLowerCase() !== "https:" || !allowedHosts.includes((location.hostname || "").toLowerCase())) {{
                return "UNTRUSTED_ORIGIN";
            }}
            const respSels = {_js_literal(resp_sels)};
            const stopSels = {_js_literal(stop_sels)};
            const inputSels = {_js_literal(input_sels)};
            const isClaude = {_js_literal(is_claude)};
            const isChatGpt = {_js_literal(is_chatgpt)};
            const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            const wait = (ms) => new Promise(r => setTimeout(r, ms));
            const stripChromeSel = "button, details, summary, nav, header, footer, .sr-only, h4[data-conversation-role], [data-testid*='copy'], [data-testid*='thought'], [data-testid*='reasoning'], [data-testid*='action-bar'], [data-testid*='artifact'], .code-block-header, [class*='code-header'], [class*='CodeHeader']";
            const extractCleanText = (rootEl) => {{
                if (!rootEl) return "";
                try {{
                    if (!rootEl.querySelector || !rootEl.querySelector(stripChromeSel)) {{
                        return (rootEl.innerText || "").trim();
                    }}
                    const clone = rootEl.cloneNode(true);
                    clone.querySelectorAll(stripChromeSel).forEach(n => n.remove());
                    clone.querySelectorAll("br").forEach(br => br.replaceWith(document.createTextNode("\\n")));
                    clone.querySelectorAll("p, li, pre, blockquote, h1, h2, h3, h4, h5, h6, tr").forEach(blk => {{
                        blk.appendChild(document.createTextNode("\\n"));
                    }});
                    return (clone.textContent || "").replace(/[ \\t]+\\n/g, "\\n").replace(/\\n{{3,}}/g, "\\n\\n").trim();
                }} catch (e) {{
                    return (rootEl.innerText || "").trim();
                }}
            }};
            const getResponses = () => {{
                if (isChatGpt) {{
                    const turnSels = [
                        "[data-content-search-unit-key$=':assistant']",
                        "[data-chatgpt-search-unit-key$=':assistant']",
                        "[data-message-author-role='assistant']",
                        "[data-message-role='assistant']",
                        "article[data-turn='assistant']",
                        "[data-markdown-text-style='assistant-message']",
                        "[data-response-body]",
                        "[data-dil-response]"
                    ];
                    for (const ts of turnSels) {{
                        try {{
                            let tNodes = Array.from(document.querySelectorAll(ts));
                            if (tNodes.length > 1) {{
                                tNodes = tNodes.filter(el => !(el.parentElement && el.parentElement.closest(ts)));
                            }}
                            if (tNodes.length > 0) return tNodes;
                        }} catch (e) {{}}
                    }}
                }}
                for (const s of respSels) {{
                    try {{
                        let nodes = Array.from(document.querySelectorAll(s));
                        if ((isClaude || isChatGpt) && nodes.length > 1) {{
                            // Keep one outer root per assistant turn so nested elements never double-count
                            nodes = nodes.filter(el => !(el.parentElement && el.parentElement.closest(s)));
                        }}
                        if (nodes.length > 0) return nodes;
                    }} catch (e) {{}}
                }}
                return [];
            }};
            const isClaudeThinkingNode = (el, root) => {{
                let cur = el;
                while (cur && cur !== root) {{
                    const tag = (cur.tagName || "").toLowerCase();
                    if (tag === "details" || tag === "summary" || tag === "button" || tag === "nav" || tag === "header" || tag === "footer") {{
                        return true;
                    }}
                    const tid = ((cur.getAttribute && (cur.getAttribute("data-testid") || cur.getAttribute("data-test-id"))) || "").toLowerCase();
                    if (tid.includes("thinking") || tid.includes("reasoning") || tid.includes("thought") || tid.includes("action-bar") || tid.includes("artifact")) {{
                        return true;
                    }}
                    const cls = (typeof cur.className === "string" ? cur.className : "").toLowerCase();
                    if (cls.includes("thinking") || cls.includes("thought") || cls.includes("reasoning")) {{
                        return true;
                    }}
                    const aria = ((cur.getAttribute && cur.getAttribute("aria-label")) || "").toLowerCase();
                    if (aria.includes("thinking") || aria.includes("thought") || aria.includes("생각")) {{
                        return true;
                    }}
                    try {{
                        const cs = window.getComputedStyle(cur);
                        if (cs) {{
                            if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return true;
                            if (cs.maxHeight === "0px" || cs.height === "0px" || cs.gridTemplateRows === "0px") return true;
                        }}
                    }} catch (e) {{}}
                    const inAnswerRow = !!(cur.closest && cur.closest(".row-start-2"));
                    const prev = cur.previousElementSibling;
                    if (prev && !inAnswerRow) {{
                        const prevIsDirectToggle = !!(prev.matches && prev.matches("button[aria-expanded]"));
                        const prevToggleBtn = prevIsDirectToggle
                            ? prev
                            : (prev.querySelector ? prev.querySelector("button[aria-expanded]") : null);
                        if (prevToggleBtn) {{
                            const prevHasBodyContent = !prevIsDirectToggle && !!(
                                prev.querySelector && prev.querySelector(".standard-markdown, .progressive-markdown, .font-claude-response-body, .prose, p")
                            );
                            const isExpanded = prevToggleBtn.getAttribute("aria-expanded") === "true";
                            const curCollapsedAttr = (
                                cur.getAttribute("aria-hidden") === "true" ||
                                /grid-rows-\\[0fr\\]/.test(typeof cur.className === "string" ? cur.className : "")
                            );
                            if (!prevHasBodyContent && (prevIsDirectToggle || isExpanded || curCollapsedAttr)) {{
                                return true;
                            }}
                        }}
                    }}
                    if (cur.classList && cur.classList.contains("row-start-1") && root.querySelector(".row-start-2")) {{
                        if (cur.querySelector("button[aria-expanded]") || /thought|thinking|생각|사고/i.test((cur.innerText || "").slice(0, 80))) {{
                            return true;
                        }}
                    }}
                    cur = cur.parentElement;
                }}
                return false;
            }};
            const extractClaudeText = (turnEl) => {{
                if (!turnEl) return "";
                const mdSel = ".standard-markdown, .progressive-markdown, .font-claude-response-body, .prose";
                const rawBlocks = Array.from(turnEl.querySelectorAll(mdSel));
                const cleanBlocks = rawBlocks.filter(b => {{
                    if (b.parentElement && b.parentElement.closest(mdSel) && b.parentElement.closest(mdSel) !== turnEl) {{
                        // Avoid duplicating text from nested markdown wrappers
                        const outer = b.parentElement.closest(mdSel);
                        if (outer && turnEl.contains(outer)) return false;
                    }}
                    return !isClaudeThinkingNode(b, turnEl);
                }});
                if (cleanBlocks.length > 0) {{
                    return cleanBlocks
                        .map(b => extractCleanText(b))
                        .filter(Boolean)
                        .join("\\n")
                        .trim();
                }}
                // If only a thinking accordion has mounted so far, wait cleanly instead of leaking thinking text!
                if (turnEl.querySelector("button[aria-expanded], [data-testid*='thinking'], [data-testid*='reasoning'], details")) {{
                    return "";
                }}
                const leafBlocks = Array.from(turnEl.querySelectorAll("p, li, blockquote, pre"))
                    .filter(el => !isClaudeThinkingNode(el, turnEl));
                if (leafBlocks.length > 0) {{
                    return leafBlocks.map(el => extractCleanText(el)).filter(Boolean).join("\\n").trim();
                }}
                return "";
            }};
            const extractChatGptText = (el) => {{
                if (!el) return "";
                const mdSel = "[data-markdown-text-style='assistant-message'], [data-dil-response], .markdown, [data-assistant-markdown], [data-assistant-markdown-root], .prose";
                if (el.matches && el.matches(mdSel)) {{
                    return extractCleanText(el);
                }}
                const mdNodes = Array.from(el.querySelectorAll(mdSel)).filter(n => {{
                    if (n.closest("details, [data-testid*='thought'], [data-testid*='reasoning']")) return false;
                    const outer = n.parentElement && n.parentElement.closest(mdSel);
                    if (outer && el.contains(outer)) return false;
                    return true;
                }});
                if (mdNodes.length > 0) {{
                    return mdNodes.map(n => extractCleanText(n)).filter(Boolean).join("\\n").trim();
                }}
                // Classic reasoning turn before .markdown mounts: never scrape "Thought for..." / "생각 중..." chrome!
                const raw = extractCleanText(el);
                if (!raw || /^(?:Thought for [^\\n]{{1,60}}|Thinking\\.{{0,3}}|생각\\s*중\\.{{0,3}}|\\d+\\s*초\\s*동안\\s*생각함?|ChatGPT\\s*(?:답변|의\\s*말)\\s*:?)$/i.test(raw)) {{
                    return "";
                }}
                return raw;
            }};
            const extractResponseText = (node) => {{
                if (!node) return "";
                if (isClaude) return extractClaudeText(node);
                if (isChatGpt) return extractChatGptText(node);
                return extractCleanText(node);
            }};
            const getUserMsgCount = () => {{
                try {{
                    if (isClaude) return document.querySelectorAll("[data-testid='user-message']").length;
                    if (isChatGpt) {{
                        for (const us of [
                            "[data-user-message-bubble='true']",
                            "[data-content-search-unit-key$=':user']",
                            "[data-chatgpt-search-unit-key$=':user']",
                            "[data-message-author-role='user'], [data-message-role='user']"
                        ]) {{
                            const cnt = document.querySelectorAll(us).length;
                            if (cnt > 0) return cnt;
                        }}
                        return 0;
                    }}
                    return document.querySelectorAll("user-query").length;
                }} catch (e) {{
                    return 0;
                }}
            }};
            const isGenerating = () => {{
                if (isClaude) {{
                    try {{
                        const sEls = Array.from(document.querySelectorAll("[data-is-streaming='true']"));
                        if (sEls.some(isVisible)) return true;
                    }} catch (e) {{}}
                }} else if (isChatGpt) {{
                    try {{
                        const sEls = Array.from(document.querySelectorAll(
                            "[data-talvt-turn-state='in_progress'], [data-markdown-text-style='assistant-message'][data-markdown-animated], [data-message-role='assistant'][data-message-streaming]:not([data-message-streaming='false'])"
                        ));
                        if (sEls.some(isVisible)) return true;
                    }} catch (e) {{}}
                }}
                for (const s of stopSels) {{
                    try {{
                        if (isVisible(document.querySelector(s))) return true;
                    }} catch (e) {{}}
                }}
                return false;
            }};

            if (isGenerating()) {{
                for (const s of stopSels) {{
                    try {{
                        const sb = document.querySelector(s);
                        if (isVisible(sb)) {{
                            try {{ sb.click(); }} catch (e) {{}}
                            break;
                        }}
                    }} catch (e) {{}}
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
            const baseLastText = baseCount > 0 ? extractResponseText(baseNodes[baseCount - 1]) : "";
            const baseUserMsgCount = getUserMsgCount();

            window.__vcStream = {{
                baseCount,
                baseLastText,
                baseUserMsgCount,
                started: false,
                generating: false,
                wasGenerating: false,
                text: "",
                lastMutAt: Date.now()
            }};
            window.__vcPollTick = 0;

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
                    const txt = extractResponseText(nodes[nodes.length - 1]);
                    if (txt && txt !== st.text) {{
                        st.text = txt;
                        st.lastMutAt = Date.now();
                    }}
                }} else if (nodes.length === st.baseCount && st.baseCount > 0) {{
                    const cur = extractResponseText(nodes[nodes.length - 1]);
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
            window.__vcObserver.observe(document.body, {{
                childList: true,
                subtree: true,
                characterData: true,
                attributes: true,
                attributeFilter: [
                    "data-is-streaming",
                    "aria-label",
                    "disabled",
                    "data-message-streaming",
                    "data-stop-generating",
                    "data-talvt-turn-state",
                    "data-markdown-animated"
                ]
            }});

            // Safely dismiss stale CDK overlay backdrops or benign modal dialogs before focusing input
            try {{
                const backdrops = Array.from(document.querySelectorAll(".cdk-overlay-backdrop")).filter(isVisible);
                if (backdrops.length > 0) {{
                    document.dispatchEvent(new KeyboardEvent("keydown", {{ key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true }}));
                    for (const bd of backdrops) {{
                        try {{ bd.click(); }} catch (e) {{}}
                    }}
                    await wait(120);
                }}
            }} catch (e) {{}}
            try {{
                const benignCloseRe = /닫기|Close|나중에|Not now|No thanks|건너뛰기|Skip|괜찮습니다/i;
                const dialogs = Array.from(document.querySelectorAll("[role='dialog'], mat-dialog-container")).filter(isVisible);
                for (const dlg of dialogs) {{
                    const btns = Array.from(dlg.querySelectorAll("button, [role='button']")).filter(isVisible);
                    const closeBtn = btns.find(b => {{
                        const lbl = ((b.innerText || "") + " " + (b.getAttribute("aria-label") || "")).replace(/\\s+/g, " ").trim();
                        return benignCloseRe.test(lbl);
                    }});
                    if (closeBtn) {{
                        try {{ closeBtn.click(); }} catch (e) {{}}
                        await wait(120);
                    }}
                }}
            }} catch (e) {{}}

            let inputEl = null;
            for (const s of inputSels) {{
                try {{
                    const el = document.querySelector(s);
                    if (isVisible(el)) {{ inputEl = el; break; }}
                }} catch (e) {{}}
            }}
            if (!inputEl) return "NO_INPUT";
            const isProseMirror = !!(
                (isClaude || isChatGpt) &&
                inputEl.tagName !== "TEXTAREA" &&
                inputEl.tagName !== "INPUT" &&
                (
                    inputEl.classList.contains("ProseMirror") ||
                    inputEl.getAttribute("data-testid") === "chat-input" ||
                    inputEl.id === "prompt-textarea" ||
                    inputEl.hasAttribute("data-composer-markdown") ||
                    inputEl.getAttribute("contenteditable") === "true"
                )
            );
            inputEl.focus();
            if (inputEl.tagName === "TEXTAREA" || inputEl.tagName === "INPUT") {{
                if (inputEl.value) {{
                    inputEl.value = "";
                    inputEl.dispatchEvent(new Event("input", {{ bubbles: true }}));
                }}
            }} else if (isProseMirror) {{
                const curTxt = (inputEl.innerText || "").replace(/\\u200b/g, "").trim();
                if (curTxt.length > 0) {{
                    try {{
                        const sel = window.getSelection();
                        const range = document.createRange();
                        range.selectNodeContents(inputEl);
                        sel.removeAllRanges();
                        sel.addRange(range);
                        document.execCommand("delete", false, null);
                    }} catch (e) {{}}
                }}
                try {{
                    const pEl = inputEl.querySelector("p") || inputEl;
                    try {{ pEl.click(); }} catch (e) {{}}
                    inputEl.focus();
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(pEl);
                    range.collapse(false);
                    sel.removeAllRanges();
                    sel.addRange(range);
                }} catch (e) {{}}
            }} else {{
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(inputEl);
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand("delete", false, null);
            }}
            return isProseMirror ? "READY_PROSEMIRROR" : "READY";
        }})()
        """
        prep = cdp.evaluate(setup_js, timeout=10.0)
        if prep not in ("READY", "READY_PROSEMIRROR"):
            _raise_for_dom_gate(
                f"{prov_kr} 입력창을 찾지 못했습니다 (selectors.json 확인 필요: {prep})",
                is_timeout=False,
            )

        image_attached = False
        if self.provider == "gemini" and image_bytes:
            norm_img, norm_mime = _normalize_vision_image(image_bytes, image_mime)
            if norm_img:
                ok_img, _img_reason = self._attach_image_via_cdp(
                    cdp,
                    input_sels,
                    norm_img,
                    image_mime=norm_mime,
                    cancel_event=cancel_event,
                )
                image_attached = bool(ok_img)
                if self._is_aborted(cancel_event):
                    try:
                        cdp.evaluate(
                            "if (window.__vcObserver) { try { window.__vcObserver.disconnect(); } catch(e) {} }",
                            timeout=2.0,
                        )
                    except Exception:
                        pass
                    return ""
                if image_attached:
                    time.sleep(0.15)
                refocus_js = f"""
                (() => {{
                    const inputSels = {_js_literal(input_sels)};
                    const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                    for (const s of inputSels) {{
                        try {{
                            const el = document.querySelector(s);
                            if (isVisible(el)) {{
                                el.focus();
                                try {{
                                    const sel = window.getSelection();
                                    const range = document.createRange();
                                    range.selectNodeContents(el);
                                    range.collapse(false);
                                    sel.removeAllRanges();
                                    sel.addRange(range);
                                }} catch (e) {{}}
                                return "FOCUSED";
                            }}
                        }} catch (e) {{}}
                    }}
                    return "NO_INPUT";
                }})()
                """
                try:
                    cdp.evaluate(refocus_js, timeout=4.0)
                except Exception:
                    pass

        is_prosemirror = prep == "READY_PROSEMIRROR"
        if is_prosemirror and ("\n" in text or len(text) > 1400):
            # Insert multi-line / long ProseMirror prompts safely via line chunks + Shift+Enter
            # (prevents ProseMirror newline flattening & avoids Claude >2.5KB [Pasted Text] chip conversion).
            lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for idx, ln in enumerate(lines):
                if idx % 12 == 0 and self._is_aborted(cancel_event):
                    try:
                        cdp.evaluate(
                            "if (window.__vcObserver) { try { window.__vcObserver.disconnect(); } catch(e) {} }",
                            timeout=2.0,
                        )
                    except Exception:
                        pass
                    return ""
                if ln:
                    for off in range(0, len(ln), 1200):
                        cdp.call("Input.insertText", {"text": ln[off : off + 1200]}, timeout=10.0)
                if idx < len(lines) - 1:
                    cdp.call(
                        "Input.dispatchKeyEvent",
                        {
                            "type": "keyDown",
                            "key": "Enter",
                            "code": "Enter",
                            "text": "\r",
                            "unmodifiedText": "\r",
                            "modifiers": 8,
                            "windowsVirtualKeyCode": 13,
                            "nativeVirtualKeyCode": 13,
                        },
                        timeout=5.0,
                    )
                    cdp.call(
                        "Input.dispatchKeyEvent",
                        {
                            "type": "keyUp",
                            "key": "Enter",
                            "code": "Enter",
                            "modifiers": 8,
                            "windowsVirtualKeyCode": 13,
                            "nativeVirtualKeyCode": 13,
                        },
                        timeout=5.0,
                    )
        else:
            cdp.call("Input.insertText", {"text": text}, timeout=10.0)
        time.sleep(0.08)

        verify_insert_js = f"""
        (() => {{
            const inputSels = {_js_literal(input_sels)};
            const promptText = {_js_literal(text)};
            const isClaude = {_js_literal(is_claude)};
            const isChatGpt = {_js_literal(is_chatgpt)};
            const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            let inputEl = null;
            for (const s of inputSels) {{
                try {{
                    const el = document.querySelector(s);
                    if (isVisible(el)) {{ inputEl = el; break; }}
                }} catch (e) {{}}
            }}
            if (!inputEl) return "NO_INPUT";
            const getVal = (el) => (el.tagName === "TEXTAREA" || el.tagName === "INPUT")
                ? (el.value || "")
                : (el.innerText || el.textContent || "");
            const noWsLen = (s) => (s || "").replace(/\\s+/g, "").length;
            const expectedLen = noWsLen(promptText);
            const minGoodLen = expectedLen <= 0 ? 0 : Math.max(1, Math.floor(expectedLen * 0.7));
            const hasPastedChip = () => {{
                if (!isClaude) return false;
                try {{
                    const scope = inputEl.closest("fieldset, form") || document;
                    const chip = scope.querySelector("[data-testid*='file-thumbnail'], [data-testid*='pasted'], [data-testid*='attachment']");
                    return isVisible(chip);
                }} catch (e) {{
                    return false;
                }}
            }};
            if (expectedLen === 0 || noWsLen(getVal(inputEl)) >= minGoodLen || hasPastedChip()) {{
                return "OK";
            }}
            const isProseMirror = !!(
                (isClaude || isChatGpt) &&
                inputEl.tagName !== "TEXTAREA" &&
                inputEl.tagName !== "INPUT" &&
                (
                    inputEl.classList.contains("ProseMirror") ||
                    inputEl.getAttribute("data-testid") === "chat-input" ||
                    inputEl.id === "prompt-textarea" ||
                    inputEl.hasAttribute("data-composer-markdown") ||
                    inputEl.getAttribute("contenteditable") === "true"
                )
            );
            inputEl.focus();
            if (inputEl.tagName === "TEXTAREA" || inputEl.tagName === "INPUT") {{
                const proto = inputEl.tagName === "TEXTAREA"
                    ? (window.HTMLTextAreaElement && window.HTMLTextAreaElement.prototype)
                    : (window.HTMLInputElement && window.HTMLInputElement.prototype);
                const desc = proto ? Object.getOwnPropertyDescriptor(proto, "value") : null;
                if (desc && typeof desc.set === "function") {{
                    desc.set.call(inputEl, promptText);
                }} else {{
                    inputEl.value = promptText;
                }}
                inputEl.dispatchEvent(new Event("input", {{ bubbles: true }}));
                inputEl.dispatchEvent(new Event("change", {{ bubbles: true }}));
            }} else if (isProseMirror) {{
                // Clear any partial insertion safely, then place caret inside <p> (NEVER raw textContent =!)
                if (noWsLen(getVal(inputEl)) > 0) {{
                    try {{
                        const sel = window.getSelection();
                        const range = document.createRange();
                        range.selectNodeContents(inputEl);
                        sel.removeAllRanges();
                        sel.addRange(range);
                        document.execCommand("delete", false, null);
                    }} catch (e) {{}}
                }}
                try {{
                    const pEl = inputEl.querySelector("p") || inputEl;
                    try {{ pEl.click(); }} catch (e) {{}}
                    inputEl.focus();
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(pEl);
                    range.collapse(false);
                    sel.removeAllRanges();
                    sel.addRange(range);
                }} catch (e) {{}}
                const trySyntheticPaste = () => {{
                    try {{
                        const dt = new DataTransfer();
                        dt.setData("text/plain", promptText);
                        const ev = new ClipboardEvent("paste", {{ bubbles: true, cancelable: true, clipboardData: dt }});
                        inputEl.dispatchEvent(ev);
                    }} catch (e) {{}}
                }};
                const tryChunkedExec = () => {{
                    const lines = promptText.replace(/\\r\\n/g, "\\n").replace(/\\r/g, "\\n").split("\\n");
                    for (let i = 0; i < lines.length; i++) {{
                        const ln = lines[i];
                        for (let off = 0; off < ln.length; off += 1200) {{
                            document.execCommand("insertText", false, ln.slice(off, off + 1200));
                        }}
                        if (i < lines.length - 1) {{
                            document.execCommand("insertLineBreak", false, null);
                        }}
                    }}
                }};
                if (isChatGpt) {{
                    trySyntheticPaste();
                    if (noWsLen(getVal(inputEl)) < minGoodLen) {{
                        tryChunkedExec();
                    }}
                }} else {{
                    // Claude: prefer chunked execCommand under attachment threshold before paste fallback
                    tryChunkedExec();
                    if (noWsLen(getVal(inputEl)) < minGoodLen && !hasPastedChip()) {{
                        trySyntheticPaste();
                    }}
                }}
            }} else {{
                try {{
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(inputEl);
                    range.collapse(false);
                    sel.removeAllRanges();
                    sel.addRange(range);
                }} catch (e) {{}}
                const inserted = document.execCommand("insertText", false, promptText);
                if (!inserted && getVal(inputEl).trim().length === 0) {{
                    inputEl.textContent = promptText;
                    inputEl.dispatchEvent(new InputEvent("input", {{ bubbles: true, data: promptText, inputType: "insertText" }}));
                }}
            }}
            return (noWsLen(getVal(inputEl)) >= minGoodLen || hasPastedChip() || getVal(inputEl).trim().length > 0)
                ? "FALLBACK_INSERTED"
                : "EMPTY";
        }})()
        """
        insert_state = ""
        try:
            insert_state = str(cdp.evaluate(verify_insert_js, timeout=6.0) or "")
            if insert_state == "FALLBACK_INSERTED":
                time.sleep(0.08)
        except Exception:
            pass
        if insert_state in ("EMPTY", "NO_INPUT"):
            _raise_for_dom_gate(
                f"{prov_kr} 입력창에 텍스트를 입력하지 못했습니다 ({insert_state}). 브라우저 창 상태를 확인해 주세요.",
                is_timeout=False,
            )

        click_max_attempts = 40 if image_attached else 15
        click_last_attempt = click_max_attempts - 1
        click_timeout = 12.0 if image_attached else 8.0
        click_js = f"""
        (async () => {{
            const sendSels = {_js_literal(send_sels)};
            const inputSels = {_js_literal(input_sels)};
            const isClaude = {_js_literal(is_claude)};
            const isChatGpt = {_js_literal(is_chatgpt)};
            const isVisible = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
            const wait = (ms) => new Promise(r => setTimeout(r, ms));
            const isSafeSendBtn = (btn) => {{
                if (!isVisible(btn) || btn.disabled || btn.getAttribute("aria-disabled") === "true") return false;
                if (btn.hasAttribute("data-visually-disabled") || btn.hasAttribute("data-stop-generating")) return false;
                const tid = (btn.getAttribute("data-testid") || "").toLowerCase();
                if (isChatGpt && (tid === "stop-button" || tid === "composer-speech-button" || btn.hasAttribute("data-composer-voice-start"))) return false;
                if (isClaude) {{
                    const lbl = (btn.getAttribute("aria-label") || "").toLowerCase();
                    if (lbl.includes("feedback") || lbl.includes("피드백")) return false;
                }}
                return true;
            }};
            for (let attempt = 0; attempt < {click_max_attempts}; attempt++) {{
                for (const s of sendSels) {{
                    try {{
                        const btn = document.querySelector(s);
                        if (isSafeSendBtn(btn)) {{
                            btn.click();
                            return "CLICKED";
                        }}
                    }} catch (e) {{}}
                }}
                if (attempt < {click_last_attempt}) await wait(75);
            }}
            for (const s of inputSels) {{
                try {{
                    const el = document.querySelector(s);
                    if (isVisible(el)) {{ el.focus(); break; }}
                }} catch (e) {{}}
            }}
            return "NO_BUTTON";
        }})()
        """

        def _dispatch_enter() -> None:
            cdp.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "key": "Enter",
                    "code": "Enter",
                    "text": "\r",
                    "unmodifiedText": "\r",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
                timeout=5.0,
            )
            cdp.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
                timeout=5.0,
            )

        clicked = cdp.evaluate(click_js, timeout=click_timeout)
        if clicked != "CLICKED":
            _dispatch_enter()

        start_time = time.monotonic()
        deadline = start_time + float(timeout_sec)
        start_timeout_sec = min(20.0, max(12.0, float(timeout_sec) * 0.4))
        post_gen_empty_timeout_sec = min(12.0, max(6.0, float(timeout_sec) * 0.25))
        post_gen_empty_since: float | None = None
        retried_submit = False
        prev_cur_text = ""
        stable_polls = 0
        last_cur_text = ""
        aborted = False
        poll_js = f"""
        (() => {{
            if (typeof window.__vcRefresh === "function") {{
                try {{ window.__vcRefresh(); }} catch (e) {{}}
            }}
            const st = window.__vcStream || {{}};
            const now = Date.now();
            window.__vcPollTick = (window.__vcPollTick || 0) + 1;
            const shouldInspect = (!st.started) || (window.__vcPollTick % 12 === 0);
            let gate = "";
            let providerError = "";
            let submitted = !!(st.started || st.generating);
            if (shouldInspect) {{
                const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                const host = (location.hostname || "").toLowerCase();
                const pathname = (location.pathname || "").toLowerCase();
                const titleLow = (document.title || "").toLowerCase();
                const loginHosts = {_js_literal(login_hosts)};
                const isClaude = {_js_literal(is_claude)};
                const isChatGpt = {_js_literal(is_chatgpt)};
                if (
                    host === "challenges.cloudflare.com" ||
                    titleLow.includes("just a moment") ||
                    titleLow.includes("attention required") ||
                    titleLow.includes("잠시만 기다려") ||
                    titleLow.includes("잠시만 기다리십시오") ||
                    document.querySelector("#challenge-running, #challenge-stage, #turnstile-wrapper, iframe[src*='challenges.cloudflare.com']")
                ) {{
                    gate = "CHALLENGE_REQUIRED";
                }} else if (
                    loginHosts.includes(host) ||
                    (isChatGpt && (pathname === "/auth" || pathname.startsWith("/auth/") || pathname.startsWith("/login") || isVis(document.querySelector("#mobile-auth-dialog, form[data-logged-out]")))) ||
                    (isClaude && (pathname.startsWith("/login") || pathname.startsWith("/signup") || pathname.startsWith("/oauth")))
                ) {{
                    gate = "LOGIN_REQUIRED";
                }} else if (isClaude || isChatGpt) {{
                    try {{
                        const dlgs = Array.from(document.querySelectorAll("[role='dialog'][aria-modal='true'], [role='alertdialog']")).filter(isVis);
                        const authRe = /log\\s*in|sign\\s*in|sign\\s*up|continue with google|로그인|회원가입|계속하려면 로그인/i;
                        for (const d of dlgs) {{
                            const dt = (d.innerText || "").replace(/\\s+/g, " ").trim();
                            if (d.querySelector("input[type='email'], input[type='password']") || authRe.test(dt)) {{
                                gate = "LOGIN_REQUIRED";
                                break;
                            }}
                        }}
                    }} catch (e) {{}}
                }}
                if (!gate && !submitted) {{
                    try {{
                        let uCnt = 0;
                        if (isClaude) uCnt = document.querySelectorAll("[data-testid='user-message']").length;
                        else if (isChatGpt) {{
                            for (const us of [
                                "[data-user-message-bubble='true']",
                                "[data-content-search-unit-key$=':user']",
                                "[data-chatgpt-search-unit-key$=':user']",
                                "[data-message-author-role='user'], [data-message-role='user']"
                            ]) {{
                                const c = document.querySelectorAll(us).length;
                                if (c > 0) {{ uCnt = c; break; }}
                            }}
                        }}
                        else uCnt = document.querySelectorAll("user-query").length;
                        if (uCnt > (st.baseUserMsgCount || 0)) {{
                            submitted = true;
                        }} else {{
                            const inputSels = {_js_literal(input_sels)};
                            for (const s of inputSels) {{
                                const el = document.querySelector(s);
                                if (isVis(el)) {{
                                    const v = (el.tagName === "TEXTAREA" || el.tagName === "INPUT") ? (el.value || "") : (el.innerText || "");
                                    if (v.trim().length === 0) submitted = true;
                                    break;
                                }}
                            }}
                        }}
                    }} catch (e) {{}}
                }}
                if (!gate && (isClaude || isChatGpt) && (!st.generating) && !(st.text || "").trim()) {{
                    try {{
                        const alertNodes = Array.from(document.querySelectorAll("[role='alert'], [data-testid*='error'], [data-testid*='toast'], .text-danger, .bg-danger-100")).filter(isVis);
                        const errRe = /reached your limit|usage limit|exceed the length limit|message is too long|already have a request in progress|something went wrong|too many requests|unable to load|overloaded|capacity|한도에 도달|길이 제한|진행 중인 요청|요청이 너무 많|메시지가 너무 깁니다|문제가 발생|오류가 발생/i;
                        const loginBannerRe = /sign in to continue|log in to continue|계속하려면 로그인|세션이 만료/i;
                        for (const an of alertNodes) {{
                            const at = (an.innerText || "").replace(/\\s+/g, " ").trim();
                            if (!at || at.length > 360) continue;
                            if (loginBannerRe.test(at)) {{
                                gate = "LOGIN_REQUIRED";
                                break;
                            }}
                            if (errRe.test(at)) {{
                                providerError = at.slice(0, 220);
                                break;
                            }}
                        }}
                    }} catch (e) {{}}
                }}
            }}
            return {{
                started: !!st.started,
                generating: !!st.generating,
                text: st.text || "",
                idleMs: now - (st.lastMutAt || now),
                submitted,
                gate,
                providerError
            }};
        }})()
        """
        def _click_browser_stop_button() -> None:
            try:
                stop_js = f"""(() => {{
                    const sels = {_js_literal(stop_sels)};
                    const isVis = (el) => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0));
                    for (const s of sels) {{
                        try {{
                            const b = document.querySelector(s);
                            if (isVis(b)) {{ try {{ b.click(); }} catch (e) {{}} return "STOPPED"; }}
                        }} catch (e) {{}}
                    }}
                    return "NONE";
                }})()"""
                cdp.evaluate(stop_js, timeout=2.0)
            except Exception:
                pass

        completed_cleanly = False
        try:
            while time.monotonic() < deadline:
                if self._is_aborted(cancel_event):
                    aborted = True
                    _click_browser_stop_button()
                    break
                st = cdp.evaluate(poll_js, timeout=6.0) or {}
                gate = str(st.get("gate") or "")
                if gate == "CHALLENGE_REQUIRED":
                    _enter_clean_midcall_auth_gate()
                    raise RuntimeError(
                        _scrub_secrets(
                            f"[CHALLENGE_REQUIRED] {prov_kr} 보안 확인(Cloudflare)이 필요합니다. 브라우저 창에서 확인을 완료해 주세요."
                        )
                    )
                if gate == "LOGIN_REQUIRED":
                    _enter_clean_midcall_auth_gate()
                    raise RuntimeError(
                        _scrub_secrets(
                            f"[LOGIN_REQUIRED] {prov_kr} 로그인이 필요하거나 세션이 만료되었습니다. 브라우저 창에서 로그인을 완료해 주세요."
                        )
                    )

                cur_text = str(st.get("text") or "")
                last_cur_text = cur_text
                if cur_text and on_progress is not None:
                    on_progress(cur_text)

                prov_err = str(st.get("providerError") or "").strip()
                if prov_err and (not st.get("generating")) and (not cur_text.strip()):
                    raise RuntimeError(_scrub_secrets(f"[PROVIDER_ERROR] {prov_kr} 오류: {prov_err}"))

                elapsed = time.monotonic() - start_time
                if (
                    (not retried_submit)
                    and elapsed >= 3.5
                    and (not st.get("started"))
                    and (not st.get("generating"))
                    and (st.get("submitted") is False)
                ):
                    retried_submit = True
                    try:
                        re_click = cdp.evaluate(click_js, timeout=4.0)
                        if re_click != "CLICKED":
                            _dispatch_enter()
                    except Exception:
                        pass

                if (
                    (not st.get("started"))
                    and (not st.get("generating"))
                    and (not cur_text.strip())
                    and elapsed >= start_timeout_sec
                ):
                    _raise_for_dom_gate(
                        f"[RESPONSE_TIMEOUT] {prov_kr} 응답이 {int(start_timeout_sec)}초 내에 시작되지 않았습니다. 브라우저 창에서 로그인·팝업·오류 여부를 확인해 주세요.",
                        is_timeout=True,
                    )

                if st.get("started") and (not st.get("generating")) and (not cur_text.strip()):
                    now_mono = time.monotonic()
                    if post_gen_empty_since is None:
                        post_gen_empty_since = now_mono
                    elif (now_mono - post_gen_empty_since) >= post_gen_empty_timeout_sec:
                        _raise_for_dom_gate(
                            f"[RESPONSE_TIMEOUT] {prov_kr} 응답 생성은 끝났지만 본문 텍스트를 읽지 못했습니다 ({int(post_gen_empty_timeout_sec)}초). 브라우저 창 또는 selectors.json을 확인해 주세요.",
                            is_timeout=True,
                        )
                else:
                    post_gen_empty_since = None

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
                        completed_cleanly = True
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

        if (not aborted) and (not completed_cleanly) and time.monotonic() >= deadline:
            _click_browser_stop_button()
            if not last_cur_text.strip():
                _raise_for_dom_gate(
                    f"[RESPONSE_TIMEOUT] {prov_kr} 응답 대기 시간({int(timeout_sec)}초)이 초과되었습니다.",
                    is_timeout=True,
                )
            self._log(
                f"[{prov_kr} Web] 응답 대기 시간({int(timeout_sec)}초) 초과로 생성 중지 클릭 후 부분 응답 반환"
            )
        return last_cur_text

    def _ensure_translator_cdp(self) -> LocalCDPClient:
        """Open/reuse a hidden 2nd CDP tab sharing BrowserProfile for background subtitle translation."""
        if self.provider in ("claude", "chatgpt") and self._main_stream_active:
            raise RuntimeError("TRANSLATOR_MAIN_STREAM_BUSY")
        # Enforce strict lock ordering (`_io_lock -> _trans_lock`): never call ensure_ready_and_check_login inside `_trans_lock`
        if self._cdp is None or not self.is_running() or self.login_mode_active or self._cdp_port is None:
            ok, reason = self.ensure_ready_and_check_login(
                show_if_ready=False,
                allow_interactive_login=False,
                _clear_abort=False,
            )
            if not ok:
                raise RuntimeError(f"TRANSLATOR_LOGIN_REQUIRED:{reason}")

        with self._trans_lock:
            if self.provider in ("claude", "chatgpt") and self._main_stream_active:
                raise RuntimeError("TRANSLATOR_MAIN_STREAM_BUSY")
            selectors = self._load_active_selectors()
            target_url = validate_provider_url(selectors.get("url"), provider=self.provider)

            if self._trans_cdp is not None and self.is_running() and not self.login_mode_active:
                try:
                    self._trans_cdp.evaluate("location.href", timeout=3.0)
                    return self._trans_cdp
                except Exception:
                    self._close_translator_target()

            port = self._cdp_port
            if port is None or not self.is_running() or self.login_mode_active:
                raise RuntimeError("TRANSLATOR_LOGIN_REQUIRED:NOT_RUNNING")
            before_hwnds: set[int] = set()
            proc = self._proc
            if os.name == "nt" and proc is not None:
                before_hwnds = set(_get_windows_for_pids(_get_child_pids(proc.pid)))

            ver = self._cdp_http_get_json(port, "/json/version", timeout_sec=3.0)
            browser_ws_raw = str((ver if isinstance(ver, dict) else {}).get("webSocketDebuggerUrl") or "")
            if not browser_ws_raw:
                raise RuntimeError("브라우저 CDP Version WebSocket을 찾지 못했습니다.")
            browser_ws = self._validate_cdp_ws_for_port(browser_ws_raw, port)

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

            targets = self._cdp_http_get_json(port, "/json", timeout_sec=3.0)
            page = next((t for t in (targets if isinstance(targets, list) else []) if isinstance(t, dict) and t.get("id") == target_id), None)
            if not page or not page.get("webSocketDebuggerUrl"):
                self._trans_target_id = target_id
                self._close_translator_target()
                raise RuntimeError("번역 탭 WebSocket URL을 찾지 못했습니다.")

            trans_ws = self._validate_cdp_ws_for_port(page["webSocketDebuggerUrl"], port)
            self._trans_target_id = target_id
            self._trans_cdp = LocalCDPClient(trans_ws, timeout=12.0)
            self._trans_turn_count = 0
            try:
                self._trans_cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=4.0)
            except Exception:
                pass

            # Chrome in --app mode spawns a second HWND for Target.createTarget: hide it immediately!
            proc = self._proc
            if os.name == "nt" and proc is not None:
                hide_deadline = time.monotonic() + 0.85
                while time.monotonic() < hide_deadline:
                    cur_proc = self._proc
                    if cur_proc is None:
                        break
                    after_hwnds = set(_get_windows_for_pids(_get_child_pids(cur_proc.pid)))
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
                if self._is_aborted():
                    self._close_translator_target()
                    raise RuntimeError("TRANSLATOR_CANCELLED")
                st = self._check_dom_login_state(self._trans_cdp, selectors)
                if st == "READY":
                    return self._trans_cdp
                if st in ("LOGIN_REQUIRED", "CHALLENGE_REQUIRED"):
                    self._close_translator_target()
                    raise RuntimeError(f"TRANSLATOR_LOGIN_REQUIRED:{st}")
                time.sleep(0.3)

            self._close_translator_target()
            raise RuntimeError(f"번역용 백그라운드 {self._provider_name_kr()} 탭 로딩 시간 초과")

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
        if self.provider in ("claude", "chatgpt") and self._main_stream_active:
            return ""
        if self.provider == "gemini":
            eff_model, eff_level = split_legacy_browser_model(model_code, thinking_level)
        else:
            eff_model = (model_code or get_provider_default_browser_model(self.provider)).strip()
            eff_level = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        eff_timeout = (
            float(timeout_sec)
            if timeout_sec is not None
            else {"low": 28.0, "medium": 38.0, "high": 60.0, "xhigh": 75.0}.get(eff_level, 28.0)
        )

        selectors = self._load_active_selectors()
        target_url = validate_provider_url(selectors.get("url"), provider=self.provider)
        for attempt in range(2):
            try:
                # Obtain/boot helper tab BEFORE holding `_trans_lock` across the prompt so `_io_lock -> _trans_lock` never inverts
                cdp = self._ensure_translator_cdp()
                with self._trans_lock:
                    if self.provider in ("claude", "chatgpt") and self._main_stream_active:
                        return ""
                    if self._trans_turn_count >= 24:
                        cdp.evaluate(
                            "window.__vcStalePage = true; window.__vcModelSpec = ''; "
                            f"window.location.href = {_js_literal(target_url)};",
                            timeout=8.0,
                        )
                        wait_dl = time.monotonic() + 10.0
                        while time.monotonic() < wait_dl:
                            if self._is_aborted():
                                return ""
                            try:
                                not_stale = cdp.evaluate("window.__vcStalePage !== true", timeout=4.0)
                                if not_stale:
                                    dom_st = self._check_dom_login_state(cdp, selectors)
                                    if dom_st == "READY":
                                        break
                                    if dom_st in ("LOGIN_REQUIRED", "CHALLENGE_REQUIRED"):
                                        self._close_translator_target()
                                        return ""
                            except Exception:
                                pass
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
                if any(
                    tok in str(exc)
                    for tok in (
                        "TRANSLATOR_LOGIN_REQUIRED",
                        "TRANSLATOR_CANCELLED",
                        "TRANSLATOR_MAIN_STREAM_BUSY",
                        "[LOGIN_REQUIRED]",
                        "[CHALLENGE_REQUIRED]",
                    )
                ):
                    if "TRANSLATOR_MAIN_STREAM_BUSY" not in str(exc):
                        self._close_translator_target()
                    return ""
                if attempt == 0 and (self._trans_cdp is not None or self._trans_target_id):
                    self._close_translator_target()
                    continue
                return ""
            except Exception:
                if attempt == 0 and (self._trans_cdp is not None or self._trans_target_id):
                    self._close_translator_target()
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
        if self.provider in ("claude", "chatgpt") and self._main_stream_active:
            self._log(f"ℹ️ [{self._provider_name_kr()} Web] 메인 대화 스트리밍 중이어서 보조 탭 작업을 건너뜁니다.")
            return ""

        if self.provider == "gemini":
            eff_model, eff_level = split_legacy_browser_model(model_code, thinking_level)
        else:
            eff_model = (model_code or get_provider_default_browser_model(self.provider)).strip()
            eff_level = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        eff_timeout = max(20.0, float(timeout_sec or 45.0))

        selectors = self._load_active_selectors()
        target_url = validate_provider_url(selectors.get("url"), provider=self.provider)
        for attempt in range(2):
            try:
                cdp = self._ensure_translator_cdp()
                with self._trans_lock:
                    if self.provider in ("claude", "chatgpt") and self._main_stream_active:
                        return ""
                    if fresh_chat or self._trans_turn_count >= 12:
                        cdp.evaluate(
                            "window.__vcStalePage = true; window.__vcModelSpec = ''; "
                            f"window.location.href = {_js_literal(target_url)};",
                            timeout=8.0,
                        )
                        wait_dl = time.monotonic() + 12.0
                        while time.monotonic() < wait_dl:
                            if self._is_aborted():
                                return ""
                            try:
                                not_stale = cdp.evaluate("window.__vcStalePage !== true", timeout=4.0)
                                if not_stale:
                                    dom_st = self._check_dom_login_state(cdp, selectors)
                                    if dom_st == "READY":
                                        break
                                    if dom_st in ("LOGIN_REQUIRED", "CHALLENGE_REQUIRED"):
                                        self._close_translator_target()
                                        return ""
                            except Exception:
                                pass
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
                if any(
                    tok in str(exc)
                    for tok in (
                        "TRANSLATOR_LOGIN_REQUIRED",
                        "TRANSLATOR_CANCELLED",
                        "TRANSLATOR_MAIN_STREAM_BUSY",
                        "[LOGIN_REQUIRED]",
                        "[CHALLENGE_REQUIRED]",
                    )
                ):
                    if "TRANSLATOR_MAIN_STREAM_BUSY" not in str(exc):
                        self._close_translator_target()
                    return ""
                if attempt == 0 and (self._trans_cdp is not None or self._trans_target_id):
                    self._close_translator_target()
                    continue
                return ""
            except Exception:
                if attempt == 0 and (self._trans_cdp is not None or self._trans_target_id):
                    self._close_translator_target()
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
        persona_refresh_block: str = "",
        persona_anchor_fallback: str = "",
        is_prime_turn: bool = False,
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
        study_visual_cb: Callable[[dict[str, Any]], None] | None = None,
        enable_study_visual_filter: bool = False,
    ) -> str:
        """
        Inject `text` via CDP Input.insertText (no innerHTML / TrustedHTML violation),
        submit via selectors.json, and stream newly added sentences via DOM MutationObserver.
        """
        if cancel_event is not None and cancel_event.is_set():
            return ""
        with self._io_lock:
            self._abort_event.clear()
            if self._is_aborted(cancel_event):
                return ""
            if browser_model is not None or thinking_level is not None:
                m_in = browser_model if browser_model is not None else self.selected_model_code
                l_in = thinking_level if thinking_level is not None else self.selected_thinking_level
                if self.provider == "gemini":
                    self.selected_model_code, self.selected_thinking_level = split_legacy_browser_model(m_in, l_in)
                else:
                    self.selected_model_code = (m_in or get_provider_default_browser_model(self.provider)).strip()
                    self.selected_thinking_level = l_in if l_in in ("low", "medium", "high", "xhigh") else "low"
            if self._cdp is None or not self.is_running() or self.login_mode_active:
                ok, reason = self.ensure_ready_and_check_login(
                    show_if_ready=self.window_visible,
                    cancel_event=cancel_event,
                    _clear_abort=False,
                )
                if not ok:
                    if reason == "CANCELLED":
                        return ""
                    if reason == "CHALLENGE_REQUIRED":
                        raise RuntimeError(
                            f"[CHALLENGE_REQUIRED] {self._provider_name_kr()} 보안 확인(Cloudflare)이 필요합니다. 열린 브라우저 창에서 확인을 완료해 주세요."
                        )
                    if reason == "LOGIN_REQUIRED":
                        raise RuntimeError(
                            f"[LOGIN_REQUIRED] {self._provider_name_kr()} 로그인이 필요합니다. 열린 브라우저 창에서 로그인을 완료해 주세요."
                        )
                    raise RuntimeError(f"{self._provider_name_kr()} 브라우저가 준비되지 않았습니다 ({reason})")

            assert self._cdp is not None
            selectors = self._load_active_selectors()
            self._select_web_model_on_cdp(self._cdp, self.selected_model_code, self.selected_thinking_level)
            prompt_timeout = 120.0 if self.selected_thinking_level in ("high", "xhigh") else 95.0

            next_turn = 1 if is_prime_turn else (self._thread_turn_count + 1)
            effective_text = text
            if (not is_prime_turn) and next_turn >= 15 and (next_turn % 15 == 0):
                refresh_txt = (persona_refresh_block or "").strip()
                if not refresh_txt and (persona_anchor_fallback or "").strip():
                    refresh_txt = f"[장기 대화 페르소나 재고정 — {next_turn}턴]\n{persona_anchor_fallback.strip()}"
                if refresh_txt and refresh_txt not in effective_text:
                    effective_text = f"{refresh_txt}\n\n{effective_text}"

            eff_img_bytes, eff_img_mime = (
                _normalize_vision_image(image_bytes, image_mime)
                if self.provider == "gemini"
                else (None, "image/jpeg")
            )
            cdp_img_kwargs: dict[str, Any] = {}
            if eff_img_bytes is not None:
                cdp_img_kwargs["image_bytes"] = eff_img_bytes
                cdp_img_kwargs["image_mime"] = eff_img_mime

            bilingual_active = bool(bilingual_inline and on_sentence_pair is not None)
            splitter = (
                BilingualSentenceStreamParser()
                if bilingual_active
                else IncrementalSentenceSplitter(
                    japanese_only=bool(use_japanese_lock),
                    korean_only=bool(use_korean_lock and not use_japanese_lock),
                )
            )
            visual_filter = _create_study_visual_filter(bool(enable_study_visual_filter))
            emitted_parts: list[str] = []
            parsed_visuals: list[dict[str, Any]] = []
            is_first = True
            last_raw_text = ""

            def _feed_splitter_delta(chunk: str) -> None:
                nonlocal is_first
                if not chunk:
                    return
                if bilingual_active:
                    assert isinstance(splitter, BilingualSentenceStreamParser)
                    assert on_sentence_pair is not None
                    for jp_sent, kr_fut in splitter.feed_delta(chunk):
                        on_sentence_pair(jp_sent, kr_fut, is_first)
                        emitted_parts.append(jp_sent)
                        is_first = False
                else:
                    assert isinstance(splitter, IncrementalSentenceSplitter)
                    for sent in splitter.feed_delta(chunk):
                        on_sentence(sent, is_first)
                        emitted_parts.append(sent)
                        is_first = False

            def _handle_progress(cur_text: str) -> None:
                nonlocal is_first, last_raw_text
                if cur_text:
                    last_raw_text = cur_text
                visible_text = (
                    visual_filter.feed_full(cur_text)
                    if (visual_filter is not None and cur_text)
                    else cur_text
                )
                if not visible_text:
                    return
                if bilingual_active:
                    assert isinstance(splitter, BilingualSentenceStreamParser)
                    assert on_sentence_pair is not None
                    for jp_sent, kr_fut in splitter.feed(visible_text):
                        on_sentence_pair(jp_sent, kr_fut, is_first)
                        emitted_parts.append(jp_sent)
                        is_first = False
                else:
                    assert isinstance(splitter, IncrementalSentenceSplitter)
                    for sent in splitter.feed(visible_text):
                        on_sentence(sent, is_first)
                        emitted_parts.append(sent)
                        is_first = False

            def _finalize_visual_filter(raw_final_text: str) -> list[dict[str, Any]]:
                if visual_filter is None:
                    return []
                if raw_final_text and raw_final_text != last_raw_text:
                    _handle_progress(raw_final_text)
                try:
                    tail_safe, visuals = visual_filter.finalize()
                except Exception:
                    tail_safe, visuals = "", []
                if tail_safe:
                    if splitter._last_seen_full and tail_safe.startswith(splitter._last_seen_full):
                        extra_delta = tail_safe[len(splitter._last_seen_full) :]
                        splitter._last_seen_full = tail_safe
                    else:
                        extra_delta = tail_safe
                    if extra_delta:
                        _feed_splitter_delta(extra_delta)
                if (not visuals) and raw_final_text and "[study_visual]" in raw_final_text.lower():
                    try:
                        from src.screen_vision.prompts import strip_study_visual_blocks

                        _, fallback_visuals = strip_study_visual_blocks(raw_final_text)
                        if fallback_visuals:
                            visuals = list(fallback_visuals)
                    except Exception:
                        pass
                return list(visuals or [])

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

            def _is_non_reconnectable_browser_error(exc: Exception) -> bool:
                msg = str(exc or "")
                if "UNTRUSTED_ORIGIN" in msg and not emitted_parts:
                    return False
                if any(
                    tok in msg
                    for tok in (
                        "[CHALLENGE_REQUIRED]",
                        "[LOGIN_REQUIRED]",
                        "[PROVIDER_ERROR]",
                        "[RESPONSE_TIMEOUT]",
                        "[BROWSER_PROFILE_LOCKED]",
                        "입력창을 찾지 못했습니다",
                    )
                ):
                    return True
                return bool(emitted_parts)

            self._main_stream_active = True
            try:
                try:
                    raw_out = self._run_cdp_prompt(
                        self._cdp,
                        selectors,
                        effective_text,
                        timeout_sec=prompt_timeout,
                        cancel_event=cancel_event,
                        on_progress=_handle_progress,
                        **cdp_img_kwargs,
                    )
                except Exception as first_exc:
                    if isinstance(first_exc, (RuntimeError, TimeoutError)) and _is_non_reconnectable_browser_error(
                        first_exc
                    ):
                        raise
                    if self._is_aborted(cancel_event):
                        return ""
                    ok, reason = self.ensure_ready_and_check_login(
                        show_if_ready=self.window_visible,
                        cancel_event=cancel_event,
                        _clear_abort=False,
                    )
                    if not ok:
                        if reason == "CANCELLED":
                            return ""
                        if reason == "CHALLENGE_REQUIRED":
                            raise RuntimeError(
                                f"[CHALLENGE_REQUIRED] {self._provider_name_kr()} 보안 확인(Cloudflare)이 필요합니다. 열린 브라우저 창에서 확인을 완료해 주세요."
                            )
                        if reason == "LOGIN_REQUIRED":
                            raise RuntimeError(
                                f"[LOGIN_REQUIRED] {self._provider_name_kr()} 로그인이 필요합니다. 열린 브라우저 창에서 로그인을 완료해 주세요."
                            )
                        raise RuntimeError(f"{self._provider_name_kr()} 브라우저 재연결 실패 ({reason})")
                    assert self._cdp is not None
                    splitter.reset()
                    if visual_filter is not None:
                        visual_filter.reset()
                    emitted_parts.clear()
                    parsed_visuals.clear()
                    is_first = True
                    last_raw_text = ""
                    self._select_web_model_on_cdp(self._cdp, self.selected_model_code, self.selected_thinking_level)
                    raw_out = self._run_cdp_prompt(
                        self._cdp,
                        selectors,
                        effective_text,
                        timeout_sec=prompt_timeout,
                        cancel_event=cancel_event,
                        on_progress=_handle_progress,
                        **cdp_img_kwargs,
                    )

                if self._is_aborted(cancel_event):
                    splitter.reset()
                    if visual_filter is not None:
                        visual_filter.reset()
                    return ""

                raw_unstripped = raw_out or last_raw_text or ""
                raw_text = raw_unstripped.strip()
                parsed_visuals = _finalize_visual_filter(raw_unstripped)
                _flush_splitter()
                emitted_text = " ".join(emitted_parts).strip()
                clean_raw_text = _strip_study_visual_for_storage(raw_text)
                if clean_raw_text or emitted_text or parsed_visuals:
                    self._conversation_primed = True
                    self._thread_turn_count = next_turn

                ko_lock_eff = bool(use_korean_lock and not use_japanese_lock)
                # 1-shot self-healing retry if locked Japanese/Korean turn drifted into the wrong language
                if (
                    (not self._is_aborted(cancel_event))
                    and _needs_japanese_repair_retry(
                        clean_raw_text,
                        emitted_text,
                        use_japanese_lock=use_japanese_lock,
                        bilingual_active=bilingual_active,
                    )
                ):
                    self._log(
                        f"[{self._provider_name_kr()} Web] 일본어 출력 이탈 감지 → 1회 자동 교정 재전송 (raw={clean_raw_text[:60]!r})"
                    )
                    splitter.reset()
                    if visual_filter is not None:
                        visual_filter.reset()
                    emitted_parts.clear()
                    parsed_visuals.clear()
                    is_first = True
                    last_raw_text = ""
                    repair_prompt = _build_japanese_repair_prompt(
                        bad_reply=clean_raw_text,
                        bilingual_inline=bilingual_active,
                    )
                    repair_raw = self._run_cdp_prompt(
                        self._cdp,
                        selectors,
                        repair_prompt,
                        timeout_sec=prompt_timeout,
                        cancel_event=cancel_event,
                        on_progress=_handle_progress,
                    )
                    if self._is_aborted(cancel_event):
                        splitter.reset()
                        if visual_filter is not None:
                            visual_filter.reset()
                        return ""
                    raw_unstripped = repair_raw or last_raw_text or ""
                    raw_text = raw_unstripped.strip()
                    parsed_visuals = _finalize_visual_filter(raw_unstripped)
                    _flush_splitter()
                    emitted_text = " ".join(emitted_parts).strip()
                    clean_raw_text = _strip_study_visual_for_storage(raw_text)
                    if emitted_parts or clean_raw_text or parsed_visuals:
                        self._conversation_primed = True
                        self._thread_turn_count = next_turn
                elif (
                    (not self._is_aborted(cancel_event))
                    and _needs_korean_repair_retry(
                        clean_raw_text,
                        emitted_text,
                        use_korean_lock=ko_lock_eff,
                    )
                ):
                    self._log(
                        f"[{self._provider_name_kr()} Web] 한국어 출력 이탈 감지 → 1회 자동 교정 재전송 (raw={clean_raw_text[:60]!r})"
                    )
                    splitter.reset()
                    if visual_filter is not None:
                        visual_filter.reset()
                    emitted_parts.clear()
                    parsed_visuals.clear()
                    is_first = True
                    last_raw_text = ""
                    repair_prompt = _build_korean_repair_prompt(bad_reply=clean_raw_text)
                    repair_raw = self._run_cdp_prompt(
                        self._cdp,
                        selectors,
                        repair_prompt,
                        timeout_sec=prompt_timeout,
                        cancel_event=cancel_event,
                        on_progress=_handle_progress,
                    )
                    if self._is_aborted(cancel_event):
                        splitter.reset()
                        if visual_filter is not None:
                            visual_filter.reset()
                        return ""
                    raw_unstripped = repair_raw or last_raw_text or ""
                    raw_text = raw_unstripped.strip()
                    parsed_visuals = _finalize_visual_filter(raw_unstripped)
                    _flush_splitter()
                    emitted_text = " ".join(emitted_parts).strip()
                    clean_raw_text = _strip_study_visual_for_storage(raw_text)
                    if emitted_parts or clean_raw_text or parsed_visuals:
                        self._conversation_primed = True
                        self._thread_turn_count = next_turn

                failed_lang_lock = (
                    use_japanese_lock
                    and _needs_japanese_repair_retry(
                        clean_raw_text,
                        emitted_text,
                        use_japanese_lock=True,
                        bilingual_active=bilingual_active,
                    )
                ) or (
                    ko_lock_eff
                    and _needs_korean_repair_retry(
                        clean_raw_text,
                        emitted_text,
                        use_korean_lock=True,
                    )
                )
                if (not self._is_aborted(cancel_event)) and (not failed_lang_lock) and parsed_visuals:
                    _dispatch_study_visuals(study_visual_cb, parsed_visuals, log_fn=self._log)

                return " ".join(emitted_parts).strip()
            finally:
                self._main_stream_active = False
                # Guarantee any open BilingualSentenceStreamParser Future is resolved on error/abort
                splitter.reset()
                if visual_filter is not None:
                    visual_filter.reset()

    def close(self) -> None:
        self.abort_active_prompt()
        with self._io_lock:
            self._terminate_proc(graceful=True)
            self._abort_event.clear()


class OfficialGeminiApiBackend:
    """
    Mode A: Official Google Gemini REST SSE streaming (ultra-low ~0.6s TTFA, zero browser RAM).
    """

    _AVAILABLE_MODELS_TTL_SEC = 600.0
    _MAX_FALLBACK_TRIES = 4
    _BAD_KEY_TOKENS: tuple[str, ...] = (
        "api_key_invalid",
        "api key not valid",
        "invalid api key",
        "api_key_expired",
        "api key expired",
        "api_key_revoked",
        "api key revoked",
        "api_key_not_found",
        "api key not found",
    )

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._own_client = http_client is None
        self._client_obj: httpx.Client | None = http_client
        self._client_lock = threading.Lock()
        self.log_cb = log_cb or (lambda _m: None)
        self._history: list[dict[str, Any]] = []
        self._system_prompt = ""
        self._chat_epoch = 0
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._available_models_cache: dict[str, tuple[float, list[str]]] = {}
        self._model_fallback_map: dict[tuple[str, str], str] = {}

    def _log(self, msg: str, extra_secrets: tuple[str, ...] = ()) -> None:
        try:
            self.log_cb(_scrub_secrets(msg, extra_secrets=extra_secrets))
        except Exception:
            pass

    @staticmethod
    def _key_hash(api_key: str) -> str:
        """Compute non-reversible 16-hex key fingerprint (never stores raw API keys in caches)."""
        return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _is_bad_api_key_error(cls, err_body: str = "") -> bool:
        low = (err_body or "").lower()
        return any(tok in low for tok in cls._BAD_KEY_TOKENS)

    @classmethod
    def _is_model_recoverable_error(cls, status_code: int, err_body: str = "") -> bool:
        """
        Return True when an API error indicates a model-specific unavailability/tier/quota problem
        that can safely fall back to another free-tier text model, while NEVER masking bad API keys.
        """
        if cls._is_bad_api_key_error(err_body):
            return False
        if status_code == 401:
            return False
        if status_code in (404, 429, 403):
            return True
        if status_code == 400:
            body_low = (err_body or "").lower()
            model_err_hints = (
                "not supported for generatecontent",
                "model is not supported",
                "unsupported model",
                "invalid model",
                "unknown model",
                "model not found",
                "deprecated model",
                "no longer available",
            )
            if any(tok in body_low for tok in model_err_hints):
                return True
            if "model" in body_low and any(
                tok in body_low
                for tok in (
                    "not found",
                    "not supported",
                    "unsupported",
                    "invalid",
                    "unknown",
                    "deprecated",
                    "unavailable",
                    "does not exist",
                    "not available",
                    "not enabled",
                    "billing",
                    "paid",
                )
            ):
                return True
        return False

    @staticmethod
    def _read_bounded_err_body(resp: Any, secret_to_scrub: str = "") -> str:
        raw_bytes = b""
        iter_bytes = getattr(resp, "iter_bytes", None)
        if callable(iter_bytes):
            buf = bytearray()
            try:
                for chunk in iter_bytes():
                    if not chunk:
                        continue
                    need = _MAX_GEMINI_API_ERROR_BYTES - len(buf)
                    if need <= 0:
                        break
                    buf.extend(chunk[:need])
                    if len(buf) >= _MAX_GEMINI_API_ERROR_BYTES:
                        break
                raw_bytes = bytes(buf)
            except Exception:
                raw_bytes = b""
        if not raw_bytes:
            raw_content = getattr(resp, "content", None)
            if isinstance(raw_content, (bytes, bytearray)):
                raw_bytes = bytes(raw_content[:_MAX_GEMINI_API_ERROR_BYTES])
            else:
                read_fn = getattr(resp, "read", None)
                if callable(read_fn):
                    try:
                        raw_bytes = bytes(read_fn() or b"")[:_MAX_GEMINI_API_ERROR_BYTES]
                    except Exception:
                        raw_bytes = b""
        if not raw_bytes:
            raw_text = getattr(resp, "text", None)
            if isinstance(raw_text, str) and raw_text:
                raw_bytes = raw_text.encode("utf-8", errors="replace")[:_MAX_GEMINI_API_ERROR_BYTES]
            else:
                json_fn = getattr(resp, "json", None)
                if callable(json_fn):
                    try:
                        obj = json_fn()
                        if obj is not None:
                            raw_bytes = json.dumps(obj, ensure_ascii=False).encode("utf-8", errors="replace")[
                                :_MAX_GEMINI_API_ERROR_BYTES
                            ]
                    except Exception:
                        raw_bytes = b""
        return _scrub_secrets(raw_bytes.decode("utf-8", errors="replace"), extra_secrets=(secret_to_scrub,))

    @staticmethod
    def _sleep_cancellable(seconds: float, cancel_event: threading.Event | None = None) -> bool:
        """Sleep up to `seconds`, returning True if interrupted by `cancel_event`."""
        if cancel_event is None:
            time.sleep(seconds)
            return False
        return cancel_event.wait(timeout=max(0.0, seconds))

    @property
    def _client(self) -> httpx.Client:
        if self._client_obj is not None:
            return self._client_obj
        with self._client_lock:
            if self._client_obj is None:
                self._client_obj = httpx.Client(
                    verify=True,
                    follow_redirects=False,
                    timeout=httpx.Timeout(75.0, connect=8.0, read=65.0, write=10.0, pool=5.0),
                    limits=httpx.Limits(max_keepalive_connections=6, max_connections=12, keepalive_expiry=60.0),
                )
            return self._client_obj

    @_client.setter
    def _client(self, client: httpx.Client | None) -> None:
        self._client_obj = client

    def _request_json_with_error(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 15.0,
        max_bytes: int = _MAX_GEMINI_API_JSON_BYTES,
        secret_to_scrub: str = "",
    ) -> tuple[int, Any, str]:
        """Execute an HTTPS-only Gemini API JSON request returning `(status_code, data, scrubbed_error_body)`."""
        safe_url = _validate_https_url(url, _ALLOWED_GEMINI_API_HOSTS)
        client = self._client
        try:
            if isinstance(client, httpx.Client):
                with client.stream(
                    method,
                    safe_url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=timeout,
                ) as resp:
                    if resp.status_code != 200:
                        return resp.status_code, None, self._read_bounded_err_body(resp, secret_to_scrub=secret_to_scrub)
                    cl_raw = resp.headers.get("content-length")
                    if cl_raw is not None:
                        try:
                            if int(cl_raw) > max_bytes:
                                raise RuntimeError("Gemini API response exceeds safety size limit")
                        except ValueError:
                            pass
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise RuntimeError("Gemini API response exceeds safety size limit")
                    try:
                        return 200, json.loads(buf.decode("utf-8", errors="replace")), ""
                    except (json.JSONDecodeError, ValueError):
                        raise RuntimeError("Gemini API 응답 JSON 파싱 오류") from None

            if method.upper() == "GET":
                resp = client.get(safe_url, params=params, headers=headers, timeout=timeout)
            else:
                resp = client.post(safe_url, headers=headers, json=json_body, timeout=timeout)
            if resp.status_code != 200:
                return resp.status_code, None, self._read_bounded_err_body(resp, secret_to_scrub=secret_to_scrub)
            raw_content = getattr(resp, "content", None)
            if isinstance(raw_content, (bytes, bytearray)) and len(raw_content) > max_bytes:
                raise RuntimeError("Gemini API response exceeds safety size limit")
            try:
                return 200, resp.json(), ""
            except (json.JSONDecodeError, ValueError):
                raise RuntimeError("Gemini API 응답 JSON 파싱 오류") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(_scrub_secrets(f"Gemini API 네트워크 오류: {exc}", extra_secrets=(secret_to_scrub,))) from None

    def _request_json_bounded(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 15.0,
        max_bytes: int = _MAX_GEMINI_API_JSON_BYTES,
        secret_to_scrub: str = "",
    ) -> tuple[int, Any]:
        """Execute an HTTPS-only Gemini API JSON request with bounded response size and scrubbed errors."""
        status_code, data, _ = self._request_json_with_error(
            method,
            url,
            headers=headers,
            params=params,
            json_body=json_body,
            timeout=timeout,
            max_bytes=max_bytes,
            secret_to_scrub=secret_to_scrub,
        )
        return status_code, data

    def start_new_chat(self, system_prompt: str = "") -> None:
        with self._lock:
            self._history = []
            self._system_prompt = system_prompt
            self._chat_epoch += 1

    def update_system_prompt(self, system_prompt: str) -> None:
        """Update system instruction cleanly without resetting multi-turn conversation history."""
        with self._lock:
            self._system_prompt = system_prompt

    @staticmethod
    def _extract_non_thought_text(parts: list[Any]) -> str:
        """Join visible answer text while stripping hidden thinking (`thought=True`) parts."""
        if not isinstance(parts, list):
            return ""
        return "".join(
            str(p.get("text") or "")
            for p in parts
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

    @staticmethod
    def _filter_chat_gemini_models(models: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in models or []:
            name = str(raw or "").strip()
            if name.lower().startswith("models/"):
                name = name[7:].strip()
            low = name.lower()
            if not name or name in seen or not low.startswith("gemini-"):
                continue
            if any(tok in low for tok in _GEMINI_NON_CHAT_TOKENS):
                continue
            if not _RE_SAFE_GEMINI_MODEL.fullmatch(name) or ".." in name:
                continue
            seen.add(name)
            out.append(name)
        return sorted(out)

    def list_generate_models(self, api_key: str, timeout_sec: float = 10.0) -> list[str]:
        """Fetch all `generateContent`-capable Gemini text model IDs from the official API."""
        if not str(api_key or "").strip():
            return []
        clean_key = _validate_api_key_header(api_key, "Gemini")
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        status_code, data, err_body = self._request_json_with_error(
            "GET",
            url,
            headers={"x-goog-api-key": clean_key},
            params={"pageSize": 200},
            timeout=timeout_sec,
            secret_to_scrub=clean_key,
        )
        if status_code != 200:
            err = RuntimeError(f"Gemini 모델 목록 조회 실패 HTTP {status_code}")
            setattr(err, "http_status", status_code)
            setattr(err, "is_auth_error", status_code in (401, 403) or self._is_bad_api_key_error(err_body))
            raise err
        raw_models = (data if isinstance(data, dict) else {}).get("models")
        models = raw_models if isinstance(raw_models, list) else []
        raw_names: list[str] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            methods = m.get("supportedGenerationMethods")
            if not isinstance(methods, list) or "generateContent" not in methods:
                continue
            raw_names.append(str(m.get("name") or ""))
        result = self._filter_chat_gemini_models(raw_names)
        with self._cache_lock:
            self._available_models_cache[self._key_hash(clean_key)] = (time.monotonic(), list(result))
        return result

    def _build_free_fallback_candidates(
        self,
        api_key: str,
        failed_model: str,
        exclude: set[str] | None = None,
        max_candidates: int = _MAX_FALLBACK_TRIES,
    ) -> list[str]:
        """
        Build prioritized free-tier-friendly fallback candidates for `api_key`,
        using cached `list_generate_models()` (10-min TTL) and falling back to static
        `GEMINI_FREE_FALLBACK_MODELS` only on transient network errors (never on 401/403).
        """
        clean_key = (api_key or "").strip()
        if not clean_key:
            return []
        cap = max(1, int(max_candidates or self._MAX_FALLBACK_TRIES))
        failed_clean = (failed_model or "").strip()
        if failed_clean.lower().startswith("models/"):
            failed_clean = failed_clean[7:].strip()
        excluded: set[str] = set()
        if failed_clean:
            excluded.add(failed_clean)
        for item in exclude or ():
            c = str(item or "").strip()
            if c.lower().startswith("models/"):
                c = c[7:].strip()
            if c:
                excluded.add(c)

        key_hash = self._key_hash(clean_key)
        now = time.monotonic()
        available: list[str] | None = None
        with self._cache_lock:
            cached = self._available_models_cache.get(key_hash)
            if cached is not None and (now - cached[0]) < self._AVAILABLE_MODELS_TTL_SEC:
                available = list(cached[1])

        if available is None:
            try:
                fetched = self.list_generate_models(clean_key, timeout_sec=8.0)
                available = self._filter_chat_gemini_models(fetched)
                with self._cache_lock:
                    self._available_models_cache[key_hash] = (time.monotonic(), list(available))
            except Exception as exc:
                err_txt = str(exc or "")
                if (
                    getattr(exc, "is_auth_error", False)
                    or getattr(exc, "http_status", 0) in (401, 403)
                    or re.search(r"\b(?:401|403)\b", err_txt)
                    or self._is_bad_api_key_error(err_txt)
                ):
                    return []
                return [m for m in GEMINI_FREE_FALLBACK_MODELS if m not in excluded][:cap]

        clean_avail = [m for m in self._filter_chat_gemini_models(available) if m not in excluded]
        if not clean_avail:
            return []
        avail_set = set(clean_avail)
        ordered: list[str] = []
        added: set[str] = set()

        for cand in GEMINI_FREE_FALLBACK_MODELS:
            if cand in avail_set and cand not in added:
                ordered.append(cand)
                added.add(cand)
        for cand in sorted([m for m in clean_avail if "flash-lite" in m.lower() and m not in added], reverse=True):
            ordered.append(cand)
            added.add(cand)
        for cand in sorted([m for m in clean_avail if "flash" in m.lower() and m not in added], reverse=True):
            ordered.append(cand)
            added.add(cand)
        for cand in sorted([m for m in clean_avail if m not in added], reverse=True):
            ordered.append(cand)
            added.add(cand)
        return ordered[:cap]

    def _post_generate_content_with_fallback(
        self,
        *,
        clean_key: str,
        requested_model: str,
        prompt_text: str,
        base_gen: dict[str, Any],
        thinking_level: str,
        extra_level_budget: int,
        timeout_sec: float,
    ) -> dict[str, Any] | None:
        """Execute a one-shot Gemini generateContent POST with per-key fallback caching."""
        key_hash = self._key_hash(clean_key)
        with self._cache_lock:
            primary_model = self._model_fallback_map.get((key_hash, requested_model), requested_model)

        def _try_one(cand_model: str) -> tuple[int, Any, str]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{cand_model}:generateContent"
            gen_cfg = self._build_gen_config_with_thinking(
                base_gen,
                cand_model,
                thinking_level,
                extra_level_budget=extra_level_budget,
            )
            payload: dict[str, Any] = {
                "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
                "generationConfig": gen_cfg,
            }
            status_code, data, err_body = self._request_json_with_error(
                "POST",
                url,
                headers={"x-goog-api-key": clean_key, "Content-Type": "application/json"},
                json_body=payload,
                timeout=timeout_sec,
                secret_to_scrub=clean_key,
            )
            if status_code == 400 and "thinkingConfig" in gen_cfg and not self._is_bad_api_key_error(err_body):
                payload["generationConfig"] = dict(base_gen)
                status_code, data, err_body = self._request_json_with_error(
                    "POST",
                    url,
                    headers={"x-goog-api-key": clean_key, "Content-Type": "application/json"},
                    json_body=payload,
                    timeout=timeout_sec,
                    secret_to_scrub=clean_key,
                )
            return status_code, data, err_body

        status_code, data, err_body = _try_one(primary_model)
        if status_code == 200 and isinstance(data, dict):
            return data

        if self._is_model_recoverable_error(status_code, err_body):
            if primary_model != requested_model:
                with self._cache_lock:
                    if self._model_fallback_map.get((key_hash, requested_model)) == primary_model:
                        self._model_fallback_map.pop((key_hash, requested_model), None)
            exclude_set = (
                {primary_model} if requested_model in GEMINI_FREE_FALLBACK_MODELS else {primary_model, requested_model}
            )
            cands = self._build_free_fallback_candidates(
                clean_key,
                primary_model,
                exclude=exclude_set,
                max_candidates=self._MAX_FALLBACK_TRIES,
            )
            for cand_model in cands[: self._MAX_FALLBACK_TRIES]:
                c_status, c_data, c_err = _try_one(cand_model)
                if c_status == 200 and isinstance(c_data, dict):
                    with self._cache_lock:
                        self._model_fallback_map[(key_hash, requested_model)] = cand_model
                    self._log(
                        f"🔄 Gemini API 모델 자동 전환: {primary_model} → {cand_model} "
                        f"(HTTP {status_code}, 무료/계정 호환 자동 폴백)",
                        extra_secrets=(clean_key,),
                    )
                    return c_data
                if not self._is_model_recoverable_error(c_status, c_err):
                    break
        return None

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
        clean_jp = str(jp_text or "").strip()
        if not clean_jp:
            return ""
        try:
            clean_key = _validate_api_key_header(api_key, "Gemini")
            clean_model = _validate_gemini_model_id(model)
        except (RuntimeError, ValueError):
            return ""
        eff_timeout = float(timeout_sec) if timeout_sec is not None else {"low": 15.0, "medium": 24.0, "high": 40.0, "xhigh": 55.0}.get(thinking_level, 18.0)
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
        try:
            data = self._post_generate_content_with_fallback(
                clean_key=clean_key,
                requested_model=clean_model,
                prompt_text=prompt,
                base_gen=base_gen,
                thinking_level=thinking_level,
                extra_level_budget=800,
                timeout_sec=eff_timeout,
            )
            if not isinstance(data, dict):
                return ""
            cands = data.get("candidates")
            if not isinstance(cands, list) or not cands or not isinstance(cands[0], dict):
                return ""
            content = cands[0].get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            raw = self._extract_non_thought_text(parts if isinstance(parts, list) else [])
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
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            return ""
        try:
            clean_key = _validate_api_key_header(api_key, "Gemini")
            clean_model = _validate_gemini_model_id(model)
        except (RuntimeError, ValueError):
            return ""
        base_gen = {"temperature": 0.2, "maxOutputTokens": max(300, int(max_output_tokens))}
        eff_timeout = max(15.0, float(timeout_sec or 40.0))
        try:
            data = self._post_generate_content_with_fallback(
                clean_key=clean_key,
                requested_model=clean_model,
                prompt_text=clean_prompt,
                base_gen=base_gen,
                thinking_level=thinking_level,
                extra_level_budget=1024,
                timeout_sec=eff_timeout,
            )
            if not isinstance(data, dict):
                return ""
            cands = data.get("candidates")
            if not isinstance(cands, list) or not cands or not isinstance(cands[0], dict):
                return ""
            content = cands[0].get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            return self._extract_non_thought_text(parts if isinstance(parts, list) else []).strip()
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
        temperature: float = 0.84,
        history_user_text: str | None = None,
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
        study_visual_cb: Callable[[dict[str, Any]], None] | None = None,
        enable_study_visual_filter: bool = False,
    ) -> str:
        if cancel_event is not None and cancel_event.is_set():
            return ""
        clean_key = _validate_api_key_header(api_key, "Gemini")
        if not str(text or "").strip() and not image_bytes:
            return ""
        requested_model = _validate_gemini_model_id(model)
        key_hash = self._key_hash(clean_key)

        with self._lock:
            if system_prompt:
                self._system_prompt = system_prompt
            prev_history = list(self._history)
            sys_instr = self._system_prompt
            chat_epoch = self._chat_epoch

        img_bytes, img_mime = _normalize_vision_image(image_bytes, image_mime)
        img_b64 = base64.b64encode(img_bytes).decode("ascii") if img_bytes else ""
        visual_filter = _create_study_visual_filter(bool(enable_study_visual_filter))

        bilingual_active = bool(bilingual_inline and on_sentence_pair is not None)
        eff_temp = round(_coerce_finite_float(temperature, 0.84, low=0.1, high=1.25), 2)
        lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        read_to = {"low": 65.0, "medium": 85.0, "high": 115.0, "xhigh": 140.0}.get(lvl, 65.0)
        if enable_study_visual_filter:
            max_out_tokens = 2800 if bilingual_active else 2400
        else:
            max_out_tokens = 1500 if bilingual_active else 950
        base_gen = {
            "temperature": eff_temp,
            "maxOutputTokens": max_out_tokens,
        }

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

        def _feed_splitter_delta(chunk: str) -> None:
            nonlocal is_first
            if not chunk:
                return
            if bilingual_active:
                assert isinstance(splitter, BilingualSentenceStreamParser)
                assert on_sentence_pair is not None
                for jp_sent, kr_fut in splitter.feed_delta(chunk):
                    on_sentence_pair(jp_sent, kr_fut, is_first)
                    emitted_parts.append(jp_sent)
                    is_first = False
            else:
                assert isinstance(splitter, IncrementalSentenceSplitter)
                for sent in splitter.feed_delta(chunk):
                    on_sentence(sent, is_first)
                    emitted_parts.append(sent)
                    is_first = False

        def _make_payload(req_user_text: str, active_gen_cfg: dict[str, Any]) -> dict[str, Any]:
            # Slice even pairs (`prev_history[-22:]`) and drop any orphaned leading non-user turn so Turn 13+ starts with `user`
            tail = [m for m in prev_history[-22:] if isinstance(m, dict) and m.get("parts")]
            while tail and str(tail[0].get("role") or "").lower() != "user":
                tail.pop(0)
            user_parts: list[dict[str, Any]] = []
            if img_b64:
                user_parts.append({"inlineData": {"mimeType": img_mime, "data": img_b64}})
            user_parts.append({"text": req_user_text})
            contents = tail + [{"role": "user", "parts": user_parts}]
            req: dict[str, Any] = {
                "contents": contents,
                "generationConfig": active_gen_cfg,
            }
            if sys_instr:
                req["system_instruction"] = {"parts": [{"text": sys_instr}]}
            return req

        def _consume_sse(req_url: str, req_payload: dict[str, Any]) -> tuple[int, str]:
            nonlocal is_first
            stream_kwargs: dict[str, Any] = {
                "headers": {"x-goog-api-key": clean_key, "Content-Type": "application/json"},
                "json": req_payload,
            }
            if isinstance(self._client, httpx.Client):
                stream_kwargs["timeout"] = httpx.Timeout(read_to + 15.0, connect=8.0, read=read_to, write=10.0, pool=5.0)
            try:
                with self._client.stream(
                    "POST",
                    req_url,
                    **stream_kwargs,
                ) as resp:
                    if resp.status_code != 200:
                        err_body = self._read_bounded_err_body(resp, secret_to_scrub=clean_key)
                        return resp.status_code, err_body

                    total_chars = 0
                    for line in resp.iter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        if not line:
                            continue
                        if len(line) > _MAX_GEMINI_SSE_LINE_CHARS:
                            raise RuntimeError("Gemini API SSE line exceeds safety size limit")
                        total_chars += len(line)
                        if total_chars > _MAX_GEMINI_SSE_TOTAL_CHARS:
                            raise RuntimeError("Gemini API SSE response exceeds safety size limit")
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            obj = json.loads(data_str)
                        except Exception:
                            continue
                        if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
                            err_obj = obj["error"]
                            err_code_raw = err_obj.get("code")
                            try:
                                err_status = int(err_code_raw) if err_code_raw is not None else 500
                            except (TypeError, ValueError):
                                err_status = 500
                            if not (400 <= err_status <= 599):
                                err_status = 500
                            err_msg = _scrub_secrets(
                                str(err_obj.get("message") or data_str),
                                extra_secrets=(clean_key,),
                            )
                            return err_status, err_msg
                        cands = obj.get("candidates") if isinstance(obj, dict) else None
                        if not isinstance(cands, list) or not cands or not isinstance(cands[0], dict):
                            continue
                        content = cands[0].get("content")
                        parts = content.get("parts") if isinstance(content, dict) else None
                        delta = self._extract_non_thought_text(parts if isinstance(parts, list) else [])
                        if not delta:
                            continue
                        raw_full.append(delta)
                        safe_delta = visual_filter.feed_delta(delta) if visual_filter is not None else delta
                        if safe_delta:
                            _feed_splitter_delta(safe_delta)
                    return 200, ""
            except httpx.HTTPError as exc:
                raise RuntimeError(_scrub_secrets(f"Gemini API 스트리밍 오류: {exc}", extra_secrets=(clean_key,))) from None

        def _try_model_stream(cand_model: str, req_user_text: str) -> tuple[int, str]:
            nonlocal is_first

            cand_url = _validate_https_url(
                f"https://generativelanguage.googleapis.com/v1beta/models/{cand_model}:streamGenerateContent?alt=sse",
                _ALLOWED_GEMINI_API_HOSTS,
            )
            cand_gen_cfg = self._build_gen_config_with_thinking(
                base_gen,
                cand_model,
                thinking_level,
                extra_level_budget=1400,
            )
            req_payload = _make_payload(req_user_text, cand_gen_cfg)
            status_code = 0
            err_body = ""
            for retry_i in range(3):
                splitter.reset()
                if visual_filter is not None:
                    visual_filter.reset()
                emitted_parts.clear()
                raw_full.clear()
                is_first = True
                status_code, err_body = _consume_sse(cand_url, req_payload)
                if (
                    status_code == 503
                    and retry_i < 2
                    and not emitted_parts
                    and (cancel_event is None or not cancel_event.is_set())
                ):
                    self._log(
                        f"⏳ Gemini API 일시 과부하 (HTTP 503) → 재시도 ({retry_i + 1}/2)",
                        extra_secrets=(clean_key,),
                    )
                    if self._sleep_cancellable(0.8 * (retry_i + 1), cancel_event):
                        break
                    continue
                break
            if (
                status_code == 400
                and not emitted_parts
                and "thinkingConfig" in cand_gen_cfg
                and not self._is_bad_api_key_error(err_body)
            ):
                splitter.reset()
                if visual_filter is not None:
                    visual_filter.reset()
                emitted_parts.clear()
                raw_full.clear()
                is_first = True
                req_payload["generationConfig"] = dict(base_gen)
                status_code, err_body = _consume_sse(cand_url, req_payload)
            return status_code, err_body

        def _run_attempt(req_user_text: str) -> tuple[str, str, list[dict[str, Any]]]:
            nonlocal is_first
            with self._cache_lock:
                primary_model = self._model_fallback_map.get((key_hash, requested_model), requested_model)

            status_code, err_body = _try_model_stream(primary_model, req_user_text)
            if (
                status_code != 200
                and not emitted_parts
                and self._is_model_recoverable_error(status_code, err_body)
                and (cancel_event is None or not cancel_event.is_set())
            ):
                if primary_model != requested_model:
                    with self._cache_lock:
                        if self._model_fallback_map.get((key_hash, requested_model)) == primary_model:
                            self._model_fallback_map.pop((key_hash, requested_model), None)
                first_status = status_code
                exclude_set = (
                    {primary_model}
                    if requested_model in GEMINI_FREE_FALLBACK_MODELS
                    else {primary_model, requested_model}
                )
                cands = self._build_free_fallback_candidates(
                    clean_key,
                    primary_model,
                    exclude=exclude_set,
                    max_candidates=self._MAX_FALLBACK_TRIES,
                )
                for cand_model in cands[: self._MAX_FALLBACK_TRIES]:
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    c_status, c_err = _try_model_stream(cand_model, req_user_text)
                    if c_status == 200:
                        with self._cache_lock:
                            self._model_fallback_map[(key_hash, requested_model)] = cand_model
                        self._log(
                            f"🔄 Gemini API 모델 자동 전환: {primary_model} → {cand_model} "
                            f"(HTTP {first_status}, 무료/계정 호환 모델로 자동 폴백)",
                            extra_secrets=(clean_key,),
                        )
                        status_code, err_body = 200, ""
                        break
                    status_code, err_body = c_status, c_err
                    if emitted_parts or not self._is_model_recoverable_error(c_status, c_err):
                        break

            if status_code != 200:
                safe_err = _scrub_secrets(err_body[:240], extra_secrets=(clean_key,))
                raise RuntimeError(f"Gemini API HTTP {status_code}: {safe_err}")

            if cancel_event is not None and cancel_event.is_set():
                splitter.reset()
                if visual_filter is not None:
                    visual_filter.reset()
                return "", "", []

            parsed_visuals: list[dict[str, Any]] = []
            if visual_filter is not None:
                try:
                    tail_safe, parsed_visuals = visual_filter.finalize()
                except Exception:
                    tail_safe, parsed_visuals = "", []
                if tail_safe:
                    _feed_splitter_delta(tail_safe)

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

            raw_joined = "".join(raw_full).strip()
            clean_spoken_text = _strip_study_visual_for_storage(raw_joined)
            return clean_spoken_text, " ".join(emitted_parts).strip(), list(parsed_visuals or [])

        try:
            full_model_text, emitted_text, parsed_visuals = _run_attempt(text)
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
                full_model_text, emitted_text, parsed_visuals = _run_attempt(f"{text}\n\n{repair_note}")
            elif (
                (cancel_event is None or not cancel_event.is_set())
                and _needs_korean_repair_retry(
                    full_model_text,
                    emitted_text,
                    use_korean_lock=ko_lock_eff,
                )
            ):
                repair_note = _build_korean_repair_prompt(bad_reply=full_model_text)
                full_model_text, emitted_text, parsed_visuals = _run_attempt(f"{text}\n\n{repair_note}")

            cancelled = cancel_event is not None and cancel_event.is_set()
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
            if (not failed_lang_lock) and (not cancelled):
                if parsed_visuals:
                    _dispatch_study_visuals(study_visual_cb, parsed_visuals, log_fn=self._log)
                clean_spoken_model_text = _strip_study_visual_for_storage(full_model_text)
                if clean_spoken_model_text:
                    hist_user = (
                        history_user_text
                        if history_user_text is not None
                        else _strip_turn_wrapper_for_history(text)
                    ).strip() or text
                    clean_hist_user = _strip_study_visual_for_storage(hist_user) or hist_user
                    with self._lock:
                        if self._chat_epoch == chat_epoch:
                            self._history = (
                                prev_history[-22:]
                                + [
                                    {"role": "user", "parts": [{"text": clean_hist_user}]},
                                    {"role": "model", "parts": [{"text": clean_spoken_model_text}]},
                                ]
                            )[-24:]
            return "" if cancelled else emitted_text
        finally:
            splitter.reset()
            if visual_filter is not None:
                visual_filter.reset()

    def close(self) -> None:
        with self._client_lock:
            client = self._client_obj
            if self._own_client and client is not None:
                self._client_obj = None
                try:
                    client.close()
                except Exception:
                    pass


class OfficialClaudeApiBackend:
    """
    Official Anthropic Claude Messages API SSE streaming backend (`https://api.anthropic.com/v1/messages`).
    Enforces strict HTTPS host allowlist, header-only `x-api-key` auth, bounded response sizes, and secret scrubbing.
    """

    ANTHROPIC_VERSION = "2023-06-01"
    _CLAUDE_404_FALLBACKS: dict[str, str] = {
        "claude-sonnet-5-5": "claude-sonnet-4-6",
        "claude-haiku-5-5": "claude-haiku-4-5",
        "claude-opus-5-5": "claude-opus-4-6",
        "claude-fable-5-2": "claude-opus-4-6",
    }
    _CLAUDE_404_FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
        "claude-sonnet-5-5": ("claude-sonnet-4-6", "claude-haiku-4-5"),
        "claude-haiku-5-5": ("claude-haiku-4-5", "claude-sonnet-4-6"),
        "claude-opus-5-5": ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"),
        "claude-fable-5-2": ("claude-opus-4-6", "claude-opus-5-5", "claude-sonnet-4-6", "claude-haiku-4-5"),
        "claude-opus-4-6": ("claude-sonnet-4-6", "claude-haiku-4-5"),
        "claude-sonnet-4-6": ("claude-haiku-4-5",),
    }
    _CLAUDE_DEFAULT_404_CHAIN: tuple[str, ...] = ("claude-sonnet-4-6", "claude-haiku-4-5")

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._own_client = http_client is None
        self._client_obj: httpx.Client | None = http_client
        self._client_lock = threading.Lock()
        self.log_cb = log_cb or (lambda _m: None)
        self._history: list[dict[str, Any]] = []
        self._system_prompt = ""
        self._chat_epoch = 0
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._model_fallback_map: dict[tuple[str, str], str] = {}
        self._compat_tier_cache: dict[tuple[str, str, str, bool], int] = {}

    @staticmethod
    def _key_hash(api_key: str) -> str:
        return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _get_404_fallback_candidates(cls, model: str, *, exclude: set[str] | None = None) -> list[str]:
        skip = set(exclude or ())
        skip.add(model)
        chain = list(cls._CLAUDE_404_FALLBACK_CHAINS.get(model, ()))
        single = cls._CLAUDE_404_FALLBACKS.get(model)
        if single and single not in chain:
            chain.insert(0, single)
        for d in cls._CLAUDE_DEFAULT_404_CHAIN:
            if d not in chain:
                chain.append(d)
        return [m for m in chain if m not in skip]

    def _ordered_compat_payloads(
        self,
        key_hash: str,
        model: str,
        lvl: str,
        stream: bool,
        payloads: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        indexed = list(enumerate(payloads))
        with self._cache_lock:
            cached_idx = self._compat_tier_cache.get((key_hash, model, lvl, bool(stream)))
        if cached_idx is not None and 0 <= cached_idx < len(indexed):
            return [indexed[cached_idx]] + [p for p in indexed if p[0] != cached_idx]
        return indexed

    def _log(self, msg: str, extra_secrets: tuple[str, ...] = ()) -> None:
        try:
            self.log_cb(_scrub_secrets(msg, extra_secrets=extra_secrets))
        except Exception:
            pass

    @staticmethod
    def _read_bounded_err_body(resp: Any, secret_to_scrub: str = "") -> str:
        raw_bytes = b""
        iter_bytes = getattr(resp, "iter_bytes", None)
        if callable(iter_bytes):
            buf = bytearray()
            try:
                for chunk in iter_bytes():
                    if not chunk:
                        continue
                    need = _MAX_GEMINI_API_ERROR_BYTES - len(buf)
                    if need <= 0:
                        break
                    buf.extend(chunk[:need])
                    if len(buf) >= _MAX_GEMINI_API_ERROR_BYTES:
                        break
                raw_bytes = bytes(buf)
            except Exception:
                raw_bytes = b""
        if not raw_bytes:
            raw_content = getattr(resp, "content", None)
            if isinstance(raw_content, (bytes, bytearray)):
                raw_bytes = bytes(raw_content[:_MAX_GEMINI_API_ERROR_BYTES])
            else:
                read_fn = getattr(resp, "read", None)
                if callable(read_fn):
                    try:
                        raw_bytes = bytes(read_fn() or b"")[:_MAX_GEMINI_API_ERROR_BYTES]
                    except Exception:
                        raw_bytes = b""
        if not raw_bytes:
            raw_text = getattr(resp, "text", None)
            if isinstance(raw_text, str) and raw_text:
                raw_bytes = raw_text.encode("utf-8", errors="replace")[:_MAX_GEMINI_API_ERROR_BYTES]
            else:
                json_fn = getattr(resp, "json", None)
                if callable(json_fn):
                    try:
                        obj = json_fn()
                        if obj is not None:
                            raw_bytes = json.dumps(obj, ensure_ascii=False).encode("utf-8", errors="replace")[
                                :_MAX_GEMINI_API_ERROR_BYTES
                            ]
                    except Exception:
                        raw_bytes = b""
        return _scrub_secrets(raw_bytes.decode("utf-8", errors="replace"), extra_secrets=(secret_to_scrub,))

    @property
    def _client(self) -> httpx.Client:
        if self._client_obj is not None:
            return self._client_obj
        with self._client_lock:
            if self._client_obj is None:
                self._client_obj = httpx.Client(
                    verify=True,
                    follow_redirects=False,
                    timeout=httpx.Timeout(85.0, connect=8.0, read=75.0, write=10.0, pool=5.0),
                    limits=httpx.Limits(max_keepalive_connections=6, max_connections=12, keepalive_expiry=60.0),
                )
            return self._client_obj

    @_client.setter
    def _client(self, client: httpx.Client | None) -> None:
        self._client_obj = client

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _forbids_temperature(model: str) -> bool:
        """Return True for Claude 4.7+/5.x/Fable models where Anthropic rejects `temperature` with HTTP 400."""
        m = (model or "").strip().lower()
        if not m.startswith("claude-"):
            return False
        if "fable" in m:
            return True
        mv = re.search(r"-(\d+)(?:[.-](\d{1,2}))?(?:$|-)", m)
        if mv:
            major = int(mv.group(1))
            minor = int(mv.group(2) or 0)
            if major >= 5 or (major == 4 and minor >= 7):
                return True
        return False

    @staticmethod
    def _supports_adaptive_thinking(model: str) -> bool:
        """Return True for Claude 4.6+/5.x/Fable models supporting `output_config.effort` & adaptive thinking."""
        m = (model or "").strip().lower()
        if not m.startswith("claude-"):
            return False
        if "fable" in m:
            return True
        if "haiku-4-5" in m or "haiku-4.5" in m:
            return False
        mv = re.search(r"-(\d+)(?:[.-](\d{1,2}))?(?:$|-)", m)
        if mv:
            major = int(mv.group(1))
            minor = int(mv.group(2) or 0)
            if major >= 5 or (major == 4 and minor >= 6):
                return True
        return False

    @staticmethod
    def _normalize_messages(
        prev_history: list[dict[str, Any]],
        req_user_text: str,
        max_messages: int = 24,
    ) -> list[dict[str, Any]]:
        """
        Guarantee Anthropic Messages API invariants even past Turn 13:
        non-empty content, strictly starts with `user`, strictly alternating `user`/`assistant` roles.
        """
        keep_prev = max(0, (int(max_messages) - 2) // 2 * 2)
        tail = list(prev_history[-keep_prev:]) if keep_prev > 0 else []
        cleaned: list[dict[str, Any]] = []
        for item in tail:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role not in ("user", "assistant") or not content:
                continue
            if not cleaned:
                if role != "user":
                    continue
                cleaned.append({"role": "user", "content": content})
            elif cleaned[-1]["role"] == role:
                cleaned[-1]["content"] = f"{cleaned[-1]['content']}\n{content}"
            else:
                cleaned.append({"role": role, "content": content})

        new_txt = str(req_user_text or "").strip()
        if new_txt:
            if cleaned and cleaned[-1]["role"] == "user":
                cleaned[-1]["content"] = f"{cleaned[-1]['content']}\n{new_txt}"
            else:
                cleaned.append({"role": "user", "content": new_txt})
        return cleaned

    def _request_json_with_error(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 15.0,
        max_bytes: int = _MAX_GEMINI_API_JSON_BYTES,
        secret_to_scrub: str = "",
    ) -> tuple[int, Any, str]:
        safe_url = _validate_https_url(url, _ALLOWED_CLAUDE_API_HOSTS)
        client = self._client
        try:
            if isinstance(client, httpx.Client):
                with client.stream(
                    method,
                    safe_url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=timeout,
                ) as resp:
                    if resp.status_code != 200:
                        return resp.status_code, None, self._read_bounded_err_body(resp, secret_to_scrub=secret_to_scrub)
                    cl_raw = resp.headers.get("content-length")
                    if cl_raw is not None:
                        try:
                            if int(cl_raw) > max_bytes:
                                raise RuntimeError("Claude API response exceeds safety size limit")
                        except ValueError:
                            pass
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise RuntimeError("Claude API response exceeds safety size limit")
                    try:
                        return 200, json.loads(buf.decode("utf-8", errors="replace")), ""
                    except (json.JSONDecodeError, ValueError):
                        raise RuntimeError("Claude API 응답 JSON 파싱 오류") from None

            if method.upper() == "GET":
                resp = client.get(safe_url, params=params, headers=headers, timeout=timeout)
            else:
                resp = client.post(safe_url, headers=headers, json=json_body, timeout=timeout)
            if resp.status_code != 200:
                return resp.status_code, None, self._read_bounded_err_body(resp, secret_to_scrub=secret_to_scrub)
            raw_content = getattr(resp, "content", None)
            if isinstance(raw_content, (bytes, bytearray)) and len(raw_content) > max_bytes:
                raise RuntimeError("Claude API response exceeds safety size limit")
            try:
                return 200, resp.json(), ""
            except (json.JSONDecodeError, ValueError):
                raise RuntimeError("Claude API 응답 JSON 파싱 오류") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(_scrub_secrets(f"Claude API 네트워크 오류: {exc}", extra_secrets=(secret_to_scrub,))) from None

    def _request_json_bounded(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 15.0,
        max_bytes: int = _MAX_GEMINI_API_JSON_BYTES,
        secret_to_scrub: str = "",
    ) -> tuple[int, Any]:
        status_code, data, _ = self._request_json_with_error(
            method,
            url,
            headers=headers,
            params=params,
            json_body=json_body,
            timeout=timeout,
            max_bytes=max_bytes,
            secret_to_scrub=secret_to_scrub,
        )
        return status_code, data

    def start_new_chat(self, system_prompt: str = "") -> None:
        with self._lock:
            self._history = []
            self._system_prompt = system_prompt
            self._chat_epoch += 1

    def update_system_prompt(self, system_prompt: str) -> None:
        with self._lock:
            self._system_prompt = system_prompt

    @staticmethod
    def _extract_visible_text(content_blocks: list[Any]) -> str:
        """Join visible text blocks while ignoring hidden thinking/redacted_thinking blocks."""
        if not isinstance(content_blocks, list):
            return ""
        return "".join(
            str(b.get("text") or "")
            for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )

    @classmethod
    def _apply_thinking_options(
        cls,
        payload: dict[str, Any],
        thinking_level: str,
        base_max_tokens: int,
        temperature: float,
    ) -> bool:
        """Attach model-safe Claude adaptive thinking & effort (never sends `temperature` on 4.7+/5.x or thinking)."""
        lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        model = str(payload.get("model") or "")
        forbids_temp = cls._forbids_temperature(model)
        adaptive = cls._supports_adaptive_thinking(model)
        if lvl == "low":
            if adaptive:
                payload["max_tokens"] = max(int(base_max_tokens), 1024)
                payload["output_config"] = {"effort": "low"}
                payload.pop("temperature", None)
                return True
            payload["max_tokens"] = int(base_max_tokens)
            if not forbids_temp:
                payload["temperature"] = temperature
            else:
                payload.pop("temperature", None)
            return False
        effort = {"medium": "medium", "high": "high", "xhigh": "max"}[lvl]
        extra = {"medium": 2048, "high": 4096, "xhigh": 8192}[lvl]
        payload["max_tokens"] = max(int(base_max_tokens) + extra, 3072)
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": effort}
        payload.pop("temperature", None)
        return True

    @classmethod
    def _build_compat_payloads(
        cls,
        *,
        model: str,
        messages: list[dict[str, Any]],
        system_prompt: str,
        thinking_level: str,
        base_max_tokens: int,
        adaptive_floor: int,
        temperature: float,
        stream: bool,
    ) -> list[dict[str, Any]]:
        """Build deduplicated progressive HTTP-400 compatibility payloads (NEVER re-injecting `temperature` on fallback)."""
        lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        forbids_temp = cls._forbids_temperature(model)
        adaptive = cls._supports_adaptive_thinking(model)
        eff_base = max(int(base_max_tokens), int(adaptive_floor)) if adaptive else int(base_max_tokens)

        def _base() -> dict[str, Any]:
            req: dict[str, Any] = {"model": model, "messages": messages}
            if system_prompt:
                req["system"] = system_prompt
            if stream:
                req["stream"] = True
            return req

        candidates: list[dict[str, Any]] = []
        if not adaptive:
            # Non-adaptive models (e.g. claude-haiku-4-5): NEVER send `thinking: adaptive` or `output_config`
            p0 = _base()
            p0["max_tokens"] = int(base_max_tokens)
            if not forbids_temp:
                p0["temperature"] = temperature
            candidates.append(p0)
            if "temperature" in p0:
                p1 = _base()
                p1["max_tokens"] = int(base_max_tokens)
                candidates.append(p1)
        else:
            # Tier 0: optimal payload for adaptive model & thinking_level
            p0 = _base()
            if lvl == "low":
                p0["max_tokens"] = eff_base
                p0["output_config"] = {"effort": "low"}
            else:
                effort = {"medium": "medium", "high": "high", "xhigh": "max"}[lvl]
                extra = {"medium": 2048, "high": 4096, "xhigh": 8192}[lvl]
                p0["max_tokens"] = max(eff_base + extra, 3072)
                p0["thinking"] = {"type": "adaptive"}
                p0["output_config"] = {"effort": effort}
            candidates.append(p0)

            # Tier 1: downgrade xhigh `max` -> `high` (some models reject `max` effort)
            if lvl == "xhigh":
                p1 = _base()
                p1["max_tokens"] = max(eff_base + 4096, 3072)
                p1["thinking"] = {"type": "adaptive"}
                p1["output_config"] = {"effort": "high"}
                candidates.append(p1)

            # Tier 2: drop `thinking`, keep `output_config.effort` (when thinking itself is rejected)
            if lvl != "low":
                p2 = _base()
                p2["max_tokens"] = max(eff_base + 1536, 2048)
                p2["output_config"] = {"effort": "high" if lvl == "xhigh" else lvl}
                candidates.append(p2)

            # Tier 3: strip both `thinking` and `output_config` — NEVER inject `temperature`!
            p3 = _base()
            p3["max_tokens"] = eff_base
            candidates.append(p3)

            # Tier 4: bare base_max_tokens without `temperature`, `thinking`, or `output_config`
            if eff_base != int(base_max_tokens):
                p4 = _base()
                p4["max_tokens"] = int(base_max_tokens)
                candidates.append(p4)

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for cand in candidates:
            sig = json.dumps(cand, sort_keys=True, ensure_ascii=False)
            if sig not in seen:
                seen.add(sig)
                out.append(cand)
        return out

    @staticmethod
    def _sleep_cancellable(seconds: float, cancel_event: threading.Event | None = None) -> bool:
        """Sleep up to `seconds`, returning True if interrupted by `cancel_event`."""
        if cancel_event is None:
            time.sleep(seconds)
            return False
        return cancel_event.wait(timeout=max(0.0, seconds))

    def _post_one_shot(
        self,
        *,
        api_key: str,
        model: str,
        prompt: str,
        thinking_level: str,
        base_max_tokens: int,
        adaptive_floor: int,
        temperature: float,
        timeout_sec: float,
    ) -> str:
        url = "https://api.anthropic.com/v1/messages"
        messages = [{"role": "user", "content": prompt}]
        lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        key_hash = self._key_hash(api_key)
        with self._cache_lock:
            primary_model = self._model_fallback_map.get((key_hash, model), model)

        tried_models: set[str] = set()
        candidates_queue: list[str] = [primary_model]
        last_status = 0
        last_err = ""
        idx_q = 0
        while idx_q < len(candidates_queue):
            active_model = candidates_queue[idx_q]
            idx_q += 1
            if active_model in tried_models:
                continue
            tried_models.add(active_model)
            payloads = self._build_compat_payloads(
                model=active_model,
                messages=messages,
                system_prompt="",
                thinking_level=lvl,
                base_max_tokens=base_max_tokens,
                adaptive_floor=adaptive_floor,
                temperature=temperature,
                stream=False,
            )
            for tier_idx, payload in self._ordered_compat_payloads(key_hash, active_model, lvl, False, payloads):
                status_code = 0
                data: Any = None
                err_body = ""
                for retry_i in range(3):
                    status_code, data, err_body = self._request_json_with_error(
                        "POST",
                        url,
                        headers=self._headers(api_key),
                        json_body=payload,
                        timeout=timeout_sec,
                        secret_to_scrub=api_key,
                    )
                    if status_code in (429, 529) and retry_i < 2:
                        time.sleep(0.8 * (retry_i + 1))
                        continue
                    break
                last_status, last_err = status_code, err_body
                if status_code == 200 and isinstance(data, dict):
                    with self._cache_lock:
                        self._compat_tier_cache[(key_hash, active_model, lvl, False)] = tier_idx
                        if active_model != model:
                            self._model_fallback_map[(key_hash, model)] = active_model
                    raw = self._extract_visible_text(data.get("content") or [])
                    stop_reason = str(data.get("stop_reason") or "")
                    if not raw.strip() and stop_reason == "max_tokens":
                        retry_payload = {
                            "model": active_model,
                            "messages": messages,
                            "max_tokens": max(int(payload.get("max_tokens", adaptive_floor)) * 2, 2048),
                        }
                        if "output_config" in payload:
                            retry_payload["output_config"] = {"effort": "low"}
                        r_sc, r_data, _ = self._request_json_with_error(
                            "POST",
                            url,
                            headers=self._headers(api_key),
                            json_body=retry_payload,
                            timeout=timeout_sec,
                            secret_to_scrub=api_key,
                        )
                        if r_sc == 400 and "output_config" in retry_payload:
                            retry_payload.pop("output_config", None)
                            r_sc, r_data, _ = self._request_json_with_error(
                                "POST",
                                url,
                                headers=self._headers(api_key),
                                json_body=retry_payload,
                                timeout=timeout_sec,
                                secret_to_scrub=api_key,
                            )
                        if r_sc == 200 and isinstance(r_data, dict):
                            raw = self._extract_visible_text(r_data.get("content") or [])
                    return raw
                if status_code != 400:
                    break
            if last_status == 404:
                if active_model != model:
                    with self._cache_lock:
                        if self._model_fallback_map.get((key_hash, model)) == active_model:
                            self._model_fallback_map.pop((key_hash, model), None)
                next_fbs = self._get_404_fallback_candidates(active_model, exclude=tried_models)
                if next_fbs:
                    fb_model = next_fbs[0]
                    self._log(
                        f"🔄 Claude API 모델 자동 전환: {active_model} → {fb_model} (HTTP 404)",
                        extra_secrets=(api_key,),
                    )
                    for cand_fb in next_fbs:
                        if cand_fb not in candidates_queue:
                            candidates_queue.append(cand_fb)
                    continue
            break
        if last_status and last_status != 200:
            self._log(
                f"⚠️ Claude API 보조 요청 실패 HTTP {last_status}: {last_err[:180]}",
                extra_secrets=(api_key,),
            )
        return ""

    def list_generate_models(self, api_key: str, timeout_sec: float = 10.0) -> list[str]:
        """Fetch available Claude model IDs from `GET https://api.anthropic.com/v1/models`."""
        if not str(api_key or "").strip():
            return []
        clean_key = _validate_api_key_header(api_key, "Claude")
        url = "https://api.anthropic.com/v1/models"
        status_code, data, err_body = self._request_json_with_error(
            "GET",
            url,
            headers={"x-api-key": clean_key, "anthropic-version": self.ANTHROPIC_VERSION},
            params={"limit": 100},
            timeout=timeout_sec,
            secret_to_scrub=clean_key,
        )
        if status_code != 200:
            detail = f": {err_body[:240]}" if err_body else ""
            raise RuntimeError(f"Claude 모델 목록 조회 실패 HTTP {status_code}{detail}")
        raw_rows = (data if isinstance(data, dict) else {}).get("data")
        rows = raw_rows if isinstance(raw_rows, list) else []
        out: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mid = str(row.get("id") or "").strip()
            if not mid or not mid.lower().startswith("claude-"):
                continue
            if not _RE_SAFE_GEMINI_MODEL.fullmatch(mid) or ".." in mid:
                continue
            out.append(mid)
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
        """Stateless one-shot Claude API translation for subtitle refinement."""
        clean_jp = str(jp_text or "").strip()
        if not clean_jp:
            return ""
        try:
            clean_key = _validate_api_key_header(api_key, "Claude")
            clean_model = _validate_api_model_id(model, default="claude-sonnet-5-5", provider_label="Claude")
        except (RuntimeError, ValueError):
            return ""
        eff_timeout = (
            float(timeout_sec)
            if timeout_sec is not None
            else {"low": 22.0, "medium": 35.0, "high": 55.0, "xhigh": 75.0}.get(thinking_level, 25.0)
        )
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
        try:
            raw = self._post_one_shot(
                api_key=clean_key,
                model=clean_model,
                prompt=prompt,
                thinking_level=thinking_level,
                base_max_tokens=260,
                adaptive_floor=1024,
                temperature=0.25,
                timeout_sec=eff_timeout,
            )
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
        """Stateless one-shot Claude API call for background memory extraction."""
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            return ""
        try:
            clean_key = _validate_api_key_header(api_key, "Claude")
            clean_model = _validate_api_model_id(model, default="claude-sonnet-5-5", provider_label="Claude")
        except (RuntimeError, ValueError):
            return ""
        base_max = max(300, int(max_output_tokens))
        eff_timeout = max(20.0, float(timeout_sec or 40.0))
        try:
            return self._post_one_shot(
                api_key=clean_key,
                model=clean_model,
                prompt=clean_prompt,
                thinking_level=thinking_level,
                base_max_tokens=base_max,
                adaptive_floor=max(base_max, 1600),
                temperature=0.2,
                timeout_sec=eff_timeout,
            ).strip()
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
        temperature: float = 0.84,
        history_user_text: str | None = None,
    ) -> str:
        if cancel_event is not None and cancel_event.is_set():
            return ""
        clean_key = _validate_api_key_header(api_key, "Claude")
        if not str(text or "").strip():
            return ""
        clean_model = _validate_api_model_id(model, default="claude-sonnet-5-5", provider_label="Claude")
        key_hash = self._key_hash(clean_key)
        url = _validate_https_url("https://api.anthropic.com/v1/messages", _ALLOWED_CLAUDE_API_HOSTS)

        with self._lock:
            if system_prompt:
                self._system_prompt = system_prompt
            prev_history = list(self._history)
            sys_instr = self._system_prompt
            chat_epoch = self._chat_epoch

        bilingual_active = bool(bilingual_inline and on_sentence_pair is not None)
        base_max = 1500 if bilingual_active else 950
        adaptive_floor = 2560 if bilingual_active else 2048
        eff_temp = round(_coerce_finite_float(temperature, 0.84, low=0.1, high=1.0), 2)
        lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        read_to = {"low": 65.0, "medium": 85.0, "high": 115.0, "xhigh": 140.0}.get(lvl, 75.0)

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

        def _consume_sse(req_payload: dict[str, Any]) -> tuple[int, str, str]:
            nonlocal is_first
            stop_reason = ""
            stream_kwargs: dict[str, Any] = {
                "headers": self._headers(clean_key),
                "json": req_payload,
            }
            if isinstance(self._client, httpx.Client):
                stream_kwargs["timeout"] = httpx.Timeout(read_to + 10.0, connect=8.0, read=read_to, write=10.0, pool=5.0)
            try:
                with self._client.stream("POST", url, **stream_kwargs) as resp:
                    if resp.status_code != 200:
                        return resp.status_code, self._read_bounded_err_body(resp, secret_to_scrub=clean_key), ""

                    total_chars = 0
                    for line in resp.iter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        if not line:
                            continue
                        if len(line) > _MAX_GEMINI_SSE_LINE_CHARS:
                            raise RuntimeError("Claude API SSE line exceeds safety size limit")
                        total_chars += len(line)
                        if total_chars > _MAX_GEMINI_SSE_TOTAL_CHARS:
                            raise RuntimeError("Claude API SSE response exceeds safety size limit")
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            break
                        try:
                            obj = json.loads(data_str)
                        except Exception:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        ev_type = str(obj.get("type") or "")
                        if ev_type == "error":
                            err_obj = obj.get("error") if isinstance(obj.get("error"), dict) else {}
                            err_type = str(err_obj.get("type") or "").strip()
                            err_msg = str(err_obj.get("message") or data_str).strip()
                            scrubbed = _scrub_secrets(
                                f"{err_type}: {err_msg}" if err_type else err_msg,
                                extra_secrets=(clean_key,),
                            )
                            if err_type == "overloaded_error" or "overloaded" in err_msg.lower():
                                return 529, scrubbed, "error"
                            return 500, scrubbed, "error"
                        if ev_type == "message_delta":
                            md = obj.get("delta")
                            if isinstance(md, dict) and md.get("stop_reason"):
                                stop_reason = str(md.get("stop_reason") or "")
                            continue
                        if ev_type == "message_stop":
                            break
                        if ev_type != "content_block_delta":
                            continue
                        delta_obj = obj.get("delta") or {}
                        if not isinstance(delta_obj, dict) or delta_obj.get("type") != "text_delta":
                            continue
                        delta = str(delta_obj.get("text") or "")
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
                    return 200, "", stop_reason
            except httpx.HTTPError as exc:
                raise RuntimeError(_scrub_secrets(f"Claude API 스트리밍 오류: {exc}", extra_secrets=(clean_key,))) from None

        def _run_attempt(req_user_text: str) -> tuple[str, str]:
            nonlocal is_first
            msgs = self._normalize_messages(prev_history, req_user_text, max_messages=24)
            with self._cache_lock:
                primary_model = self._model_fallback_map.get((key_hash, clean_model), clean_model)
            active_model = primary_model
            status_code = 0
            err_body = ""
            stop_reason = ""
            used_max_tokens = adaptive_floor
            used_output_config = False

            tried_models: set[str] = set()
            candidates_queue: list[str] = [primary_model]
            idx_q = 0
            while idx_q < len(candidates_queue):
                active_model = candidates_queue[idx_q]
                idx_q += 1
                if active_model in tried_models:
                    continue
                tried_models.add(active_model)
                payloads = self._build_compat_payloads(
                    model=active_model,
                    messages=msgs,
                    system_prompt=sys_instr,
                    thinking_level=lvl,
                    base_max_tokens=base_max,
                    adaptive_floor=adaptive_floor,
                    temperature=eff_temp,
                    stream=True,
                )
                for tier_idx, req_payload in self._ordered_compat_payloads(key_hash, active_model, lvl, True, payloads):
                    used_max_tokens = int(req_payload.get("max_tokens", adaptive_floor))
                    used_output_config = "output_config" in req_payload
                    for retry_i in range(3):
                        splitter.reset()
                        emitted_parts.clear()
                        raw_full.clear()
                        is_first = True
                        status_code, err_body, stop_reason = _consume_sse(req_payload)
                        if (
                            status_code in (429, 529)
                            and retry_i < 2
                            and not emitted_parts
                            and (cancel_event is None or not cancel_event.is_set())
                        ):
                            self._log(
                                f"⏳ Claude API 일시 과부하/속도 제한 (HTTP {status_code}) → 재시도 ({retry_i + 1}/2)",
                                extra_secrets=(clean_key,),
                            )
                            if self._sleep_cancellable(0.8 * (retry_i + 1), cancel_event):
                                break
                            continue
                        break
                    if status_code == 200:
                        with self._cache_lock:
                            self._compat_tier_cache[(key_hash, active_model, lvl, True)] = tier_idx
                            if active_model != clean_model:
                                self._model_fallback_map[(key_hash, clean_model)] = active_model
                        break
                    if status_code != 400:
                        break
                if status_code == 200:
                    break
                if status_code == 404 and (cancel_event is None or not cancel_event.is_set()):
                    if active_model != clean_model:
                        with self._cache_lock:
                            if self._model_fallback_map.get((key_hash, clean_model)) == active_model:
                                self._model_fallback_map.pop((key_hash, clean_model), None)
                    next_fbs = self._get_404_fallback_candidates(active_model, exclude=tried_models)
                    if next_fbs:
                        fb_model = next_fbs[0]
                        self._log(
                            f"🔄 Claude API 모델 자동 전환: {active_model} → {fb_model} (HTTP 404)",
                            extra_secrets=(clean_key,),
                        )
                        for cand_fb in next_fbs:
                            if cand_fb not in candidates_queue:
                                candidates_queue.append(cand_fb)
                        continue
                break

            if status_code != 200:
                safe_err = _scrub_secrets(err_body[:240], extra_secrets=(clean_key,))
                raise RuntimeError(f"Claude API HTTP {status_code}: {safe_err}")

            # Retry once with higher max_tokens if adaptive thinking exhausted the token budget with 0 visible text
            if (
                not "".join(raw_full).strip()
                and stop_reason == "max_tokens"
                and (cancel_event is None or not cancel_event.is_set())
            ):
                splitter.reset()
                emitted_parts.clear()
                raw_full.clear()
                is_first = True
                retry_payload: dict[str, Any] = {
                    "model": active_model,
                    "messages": msgs,
                    "stream": True,
                    "max_tokens": max(used_max_tokens * 2, 4096),
                }
                if sys_instr:
                    retry_payload["system"] = sys_instr
                if used_output_config:
                    retry_payload["output_config"] = {"effort": "low"}
                r_sc, r_err, r_stop = _consume_sse(retry_payload)
                if r_sc == 400 and "output_config" in retry_payload:
                    splitter.reset()
                    emitted_parts.clear()
                    raw_full.clear()
                    is_first = True
                    retry_payload.pop("output_config", None)
                    r_sc, r_err, r_stop = _consume_sse(retry_payload)
                if r_sc != 200:
                    safe_err = _scrub_secrets(r_err[:240], extra_secrets=(clean_key,))
                    raise RuntimeError(f"Claude API HTTP {r_sc}: {safe_err}")
                stop_reason = r_stop
                if not "".join(raw_full).strip() and stop_reason == "max_tokens":
                    raise RuntimeError(
                        "Claude API 추론 토큰 한도 초과로 빈 응답이 반환되었습니다 (stop_reason=max_tokens)."
                    )

            if not "".join(raw_full).strip() and stop_reason == "refusal":
                raise RuntimeError("Claude API가 안전 정책으로 응답을 거부했습니다 (refusal).")

            if cancel_event is not None and cancel_event.is_set():
                splitter.reset()
                return "", ""

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

        try:
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

            cancelled = cancel_event is not None and cancel_event.is_set()
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
            if full_model_text and (not failed_lang_lock) and (not cancelled):
                hist_user = (
                    history_user_text
                    if history_user_text is not None
                    else _strip_turn_wrapper_for_history(text)
                ).strip() or text
                with self._lock:
                    if self._chat_epoch == chat_epoch:
                        self._history = (
                            prev_history[-22:]
                            + [
                                {"role": "user", "content": hist_user},
                                {"role": "assistant", "content": full_model_text},
                            ]
                        )[-24:]
            return "" if cancelled else emitted_text
        finally:
            splitter.reset()

    def close(self) -> None:
        with self._client_lock:
            client = self._client_obj
            if self._own_client and client is not None:
                self._client_obj = None
                try:
                    client.close()
                except Exception:
                    pass


class OfficialOpenAIApiBackend:
    """
    Official OpenAI ChatGPT REST SSE streaming backend (`https://api.openai.com/v1/chat/completions`).
    Enforces strict HTTPS host allowlist, Bearer header auth, bounded response sizes, and secret scrubbing.
    """

    _OPENAI_404_FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
        "gpt-6.2-astra": ("gpt-6.2-sol", "gpt-5.4", "gpt-5.4-mini", "gpt-4o-mini"),
        "gpt-6.2-sol": ("gpt-6-luna", "gpt-5.4-mini", "gpt-4o-mini"),
        "gpt-6-luna": ("gpt-5.4-mini", "gpt-4o-mini"),
        "gpt-5.4": ("gpt-5.4-mini", "gpt-4o-mini"),
        "gpt-5.4-nano": ("gpt-5.4-mini", "gpt-4o-mini"),
        "gpt-5.4-mini": ("gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"),
        "gpt-5-mini": ("gpt-5.4-mini", "gpt-4o-mini"),
        "gpt-4.1-mini": ("gpt-5.4-mini", "gpt-4o-mini"),
    }
    _OPENAI_DEFAULT_404_CHAIN: tuple[str, ...] = ("gpt-5.4-mini", "gpt-4o-mini")

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._own_client = http_client is None
        self._client_obj: httpx.Client | None = http_client
        self._client_lock = threading.Lock()
        self.log_cb = log_cb or (lambda _m: None)
        self._history: list[dict[str, Any]] = []
        self._system_prompt = ""
        self._chat_epoch = 0
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._model_fallback_map: dict[tuple[str, str], str] = {}
        self._compat_tier_cache: dict[tuple[str, str, str, bool], int] = {}

    @staticmethod
    def _key_hash(api_key: str) -> str:
        return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _get_404_fallback_candidates(cls, model: str, *, exclude: set[str] | None = None) -> list[str]:
        skip = set(exclude or ())
        skip.add(model)
        chain = list(cls._OPENAI_404_FALLBACK_CHAINS.get(model, ()))
        for d in cls._OPENAI_DEFAULT_404_CHAIN:
            if d not in chain:
                chain.append(d)
        return [m for m in chain if m not in skip]

    def _ordered_compat_payloads(
        self,
        key_hash: str,
        model: str,
        lvl: str,
        stream: bool,
        payloads: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        indexed = list(enumerate(payloads))
        with self._cache_lock:
            cached_idx = self._compat_tier_cache.get((key_hash, model, lvl, bool(stream)))
        if cached_idx is not None and 0 <= cached_idx < len(indexed):
            return [indexed[cached_idx]] + [p for p in indexed if p[0] != cached_idx]
        return indexed

    def _log(self, msg: str, extra_secrets: tuple[str, ...] = ()) -> None:
        try:
            self.log_cb(_scrub_secrets(msg, extra_secrets=extra_secrets))
        except Exception:
            pass

    @staticmethod
    def _read_bounded_err_body(resp: Any, secret_to_scrub: str = "") -> str:
        raw_bytes = b""
        iter_bytes = getattr(resp, "iter_bytes", None)
        if callable(iter_bytes):
            buf = bytearray()
            try:
                for chunk in iter_bytes():
                    if not chunk:
                        continue
                    need = _MAX_GEMINI_API_ERROR_BYTES - len(buf)
                    if need <= 0:
                        break
                    buf.extend(chunk[:need])
                    if len(buf) >= _MAX_GEMINI_API_ERROR_BYTES:
                        break
                raw_bytes = bytes(buf)
            except Exception:
                raw_bytes = b""
        if not raw_bytes:
            raw_content = getattr(resp, "content", None)
            if isinstance(raw_content, (bytes, bytearray)):
                raw_bytes = bytes(raw_content[:_MAX_GEMINI_API_ERROR_BYTES])
            else:
                read_fn = getattr(resp, "read", None)
                if callable(read_fn):
                    try:
                        raw_bytes = bytes(read_fn() or b"")[:_MAX_GEMINI_API_ERROR_BYTES]
                    except Exception:
                        raw_bytes = b""
        if not raw_bytes:
            raw_text = getattr(resp, "text", None)
            if isinstance(raw_text, str) and raw_text:
                raw_bytes = raw_text.encode("utf-8", errors="replace")[:_MAX_GEMINI_API_ERROR_BYTES]
            else:
                json_fn = getattr(resp, "json", None)
                if callable(json_fn):
                    try:
                        obj = json_fn()
                        if obj is not None:
                            raw_bytes = json.dumps(obj, ensure_ascii=False).encode("utf-8", errors="replace")[
                                :_MAX_GEMINI_API_ERROR_BYTES
                            ]
                    except Exception:
                        raw_bytes = b""
        return _scrub_secrets(raw_bytes.decode("utf-8", errors="replace"), extra_secrets=(secret_to_scrub,))

    @property
    def _client(self) -> httpx.Client:
        if self._client_obj is not None:
            return self._client_obj
        with self._client_lock:
            if self._client_obj is None:
                self._client_obj = httpx.Client(
                    verify=True,
                    follow_redirects=False,
                    timeout=httpx.Timeout(90.0, connect=8.0, read=70.0, write=10.0, pool=5.0),
                    limits=httpx.Limits(max_keepalive_connections=6, max_connections=12, keepalive_expiry=60.0),
                )
            return self._client_obj

    @_client.setter
    def _client(self, client: httpx.Client | None) -> None:
        self._client_obj = client

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _is_reasoning_model(model: str) -> bool:
        m = (model or "").strip().lower()
        if "-chat" in m:
            return False
        return m.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6"))

    @staticmethod
    def _supports_reasoning_none(model: str) -> bool:
        """Return True ONLY for OpenAI reasoning models that accept `reasoning_effort='none'` (`gpt-5.4*`, `gpt-6-luna*`)."""
        m = (model or "").strip().lower()
        if "-chat" in m:
            return False
        return m.startswith(("gpt-5.4", "gpt-6-luna"))

    @staticmethod
    def _extract_content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("type") in ("text", "output_text", None):
                    txt = item.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
                    elif isinstance(txt, dict) and isinstance(txt.get("value"), str):
                        parts.append(txt["value"])
            return "".join(parts)
        return ""

    @staticmethod
    def _convert_system_to_user_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fallback for legacy `o1-mini`/`o1-preview` models that reject `role: system`."""
        sys_parts: list[str] = []
        rest: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "").lower() == "system":
                c = str(msg.get("content") or "").strip()
                if c:
                    sys_parts.append(c)
            else:
                rest.append(dict(msg))
        if not sys_parts:
            return rest
        prefix = "\n\n".join(sys_parts)
        if rest and str(rest[0].get("role") or "").lower() == "user":
            rest[0] = {"role": "user", "content": f"{prefix}\n\n{rest[0].get('content', '')}"}
            return rest
        return [{"role": "user", "content": prefix}] + rest

    def _build_chat_payload(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        base_max_tokens: int,
        temperature: float,
        stream: bool,
        compat_tier: int = 0,
    ) -> dict[str, Any]:
        """Build OpenAI chat/completions payload with progressive compatibility fallback tiers (0..4)."""
        lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        is_reasoning = self._is_reasoning_model(model)
        supports_none = self._supports_reasoning_none(model)
        eff_base = max(int(base_max_tokens), 1024 if base_max_tokens >= 600 else 512) if is_reasoning else int(base_max_tokens)
        req: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if stream:
            req["stream"] = True

        if is_reasoning:
            if compat_tier == 0:
                if lvl == "low" and supports_none:
                    req["max_completion_tokens"] = eff_base + 512
                    req["reasoning_effort"] = "none"
                    req["temperature"] = temperature
                else:
                    extra = {"low": 1536, "medium": 3072, "high": 5120, "xhigh": 8192}[lvl]
                    req["max_completion_tokens"] = eff_base + extra
                    req["reasoning_effort"] = lvl
            elif compat_tier == 1:
                if lvl == "low" and supports_none:
                    req["max_completion_tokens"] = eff_base + 512
                    req["reasoning_effort"] = "none"
                elif lvl == "xhigh":
                    req["max_completion_tokens"] = eff_base + 5120
                    req["reasoning_effort"] = "high"
                else:
                    req["max_completion_tokens"] = eff_base + 2048
            elif compat_tier == 2:
                if lvl == "low" and supports_none:
                    req["max_completion_tokens"] = eff_base + 1536
                    req["reasoning_effort"] = "low"
                else:
                    req["max_completion_tokens"] = eff_base + 2048
            elif compat_tier == 3:
                req["max_completion_tokens"] = eff_base + 2048
            elif compat_tier == 4:
                req["messages"] = self._convert_system_to_user_messages(messages)
                req["max_completion_tokens"] = eff_base + 2048
            else:
                req["max_tokens"] = int(base_max_tokens)
        else:
            if compat_tier == 0:
                req["max_completion_tokens"] = int(base_max_tokens)
                req["temperature"] = temperature
            elif compat_tier == 1:
                req["max_tokens"] = int(base_max_tokens)
                req["temperature"] = temperature
            else:
                req["max_tokens"] = int(base_max_tokens)
        return req

    def _build_compat_payloads(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        base_max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> list[dict[str, Any]]:
        """Build deduplicated OpenAI chat/completions payloads so identical payloads are never retried on HTTP 400."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tier in (0, 1, 2, 3, 4, 5):
            cand = self._build_chat_payload(
                model=model,
                messages=messages,
                thinking_level=thinking_level,
                base_max_tokens=base_max_tokens,
                temperature=temperature,
                stream=stream,
                compat_tier=tier,
            )
            sig = json.dumps(cand, sort_keys=True, ensure_ascii=False)
            if sig not in seen:
                seen.add(sig)
                out.append(cand)
        return out

    def _request_json_with_error(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 15.0,
        max_bytes: int = _MAX_GEMINI_API_JSON_BYTES,
        secret_to_scrub: str = "",
    ) -> tuple[int, Any, str]:
        safe_url = _validate_https_url(url, _ALLOWED_OPENAI_API_HOSTS)
        client = self._client
        try:
            if isinstance(client, httpx.Client):
                with client.stream(
                    method,
                    safe_url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=timeout,
                ) as resp:
                    if resp.status_code != 200:
                        return resp.status_code, None, self._read_bounded_err_body(resp, secret_to_scrub=secret_to_scrub)
                    cl_raw = resp.headers.get("content-length")
                    if cl_raw is not None:
                        try:
                            if int(cl_raw) > max_bytes:
                                raise RuntimeError("OpenAI API response exceeds safety size limit")
                        except ValueError:
                            pass
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise RuntimeError("OpenAI API response exceeds safety size limit")
                    try:
                        return 200, json.loads(buf.decode("utf-8", errors="replace")), ""
                    except (json.JSONDecodeError, ValueError):
                        raise RuntimeError("OpenAI API 응답 JSON 파싱 오류") from None

            if method.upper() == "GET":
                resp = client.get(safe_url, params=params, headers=headers, timeout=timeout)
            else:
                resp = client.post(safe_url, headers=headers, json=json_body, timeout=timeout)
            if resp.status_code != 200:
                return resp.status_code, None, self._read_bounded_err_body(resp, secret_to_scrub=secret_to_scrub)
            raw_content = getattr(resp, "content", None)
            if isinstance(raw_content, (bytes, bytearray)) and len(raw_content) > max_bytes:
                raise RuntimeError("OpenAI API response exceeds safety size limit")
            try:
                return 200, resp.json(), ""
            except (json.JSONDecodeError, ValueError):
                raise RuntimeError("OpenAI API 응답 JSON 파싱 오류") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(_scrub_secrets(f"OpenAI API 네트워크 오류: {exc}", extra_secrets=(secret_to_scrub,))) from None

    def _request_json_bounded(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = 15.0,
        max_bytes: int = _MAX_GEMINI_API_JSON_BYTES,
        secret_to_scrub: str = "",
    ) -> tuple[int, Any]:
        status_code, data, _ = self._request_json_with_error(
            method,
            url,
            headers=headers,
            params=params,
            json_body=json_body,
            timeout=timeout,
            max_bytes=max_bytes,
            secret_to_scrub=secret_to_scrub,
        )
        return status_code, data

    def start_new_chat(self, system_prompt: str = "") -> None:
        with self._lock:
            self._history = []
            self._system_prompt = system_prompt
            self._chat_epoch += 1

    def update_system_prompt(self, system_prompt: str) -> None:
        with self._lock:
            self._system_prompt = system_prompt

    def list_generate_models(self, api_key: str, timeout_sec: float = 10.0) -> list[str]:
        """Fetch chat-capable OpenAI model IDs from `GET https://api.openai.com/v1/models`."""
        if not str(api_key or "").strip():
            return []
        clean_key = _validate_api_key_header(api_key, "OpenAI")
        url = "https://api.openai.com/v1/models"
        status_code, data, err_body = self._request_json_with_error(
            "GET",
            url,
            headers={"Authorization": f"Bearer {clean_key}"},
            timeout=timeout_sec,
            secret_to_scrub=clean_key,
        )
        if status_code != 200:
            detail = f": {err_body[:240]}" if err_body else ""
            raise RuntimeError(f"OpenAI 모델 목록 조회 실패 HTTP {status_code}{detail}")
        raw_rows = (data if isinstance(data, dict) else {}).get("data")
        rows = raw_rows if isinstance(raw_rows, list) else []
        skip_tokens = (
            "embedding",
            "whisper",
            "tts",
            "dall-e",
            "sora",
            "realtime",
            "audio",
            "transcribe",
            "moderation",
            "image",
            "search",
            "instruct",
            "babbage",
            "davinci",
            "codex",
            "computer-use",
        )
        out: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mid = str(row.get("id") or "").strip()
            low = mid.lower()
            if not low.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")):
                continue
            if any(tok in low for tok in skip_tokens):
                continue
            if not _RE_SAFE_GEMINI_MODEL.fullmatch(mid) or ".." in mid:
                continue
            out.append(mid)
        return sorted(set(out))

    @staticmethod
    def _sleep_cancellable(seconds: float, cancel_event: threading.Event | None = None) -> bool:
        if cancel_event is None:
            time.sleep(seconds)
            return False
        return cancel_event.wait(timeout=max(0.0, seconds))

    def _post_one_shot(
        self,
        api_key: str,
        model: str,
        prompt: str,
        *,
        thinking_level: str,
        base_max_tokens: int,
        temperature: float,
        timeout_sec: float,
    ) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        messages = [{"role": "user", "content": prompt}]
        lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        key_hash = self._key_hash(api_key)
        with self._cache_lock:
            primary_model = self._model_fallback_map.get((key_hash, model), model)

        tried_models: set[str] = set()
        candidates_queue: list[str] = [primary_model]
        last_status = 0
        last_err = ""
        idx_q = 0
        while idx_q < len(candidates_queue):
            active_model = candidates_queue[idx_q]
            idx_q += 1
            if active_model in tried_models:
                continue
            tried_models.add(active_model)
            payloads = self._build_compat_payloads(
                model=active_model,
                messages=messages,
                thinking_level=lvl,
                base_max_tokens=base_max_tokens,
                temperature=temperature,
                stream=False,
            )
            for tier_idx, payload in self._ordered_compat_payloads(key_hash, active_model, lvl, False, payloads):
                status_code = 0
                data: Any = None
                err_body = ""
                for retry_i in range(3):
                    status_code, data, err_body = self._request_json_with_error(
                        "POST",
                        url,
                        headers=self._headers(api_key),
                        json_body=payload,
                        timeout=timeout_sec,
                        secret_to_scrub=api_key,
                    )
                    if status_code in (429, 503) and retry_i < 2:
                        time.sleep(0.8 * (retry_i + 1))
                        continue
                    break
                last_status, last_err = status_code, err_body
                if status_code == 200 and isinstance(data, dict):
                    with self._cache_lock:
                        self._compat_tier_cache[(key_hash, active_model, lvl, False)] = tier_idx
                        if active_model != model:
                            self._model_fallback_map[(key_hash, model)] = active_model
                    choices = data.get("choices")
                    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                        return ""
                    msg = choices[0].get("message")
                    txt = self._extract_content_text(msg.get("content") if isinstance(msg, dict) else None)
                    fin = str(choices[0].get("finish_reason") or "")
                    if not txt.strip() and fin == "length":
                        retry_p = dict(payload)
                        retry_p.pop("temperature", None)
                        cur_tok = int(retry_p.get("max_completion_tokens") or retry_p.get("max_tokens") or base_max_tokens)
                        if "max_completion_tokens" in retry_p:
                            retry_p["max_completion_tokens"] = max(cur_tok * 2, 2560)
                        else:
                            retry_p["max_tokens"] = max(cur_tok * 2, 2048)
                        if self._is_reasoning_model(active_model):
                            retry_p["reasoning_effort"] = "none" if self._supports_reasoning_none(active_model) else "low"
                        r_sc, r_data, _ = self._request_json_with_error(
                            "POST",
                            url,
                            headers=self._headers(api_key),
                            json_body=retry_p,
                            timeout=timeout_sec,
                            secret_to_scrub=api_key,
                        )
                        if r_sc == 400 and retry_p.get("reasoning_effort") == "none":
                            retry_p["reasoning_effort"] = "low"
                            r_sc, r_data, _ = self._request_json_with_error(
                                "POST",
                                url,
                                headers=self._headers(api_key),
                                json_body=retry_p,
                                timeout=timeout_sec,
                                secret_to_scrub=api_key,
                            )
                        if r_sc == 200 and isinstance(r_data, dict):
                            rc = r_data.get("choices")
                            if isinstance(rc, list) and rc and isinstance(rc[0], dict):
                                r_msg = rc[0].get("message")
                                txt = self._extract_content_text(r_msg.get("content") if isinstance(r_msg, dict) else None)
                    return txt
                if status_code != 400:
                    break
            if last_status == 404:
                if active_model != model:
                    with self._cache_lock:
                        if self._model_fallback_map.get((key_hash, model)) == active_model:
                            self._model_fallback_map.pop((key_hash, model), None)
                next_fbs = self._get_404_fallback_candidates(active_model, exclude=tried_models)
                if next_fbs:
                    fb_model = next_fbs[0]
                    self._log(
                        f"🔄 OpenAI API 모델 자동 전환: {active_model} → {fb_model} (HTTP 404)",
                        extra_secrets=(api_key,),
                    )
                    for cand_fb in next_fbs:
                        if cand_fb not in candidates_queue:
                            candidates_queue.append(cand_fb)
                    continue
            break
        if last_status and last_status != 200:
            self._log(
                f"⚠️ OpenAI API 보조 요청 실패 HTTP {last_status}: {last_err[:180]}",
                extra_secrets=(api_key,),
            )
        return ""

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
        """Stateless one-shot OpenAI API translation for subtitle refinement."""
        clean_jp = str(jp_text or "").strip()
        if not clean_jp:
            return ""
        try:
            clean_key = _validate_api_key_header(api_key, "OpenAI")
            clean_model = _validate_api_model_id(model, default="gpt-5.4-mini", provider_label="OpenAI")
        except (RuntimeError, ValueError):
            return ""
        eff_timeout = (
            float(timeout_sec)
            if timeout_sec is not None
            else {"low": 25.0, "medium": 40.0, "high": 65.0, "xhigh": 85.0}.get(thinking_level, 28.0)
        )
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
        try:
            raw = self._post_one_shot(
                clean_key,
                clean_model,
                prompt,
                thinking_level=thinking_level,
                base_max_tokens=260,
                temperature=0.25,
                timeout_sec=eff_timeout,
            )
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
        """Stateless one-shot OpenAI API call for background memory extraction."""
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            return ""
        try:
            clean_key = _validate_api_key_header(api_key, "OpenAI")
            clean_model = _validate_api_model_id(model, default="gpt-5.4-mini", provider_label="OpenAI")
        except (RuntimeError, ValueError):
            return ""
        try:
            return self._post_one_shot(
                clean_key,
                clean_model,
                clean_prompt,
                thinking_level=thinking_level,
                base_max_tokens=max(300, int(max_output_tokens)),
                temperature=0.2,
                timeout_sec=max(20.0, float(timeout_sec or 45.0)),
            ).strip()
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
        temperature: float = 0.84,
        history_user_text: str | None = None,
    ) -> str:
        if cancel_event is not None and cancel_event.is_set():
            return ""
        clean_key = _validate_api_key_header(api_key, "OpenAI")
        if not str(text or "").strip():
            return ""
        clean_model = _validate_api_model_id(model, default="gpt-5.4-mini", provider_label="OpenAI")
        key_hash = self._key_hash(clean_key)
        url = _validate_https_url("https://api.openai.com/v1/chat/completions", _ALLOWED_OPENAI_API_HOSTS)

        with self._lock:
            if system_prompt:
                self._system_prompt = system_prompt
            prev_history = list(self._history)
            sys_instr = self._system_prompt
            chat_epoch = self._chat_epoch

        bilingual_active = bool(bilingual_inline and on_sentence_pair is not None)
        base_max = 1500 if bilingual_active else 950
        eff_temp = round(_coerce_finite_float(temperature, 0.84, low=0.1, high=1.25), 2)
        lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        read_to = {"low": 65.0, "medium": 85.0, "high": 115.0, "xhigh": 140.0}.get(lvl, 70.0)

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

        def _make_messages(req_user_text: str) -> list[dict[str, Any]]:
            msgs: list[dict[str, Any]] = []
            if sys_instr:
                msgs.append({"role": "system", "content": sys_instr})
            # Slice even pairs (`prev_history[-22:]`) so Turn 13+ never starts with an orphaned assistant message!
            tail = [m for m in prev_history[-22:] if isinstance(m, dict) and str(m.get("content") or "").strip()]
            while tail and str(tail[0].get("role") or "").lower() != "user":
                tail.pop(0)
            msgs.extend(tail)
            msgs.append({"role": "user", "content": req_user_text})
            return msgs

        def _consume_sse(req_payload: dict[str, Any]) -> tuple[int, str, str, str]:
            nonlocal is_first
            finish_reason = ""
            refusal_parts: list[str] = []
            stream_kwargs: dict[str, Any] = {
                "headers": self._headers(clean_key),
                "json": req_payload,
            }
            if isinstance(self._client, httpx.Client):
                stream_kwargs["timeout"] = httpx.Timeout(read_to + 15.0, connect=8.0, read=read_to, write=10.0, pool=5.0)
            try:
                with self._client.stream("POST", url, **stream_kwargs) as resp:
                    if resp.status_code != 200:
                        return resp.status_code, self._read_bounded_err_body(resp, secret_to_scrub=clean_key), "", ""

                    total_chars = 0
                    for line in resp.iter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        if not line:
                            continue
                        if len(line) > _MAX_GEMINI_SSE_LINE_CHARS:
                            raise RuntimeError("OpenAI API SSE line exceeds safety size limit")
                        total_chars += len(line)
                        if total_chars > _MAX_GEMINI_SSE_TOTAL_CHARS:
                            raise RuntimeError("OpenAI API SSE response exceeds safety size limit")
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            obj = json.loads(data_str)
                        except Exception:
                            continue
                        if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
                            err_obj = obj["error"]
                            err_type = str(err_obj.get("type") or err_obj.get("code") or "").strip()
                            err_msg = str(err_obj.get("message") or data_str).strip()
                            scrubbed = _scrub_secrets(
                                f"{err_type}: {err_msg}" if err_type else err_msg,
                                extra_secrets=(clean_key,),
                            )
                            low_err = f"{err_type} {err_msg}".lower()
                            if "rate_limit" in low_err or "too many requests" in low_err:
                                return 429, scrubbed, "", ""
                            if "overloaded" in low_err or "service_unavailable" in low_err:
                                return 503, scrubbed, "", ""
                            return 500, scrubbed, "", ""
                        choices = obj.get("choices") if isinstance(obj, dict) else None
                        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                            continue
                        fr = choices[0].get("finish_reason")
                        if fr:
                            finish_reason = str(fr)
                        delta_obj = choices[0].get("delta") or {}
                        if not isinstance(delta_obj, dict):
                            continue
                        ref = delta_obj.get("refusal")
                        if isinstance(ref, str) and ref:
                            refusal_parts.append(ref)
                        delta = self._extract_content_text(delta_obj.get("content"))
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
                    return 200, "", finish_reason, "".join(refusal_parts).strip()
            except httpx.HTTPError as exc:
                raise RuntimeError(_scrub_secrets(f"OpenAI API 스트리밍 오류: {exc}", extra_secrets=(clean_key,))) from None

        def _run_attempt(req_user_text: str) -> tuple[str, str]:
            nonlocal is_first
            msgs = _make_messages(req_user_text)
            with self._cache_lock:
                primary_model = self._model_fallback_map.get((key_hash, clean_model), clean_model)
            active_model = primary_model
            status_code = 0
            err_body = ""
            finish_reason = ""
            refusal_text = ""
            used_payload: dict[str, Any] = {}

            tried_models: set[str] = set()
            candidates_queue: list[str] = [primary_model]
            idx_q = 0
            while idx_q < len(candidates_queue):
                active_model = candidates_queue[idx_q]
                idx_q += 1
                if active_model in tried_models:
                    continue
                tried_models.add(active_model)
                payloads = self._build_compat_payloads(
                    model=active_model,
                    messages=msgs,
                    thinking_level=lvl,
                    base_max_tokens=base_max,
                    temperature=eff_temp,
                    stream=True,
                )
                for tier_idx, req_payload in self._ordered_compat_payloads(key_hash, active_model, lvl, True, payloads):
                    used_payload = req_payload
                    for retry_i in range(3):
                        splitter.reset()
                        emitted_parts.clear()
                        raw_full.clear()
                        is_first = True
                        status_code, err_body, finish_reason, refusal_text = _consume_sse(req_payload)
                        if (
                            status_code in (429, 503)
                            and retry_i < 2
                            and not emitted_parts
                            and (cancel_event is None or not cancel_event.is_set())
                        ):
                            self._log(
                                f"⏳ OpenAI API 일시 과부하/속도 제한 (HTTP {status_code}) → 재시도 ({retry_i + 1}/2)",
                                extra_secrets=(clean_key,),
                            )
                            if self._sleep_cancellable(0.8 * (retry_i + 1), cancel_event):
                                break
                            continue
                        break
                    if status_code == 200:
                        with self._cache_lock:
                            self._compat_tier_cache[(key_hash, active_model, lvl, True)] = tier_idx
                            if active_model != clean_model:
                                self._model_fallback_map[(key_hash, clean_model)] = active_model
                        break
                    if status_code != 400:
                        break
                if status_code == 200:
                    break
                if status_code == 404 and (cancel_event is None or not cancel_event.is_set()):
                    if active_model != clean_model:
                        with self._cache_lock:
                            if self._model_fallback_map.get((key_hash, clean_model)) == active_model:
                                self._model_fallback_map.pop((key_hash, clean_model), None)
                    next_fbs = self._get_404_fallback_candidates(active_model, exclude=tried_models)
                    if next_fbs:
                        fb_model = next_fbs[0]
                        self._log(
                            f"🔄 OpenAI API 모델 자동 전환: {active_model} → {fb_model} (HTTP 404)",
                            extra_secrets=(clean_key,),
                        )
                        for cand_fb in next_fbs:
                            if cand_fb not in candidates_queue:
                                candidates_queue.append(cand_fb)
                        continue
                break

            if status_code != 200:
                safe_err = _scrub_secrets(err_body[:240], extra_secrets=(clean_key,))
                raise RuntimeError(f"OpenAI API HTTP {status_code}: {safe_err}")

            if refusal_text and not "".join(raw_full).strip():
                safe_ref = _scrub_secrets(refusal_text[:180], extra_secrets=(clean_key,))
                raise RuntimeError(f"OpenAI API가 응답을 거부했습니다 (refusal): {safe_ref}")

            # Retry once with higher token budget / lower effort if reasoning exhausted max_completion_tokens
            if (
                not "".join(raw_full).strip()
                and finish_reason == "length"
                and (cancel_event is None or not cancel_event.is_set())
            ):
                splitter.reset()
                emitted_parts.clear()
                raw_full.clear()
                is_first = True
                retry_payload = dict(used_payload)
                retry_payload.pop("temperature", None)
                cur_budget = int(
                    retry_payload.get("max_completion_tokens") or retry_payload.get("max_tokens") or base_max
                )
                if "max_completion_tokens" in retry_payload:
                    retry_payload["max_completion_tokens"] = max(cur_budget * 2, 4096)
                else:
                    retry_payload["max_tokens"] = max(cur_budget * 2, 3072)
                if self._is_reasoning_model(active_model):
                    retry_payload["reasoning_effort"] = "none" if self._supports_reasoning_none(active_model) else "low"
                r_sc, r_err, r_fin, r_ref = _consume_sse(retry_payload)
                if r_sc == 400 and retry_payload.get("reasoning_effort") == "none":
                    retry_payload["reasoning_effort"] = "low"
                    r_sc, r_err, r_fin, r_ref = _consume_sse(retry_payload)
                if r_sc != 200:
                    safe_err = _scrub_secrets(r_err[:240], extra_secrets=(clean_key,))
                    raise RuntimeError(f"OpenAI API HTTP {r_sc}: {safe_err}")
                if r_ref and not "".join(raw_full).strip():
                    safe_ref = _scrub_secrets(r_ref[:180], extra_secrets=(clean_key,))
                    raise RuntimeError(f"OpenAI API가 응답을 거부했습니다 (refusal): {safe_ref}")
                if not "".join(raw_full).strip() and r_fin == "length":
                    raise RuntimeError("OpenAI API 추론 토큰 한도 초과로 빈 응답이 반환되었습니다 (finish_reason=length).")

            if cancel_event is not None and cancel_event.is_set():
                splitter.reset()
                return "", ""

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

        try:
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

            cancelled = cancel_event is not None and cancel_event.is_set()
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
            if full_model_text and (not failed_lang_lock) and (not cancelled):
                hist_user = (
                    history_user_text
                    if history_user_text is not None
                    else _strip_turn_wrapper_for_history(text)
                ).strip() or text
                with self._lock:
                    if self._chat_epoch == chat_epoch:
                        self._history = (
                            prev_history[-22:]
                            + [
                                {"role": "user", "content": hist_user},
                                {"role": "assistant", "content": full_model_text},
                            ]
                        )[-24:]
            return "" if cancelled else emitted_text
        finally:
            splitter.reset()

    def close(self) -> None:
        with self._client_lock:
            client = self._client_obj
            if self._own_client and client is not None:
                self._client_obj = None
                try:
                    client.close()
                except Exception:
                    pass


class GeminiSessionManager:
    """Unified multi-provider (Gemini / Claude / ChatGPT) facade switching between Browser and Official API."""

    def __init__(
        self,
        app_root: Path | None = None,
        http_client: httpx.Client | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> None:
        self.browser = BrowserGeminiBackend(app_root=app_root, log_cb=log_cb)
        self.api = OfficialGeminiApiBackend(http_client=http_client, log_cb=log_cb)
        self.claude_api = OfficialClaudeApiBackend(http_client=http_client, log_cb=log_cb)
        self.openai_api = OfficialOpenAIApiBackend(http_client=http_client, log_cb=log_cb)
        self._primed_signature = ""
        self._primed_bilingual = False

    def get_api_backend(self, provider: str = "gemini") -> Any:
        p = normalize_ai_provider(provider)
        if p == "claude":
            return self.claude_api
        if p == "chatgpt":
            return self.openai_api
        return self.api

    def abort_active_prompt(self) -> None:
        """Cooperatively abort any in-flight browser prompt, boot, navigation, or model sync."""
        self.browser.abort_active_prompt()

    def is_provider_active(self, provider: str) -> bool:
        """Lock-free check whether the isolated browser is already running for `provider`."""
        return self.browser.is_provider_active(provider)

    def ensure_ready_and_check_login(
        self,
        show_if_ready: bool = False,
        allow_interactive_login: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, str]:
        if cancel_event is not None:
            try:
                return self.browser.ensure_ready_and_check_login(
                    show_if_ready=show_if_ready,
                    allow_interactive_login=allow_interactive_login,
                    cancel_event=cancel_event,
                )
            except TypeError:
                pass
        return self.browser.ensure_ready_and_check_login(
            show_if_ready=show_if_ready,
            allow_interactive_login=allow_interactive_login,
        )

    def complete_interactive_login(self, cancel_event: threading.Event | None = None) -> tuple[bool, str]:
        if cancel_event is not None:
            try:
                return self.browser.complete_interactive_login(cancel_event=cancel_event)
            except TypeError:
                pass
        return self.browser.complete_interactive_login()

    def start_new_chat(self, force: bool = False, cancel_event: threading.Event | None = None) -> None:
        if cancel_event is not None:
            try:
                if force:
                    self.browser.start_new_chat(force=True, cancel_event=cancel_event)
                else:
                    self.browser.start_new_chat(cancel_event=cancel_event)
                return
            except TypeError:
                pass
        if force:
            try:
                self.browser.start_new_chat(force=True)
                return
            except TypeError:
                pass
        self.browser.start_new_chat()

    def set_provider(self, provider: str, browser_model: str | None = None) -> bool:
        """Switch active AI provider across browser session and invalidate primed state if changed."""
        changed = self.browser.set_provider(provider, model_code=browser_model)
        if changed:
            self.invalidate_session()
        return changed

    def reload_selectors(self) -> dict[str, Any]:
        """Reload selectors from `app_paths.get_selectors_path()` without touching BrowserProfile."""
        return self.browser.reload_selectors()

    def invalidate_session(self) -> None:
        """Mark any primed chat session as stale so the next turn opens a fresh conversation."""
        self._primed_signature = ""
        self._primed_bilingual = False
        self.browser.reset_thread_turn_count()

    def reset_conversation(self, backend_mode: str, system_prompt: str = "", provider: str = "gemini") -> None:
        p = normalize_ai_provider(provider)
        self.invalidate_session()
        if backend_mode == "api":
            self.get_api_backend(p).start_new_chat(system_prompt)
        else:
            self.browser.set_provider(p)
            self.browser.start_new_chat()

    def _persona_signature(
        self,
        backend_mode: str,
        persona_prompt: str,
        use_japanese_lock: bool,
        ko_style_guide: str,
        jp_beginner_coach: bool = False,
        use_korean_lock: bool = False,
        provider: str = "gemini",
        persona_variant: str = "official",
        variant_directive: str = "",
        character_dna_block: str = "",
    ) -> str:
        p = normalize_ai_provider(provider)
        pv = str(persona_variant or "official").strip().lower()
        clean_dna = (character_dna_block or "").strip()
        raw = (
            f"{p}|{backend_mode}|{pv}|{int(bool(use_japanese_lock))}|{int(bool(use_korean_lock))}|"
            f"{int(bool(jp_beginner_coach))}|{persona_prompt.strip()}|{clean_dna}|{variant_directive.strip()}|{ko_style_guide.strip()}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

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
        provider: str = "gemini",
        persona_variant: str = "official",
        variant_directive: str = "",
        opening_instruction: str = "",
        temperature: float | None = None,
        character_dna_block: str = "",
    ) -> str:
        p = normalize_ai_provider(provider)
        eff_ko_lock = bool(use_korean_lock and not use_japanese_lock)
        eff_coach = bool(jp_beginner_coach and not eff_ko_lock)
        clean_memory = (memory_context or "").strip()
        default_temp = get_llm_temperature_for_variant(persona_variant)
        eff_temp = (
            _coerce_finite_float(temperature, default_temp, low=0.1, high=1.25)
            if temperature is not None
            else default_temp
        )
        full_sys = build_first_message_prompt(
            persona_prompt,
            use_japanese_lock=use_japanese_lock,
            bilingual_inline=bilingual_inline,
            ko_style_guide=ko_style_guide,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
            memory_context=clean_memory,
            variant_directive=variant_directive,
            character_dna_block=character_dna_block,
        )
        sig = self._persona_signature(
            backend_mode,
            persona_prompt,
            use_japanese_lock,
            ko_style_guide,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
            provider=p,
            persona_variant=persona_variant,
            variant_directive=variant_directive,
            character_dna_block=character_dna_block,
        )
        # Invalidate prior signature until the new greeting succeeds so a failed/cancelled prime never skips persona setup
        self._primed_signature = ""
        self._primed_bilingual = False

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

        opening_user = (opening_instruction or "").strip() or build_variant_opening_instruction(
            persona_variant,
            reunion_hint=reunion_hint,
            lang_hint=lang_hint,
            jp_coach=eff_coach,
        )

        if backend_mode == "api":
            api_backend = self.get_api_backend(p)
            api_backend.start_new_chat(full_sys)
            out = api_backend.send_and_stream(
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
                temperature=eff_temp,
            )
            if out and (cancel_event is None or not cancel_event.is_set()):
                self._primed_signature = sig
                self._primed_bilingual = bool(bilingual_inline)
            return out

        self.browser.set_provider(p, model_code=browser_model)
        self.start_new_chat(cancel_event=cancel_event)
        first_msg = f"{full_sys}\n\n{opening_user}"
        out = self.browser.send_and_stream(
            first_msg,
            on_sentence=on_sentence,
            cancel_event=cancel_event,
            browser_model=browser_model,
            thinking_level=thinking_level,
            bilingual_inline=bilingual_inline,
            on_sentence_pair=on_sentence_pair,
            use_japanese_lock=use_japanese_lock,
            use_korean_lock=eff_ko_lock,
            is_prime_turn=True,
        )
        if out and (cancel_event is None or not cancel_event.is_set()):
            self._primed_signature = sig
            self._primed_bilingual = bool(bilingual_inline)
        return out

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
        provider: str = "gemini",
        persona_variant: str = "official",
        variant_directive: str = "",
        persona_anchor: str = "",
        temperature: float | None = None,
        character_dna_block: str = "",
        persona_refresh_block: str = "",
        image_bytes: bytes | None = None,
        image_mime: str = "image/jpeg",
        study_visual_cb: Callable[[dict[str, Any]], None] | None = None,
        enable_study_visual_filter: bool = False,
    ) -> str:
        p = normalize_ai_provider(provider)
        eff_ko_lock = bool(use_korean_lock and not use_japanese_lock)
        eff_coach = bool(jp_beginner_coach and not eff_ko_lock)
        eff_image_bytes = image_bytes if p == "gemini" else None
        eff_image_mime = image_mime if p == "gemini" else "image/jpeg"
        default_temp = get_llm_temperature_for_variant(persona_variant)
        eff_temp = (
            _coerce_finite_float(temperature, default_temp, low=0.1, high=1.25)
            if temperature is not None
            else default_temp
        )
        full_sys = build_first_message_prompt(
            persona_prompt,
            use_japanese_lock=use_japanese_lock,
            bilingual_inline=bilingual_inline,
            ko_style_guide=ko_style_guide,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
            memory_context=(memory_context or "").strip(),
            variant_directive=variant_directive,
            character_dna_block=character_dna_block,
        )
        sig = self._persona_signature(
            backend_mode,
            persona_prompt,
            use_japanese_lock,
            ko_style_guide,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
            provider=p,
            persona_variant=persona_variant,
            variant_directive=variant_directive,
            character_dna_block=character_dna_block,
        )
        wrapped_user = wrap_user_turn_for_japanese_lock(
            user_text,
            use_japanese_lock=use_japanese_lock,
            bilingual_inline=bilingual_inline,
            jp_beginner_coach=eff_coach,
            use_korean_lock=eff_ko_lock,
            persona_anchor=persona_anchor,
        )
        clean_history_user = (user_text or "").strip() or wrapped_user

        if backend_mode == "api":
            api_backend = self.get_api_backend(p)
            needs_prime = self._primed_signature != sig
            if needs_prime:
                api_backend.start_new_chat(full_sys)
            elif self._primed_bilingual != bool(bilingual_inline):
                api_backend.update_system_prompt(full_sys)
            api_kwargs: dict[str, Any] = {
                "api_key": api_key,
                "model": api_model,
                "system_prompt": full_sys,
                "text": wrapped_user,
                "on_sentence": on_sentence,
                "cancel_event": cancel_event,
                "thinking_level": thinking_level,
                "bilingual_inline": bilingual_inline,
                "on_sentence_pair": on_sentence_pair,
                "use_japanese_lock": use_japanese_lock,
                "use_korean_lock": eff_ko_lock,
                "temperature": eff_temp,
                "history_user_text": clean_history_user,
            }
            if p == "gemini":
                api_kwargs["image_bytes"] = eff_image_bytes
                api_kwargs["image_mime"] = eff_image_mime
                api_kwargs["study_visual_cb"] = study_visual_cb
                api_kwargs["enable_study_visual_filter"] = enable_study_visual_filter
            out = api_backend.send_and_stream(**api_kwargs)
            if out and (cancel_event is None or not cancel_event.is_set()):
                self._primed_signature = sig
                self._primed_bilingual = bool(bilingual_inline)
            return out

        self.browser.set_provider(p, model_code=browser_model)
        payload = wrapped_user
        thread_lost = (self._primed_signature == sig) and (not self.browser.has_active_thread())
        needs_prime = (self._primed_signature != sig) or thread_lost
        bilingual_switched = (not needs_prime) and (self._primed_bilingual != bool(bilingual_inline))
        if needs_prime:
            if not thread_lost:
                self.start_new_chat(cancel_event=cancel_event)
            payload = f"{full_sys}\n\n{wrapped_user}"
        elif bilingual_switched:
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
            elif use_japanese_lock:
                switch_rule = JAPANESE_VOICE_OUTPUT_LOCK
            else:
                switch_rule = ""
            payload = f"{switch_rule}\n\n{wrapped_user}" if switch_rule else wrapped_user
        out = self.browser.send_and_stream(
            payload,
            on_sentence=on_sentence,
            cancel_event=cancel_event,
            browser_model=browser_model,
            thinking_level=thinking_level,
            bilingual_inline=bilingual_inline,
            on_sentence_pair=on_sentence_pair,
            use_japanese_lock=use_japanese_lock,
            use_korean_lock=eff_ko_lock,
            persona_refresh_block=persona_refresh_block,
            persona_anchor_fallback=persona_anchor,
            is_prime_turn=needs_prime,
            image_bytes=eff_image_bytes,
            image_mime=eff_image_mime,
            study_visual_cb=study_visual_cb,
            enable_study_visual_filter=enable_study_visual_filter,
        )
        if out and (cancel_event is None or not cancel_event.is_set()):
            self._primed_signature = sig
            self._primed_bilingual = bool(bilingual_inline)
        return out

    def run_background_task(
        self,
        backend_mode: str,
        api_key: str,
        api_model: str,
        prompt: str,
        browser_model: str = "hydrogen",
        thinking_level: str = "low",
        timeout_sec: float = 45.0,
        provider: str = "gemini",
    ) -> str:
        """
        Run a stateless background task (e.g., 3-stage memory summarization) using whichever
        AI provider/backend is active — official API or hidden 2nd browser CDP tab ($0 extra cost).
        """
        p = normalize_ai_provider(provider)
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return ""
        clean_key = (api_key or "").strip()
        if backend_mode == "api" or (clean_key and not self.browser.is_running()):
            if not clean_key:
                return ""
            return self.get_api_backend(p).generate_background_text(
                api_key=clean_key,
                model=api_model,
                prompt=clean_prompt,
                thinking_level=thinking_level,
                timeout_sec=timeout_sec,
            )
        if self.browser.provider != p:
            return ""
        return self.browser.run_background_prompt(
            prompt=clean_prompt,
            model_code=browser_model,
            thinking_level=thinking_level,
            timeout_sec=timeout_sec,
            fresh_chat=True,
        )

    def close(self) -> None:
        self.abort_active_prompt()
        self.browser.close()
        self.api.close()
        self.claude_api.close()
        self.openai_api.close()
