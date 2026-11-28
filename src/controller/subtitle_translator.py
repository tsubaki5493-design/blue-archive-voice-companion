"""
Real-Time Zero-Cost Japanese -> Korean Subtitle Translator & Formatter
-----------------------------------------------------------------------
Preserves 100% pure Japanese Fish Audio voice output with ZERO added TTS latency
by translating dialogue asynchronously in parallel with Fish Audio synthesis,
enhanced with a Blue Archive proper-noun glossary and punctuation normalization.
"""

from __future__ import annotations

import inspect
import json
import re
import threading
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from src.controller.gemini_connector import (
    CHATGPT_API_MODELS,
    CHATGPT_BROWSER_MODELS,
    CLAUDE_API_MODELS,
    CLAUDE_BROWSER_MODELS,
    GEMINI_API_MODELS,
    GEMINI_BROWSER_MODELS,
    normalize_ai_provider,
)
from src.controller.ko_speech_profiles import (
    _sanitize_for_banmal,
    extract_ko_speech_profile,
)

SUBTITLE_MODES: tuple[str, ...] = ("jp_only", "kr_only", "jp_kr")

SUBTITLE_MODE_LABELS: dict[str, str] = {
    "jp_only": "일본어 원문",
    "kr_only": "한글 자막",
    "jp_kr": "일본어 원문 + 한글 자막",
}


_RE_STRIP_EMOJI_PREFIX = re.compile(r"^[^\w\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7a3]+\s*")


def get_subtitle_engine_choices(
    extra_api_models: list[str] | None = None,
    extra_web_models: list[tuple[str, str]] | None = None,
    web_models: list[tuple[str, str]] | None = None,
    provider: str = "gemini",
) -> list[tuple[str, str]]:
    """Build complete subtitle translation model choices for the active AI provider + fast NMT."""
    p = normalize_ai_provider(provider)
    choices: list[tuple[str, str]] = [
        ("auto_inline", "✨ 자동 동시번역 (기본 · 지연 0초)"),
    ]
    seen_codes: set[str] = {"auto_inline"}

    if p == "claude":
        web_prefix, web_tag = "claude_web:", "🌐 클로드 웹 "
        api_prefix, api_tag = "claude_api:", "⚡ Claude API "
        default_web = list(CLAUDE_BROWSER_MODELS)
        default_api = list(CLAUDE_API_MODELS)
    elif p == "chatgpt":
        web_prefix, web_tag = "chatgpt_web:", "🌐 챗지피티 웹 "
        api_prefix, api_tag = "openai_api:", "⚡ OpenAI API "
        default_web = list(CHATGPT_BROWSER_MODELS)
        default_api = list(CHATGPT_API_MODELS)
    else:
        web_prefix, web_tag = "web:", "🌐 웹 "
        api_prefix, api_tag = "api:", "⚡ API "
        default_web = list(GEMINI_BROWSER_MODELS)
        default_api = list(GEMINI_API_MODELS)

    base_web = list(web_models) if web_models else default_web
    for code, lbl in base_web:
        short = _RE_STRIP_EMOJI_PREFIX.sub("", lbl.split("(", 1)[0]).strip()
        if not short:
            short = lbl.strip()
        eng_code = f"{web_prefix}{code}"
        if eng_code not in seen_codes:
            seen_codes.add(eng_code)
            choices.append((eng_code, f"{web_tag}{short}"))

    for code, lbl in extra_web_models or []:
        eng_code = f"{web_prefix}{code}"
        if eng_code not in seen_codes:
            seen_codes.add(eng_code)
            choices.append((eng_code, lbl if lbl.startswith("🌐") else f"🌐 {lbl}"))

    for code, _lbl in default_api:
        eng_code = f"{api_prefix}{code}"
        if eng_code not in seen_codes:
            seen_codes.add(eng_code)
            choices.append((eng_code, f"{api_tag}{code}"))

    for m in extra_api_models or []:
        clean_m = (m or "").strip()
        if not clean_m:
            continue
        eng_code = f"{api_prefix}{clean_m}"
        if eng_code not in seen_codes:
            seen_codes.add(eng_code)
            choices.append((eng_code, f"{api_tag}{clean_m}"))

    choices.append(("fast_nmt", "🚀 초고속 NMT 전용 (~0.15초 · AI 미사용)"))
    return choices


SUBTITLE_ENGINES: list[tuple[str, str]] = get_subtitle_engine_choices()

SUBTITLE_ENGINE_LABELS: dict[str, str] = dict(SUBTITLE_ENGINES)


def resolve_subtitle_engine_label(code: str, choices: list[tuple[str, str]] | None = None) -> str:
    """Return user-facing dropdown label for any stored `subtitle_engine` code."""
    clean_code = str(code or "").strip()
    mapping = dict(choices or SUBTITLE_ENGINES)
    if clean_code in mapping:
        return mapping[clean_code]
    if clean_code.startswith("claude_api:"):
        return f"⚡ Claude API {clean_code[11:]}"
    if clean_code.startswith("claude_web:"):
        return f"🌐 클로드 웹 {clean_code[11:]}"
    if clean_code.startswith("openai_api:"):
        return f"⚡ OpenAI API {clean_code[11:]}"
    if clean_code.startswith("chatgpt_web:"):
        return f"🌐 챗지피티 웹 {clean_code[12:]}"
    if clean_code.startswith("api:"):
        return f"⚡ API {clean_code[4:]}"
    if clean_code.startswith("web:"):
        return f"🌐 웹 {clean_code[4:]}"
    return mapping.get("auto_inline", "✨ 자동 동시번역 (기본 · 지연 0초)")

_RE_CLEAN_BASE_REG_NOTE = re.compile(r"※[^）)\n]*")
_RE_CLEAN_BASE_REG = re.compile(
    r"(?:タメ口|敬語|丁寧語?|「?です・?ます」?)[^。、）\)]*(?:禁止|厳禁|不使用|使わない|使わず)"
)
_RE_CLEAN_BASE_REG_COND = re.compile(
    r"(?:ただし|但し|焦ると|感情が昂ぶると|内心では|独り言では)[^。]*"
)
_RE_JP_NAME_SPLIT = re.compile(r"[\s・]+")

_RE_STUTTER_PREFIX = re.compile(r"([\u3040-\u309f\u30a0-\u30ff]{1,3})[、,]\s*(?=\1)")

_RE_JAPANESE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff]")
_RE_KATAKANA_ONLY = re.compile(r"^[\u30a0-\u30ffー]+$")
_RE_TRAIL_KATA = re.compile(r"([\u30a0-\u30ffー]{2,})$")
_RE_PARENS = re.compile(r"[（(][^）)]*[）)]")

_RE_EXC_QUEST = re.compile(r"!\s*\?")
_RE_QUEST_EXC = re.compile(r"\?\s*!")
_RE_SPACE_BEFORE_PUNCT = re.compile(r"\s+([!?.,…])")
_RE_MANY_DOTS = re.compile(r"\.{4,}")
_RE_MULTI_SPACE = re.compile(r"[ \t]{2,}")

_CORE_GLOSSARY: dict[str, str] = {
    "シャーレ": "샬레",
    "キヴォトス": "키보토스",
    "便利屋68": "흥신소 68",
    "対策委員会": "대책위원회",
    "風紀委員会": "선도부",
    "ゲーム開発部": "게임개발부",
    "補習授業部": "보충수업부",
    "美食研究会": "미식연구회",
    "放課後スイーツ部": "방과후 디저트부",
    "正義実現委員会": "정의실현부",
    "シスターフッド": "시스터후드",
    "連邦生徒会": "총학생회",
    "セミナー": "세미나",
    "ヴェリタス": "베리타스",
    "エンジニア部": "엔지니어부",
    "モモトーク": "모모톡",
    "トリニティ": "트리니티",
    "ゲヘナ": "게헨나",
    "ミレニアム": "밀레니엄",
    "アビドス": "아비도스",
    "百鬼夜行": "백귀야행",
    "山海経": "산해경",
    "レッドウィンター": "붉은겨울",
    "ヴァルキューレ": "발키리",
    "アリウス": "아리우스",
    "ワイルドハント": "와일드헌트",
    "ハイランダー": "하이랜더",
    "玄龍門": "현룡문",
    "玄武商会": "현무상회",
    "百花繚乱": "백화요란",
    "忍術研究部": "인법연구부",
    "陰陽部": "음양부",
    "修行部": "수행부",
    "梅花園": "매화원",
    "錬丹術研究会": "연단방",
    "ティーパーティー": "티파티",
    "万魔殿": "판데모니움 소사이어티",
    "温泉開発部": "온천개발부",
    "給食部": "급양부",
    "救急医学部": "응급의학부",
    "シッテムの箱": "싯딤의 상자",
    "アリウススクワッド": "아리우스 스쿼드",
    "RABBIT小隊": "RABBIT 소대",
    "FOX小隊": "FOX 소대",
    "生活安全局": "생활안전국",
    "公安局": "공안국",
    "227号特別クラス": "227호 특별반",
    "デカグラマトン": "데카그라마톤",
    "七囚人": "일곱 죄수",
    "ご主人様": "주인님",
    "主殿": "주군",
    "あるじどの": "주군",
    "あなた様": "당신님",
    "カムラッド": "동무",
    "身共": "소인",
    "其方": "그대",
    "先生殿": "선생 공",
    "手前様": "나으리",
    "ぼく様": "이 몸님",
    "トレーナー": "트레이너",
    "マスター": "마스터",
    "先生": "선생님",
    "先輩": "선배",
}

_INTERJECTION_GLOSSARY: dict[str, str] = {
    "パンパカパーン": "빠밤빠밤",
    "パヒャヒャッ": "파햐햣",
    "パヒャッ": "파햣",
    "パヒャ": "파햐",
    "ごきげんよう": "평안하신지요",
    "ごめんあそばせ": "실례하겠어요",
    "ピース、ピース": "피스, 피스",
    "ぴーすぴーす": "피스 피스",
    "おーほほっ": "오호홋",
    "じゃじゃーん": "짜잔",
    "じゃーん": "짜잔",
    "ふっふーん": "흐흥",
    "うははは": "우하하하",
    "あっはは": "아하하",
    "うへへ〜": "우헤헤~",
    "うへへ": "우헤헤",
    "うへ〜": "우헤~",
    "ニンニン": "닌닌",
    "にんにん": "닌닌",
    "にゅふふ": "뉴후후",
    "にゃはは": "냐하하",
    "きひひっ": "키히힛",
    "わっぴ〜": "왓삐~",
    "わっぴー": "왓삐~",
    "えっへん": "에헴",
    "イヒヒ": "이히히",
    "イヒッ": "이힛",
    "にはは": "니하하",
    "メーッ": "메엣",
    "やっほー": "야호",
    "やっほ〜": "야호~",
    "ふぁ〜": "흐아~",
    "むにゃ": "음냐",
    "ふふっ": "후훗",
    "くふふ": "쿠후후",
    "へへっ": "헤헷",
    "にひひ": "니히히",
    "うふふ": "우후후",
    "むふふ": "무후후",
    "きゃっ": "꺄악",
    "ひゃっ": "햣",
    "せんせー": "선생님",
    "せんせ": "선생",
    "もう、": "정말, ",
    "あの、": "저기, ",
    "えっと、": "음, ",
    "えーっと": "그러니까",
    "うそっ": "거짓말",
    "まじで": "진짜로",
    "ふふん": "흐흥",
    "えへへ": "에헤헤",
    "てへぺろ": "데헤페로",
    "うぅ": "으으",
    "むぅ": "무으",
}

_PRIMARY_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
_FALLBACK_TRANSLATE_URL = "https://clients5.google.com/translate_a/t"
_ALLOWED_TRANSLATE_HOSTS: tuple[str, ...] = ("translate.googleapis.com", "clients5.google.com")
_MAX_TRANSLATE_QUERY_CHARS = 4000
_MAX_TRANSLATE_RESPONSE_BYTES = 256 * 1024  # 256 KiB cap per subtitle response
_RE_CTRL_OR_WS = re.compile(r"[\x00-\x20\x7f]")


def _validate_translate_url(url: str) -> str:
    """Enforce strict HTTPS-only requests to allowed Google Translate endpoints."""
    raw = str(url or "").strip()
    if not raw or _RE_CTRL_OR_WS.search(raw):
        raise ValueError("Invalid translate URL")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid translate URL") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("Refusing non-HTTPS translate endpoint")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Refusing credentials in translate URL")
    if (parsed.hostname or "").lower() not in _ALLOWED_TRANSLATE_HOSTS:
        raise ValueError("Refusing untrusted translate host")
    if port not in (None, 443):
        raise ValueError("Refusing non-standard port for translate endpoint")
    return raw


@dataclass
class SubtitleEntry:
    """Structured real-time dialogue subtitle line synced to audio playback."""

    entry_id: int
    char_id: str
    char_name_kr: str
    char_name_jp: str
    jp_text: str
    kr_text: str = ""
    is_turn_start: bool = True
    pending: bool = False
    refining: bool = False


def build_character_ko_style_guide(
    char_data: dict[str, Any] | None,
    jp_beginner_coach: bool = False,
    persona_variant: str = "official",
) -> str:
    """Build a rich, character-specific Korean dialogue style guide for LLM subtitles."""
    vc = str(persona_variant or "official").strip().lower()
    variant_tone_notes = {
        "official": "- 현재 해석: 공식 원작 100% — 과장된 패러디나 임의 변형 없이 블루 아카이브 한국어판 공식 대사 어조 그대로 번역하라.",
        "fanon_mainstream": "- 현재 해석: 2-A 주류 종합 — 공식 말투 뼈대 위에 친근한 2차창작 티키타카와 자연스러운 설렘이 살아나게 번역하라.",
        "fanon_pure_love": "- 현재 해석: 2-B 고습도 순애 — 공식 말투 뼈대는 유지하되 다정하고 촉촉한 거리감·애정·은근한 독점 뉘앙스가 살아나게 번역하라.",
        "fanon_spicy_meme": "- 현재 해석: 2-C 매운맛 개그 — 공식 말투 뼈대는 유지하되 억울한 해명·자폭·유쾌한 딴죽·밈 개그 리듬이 톡톡 튀게 번역하라.",
        "fanon_optimal_blend": "- 현재 해석: 2-D 최적 배합 — 공식 말투 뼈대 속에서 일상↔밈↔순애 뉘앙스 전환이 자연스럽게 느껴지게 번역하라.",
    }
    variant_line = variant_tone_notes.get(vc, variant_tone_notes["official"])
    coach_line = (
        "- 서툰 일본어 배려 모드: 캐릭터 고유 말투(반말/존댓말)와 자연스러운 일상 회화 톤을 유지하고 선생처럼 가르치는 어조로 바꾸지 마라. "
        "사용자가 물어보거나 문맥상 `「...」` 일본어 표현이 대사에 직접 언급된 경우에만 한국어 자막에 `'한국어 발음' [뜻]` 형식으로 적고 전각 괄호 `（...）`는 쓰지 마라."
        if jp_beginner_coach
        else ""
    )
    if not isinstance(char_data, dict) or not char_data:
        base = f"[캐릭터 한국어 자막 번역 가이드]\n- 일본어 원문 대사를 자연스럽고 생동감 있는 한국어 구어체 자막으로 번역하라.\n{variant_line}"
        return f"{base}\n{coach_line}" if coach_line else base

    profile = extract_ko_speech_profile(char_data)
    name_kr = profile["name_kr"]
    raw_persona = char_data.get("persona")
    persona = raw_persona if isinstance(raw_persona, dict) else {}
    raw_sm = char_data.get("speech_mechanics_jp")
    if not isinstance(raw_sm, dict):
        raw_sm = persona.get("speech_mechanics_jp")
    sm = raw_sm if isinstance(raw_sm, dict) else {}
    base_reg = str(sm.get("base_register") or "")
    speech_mode = str(sm.get("speech_mode") or "").strip().lower()
    summary = str(persona.get("summary") or "").strip()

    clean_base_reg = _RE_CLEAN_BASE_REG_NOTE.sub("", base_reg)
    clean_base_reg = _RE_CLEAN_BASE_REG.sub("", clean_base_reg)
    clean_base_reg = _RE_CLEAN_BASE_REG_COND.sub("", clean_base_reg)
    if "tameguchi" in speech_mode:
        polarity = "banmal"
    elif "keigo" in speech_mode:
        polarity = "jondaetmal"
    elif "タメ口" in clean_base_reg or "반말" in summary:
        polarity = "banmal"
    elif any(k in clean_base_reg for k in ("敬語", "丁寧", "です", "ます")) or "존댓말" in summary:
        polarity = "jondaetmal"
    else:
        polarity = profile.get("polarity", "mixed")

    register_kr = profile["register_kr"]
    if polarity == "banmal":
        reg_rule = (
            f"번역 자막 말투는 반드시 캐릭터 고유의 한국어 반말 어조로 번역하라 — {register_kr}"
        )
    elif polarity == "jondaetmal":
        reg_rule = (
            f"번역 자막 말투는 반드시 캐릭터 고유의 한국어 존댓말 어조로 번역하라 — {register_kr}"
        )
    else:
        reg_rule = (
            f"번역 자막 말투는 캐릭터 성격과 호칭에 맞는 자연스러운 한국어 구어체로 번역하라 — {register_kr}"
        )

    lines = [
        f"[캐릭터 한국어 자막 번역 가이드: {name_kr}]",
        f"- 화자: {name_kr} | 한국어 1인칭: {profile['first_person_kr']} | 상대방 호칭: {profile['sensei_address_kr']}",
        f"- {reg_rule}",
    ]
    if profile["signature_endings_kr"]:
        lines.append("- 대표 한국어 어미: " + " / ".join(profile["signature_endings_kr"][:5]))
    if profile["interjections_kr"]:
        lines.append("- 고유 감탄사·말버릇: " + " / ".join(profile["interjections_kr"][:5]))
    if profile["forbidden_kr"]:
        lines.append("- 자막 금지 표현: " + " | ".join(profile["forbidden_kr"][:3]))
    lines.append(variant_line)
    if base_reg:
        lines.append(f"- 원문 어조 참고: {base_reg[:160]}")
    if profile["sample_lines_kr"]:
        lines.append(f'- 한국어 번역 감각 참고(복사 금지): "{profile["sample_lines_kr"][0]}"')
    if summary and vc == "official":
        # Keep official mode grounded without injecting fanon summary lines into 2-A~2-D
        lines.append(f"- 기본 캐릭터 참고: {summary[:140]}")

    body = "\n".join(lines)
    if polarity == "banmal":
        body = _sanitize_for_banmal(body)
    if coach_line:
        body = f"{body}\n{coach_line}"
    return body


def contains_japanese(text: str) -> bool:
    """Return True when text contains Hiragana, Katakana, or Kanji."""
    return bool(_RE_JAPANESE.search(str(text or "")))


def _strip_parens(text: str) -> str:
    return _RE_PARENS.sub("", str(text or "")).strip()


def _extract_given_pair(name_jp: str, name_kr: str) -> tuple[str, str]:
    """Extract clean (given_jp, given_kr) when a full character name is provided."""
    clean_jp = _strip_parens(name_jp)
    clean_kr = _strip_parens(name_kr)
    if not clean_jp or not clean_kr:
        return "", ""

    kr_parts = [p for p in clean_kr.split() if p]
    given_kr = kr_parts[-1] if kr_parts else clean_kr

    jp_parts = [p for p in _RE_JP_NAME_SPLIT.split(clean_jp) if p]
    if len(jp_parts) >= 2:
        given_jp = jp_parts[-1]
    else:
        m = _RE_TRAIL_KATA.search(clean_jp)
        given_jp = m.group(1) if (m and len(m.group(1)) < len(clean_jp)) else ""

    return given_jp, given_kr


@lru_cache(maxsize=256)
def _compiled_katakana_term_re(jp_term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\u30a0-\u30ffー]){re.escape(jp_term)}(?![\u30a0-\u30ffー])")


def _replace_term(text: str, jp_term: str, kr_term: str) -> str:
    if not jp_term or not kr_term or jp_term not in text:
        return text
    if _RE_KATAKANA_ONLY.match(jp_term):
        # Guard katakana short names (e.g. リン inside プリン) with katakana boundaries
        return _compiled_katakana_term_re(jp_term).sub(kr_term, text)
    return text.replace(jp_term, kr_term)


def normalize_korean_subtitle(text: str) -> str:
    """Clean machine-translated punctuation spacing for readable subtitles."""
    out = str(text or "").strip()
    if not out:
        return ""
    out = _RE_EXC_QUEST.sub("!?", out)
    out = _RE_QUEST_EXC.sub("?!", out)
    out = _RE_SPACE_BEFORE_PUNCT.sub(r"\1", out)
    out = _RE_MANY_DOTS.sub("……", out)
    out = _RE_MULTI_SPACE.sub(" ", out)
    return out.strip()


class BlueArchiveGlossary:
    """Pre-substitution glossary for Blue Archive names, schools, clubs, and tics."""

    def __init__(self, characters: dict[str, dict[str, Any]] | None = None) -> None:
        self._lock = threading.Lock()
        self._sorted_pairs: list[tuple[str, str]] = []
        self._global_map: dict[str, str] = {}
        self._global_re: re.Pattern[str] | None = None
        self._char_terms_cache: dict[str, list[tuple[str, str]]] = {}
        self.update_characters(characters or {})

    def update_characters(self, characters: dict[str, dict[str, Any]]) -> None:
        merged: dict[str, str] = {}
        merged.update(_CORE_GLOSSARY)
        merged.update(_INTERJECTION_GLOSSARY)

        for c in (characters.values() if isinstance(characters, dict) else ()):
            if not isinstance(c, dict):
                continue
            raw_jp = _strip_parens(str(c.get("name_jp") or ""))
            raw_kr = _strip_parens(str(c.get("name_kr") or ""))
            if len(raw_jp) >= 3 and raw_kr:
                merged.setdefault(raw_jp, raw_kr)

            sch_jp = _strip_parens(str(c.get("school_jp") or ""))
            sch_kr = _strip_parens(str(c.get("school") or ""))
            if len(sch_jp) >= 3 and sch_kr and sch_jp not in {"なし", "その他"}:
                merged.setdefault(sch_jp, sch_kr)

        pairs = sorted(merged.items(), key=lambda kv: (-len(kv[0]), kv[0]))
        if pairs:
            alts = [
                (
                    rf"(?<![\u30a0-\u30ffー]){re.escape(jp)}(?![\u30a0-\u30ffー])"
                    if _RE_KATAKANA_ONLY.match(jp)
                    else re.escape(jp)
                )
                for jp, _ in pairs
            ]
            compiled_re: re.Pattern[str] | None = re.compile("|".join(alts))
        else:
            compiled_re = None

        with self._lock:
            self._sorted_pairs = pairs
            self._global_map = merged
            self._global_re = compiled_re
            self._char_terms_cache.clear()

    def _get_char_specific_terms(self, char_data: dict[str, Any]) -> list[tuple[str, str]]:
        if not isinstance(char_data, dict):
            return []
        cid = str(char_data.get("id") or "")
        if cid:
            with self._lock:
                cached = self._char_terms_cache.get(cid)
                if cached is not None:
                    return cached

        char_specific: list[tuple[str, str]] = []
        raw_jp = _strip_parens(str(char_data.get("name_jp") or ""))
        raw_kr = _strip_parens(str(char_data.get("name_kr") or ""))
        if len(raw_jp) >= 2 and raw_kr:
            char_specific.append((raw_jp, raw_kr))
        given_jp, given_kr = _extract_given_pair(raw_jp, raw_kr)
        if len(given_jp) >= 2 and given_kr:
            char_specific.append((given_jp, given_kr))

        club_jp = _strip_parens(str(char_data.get("club_jp") or ""))
        club_kr = _strip_parens(str(char_data.get("club") or ""))
        if len(club_jp) >= 2 and club_kr:
            char_specific.append((club_jp, club_kr))

        title_jp = _strip_parens(str(char_data.get("user_title_jp") or ""))
        title_kr = _strip_parens(str(char_data.get("user_title") or ""))
        if len(title_jp) >= 2 and title_kr:
            char_specific.append((title_jp, title_kr))

        char_specific.sort(key=lambda kv: (-len(kv[0]), kv[0]))
        if cid:
            with self._lock:
                self._char_terms_cache[cid] = char_specific
        return char_specific

    def prepare_for_translation(self, text: str, char_data: dict[str, Any] | None = None) -> str:
        """Substitute known Japanese Blue Archive proper nouns into Hangul prior to NMT."""
        result = str(text or "").strip()
        if not result:
            return ""

        if isinstance(char_data, dict) and char_data:
            for jp_term, kr_term in self._get_char_specific_terms(char_data):
                result = _replace_term(result, jp_term, kr_term)

        with self._lock:
            global_re = self._global_re
            global_map = self._global_map

        if global_re is not None:
            result = global_re.sub(lambda m: global_map[m.group(0)], result)

        # Collapse Japanese stutter repetitions like 'な、なんだ' before fast NMT
        result = _RE_STUTTER_PREFIX.sub("", result)
        return result


class RealtimeSubtitleTranslator:
    """Thread-safe, non-blocking, zero-API-cost JP->KO subtitle translator with LRU cache."""

    def __init__(
        self,
        characters: dict[str, dict[str, Any]] | None = None,
        cache_size: int = 512,
        http_client: httpx.Client | None = None,
        llm_translator: Callable[..., str] | None = None,
    ) -> None:
        self.glossary = BlueArchiveGlossary(characters)
        try:
            parsed_cache_size = int(cache_size) if not isinstance(cache_size, bool) else 512
        except Exception:
            parsed_cache_size = 512
        self._cache_size = max(32, parsed_cache_size)
        self._cache: OrderedDict[tuple[str, str], tuple[str, str]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._ctx_lock = threading.Lock()
        self._recent_context: deque[str] = deque(maxlen=6)
        self._state_lock = threading.Lock()
        self._owns_client = http_client is None
        self._http: httpx.Client | None = http_client
        self._http_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sub-ko")
        self._llm_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sub-llm")
        self._llm_translator: Callable[..., str] | None = None
        self._llm_accepts_level = True
        self.set_llm_translator(llm_translator)
        self._closed = False

    def _get_http_client(self) -> httpx.Client:
        with self._http_lock:
            if self._closed:
                raise RuntimeError("Subtitle translator is closed")
            if self._http is None:
                limits = httpx.Limits(max_keepalive_connections=4, max_connections=6, keepalive_expiry=120.0)
                self._http = httpx.Client(
                    verify=True,
                    follow_redirects=False,
                    transport=httpx.HTTPTransport(
                        verify=True,
                        local_address="0.0.0.0",
                        retries=1,
                        limits=limits,
                    ),
                    timeout=httpx.Timeout(2.5, connect=1.5, read=2.5, write=2.0, pool=2.0),
                    limits=limits,
                )
            return self._http

    def set_llm_translator(self, cb: Callable[..., str] | None) -> None:
        if cb is None:
            accepts_level = True
        else:
            try:
                sig = inspect.signature(cb)
                params = list(sig.parameters.values())
                accepts_level = len(params) >= 5 or any(
                    p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
                )
            except Exception:
                accepts_level = True
        with self._state_lock:
            self._llm_translator = cb
            self._llm_accepts_level = accepts_level

    def update_characters(self, characters: dict[str, dict[str, Any]]) -> None:
        self.glossary.update_characters(characters)

    def _cache_key(
        self,
        clean_text: str,
        char_data: dict[str, Any] | None,
        *,
        persona_variant: str = "official",
        jp_beginner_coach: bool = False,
    ) -> tuple[str, str]:
        cid = str(char_data.get("id") or "") if isinstance(char_data, dict) else ""
        vc = str(persona_variant or "official").strip().lower() or "official"
        if vc == "official" and not jp_beginner_coach:
            return cid, clean_text
        return f"{cid}|{vc}|{'coach' if jp_beginner_coach else 'std'}", clean_text

    def _cache_get_entry(self, key: tuple[str, str]) -> tuple[str, str] | None:
        with self._cache_lock:
            val = self._cache.get(key)
            if val is not None:
                self._cache.move_to_end(key)
            return val

    def _cache_get(self, key: tuple[str, str]) -> str | None:
        entry = self._cache_get_entry(key)
        return entry[0] if entry is not None else None

    def _cache_put(self, key: tuple[str, str], val: str, quality: str = "fast") -> None:
        with self._cache_lock:
            prev = self._cache.get(key)
            if prev is not None and prev[1].startswith("llm:") and not quality.startswith("llm:"):
                return
            self._cache[key] = (val, quality)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def record_dialogue_pair(
        self,
        jp_text: str,
        kr_text: str,
        char_data: dict[str, Any] | None = None,
        quality: str = "llm:auto_inline:low",
        *,
        persona_variant: str = "official",
        jp_beginner_coach: bool = False,
    ) -> None:
        """Record an inline or LLM-verified JP->KO pair into cache and rolling context."""
        clean_jp = str(jp_text or "").strip()
        clean_kr = normalize_korean_subtitle(kr_text)
        if not clean_jp or not clean_kr:
            return
        self._cache_put(
            self._cache_key(
                clean_jp,
                char_data,
                persona_variant=persona_variant,
                jp_beginner_coach=jp_beginner_coach,
            ),
            clean_kr,
            quality=quality,
        )
        with self._ctx_lock:
            self._recent_context.append(f"{clean_jp} => {clean_kr}")

    def clear_context(self) -> None:
        """Clear rolling dialogue context (e.g. on character or persona switch)."""
        with self._ctx_lock:
            self._recent_context.clear()

    def should_refine_with_llm(self, engine: str) -> bool:
        with self._state_lock:
            return (engine or "auto_inline") != "fast_nmt" and self._llm_translator is not None and not self._closed

    def _get_json_bounded(self, url: str, params: dict[str, str], label: str) -> Any:
        safe_url = _validate_translate_url(url)
        safe_params = dict(params)
        if "q" in safe_params:
            safe_params["q"] = str(safe_params["q"] or "")[:_MAX_TRANSLATE_QUERY_CHARS]

        client = self._get_http_client()
        if isinstance(client, httpx.Client):
            with client.stream("GET", safe_url, params=safe_params) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"{label} translate HTTP {resp.status_code}")
                cl_raw = resp.headers.get("content-length")
                if cl_raw is not None:
                    try:
                        if int(cl_raw) > _MAX_TRANSLATE_RESPONSE_BYTES:
                            raise RuntimeError(f"{label} translate response too large")
                    except ValueError:
                        pass
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > _MAX_TRANSLATE_RESPONSE_BYTES:
                        raise RuntimeError(f"{label} translate response too large")
                return json.loads(buf.decode("utf-8", errors="replace"))

        resp = client.get(safe_url, params=safe_params)
        if resp.status_code != 200:
            raise RuntimeError(f"{label} translate HTTP {resp.status_code}")
        raw_content = getattr(resp, "content", None)
        if isinstance(raw_content, (bytes, bytearray)) and len(raw_content) > _MAX_TRANSLATE_RESPONSE_BYTES:
            raise RuntimeError(f"{label} translate response too large")
        return resp.json()

    def _query_primary(self, prepared: str) -> str:
        data = self._get_json_bounded(
            _PRIMARY_TRANSLATE_URL,
            params={"client": "gtx", "sl": "ja", "tl": "ko", "dt": "t", "q": prepared},
            label="Primary",
        )
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            return ""
        return "".join(str(seg[0]) for seg in data[0] if isinstance(seg, list) and seg and seg[0])

    def _query_fallback(self, prepared: str) -> str:
        data = self._get_json_bounded(
            _FALLBACK_TRANSLATE_URL,
            params={"client": "dict-chrome-ex", "sl": "ja", "tl": "ko", "q": prepared},
            label="Fallback",
        )
        if isinstance(data, list):
            parts = []
            for item in data:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, list) and item and isinstance(item[0], str):
                    parts.append(item[0])
            return "".join(parts)
        return ""

    def _fast_nmt_cache_key(
        self,
        clean_text: str,
        char_data: dict[str, Any] | None,
    ) -> tuple[str, str]:
        cid = str(char_data.get("id") or "") if isinstance(char_data, dict) else ""
        return f"{cid}|__fast_nmt__", clean_text

    def translate_sync(
        self,
        text: str,
        char_data: dict[str, Any] | None = None,
        *,
        persona_variant: str = "official",
        jp_beginner_coach: bool = False,
    ) -> str:
        """Translate Japanese spoken text into natural Korean subtitles cleanly and safely."""
        clean = str(text or "").strip()
        if not clean:
            return ""
        if not contains_japanese(clean):
            return clean

        key = self._cache_key(
            clean,
            char_data,
            persona_variant=persona_variant,
            jp_beginner_coach=jp_beginner_coach,
        )
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        fast_key = self._fast_nmt_cache_key(clean, char_data)
        fast_cached = self._cache_get(fast_key)
        if fast_cached is not None:
            self._cache_put(key, fast_cached, quality="fast")
            return fast_cached

        prepared = self.glossary.prepare_for_translation(clean, char_data=char_data)
        raw_ko = ""
        try:
            raw_ko = self._query_primary(prepared)
        except Exception:
            try:
                raw_ko = self._query_fallback(prepared)
            except Exception:
                raw_ko = ""

        normalized = normalize_korean_subtitle(raw_ko)
        if normalized:
            self._cache_put(fast_key, normalized, quality="fast")
            self._cache_put(key, normalized, quality="fast")
        return normalized

    def submit(
        self,
        text: str,
        char_data: dict[str, Any] | None = None,
        *,
        persona_variant: str = "official",
        jp_beginner_coach: bool = False,
    ) -> Future[str]:
        """Schedule asynchronous translation (or resolve immediately if cached / already Korean)."""
        clean = str(text or "").strip()
        if not clean or not contains_japanese(clean):
            fut: Future[str] = Future()
            fut.set_result(clean)
            return fut

        key = self._cache_key(
            clean,
            char_data,
            persona_variant=persona_variant,
            jp_beginner_coach=jp_beginner_coach,
        )
        cached = self._cache_get(key)
        if cached is not None:
            fut = Future()
            fut.set_result(cached)
            return fut

        fast_key = self._fast_nmt_cache_key(clean, char_data)
        fast_cached = self._cache_get(fast_key)
        if fast_cached is not None:
            self._cache_put(key, fast_cached, quality="fast")
            fut = Future()
            fut.set_result(fast_cached)
            return fut

        with self._state_lock:
            if self._closed:
                fut = Future()
                fut.set_result("")
                return fut
            try:
                return self._executor.submit(
                    self.translate_sync,
                    clean,
                    char_data,
                    persona_variant=persona_variant,
                    jp_beginner_coach=jp_beginner_coach,
                )
            except RuntimeError:
                fut = Future()
                fut.set_result("")
                return fut

    def submit_progressive(
        self,
        text: str,
        char_data: dict[str, Any] | None = None,
        engine: str = "auto_inline",
        thinking_level: str = "low",
        on_refined: Callable[[str], None] | None = None,
        *,
        persona_variant: str = "official",
        jp_beginner_coach: bool = False,
    ) -> tuple[Future[str], bool]:
        """
        Return `(fast_future, will_refine)` for 2-stage progressive subtitles:
        1. `fast_future` resolves in ~0.15s (or 0ms on cache hit) so subtitles never lag audio.
        2. When `will_refine=True`, `_llm_executor` runs context- & persona-aware LLM refinement
           in the background, caches at `llm:<engine>:<thinking_level>` quality, and invokes `on_refined`.
        """
        clean = str(text or "").strip()
        if not clean or not contains_japanese(clean):
            fut: Future[str] = Future()
            fut.set_result(clean)
            return fut, False

        eff_eng = engine or "auto_inline"
        eff_lvl = thinking_level if thinking_level in ("low", "medium", "high", "xhigh") else "low"
        wanted_q = f"llm:{eff_eng}:{eff_lvl}"

        key = self._cache_key(
            clean,
            char_data,
            persona_variant=persona_variant,
            jp_beginner_coach=jp_beginner_coach,
        )
        cached_entry = self._cache_get_entry(key)
        if cached_entry is not None:
            val, quality = cached_entry
            fut = Future()
            fut.set_result(val)
            if not self.should_refine_with_llm(eff_eng) or quality == wanted_q:
                return fut, False
            scheduled = self._schedule_llm_refinement(
                clean,
                char_data,
                eff_eng,
                eff_lvl,
                on_refined,
                persona_variant=persona_variant,
                jp_beginner_coach=jp_beginner_coach,
            )
            return fut, scheduled

        fast_fut = self.submit(
            clean,
            char_data=char_data,
            persona_variant=persona_variant,
            jp_beginner_coach=jp_beginner_coach,
        )
        if not self.should_refine_with_llm(eff_eng):
            return fast_fut, False

        scheduled = self._schedule_llm_refinement(
            clean,
            char_data,
            eff_eng,
            eff_lvl,
            on_refined,
            persona_variant=persona_variant,
            jp_beginner_coach=jp_beginner_coach,
        )
        return fast_fut, scheduled

    def _schedule_llm_refinement(
        self,
        clean_jp: str,
        char_data: dict[str, Any] | None,
        engine: str,
        thinking_level: str,
        on_refined: Callable[[str], None] | None,
        *,
        persona_variant: str = "official",
        jp_beginner_coach: bool = False,
    ) -> bool:
        with self._state_lock:
            cb = self._llm_translator
            if self._closed or cb is None:
                return False
            accepts_level = self._llm_accepts_level
        wanted_q = f"llm:{engine}:{thinking_level}"

        def _worker() -> None:
            refined_clean = ""
            try:
                if self._closed:
                    return
                with self._ctx_lock:
                    ctx = list(self._recent_context)
                style_guide = build_character_ko_style_guide(
                    char_data,
                    jp_beginner_coach=jp_beginner_coach,
                    persona_variant=persona_variant,
                )
                if accepts_level:
                    raw = cb(clean_jp, style_guide, ctx, engine, thinking_level)
                else:
                    raw = cb(clean_jp, style_guide, ctx, engine)
                refined_clean = normalize_korean_subtitle(raw)
                if refined_clean:
                    self.record_dialogue_pair(
                        clean_jp,
                        refined_clean,
                        char_data=char_data,
                        quality=wanted_q,
                        persona_variant=persona_variant,
                        jp_beginner_coach=jp_beginner_coach,
                    )
            except Exception:
                refined_clean = ""
            finally:
                if on_refined is not None:
                    try:
                        on_refined(refined_clean)
                    except Exception:
                        pass

        with self._state_lock:
            if self._closed:
                return False
            try:
                self._llm_executor.submit(_worker)
                return True
            except RuntimeError:
                return False

    def warmup_async(self) -> None:
        """Warm TCP+TLS socket in background so first live subtitle incurs minimal latency."""
        if self._closed:
            return

        def _worker() -> None:
            try:
                if not self._closed:
                    self._query_primary("先生")
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="sub-ko-warmup").start()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            for pool in (self._executor, self._llm_executor):
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
        with self._http_lock:
            if self._owns_client and self._http is not None:
                try:
                    self._http.close()
                except Exception:
                    pass
                self._http = None


def format_subtitle_entries_plain(entries: list[SubtitleEntry], mode: str) -> str:
    """Format a list of SubtitleEntry items into plain text according to the selected mode."""
    eff_mode = mode if mode in SUBTITLE_MODES else "jp_kr"
    blocks: list[str] = []
    prev_cid: str | None = None

    for entry in entries:
        show_header = entry.is_turn_start or (entry.char_id != prev_cid)
        prev_cid = entry.char_id
        jp = (entry.jp_text or "").strip()
        kr = (entry.kr_text or "").strip()
        kr_display = f"{kr} ⟳" if (kr and entry.refining) else kr

        lines: list[str] = []
        if show_header:
            speaker = entry.char_name_kr or entry.char_name_jp or entry.char_id or "캐릭터"
            lines.append(f"▸ {speaker}")

        if eff_mode == "jp_only":
            if jp:
                lines.append(f"  {jp}")
        elif eff_mode == "kr_only":
            if kr_display:
                lines.append(f"  {kr_display}")
            elif entry.pending:
                lines.append("  (자막 번역 중...)")
            elif jp:
                lines.append(f"  {jp}")
        else:  # jp_kr
            if jp and kr and jp == kr:
                lines.append(f"  {jp}")
            else:
                if jp:
                    lines.append(f"  {jp}")
                if kr_display:
                    lines.append(f"  {kr_display}")
                elif entry.pending and contains_japanese(jp):
                    lines.append("  (자막 번역 중...)")

        if lines:
            blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
