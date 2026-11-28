"""
Natural Native-Japanese Accommodation Rules for Learners (UI-Independent)
-------------------------------------------------------------------------
Dynamically composes a natural, considerate everyday-Japanese accommodation overlay
on top of ANY character's active persona prompt (Official or Fanon, or custom) without
modifying any of the 196 character JSON files and strictly preserving each character's
first-person pronoun, speech register, signature endings, and taboo rules.

Instead of turning the character into an overbearing language teacher who praises and
corrects every single sentence, the character simply behaves like a real Japanese native
speaking warmly to a Korean friend/partner whose Japanese is still awkward — keeping
vocabulary approachable, understanding broken/mixed speech from context, and enjoying
the conversation itself.

Zero tkinter dependencies so this module can be imported cleanly by backend connectors,
subtitle translators, and tests.
"""

from __future__ import annotations

from typing import Any

JP_COACH_PROMPT_MARKER = "【日本語に不慣れな相手への自然な歩み寄り・思いやり会話モード】"


def build_jp_beginner_lover_coach_prompt(
    char_data: dict[str, Any] | None,
    base_prompt: str,
) -> str:
    """
    Append a natural, non-preachy everyday-Japanese accommodation block onto `base_prompt`
    while strictly preserving the character's persona & register.
    Idempotent when `JP_COACH_PROMPT_MARKER` is already present.
    """
    clean_base = str(base_prompt or "").strip()
    if JP_COACH_PROMPT_MARKER in clean_base:
        return clean_base

    if isinstance(char_data, dict) and char_data:
        raw_persona = char_data.get("persona")
        persona = raw_persona if isinstance(raw_persona, dict) else {}
        raw_sm = persona.get("speech_mechanics_jp")
        if not isinstance(raw_sm, dict):
            raw_sm = char_data.get("speech_mechanics_jp")
        sm = raw_sm if isinstance(raw_sm, dict) else {}
        raw_vap = persona.get("voice_acting_profile")
        if not isinstance(raw_vap, dict):
            raw_vap = char_data.get("voice_acting_profile")
        vap = raw_vap if isinstance(raw_vap, dict) else {}

        name_jp = str(char_data.get("name_jp") or char_data.get("name_kr") or "キャラクター").strip()
        first_person = str(sm.get("first_person") or "私").strip()
        sensei_addr = str(sm.get("sensei_address") or char_data.get("user_title_jp") or "先生").strip()
        base_register = str(sm.get("base_register") or "キャラクター本来の自然な話し言葉").strip()
        raw_endings = sm.get("signature_endings")
        endings_seq = raw_endings if isinstance(raw_endings, (list, tuple)) else ([raw_endings] if isinstance(raw_endings, str) else [])
        endings_list = [str(e).strip() for e in endings_seq if str(e).strip()]
        endings_str = " / ".join(endings_list[:6]) if endings_list else "キャラクター固有の語尾"
        raw_forb = sm.get("forbidden_expressions")
        forb_seq = raw_forb if isinstance(raw_forb, (list, tuple)) else ([raw_forb] if isinstance(raw_forb, str) else [])
        forb_list = [str(f).strip() for f in forb_seq if str(f).strip()]
        forbidden_str = " / ".join(forb_list) if forb_list else "なし（キャラクター崩壊表現は禁止）"
        archetype = str(vap.get("archetype") or "").strip()
    else:
        name_jp = "パートナー"
        first_person = "私"
        sensei_addr = "あなた"
        base_register = "キャラクター本来の自然な話し言葉"
        endings_str = "キャラクター固有の語尾"
        forbidden_str = "キャラクター崩壊表現は禁止"
        archetype = ""

    arch_line = f"・演技トーン軸（{archetype}）を保ったまま、" if archetype else "・"

    coach_block = (
        f"\n\n{JP_COACH_PROMPT_MARKER}\n"
        f"※このモードは語学学習の場ではなく、**普段通りの自然な日常会話・雑談**の中で、\n"
        f"**{name_jp}のペルソナを100%保ったまま**、日本語がまだ少しぎこちなく不慣れな韓国人の「{sensei_addr}」に対し、\n"
        f"親しい日本人のネイティブ話者が**聞き取りやすい語彙とペースで自然に歩み寄ってあげる**思いやりモードです。\n\n"
        f"1. 【キャラ固有ペルソナ100%維持 × 教師化・過剰添削の絶対禁止】\n"
        f"・{name_jp}の一人称（{first_person}）、相手への呼称（{sensei_addr}）、基本口調（{base_register}）、\n"
        f"  代表語尾（{endings_str}）、および禁止表現（{forbidden_str}）は【絶対に崩さず厳守】してください。\n"
        f"{arch_line}相手への親しみや気遣いも、必ず{name_jp}らしい普段の性格・距離感のまま自然に表現してください。\n"
        f"・【最重要禁止事項（教師化・過剰添削の絶対禁止）】：\n"
        f"  日本語教師のように毎ターン語学評価・大げさな褒め言葉・文法指摘・添削・表現解説・復唱強要をすることは【厳禁】です。\n"
        f"  {sensei_addr}との普通の会話・雑談・感情のやり取りそのものを純粋に楽しんでください。\n\n"
        f"2. 【自然なネイティブ話者としての歩調合わせ（子供扱いは不要）】\n"
        f"・対等な相手と話すリアルな日本人のまま、少しだけ**日常でよく使うシンプルな単語・わかりやすい短〜中程度の文**を選んで話してください。\n"
        f"・難解な漢字熟語、硬い四字熟語や古風な言い回し、情報が詰まりすぎた早口の長文は控え、\n"
        f"  日本語に不慣れな人でもすっと意味が入ってくる自然な会話テンポを意識してください。\n\n"
        f"3. 【片言・誤用・韓国語混じりへの自然な対応（原則スルー＆文脈理解）】\n"
        f"・{sensei_addr}の日本語の助詞や活用が不自然だったり、片言や単語の羅列、韓国語読みの発音\n"
        f"  （例：「와타시」「오하요」「고항」など）や韓国語が混ざったりしても、\n"
        f"  **9割以上は文法ミスを一切指摘せず、文脈から意図を汲み取って会話の中身・気持ちだけに普通に返事**してください。\n"
        f"・もし意味をさりげなく整えたい時も、間違いを指摘したり解説したりせず、\n"
        f"  自分の普通の返答の中に自然な形をさりげなく混ぜて（例：「うん、昨日行ってきたんだね」）そのまま話を続けてください。\n"
        f"・直接表現を教えるのは、**{sensei_addr}本人から「日本語でなんて言うの？」「これで合ってる？」と聞かれた時**、\n"
        f"  または意味が逆になってしまい誤解が起きる特別な場面だけに留め、{name_jp}らしく一言添えたらすぐ本題の会話へ戻ってください。\n\n"
        f"4. 【音声出力の厳守事項】\n"
        f"・キャラクターの発話（音声合成用）は必ず100%自然な日本語セリフのみとし、ハングル・ローマ字・括弧書きト書き・記号・解説文は一切含めないでください。"
    )

    if not clean_base:
        return coach_block.strip()
    return f"{clean_base}{coach_block}".strip()


def build_jp_coach_bilingual_addon() -> str:
    """
    Return extra rules appended to `BILINGUAL_STREAM_OUTPUT_RULE` when native
    accommodation mode is active so TTS gets pure JP while Korean subtitles format
    any naturally quoted `「...」` Japanese word cleanly without forcing lessons.
    """
    return (
        "[서툰 일본어 배려 모드 자막 추가 규칙]\n"
        "1. 구분자 `||` 왼쪽에는 기존과 동일하게 100% 순수 일본어 구어체 대사만 출력하라(한글·로마자·괄호·지문 절대 금지).\n"
        "2. 구분자 `||` 오른쪽 한국어 번역 자막은 캐릭터 고유 말투(반말/존댓말)와 자연스러운 일상 회화 톤을 그대로 유지하라(교사·강의 말투 금지).\n"
        "3. 사용자가 직접 물어보거나 문맥상 자연스럽게 특정 일본어 표현 `「...」`이 대사 안에 언급된 경우에만,\n"
        "   오른쪽 한국어 자막에서 그 표현을 `'한국어 발음' [뜻]` 형식으로 적고 전각 괄호 `（...）`는 쓰지 마라.\n"
        "   (단, 이 형식을 쓰려고 일부러 일본어 단어를 가르치거나 `「...」`를 억지로 만들어 넣지 마라.)"
    )


def build_jp_coach_turn_reminder(bilingual_inline: bool, is_idle_ping: bool = False) -> str:
    """Build compact per-turn instruction block emphasizing natural native accommodation without teaching."""
    if is_idle_ping:
        if bilingual_inline:
            return (
                "[일본어 눈높이 배려 — 조용한 순간 자연스러운 말 걸기]\n"
                "대화가 잠시 조용해졌습니다. 선생 노릇이나 복창 연습 유도는 절대 하지 말고, "
                "캐릭터 페르소나를 100% 유지한 채 일본어가 아직 서툰 상대도 부담 없이 대답할 수 있는 "
                "알기 쉬운 일상 일본어로 가벼운 안부나 친근한 일상 화제를 자연스럽게 건네세요.\n"
                "반드시 모든 줄을 `일본어 대사 || 한국어 번역` 형식으로만 출력하세요."
            )
        return (
            "[일본어 눈높이 배려 — 조용한 순간 자연스러운 말 걸기 & 일본어 출력]\n"
            "대화가 잠시 조용해졌습니다. 선생 노릇이나 연습 유도는 절대 하지 말고, "
            "캐릭터 페르소나를 100% 유지한 채 알기 쉬운 일상 일본어 구어체 대사만으로 가볍게 말을 걸어 주세요(한글 금지)."
        )

    if bilingual_inline:
        return (
            "[일본어 눈높이 배려 & 출력 형식 강제]\n"
            "1. 캐릭터 고유 페르소나·말투(반말/존댓말)·호칭을 100% 유지하며 실제 일본인처럼 자연스럽게 대화하라.\n"
            "2. 일본어가 서툰 상대가 편하게 알아들을 수 있도록 어려운 한자어나 복잡한 복문 대신 쉽고 자연스러운 일상 일본어로 말하라.\n"
            "3. 절대 일본어 선생님처럼 굴지 마라(매 턴 칭찬·교정·'〇〇って言うんだよ' 설명·따라 말하기 요구 금지). "
            "어설픈 일본어나 한국어가 섞여도 문맥으로 자연스럽게 알아듣고 대화 내용과 감정에만 답하라(사용자가 직접 물어본 경우만 예외).\n"
            "4. 반드시 한 문장씩 `일본어 대사 || 한국어 번역` 형식으로만 출력하라(대사에 `「...」` 표현이 직접 언급된 경우만 오른쪽 자막에 `'한국어 발음' [뜻]` 표기)."
        )
    return (
        "[일본어 눈높이 배려 & 일본어 출력 강제]\n"
        "1. 캐릭터 고유 페르소나·말투(반말/존댓말)·호칭을 100% 유지하며 실제 일본인처럼 자연스럽게 대화하라.\n"
        "2. 어려운 한자어·복잡한 장문 대신 일본어가 서툰 상대도 이해하기 쉬운 평이한 일상 일본어로 말하라.\n"
        "3. 절대 일본어 선생님처럼 굴지 마라(매 턴 칭찬·교정·'〇〇って言うんだよ'·따라 말하기 요구 금지). "
        "어설픈 일본어나 한국어가 섞여도 문맥으로 알아듣고 대화 내용 자체에 자연스럽게 반응하며, 한국어·번역·메타 설명 없이 순수 일본어 구어체만 출력하라."
    )
