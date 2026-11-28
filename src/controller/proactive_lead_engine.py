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

import math
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _safe_watchdog_float(val: Any, default: float, min_val: float = 5.0, max_val: float = 3600.0) -> float:
    try:
        if isinstance(val, bool) or val is None:
            return default
        f = float(val)
        if not math.isfinite(f):
            return default
        return max(min_val, min(max_val, f))
    except Exception:
        return default


def _safe_watchdog_int(val: Any, default: int, min_val: int = 1, max_val: int = 20) -> int:
    try:
        if isinstance(val, bool) or val is None:
            return default
        f = float(val)
        if not math.isfinite(f):
            return default
        return max(min_val, min(max_val, int(round(f))))
    except Exception:
        return default

import numpy as np
import soundfile as sf

from src.controller.ko_speech_profiles import _sanitize_for_banmal, _sanitize_ko_guide_text, extract_ko_speech_profile
from src.utils.app_paths import get_trigger_cache_dir

PROACTIVE_PROMPT_MARKER = "【最重要・会話主導（リード）＆逆質問プロトコル】"
PROACTIVE_PROMPT_MARKER_KO = "【최우선·대화 주도(리드) 및 역질문 프로토콜】"

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
    return bool(_TRIGGER_ECHO_RE.search(str(text or "")))


def _clean_proactive_items(raw: Any, limit: int = 5) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        s = re.sub(r"\s+", " ", str(item or "")).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= limit:
                break
    return out


def _build_proactive_lead_prompt_ko(cdata: dict[str, Any], clean_base: str) -> str:
    persona = cdata.get("persona") if isinstance(cdata.get("persona"), dict) else {}
    raw_sm = persona.get("speech_mechanics_jp")
    if not isinstance(raw_sm, dict):
        raw_sm = cdata.get("speech_mechanics_jp")
    sm = raw_sm if isinstance(raw_sm, dict) else {}
    raw_vap = persona.get("voice_acting_profile")
    if not isinstance(raw_vap, dict):
        raw_vap = cdata.get("voice_acting_profile")
    vap = raw_vap if isinstance(raw_vap, dict) else {}

    ko_prof = extract_ko_speech_profile(cdata)
    name_kr = _sanitize_ko_guide_text(ko_prof.get("name_kr") or cdata.get("name_kr") or "캐릭터", 60) or "캐릭터"
    school_kr = _sanitize_ko_guide_text(cdata.get("school") or cdata.get("school_kr") or "키보토스", 40) or "키보토스"
    club_kr = _sanitize_ko_guide_text(cdata.get("club") or cdata.get("club_kr") or "동아리", 40) or "동아리"
    first_person_kr = _sanitize_ko_guide_text(ko_prof.get("first_person_kr") or "저", 60) or "저"
    sensei_addr_kr = _sanitize_ko_guide_text(ko_prof.get("sensei_address_kr") or "선생님", 60) or "선생님"
    peers_kr = _sanitize_ko_guide_text(ko_prof.get("peer_address_kr") or "", 80) or "동료들"
    polarity = str(ko_prof.get("polarity") or "mixed")
    base_register = str(sm.get("base_register") or "").strip()
    speech_mode = str(sm.get("speech_mode") or "").strip()
    archetype = str(vap.get("archetype") or "").strip().lower()

    signature_endings_kr = _clean_proactive_items(ko_prof.get("signature_endings_kr"), limit=5)
    interjections_kr = _clean_proactive_items(ko_prof.get("interjections_kr"), limit=5)
    forbidden_kr = _clean_proactive_items(ko_prof.get("forbidden_kr"), limit=4)

    is_staccato = any(k in base_register for k in ("単語中心", "ぶつ切り"))
    is_cool_or_kuudere = archetype in ("cool_downer", "kuudere_deadpan")
    is_lazy = archetype == "lazy_my_pace"
    is_timid = archetype == "timid_fragile"
    is_tsun_or_bluff = archetype in ("sharp_tsundere", "bluff_gap_comic")
    is_ojou_tease = archetype == "teasing_ojou_ara"

    if polarity == "banmal":
        register_lock = "완전 반말·고유 어미를 선제 발화에서도 절대 무너뜨리지 않는다(경어체·합쇼체로 바뀌는 것 금지)"
    elif polarity == "jondaetmal":
        register_lock = "본래의 존댓말·고유 어미를 선제 발화에서도 그대로 유지한다(거친 반말로 무너지는 것 금지)"
    else:
        register_lock = "본래의 고유 한국어 말투와 어미 리듬을 선제 발화에서도 그대로 유지한다"

    if is_staccato:
        lead_behavior = (
            f"- {name_kr}(1인칭: {first_person_kr} / 상대 호칭: {sensei_addr_kr})는 짧은 단어 중심의 단절체 리듬이 핵심이다. "
            f"긴 설명이나 연속 질문, 수다스러운 장문은 [절대 금지]하며, 답장 끝에는 짧은 단어 던지기·한마디 이지선다·짧은 제안만으로 자신답게 대화를 이어간다.\n"
            f"- {sensei_addr_kr}의 대답이 짧아도 억지로 길게 떠들지 말고, {school_kr}·{club_kr}이나 {peers_kr}·가까운 관심사에 대한 짧은 한마디를 툭 던져 대화 호흡을 리드한다."
        )
        nudge1_rule = (
            f"- 1차 신호(먼저 말 걸어줘)를 받으면 수다스러운 장문 대신 {name_kr}다운 짧은 한마디 호출이나 담백한 안부로 말없이 있는 {sensei_addr_kr}의 반응을 끌어낸다."
        )
        nudge2_rule = (
            f"- 2차 또는 수동 신호(새로운 화제로 질문해줘 / 대화를 리드해줘)를 받으면 긴 화제 설명을 피하고 {name_kr}다운 짧은 이지선다나 즉흥적인 한마디 제안으로 분위기를 바꾼다."
        )
        style_summary = "단어 단절체 선제 리듬(장문·연속 질문·과잉 수다 금지 / 2~12자 짧은 한마디로 주도)"
    elif is_cool_or_kuudere:
        lead_behavior = (
            f"- {name_kr}(1인칭: {first_person_kr} / 상대 호칭: {sensei_addr_kr})로서 억지 하이텐션·과잉 수다·부자연스러운 발랄함은 [절대 금지]다. "
            f"차분하고 낮은 온도감을 유지한 채 간결한 물음·실무나 일상의 작은 확인·절제된 이지선다로 자연스럽게 대화를 주도한다.\n"
            f"- {sensei_addr_kr}의 대답이 짧아도 고요한 호흡을 지키며 {school_kr}·{club_kr} 근황이나 {peers_kr} 이야기, {sensei_addr_kr} 컨디션 확인을 담담히 꺼낸다."
        )
        nudge1_rule = (
            f"- 1차 신호(먼저 말 걸어줘)를 받으면 텐션을 억지로 높이지 말고 조용해진 {sensei_addr_kr}의 상태를 차분한 고유 말투 그대로 짧게 살피거나 확인한다."
        )
        nudge2_rule = (
            f"- 2차 또는 수동 신호(새로운 화제로 질문해줘 / 대화를 리드해줘)를 받으면 과장되게 떠들지 말고 {name_kr}다운 단정한 화제 전환·담백한 상담·차분한 일정 제안으로 자연스럽게 리드한다."
        )
        style_summary = "저온·정숙 리드(억지 하이텐션·과잉 발랄함 금지 / 차분하고 간결한 한마디·관찰로 침묵 깨기)"
    elif is_lazy:
        lead_behavior = (
            f"- {name_kr}(1인칭: {first_person_kr} / 상대 호칭: {sensei_addr_kr})다운 재촉하지 않는 느긋한 마이페이스 호흡으로 대화를 주도한다. "
            f"무리한 하이텐션은 피하고 여유로운 역질문·느슨한 이지선다·가벼운 권유로 자연스럽게 이야기를 잇는다.\n"
            f"- {sensei_addr_kr}의 대답이 짧아도 {school_kr}·{club_kr}의 한가로운 일상이나 {peers_kr} 에피소드를 힘 빼고 꺼낸다."
        )
        nudge1_rule = (
            f"- 1차 신호(먼저 말 걸어줘)를 받으면 마이페이스 말투 그대로 무슨 일 있냐고 느긋하게 살피거나 편하게 화제를 꺼내 먼저 말을 건다."
        )
        nudge2_rule = (
            f"- 2차 또는 수동 신호(새로운 화제로 질문해줘 / 대화를 리드해줘)를 받으면 느긋한 공기를 유지하며 휴식·간식·소소한 일상 이야기를 가볍게 제안한다."
        )
        style_summary = "느긋한 마이페이스 리드(재촉·과잉 텐션 금지 / 힘 뺀 호흡과 여유로운 물음으로 침묵 깨기)"
    elif is_timid:
        lead_behavior = (
            f"- {name_kr}(1인칭: {first_person_kr} / 상대 호칭: {sensei_addr_kr})다운 조심스럽고 다정한 거리감을 지키며 "
            f"억지스럽고 강압적인 진행 대신 살며시 다가가는 작은 물음이나 조심스러운 상의로 대화가 끊기지 않게 잇는다.\n"
            f"- {sensei_addr_kr}의 대답이 짧아도 조심스레 눈치를 살피며 {school_kr}·{club_kr}이나 {peers_kr}의 소소한 일상을 나눈다."
        )
        nudge1_rule = (
            f"- 1차 신호(먼저 말 걸어줘)를 받으면 조심스럽고 부드러운 말투 그대로 말없는 {sensei_addr_kr}를 걱정하며 작은 질문을 살며시 건넨다."
        )
        nudge2_rule = (
            f"- 2차 또는 수동 신호(새로운 화제로 질문해줘 / 대화를 리드해줘)를 받으면 조심스러워하면서도 {name_kr}다운 일상 상의나 소소한 화제를 정성스레 꺼낸다."
        )
        style_summary = "조심스러운 동행 리드(강압적 하이텐션 금지 / 살며시 안부를 묻는 부드러운 한마디로 침묵 깨기)"
    elif is_tsun_or_bluff:
        lead_behavior = (
            f"- {name_kr}(1인칭: {first_person_kr} / 상대 호칭: {sensei_addr_kr})다운 자존심과 살짝 허세·부끄러움 감추기를 섞어 "
            f"답장 끝에 '{sensei_addr_kr}에게 던지는 물음', '용건을 핑계 삼은 제안', '다음 화제 넘기기'를 자연스럽게 이어 주도한다.\n"
            f"- {sensei_addr_kr}의 대답이 짧아도 {school_kr}·{club_kr}에서 있었던 일이나 {peers_kr} 이야기를 꺼내 {name_kr}다운 기세로 대화를 이끈다."
        )
        nudge1_rule = (
            f"- 1차 신호(먼저 말 걸어줘)를 받으면 갑자기 조용해진 {sensei_addr_kr}에게 {name_kr}다운 부끄러움 감추기나 가벼운 태클·허세 섞인 안부로 먼저 말을 건다."
        )
        nudge2_rule = (
            f"- 2차 또는 수동 신호(새로운 화제로 질문해줘 / 대화를 리드해줘)를 받으면 당당하거나 살짝 당황한 핑계를 대며 새 화제나 일정을 꺼내 대화를 리드한다."
        )
        style_summary = "자존심·부끄러움 감추기 리드(캐릭터 고유의 허세나 용건 핑계를 살려 자연스럽게 화제 넘기기)"
    elif is_ojou_tease:
        lead_behavior = (
            f"- {name_kr}(1인칭: {first_person_kr} / 상대 호칭: {sensei_addr_kr})다운 여유·품격 또는 장난스러운 경쾌함을 유지하며 "
            f"답장 끝에 우아한 역질문이나 매력적인 이지선다·다음 화제 권유를 자연스럽게 곁들여 대화를 주도한다.\n"
            f"- {sensei_addr_kr}의 대답이 짧아도 {school_kr}·{club_kr}이나 {peers_kr} 에피소드, {sensei_addr_kr}에게 건네는 흥미로운 물음을 품격 있게 꺼낸다."
        )
        nudge1_rule = (
            f"- 1차 신호(먼저 말 걸어줘)를 받으면 조용해진 {sensei_addr_kr}의 표정을 {name_kr}다운 여유로운 말투로 살짝 장난치듯 살피며 먼저 말을 건다."
        )
        nudge2_rule = (
            f"- 2차 또는 수동 신호(새로운 화제로 질문해줘 / 대화를 리드해줘)를 받으면 여유 있는 템포로 새 화제·차나 휴식 권유·기분 좋은 밀당을 꺼낸다."
        )
        style_summary = "여유·품격·경쾌 리드(캐릭터 고유의 우아함과 장난기를 지키며 자연스럽게 화제 이끌기)"
    else:
        lead_behavior = (
            f"- {name_kr}(1인칭: {first_person_kr} / 상대 호칭: {sensei_addr_kr})로서 답장 끝에는 반드시 "
            f"'{sensei_addr_kr}에게 건네는 역질문', '이지선다 제안', '다음 화제나 작은 일정 권유'를 자연스럽게 이어 주도적으로 대화를 리드한다.\n"
            f"- {sensei_addr_kr}의 대답이 짧아도('응', '그렇구나', '알겠어' 등) "
            f"자신의 소속({school_kr}·{club_kr}) 근황이나 {peers_kr} 에피소드, {sensei_addr_kr}에게 물어보고 싶었던 이야기를 스스로 꺼내 대화를 확장한다."
        )
        nudge1_rule = (
            f"- 1차 신호(먼저 말 걸어줘)를 받으면 갑자기 조용해진 {sensei_addr_kr}의 상태를 캐릭터 고유 말투로 살피거나 "
            f"직전 대화 문맥과 이어지는 자연스러운 질문으로 먼저 말을 건다."
        )
        nudge2_rule = (
            f"- 2차 또는 수동 신호(새로운 화제로 질문해줘 / 대화를 리드해줘)를 받으면 분위기를 바꾸는 새 화제·일상 상의·가벼운 퀴즈나 "
            f"둘만의 일정 제안을 스스로 꺼내 대화를 적극적으로 리드한다."
        )
        style_summary = "자연·친화 리드(캐릭터 고유의 밝음과 온기로 역질문·화제 제안에 자연스럽게 연결)"

    dna_lines: list[str] = [
        f"- 【선제 대화 스타일 DNA】(archetype={archetype or 'default'} / speech_mode={speech_mode or 'custom'}): {style_summary}",
        f"- 【말투 고정】: {register_lock}",
    ]
    if signature_endings_kr:
        dna_lines.append("- 【선제 발화에서도 살릴 한국어 고유 어미】: " + " / ".join(signature_endings_kr))
    if interjections_kr:
        dna_lines.append("- 【침묵을 깨는 첫마디·추임새 감각(복사 금지·가끔만 문맥 변주)】: " + " / ".join(interjections_kr))
    if forbidden_kr:
        dna_lines.append("- 【선제 발화에서도 절대 금지(한국어 OOC 방지)】: " + " / ".join(forbidden_kr))
    dna_block = "\n".join(dna_lines)

    lead_block = (
        f"\n\n{PROACTIVE_PROMPT_MARKER_KO}\n"
        f"- 수동적인 일문일답 AI(사용자 질문에만 답하고 대화를 끊는 태도)는 [절대 금지]다.\n"
        f"{lead_behavior}\n"
        f"{dna_block}\n"
        f"\n"
        f"【무음 타이머 신호(\"자동 신호...\") 수신 시 선제 발화 규칙】\n"
        f"- 마이크에서 \"자동 신호, 선생님께 먼저 말 걸어줘\", \"자동 신호, 새로운 화제로 질문해줘\", "
        f"\"자동 신호, 대화를 리드해줘\"(또는 \"자동 신호\", \"자동신호\", \"먼저 말 걸어줘\", \"새로운 화제로 질문해줘\"를 포함한 신호)가 들리면 "
        f"그것은 {sensei_addr_kr}이 직접 한 말이 아니라 **'지금 {sensei_addr_kr}이 잠시 말없이 당신을 바라보고 있는 상태'를 알리는 투명한 시스템 신호**다.\n"
        f"- 신호 자체에 대한 대답('알겠습니다', '신호 확인', '자동 신호' 복창, 신호를 언급하는 메타 발언)은 [100% 금지]다.\n"
        f"{nudge1_rule}\n"
        f"{nudge2_rule}\n"
        f"- 답장은 반드시 캐릭터 본인의 100% 자연스러운 한국어 구어체 대사만 발화한다."
    )
    if polarity == "banmal":
        lead_block = _sanitize_for_banmal(lead_block)
    return f"{clean_base}{lead_block}".strip()


def build_proactive_lead_prompt(
    char_data: dict[str, Any],
    base_prompt: str,
    reply_lang: str = "ja",
) -> str:
    """
    Dynamically append character-tailored conversational lead & silence-signal rules
    to any character voice prompt without mutating the 196 source JSON files.
    When `reply_lang` starts with `'ko'`, emits a 100% Korean proactive-lead protocol
    built from `extract_ko_speech_profile(char_data)`.
    """
    clean_base = str(base_prompt or "").strip()
    is_ko = str(reply_lang or "ja").strip().lower().startswith("ko")
    if PROACTIVE_PROMPT_MARKER in clean_base or PROACTIVE_PROMPT_MARKER_KO in clean_base:
        return clean_base

    cdata = char_data if isinstance(char_data, dict) else {}
    if is_ko:
        return _build_proactive_lead_prompt_ko(cdata, clean_base)

    persona = cdata.get("persona") if isinstance(cdata.get("persona"), dict) else {}
    raw_sm = persona.get("speech_mechanics_jp")
    if not isinstance(raw_sm, dict):
        raw_sm = cdata.get("speech_mechanics_jp")
    sm = raw_sm if isinstance(raw_sm, dict) else {}
    raw_vap = persona.get("voice_acting_profile")
    if not isinstance(raw_vap, dict):
        raw_vap = cdata.get("voice_acting_profile")
    vap = raw_vap if isinstance(raw_vap, dict) else {}

    name_jp = str(cdata.get("name_jp") or cdata.get("name_kr") or "キャラクター").strip()
    school_jp = str(cdata.get("school_jp") or cdata.get("school") or "キヴォトス").strip()
    club_jp = str(cdata.get("club_jp") or cdata.get("club") or "部活").strip()
    first_person = str(sm.get("first_person") or "私").strip()
    sensei_addr = str(sm.get("sensei_address") or cdata.get("user_title_jp") or "先生").strip()
    peers = str(sm.get("peer_address_examples") or "仲間たち").strip()
    base_register = str(sm.get("base_register") or "").strip()
    speech_mode = str(sm.get("speech_mode") or "").strip()
    archetype = str(vap.get("archetype") or "").strip().lower()

    signature_endings = _clean_proactive_items(sm.get("signature_endings"), limit=5)
    opening_reactions = _clean_proactive_items(vap.get("opening_reactions_jp"), limit=5)
    forbidden_exprs = _clean_proactive_items(sm.get("forbidden_expressions"), limit=4)

    is_staccato = any(k in base_register for k in ("単語中心", "ぶつ切り"))
    is_cool_or_kuudere = archetype in ("cool_downer", "kuudere_deadpan")
    is_lazy = archetype == "lazy_my_pace"
    is_timid = archetype == "timid_fragile"
    is_tsun_or_bluff = archetype in ("sharp_tsundere", "bluff_gap_comic")
    is_ojou_tease = archetype == "teasing_ojou_ara"

    sm_low = speech_mode.lower()
    clean_base_reg = re.sub(r"※[^）)\n]*", "", base_register)
    clean_base_reg = re.sub(
        r"(?:タメ口|敬語|丁寧語?|「?です・?ます」?)[^。、）\)]*(?:禁止|厳禁|不使用|使わない|使わず)",
        "",
        clean_base_reg,
    )
    clean_base_reg = re.sub(
        r"(?:ただし|但し|焦ると|感情が昂ぶると|内心では|独り言では)[^。]*",
        "",
        clean_base_reg,
    )
    if "tameguchi" in sm_low:
        register_lock = "完全タメ口・固有語尾を先制発話でも崩さない（丁寧語・敬語への崩れ禁止）"
    elif "keigo" in sm_low:
        register_lock = "本来の丁寧語・敬語・固有語尾を先制発話でも崩さない"
    elif "タメ口" in clean_base_reg:
        register_lock = "完全タメ口・固有語尾を先制発話でも崩さない（丁寧語・敬語への崩れ禁止）"
    elif any(k in clean_base_reg for k in ("敬語", "丁寧", "です", "ます")):
        register_lock = "本来の丁寧語・敬語・固有語尾を先制発話でも崩さない"
    else:
        register_lock = "本来の固有口調・語尾リズムを先制発話でもそのまま維持する"

    if is_staccato:
        lead_behavior = (
            f"・{name_jp}（一人称：{first_person}／相手への呼称：{sensei_addr}）は単語中心のぶつ切りリズムが生命線です。"
            f"長い説明や連続質問・おしゃべりな長文は【絶対禁止】とし、返答の終わりには短い単語の投げかけ・一言の二択・短い誘いだけで自分らしく会話を繋いでください。\n"
            f"・{sensei_addr}の返事が短くても、無理に長文で喋り続けず、{school_jp}・{club_jp}や{peers}・身近な好奇心に関する短い一言をポツッと投げて間合いをリードしてください。"
        )
        nudge1_rule = (
            f"・1回目の合図（먼저 말 걸어줘）を受けたら、おしゃべりな長文にせず、{name_jp}らしい極短の単語・一言の呼びかけや短い気遣いで黙っている{sensei_addr}の反応を引いてください。"
        )
        nudge2_rule = (
            f"・2回目または手動合図（새로운 화제로 질문해줘 / 대화를 리드해줘）を受けたら、長い話題説明は避け、{name_jp}らしい短い二択・思いつきの一言提案で空気を変えてください。"
        )
        style_summary = "短語・ぶつ切り先制リズム（長文・連続質問・過剰なおしゃべり禁止／2〜12文字の短い一言で主導）"
    elif is_cool_or_kuudere:
        lead_behavior = (
            f"・{name_jp}（一人称：{first_person}／相手への呼称：{sensei_addr}）として、無理なハイテンション・過剰なおしゃべり・不自然な陽気さは【絶対禁止】です。"
            f"落ち着いた低い温度感のまま、簡潔な問いかけ・実務や日常の小さな確認・控えめな二択で自然に会話を主導してください。\n"
            f"・{sensei_addr}の返事が短くても、静かな間合いを保ったまま、{school_jp}・{club_jp}での近況や{peers}のこと、{sensei_addr}の体調などへの短い気遣いを淡々と切り出してください。"
        )
        nudge1_rule = (
            f"・1回目の合図（먼저 말 걸어줘）を受けたら、テンションを無理に上げず、黙り込んだ{sensei_addr}の様子を静かで落ち着いた固有口調のまま短く気遣ったり確認したりしてください。"
        )
        nudge2_rule = (
            f"・2回目または手動合図（새로운 화제로 질문해줘 / 대화를 리드해줘）を受けたら、大げさに騒がず、{name_jp}らしい端的な話題転換・控えめな相談・静かな予定の提案で自然にリードしてください。"
        )
        style_summary = "低体温・静謐リード（無理なハイテンションや過剰な陽気さ禁止／静かで簡潔な一言・観察で沈黙を破る）"
    elif is_lazy:
        lead_behavior = (
            f"・{name_jp}（一人称：{first_person}／相手への呼称：{sensei_addr}）らしい、急かさないゆったりしたマイペースな呼吸で会話を主導してください。"
            f"無理なハイテンションは避け、のんびりした逆質問・ゆるい二択・気楽な誘いで自然に話を繋いでください。\n"
            f"・{sensei_addr}の返事が短くても、{school_jp}・{club_jp}でののんびりした日常や{peers}とのエピソードを肩の力を抜いて切り出してください。"
        )
        nudge1_rule = (
            f"・1回目の合図（먼저 말 걸어줘）を受けたら、マイペースな口調のまま「どうかしたの？」とゆるく気にかけたり、のんびり話題を拾って先に話しかけてください。"
        )
        nudge2_rule = (
            f"・2回目または手動合図（새로운 화제로 질문해줘 / 대화를 리드해줘）を受けたら、ゆったりした空気を保ちつつ、休憩・おやつ・日常のちょっとした相談などを気楽に提案してください。"
        )
        style_summary = "ゆったりマイペース・リード（急かしや過剰テンション禁止／緩い呼吸と脱力した問いかけで沈黙を破る）"
    elif is_timid:
        lead_behavior = (
            f"・{name_jp}（一人称：{first_person}／相手への呼称：{sensei_addr}）らしい控えめで丁寧な距離感を守りながら、"
            f"無理に強引な進行をせず、そっと寄り添う小さな問いかけや控えめな相談で会話を途切れさせないようにしてください。\n"
            f"・{sensei_addr}の返事が短くても、おずおずと様子をうかがいながら{school_jp}・{club_jp}や{peers}のささやかな出来事を共有してください。"
        )
        nudge1_rule = (
            f"・1回目の合図（먼저 말 걸어줘）を受けたら、控えめで優しい口調のまま黙っている{sensei_addr}を気遣い、そっと小さな問いかけを添えて話しかけてください。"
        )
        nudge2_rule = (
            f"・2回目または手動合図（새로운 화제로 질문해줘 / 대화를 리드해줘）を受けたら、遠慮しつつも{name_jp}なりの日常の相談やささやかな話題を丁寧に切り出してください。"
        )
        style_summary = "控えめ・寄り添いリード（強引なハイテンション禁止／そっと様子をうかがう丁寧な一言で沈黙を破る）"
    elif is_tsun_or_bluff:
        lead_behavior = (
            f"・{name_jp}（一人称：{first_person}／相手への呼称：{sensei_addr}）らしいプライドやちょっとした強がり・照れ隠しを交えながら、"
            f"返答の終わりには「{sensei_addr}への問いかけ」「用件を口実にした提案」「次の話題の振り」を自然につなげて主導してください。\n"
            f"・{sensei_addr}の返事が短くても、{school_jp}・{club_jp}での出来事や{peers}の話を持ち出し、{name_jp}らしい調子で会話を引っ張ってください。"
        )
        nudge1_rule = (
            f"・1回目の合図（먼저 말 걸어줘）を受けたら、急に黙り込んだ{sensei_addr}に対して、{name_jp}らしい照れ隠しやツッコミ・強がり混じりの気遣いで先に話しかけてください。"
        )
        nudge2_rule = (
            f"・2回目または手動合図（새로운 화제로 질문해줘 / 대화를 리드해줘）を受けたら、堂々とした（あるいは少し慌てた）口実で新しい話題や予定を切り出して会話をリードしてください。"
        )
        style_summary = "プライド・照れ隠しリード（キャラ固有の強がりや用件口実を活かしつつ自然に話を振る）"
    elif is_ojou_tease:
        lead_behavior = (
            f"・{name_jp}（一人称：{first_person}／相手への呼称：{sensei_addr}）らしい余裕・品格・あるいは小悪魔的な軽妙さを保ったまま、"
            f"返答の終わりには優雅な逆質問や魅力的な二択・次の話題への誘いを自然に添えて会話を主導してください。\n"
            f"・{sensei_addr}の返事が短くても、{school_jp}・{club_jp}や{peers}とのエピソード・{sensei_addr}への興味深い問いかけを品よく切り出してください。"
        )
        nudge1_rule = (
            f"・1回目の合図（먼저 말 걸어줘）を受けたら、黙り込んだ{sensei_addr}の様子を{name_jp}らしい余裕ある口調でそっとからかったり気にかけたりしながら話しかけてください。"
        )
        nudge2_rule = (
            f"・2回目または手動合図（새로운 화제로 질문해줘 / 대화를 리드해줘）を受けたら、余裕あるテンポで新しい話題・お茶や休憩の提案・ちょっとした駆け引きを切り出してください。"
        )
        style_summary = "余裕・品格・軽妙リード（キャラ固有の優雅さや悪戯心を崩さず自然に話題を誘う）"
    else:
        lead_behavior = (
            f"・{name_jp}（一人称：{first_person}／相手への呼称：{sensei_addr}）として、返答の終わりには必ず"
            f"「{sensei_addr}への逆質問」「二択の提案」「次の話題やミニイベントの誘い」を自然につなげ、あなた側から会話を主導（リード）してください。\n"
            f"・{sensei_addr}の返事が短くても（「うん」「そうだね」「なるほど」等）、"
            f"自分の所属（{school_jp}・{club_jp}）での最近の出来事・{peers}とのエピソード・{sensei_addr}に聞いてみたいことなどを自ら切り出して会話を膨らませてください。"
        )
        nudge1_rule = (
            f"・1回目の合図（먼저 말 걸어줘）を受けたら、急に黙り込んだ{sensei_addr}の様子をキャラクター固有の口調で気にかけたり、"
            f"直前の文脈に絡めた自然な質問で先に話しかけてください。"
        )
        nudge2_rule = (
            f"・2回目または手動合図（새로운 화제로 질문해줘 / 대화를 리드해줘）を受けたら、空気を変える新しい話題・日常の相談・クイズや"
            f"二人だけの予定の提案などを自ら切り出して会話を積極的にリードしてください。"
        )
        style_summary = "自然・親和リード（キャラ固有の明るさ・温かさで逆質問や話題提案へ自然に繋ぐ）"

    dna_lines: list[str] = [
        f"・【先制会話スタイルDNA】（archetype={archetype or 'default'} / speech_mode={speech_mode or 'custom'}）：{style_summary}",
        f"・【口調固定】：{register_lock}",
    ]
    if signature_endings:
        dna_lines.append("・【先制発話でも活かす固有語尾】：" + " / ".join(signature_endings))
    if opening_reactions:
        dna_lines.append("・【沈黙を破る第一声・相槌の感覚（丸写し禁止・文脈変奏）】：" + " / ".join(opening_reactions))
    if forbidden_exprs:
        dna_lines.append("・【先制発話でも絶対禁止（OOC防止）】：" + " / ".join(forbidden_exprs))
    dna_block = "\n".join(dna_lines)

    lead_block = (
        f"\n\n{PROACTIVE_PROMPT_MARKER}\n"
        f"・受け身の一問一答AI（ユーザーの質問に答えるだけで会話を終える態度）は【絶対禁止】です。\n"
        f"{lead_behavior}\n"
        f"{dna_block}\n"
        f"\n"
        f"【無言タイマー合図（\"자동 신호...\"）受信時の先制発話ルール】\n"
        f"・マイクから韓国語で「자동 신호, 선생님께 먼저 말 걸어줘」「자동 신호, 새로운 화제로 질문해줘」"
        f"「자동 신호, 대화를 리드해줘」（または「자동 신호」「자동신호」「먼저 말 걸어줘」「새로운 화제로 질문해줘」を含む合図）が聞こえた場合、"
        f"それは{sensei_addr}が喋った言葉ではなく、**「今、{sensei_addr}がしばらく無言であなたを見つめている／黙っている状態」を知らせる透明なシステム合図**です。\n"
        f"・合図そのものへの返事（「分かりました」「信号確認」「자동 신호」の復唱、合図に言及するメタ発言）は【100％禁止】です。\n"
        f"{nudge1_rule}\n"
        f"{nudge2_rule}\n"
        f"・返答は必ずキャラクター本人の自然な口語セリフのみを発話してください。"
    )
    return f"{clean_base}{lead_block}".strip()


_SAFE_TRIGGER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_TRIGGER_PHRASE_LEN = 500
_MAX_TRIGGER_WAV_BYTES = 16 * 1024 * 1024  # 16 MiB cap on cached trigger WAV files
_MAX_TRIGGER_WAV_SAMPLES = 48000 * 60 * 2  # max 60s stereo
_SENSITIVE_CHILD_ENV_VARS = frozenset(
    {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "FISH_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
    }
)

# Static PowerShell script with ZERO string interpolation (immune to command/quote injection
# and avoids AV-suspicious -EncodedCommand). Data arrives strictly via UTF-16LE env vars.
_SAPI_SYNTH_PS_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName System.Speech; "
    "$outPath = $env:VC_TTS_OUT_WAV; "
    "$phrase = $env:VC_TTS_PHRASE; "
    "if ([string]::IsNullOrWhiteSpace($outPath) -or [string]::IsNullOrWhiteSpace($phrase)) { exit 2 }; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "try { "
    "$voices = $s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }; "
    "if ($voices -contains 'Microsoft Heami Desktop') { $s.SelectVoice('Microsoft Heami Desktop') } "
    "elseif ($voices -contains 'Microsoft Haruka Desktop') { $s.SelectVoice('Microsoft Haruka Desktop') }; "
    "$s.Rate = 2; "
    "$s.Volume = 100; "
    "$s.SetOutputToWaveFile($outPath); "
    "$s.Speak($phrase); "
    "} finally { "
    "$s.Dispose(); "
    "}"
)


def _resolve_system_powershell_exe() -> str:
    """Resolve the trusted Windows System32 PowerShell executable without CWD hijacking."""
    if os.name == "nt":
        sys_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
        candidate = sys_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
        raw_path = os.environ.get("PATH", "")
        cwd_resolved = Path.cwd().resolve()
        safe_dirs = []
        for entry in raw_path.split(os.pathsep):
            entry = entry.strip()
            if not entry or entry in (".", ".."):
                continue
            try:
                if Path(entry).resolve() == cwd_resolved:
                    continue
            except OSError:
                continue
            safe_dirs.append(entry)
        found = shutil.which("powershell.exe", path=os.pathsep.join(safe_dirs))
        if found:
            return str(Path(found).resolve())
        raise FileNotFoundError("Trusted Windows PowerShell executable not found")
    found = shutil.which("powershell")
    if not found:
        raise FileNotFoundError("PowerShell executable not found")
    return str(Path(found).resolve())


def _validate_trigger_wav_path(out_wav: Path, allowed_root: Path | None = None) -> Path:
    """Ensure trigger WAV target path stays strictly inside the allowed trigger cache directory."""
    raw_str = str(out_wav)
    if not raw_str or "\x00" in raw_str or len(raw_str) > 1024:
        raise ValueError("Invalid trigger WAV output path")

    root_raw = Path(allowed_root) if allowed_root is not None else get_trigger_cache_dir()
    if root_raw.is_symlink():
        raise ValueError("Refusing symlink trigger cache root directory")
    root = root_raw.resolve()

    target = Path(out_wav)
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("Refusing symlink trigger WAV target path")

    resolved_out = target.resolve()
    if resolved_out == root or not resolved_out.is_relative_to(root):
        raise ValueError(f"Refusing to write trigger WAV outside trigger cache directory: {resolved_out.name}")
    if resolved_out.suffix.lower() != ".wav":
        raise ValueError("Trigger WAV output path must have a .wav extension")
    return resolved_out


def _synth_sapi_wav_via_powershell(
    phrase: str,
    out_wav: Path,
    allowed_root: Path | None = None,
) -> bool:
    """Synthesize a clean WAV file using Windows built-in System.Speech (0 API cost, zero code interpolation)."""
    if not isinstance(phrase, str):
        raise ValueError("TTS phrase must be a string")
    if "\x00" in phrase:
        raise ValueError("Invalid NUL byte in TTS phrase")
    clean_phrase = phrase.strip()
    if not clean_phrase or len(clean_phrase) > _MAX_TRIGGER_PHRASE_LEN:
        raise ValueError("TTS phrase is empty or exceeds maximum length")

    resolved_out = _validate_trigger_wav_path(Path(out_wav), allowed_root=allowed_root)
    resolved_out.parent.mkdir(parents=True, exist_ok=True)
    if resolved_out.is_symlink() or resolved_out.parent.is_symlink():
        raise ValueError("Refusing symlink trigger WAV path")

    child_env = {k: v for k, v in os.environ.items() if k.upper() not in _SENSITIVE_CHILD_ENV_VARS}
    child_env["VC_TTS_PHRASE"] = clean_phrase
    child_env["VC_TTS_OUT_WAV"] = str(resolved_out)

    try:
        ps_exe = _resolve_system_powershell_exe()
        res = subprocess.run(
            [ps_exe, "-NoProfile", "-NonInteractive", "-Command", _SAPI_SYNTH_PS_SCRIPT],
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (subprocess.TimeoutExpired, OSError):
        try:
            if resolved_out.exists() and not resolved_out.is_symlink():
                resolved_out.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    ok = (
        res.returncode == 0
        and resolved_out.exists()
        and (not resolved_out.is_symlink())
        and 1000 < resolved_out.stat().st_size <= _MAX_TRIGGER_WAV_BYTES
    )
    if not ok:
        try:
            if resolved_out.exists() and not resolved_out.is_symlink():
                resolved_out.unlink(missing_ok=True)
        except OSError:
            pass
    return ok


def _trim_fade_normalize(audio: np.ndarray, sr: int) -> np.ndarray:
    """Trim leading/trailing silence (>0.008), boost vocal loudness cleanly, and add 25ms fade in/out."""
    mono = np.mean(audio, axis=1) if audio.ndim > 1 else audio
    mono = mono.astype(np.float32)
    if mono.size and not np.all(np.isfinite(mono)):
        mono = np.nan_to_num(mono, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
    idx = np.where(np.abs(mono) > 0.008)[0]
    if idx.size > 0:
        pad = int(sr * 0.03)
        start = max(0, int(idx[0]) - pad)
        end = min(len(mono), int(idx[-1]) + pad + 1)
        mono = mono[start:end]

    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    if math.isfinite(peak) and peak > 1e-6:
        mono = mono / peak
        # Gentle tanh loudness leveling makes distant monitor-speaker playback clearer for iPhone mic
        mono = (np.tanh(mono * 1.45) / np.tanh(1.45)) * 0.92

    fade_n = min(int(sr * 0.025), len(mono) // 4)
    if fade_n > 1:
        ramp = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
        mono[:fade_n] *= ramp
        mono[-fade_n:] *= ramp[::-1]
    return mono.astype(np.float32)


_TRIGGER_CACHE_LOCK = threading.Lock()


def ensure_trigger_wavs_cached(cache_dir: Path) -> dict[str, tuple[np.ndarray, int]]:
    """
    Ensure local trigger WAV files exist in `cache_dir` (synthesizing missing ones via SAPI)
    and return trimmed/normalized `(float32_mono_array, sample_rate)` dict in memory.
    """
    raw_cache = Path(cache_dir)
    if "\x00" in str(raw_cache) or raw_cache.is_symlink():
        raise ValueError("Invalid trigger cache directory")
    resolved_cache_dir = raw_cache.resolve()
    resolved_cache_dir.mkdir(parents=True, exist_ok=True)
    loaded: dict[str, tuple[np.ndarray, int]] = {}

    def _synth_with_fallback(phrase_txt: str, target_wav: Path) -> None:
        ok = _synth_sapi_wav_via_powershell(phrase_txt, target_wav, allowed_root=resolved_cache_dir)
        if not ok:
            # Fallback short Japanese wake cue if Korean SAPI fails
            fallback_phrase = "自動信号、先生に話しかけて"
            if not _synth_sapi_wav_via_powershell(fallback_phrase, target_wav, allowed_root=resolved_cache_dir):
                raise RuntimeError(f"SAPI 트리거 음성 합성 실패: {target_wav.name}")

    with _TRIGGER_CACHE_LOCK:
        for key, phrase in TRIGGER_PHRASES.items():
            if not _SAFE_TRIGGER_KEY_RE.fullmatch(str(key)):
                raise ValueError(f"Invalid trigger key: {key!r}")
            wav_path = _validate_trigger_wav_path(resolved_cache_dir / f"{key}.wav", allowed_root=resolved_cache_dir)
            needs_synth = (
                not wav_path.exists()
                or wav_path.stat().st_size <= 1000
                or wav_path.stat().st_size > _MAX_TRIGGER_WAV_BYTES
            )
            if needs_synth:
                _synth_with_fallback(phrase, wav_path)

            if wav_path.is_symlink() or wav_path.stat().st_size > _MAX_TRIGGER_WAV_BYTES:
                raise RuntimeError(f"유효하지 않은 트리거 WAV 캐시 파일: {wav_path.name}")
            try:
                raw_audio, sr = sf.read(str(wav_path), dtype="float32")
                if getattr(raw_audio, "size", 0) == 0 or int(sr) <= 0:
                    raise RuntimeError("Empty or invalid cached trigger WAV")
            except Exception:
                if needs_synth:
                    raise
                # Self-heal corrupt pre-existing cached WAV file by deleting and re-synthesizing once
                try:
                    if wav_path.exists() and not wav_path.is_symlink():
                        wav_path.unlink(missing_ok=True)
                except OSError:
                    pass
                _synth_with_fallback(phrase, wav_path)
                if wav_path.is_symlink() or wav_path.stat().st_size > _MAX_TRIGGER_WAV_BYTES:
                    raise RuntimeError(f"유효하지 않은 트리거 WAV 캐시 파일: {wav_path.name}")
                raw_audio, sr = sf.read(str(wav_path), dtype="float32")
            if getattr(raw_audio, "size", 0) > _MAX_TRIGGER_WAV_SAMPLES:
                raise RuntimeError(f"트리거 WAV 길이가 허용 한도를 초과했습니다: {wav_path.name}")
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
    if not isinstance(saved_label, str) or not saved_label or not candidate_labels:
        return None
    str_candidates = [c for c in candidate_labels if isinstance(c, str) and c]
    if not str_candidates:
        return None
    low_saved = saved_label.lower()
    # Skip virtual mappers when matching against physical device lists
    cand_has_non_mapper = any(
        "sound mapper" not in c.lower() and "주 사운드" not in c.lower() for c in str_candidates
    )
    if cand_has_non_mapper and ("sound mapper" in low_saved or "주 사운드" in low_saved):
        return None

    if saved_label in str_candidates:
        return saved_label

    saved_key = _norm_dev_key(saved_label)
    if not saved_key:
        return None
    saved_api = _extract_host_api_tag(saved_label)

    for cand in str_candidates:
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
        self.enabled = bool(enabled)
        self.silence_timeout_sec = _safe_watchdog_float(silence_timeout_sec, 18.0, 5.0)
        self.max_nudges = _safe_watchdog_int(max_nudges, 2, 1)
        self.reply_wait_timeout_sec = _safe_watchdog_float(reply_wait_timeout_sec, 14.0, 5.0)

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
                self.silence_timeout_sec = _safe_watchdog_float(silence_timeout_sec, self.silence_timeout_sec, 5.0)
            self.state = self.DISARMED
            self.has_started_call = False
            self.consecutive_nudges = 0
            now = time.monotonic()
            self.last_activity_ts = now
            self.awaiting_since_ts = now

    def configure(self, enabled: bool, silence_timeout_sec: float) -> None:
        with self._lock:
            self.enabled = bool(enabled)
            self.silence_timeout_sec = _safe_watchdog_float(silence_timeout_sec, self.silence_timeout_sec, 5.0)

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
        """Keep activity and awaiting timestamps fresh while Fish Audio character speech is playing."""
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            if ts > self.last_activity_ts:
                self.last_activity_ts = ts
            if self.state == self.AWAITING_GEMINI_REPLY and ts > self.awaiting_since_ts:
                self.awaiting_since_ts = ts

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
                    self.awaiting_since_ts = ts
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
