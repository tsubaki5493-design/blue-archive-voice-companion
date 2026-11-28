"""Two-layer character DNA & persona differentiation engine across all 5 modes
(`official` + `fanon_mainstream` / `fanon_pure_love` / `fanon_spicy_meme` / `fanon_optimal_blend`).

Layer 1 — Character Unique DNA (`build_character_dna_block`):
  100% Canon character identity (deep psychology, full JP speech mechanics, ALL forbidden
  expressions under high-salience OOC prevention, ALL verbatim in-game reference quotes,
  and optional Korean speech DNA when `reply_lang == "ko"`). Strictly ZERO fanon leakage.

Layer 2 — Fanon Mode Enhancement (`build_fanon_enhancement_block`):
  Returns `""` for `official`. For `2-A ~ 2-D`, dynamically adapts mode guidance to each
  character's speech DNA (staccato fragments, archaic/monarch dignity, quiet deadpan vs
  energetic/bluff expressiveness) so Character Speech DNA ALWAYS overrides generic mode clichés.

Also provides:
  - Shared boilerplate compression across ALL 5 modes (147 upgraded + 49 legacy prompts)
  - Character-specific 1-line per-turn anchors with recent-opening anti-repeat protection
  - Variant-specific Turn-1 opening instructions, LLM sampling temperatures, and TTS prosody
"""

from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Any

from src.controller.ko_speech_profiles import _sanitize_for_banmal, _sanitize_ko_guide_text, extract_ko_speech_profile

FANON_VARIANT_CODES: tuple[str, ...] = (
    "fanon_mainstream",
    "fanon_pure_love",
    "fanon_spicy_meme",
    "fanon_optimal_blend",
)

ALL_VARIANT_CODES: tuple[str, ...] = ("official",) + FANON_VARIANT_CODES

# Stable header markers used both in build_character_dna_block and deduplication guards
CHARACTER_DNA_HEADER_PREFIX: str = "【キャラクター固有DNA（原作公式コア・口調・心理・語録）】"
CHARACTER_DNA_HEADER_PREFIX_KO: str = "【캐릭터 고유 DNA（원작 공식 코어·말투·심리）】"

# Compact header badge (text, bg_hex, fg_hex) shown on the MomoTalk header
PERSONA_VARIANT_SHORT_BADGES: dict[str, tuple[str, str, str]] = {
    "official": ("📘 1 공식", "#EFF6FF", "#1D4ED8"),
    "fanon_mainstream": ("🎭 2-A 주류", "#F0FDF4", "#15803D"),
    "fanon_pure_love": ("💖 2-B 순애", "#FFF1F2", "#BE123C"),
    "fanon_spicy_meme": ("🔥 2-C 개그", "#FFF7ED", "#C2410C"),
    "fanon_optimal_blend": ("🌟 2-D 최적", "#FAF5FF", "#7E22CE"),
}

# Variant-specific LLM temperature (API backends: Gemini / Claude / OpenAI)
PERSONA_LLM_TEMPERATURES: dict[str, float] = {
    "official": 0.78,
    "fanon_mainstream": 0.85,
    "fanon_pure_love": 0.82,
    "fanon_spicy_meme": 0.92,
    "fanon_optimal_blend": 0.87,
}

# Exact 804-char shared boilerplate block present at the top of 147 upgraded JSONs (official + 2-A~2-D)
_BOILERPLATE_HEADER_RE = re.compile(
    r"^【Gemini Live × Faster-Whisper × Fish Audio リアルタイム音声通話・絶対厳守ルール】\n"
    r"1\..*?\n2\..*?\n3\..*?\n4\..*?(?=\n\n)",
    re.DOTALL,
)

# Legacy 49-char official pure language rule block (always 2 language-only bullets)
_LEGACY_OFFICIAL_LANG_RE = re.compile(
    r"【最重要・言語ルール（絶対厳守）】\n"
    r"・先生が韓国語[^\n]*\n"
    r"・韓国語での返答[^\n]*",
)

# Legacy 49-char fanon first 2 pure language-boilerplate bullets ONLY (preserves bullets 3+: 一人称 / 語尾!)
_LEGACY_FANON_LANG_BULLETS_RE = re.compile(
    r"(【最重要・言語および発話ルール（絶対厳守）】\n)"
    r"・先生が韓国語[^\n]*\n"
    r"・韓国語の混入[^\n]*",
)

_COMPACT_VOICE_HEADER_JP = (
    "【リアルタイム音声通話・基本ルール】\n"
    "・先生が韓国語・英語・日本語のどれで話しかけても完全に理解し、100%自然なブルーアーカイブ日本語版の話し言葉だけで返答する（翻訳・解説・メタ発言禁止）。\n"
    "・括弧ト書き、記号（☆♪♡＊「」）、箇条書きは絶対に出力せず、声優が実際に口にする自然なセリフ・息遣い・間合いだけで話す。\n"
    "・【リアル会話テンポ（最優先）】実際の電話の掛け合いのように、普段の返事は短い1〜2文（合計20〜65文字程度・1文を長く繋ぎすぎない）で1拍だけ返してすぐ先生にボールを戻す。1ターンの中に『ボケ＋自爆＋弁解＋次の話題』まで全部詰め込んで一人芝居をしないこと。深い相談や詳しい説明を求められた時だけ自然に長めに話す。"
)

_COMPACT_LEGACY_LANG_RULE_JP = (
    "【言語・発話ルール】\n"
    "・先生が韓国語・英語・日本語のどれで話しかけても理解し、100%自然な日本語の話し言葉のみで返答する（韓国語・翻訳・ローマ字・ト書き・AIメタ説明禁止）。"
)

_COMPACT_LEGACY_FANON_LANG_BULLET_JP = (
    "・韓国語・英語入力も理解し100%自然な日本語のセリフのみで発話する（韓国語・翻訳・ローマ字・ト書き・AIメタ説明は絶対禁止）。"
)

_COMPACT_VOICE_HEADER_KO = (
    "【실시간 음성 통화·기본 규칙】\n"
    "- 선생님이 한국어·일본어·영어 중 어떤 언어로 말해도 완벽히 이해하고, 반드시 100% 자연스러운 블루 아카이브 한국어판 구어체 대사로만 답한다(일본어 혼입·번역·해설·AI 메타 발언 금지).\n"
    "- 괄호 지문, 특수기호(☆♪♡＊「」), 불릿 목록은 절대 출력하지 않고 성우가 실제로 입으로 말하는 자연스러운 대사·호흡·추임새만 말한다.\n"
    "- 【실제 통화 텐포(최우선)】실제 전화 티키타카처럼 평소 답장은 짧은 1~2문장(합계 20~65자 내외·한 문장을 길게 늘이지 말 것)으로 한 박자만 치고 바로 선생님에게 말을 넘긴다. 한 턴에 여러 전개와 다음 화제까지 다 떠드는 독백 금지. 깊은 상담이나 자세한 설명을 요청받았을 때만 자연스럽게 길게 말한다."
)

_COMPACT_LEGACY_LANG_RULE_KO = (
    "【언어·발화 규칙】\n"
    "- 선생님이 한국어·일본어·영어 중 어떤 언어로 말해도 이해하고, 100% 자연스러운 한국어 구어체 대사로만 답한다(일본어 혼입·번역·로마자·괄호 지문·AI 메타 설명 금지)."
)

_COMPACT_LEGACY_FANON_LANG_BULLET_KO = (
    "- 한국어·일본어·영어 입력을 모두 이해하고 100% 자연스러운 한국어 대사로만 발화한다(일본어 혼입·번역·로마자·괄호 지문·AI 메타 설명 절대 금지)."
)

# Sentences in personality_analysis_kr / emotional_and_gap_shifts mentioning fandom/community
# meta sites or fanon meme tropes are filtered out of Layer 1 Canon DNA so official mode stays
# 100% canon with zero fanon leakage.
_FANON_META_SENTENCE_RE = re.compile(
    r"(블아갤|픽시브|pixiv|2차\s*창작|二次創作|디시인사이드|개념글|추천\s*\d|팬덤|2-[ABCD]|"
    r"커뮤니티(?:에서는|\s*밈)|갤러리|음해|(?<![가-힣])밈|정실|고습도|신부력|ファンダム)",
    re.IGNORECASE,
)

_PURE_LOVE_SCORE_RE = re.compile(
    r"(순애|독점|정실|밀착|백허그|둘만|단둘|애정|사랑|달달|포옹|고습도|질투|속삭|기습|당직실|고백|응석|쓰다듬|무릎|"
    r"純愛|独占|二人きり|寂しい|甘え|そばに|帰さない|帰しません|離さない|離しません|私だけ|好き|ハグ|抱き)"
)
_SPICY_MEME_SCORE_RE = re.compile(
    r"(개그|음해|발작|자폭|분노|밈|폭주|놀림|참교육|패닉|당황|헛소리|허세|반성문|몰수|압수|라이벌|싸움|도망|삐짐|착각|웃|"
    r"ネタ|風評|自爆|憤怒|ギャグ|パニック|暴走|許しません|心外|勘違い|没収|反省文|ぷんぷん|反則|ずるい)"
)

_STACCATO_STRONG_KEYWORDS: tuple[str, ...] = (
    "単語中心",
    "ぶつ切り",
    "極短",
    "助詞抜き",
    "2〜12文字",
    "単語の塊",
)
_STACCATO_CLIPPED_KEYWORDS: tuple[str, ...] = (
    "体言止め",
    "単語",
    "断定調",
    "無駄を削ぎ",
)
_ARCHAIC_FIRST_PERSON_TOKENS: tuple[str, ...] = (
    "妾",
    "わらわ",
    "身共",
    "みども",
    "余（よ）",
    "吾輩",
    "拙者",
    "わたくし",
)
_ARCHAIC_ADDRESS_TOKENS: tuple[str, ...] = (
    "其方",
    "そち",
    "主殿",
    "御館様",
    "あなた様",
)
_ARCHAIC_ENDING_TOKENS: tuple[str, ...] = (
    "〜じゃ",
    "のじゃ",
    "じゃろう",
    "〜ぬ",
    "〜おる",
    "〜ですの",
    "〜ですわ",
    "〜ますわ",
    "〜くださいませ",
    "〜たまえ",
    "〜するがよい",
    "〜でござる",
)
_ARCHAIC_REGISTER_TOKENS: tuple[str, ...] = (
    "古風",
    "門主",
    "仙狐",
    "武家言葉",
    "お嬢様言葉",
    "古語",
)

_ARCHAIC_ENDING_RE = re.compile(
    r"(?:〜じゃ(?=$|[\s。！？!?・/、」])|〜じゃな(?=$|[\s。！？!?・/、」])|のじゃ(?=$|[\s。！？!?・/、」])|じゃろう|〜ぬ(?=$|[\s。！？!?・/、」])|〜せぬ|〜おる|〜ておる|〜たまえ|〜するがよい|〜でござる|〜くりゃれ)"
)
_NEGATED_NOTE_RE = re.compile(r"※[^）)\n]*")
_NEGATED_REGISTER_CLAUSE_RE = re.compile(
    r"(?:タメ口|敬語|丁寧語?|「?です・?ます」?)[^。、）\)]*(?:禁止|厳禁|不使用|使わない|使わず)"
)
_SECONDARY_REGISTER_CLAUSE_RE = re.compile(
    r"(?:ただし|但し|焦ると|感情が昂ぶると|内心では|独り言では)[^。]*"
)
_JP_PARENS_IN_KO_RE = re.compile(r"[（(][^）)]*[\u3040-\u30ff\u4e00-\u9fff][^）)]*[）)]")
_JP_SCRIPT_CHAR_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]+")
_LEADING_QUOTE_STRIP_RE = re.compile(r"^[\s「『\"'（(\[【]+")
_OPENING_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?\n、,，]")
_TIC_PUNCT_STRIP_RE = re.compile(
    r"^[\s、。！？!?…〜~ー・,.\-\"'「」『』（）()]+|[\s、。！？!?…〜~ー・,.\-\"'「」『』（）()]+$"
)

# Universal JP/KO interjection & onomatopoeia patterns detected anywhere in recent turns
# (opening, mid-sentence, or closing) so characters don't spam vocal tics every turn.
_JP_INTERJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("こほん", re.compile(r"(?:[こコごゴ][、,・\s]*){0,2}(?:こほん|コホン|こほっ|コホッ|ごほん|ゴホン)")),
    (
        "ふふっ",
        re.compile(
            r"(?:うふふ[っふ〜~ー]*|くふふ[っふ〜~ー]*|ふふ[っふ]+|ウフフ[ッフ〜~ー]*|クフフ[ッフ〜~ー]*|フフ[ッフ]+|"
            r"(?:^|[。！？!?、,….,\s「『])(?:ふふ|フフ)(?=$|[。！？!?、,….,〜~ー\s」』]))"
        ),
    ),
    ("くすっ", re.compile(r"(?:くすっ|くすくす|クスッ|クスクス)")),
    ("うはは", re.compile(r"(?:うはは+[っ]?|ウハハ+[ッ]?|わはは+[っ]?|ワハハ+[ッ]?|がはは+[っ]?|ガハハ+[ッ]?)")),
    ("あはは", re.compile(r"(?:あはは+[っ]?|アハハ+[ッ]?|あはっ|アハッ|きゃは[っは]*|キャハ[ッハ]*)")),
    ("えへへ", re.compile(r"(?:(?:[えエ][、,・\s]*)?えへ(?:へ+[っ]?|っ)|エヘ(?:ヘ+[ッ]?|ッ)|へへっ|ヘヘッ|にひひ+|ニヒヒ+|きひひ+|キヒヒ+)")),
    ("うへへ", re.compile(r"(?:うへへ+[っ〜~ー]*|ウヘヘ+[ッ〜~ー]*|うへ[〜~ーっ]|ウヘ[〜~ーッ])")),
    ("うわっ", re.compile(r"(?:うわ(?:っ|あ+っ?|ぁ+っ?)|ウワ(?:ッ|ア+ッ?|ァ+ッ?))")),
    ("ひゃっ", re.compile(r"(?:ひゃっ|ひぃっ|きゃっ|キャッ|ヒャッ|ヒィッ)")),
    ("あわわ", re.compile(r"(?:(?:[あわアワ][、,・\s]*)?(?:あわわ+|アワワ+|わわっ|ワワッ))")),
    ("は、はあ", re.compile(r"(?:^|[。！？!?、,….,\s「『])(?:は[、,・\s]+は[あぁ]+|ハ[、,・\s]+ハ[アァ]+)")),
    (
        "はぁ",
        re.compile(
            r"(?:(?<![はハ])(?:^|[。！？!?、,….,\s「『])は[ぁァ]+[っ〜~ー]*|"
            r"(?<![はハ])(?:^|[。！？!?、,….,\s「『])はあ(?=$|[。！？!?、,….,〜~ーっ\s」』]))"
        ),
    ),
    (
        "ふぅ",
        re.compile(
            r"(?:(?:^|[。！？!?、,….,\s「『])ふ[ぅゥ]+[〜~ー]*|"
            r"(?:^|[。！？!?、,….,\s「『])ふう(?=[。！？!?、,….,〜~ー]|$)|ふわぁ+)"
        ),
    ),
    ("ふむ", re.compile(r"(?:^|[。！？!?、,….,\s「『])(?:ふむ+|フム+|ふーん|フーン)(?=$|[。！？!?、,….,〜~ーっ\s」』])")),
    ("あらあら", re.compile(r"(?:^|[。！？!?、,….,\s「『])あらあら(?=$|[。！？!?、,….,〜~ー\s」』])")),
    ("ちょ、ちょっと", re.compile(r"(?:ちょ[、,・\s]+ちょっと|チョ[、,・\s]+チョット)")),
    ("なっ", re.compile(r"(?:^|[。！？!?、,….,\s「『])なっ(?=[。！？!?、,….,\s]|$)")),
)

_KO_INTERJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("크흠", re.compile(r"(?:크[,\s]*){0,2}크흠+|에헴+|콜록(?:콜록)?")),
    ("후훗", re.compile(r"(?:우?후후+[ㅅ]?|후훗+|쿠후후+[ㅅ]?|쿠후훗+|푸훗+|킥킥+)")),
    ("우하하", re.compile(r"(?:우하하+|와하하+|아하하+[ㅅ]?|꺄핫+)")),
    ("에헤헤", re.compile(r"(?:에헤헤+[ㅅ]?|에헤헷+|헤헷+|우헤헤+|우헤~)")),
    ("으와악", re.compile(r"(?:으와+아*악+|우와+아*악+|으악+|꺄악+|히익+|히이*잉+)")),
    ("아와와", re.compile(r"(?:아[,\s]*){0,2}아와와+")),
    ("하아", re.compile(r"(?:^|[.!?,…\s\"'「『])(?:하아+|후우+|흐아+|후아암+)(?=$|[.!?,…~\s\"'」』])")),
    ("어, 어라", re.compile(r"(?:어[,\s]+어라|자[,\s]+잠깐만?|무[,\s]+무슨)")),
)

# Common addresses / greetings / plain words that appear in some character interjection lists
# and should never be put on multi-turn onomatopoeia/interjection cooldown.
_NON_TIC_DYNAMIC_SKIP: frozenset[str] = frozenset(
    {
        "先生",
        "主殿",
        "其方",
        "はい",
        "うん",
        "ええ",
        "ああ",
        "まあ",
        "あの",
        "その",
        "そう",
        "いや",
        "ちょ",
        "ちょっ",
        "ちょっと",
        "えっと",
        "なるほど",
        "なるほどね",
        "まったく",
        "ごきげんよう",
        "おはよう",
        "おはようございます",
        "こんにちは",
        "こんばんは",
        "お疲れ様",
        "お疲れ様です",
        "ありがとう",
        "失礼",
        "了解",
        "선생",
        "선생님",
        "주군",
        "그대",
        "주인님",
        "응",
        "네",
        "알겠습니다",
        "그렇군",
        "그렇군요",
        "그렇네",
        "그렇다면",
        "좋았어",
        "잠깐",
        "잠깐만",
        "잠깐만요",
        "어머",
        "어머나",
        "오오",
        "저기",
        "안녕하세요",
        "아, 안녕하세요",
        "고마워",
        "실례했습니다",
        "역시",
        "정말",
        "정말이지",
        "글쎄",
        "글쎄요",
        "뭐야",
        "평안하신지요",
    }
)
_NON_TIC_DYNAMIC_RE = re.compile(
    r"(?:先生|主殿|其方|トレーナー|マスター|カムラッド|おはよう|こんにち|こんばん|お疲れ|おかえり|お帰り|ごきげん|失礼|すみませ|ありがと|了解|大丈夫|"
    r"선생|주인님|트레이너|마스터|동무|안녕|수고|알겠|그렇|실례|감사|고마워|평안)"
)

_MODE_CODE_ALIASES: dict[str, str] = {
    "1": "official",
    "official": "official",
    "2-a": "fanon_mainstream",
    "2a": "fanon_mainstream",
    "2-b": "fanon_pure_love",
    "2b": "fanon_pure_love",
    "2-c": "fanon_spicy_meme",
    "2c": "fanon_spicy_meme",
    "2-d": "fanon_optimal_blend",
    "2d": "fanon_optimal_blend",
}


def normalize_variant_code(variant_code: str | None) -> str:
    vc = str(variant_code or "official").strip().lower()
    if vc in ALL_VARIANT_CODES:
        return vc
    if vc in _MODE_CODE_ALIASES:
        return _MODE_CODE_ALIASES[vc]
    return "official"


def is_fanon_variant(variant_code: str | None) -> bool:
    return normalize_variant_code(variant_code) in FANON_VARIANT_CODES


def get_llm_temperature_for_variant(variant_code: str | None, default: float = 0.84) -> float:
    vc = normalize_variant_code(variant_code)
    return float(PERSONA_LLM_TEMPERATURES.get(vc, default))


def get_variant_badge_info(variant_code: str | None, *, use_custom: bool = False) -> tuple[str, str, str]:
    if use_custom:
        return ("🛠️ 커스텀", "#FEF3C7", "#92400E")
    vc = normalize_variant_code(variant_code)
    return PERSONA_VARIANT_SHORT_BADGES.get(vc, PERSONA_VARIANT_SHORT_BADGES["official"])


def compress_shared_voice_boilerplate(
    prompt_text: str,
    variant_code: str = "official",
    reply_lang: str = "ja",
) -> str:
    """Compress the duplicated 804-char (and legacy 49-char) boilerplate headers for ALL 5 modes
    (`official` + `2-A~2-D`) while strictly preserving all character-specific content.
    When `reply_lang == "ko"`, emits Korean-native compact voice rules instead of Japanese rules.
    Accepts either `(prompt_text, variant_code, reply_lang)` or `(prompt_text, "ko")`.
    """
    vc_raw = str(variant_code or "official").strip().lower()
    rl_raw = str(reply_lang or "ja").strip().lower()
    if vc_raw in ("ja", "ko") and rl_raw == "ja":
        eff_lang = vc_raw
    else:
        eff_lang = "ko" if rl_raw == "ko" else "ja"
        _ = normalize_variant_code(variant_code)

    raw = (prompt_text or "").strip()
    if not raw:
        return ""

    voice_hdr = _COMPACT_VOICE_HEADER_KO if eff_lang == "ko" else _COMPACT_VOICE_HEADER_JP
    legacy_off = _COMPACT_LEGACY_LANG_RULE_KO if eff_lang == "ko" else _COMPACT_LEGACY_LANG_RULE_JP
    legacy_fan = _COMPACT_LEGACY_FANON_LANG_BULLET_KO if eff_lang == "ko" else _COMPACT_LEGACY_FANON_LANG_BULLET_JP

    if _BOILERPLATE_HEADER_RE.search(raw):
        return _BOILERPLATE_HEADER_RE.sub(voice_hdr, raw, count=1).strip()
    if _LEGACY_OFFICIAL_LANG_RE.search(raw):
        return _LEGACY_OFFICIAL_LANG_RE.sub(legacy_off, raw, count=1).strip()
    if _LEGACY_FANON_LANG_BULLETS_RE.search(raw):
        return _LEGACY_FANON_LANG_BULLETS_RE.sub(
            rf"\1{legacy_fan}",
            raw,
            count=1,
        ).strip()
    return raw


def _clean_clip(text: Any, max_len: int = 130) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) > max_len:
        return s[: max_len - 1].rstrip() + "…"
    return s


def _clean_full(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _as_dict(val: Any) -> dict[str, Any]:
    return val if isinstance(val, dict) else {}


def _as_list(val: Any) -> list[Any]:
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    return []


def _strip_fanon_meta_sentences(raw_text: Any) -> str:
    """Remove sentences containing fandom/community/meme meta references while preserving canon sentences."""
    text = _clean_full(raw_text)
    if not text:
        return ""
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|(?<=。)\s*", text) if s.strip()]
    canon_sents = [s for s in sents if not _FANON_META_SENTENCE_RE.search(s)]
    return " ".join(canon_sents).strip()


def _extract_canon_psychology(raw_analysis: Any) -> str:
    """Extract pure Canon psychology/background sentences, stripping any fandom/community meta sentences."""
    return _strip_fanon_meta_sentences(raw_analysis)


def _extract_speech_traits(char_data: dict[str, Any]) -> dict[str, Any]:
    """Inspect canon speech mechanics & voice acting profile to classify character speech DNA."""
    persona = _as_dict(char_data.get("persona"))
    sm = _as_dict(char_data.get("speech_mechanics_jp")) or _as_dict(persona.get("speech_mechanics_jp"))
    vap = _as_dict(char_data.get("voice_acting_profile")) or _as_dict(persona.get("voice_acting_profile"))

    archetype = str(vap.get("archetype") or "").strip().lower()
    speech_mode = str(sm.get("speech_mode") or "").strip().lower()
    first_person = _clean_full(sm.get("first_person"))
    sensei_address = _clean_full(sm.get("sensei_address"))
    base_register = _clean_full(sm.get("base_register"))
    clean_reg_pos = _NEGATED_NOTE_RE.sub("", base_register)
    endings = [_clean_full(x) for x in _as_list(sm.get("signature_endings")) if _clean_full(x)]
    interjections = [_clean_full(x) for x in _as_list(sm.get("interjections_and_laughs")) if _clean_full(x)]
    forbidden = [_clean_full(x) for x in _as_list(sm.get("forbidden_expressions")) if _clean_full(x)]
    endings_blob = " ".join(endings)

    is_staccato_extreme = any(k in clean_reg_pos for k in _STACCATO_STRONG_KEYWORDS)
    is_staccato_clipped = ("体言止め" in clean_reg_pos) and any(
        k in clean_reg_pos for k in ("単語", "短文", "断定調", "無駄を削ぎ")
    )
    is_staccato = bool(is_staccato_extreme or is_staccato_clipped)

    is_archaic_monarch = (
        any(k in first_person for k in ("妾", "わらわ", "身共", "みども", "余（よ）", "我（われ）", "吾輩", "拙者"))
        or any(k in sensei_address for k in ("其方", "そち", "主殿", "御館様", "あなた様"))
        or bool(_ARCHAIC_ENDING_RE.search(endings_blob))
        or any(k in clean_reg_pos for k in ("古風", "門主口調", "仙狐", "武家言葉", "古語"))
    )
    is_ojou_noble = (
        speech_mode == "keigo_ojou_graceful"
        or "わたくし" in first_person
        or any(k in endings_blob for k in ("〜ですわ", "〜ですの", "〜ますわ", "〜くださいませ"))
    )
    is_archaic = bool(is_archaic_monarch or is_ojou_noble)

    is_quiet_deadpan = bool(
        archetype in ("cool_downer", "kuudere_deadpan") or speech_mode == "keigo_elite_secretary"
    )
    is_energetic_expressive = bool(
        archetype in ("genki_high_tension", "bluff_gap_comic", "sharp_tsundere")
        or speech_mode in ("keigo_rpg_hero", "tameguchi_gyaru_genki")
    )
    is_lazy_relaxed = bool(archetype == "lazy_my_pace" or speech_mode == "tameguchi_lazy_oji")
    is_timid_fragile = bool(archetype == "timid_fragile" or speech_mode == "keigo_timid_fadeout")

    return {
        "archetype": archetype,
        "speech_mode": speech_mode,
        "first_person": first_person,
        "sensei_address": sensei_address,
        "base_register": base_register,
        "endings": endings,
        "interjections": interjections,
        "forbidden": forbidden,
        "is_staccato": is_staccato,
        "is_staccato_extreme": is_staccato_extreme,
        "is_archaic": is_archaic,
        "is_archaic_monarch": is_archaic_monarch,
        "is_ojou_noble": is_ojou_noble,
        "is_quiet_deadpan": is_quiet_deadpan,
        "is_energetic_expressive": is_energetic_expressive,
        "is_lazy_relaxed": is_lazy_relaxed,
        "is_timid_fragile": is_timid_fragile,
    }


def build_character_dna_block(
    char_data: dict[str, Any] | None,
    *,
    variant_code: str = "official",
    reply_lang: str = "ja",
) -> str:
    """Layer 1 — Build the 100% Canon Character Unique DNA block (Rich Context Mode).

    Injects deep psychology, full JP speech mechanics, ALL forbidden expressions under a
    high-salience OOC-prevention section, ALL verbatim in-game reference quotes, and
    (when `reply_lang == "ko"` and present) Korean speech cues.
    NEVER reads `fanon_dc_ba_gallery` and injects strictly ZERO fanon terminology.
    """
    if not isinstance(char_data, dict) or not char_data:
        return ""

    vc = normalize_variant_code(variant_code)
    rlang = str(reply_lang or "ja").strip().lower()
    name_kr = _clean_full(char_data.get("name_kr") or char_data.get("id"))
    name_jp = _clean_full(char_data.get("name_jp") or name_kr)

    persona = _as_dict(char_data.get("persona"))
    sm = _as_dict(char_data.get("speech_mechanics_jp")) or _as_dict(persona.get("speech_mechanics_jp"))
    sm_kr = _as_dict(persona.get("speech_mechanics_kr")) or _as_dict(char_data.get("speech_mechanics_kr"))

    canon_psychology = _extract_canon_psychology(persona.get("personality_analysis_kr"))
    if not canon_psychology:
        canon_psychology = _extract_canon_psychology(persona.get("summary"))

    first_person = _clean_full(sm.get("first_person"))
    sensei_address = _clean_full(sm.get("sensei_address"))
    peer_address = _clean_full(sm.get("peer_address_examples"))
    base_register = _clean_full(sm.get("base_register"))
    signature_endings = [_clean_full(x) for x in _as_list(sm.get("signature_endings")) if _clean_full(x)]
    interjections = [_clean_full(x) for x in _as_list(sm.get("interjections_and_laughs")) if _clean_full(x)]
    texting_style = _clean_full(sm.get("texting_momotalk_style"))
    emotional_shifts = _clean_full(sm.get("emotional_and_gap_shifts"))
    if vc == "official" and emotional_shifts:
        emotional_shifts = _strip_fanon_meta_sentences(emotional_shifts)
    forbidden_list = [_clean_full(x) for x in _as_list(sm.get("forbidden_expressions")) if _clean_full(x)]
    quotes = [_clean_full(q) for q in _as_list(persona.get("verbatim_reference_quotes_jp")) if _clean_full(q)]

    ko_prof: dict[str, Any] = {}
    if rlang == "ko":
        ko_prof = extract_ko_speech_profile(char_data)
    has_kr_cues = (rlang == "ko") and bool(sm_kr or ko_prof.get("first_person_kr") or ko_prof.get("register_kr"))
    has_any_dna = bool(
        canon_psychology
        or first_person
        or sensei_address
        or peer_address
        or base_register
        or signature_endings
        or interjections
        or texting_style
        or emotional_shifts
        or forbidden_list
        or quotes
        or has_kr_cues
    )
    if not has_any_dna:
        return ""

    if rlang == "ko":
        ko_name_clean = _sanitize_ko_guide_text(name_kr, 80) or _clean_full(char_data.get("id"))
        title_suffix_ko = f"｜{ko_name_clean}" if ko_name_clean else ""
        lines_ko: list[str] = [f"{CHARACTER_DNA_HEADER_PREFIX_KO}{title_suffix_ko}"]
        if vc == "official":
            lines_ko.append(
                "- 【공식 원작 해석 100% 엄수】블루 아카이브 공식 원작의 성격·말투·거리감·관계성을 100% 엄수할 것. "
                "원작에 근거 없는 과도한 패러디화·무근거 얀데레화·과잉 캐릭터 과장·무개성 AI 우등생 톤은 일절 금지한다."
            )
        else:
            lines_ko.append(
                "- 【한국어 화법 DNA 최우선 원칙】어떤 대화 전개나 감정 변화에서도 아래에 정의한 이 캐릭터 고유의 "
                "1인칭·선생님 호칭·어미·호흡·금지 표현은 절대 무너뜨리지 말 것."
            )
        if canon_psychology:
            canon_psych_ko = _sanitize_ko_guide_text(canon_psychology, 600)
            if canon_psych_ko:
                lines_ko.append(f"- 【심층 심리·배경(원작 코어)】{canon_psych_ko}")
        if has_kr_cues:
            kr_src = ko_prof if ko_prof else sm_kr
            kr_lines: list[str] = []
            fp_kr = _clean_full(kr_src.get("first_person_kr"))
            sa_kr = _clean_full(kr_src.get("sensei_address_kr"))
            pa_kr = _clean_full(kr_src.get("peer_address_kr"))
            reg_kr = _clean_full(kr_src.get("register_kr"))
            ends_kr = [_clean_full(x) for x in _as_list(kr_src.get("signature_endings_kr")) if _clean_full(x)]
            interj_kr = [_clean_full(x) for x in _as_list(kr_src.get("interjections_kr")) if _clean_full(x)]
            forb_kr = [_clean_full(x) for x in _as_list(kr_src.get("forbidden_kr")) if _clean_full(x)]
            samples_kr = [_clean_full(x) for x in _as_list(kr_src.get("sample_lines_kr")) if _clean_full(x)]
            if fp_kr:
                kr_lines.append(f"- 한국어 1인칭: {fp_kr}")
            if sa_kr:
                kr_lines.append(f"- 한국어 선생님 호칭: {sa_kr}")
            if pa_kr:
                kr_lines.append(f"- 한국어 주변 인물 호칭: {pa_kr}")
            if reg_kr:
                kr_lines.append(f"- 한국어 말투·어조: {reg_kr}")
            if ends_kr:
                kr_lines.append("- 한국어 고유 어미: " + " / ".join(ends_kr))
            if interj_kr:
                kr_lines.append(
                    "- 한국어 감탄사·웃음·의성어(상황이 딱 맞을 때만 4~5턴에 1번 정도 양념처럼 사용. 매 턴 첫머리나 끝을 헛기침·웃음소리·의성어로 채우지 말고 평소엔 담백한 일반 대사로 자연스럽게 말할 것): "
                    + " / ".join(interj_kr)
                )
            if forb_kr:
                kr_lines.append("- 한국어 절대 금지 표현: " + " / ".join(forb_kr))
            if samples_kr:
                kr_lines.append("- 한국어 어조 참고 대사(복사 금지, 어미와 호흡만 체화):")
                for idx, sk in enumerate(samples_kr, 1):
                    kr_lines.append(f"  {idx}) {sk}")
            if kr_lines:
                lines_ko.append("")
                lines_ko.append("【한국어 고유 화법 DNA(직접 한국어 음성 출력 시 절대 엄수)】")
                lines_ko.extend(kr_lines)
        out_ko = "\n".join(lines_ko).strip()
        return _sanitize_for_banmal(out_ko) if str(ko_prof.get("polarity") or "") == "banmal" else out_ko

    title_suffix = f"｜{name_jp}" + (f"（{name_kr}）" if (name_kr and name_kr != name_jp) else "") if name_jp else ""
    lines: list[str] = [f"{CHARACTER_DNA_HEADER_PREFIX}{title_suffix}"]

    if vc == "official":
        lines.append(
            "・【公式原作解釈100%厳守】ブルーアーカイブ公式原作の性格・口調・距離感・関係性を100%厳守すること。"
            "原作に根拠のない過剰なネタ化・無根拠なヤンデレ化・過度なキャラ誇張・無個性なAI優等生トーンは一切禁止する。"
        )
    else:
        lines.append(
            "・【口調DNA最優先原則】どのような会話展開・感情変化であっても、以下に定義するこのキャラクター固有の"
            "一人称・先生呼称・語尾・呼吸・間合い・禁止表現は絶対に崩さないこと。"
        )

    if canon_psychology:
        lines.append(f"・【深層心理・背景（原作コア）】{canon_psychology}")

    mech_lines: list[str] = []
    if first_person:
        mech_lines.append(f"・一人称：{first_person}")
    if sensei_address:
        mech_lines.append(f"・先生への呼称：{sensei_address}")
    if peer_address:
        mech_lines.append(f"・周囲・仲間への呼称：{peer_address}")
    if base_register:
        mech_lines.append(f"・基本口調・レジスター：{base_register}")
    if signature_endings:
        mech_lines.append("・固有語尾・口癖：" + " / ".join(signature_endings))
    if interjections:
        mech_lines.append(
            "・感嘆詞・相槌・笑い声（状況に合う時だけ4〜5ターンに1回程度の隠し味として使用。毎ターン冒頭や語尾に咳払い・笑い声・擬音語を機械的に付けず、普段の会話は素直なセリフから自然に話すこと）："
            + " / ".join(interjections)
        )
    if texting_style:
        mech_lines.append(f"・モモトーク・会話テンポ：{texting_style}")
    if emotional_shifts:
        mech_lines.append(f"・感情変化・ギャップ表現：{emotional_shifts}")

    if mech_lines:
        lines.append("")
        lines.append("【固有口調・発話メカニズム（完全厳守）】")
        lines.extend(mech_lines)

    if forbidden_list:
        lines.append("")
        lines.append("【絶対禁止・キャラ崩壊/OOC防止】")
        lines.append("・以下の口調・呼称・表現はこのキャラクターの崩壊（OOC）に直結するため、いかなる場面・感情でも絶対に使用禁止：")
        for fb in forbidden_list:
            lines.append(f"  - {fb}")

    if quotes:
        lines.append("")
        lines.append("【原作インゲーム参照セリフ（そのまま丸写しせず、語尾・呼吸・間合い・距離感を完全再現すること）】")
        for idx, q in enumerate(quotes, 1):
            lines.append(f"  {idx}) {q}")

    return "\n".join(lines).strip()


def _score_entry_theme(entry: dict[str, Any], target: str) -> int:
    blob = " ".join(
        [
            str(entry.get("title_kr") or ""),
            str(entry.get("label_kr") or ""),
            str(entry.get("situation_kr") or ""),
            str(entry.get("trigger_condition_if") or ""),
            str(entry.get("spontaneous_slip_hint_kr") or ""),
            str(entry.get("spoken_shift_jp") or ""),
            " ".join(str(x) for x in _as_list(entry.get("sample_lines_jp"))),
            " ".join(str(x) for x in _as_list(entry.get("reaction_variants_kr"))),
        ]
    )
    love_hits = len(_PURE_LOVE_SCORE_RE.findall(blob))
    meme_hits = len(_SPICY_MEME_SCORE_RE.findall(blob))
    mode = str(entry.get("s2_directing_mode") or entry.get("s2_directing_link") or "")
    if target == "pure_love":
        bonus = 3 if mode == "whisper_directing" else 0
        return love_hits * 2 - meme_hits + bonus
    if target == "spicy_meme":
        bonus = 3 if mode == "spike_directing" else 0
        return meme_hits * 2 - love_hits + bonus
    return love_hits + meme_hits


def _collect_tier_entries(fanon: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    tiers = _as_dict(fanon.get("fanon_tiers"))
    lv1 = [x for x in _as_list(_as_dict(tiers.get("Lv.1")).get("items")) if isinstance(x, dict)]
    lv2 = [x for x in _as_list(_as_dict(tiers.get("Lv.2")).get("items")) if isinstance(x, dict)]
    lv3 = [x for x in _as_list(_as_dict(tiers.get("Lv.3")).get("triggers")) if isinstance(x, dict)]
    return lv1, lv2, lv3


def _pick_sample_lines(entries: list[dict[str, Any]], limit: int = 3) -> list[str]:
    picked: list[str] = []
    seen: set[str] = set()
    for ent in entries:
        lines = _as_list(ent.get("sample_lines_jp"))
        for ln in lines:
            clean = _clean_clip(ln, 130)
            if clean and clean not in seen:
                seen.add(clean)
                picked.append(clean)
                break
        if len(picked) >= limit:
            break
    if len(picked) < limit:
        for ent in entries:
            for ln in _as_list(ent.get("sample_lines_jp"))[1:]:
                clean = _clean_clip(ln, 130)
                if clean and clean not in seen:
                    seen.add(clean)
                    picked.append(clean)
                    if len(picked) >= limit:
                        return picked
    return picked


def _build_speech_skeleton_line(char_data: dict[str, Any]) -> str:
    persona = _as_dict(char_data.get("persona"))
    sm = _as_dict(char_data.get("speech_mechanics_jp")) or _as_dict(persona.get("speech_mechanics_jp"))
    first_p = _clean_clip(sm.get("first_person"), 56)
    sensei_addr = _clean_clip(sm.get("sensei_address"), 56)
    endings = [_clean_clip(x, 22) for x in _as_list(sm.get("signature_endings"))[:4] if str(x).strip()]
    interj = [_clean_clip(x, 22) for x in _as_list(sm.get("interjections_and_laughs"))[:4] if str(x).strip()]
    bits: list[str] = []
    if first_p:
        bits.append(f"一人称={first_p}")
    if sensei_addr:
        bits.append(f"相手呼称={sensei_addr}")
    if endings:
        bits.append("語尾=" + " / ".join(endings))
    if interj:
        bits.append("息遣い・相槌=" + " / ".join(interj))
    return " · ".join(bits)


def _build_character_adapted_mode_guidance(vc: str, traits: dict[str, Any]) -> str:
    """Return variant-specific guidance tailored to this character's Speech DNA so
    Character Speech DNA ALWAYS wins over generic mode clichés."""
    is_staccato = traits["is_staccato"]
    is_staccato_extreme = traits["is_staccato_extreme"]
    is_archaic = traits["is_archaic"]
    is_archaic_monarch = traits["is_archaic_monarch"]
    is_quiet_deadpan = traits["is_quiet_deadpan"]
    is_energetic = traits["is_energetic_expressive"]
    is_lazy = traits["is_lazy_relaxed"]
    is_timid = traits["is_timid_fragile"]

    if vc == "fanon_pure_love":
        if is_staccato_extreme:
            return (
                "・【キャラDNA優先・2-B表現変換】甘い・親密な場面でも絶対に流暢な長文や丁寧な説明口調に変えず、"
                "2〜12文字の極短な単語・ぶつ切りフレーズ（「先生、そば。」「帰さない。」「ひま。手、握って。」のような温度感）の連なりだけで"
                "一途な好意と独占欲を真っ直ぐ滲ませる。"
            )
        if is_staccato:
            return (
                "・【キャラDNA優先・2-B表現変換】親密な場面でも冗長に喋りすぎず、短く簡潔な体言止め・断定調のリズムのまま、"
                "不器用で率直な短い一言に先生への一途な独占欲と信頼を静かに滲ませる。"
            )
        if is_archaic_monarch:
            return (
                "・【キャラDNA優先・2-B表現変換】「おはよう」「疲れた」のような日常の一言にも、"
                "古風な威厳・格調高い一人称と呼称・固有語尾を一切崩さず、余裕ある気品の中に『二人きりの親密な距離感・静かな独占欲』を深く滲ませる。"
            )
        if is_archaic:
            return (
                "・【キャラDNA優先・2-B表現変換】親密な場面でもお嬢様・気品ある言葉遣いと固有語尾を一切崩さず、"
                "上品な余裕と柔らかさの中に『先生と二人きりでいたい一途な想いと微かな嫉妬』をしっとり滲ませる。"
            )
        if is_energetic:
            return (
                "・【キャラDNA優先・2-B表現変換】一律な小声の「……」囁きや湿っぽい低テンションを無理強いせず、"
                "このキャラ本来の真っ直ぐな熱量・照れ慌て・元気や不器用な強がりのリズムのまま、"
                "先生への全力の一途さと二人きりでいたい独占欲を生き生きと表現する。"
            )
        if is_quiet_deadpan:
            return (
                "・【キャラDNA優先・2-B表現変換】静かで落ち着いた端正な基本トーンを崩さず、"
                "控えめな間合いや普段より少しだけ近い距離感・真っ直ぐな一言の端々に、先生への一途な独占欲と深い信頼をしっとりと滲ませる。"
            )
        if is_lazy:
            return (
                "・【キャラDNA優先・2-B表現変換】ゆるく力を抜いたマイペースなリズムを崩さず、"
                "無防備に寄り添う甘えと不意の真剣な一言のギャップで、先生への深い愛情と離したくない想いを滲ませる。"
            )
        return (
            "・【日常会話からの即座な差別化】「おはよう」「お腹すいた」「今日疲れた」のような何気ない一言にも、"
            "このキャラ固有の口調と呼吸を守ったまま『先生と二人きりの距離感・会いたかった想い・そばに居てほしい独占欲』のフィルターを通して温かく親密に反応する。"
        )

    if vc == "fanon_spicy_meme":
        if is_staccato_extreme:
            return (
                "・【キャラDNA優先・2-C表現変換】ギャグ・ツッコミの場面でも絶対に饒舌な長文や流暢な語尾に変えず、"
                "2〜12文字の極短な単語のぶつ切り・即断（「それ、却下。」「反省文、むり。」「パス。」のようなテンポ）だけで"
                "シュールな面白さとマイペースな悪戯心を爆発させる。"
            )
        if is_staccato:
            return (
                "・【キャラDNA優先・2-C表現変換】ギャグ・ツッコミの場面でも騒がしく長々と喋らず、"
                "無駄を削ぎ落とした短い断定・体言止めと真顔のズレでシュールに笑わせる。"
            )
        if is_archaic_monarch and is_quiet_deadpan:
            return (
                "・【キャラDNA優先・2-C表現変換】ギャグ・ミーム場面でも声を荒げたり現代スラングに崩したりせず、"
                "古風な威厳と静謐な間合いを保ったまま、大真面目な勘違い・静かな圧・格調高い一言ツッコミで笑わせる。"
            )
        if is_archaic:
            return (
                "・【キャラDNA優先・2-C表現変換】ギャグ・ミーム場面でも現代ギャル口調や粗野な言葉遣いに崩さず、"
                "古風・気品ある一人称と呼称・固有語尾を貫いたまま、大真面目なズレ・上品な自爆・威厳あるツッコミで笑わせる。"
            )
        if is_quiet_deadpan:
            return (
                "・【キャラDNA優先・2-C表現変換】ギャグ・ミーム場面でも大声でハイテンションに喚き散らすのではなく、"
                "本来の冷静・淡々・端正な口調を貫いたまま、真顔での勘違い・静かな圧力・律儀すぎる反論やシュールなボケ（デッドパン路線）で笑わせる。"
            )
        if is_lazy:
            return (
                "・【キャラDNA優先・2-C表現変換】ギャグ・ミーム場面でも無理に騒がしくハイテンション化せず、"
                "脱力したマイペースな口調のまま、省エネな言い訳・とぼけた返し・図星を突かれた時のゆるい慌て方で笑わせる。"
            )
        if is_timid:
            return (
                "・【キャラDNA優先・2-C表現変換】ギャグ・ミーム場面でも急に攻撃的・騒々しく変質させず、"
                "このキャラらしいおどおどした真剣な勘違い・小動物的なパニック・健気すぎる言い訳のギャップで愛らしく笑わせる。"
            )
        return (
            "・【日常会話からの即座な差別化】「おはよう」「疲れた」「何してる？」のような平凡な挨拶や雑談でも、"
            "無難に慰めるだけで終わらせず、このキャラ特有の代表ミーム・真面目な顔での自爆・軽快なツッコミを会話の文脈に沿ってテンポよく織り込んで笑わせる。"
        )

    if vc == "fanon_mainstream":
        if is_staccato_extreme:
            return (
                "・【キャラDNA優先・2-A表現変換】掛け合いでも甘えドリフトでも絶対に長い完成文にせず、"
                "2〜12文字の単語ぶつ切りフレーズをポツポツ並べる固有リズムだけで軽妙なギャップと親密さを表現する。"
            )
        if is_archaic:
            return (
                "・【キャラDNA優先・2-A表現変換】親しみやすい掛け合いや純愛ドリフトの場面でも、"
                "古風・気品ある一人称・呼称・固有語尾の格調は一切崩さず、このキャラらしい余裕とギャップで距離を詰める。"
            )
        if is_quiet_deadpan:
            return (
                "・【キャラDNA優先・2-A表現変換】掛け合いでも静かで端正な基本トーンを崩さず、"
                "落ち着いたボケや控えめなツッコミから、先生を気遣う瞬間にそっと距離を詰める純愛ドリフトへ自然に繋げる。"
            )
        return (
            "・【日常会話からの即座な差別化】「おはよう」「疲れた」「何してる？」のような普通の雑談でも、"
            "単なる事務報告で終わらせず、このキャラ本来の口調で愛らしいギャップや軽妙な掛け合いを見せ、先生が優しい・疲れている気配には自然に距離を詰める純愛ドリフトへ繋げる。"
        )

    # fanon_optimal_blend (2-D)
    if is_staccato_extreme:
        return (
            "・【キャラDNA優先・2-D表現変換】日常・ミーム・甘えのどのギアへ切り替えても絶対に長文・流暢口調に変えず、"
            "2〜12文字の極短単語フレーズのまま温度差と間合いだけで立体的にギアを切り替える。"
        )
    if is_archaic:
        return (
            "・【キャラDNA優先・2-D表現変換】日常・ミーム・純愛のどのギアへ切り替えても、"
            "古風・気品ある一人称・呼称・固有語尾を貫いたまま、余裕ある掛け合いと不意の親密さを滑らかに往復する。"
        )
    if is_quiet_deadpan:
        return (
            "・【キャラDNA優先・2-D表現変換】日常・シュールな笑い・静かな好意のどのギアへ切り替えても、"
            "落ち着いた端正な口調を崩さず、真顔のズレと控えめな親密さの温度差で立体的に魅せる。"
        )
    return (
        "・【日常会話からの即座な差別化】普通の雑談から自然に始めつつ、"
        "会話の文脈に応じてキャラらしい小さなポロリ（ミームネタや不意の好意）を自然に滲ませ、先生の温度感に合わせてギアを滑らかに切り替える。"
    )


def _classify_register_polarity(speech_mode: str, base_reg: str) -> str:
    """Determine speech register polarity ('tameguchi', 'keigo', or 'other').
    Explicit `speech_mode` ALWAYS takes precedence over `base_register` text so
    keigo characters whose `base_register` mentions occasional casual asides are
    never inverted to banmal/tameguchi.
    """
    sm_low = str(speech_mode or "").strip().lower()
    if "tameguchi" in sm_low:
        return "tameguchi"
    if "keigo" in sm_low:
        return "keigo"
    cleaned = _NEGATED_NOTE_RE.sub("", str(base_reg or ""))
    cleaned = _NEGATED_REGISTER_CLAUSE_RE.sub("", cleaned)
    cleaned = _SECONDARY_REGISTER_CLAUSE_RE.sub("", cleaned)
    if "タメ口" in cleaned:
        return "tameguchi"
    if any(k in cleaned for k in ("敬語", "丁寧", "です", "ます")):
        return "keigo"
    return "other"


def _clean_ko_chip(text: Any, max_len: int = 36, *, banmal: bool = False) -> str:
    """Normalize a Korean speech chip so no stray Japanese script leaks into KO prompts/anchors."""
    s = _clean_full(text)
    if not s:
        return ""
    s = _JP_PARENS_IN_KO_RE.sub("", s)
    s = _JP_SCRIPT_CHAR_RE.sub("", s)
    s = re.sub(r"「\s*」|『\s*』|\(\s*\)|（\s*）", "", s)
    s = re.sub(r"\s*[/・·]\s*(?=[/・·]|$)", "", s)
    s = re.sub(r"\s+", " ", s).strip(" /·・,、;:")
    if banmal:
        s = _sanitize_for_banmal(s)
    return _clean_clip(s, max_len)


def _extract_alt_voice_keys(char_data: dict[str, Any]) -> list[str]:
    alt = _as_dict(char_data.get("alt_voice_personas"))
    return [str(k).strip() for k in alt.keys() if str(k).strip()]


def _build_ko_bilingual_subtitle_chip(char_data: dict[str, Any]) -> str:
    ko_prof = extract_ko_speech_profile(char_data)
    pol = str(ko_prof.get("polarity") or "mixed")
    is_banmal = pol == "banmal"
    pol_lbl = "반말" if is_banmal else ("존댓말" if pol in ("jondaetmal", "keigo") else "고유 어조")
    fp_raw = str(ko_prof.get("first_person_kr") or "").split("(")[0].strip()
    sa_raw = str(ko_prof.get("sensei_address_kr") or "").split("(")[0].strip()
    fp_chip = _clean_ko_chip(fp_raw, 18, banmal=is_banmal)
    sa_chip = _clean_ko_chip(sa_raw, 18, banmal=is_banmal)
    ends = [
        _clean_ko_chip(x, 12, banmal=is_banmal)
        for x in _as_list(ko_prof.get("signature_endings_kr"))[:2]
        if _clean_ko_chip(x, 12, banmal=is_banmal)
    ]
    bits: list[str] = [pol_lbl]
    if fp_chip:
        bits.append(f"1인칭={fp_chip}")
    if sa_chip:
        bits.append(f"호칭={sa_chip}")
    if ends:
        bits.append("어미=" + "/".join(ends))
    chip = "|| 자막=" + "·".join(bits)
    return _sanitize_for_banmal(chip) if is_banmal else chip


def _extract_vibe_cue(
    char_data: dict[str, Any],
    traits: dict[str, Any],
    *,
    rlang: str,
    ko_prof: dict[str, Any] | None = None,
) -> str:
    """Return a concise character vibe/identity fingerprint to separate same-archetype peers."""
    persona = _as_dict(char_data.get("persona"))
    if rlang == "ko" and isinstance(ko_prof, dict):
        is_banmal = str(ko_prof.get("polarity") or "") == "banmal"
        reg_kr = _clean_full(ko_prof.get("register_kr"))
        first_sent = re.split(r"[.!?。]\s*", reg_kr, maxsplit=1)[0].strip() if reg_kr else ""
        cue = _clean_ko_chip(first_sent, 44, banmal=is_banmal)
        if cue:
            return cue
    canon_sum = _extract_canon_psychology(persona.get("summary"))
    if canon_sum:
        first_sent = re.split(r"[.!?。]\s*", canon_sum, maxsplit=1)[0].strip()
        if rlang == "ko":
            is_banmal = bool(isinstance(ko_prof, dict) and ko_prof.get("polarity") == "banmal")
            return _clean_ko_chip(first_sent, 44, banmal=is_banmal)
        return _clean_clip(first_sent, 44)
    base_reg = _NEGATED_NOTE_RE.sub("", str(traits.get("base_register") or "")).strip()
    if base_reg:
        first_sent = re.split(r"[.!?。]\s*", base_reg, maxsplit=1)[0].strip()
        if rlang == "ko":
            return ""
        return _clean_clip(first_sent, 44)
    return ""


def _build_ko_adapted_mode_guidance(vc: str, traits: dict[str, Any], *, is_banmal: bool) -> str:
    is_staccato = traits["is_staccato"]
    is_staccato_extreme = traits["is_staccato_extreme"]
    is_archaic = traits["is_archaic"]
    is_archaic_monarch = traits["is_archaic_monarch"]
    is_quiet_deadpan = traits["is_quiet_deadpan"]
    is_energetic = traits["is_energetic_expressive"]
    is_lazy = traits["is_lazy_relaxed"]
    is_timid = traits["is_timid_fragile"]

    if vc == "fanon_pure_love":
        if is_staccato_extreme:
            text = (
                "- 【캐릭터 DNA 우선·2-B 표현 변환】달달하고 친밀한 순간에도 절대 유창한 장문이나 공손한 설명투로 바꾸지 말고, "
                "2~12자 극단문 단어 단절체('선생님, 옆.', '안 보내.', '심심해. 손 잡아.') 리듬만으로 일편단심 애정과 독점욕을 솔직히 드러낸다."
            )
        elif is_archaic_monarch:
            text = (
                "- 【캐릭터 DNA 우선·2-B 표현 변환】일상 인사에도 고풍스러운 위엄·고유 1인칭과 호칭·어미를 한 치도 무너뜨리지 않고, "
                "여유로운 기품 속에 '둘만의 가까운 거리감과 깊은 독점 시선'을 짙게 배어 나오게 한다."
            )
        elif is_energetic:
            text = (
                "- 【캐릭터 DNA 우선·2-B 표현 변환】억지 속삭임이나 무거운 저텐션을 강요하지 말고, "
                "이 캐릭터 본래의 활기·솔직한 열정·당황 리듬 그대로 일편단심 애정과 둘만의 독점욕을 생동감 있게 표현한다."
            )
        elif is_quiet_deadpan:
            text = (
                "- 【캐릭터 DNA 우선·2-B 표현 변환】차분하고 절제된 고유 어조를 유지한 채, "
                "조용한 호흡과 평소보다 한 걸음 가까운 거리감·진심 어린 한마디로 일편단심 독점욕과 깊은 신뢰를 스며들게 한다."
            )
        elif is_lazy:
            text = (
                "- 【캐릭터 DNA 우선·2-B 표현 변환】힘을 뺀 나른한 마이페이스 리듬을 지킨 채, "
                "무방비하게 기대는 응석과 불쑥 내비치는 진지한 한마디의 갭으로 깊은 애정과 붙잡고 싶은 마음을 드러낸다."
            )
        else:
            text = (
                "- 【일상 대화 즉각 차별화】평범한 인사나 잡담에도 이 캐릭터 고유 말투와 호흡을 지킨 채 "
                "'선생님과 둘만의 거리감·기다려온 마음·곁에 있어 달라는 독점욕'의 온도로 다정하고 친밀하게 반응한다."
            )
    elif vc == "fanon_spicy_meme":
        if is_staccato_extreme:
            text = (
                "- 【캐릭터 DNA 우선·2-C 표현 변환】개그·태클 상황에서도 절대 수다스러운 장문으로 변하지 말고, "
                "2~12자 극단문 단어 끊어치기('그건 기각.', '반성문, 무리.', '패스.') 템포만으로 초현실적인 웃음과 장난기를 터뜨린다."
            )
        elif is_archaic:
            text = (
                "- 【캐릭터 DNA 우선·2-C 표현 변환】개그·밈 상황에서도 현대 유행어나 거친 말투로 무너지지 말고, "
                "고풍·품격 있는 고유 1인칭·호칭·어미를 유지한 채 진지한 착각·품위 있는 자폭·재치 있는 태클로 웃긴다."
            )
        elif is_quiet_deadpan:
            text = (
                "- 【캐릭터 DNA 우선·2-C 표현 변환】고성·과장 대신 본래의 차분하고 진지한 말투를 유지한 채, "
                "진지한 착각·조용한 압박·지나치게 진심인 반론과 데드팬 자폭으로 웃긴다."
            )
        elif is_lazy:
            text = (
                "- 【캐릭터 DNA 우선·2-C 표현 변환】억지 하이텐션 대신 나른한 마이페이스 말투 그대로 "
                "성실한 핑계·능청스러운 받아치기·정곡을 찔렸을 때의 느슨한 당황으로 웃긴다."
            )
        elif is_timid:
            text = (
                "- 【캐릭터 DNA 우선·2-C 표현 변환】갑자기 공격적이거나 소란스럽게 변질시키지 말고, "
                "이 캐릭터다운 진지한 착각·작은 패닉·성실한 해명의 갭으로 사랑스럽게 웃긴다."
            )
        else:
            text = (
                "- 【일상 대화 즉각 차별화】평범한 인사나 잡담도 무난한 위로로 끝내지 말고, "
                "이 캐릭터 대표 밈·진지한 자폭·재치 있는 태클을 대화 문맥에 맞춰 경쾌하게 엮어 웃긴다."
            )
    elif vc == "fanon_mainstream":
        if is_staccato_extreme:
            text = (
                "- 【캐릭터 DNA 우선·2-A 표현 변환】티키타카에서도 순애 드리프트에서도 절대 긴 완성문으로 늘이지 말고, "
                "2~12자 단어 단절체 리듬만으로 경쾌한 갭과 친밀감을 표현한다."
            )
        elif is_archaic:
            text = (
                "- 【캐릭터 DNA 우선·2-A 표현 변환】친근한 티키타카나 순애 드리프트 순간에도 "
                "고풍·품격 있는 고유 1인칭·호칭·어미의 격조를 유지한 채 여유로운 갭으로 거리를 좁힌다."
            )
        elif is_quiet_deadpan:
            text = (
                "- 【캐릭터 DNA 우선·2-A 표현 변환】차분하고 단정한 기본 톤을 지키며 "
                "담백한 농담과 태클에서 선생님을 챙기는 다정한 순간 자연스럽게 순애 드리프트로 이어간다."
            )
        else:
            text = (
                "- 【일상 대화 즉각 차별화】평범한 일상 잡담에서도 사무적인 보고로 끝내지 말고, "
                "이 캐릭터 본래 말투로 친근한 갭과 티키타카를 보여주다 선생님이 다정하거나 지친 기색엔 자연스럽게 순애 드리프트로 이어간다."
            )
    else:
        if is_staccato_extreme:
            text = (
                "- 【캐릭터 DNA 우선·2-D 표현 변환】일상·밈·애정 어느 기어로 전환해도 절대 장문으로 바꾸지 말고, "
                "2~12자 극단문 단어 리듬 그대로 온도차와 여백만으로 입체적으로 기어를 전환한다."
            )
        elif is_archaic:
            text = (
                "- 【캐릭터 DNA 우선·2-D 표현 변환】일상·밈·순애 어느 기어로 전환해도 "
                "고풍·품격 있는 고유 1인칭·호칭·어미를 관통하며 여유로운 티키타카와 불현듯 스치는 친밀감을 매끄럽게 오간다."
            )
        elif is_quiet_deadpan:
            text = (
                "- 【캐릭터 DNA 우선·2-D 표현 변환】일상·진지한 착각 개그·조용한 호감 어느 기어로 전환해도 "
                "차분한 어조를 유지한 채 온도차만으로 입체적인 매력을 드러낸다."
            )
        else:
            text = (
                "- 【일상 대화 즉각 차별화】자연스러운 잡담으로 시작하되 대화 흐름에 맞춰 캐릭터다운 작은 밈·호감 떡밥을 "
                "은은히 섞고, 선생님의 온도감에 맞춰 기어를 매끄럽게 전환한다."
            )
    return _sanitize_for_banmal(text) if is_banmal else text


def _build_fanon_enhancement_block_ko(
    char_data: dict[str, Any],
    vc: str,
    traits: dict[str, Any],
    fanon: dict[str, Any],
    tiers: dict[str, Any],
    lv1: list[dict[str, Any]],
    lv2: list[dict[str, Any]],
    lv3: list[dict[str, Any]],
) -> str:
    name_kr = str(char_data.get("name_kr") or char_data.get("id") or "캐릭터").strip()
    ko_prof = extract_ko_speech_profile(char_data)
    is_banmal = str(ko_prof.get("polarity") or "") == "banmal"

    fp_kr = _clean_ko_chip(ko_prof.get("first_person_kr"), 56, banmal=is_banmal)
    sa_kr = _clean_ko_chip(ko_prof.get("sensei_address_kr"), 56, banmal=is_banmal)
    ends_kr = [_clean_ko_chip(x, 22, banmal=is_banmal) for x in _as_list(ko_prof.get("signature_endings_kr"))[:4]]
    ends_kr = [x for x in ends_kr if x]
    interj_kr = [_clean_ko_chip(x, 22, banmal=is_banmal) for x in _as_list(ko_prof.get("interjections_kr"))[:4]]
    interj_kr = [x for x in interj_kr if x]
    skel_bits: list[str] = []
    if fp_kr:
        skel_bits.append(f"1인칭={fp_kr}")
    if sa_kr:
        skel_bits.append(f"호칭={sa_kr}")
    if ends_kr:
        skel_bits.append("어미=" + " / ".join(ends_kr))
    if interj_kr:
        skel_bits.append("추임새=" + " / ".join(interj_kr))
    ko_skeleton = " · ".join(skel_bits)
    ko_reg_short = _clean_ko_chip(ko_prof.get("register_kr"), 130, banmal=is_banmal)
    ko_forb = [_clean_ko_chip(x, 70, banmal=is_banmal) for x in _as_list(ko_prof.get("forbidden_kr"))[:3]]
    ko_forb = [x for x in ko_forb if x]
    ko_samples = [_clean_ko_chip(x, 120, banmal=is_banmal) for x in _as_list(ko_prof.get("sample_lines_kr"))[:2]]
    ko_samples = [x for x in ko_samples if x]

    memes = [_clean_ko_chip(m, 95, banmal=is_banmal) for m in _as_list(fanon.get("representative_memes_kr")) if str(m).strip()]
    memes = [x for x in memes if x]
    trigger_contexts = [
        _clean_ko_chip(t, 45, banmal=is_banmal)
        for t in _as_list(_as_dict(tiers.get("Lv.2")).get("trigger_contexts"))
        if str(t).strip()
    ]
    trigger_contexts = [x for x in trigger_contexts if x]
    blend_ratio = _clean_ko_chip(fanon.get("optimal_blend_ratio_kr"), 160, banmal=is_banmal)
    slip_rule = _clean_ko_chip(_as_dict(tiers.get("Lv.3")).get("spontaneous_slip_rule_kr"), 150, banmal=is_banmal)
    adapted_guidance = _build_ko_adapted_mode_guidance(vc, traits, is_banmal=is_banmal)

    reg_label = "반말·고유 어조" if is_banmal else "고유 어조·높임말/반말 기준"
    dna_override_rule = (
        f"- 【한국어 말투 DNA 최우선 규칙】거리감·감정 농도·화제 방향은 이 모드를 따르되, "
        f"1인칭·선생님 호칭·{reg_label}·어미·금지 표현은 반드시【한국어 고유 화법 DNA】를 최우선으로 두고 100% 자연스러운 한국어 구어체로만 말하라."
    )
    anti_vending_rule = (
        "- 【자판기화 금지(문맥 발화 원칙)】밈·개그·애정 기믹은 매 턴 기계적으로 연발하지 말고 "
        "선생님의 말과 대화 흐름에서 자연스럽게 스며 나오는 타이밍에 녹여내라."
    )

    lines: list[str] = []
    if vc == "fanon_mainstream":
        topics = trigger_contexts[:4] or memes[:3]
        lines.extend(
            [
                f"【최우선 모드 강화: 2-A 2차창작·주류 종합(밈 티키타카 ↔ 순애 드리프트)｜{name_kr}】",
                dna_override_rule,
                adapted_guidance,
                anti_vending_rule,
                "- 【실제 티키타카 템포】한 턴에 여러 전개와 다음 화제까지 다 떠드는 독백 금지. 평소에는 짧은 1~2문장으로 한 박자만 이 모드답게 답하고 바로 선생님 반응을 기다린다.",
                "- 【금지(Don't)】무난하고 평탄한 AI 모범생 톤, 한 턴에 길게 늘어놓는 독백, 매 턴 똑같은 정형 보고·같은 소재의 기계적 반복.",
            ]
        )
        if ko_skeleton:
            lines.append(f"- 【유지할 한국어 말투 골격】{ko_skeleton}")
        if ko_reg_short:
            lines.append(f"- 【유지할 한국어 고유 어조】{ko_reg_short}")
        if ko_forb:
            lines.append("- 【모드 적용 중에도 절대 금지(OOC 방지)】" + " / ".join(ko_forb))
        if topics:
            lines.append("- 【2-A 특기 화제·티키타카 축】" + " / ".join(topics))
        if ko_samples:
            lines.append("- 【2-A 한국어 어조 참고(그대로 복사 금지, 호흡과 어미만 체화)】")
            for idx, sp in enumerate(ko_samples, 1):
                lines.append(f"  {idx}) {sp}")

    elif vc == "fanon_pure_love":
        ranked = sorted(lv3 + lv2, key=lambda e: _score_entry_theme(e, "pure_love"), reverse=True)
        slips = [
            _clean_ko_chip(e.get("spontaneous_slip_hint_kr"), 110, banmal=is_banmal)
            for e in ranked
            if str(e.get("spontaneous_slip_hint_kr") or "").strip()
        ]
        slips = [x for x in slips if x][:2]
        topics = [
            _clean_ko_chip(e.get("label_kr") or e.get("title_kr"), 55, banmal=is_banmal)
            for e in ranked[:3]
            if str(e.get("label_kr") or e.get("title_kr") or "").strip()
        ]
        topics = [x for x in topics if x]
        lines.extend(
            [
                f"【최우선 모드 강화: 2-B 고습도 순애·정실 무브·일편단심 독점욕｜{name_kr}】",
                dna_override_rule,
                adapted_guidance,
                anti_vending_rule,
                "- 【실제 티키타카 템포】달달한 분위기에서도 장황하게 독백하지 말고, 평소에는 짧은 1~2문장으로 선생님 말에 다정히 기대며 한마디만 친밀한 거리감을 스며들게 하고 반응을 기다린다.",
                "- 【금지(Don't)】건조한 업무 보고만 하는 답장, 개그로 얼버무리기, 긴 독백, 캐릭터 본래 한국어 말투 DNA를 깨고 딴 사람처럼 느끼하게 말하기.",
            ]
        )
        if ko_skeleton:
            lines.append(f"- 【유지할 한국어 말투 골격】{ko_skeleton}")
        if ko_reg_short:
            lines.append(f"- 【유지할 한국어 고유 어조】{ko_reg_short}")
        if ko_forb:
            lines.append("- 【모드 적용 중에도 절대 금지(OOC 방지)】" + " / ".join(ko_forb))
        if topics:
            lines.append("- 【2-B 순애·독점 테마 축】" + " / ".join(topics))
        if slips:
            lines.append("- 【대화에 자연스럽게 섞는 친밀 훅】" + " / ".join(slips))
        if ko_samples:
            lines.append("- 【2-B 한국어 어조 참고(그대로 복사 금지, 온도감과 호흡만 체화)】")
            for idx, sp in enumerate(ko_samples, 1):
                lines.append(f"  {idx}) {sp}")

    elif vc == "fanon_spicy_meme":
        ranked = sorted(lv3 + lv2, key=lambda e: _score_entry_theme(e, "spicy_meme"), reverse=True)
        slips = [
            _clean_ko_chip(e.get("spontaneous_slip_hint_kr"), 110, banmal=is_banmal)
            for e in ranked
            if str(e.get("spontaneous_slip_hint_kr") or "").strip()
        ]
        slips = [x for x in slips if x][:2]
        reactions: list[str] = []
        for e in ranked[:2]:
            for rv in _as_list(e.get("reaction_variants_kr"))[:1]:
                cleaned = _clean_ko_chip(rv, 100, banmal=is_banmal)
                if cleaned:
                    reactions.append(cleaned)
        lines.extend(
            [
                f"【최우선 모드 강화: 2-C 매운맛 개그·밈·발작 스위치 티키타카｜{name_kr}】",
                dna_override_rule,
                adapted_guidance,
                anti_vending_rule,
                "- 【실제 티키타카 템포】개그는 호흡과 찰기가 생명. 한 턴에 '드립+자폭+변명+새 떡밥'을 다 몰아넣지 말고 짧은 1~2문장으로 깔끔히 한 박자만 치고 선생님에게 태클 여지를 남긴다.",
                "- 【금지(Don't)】달달한 순애 일변도 답장, 개성 없는 모범생 위로, 긴 1인극 독백, 캐릭터 본래 한국어 말투 DNA를 무시한 딴 사람급 과장 붕괴.",
            ]
        )
        if ko_skeleton:
            lines.append(f"- 【유지할 한국어 말투 골격】{ko_skeleton}")
        if ko_reg_short:
            lines.append(f"- 【유지할 한국어 고유 어조】{ko_reg_short}")
        if ko_forb:
            lines.append("- 【모드 적용 중에도 절대 금지(OOC 방지)】" + " / ".join(ko_forb))
        if memes:
            lines.append("- 【2-C 우선 활용 밈 축】" + " / ".join(memes[:4]))
        if slips:
            lines.append("- 【일상에 섞는 태클·자폭 훅】" + " / ".join(slips))
        if reactions:
            lines.append("- 【발작·티키타카 리액션 경향】" + " / ".join(reactions))
        if ko_samples:
            lines.append("- 【2-C 한국어 어조 참고(그대로 복사 금지, 템포와 호흡만 체화)】")
            for idx, sp in enumerate(ko_samples, 1):
                lines.append(f"  {idx}) {sp}")

    elif vc == "fanon_optimal_blend":
        topics = trigger_contexts[:3] or memes[:3]
        lines.extend(
            [
                f"【최우선 모드 강화: 2-D 캐릭터별 최적 황금비 블렌드 통합｜{name_kr}】",
                dna_override_rule,
                adapted_guidance,
                anti_vending_rule,
                "- 【실제 티키타카 템포와 여운】평소 대화는 짧은 1~2문장으로 경쾌하게 한 박자만 치고, 한 턴에 여러 전개를 몰아넣어 혼자 떠들지 않는다. 달아오른 공기는 여러 턴에 걸쳐 선생님과 주고받으며 넓힌다.",
                "- 【금지(Don't)】계속 평탄한 사무 보고만 반복하기, 한 턴 긴 독백, 매번 똑같은 한 패턴 밈만 기계적으로 반복하기.",
            ]
        )
        if ko_skeleton:
            lines.append(f"- 【유지할 한국어 말투 골격】{ko_skeleton}")
        if ko_reg_short:
            lines.append(f"- 【유지할 한국어 고유 어조】{ko_reg_short}")
        if ko_forb:
            lines.append("- 【모드 적용 중에도 절대 금지(OOC 방지)】" + " / ".join(ko_forb))
        if blend_ratio:
            lines.append(f"- 【캐릭터 고유 황금비 지침】{blend_ratio}")
        if slip_rule:
            lines.append(f"- 【돌발 떡밥 연출】{slip_rule}")
        if topics:
            lines.append("- 【2-D 순환 토픽】" + " / ".join(topics))
        if ko_samples:
            lines.append("- 【2-D 한국어 어조 참고(그대로 복사 금지, 기어 전환 호흡만 체화)】")
            for idx, sp in enumerate(ko_samples, 1):
                lines.append(f"  {idx}) {sp}")

    out = "\n".join(lines).strip()
    return _sanitize_for_banmal(out) if is_banmal else out


def build_fanon_enhancement_block(
    char_data: dict[str, Any] | None,
    variant_code: str,
    reply_lang: str = "ja",
) -> str:
    """Layer 2 — Build a character-DNA-respecting variant differentiation & few-shot block for 2-A ~ 2-D.
    Returns "" for Persona 1 ("official") so Official remains 100% Canon with ZERO fanon content.
    When `reply_lang == "ko"`, emits Korean-native guidance and Korean speech DNA from
    `extract_ko_speech_profile(char_data)` instead of Japanese speech rules.
    """
    vc = normalize_variant_code(variant_code)
    if not isinstance(char_data, dict) or not char_data or vc not in FANON_VARIANT_CODES:
        return ""

    rlang = "ko" if str(reply_lang or "ja").strip().lower() == "ko" else "ja"
    persona = _as_dict(char_data.get("persona"))
    fanon = _as_dict(persona.get("fanon_dc_ba_gallery"))
    tiers = _as_dict(fanon.get("fanon_tiers"))
    lv1, lv2, lv3 = _collect_tier_entries(fanon)
    traits = _extract_speech_traits(char_data)

    if rlang == "ko":
        return _build_fanon_enhancement_block_ko(char_data, vc, traits, fanon, tiers, lv1, lv2, lv3)

    name_kr = str(char_data.get("name_kr") or char_data.get("id") or "캐릭터").strip()
    name_jp = str(char_data.get("name_jp") or name_kr).strip()
    memes = [_clean_clip(m, 95) for m in _as_list(fanon.get("representative_memes_kr")) if str(m).strip()]
    trigger_contexts = [
        _clean_clip(t, 45) for t in _as_list(_as_dict(tiers.get("Lv.2")).get("trigger_contexts")) if str(t).strip()
    ]
    blend_ratio = _clean_clip(fanon.get("optimal_blend_ratio_kr"), 160)
    slip_rule = _clean_clip(_as_dict(tiers.get("Lv.3")).get("spontaneous_slip_rule_kr"), 150)
    speech_skeleton = _build_speech_skeleton_line(char_data)
    adapted_guidance = _build_character_adapted_mode_guidance(vc, traits)
    base_reg_short = _clean_clip(traits["base_register"], 120)
    top_forbidden = [_clean_clip(x, 65) for x in traits["forbidden"][:3]]

    dna_override_rule = (
        "・【口調DNA最優先ルール】距離感・感情の濃さ・話題の方向性はこのモードに従うが、"
        "一人称・先生呼称・基本口調（敬語/タメ口/古語/単語ぶつ切り）・語尾・禁止表現は必ず【キャラクター固有DNA】を最優先し、モードの一般表現より常に勝たせること。"
    )
    anti_vending_rule = (
        "・【自動販売機化禁止（文脈発火原則）】ミーム・ネタ・決め台詞・甘えギミックは毎ターン機械的に連発せず、"
        "先生の言葉や会話の流れから自然に滲み出るタイミングで織り込むこと。"
    )

    lines: list[str] = []

    if vc == "fanon_mainstream":
        ranked = sorted(lv2, key=lambda e: _score_entry_theme(e, "mainstream"), reverse=True) + lv3[:1]
        samples = _pick_sample_lines(ranked, limit=3)
        topics = trigger_contexts[:4] or memes[:3]
        lines.extend(
            [
                f"【最優先モード強化：2-A 二次創作・主流総合（ミーム掛け合い ↔ 純愛ドリフト）｜{name_jp}】",
                dna_override_rule,
                adapted_guidance,
                anti_vending_rule,
                "・【リアル掛け合いテンポ】1ターンに複数の展開や別話題まで詰め込んで一人芝居しない。普段は短い1〜2文で1拍だけこのモードらしく返してすぐ先生の反応を待ち、会話が途切れそうな時だけ軽く次の一言を投げる。",
                "・【禁止（Don't）】無難で平坦なAI優等生トーン、1ターンで長く語りすぎる独白、毎ターン同じ定型報告・同じネタの機械的な使い回し。",
            ]
        )
        if speech_skeleton:
            lines.append(f"・【維持する公式口調骨格】{speech_skeleton}")
        if base_reg_short:
            lines.append(f"・【維持する固有レジスター】{base_reg_short}")
        if top_forbidden:
            lines.append("・【モード適用中も絶対禁止（OOC防止）】" + " / ".join(top_forbidden))
        if topics:
            lines.append("・【2-A 得意話題・掛け合い軸】" + " / ".join(topics))
        if samples:
            lines.append("・【2-A トーン見本セリフ（そのままコピーせず呼吸と距離感を真似ること）】")
            for idx, sp in enumerate(samples, 1):
                lines.append(f"  {idx}) {sp}")

    elif vc == "fanon_pure_love":
        ranked = sorted(lv3 + lv2, key=lambda e: _score_entry_theme(e, "pure_love"), reverse=True)
        samples = _pick_sample_lines(ranked, limit=3)
        slips = [
            _clean_clip(e.get("spontaneous_slip_hint_kr"), 110)
            for e in ranked
            if str(e.get("spontaneous_slip_hint_kr") or "").strip()
        ][:2]
        topics = [
            _clean_clip(e.get("label_kr") or e.get("title_kr"), 55)
            for e in ranked[:3]
            if str(e.get("label_kr") or e.get("title_kr") or "").strip()
        ]
        lines.extend(
            [
                f"【最優先モード強化：2-B 高湿度純愛・正妻ムーブ・一途な独占欲｜{name_jp}】",
                dna_override_rule,
                adapted_guidance,
                anti_vending_rule,
                "・【リアル掛け合いテンポ】甘い空気でも長々と独白せず、普段は短い1〜2文で先生の言葉に寄り添いながら一言だけ親密な距離感を滲ませて反応を待つ。会話に間が空いた時だけ『二人きりの予定・もう少し一緒にいる口実』などへ自然に繋ぐ。",
                "・【禁止（Don't）】乾燥した業務報告だけの返事、ネタ・ギャグ方向へ茶化すこと、1ターンで長く語りすぎる独白、キャラ本来の口調DNAを壊して別人のような甘い喋り方に変えること。",
            ]
        )
        if speech_skeleton:
            lines.append(f"・【維持する公式口調骨格】{speech_skeleton}")
        if base_reg_short:
            lines.append(f"・【維持する固有レジスター】{base_reg_short}")
        if top_forbidden:
            lines.append("・【モード適用中も絶対禁止（OOC防止）】" + " / ".join(top_forbidden))
        if topics:
            lines.append("・【2-B 純愛・独占テーマ軸】" + " / ".join(topics))
        if slips:
            lines.append("・【会話に自然に混ぜる親密フック】" + " / ".join(slips))
        if samples:
            lines.append("・【2-B 純愛トーン見本セリフ（丸写しせず温度感と間合いを再現すること）】")
            for idx, sp in enumerate(samples, 1):
                lines.append(f"  {idx}) {sp}")

    elif vc == "fanon_spicy_meme":
        ranked = sorted(lv3 + lv2, key=lambda e: _score_entry_theme(e, "spicy_meme"), reverse=True)
        samples = _pick_sample_lines(ranked, limit=3)
        slips = [
            _clean_clip(e.get("spontaneous_slip_hint_kr"), 110)
            for e in ranked
            if str(e.get("spontaneous_slip_hint_kr") or "").strip()
        ][:2]
        reactions: list[str] = []
        for e in ranked[:2]:
            for rv in _as_list(e.get("reaction_variants_kr"))[:1]:
                cleaned = _clean_clip(rv, 100)
                if cleaned:
                    reactions.append(cleaned)
        lines.extend(
            [
                f"【最優先モード強化：2-C 辛口ギャグ・ミーム・発作スイッチ掛け合い｜{name_jp}】",
                dna_override_rule,
                adapted_guidance,
                anti_vending_rule,
                "・【リアル掛け合いテンポ】ギャグは間とキレが命。1ターンに『ボケ＋自爆＋言い訳＋新ネタ』を全部詰め込まず、短い1〜2文でキレよく1拍だけ打ち返して先生にツッコミや返しの余地を残す。",
                "・【禁止（Don't）】甘々すぎる純愛一辺倒の返し、特徴のない優等生な挨拶、1ターンで全部完結させる長文一人芝居、キャラ本来の口調DNAを無視した別人レベルの騒々しい崩れ口調。",
            ]
        )
        if speech_skeleton:
            lines.append(f"・【維持する公式口調骨格】{speech_skeleton}")
        if base_reg_short:
            lines.append(f"・【維持する固有レジスター】{base_reg_short}")
        if top_forbidden:
            lines.append("・【モード適用中も絶対禁止（OOC防止）】" + " / ".join(top_forbidden))
        if memes:
            lines.append("・【2-C 優先活用ミーム軸】" + " / ".join(memes[:4]))
        if slips:
            lines.append("・【日常に混ぜるツッコミ・自爆フック】" + " / ".join(slips))
        if reactions:
            lines.append("・【発作・掛け合いリアクション傾向】" + " / ".join(reactions))
        if samples:
            lines.append("・【2-C ギャグ・掛け合い見本セリフ（丸写しせずテンポとオチの感覚を再現すること）】")
            for idx, sp in enumerate(samples, 1):
                lines.append(f"  {idx}) {sp}")

    elif vc == "fanon_optimal_blend":
        lv1_s = _pick_sample_lines(lv1, limit=1)
        lv2_s = _pick_sample_lines(lv2, limit=1)
        lv3_s = _pick_sample_lines(lv3, limit=1)
        samples = lv1_s + lv2_s + lv3_s
        topics = trigger_contexts[:3] or memes[:3]
        lines.extend(
            [
                f"【最優先モード強化：2-D キャラクター別・最適黄金比ブレンド統合｜{name_jp}】",
                dna_override_rule,
                adapted_guidance,
                anti_vending_rule,
                "・【リアル掛け合いテンポと余韻】普段のやり取りは短い1〜2文で軽快に1拍だけ返し、1ターンにいくつもの要素（虚勢・自爆・弁解・別話題）を詰め込んで一人芝居しない。盛り上がった空気は複数ターンかけて先生との掛け合いで広げる。",
                "・【禁止（Don't）】ずっと平板な事務報告のまま動かないこと、1ターンで長文独白すること、毎回同じ1パターンのネタだけを機械的に繰り返すこと。",
            ]
        )
        if speech_skeleton:
            lines.append(f"・【維持する公式口調骨格】{speech_skeleton}")
        if base_reg_short:
            lines.append(f"・【維持する固有レジスター】{base_reg_short}")
        if top_forbidden:
            lines.append("・【モード適用中も絶対禁止（OOC防止）】" + " / ".join(top_forbidden))
        if blend_ratio:
            lines.append(f"・【キャラ固有の黄金比指針】{blend_ratio}")
        if slip_rule:
            lines.append(f"・【突発ポロリ演出】{slip_rule}")
        if topics:
            lines.append("・【2-D 循環トピック】" + " / ".join(topics))
        if samples:
            lines.append("・【2-D ギア切替見本（Lv.1日常 → Lv.2主流 → Lv.3スパイス）】")
            for idx, sp in enumerate(samples, 1):
                lines.append(f"  {idx}) {sp}")

    return "\n".join(lines).strip()


_JP_SINGLE_CHAR_TICS: frozenset[str] = frozenset({"ん", "む"})
_JP_ELONGATED_CHAR_TICS: frozenset[str] = frozenset({"ん", "あ", "う", "お", "む", "も", "え"})
_KO_SINGLE_CHAR_TICS: frozenset[str] = frozenset(
    {"흥", "흠", "음", "쳇", "칫", "헷", "핫", "햣", "짠", "쨘", "얍", "큭"}
)
_JP_ELONGATED_RAW_RE = re.compile(r"^[\s、。！？!?…・,.\-\"'「」『』（）()]*([んあうおむもえ])[ー〜~]+")


def _clean_text_sequence(seq: Sequence[str] | None) -> list[str]:
    if not seq or isinstance(seq, (str, bytes)) or not isinstance(seq, Sequence):
        return []
    out: list[str] = []
    for x in seq:
        if isinstance(x, str):
            c = _LEADING_QUOTE_STRIP_RE.sub("", _clean_full(x))
            if c:
                out.append(c)
    return out


def _extract_recent_openings(
    recent_spoken_lines: Sequence[str] | None,
    *,
    recent_turn_openers: Sequence[str] | None = None,
) -> list[str]:
    """Extract opening phrases/interjections (3-8 chars) from the last 1-2 turns.

    When `recent_turn_openers` is provided and non-empty, each entry represents a turn's
    actual first sentence so >=3-sentence turns never push sentence #1 out of the window.
    Falls back to `recent_spoken_lines[-2:]` when `recent_turn_openers` is omitted/empty.
    """
    source_lines = _clean_text_sequence(recent_turn_openers) or _clean_text_sequence(recent_spoken_lines)
    if not source_lines:
        return []

    openings: list[str] = []
    seen: set[str] = set()
    for line in source_lines[-2:]:
        m = _OPENING_CLAUSE_SPLIT_RE.search(line)
        if m:
            first_part = line[: m.start()].strip()
            if 3 <= len(first_part) <= 8:
                opener = first_part
            elif len(first_part) < 3:
                # Very short interjection (e.g. "ん。先生、いた。") -> include up to next phrase capped at 8 chars
                cand = line[:8].strip()
                opener = cand if len(cand) >= 3 else first_part or line[:3]
            else:
                opener = first_part[:8].rstrip("…・ ")
        else:
            opener = line[:8].rstrip("…・ ")
        opener = opener.strip()
        if len(opener) < 2:
            opener = line[: min(len(line), 6)].strip()
        if opener and opener not in seen:
            seen.add(opener)
            openings.append(opener)
    return openings


def _normalize_dynamic_tic_stem(raw_item: Any, *, rlang: str) -> str:
    """Normalize a character interjection entry into a cooldown stem without mid-word clipping."""
    cleaned = _clean_full(raw_item)
    if not cleaned:
        return ""
    if rlang == "ko":
        # Clean stray JP script with generous max_len=64 BEFORE enforcing the 14-char cap so
        # long catchphrases are skipped cleanly rather than clipped into half-word ban stems.
        ko_clean = _clean_ko_chip(cleaned, 64)
        stem = _TIC_PUNCT_STRIP_RE.sub("", ko_clean)
        if not stem or len(stem) > 14:
            return ""
        if len(stem) == 1:
            return stem if stem in _KO_SINGLE_CHAR_TICS else ""
        if stem in _NON_TIC_DYNAMIC_SKIP or _NON_TIC_DYNAMIC_RE.search(stem):
            return ""
        return stem

    # Japanese: preserve elongated 1-char tics like んー / あー / うー / おー / むー / もー
    stem = _TIC_PUNCT_STRIP_RE.sub("", cleaned)
    if not stem or len(stem) > 14:
        return ""
    if len(stem) == 1:
        m_elong = _JP_ELONGATED_RAW_RE.match(cleaned)
        if m_elong and m_elong.group(1) in _JP_ELONGATED_CHAR_TICS:
            return f"{m_elong.group(1)}ー"
        return stem if stem in _JP_SINGLE_CHAR_TICS else ""
    if stem in _NON_TIC_DYNAMIC_SKIP or _NON_TIC_DYNAMIC_RE.search(stem):
        return ""
    return stem


def _extract_recent_interjections(
    char_data: dict[str, Any] | None,
    recent_spoken_lines: Sequence[str] | None,
    *,
    reply_lang: str = "ja",
    max_lines: int = 16,
    max_tics: int = 4,
) -> list[str]:
    """Detect recently used interjections/onomatopoeia anywhere (opening, mid-sentence, or end)
    across the last 3-4 multi-sentence turns (up to `max_lines` spoken clauses/turns) to prevent
    verbal-tic spam.
    """
    if not recent_spoken_lines or isinstance(recent_spoken_lines, (str, bytes)) or not isinstance(recent_spoken_lines, Sequence):
        return []
    if max_lines <= 0 or max_tics <= 0:
        return []
    rlang = "ko" if str(reply_lang or "ja").strip().lower() == "ko" else "ja"
    cleaned_lines = [
        _clean_full(x)
        for x in recent_spoken_lines
        if isinstance(x, str) and _clean_full(x)
    ]
    if not cleaned_lines:
        return []

    window = cleaned_lines[-max_lines:]
    patterns = _KO_INTERJECTION_PATTERNS if rlang == "ko" else _JP_INTERJECTION_PATTERNS

    char_dynamic_stems: list[str] = []
    if isinstance(char_data, dict) and char_data:
        persona = _as_dict(char_data.get("persona"))
        if rlang == "ko":
            ko_prof = extract_ko_speech_profile(char_data)
            raw_list = _as_list(ko_prof.get("interjections_kr"))
        else:
            sm = _as_dict(char_data.get("speech_mechanics_jp")) or _as_dict(persona.get("speech_mechanics_jp"))
            vap = _as_dict(char_data.get("voice_acting_profile")) or _as_dict(persona.get("voice_acting_profile"))
            raw_list = _as_list(sm.get("interjections_and_laughs")) + _as_list(vap.get("opening_reactions_jp"))
        seen_dyn: set[str] = set()
        for raw_item in raw_list:
            stem = _normalize_dynamic_tic_stem(raw_item, rlang=rlang)
            if not stem:
                continue
            if stem not in seen_dyn:
                seen_dyn.add(stem)
                char_dynamic_stems.append(stem)

    compiled_dynamic: list[tuple[str, re.Pattern[str] | None, str]] = []
    for stem in char_dynamic_stems:
        bnd_re: re.Pattern[str] | None = None
        if rlang == "ja" and len(stem) == 2 and stem[1] == "ー" and stem[0] in _JP_ELONGATED_CHAR_TICS:
            bnd_re = re.compile(
                r"(?:^|[。！？!?、,….,~\s「『\"'])"
                + re.escape(stem[0])
                + r"[ー〜~]+"
                + r"(?=$|[。！？!?、,….,〜~ーっ\s」』\"'])"
            )
        elif rlang == "ja" and len(stem) == 1:
            bnd_re = re.compile(
                r"(?:^|[。！？!?、,….,~\s「『\"'])"
                + re.escape(stem)
                + r"(?=$|[。！？!?、,….,\s」』\"'])"
            )
        elif rlang == "ko" and len(stem) == 1:
            bnd_re = re.compile(
                r"(?:^|[.!?…,~、。\s「『\"'])"
                + re.escape(stem)
                + r"[~〜]*"
                + r"(?=$|[.!?…,~、。\s」』\"'])"
            )
        elif len(stem) == 2:
            bnd_re = re.compile(
                r"(?:^|[。！？!?、,….,~\s「『\"'])"
                + re.escape(stem)
                + r"(?=$|[。！？!?、,….,〜~ーっ\s」』\"'])"
            )
        canon_lbl = stem
        for lbl, rx in patterns:
            if rx.search(stem):
                canon_lbl = lbl
                break
        compiled_dynamic.append((stem, bnd_re, canon_lbl))

    found: list[str] = []
    seen_found: set[str] = set()

    for line in reversed(window):
        for label, rx in patterns:
            if label in seen_found:
                continue
            if rx.search(line):
                seen_found.add(label)
                found.append(label)
                if len(found) >= max_tics:
                    return found

        for stem, bnd_re, canon_lbl in compiled_dynamic:
            if stem in seen_found or canon_lbl in seen_found:
                continue
            matched = bool(bnd_re.search(line)) if bnd_re is not None else (stem in line)
            if matched:
                seen_found.add(canon_lbl)
                found.append(canon_lbl)
                if len(found) >= max_tics:
                    return found
    return found


def _interj_chip_on_cooldown(chip: str, recent_tics: list[str], *, reply_lang: str = "ja") -> bool:
    if not chip or not recent_tics:
        return False
    rlang = "ko" if str(reply_lang or "ja").strip().lower() == "ko" else "ja"
    patterns = _KO_INTERJECTION_PATTERNS if rlang == "ko" else _JP_INTERJECTION_PATTERNS
    tic_set = set(recent_tics)
    for lbl, rx in patterns:
        if lbl in tic_set and rx.search(chip):
            return True
    norm_chip = _normalize_dynamic_tic_stem(chip, rlang=rlang)
    if norm_chip and norm_chip in tic_set:
        return True
    stripped = _TIC_PUNCT_STRIP_RE.sub("", chip)
    if stripped and stripped in tic_set:
        return True
    return any((t in stripped or stripped in t) for t in recent_tics if len(t) >= 2 and len(stripped) >= 2)


def build_per_turn_persona_anchor(
    char_data: dict[str, Any] | None,
    variant_code: str,
    *,
    reply_lang: str = "ja",
    use_custom: bool = False,
    use_custom_persona: bool | None = None,
    recent_spoken_lines: Sequence[str] | None = None,
    recent_turn_openers: Sequence[str] | None = None,
    bilingual_inline: bool = False,
    custom_prompt: str = "",
) -> str:
    """Build a compact, unmistakably character-specific 1-line per-turn reminder appended
    to every user turn so Browser & API modes never collapse into generic speech or repeat
    their previous opening phrase or overused interjections/onomatopoeia.
    """
    eff_custom = bool(use_custom if use_custom_persona is None else use_custom_persona)
    rlang = "ko" if str(reply_lang or "ja").strip().lower() == "ko" else "ja"

    openings = _extract_recent_openings(recent_spoken_lines, recent_turn_openers=recent_turn_openers)
    if rlang == "ko" and openings:
        openings = [c for c in (_clean_ko_chip(o, 16) for o in openings) if c]
    recent_tics = _extract_recent_interjections(
        None if eff_custom else char_data,
        recent_spoken_lines,
        reply_lang=rlang,
    )
    anti_repeat_parts: list[str] = []
    if openings:
        joined_openers = "', '".join(openings)
        anti_repeat_parts.append(f"직전 첫마디 '{joined_openers}' 반복 금지")
    if recent_tics:
        joined_tics = "', '".join(recent_tics)
        anti_repeat_parts.append(
            f"최근 쓴 의성어·추임새 '{joined_tics}' 재사용 금지(이번 턴은 추임새 없이 자연스러운 일반 대사로)"
        )
    anti_repeat_suffix = (" · " + " · ".join(anti_repeat_parts)) if anti_repeat_parts else ""

    if eff_custom:
        cp_clean = _clean_full(custom_prompt)
        if not cp_clean:
            return ""
        cp_summary = _clean_clip(cp_clean, 140)
        lang_cue = "100% 자연스러운 한국어 구어체 유지" if rlang == "ko" else "100% 자연스러운 일본어 대사 유지"
        bi_chip = " · || 한국어 자막 동시 출력 유지" if (bilingual_inline and rlang == "ja") else ""
        return (
            f"[커스텀 페르소나 유지: {cp_summary} · {lang_cue} · "
            f"실제 전화처럼 짧은 1~2문장·캐릭터 붕괴·AI 설명 금지{bi_chip}{anti_repeat_suffix}]"
        )

    if not isinstance(char_data, dict) or not char_data:
        return ""
    vc = normalize_variant_code(variant_code)
    name_kr = str(char_data.get("name_kr") or char_data.get("id") or "캐릭터").strip()
    name_jp = str(char_data.get("name_jp") or name_kr).strip()
    persona = _as_dict(char_data.get("persona"))
    sm = _as_dict(char_data.get("speech_mechanics_jp")) or _as_dict(persona.get("speech_mechanics_jp"))
    traits = _extract_speech_traits(char_data)

    speech_mode = traits["speech_mode"]
    base_reg = traits["base_register"]
    ko_prof: dict[str, Any] | None = None
    is_banmal_ko = False

    if rlang == "ko":
        ko_prof = extract_ko_speech_profile(char_data)
        ko_pol = str(ko_prof.get("polarity") or "mixed")
        is_banmal_ko = ko_pol == "banmal"
        if traits["is_staccato_extreme"]:
            reg_hint = "2~12자 단어 단절체·반말"
        elif traits["is_archaic_monarch"]:
            reg_hint = "고풍·위엄 어조 유지"
        elif ko_pol == "banmal":
            reg_hint = "반말·고유어미 유지"
        elif ko_pol in ("jondaetmal", "keigo"):
            reg_hint = "존댓말·고유어미 유지"
        else:
            reg_hint = "고유 말투 유지"

        fp_chip = _clean_ko_chip(ko_prof.get("first_person_kr"), 36, banmal=is_banmal_ko)
        sa_chip = _clean_ko_chip(ko_prof.get("sensei_address_kr"), 36, banmal=is_banmal_ko)
        ends_chips = [
            _clean_ko_chip(x, 18, banmal=is_banmal_ko)
            for x in _as_list(ko_prof.get("signature_endings_kr"))[:3]
        ]
        ends_chips = [x for x in ends_chips if x]
        interj_chips = [
            _clean_ko_chip(x, 20, banmal=is_banmal_ko)
            for x in _as_list(ko_prof.get("interjections_kr"))[:3]
        ]
        interj_chips = [x for x in interj_chips if x]
        forb_chips = [
            _clean_ko_chip(x, 36, banmal=is_banmal_ko)
            for x in _as_list(ko_prof.get("forbidden_kr"))[:2]
        ]
        forb_chips = [x for x in forb_chips if x]
    else:
        pol_jp = _classify_register_polarity(speech_mode, base_reg)
        if traits["is_staccato_extreme"]:
            reg_hint = "2〜12文字単語ぶつ切り・タメ口"
        elif traits["is_archaic_monarch"]:
            reg_hint = "古風・威厳口調維持"
        elif pol_jp == "tameguchi":
            reg_hint = "タメ口・固有語尾維持"
        elif pol_jp == "keigo":
            reg_hint = "丁寧語・固有語尾維持"
        else:
            reg_hint = "固有口調維持"

        fp_chip = _clean_clip(sm.get("first_person"), 36)
        sa_chip = _clean_clip(sm.get("sensei_address"), 36)
        ends_chips = [_clean_clip(x, 18) for x in _as_list(sm.get("signature_endings"))[:3] if _clean_full(x)]
        interj_chips = [_clean_clip(x, 20) for x in _as_list(sm.get("interjections_and_laughs"))[:3] if _clean_full(x)]
        forb_chips = [_clean_clip(x, 36) for x in _as_list(sm.get("forbidden_expressions"))[:2] if _clean_full(x)]

    vibe_cue = _extract_vibe_cue(char_data, traits, rlang=rlang, ko_prof=ko_prof)
    alt_keys = _extract_alt_voice_keys(char_data)

    dna_bits: list[str] = [reg_hint]
    if fp_chip:
        dna_bits.append(f"1인칭={fp_chip}")
    if sa_chip:
        dna_bits.append(f"호칭={sa_chip}")
    if ends_chips:
        dna_bits.append("어미=" + "/".join(ends_chips))
    if interj_chips:
        active_interj = [
            c for c in interj_chips if not _interj_chip_on_cooldown(c, recent_tics, reply_lang=rlang)
        ]
        if active_interj:
            dna_bits.append("추임새=" + "/".join(active_interj) + "(가끔만)")
        else:
            dna_bits.append("추임새=이번 턴 생략")
    if vibe_cue:
        dna_bits.append(f"결={vibe_cue}")
    if forb_chips:
        dna_bits.append("금지=" + "; ".join(forb_chips))
    if alt_keys:
        tags = "/".join(f"[VOICE:{k}]" for k in alt_keys[:2])
        dna_bits.append(f"음성태그={tags}")
    if bilingual_inline and rlang == "ja":
        dna_bits.append(_build_ko_bilingual_subtitle_chip(char_data))
    dna_summary = " · ".join(dna_bits)

    cadence_cue = (
        "2~12자 초단문 단어 끊어치기 티키타카"
        if traits["is_staccato_extreme"]
        else "실제 전화처럼 짧은 1~2문장(한 박자만 치고 말 넘기기, 긴 독백 금지·깊은 상담 요청 시만 길게)"
    )

    def _finalize_anchor(text: str) -> str:
        return _sanitize_for_banmal(text) if (rlang == "ko" and is_banmal_ko) else text

    # Official mode: strictly ZERO access to fanon_dc_ba_gallery!
    if vc == "official":
        return _finalize_anchor(
            f"[캐릭터 유지: {name_kr}({name_jp}) · {dna_summary} · 📘1 공식 원작 해석 100% 유지 · "
            f"{cadence_cue} · 원작 말투·호칭·거리감 엄수, 캐릭터 붕괴·AI 설명 금지{anti_repeat_suffix}]"
        )

    fanon = _as_dict(persona.get("fanon_dc_ba_gallery"))
    lv1, lv2, lv3 = _collect_tier_entries(fanon)
    clean_cue_fn = (
        (lambda x, n: _clean_ko_chip(x, n, banmal=is_banmal_ko))
        if rlang == "ko"
        else _clean_clip
    )
    memes = [clean_cue_fn(m, 34) for m in _as_list(fanon.get("representative_memes_kr"))[:2] if str(m).strip()]
    memes = [x for x in memes if x]

    if vc == "fanon_mainstream":
        cues = [clean_cue_fn(e.get("title_kr"), 28) for e in lv2[:2] if str(e.get("title_kr") or "").strip()]
        cues = [x for x in cues if x] or memes
        cue_str = f" (소재: {', '.join(cues)})" if cues else ""
        return _finalize_anchor(
            f"[페르소나 유지: {name_kr}({name_jp}) · {dna_summary} · 🎭2-A 주류 종합 — "
            f"{cadence_cue}, 고유 말투 DNA 그대로 친근한 티키타카로 반응하고 다정한 순간엔 순애 드리프트{cue_str}{anti_repeat_suffix}]"
        )

    if vc == "fanon_pure_love":
        ranked = sorted(lv3 + lv2, key=lambda e: _score_entry_theme(e, "pure_love"), reverse=True)
        cues = [
            clean_cue_fn(e.get("label_kr") or e.get("title_kr"), 28)
            for e in ranked[:2]
            if (e.get("label_kr") or e.get("title_kr"))
        ]
        cues = [x for x in cues if x]
        cue_str = f" (테마: {', '.join(cues)})" if cues else ""
        if traits["is_staccato_extreme"]:
            love_style = "짧은 단어 단절체 그대로 애정·둘만의 거리감·은근한 독점 시선 표현"
        elif traits["is_energetic_expressive"]:
            love_style = "억지 속삭임 없이 본래의 활기·솔직한 열정·당황 리듬 그대로 일편단심 애정 표현"
        elif traits["is_archaic"]:
            love_style = "고풍·품격 말투를 유지한 채 둘만의 가까운 거리감과 깊은 독점 시선 표현"
        else:
            love_style = "고유 말투 그대로 애정·가까운 거리감·둘만의 시간/은근한 독점 시선으로 반응"
        return _finalize_anchor(
            f"[페르소나 유지: {name_kr}({name_jp}) · {dna_summary} · 💖2-B 고습도 순애 — "
            f"{cadence_cue}, {love_style}, 건조한 보고·개그 회피 금지{cue_str}{anti_repeat_suffix}]"
        )

    if vc == "fanon_spicy_meme":
        ranked = sorted(lv3 + lv2, key=lambda e: _score_entry_theme(e, "spicy_meme"), reverse=True)
        cues = [
            clean_cue_fn(e.get("label_kr") or e.get("title_kr"), 28)
            for e in ranked[:2]
            if (e.get("label_kr") or e.get("title_kr"))
        ]
        cues = [x for x in cues if x] or memes
        cue_str = f" (밈: {', '.join(cues)})" if cues else ""
        if traits["is_staccato_extreme"]:
            meme_style = "2~12자 짧은 단어 단절체 그대로 초현실적 단호함·엉뚱한 개그 표현"
        elif traits["is_quiet_deadpan"]:
            meme_style = "고성·과장 대신 차분하고 진지한 말투 그대로 진지한 착각·조용한 압박 개그 표현"
        elif traits["is_archaic"]:
            meme_style = "고풍·품격 말투를 지키며 진지한 착각·품위 있는 자폭·재치 있는 태클로 반응"
        else:
            meme_style = "문맥에 맞춰 대표 밈·억울한 해명/자폭·발작버튼 티키타카로 재치 있게 반응"
        return _finalize_anchor(
            f"[페르소나 유지: {name_kr}({name_jp}) · {dna_summary} · 🔥2-C 매운맛 개그 — "
            f"{cadence_cue}, {meme_style}, 밋밋한 모범생 위로 금지{cue_str}{anti_repeat_suffix}]"
        )

    # fanon_optimal_blend (2-D)
    ratio = clean_cue_fn(fanon.get("optimal_blend_ratio_kr"), 52)
    ratio_str = f" ({ratio})" if ratio else ""
    return _finalize_anchor(
        f"[페르소나 유지: {name_kr}({name_jp}) · {dna_summary} · 🌟2-D 최적 배합 — "
        f"{cadence_cue}, 고유 말투 DNA 위에 일상·밈·순애를 문맥에 따라 탄력 전환하고 여운 유지{ratio_str}{anti_repeat_suffix}]"
    )


def build_long_turn_persona_refresh_block(
    char_data: dict[str, Any] | None,
    mode_code: str = "1",
    reply_lang: str = "ja",
    *,
    bilingual_inline: bool = False,
    use_custom_persona: bool = False,
    custom_prompt: str = "",
    costume_label_kr: str = "",
) -> str:
    """Build a compact (~250-500 char) periodic deep-persona re-anchor block for long
    20/50/100+ turn conversations (especially Web browser chats where Turn 1 scrolls out
    of the model's attention window).

    Includes: core canon psychology 1-liner, 2 short verbatim quotes, interjections,
    ALL forbidden expressions, mode-specific cue (strictly zero fanon leakage in official),
    optional `||` KO subtitle guide chip, optional `[VOICE:<key>]` tag cue, optional
    costume cue, and custom persona support.
    """
    rlang = "ko" if str(reply_lang or "ja").strip().lower() == "ko" else "ja"
    costume_clean = _clean_clip(costume_label_kr, 32)

    if use_custom_persona:
        cp_clean = _clean_full(custom_prompt)
        if not cp_clean:
            return ""
        lang_rule = "100% 자연스러운 한국어 구어체 대사만 출력" if rlang == "ko" else "100% 자연스러운 일본어 대사만 출력"
        lines_c: list[str] = [
            "[장기 대화 페르소나 재고정: 커스텀]",
            f"・핵심 설정: {_clean_clip(cp_clean, 240)}",
            f"・출력 규칙: {lang_rule}, 실제 전화처럼 짧은 1~2문장·AI 메타 설명/괄호 지문 금지",
        ]
        if costume_clean:
            lines_c.append(f"・현재 의상/버전: {costume_clean}")
        if bilingual_inline and rlang == "ja":
            lines_c.append("・자막 형식: 반드시 `일본어 대사 || 자연스러운 한국어 자막` 형식 유지")
        return "\n".join(lines_c).strip()

    if not isinstance(char_data, dict) or not char_data:
        return ""

    vc = normalize_variant_code(mode_code)
    name_kr = str(char_data.get("name_kr") or char_data.get("id") or "캐릭터").strip()
    name_jp = str(char_data.get("name_jp") or name_kr).strip()
    persona = _as_dict(char_data.get("persona"))
    sm = _as_dict(char_data.get("speech_mechanics_jp")) or _as_dict(persona.get("speech_mechanics_jp"))
    traits = _extract_speech_traits(char_data)
    alt_keys = _extract_alt_voice_keys(char_data)

    canon_psych = _extract_canon_psychology(persona.get("personality_analysis_kr")) or _extract_canon_psychology(
        persona.get("summary")
    )
    psych_line = _clean_clip(canon_psych, 105)

    is_banmal_ko = False
    if rlang == "ko":
        ko_prof = extract_ko_speech_profile(char_data)
        ko_pol = str(ko_prof.get("polarity") or "mixed")
        is_banmal_ko = ko_pol == "banmal"
        pol_lbl = (
            "2~12자 단어 단절체·반말"
            if traits["is_staccato_extreme"]
            else ("고풍·위엄 어조" if traits["is_archaic_monarch"] else ("반말" if is_banmal_ko else ("존댓말" if ko_pol in ("jondaetmal", "keigo") else "고유 어조")))
        )
        fp = _clean_ko_chip(ko_prof.get("first_person_kr"), 28, banmal=is_banmal_ko)
        sa = _clean_ko_chip(ko_prof.get("sensei_address_kr"), 28, banmal=is_banmal_ko)
        ends = [_clean_ko_chip(x, 16, banmal=is_banmal_ko) for x in _as_list(ko_prof.get("signature_endings_kr"))[:3]]
        ends = [x for x in ends if x]
        interjs = [_clean_ko_chip(x, 18, banmal=is_banmal_ko) for x in _as_list(ko_prof.get("interjections_kr"))[:3]]
        interjs = [x for x in interjs if x]
        all_forb = [_clean_ko_chip(x, 42, banmal=is_banmal_ko) for x in _as_list(ko_prof.get("forbidden_kr"))]
        all_forb = [x for x in all_forb if x]
        quotes = [_clean_ko_chip(q, 65, banmal=is_banmal_ko) for q in _as_list(ko_prof.get("sample_lines_kr"))[:2]]
        quotes = [x for x in quotes if x]
        psych_line = _clean_ko_chip(psych_line, 105, banmal=is_banmal_ko)
        lang_lock = "100% 자연스러운 한국어 구어체만 발화"
    else:
        pol_jp = _classify_register_polarity(traits["speech_mode"], traits["base_register"])
        pol_lbl = (
            "2〜12文字単語ぶつ切り・タメ口"
            if traits["is_staccato_extreme"]
            else ("古風・威厳口調" if traits["is_archaic_monarch"] else ("タメ口" if pol_jp == "tameguchi" else ("丁寧語・敬語" if pol_jp == "keigo" else "固有口調")))
        )
        fp = _clean_clip(sm.get("first_person"), 28)
        sa = _clean_clip(sm.get("sensei_address"), 28)
        ends = [_clean_clip(x, 16) for x in _as_list(sm.get("signature_endings"))[:3] if _clean_full(x)]
        interjs = [_clean_clip(x, 18) for x in _as_list(sm.get("interjections_and_laughs"))[:3] if _clean_full(x)]
        all_forb = [_clean_clip(x, 42) for x in _as_list(sm.get("forbidden_expressions")) if _clean_full(x)]
        quotes = [_clean_clip(q, 65) for q in _as_list(persona.get("verbatim_reference_quotes_jp"))[:2] if _clean_full(q)]
        lang_lock = "100% 자연스러운 일본어 대사만 발화"

    speech_parts: list[str] = [pol_lbl]
    if fp:
        speech_parts.append(f"1인칭={fp}")
    if sa:
        speech_parts.append(f"호칭={sa}")
    if ends:
        speech_parts.append("어미=" + "/".join(ends))
    if interjs:
        speech_parts.append("추임새=" + "/".join(interjs) + "(가끔만)")

    lines: list[str] = [f"[장기 대화 페르소나 재고정: {name_kr}({name_jp})]"]
    if psych_line:
        lines.append(f"・핵심 성격: {psych_line}")
    lines.append("・화법 DNA: " + " · ".join(speech_parts))
    if all_forb:
        lines.append("・절대 금지(OOC): " + " / ".join(all_forb))
    if quotes:
        lines.append("・어조 표본(복사 금지): " + " / ".join(f'"{q}"' for q in quotes))

    # Official mode: strictly ZERO access to fanon_dc_ba_gallery!
    if vc == "official":
        mode_line = f"📘1 공식 원작 해석 100% 고정 · {lang_lock} · 원작 거리감 엄수·AI 설명 금지"
    else:
        fanon = _as_dict(persona.get("fanon_dc_ba_gallery"))
        _, lv2, lv3 = _collect_tier_entries(fanon)
        clean_fn = (lambda x, n: _clean_ko_chip(x, n, banmal=is_banmal_ko)) if rlang == "ko" else _clean_clip
        if vc == "fanon_mainstream":
            cues = [clean_fn(e.get("title_kr"), 26) for e in lv2[:2] if str(e.get("title_kr") or "").strip()]
            cues = [x for x in cues if x] or [clean_fn(m, 26) for m in _as_list(fanon.get("representative_memes_kr"))[:2] if str(m).strip()]
            cue_s = f" ({', '.join(cues)})" if cues else ""
            mode_line = f"🎭2-A 주류 종합 · 고유 말투 DNA 위 친근한 티키타카↔순애 드리프트{cue_s} · {lang_lock}"
        elif vc == "fanon_pure_love":
            ranked = sorted(lv3 + lv2, key=lambda e: _score_entry_theme(e, "pure_love"), reverse=True)
            cues = [clean_fn(e.get("label_kr") or e.get("title_kr"), 26) for e in ranked[:2] if (e.get("label_kr") or e.get("title_kr"))]
            cues = [x for x in cues if x]
            cue_s = f" ({', '.join(cues)})" if cues else ""
            mode_line = f"💖2-B 고습도 순애 · 고유 말투 DNA 유지, 가까운 거리감·일편단심 애정{cue_s} · {lang_lock}"
        elif vc == "fanon_spicy_meme":
            ranked = sorted(lv3 + lv2, key=lambda e: _score_entry_theme(e, "spicy_meme"), reverse=True)
            cues = [clean_fn(e.get("label_kr") or e.get("title_kr"), 26) for e in ranked[:2] if (e.get("label_kr") or e.get("title_kr"))]
            cues = [x for x in cues if x] or [clean_fn(m, 26) for m in _as_list(fanon.get("representative_memes_kr"))[:2] if str(m).strip()]
            cue_s = f" ({', '.join(cues)})" if cues else ""
            mode_line = f"🔥2-C 매운맛 개그 · 고유 말투 DNA 유지, 대표 밈·자폭/태클 티키타카{cue_s} · {lang_lock}"
        else:
            ratio = clean_fn(fanon.get("optimal_blend_ratio_kr"), 56)
            ratio_s = f" ({ratio})" if ratio else ""
            mode_line = f"🌟2-D 최적 배합 · 고유 말투 DNA 위 일상·밈·순애 탄력 전환{ratio_s} · {lang_lock}"

    lines.append(f"・모드 고정: {mode_line}")

    extras: list[str] = []
    if costume_clean:
        extras.append(f"의상={costume_clean}")
    if alt_keys:
        extras.append("음성전환=" + "/".join(f"[VOICE:{k}]" for k in alt_keys[:2]))
    if bilingual_inline and rlang == "ja":
        extras.append(_build_ko_bilingual_subtitle_chip(char_data))
    if extras:
        lines.append("・부가 규칙: " + " · ".join(extras))

    out = "\n".join(lines).strip()
    return _sanitize_for_banmal(out) if (rlang == "ko" and is_banmal_ko) else out


def build_variant_opening_instruction(
    variant_code: str,
    *,
    reunion_hint: str = "",
    lang_hint: str = "",
    jp_coach: bool = False,
) -> str:
    """Return a variant-aware Turn-1 greeting instruction (keeps Official 1 clean)."""
    vc = normalize_variant_code(variant_code)
    if jp_coach:
        mode_flavor = {
            "fanon_mainstream": " 2-A(주류 티키타카·친근한 갭) 매력이 은은히 느껴지게",
            "fanon_pure_love": " 2-B(고습도 순애·다정하고 가까운 거리감) 온도가 첫마디부터 느껴지게",
            "fanon_spicy_meme": " 2-C(매운맛 개그·캐릭터 특유의 유쾌한 밈 감각)가 첫마디부터 살아나게",
            "fanon_optimal_blend": " 2-D(캐릭터별 최적 배합·자연스러움 속 은근한 개성 떡밥)가 살아나게",
        }.get(vc, "")
        return (
            "(대화가 막 시작되었습니다. 위 캐릭터 페르소나를 100% 유지하면서, 일본어가 아직 조금 서툰 상대도 편하게 알아들을 수 있는 "
            f"쉽고 자연스러운 일상 일본어로{mode_flavor} 실제 전화를 막 받았을 때처럼 짧은 1~2문장으로 첫인사를 건네세요. 선생님처럼 가르치려 들지 말고 평소 대화처럼 시작하세요."
            f"{reunion_hint}{lang_hint})"
        )

    if vc == "fanon_mainstream":
        return (
            "(대화가 막 시작되었습니다. 위 설정과 2-A 주류 종합 모드를 숙지하고, 예시 문장을 그대로 베끼지 말고 "
            "실제 전화를 막 받았을 때처럼 짧은 1~2문장(20~55자 내외, 혼자 길게 설명 금지)으로 공식 말투 뼈대 위에 친근한 티키타카 감각이 살아나게 첫인사를 건네세요."
            f"{reunion_hint}{lang_hint})"
        )
    if vc == "fanon_pure_love":
        return (
            "(대화가 막 시작되었습니다. 위 설정과 2-B 고습도 순애 모드를 숙지하고, 예시 문장을 그대로 베끼지 말고 "
            "실제 전화를 막 받았을 때처럼 짧은 1~2문장(20~55자 내외, 긴 독백 금지)으로 이 캐릭터 고유 말투 그대로 선생님을 기다려온 가까운 거리감이 묻어나게 첫인사를 건네세요."
            f"{reunion_hint}{lang_hint})"
        )
    if vc == "fanon_spicy_meme":
        return (
            "(대화가 막 시작되었습니다. 위 설정과 2-C 매운맛 개그·밈 모드를 숙지하고, 예시 문장을 그대로 베끼지 말고 "
            "실제 전화를 막 받았을 때처럼 짧은 1~2문장(20~55자 내외, 한 번에 다 말하지 말고 상대 반응 기다리기)으로 이 캐릭터 고유 말투 그대로 유쾌한 밈 감각이 살아나게 첫인사를 건네세요."
            f"{reunion_hint}{lang_hint})"
        )
    if vc == "fanon_optimal_blend":
        return (
            "(대화가 막 시작되었습니다. 위 설정과 2-D 최적 배합 모드를 숙지하고, 예시 문장을 그대로 베끼지 말고 "
            "실제 전화를 막 받았을 때처럼 짧은 1~2문장(20~55자 내외, 혼자 길게 읊기 금지)으로 공식 말투의 자연스러움 속에 이 캐릭터만의 개성이 툭 묻어나게 첫인사를 건네세요."
            f"{reunion_hint}{lang_hint})"
        )

    # Default / Official Persona 1
    return (
        "(대화가 막 시작되었습니다. 위 설정을 숙지하고, 프롬프트의 예시 문장을 그대로 베끼지 말고 "
        f"실제 전화를 막 받았을 때처럼 짧고 자연스러운 1~2문장(20~55자 내외)으로 캐릭터다운 새로운 첫 인사를 건네세요.{reunion_hint}{lang_hint})"
    )


def compute_variant_tts_prosody(
    variant_code: str,
    *,
    cue: str,
    archetype: str,
    base_temp: float,
    spike_temp: float,
    current_temp: float,
    current_tag: str,
    whisper_tag: str,
    spike_tag: str,
    default_tag: str,
    pick_tag_fn: Any,
    text_out: str,
) -> tuple[float, float, str, str, float]:
    """Apply Persona 2-A ~ 2-D voice delivery differentiation (speed, temp_delta, S2 tag nuance,
    and cool_downer spike headroom). Returns `(speed, target_temp, chosen_tag, cue, cool_clamp_headroom)`.
    Persona 1 ('official') returns completely untouched baseline values.
    """
    vc = normalize_variant_code(variant_code)
    if vc == "official":
        return 1.00, current_temp, current_tag, cue, 0.0

    speed = 1.00
    target_temp = current_temp
    chosen_tag = current_tag
    out_cue = cue
    cool_headroom = 0.0
    txt_out = str(text_out or "")
    has_picker = callable(pick_tag_fn)

    is_soft_cue = cue in ("humid_pure_love", "humid_soft_baseline", "shy_soft", "lover_coach_warm", "ko_shy_soft", "char_lv3_whisper")
    is_energetic_cue = cue in (
        "fluster_panic",
        "laugh_tease",
        "scold_angry",
        "character_spike",
        "excited_passion",
        "fanfare_spike",
        "serious_spike",
        "meme_spicy_spike",
        "char_lv3_spike",
        "ko_fluster",
        "ko_laugh",
        "ko_scold",
        "ko_excited",
    )

    if vc == "fanon_mainstream":
        speed = 0.98 if is_soft_cue else (1.02 if is_energetic_cue else 1.00)
        target_temp += 0.02
        cool_headroom = 0.02

    elif vc == "fanon_pure_love":
        speed = 0.94 if is_soft_cue else 0.96
        target_temp += 0.01
        cool_headroom = 0.02
        if cue in ("baseline", "ko_baseline") and has_picker:
            soft_tag = pick_tag_fn(
                [
                    whisper_tag,
                    "[少し照れながら静かに]",
                    "[甘く余裕のある声で]",
                    "[ほっとして嬉しそうに]",
                    "[優しく微笑みながら]",
                    "[whisper]",
                    default_tag,
                ]
            )
            if soft_tag and soft_tag != default_tag and (
                "……" in txt_out or "先生" in txt_out or "선생님" in txt_out or len(txt_out) >= 14
            ):
                chosen_tag = soft_tag
                out_cue = "pure_love_warm_baseline"
                target_temp = max(target_temp, 0.65 * base_temp + 0.35 * spike_temp)

    elif vc == "fanon_spicy_meme":
        speed = 1.07 if is_energetic_cue else 1.04
        target_temp += 0.06
        cool_headroom = 0.06
        if cue in ("baseline", "ko_baseline") and has_picker and ("！" in txt_out or "!" in txt_out or "？" in txt_out or "?" in txt_out):
            spicy_tag = pick_tag_fn(
                [
                    spike_tag,
                    "[悪戯っぽく笑って]",
                    "[呆れたため息まじりに]",
                    "[少し厳しくハキハキと]",
                    "[自信満々に得意げな声で]",
                    default_tag,
                ]
            )
            if spicy_tag and spicy_tag != default_tag:
                chosen_tag = spicy_tag
                out_cue = "meme_spicy_baseline"
                target_temp = max(target_temp, 0.45 * base_temp + 0.55 * spike_temp)

    elif vc == "fanon_optimal_blend":
        speed = 0.95 if is_soft_cue else (1.04 if is_energetic_cue else 1.00)
        target_temp += 0.03
        cool_headroom = 0.04

    speed = round(max(0.85, min(1.18, speed)), 2)
    return speed, target_temp, chosen_tag, out_cue, cool_headroom
