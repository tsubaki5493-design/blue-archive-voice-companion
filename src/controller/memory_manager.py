"""
MomoTalk Persistent Conversation History, 2-Tier Hybrid Memory & 3-Stage Hygiene Engine
---------------------------------------------------------------------------------------
UI-independent core module (zero tkinter dependencies) storing all user data exclusively
under `%LOCALAPPDATA%\\VoiceCompanionData\\memory\\`.

Key features:
  1. 2-Tier Hybrid Memory per (student base_cid, track in {"official", "fanon", "custom"}):
     - Long-term structured memories (`confirmed_memories`, `pending_memories`, `open_threads`)
     - Short-term recent dialogue transcript (`sessions`)
     - Shared global `sensei_profile.json` visible to all 196 students
     - Persisted `recent_calls.json` powering the MomoTalk "최근 통화" tab across restarts
  2. 3-Stage Anti-Poisoning & Context Hygiene Pipeline:
     - Stage 1: Deterministic filter excluding mic checks, test calls, `[IDLE_PING]` echoes,
       and short trivial sessions (<2 substantive user turns)
     - Stage 2: Context-aware LLM extraction prompt pairing raw user STT + character reply +
       Blue Archive proper-noun hints so STT typos are repaired before storing memories
     - Stage 3: Two-tier promotion (`confirmed` vs `pending`), automatic pre-update snapshot
       backups (last 5), 1-click rollback, and fuzzy deduplication
  3. Natural Reunion Context Builder:
     - Formats current time-of-day, elapsed time since last call, cumulative call count,
       shared Sensei profile, confirmed memories, open threads, and recent turns with subtle
       reunion guidance (never robotically listing memories)
  4. Full JSON Backup / Restore & Per-Session Deletion
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.utils.app_paths import get_memory_dir, harden_user_data_acl, redact_secrets

MEMORY_SCHEMA_VERSION = 1
MAX_CONFIRMED_MEMORIES = 30
MAX_PENDING_MEMORIES = 12
MAX_OPEN_THREADS = 5
MAX_SNAPSHOTS = 5
MAX_SESSIONS_PER_BUCKET = 60
MAX_TURNS_PER_SESSION = 300
MAX_MEMORY_JSON_BYTES = 5 * 1024 * 1024
MAX_BACKUP_JSON_BYTES = 32 * 1024 * 1024
MAX_IMPORT_BUCKETS = 1000
MAX_EXTRACTION_RESPONSE_CHARS = 512_000
DEFAULT_CHECKPOINT_TURN_DELTA = 20
DEFAULT_MIN_SUBSTANTIVE_USER_TURNS = 2

VALID_MEMORY_TRACKS: tuple[str, ...] = ("official", "fanon", "custom")
_STRICT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_ALLOWED_BUCKET_KEYS = frozenset(
    {
        "schema_version",
        "base_cid",
        "track",
        "total_calls",
        "last_called_at",
        "confirmed_memories",
        "pending_memories",
        "open_threads",
        "snapshots",
        "sessions",
    }
)

MEMORY_CATEGORIES: tuple[str, ...] = (
    "preference",
    "promise",
    "event",
    "relationship",
    "fact",
)

MEMORY_CATEGORY_LABELS: dict[str, str] = {
    "preference": "취향·관심사",
    "promise": "약속·일정",
    "event": "함께한 사건",
    "relationship": "관계·호칭",
    "fact": "생활·정보",
}

MEMORY_TRACK_LABELS: dict[str, str] = {
    "official": "📘 공식 원작",
    "fanon": "🎭 2차창작",
    "custom": "✏️ 커스텀",
}

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_NORM_DEDUPE_RE = re.compile(r"[\s.,!?~…·:;\"'()\[\]{}<>/\\-]+")
_IDLE_PING_RE = re.compile(r"\[?\s*IDLE_PING\s*\]?", re.IGNORECASE)

# Explicit mic/system/audio test phrases (Korean, Japanese, English)
_TEST_PHRASE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:아[\s,.~!?]*){1,6}$"),
    re.compile(r"^(?:어[\s,.~!?]*){1,5}$"),
    re.compile(
        r"^(?:(?:아[\s,.~]*){1,4})?(?:(?:마이크|소리|음성|목소리|스피커)\s*)?"
        r"(?:테스트|테스팅|체크|점검)(?:\s*[0-9일이삼사하나둘셋\s.,!?~]*)$"
    ),
    re.compile(
        r"^(?:(?:아[\s,.~]*){1,4})?(?:(?:마이크|소리|음성|목소리|스피커)\s*)?"
        r"(?:잘\s*들려|들려|들리나|들리니|들리세요|들립니까|나오나|되나)[?!.\s~]*$"
    ),
    re.compile(
        r"^(?:(?:아[\s,.~]*){1,4})?(?:하나\s*둘\s*셋|일\s*이\s*삼|원\s*투\s*쓰리|1\s*2\s*3)"
        r"(?:\s*(?:테스트|체크|아아))*[?!.\s~]*$"
    ),
    re.compile(r"^(?:あー+|あ\s*あ)[\s。、!?~]*(?:テスト|マイク|聞こえ)?[\s。、!?~]*$"),
    re.compile(r"^(?:マイク\s*)?(?:テスト|チェック)(?:[0-9一二三\s。、!?~]*)$"),
    re.compile(r"^(?:聞こえる|聞こえますか|声聞こえる)[?？!！。\s~]*$"),
    re.compile(r"^(?:mic\s*test|testing|test|check|check\s*1\s*2(?:\s*3)?|can\s*you\s*hear\s*me)[?!.\s~]*$", re.IGNORECASE),
    re.compile(r"^(?:시청해\s*주셔서\s*감사합니다|구독과\s*좋아요|ご視聴ありがとうございました|チャンネル登録|MBC\s*뉴스)", re.IGNORECASE),
)

_ONLY_PUNCT_RE = re.compile(r"^[\s.,!?~…·:;\"'()\[\]{}<>/\\|_+=\-*#@$%^&]+$")


def persona_variant_to_memory_track(variant_code: str, use_custom_persona: bool = False) -> str:
    """Map persona variant code onto memory bucket track: 'official', 'fanon', or 'custom'."""
    if use_custom_persona:
        return "custom"
    vc = (variant_code or "official").strip().lower()
    if vc == "official":
        return "official"
    return "fanon"


def is_test_or_noise_utterance(text: str) -> bool:
    """
    Stage 1 deterministic hygiene filter.
    Return True for mic tests, audio checks, `[IDLE_PING]` echoes, Whisper boilerplate,
    and punctuation-only strings. Never blocks genuine short conversational replies.
    """
    s = (text or "").strip()
    if not s:
        return True
    if _IDLE_PING_RE.search(s):
        return True
    if _ONLY_PUNCT_RE.match(s):
        return True
    compact = re.sub(r"\s+", " ", s).strip()
    for pat in _TEST_PHRASE_PATTERNS:
        if pat.match(compact):
            return True
    return False


def _norm_for_dedupe(text: str) -> str:
    return _NORM_DEDUPE_RE.sub("", (text or "").strip()).lower()


def _is_near_duplicate(a: str, b: str) -> bool:
    na = _norm_for_dedupe(a)
    nb = _norm_for_dedupe(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 8 and len(nb) >= 8 and (na in nb or nb in na):
        return True
    return False


def _validate_student_id(base_cid: str) -> str:
    """Validate a character/student ID strictly so traversal, drive letters, or NULs can never enter paths."""
    if not isinstance(base_cid, str):
        raise ValueError("Invalid student ID type")
    if "\x00" in base_cid:
        raise ValueError("Invalid student ID: NUL byte not allowed")
    s = base_cid.strip()
    if not s or ".." in s or "/" in s or "\\" in s or ":" in s:
        raise ValueError(f"Invalid student ID: path traversal characters not allowed ({base_cid!r})")
    if not _STRICT_ID_RE.fullmatch(s):
        raise ValueError(f"Invalid student ID format: {base_cid!r}")
    if s.upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Invalid student ID: reserved device name ({s!r})")
    return s


def _validate_track(track: str) -> str:
    """Validate memory track strictly against allowed track names."""
    if track is None:
        return "official"
    if not isinstance(track, str):
        raise ValueError("Invalid memory track type")
    if "\x00" in track or ".." in track or "/" in track or "\\" in track or ":" in track:
        raise ValueError(f"Invalid memory track: path traversal characters not allowed ({track!r})")
    s = track.strip()
    if not s:
        return "official"
    if s not in VALID_MEMORY_TRACKS:
        raise ValueError(f"Unsupported memory track: {track!r}")
    return s


def _validate_session_id(session_id: str) -> str:
    """Validate session_id against NUL bytes, path separators, traversal sequences, and excessive length."""
    if not isinstance(session_id, str):
        raise ValueError("Invalid session_id type")
    if "\x00" in session_id:
        raise ValueError("Invalid session_id: NUL byte not allowed")
    s = session_id.strip()
    if not s or len(s) > 128 or ".." in s or "/" in s or "\\" in s or ":" in s:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return s


def _safe_bucket_key(base_cid: str, track: str) -> str:
    cid_s = _validate_student_id(base_cid)
    tr_s = _validate_track(track)
    return f"{cid_s}__{tr_s}"


def _ensure_within_dir(root_dir: Path, candidate: Path) -> Path:
    """Verify `candidate` is not a symlink and resolves strictly inside `root_dir`."""
    if "\x00" in str(candidate):
        raise ValueError("Path contains NUL byte")
    if candidate.is_symlink():
        raise ValueError(f"Refusing to access symlink inside memory directory: {candidate.name}")
    root = root_dir.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Resolved path escapes memory directory: {candidate}") from exc
    if resolved == root:
        raise ValueError("Resolved path must point to a child file")
    return resolved


def _safe_read_json(path: Path, max_bytes: int = MAX_MEMORY_JSON_BYTES) -> Any:
    """Load UTF-8 JSON from a non-symlink file with a strict byte-size ceiling."""
    if "\x00" in str(path):
        raise ValueError("Path contains NUL byte")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Refusing to read non-regular or symlinked file: {path.name}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"JSON file exceeds size limit ({max_bytes} bytes): {path.name}")
    with open(path, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"JSON file exceeds size limit ({max_bytes} bytes): {path.name}")
    return json.loads(raw.decode("utf-8"))


def _scrub_str(value: Any) -> str:
    return redact_secrets(str(value or ""))


def _atomic_write_json(target: Path, data: Any, *, harden_parent: bool = True) -> Path:
    if "\x00" in str(target):
        raise ValueError("Path contains NUL byte")
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite symlinked file: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if harden_parent:
        harden_user_data_acl(target.parent)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=".mem_", suffix=".tmp", dir=str(target.parent))
    fd_closed = False
    try:
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd_closed = True
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        last_perm_err: Exception | None = None
        for attempt in range(6):
            try:
                os.replace(tmp_name, target)
                last_perm_err = None
                break
            except PermissionError as exc:
                last_perm_err = exc
                time.sleep(0.04 * (attempt + 1))
        if last_perm_err is not None:
            raise last_perm_err
    except Exception:
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return target


def format_time_of_day_kr(now_dt: datetime.datetime | None = None) -> tuple[str, str]:
    """Return `(formatted_clock_str, period_label_kr)` for natural temporal grounding."""
    dt = now_dt or datetime.datetime.now()
    h = dt.hour
    if 0 <= h < 5:
        period = "새벽"
    elif 5 <= h < 10:
        period = "아침"
    elif 10 <= h < 17:
        period = "낮"
    elif 17 <= h < 21:
        period = "저녁"
    else:
        period = "밤"
    clock_str = dt.strftime("%Y-%m-%d %H:%M")
    return clock_str, period


def format_elapsed_since_kr(last_ts: float | None, now_ts: float | None = None) -> str:
    """Format elapsed duration since last call into natural Korean phrasing."""
    if not last_ts or last_ts <= 0:
        return "첫 통화"
    now = time.time() if now_ts is None else float(now_ts)
    diff = max(0.0, now - float(last_ts))
    if diff < 600:
        return "방금 전"
    if diff < 3600:
        mins = max(10, int(diff // 60))
        return f"약 {mins}분 전"
    now_dt = datetime.datetime.fromtimestamp(now)
    last_dt = datetime.datetime.fromtimestamp(float(last_ts))
    day_diff = (now_dt.date() - last_dt.date()).days
    if day_diff <= 0:
        hours = max(1, int(diff // 3600))
        return f"오늘 ({hours}시간 전)"
    if day_diff == 1:
        return "어제 이후 (하루 만)"
    if day_diff < 7:
        return f"{day_diff}일 만"
    if day_diff < 30:
        weeks = max(1, day_diff // 7)
        return f"약 {weeks}주 만"
    months = max(1, day_diff // 30)
    return f"오랜만 (약 {months}개월 만)"


def filter_turns_for_memory_extraction(
    turns: list[dict[str, Any]],
    min_substantive_user_turns: int = DEFAULT_MIN_SUBSTANTIVE_USER_TURNS,
) -> list[dict[str, Any]]:
    """
    Stage 1 turn-level & session-level hygiene filter.
    Drops explicit mic/test user utterances (and their immediate assistant responses) and
    isolated unanswered proactive pings. Returns `[]` if fewer than `min_substantive_user_turns`
    meaningful user turns remain.
    """
    if not turns:
        return []

    filtered: list[dict[str, Any]] = []
    skip_next_assistant = False
    substantive_user_count = 0

    for idx, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        if role == "user":
            raw_txt = str(turn.get("corrected_text") or turn.get("raw_stt") or turn.get("kr_text") or turn.get("jp_text") or "").strip()
            if bool(turn.get("filtered_noise")) or is_test_or_noise_utterance(raw_txt):
                skip_next_assistant = True
                continue
            skip_next_assistant = False
            substantive_user_count += 1
            filtered.append({"turn_index": idx, **turn})
        elif role == "assistant":
            if skip_next_assistant:
                skip_next_assistant = False
                continue
            jp_t = str(turn.get("jp_text") or "").strip()
            kr_t = str(turn.get("kr_text") or "").strip()
            if not jp_t and not kr_t:
                continue
            # Keep proactive ping assistant line only if there is a subsequent user reply
            if bool(turn.get("is_proactive_ping")):
                has_later_user = any(
                    isinstance(t, dict)
                    and str(t.get("role") or "").lower() == "user"
                    and not is_test_or_noise_utterance(
                        str(t.get("corrected_text") or t.get("raw_stt") or t.get("kr_text") or t.get("jp_text") or "")
                    )
                    for t in turns[idx + 1 :]
                )
                if not has_later_user:
                    continue
            filtered.append({"turn_index": idx, **turn})

    if substantive_user_count < max(1, int(min_substantive_user_turns)):
        return []
    return filtered


def build_memory_extraction_prompt(
    char_data: dict[str, Any] | None,
    track: str,
    existing_confirmed: list[dict[str, Any]],
    existing_pending: list[dict[str, Any]],
    existing_threads: list[str],
    filtered_turns: list[dict[str, Any]],
    sensei_profile: dict[str, Any] | None = None,
) -> str:
    """
    Stage 2 context-aware memory extraction & STT typo cross-check prompt.
    Pairs raw user STT utterances with the character's contextual replies so STT phonetic typos
    are repaired cleanly and uncertain/contradictory claims are held in `add_pending`.
    """
    ch = char_data or {}
    name_kr = str(ch.get("name_kr") or ch.get("id") or "캐릭터").strip()
    name_jp = str(ch.get("name_jp") or "").strip()
    school_kr = str(ch.get("school") or "").strip()
    club_kr = str(ch.get("club") or "").strip()
    track_label = MEMORY_TRACK_LABELS.get(track, track)

    glossary_parts = [p for p in (name_kr, name_jp, school_kr, club_kr, "선생님", "샬레", "키보토스") if p]
    glossary_line = ", ".join(dict.fromkeys(glossary_parts))

    sp_lines: list[str] = []
    if isinstance(sensei_profile, dict):
        nick = str(sensei_profile.get("nickname") or "").strip()
        bio = str(sensei_profile.get("bio_notes") or "").strip()
        prefs = [str(p).strip() for p in (sensei_profile.get("preferences") or []) if str(p).strip()]
        if nick:
            sp_lines.append(f"- 호칭/별명: {nick}")
        if bio:
            sp_lines.append(f"- 메모: {bio}")
        if prefs:
            sp_lines.append(f"- 관심사/취향: {', '.join(prefs)}")
    sensei_block = "\n".join(sp_lines) if sp_lines else "(미등록)"

    conf_lines = [
        f"- [{m.get('id', '')}] ({m.get('category', 'fact')}) {m.get('text', '')}"
        for m in (existing_confirmed or [])
        if isinstance(m, dict) and str(m.get("text") or "").strip()
    ]
    conf_block = "\n".join(conf_lines) if conf_lines else "(없음)"

    pend_lines = [
        f"- [{m.get('id', '')}] ({m.get('category', 'fact')}) {m.get('text', '')} (보류 사유: {m.get('reason', '')})"
        for m in (existing_pending or [])
        if isinstance(m, dict) and str(m.get("text") or "").strip()
    ]
    pend_block = "\n".join(pend_lines) if pend_lines else "(없음)"

    thread_lines = [f"- {t}" for t in (existing_threads or []) if str(t).strip()]
    thread_block = "\n".join(thread_lines) if thread_lines else "(없음)"

    transcript_lines: list[str] = []
    for item in filtered_turns:
        t_idx = item.get("turn_index", 0)
        role = str(item.get("role") or "")
        if role == "user":
            raw_u = str(item.get("raw_stt") or item.get("kr_text") or item.get("jp_text") or "").strip()
            transcript_lines.append(f"[turn {t_idx}] 선생님(STT 원문): {raw_u}")
        else:
            jp_t = str(item.get("jp_text") or "").strip()
            kr_t = str(item.get("kr_text") or "").strip()
            if jp_t and kr_t and jp_t != kr_t:
                transcript_lines.append(f"[turn {t_idx}] {name_kr}: {jp_t} / ({kr_t})")
            else:
                transcript_lines.append(f"[turn {t_idx}] {name_kr}: {kr_t or jp_t}")
    transcript_block = "\n".join(transcript_lines)

    return (
        "너는 AI 보이스 컴패니언의 '문맥 기반 기억 정제기'다.\n"
        "아래 통화 기록을 분석해 다음 대화에서 캐릭터가 자연스럽게 기억할 핵심 정보만 정제된 JSON으로 추출하라.\n\n"
        f"[대화 대상]\n"
        f"- 학생: {name_kr} ({name_jp}) / 해석 트랙: {track_label}\n"
        f"- 고유명사 참고(STT 오타 복원용): {glossary_line}\n\n"
        f"[공통 선생님 프로필 (이미 전역 저장됨 — 중복 저장 금지)]\n{sensei_block}\n\n"
        f"[기존 확정 장기 기억]\n{conf_block}\n\n"
        f"[기존 확인 대기(보류) 기억]\n{pend_block}\n\n"
        f"[기존 진행 중 화제/약속]\n{thread_block}\n\n"
        f"[이번 통화 기록]\n{transcript_block}\n\n"
        "[엄격한 3중 정제 규칙]\n"
        "1. 테스트·잡음 배제: 마이크 테스트, 목소리/발음 확인, 프로그램·AI·프롬프트·버그 언급, 의미 없는 1회성 감탄사는 절대 기억으로 저장하지 마라. 통화 전체가 테스트나 빈 잡담이면 `skip_reason`에 사유를 적고 나머지 배열은 모두 비워라.\n"
        "2. 문맥 기반 STT 오타 교정: '선생님(STT 원문)'은 실시간 음성인식 결과이므로 발음 유사 오타(예: 캐릭터 이름 오타, 이상한 동음어)가 섞일 수 있다. 반드시 앞뒤 흐름과 캐릭터가 문맥상 이해하고 답한 내용을 교차 검증해 원래 의도를 복원하라. 명백한 오타를 교정했다면 `stt_corrections`에 기록하라.\n"
        "3. 확신도 분리 및 모순 방지:\n"
        "   - 분명하고 재사용 가치가 있는 사실·취향·약속·함께한 사건만 `add_confirmed`에 간결한 한국어 1문장(50자 이내)으로 추가하라.\n"
        "   - STT가 불분명해 추측이 섞였거나, 기존 확정 기억과 모순되는 1회성 발화는 절대 바로 확정·덮어쓰기하지 말고 `add_pending`에 보류 사유와 함께 넣어라.\n"
        "   - [기존 확인 대기(보류) 기억] 중 이번 대화에서 사실로 재확인된 항목이 있으면 `promote_pending_ids`에 그 ID를 넣어 확정 승격하라.\n"
        "   - 기존 확정 기억이나 공통 선생님 프로필에 이미 담긴 내용은 중복 추가하지 마라.\n"
        "4. `category`는 반드시 `preference`, `promise`, `event`, `relationship`, `fact` 중 하나만 사용하라.\n"
        "5. `open_threads`는 다음 통화에서 이어갈 만한 미완료 약속·화제만 최대 5개 유지하라(완료된 화제는 제거).\n"
        "6. 설명이나 마크다운 없이 오직 아래 스키마의 유효한 JSON 객체 하나만 출력하라:\n"
        "{\n"
        '  "skip_reason": "",\n'
        '  "stt_corrections": [{"turn_index": 0, "raw": "", "corrected": "", "confidence": "high"}],\n'
        '  "add_confirmed": [{"category": "preference", "text": ""}],\n'
        '  "promote_pending_ids": [],\n'
        '  "update_confirmed": [{"id": "", "text": ""}],\n'
        '  "remove_confirmed_ids": [],\n'
        '  "add_pending": [{"category": "event", "text": "", "reason": ""}],\n'
        '  "open_threads": []\n'
        "}"
    )


def parse_memory_extraction_json(raw_text: str) -> dict[str, Any] | None:
    """Safely extract and normalize the JSON response from the LLM memory extractor."""
    s = (raw_text or "").strip()
    if not s or len(s) > MAX_EXTRACTION_RESPONSE_CHARS:
        return None
    # Strip markdown code fences if present
    fence_m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.IGNORECASE)
    if fence_m:
        s = fence_m.group(1).strip()

    parsed: Any = None
    try:
        parsed = json.loads(s)
    except Exception:
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(s[start : end + 1])
            except Exception:
                return None
        else:
            return None

    if not isinstance(parsed, dict):
        return None

    def _clean_cat(cat: Any) -> str:
        c = str(cat or "fact").strip().lower()
        return c if c in MEMORY_CATEGORIES else "fact"

    out: dict[str, Any] = {
        "skip_reason": _scrub_str(parsed.get("skip_reason")).strip(),
        "stt_corrections": [],
        "add_confirmed": [],
        "promote_pending_ids": [],
        "update_confirmed": [],
        "remove_confirmed_ids": [],
        "add_pending": [],
        "open_threads": None,  # None means "not provided / keep existing"
    }

    for item in parsed.get("stt_corrections") or []:
        if not isinstance(item, dict):
            continue
        try:
            t_idx = int(item.get("turn_index"))
        except Exception:
            continue
        corr = _scrub_str(item.get("corrected")).strip()
        raw_v = _scrub_str(item.get("raw")).strip()
        conf = str(item.get("confidence") or "high").strip().lower()
        if corr and corr != raw_v and conf in ("high", "medium"):
            out["stt_corrections"].append(
                {"turn_index": t_idx, "raw": raw_v[:200], "corrected": corr[:200], "confidence": conf}
            )

    for item in parsed.get("add_confirmed") or []:
        if isinstance(item, str) and item.strip():
            txt = _scrub_str(item).strip()
            if txt and not is_test_or_noise_utterance(txt):
                out["add_confirmed"].append({"category": "fact", "text": txt[:120]})
        elif isinstance(item, dict):
            txt = _scrub_str(item.get("text")).strip()
            if txt and not is_test_or_noise_utterance(txt):
                out["add_confirmed"].append({"category": _clean_cat(item.get("category")), "text": txt[:120]})

    for pid in parsed.get("promote_pending_ids") or []:
        s_pid = str(pid or "").strip()
        if s_pid:
            out["promote_pending_ids"].append(s_pid)

    for item in parsed.get("update_confirmed") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        txt = _scrub_str(item.get("text")).strip()
        if mid and txt:
            out["update_confirmed"].append({"id": mid, "text": txt[:120]})

    for mid in parsed.get("remove_confirmed_ids") or []:
        s_mid = str(mid or "").strip()
        if s_mid:
            out["remove_confirmed_ids"].append(s_mid)

    for item in parsed.get("add_pending") or []:
        if not isinstance(item, dict):
            continue
        txt = _scrub_str(item.get("text")).strip()
        reason = _scrub_str(item.get("reason") or "문맥 확인 필요").strip()
        if txt and not is_test_or_noise_utterance(txt):
            out["add_pending"].append(
                {"category": _clean_cat(item.get("category")), "text": txt[:120], "reason": reason[:80]}
            )

    if "open_threads" in parsed and isinstance(parsed.get("open_threads"), list):
        threads: list[str] = []
        for t in parsed.get("open_threads") or []:
            st = _scrub_str(t).strip()
            if st and not any(_is_near_duplicate(st, prev) for prev in threads):
                threads.append(st[:100])
        out["open_threads"] = threads[:MAX_OPEN_THREADS]

    return out


class MemoryManager:
    """Thread-safe persistent store for MomoTalk logs, 2-tier student memory, and Sensei profile."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir_override = Path(base_dir).resolve() if base_dir is not None else None
        self._lock = threading.RLock()

    @property
    def memory_dir(self) -> Path:
        if self._base_dir_override is not None:
            self._base_dir_override.mkdir(parents=True, exist_ok=True)
            harden_user_data_acl(self._base_dir_override)
            return self._base_dir_override
        return get_memory_dir()

    @property
    def students_dir(self) -> Path:
        p = self.memory_dir / "students"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def sensei_profile_path(self) -> Path:
        return _ensure_within_dir(self.memory_dir, self.memory_dir / "sensei_profile.json")

    @property
    def recent_calls_path(self) -> Path:
        return _ensure_within_dir(self.memory_dir, self.memory_dir / "recent_calls.json")

    def _bucket_path(self, base_cid: str, track: str) -> Path:
        key = _safe_bucket_key(base_cid, track)
        candidate = self.students_dir / f"{key}.json"
        resolved = _ensure_within_dir(self.students_dir, candidate)
        if resolved.parent != self.students_dir.resolve():
            raise ValueError("Bucket path must be a direct child of students directory")
        return resolved

    @staticmethod
    def _default_bucket(base_cid: str, track: str) -> dict[str, Any]:
        clean_cid = _validate_student_id(base_cid)
        clean_track = _validate_track(track)
        return {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "base_cid": clean_cid,
            "track": clean_track,
            "total_calls": 0,
            "last_called_at": 0.0,
            "confirmed_memories": [],
            "pending_memories": [],
            "open_threads": [],
            "snapshots": [],
            "sessions": [],
        }

    def _load_bucket_unlocked(self, base_cid: str, track: str) -> dict[str, Any]:
        path = self._bucket_path(base_cid, track)
        base = self._default_bucket(base_cid, track)
        if path.is_file():
            try:
                raw = _safe_read_json(path, max_bytes=MAX_MEMORY_JSON_BYTES)
                if isinstance(raw, dict):
                    for k in _ALLOWED_BUCKET_KEYS:
                        if k in raw:
                            base[k] = raw[k]
                    base["base_cid"] = _validate_student_id(base_cid)
                    base["track"] = _validate_track(track)
                    for k in ("confirmed_memories", "pending_memories", "open_threads", "snapshots", "sessions"):
                        if not isinstance(base.get(k), list):
                            base[k] = []
                    base["total_calls"] = int(base.get("total_calls", 0) or 0)
                    base["last_called_at"] = float(base.get("last_called_at", 0.0) or 0.0)
            except Exception:
                pass
        return base

    def _save_bucket_unlocked(self, base_cid: str, track: str, bucket: dict[str, Any]) -> Path:
        clean_cid = _validate_student_id(base_cid)
        clean_track = _validate_track(track)
        target = self._bucket_path(clean_cid, clean_track)
        sanitized: dict[str, Any] = {k: bucket[k] for k in _ALLOWED_BUCKET_KEYS if k in bucket}
        sanitized["schema_version"] = MEMORY_SCHEMA_VERSION
        sanitized["base_cid"] = clean_cid
        sanitized["track"] = clean_track
        if len(sanitized.get("sessions", [])) > MAX_SESSIONS_PER_BUCKET:
            sanitized["sessions"] = sanitized["sessions"][-MAX_SESSIONS_PER_BUCKET:]
        if len(sanitized.get("snapshots", [])) > MAX_SNAPSHOTS:
            sanitized["snapshots"] = sanitized["snapshots"][-MAX_SNAPSHOTS:]
        return _atomic_write_json(target, sanitized)

    # ------------------------------------------------------------------
    # Shared Sensei Profile
    # ------------------------------------------------------------------

    def load_sensei_profile(self) -> dict[str, Any]:
        with self._lock:
            default = {
                "nickname": "",
                "bio_notes": "",
                "preferences": [],
                "updated_at": 0.0,
            }
            if self.sensei_profile_path.is_file():
                try:
                    raw = _safe_read_json(self.sensei_profile_path, max_bytes=MAX_MEMORY_JSON_BYTES)
                    if isinstance(raw, dict):
                        default["nickname"] = _scrub_str(raw.get("nickname")).strip()
                        default["bio_notes"] = _scrub_str(raw.get("bio_notes")).strip()
                        prefs = raw.get("preferences") or []
                        if isinstance(prefs, list):
                            default["preferences"] = [_scrub_str(p).strip() for p in prefs if str(p).strip()][:20]
                        default["updated_at"] = float(raw.get("updated_at", 0.0) or 0.0)
                except Exception:
                    pass
            return default

    def save_sensei_profile(
        self,
        nickname: str = "",
        bio_notes: str = "",
        preferences: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            clean_prefs: list[str] = []
            for p in preferences or []:
                sp = _scrub_str(p).strip()
                if sp and sp not in clean_prefs:
                    clean_prefs.append(sp[:50])
            data = {
                "nickname": _scrub_str(nickname).strip()[:40],
                "bio_notes": _scrub_str(bio_notes).strip()[:600],
                "preferences": clean_prefs[:20],
                "updated_at": time.time(),
            }
            _atomic_write_json(self.sensei_profile_path, data)
            return data

    # ------------------------------------------------------------------
    # Persisted Recent Calls (Left 💬 Tab)
    # ------------------------------------------------------------------

    def _load_recent_unlocked(self) -> list[dict[str, Any]]:
        if self.recent_calls_path.is_file():
            try:
                raw = _safe_read_json(self.recent_calls_path, max_bytes=MAX_MEMORY_JSON_BYTES)
                entries = raw.get("entries") if isinstance(raw, dict) else None
                if isinstance(entries, list):
                    return [e for e in entries if isinstance(e, dict) and str(e.get("entry_id") or "").strip()]
            except Exception:
                pass
        return []

    def _save_recent_unlocked(self, entries: list[dict[str, Any]]) -> None:
        _atomic_write_json(self.recent_calls_path, {"schema_version": MEMORY_SCHEMA_VERSION, "entries": entries[:120]})

    def get_recent_calls(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._load_recent_unlocked())

    def get_recent_entry_ids(self) -> list[str]:
        with self._lock:
            return [str(e["entry_id"]) for e in self._load_recent_unlocked() if str(e.get("entry_id") or "").strip()]

    def record_recent_call(
        self,
        base_cid: str,
        entry_id: str,
        last_preview: str = "",
        called_at: float | None = None,
        increment_calls: bool = False,
    ) -> None:
        with self._lock:
            entries = self._load_recent_unlocked()
            eid = _validate_student_id(str(entry_id or base_cid).strip())
            bcid = _validate_student_id(str(base_cid or eid).strip())
            ts = time.time() if called_at is None else float(called_at)
            existing = next((e for e in entries if e.get("entry_id") == eid or e.get("base_cid") == bcid), None)
            prev_calls = int(existing.get("total_calls", 0) or 0) if existing else 0
            prev_preview = _scrub_str(existing.get("last_preview")) if existing else ""
            entries = [e for e in entries if e.get("entry_id") != eid and e.get("base_cid") != bcid]
            entries.insert(
                0,
                {
                    "base_cid": bcid,
                    "entry_id": eid,
                    "last_called_at": ts,
                    "last_preview": _scrub_str(last_preview or prev_preview).strip()[:120],
                    "total_calls": prev_calls + (1 if increment_calls else 0),
                },
            )
            self._save_recent_unlocked(entries)

    def _refresh_recent_entry_for_student_unlocked(self, base_cid: str) -> None:
        """Recompute or remove recent_calls entry after session deletions/resets."""
        latest_ts = 0.0
        latest_eid = base_cid
        latest_preview = ""
        total_calls = 0
        for tr in ("official", "fanon", "custom"):
            p = self._bucket_path(base_cid, tr)
            if not p.is_file():
                continue
            b = self._load_bucket_unlocked(base_cid, tr)
            sessions = b.get("sessions") or []
            total_calls += len(sessions)
            for sess in sessions:
                sts = float(sess.get("ended_at") or sess.get("started_at") or 0.0)
                if sts >= latest_ts:
                    latest_ts = sts
                    latest_eid = str(sess.get("entry_id") or base_cid)
                    turns = sess.get("turns") or []
                    for t in reversed(turns):
                        txt = str(t.get("kr_text") or t.get("jp_text") or "").strip()
                        if txt:
                            latest_preview = txt[:120]
                            break
        entries = self._load_recent_unlocked()
        entries = [e for e in entries if e.get("base_cid") != base_cid and e.get("entry_id") != base_cid]
        if total_calls > 0 and latest_ts > 0:
            entries.append(
                {
                    "base_cid": base_cid,
                    "entry_id": latest_eid,
                    "last_called_at": latest_ts,
                    "last_preview": latest_preview,
                    "total_calls": total_calls,
                }
            )
            entries.sort(key=lambda e: float(e.get("last_called_at", 0.0) or 0.0), reverse=True)
        self._save_recent_unlocked(entries)

    # ------------------------------------------------------------------
    # Student Bucket Read & Session Lifecycle
    # ------------------------------------------------------------------

    def get_student_bucket(self, base_cid: str, track: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._load_bucket_unlocked(base_cid, track))

    def has_student_history(self, base_cid: str, track: str) -> bool:
        with self._lock:
            b = self._load_bucket_unlocked(base_cid, track)
            for s in b.get("sessions") or []:
                if s.get("turns"):
                    return True
            return False

    def get_student_memory_counts(self, base_cid: str, track: str) -> dict[str, int]:
        with self._lock:
            b = self._load_bucket_unlocked(base_cid, track)
            sessions_with_turns = [s for s in (b.get("sessions") or []) if s.get("turns")]
            return {
                "confirmed": len(b.get("confirmed_memories") or []),
                "pending": len(b.get("pending_memories") or []),
                "threads": len(b.get("open_threads") or []),
                "sessions": len(sessions_with_turns),
                "snapshots": len(b.get("snapshots") or []),
                "total_calls": int(b.get("total_calls", 0) or 0),
            }

    def begin_session(
        self,
        base_cid: str,
        track: str,
        entry_id: str = "",
        costume_label_kr: str = "",
        reply_language: str = "ja",
        started_at: float | None = None,
    ) -> str:
        """Create and persist a new active call session for `(base_cid, track)`."""
        clean_cid = _validate_student_id(base_cid)
        clean_track = _validate_track(track)
        clean_eid = _validate_student_id(entry_id) if str(entry_id or "").strip() else clean_cid
        with self._lock:
            ts = time.time() if started_at is None else float(started_at)
            dt_tag = datetime.datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M%S")
            sess_id = f"sess_{dt_tag}_{uuid.uuid4().hex[:6]}"
            b = self._load_bucket_unlocked(clean_cid, clean_track)
            b["sessions"].append(
                {
                    "session_id": sess_id,
                    "entry_id": clean_eid,
                    "costume_label_kr": _scrub_str(costume_label_kr).strip()[:80],
                    "reply_language": "ko" if str(reply_language).lower() == "ko" else "ja",
                    "started_at": ts,
                    "ended_at": 0.0,
                    "summarized_turn_count": 0,
                    "needs_summary": False,
                    "turns": [],
                }
            )
            self._save_bucket_unlocked(clean_cid, clean_track, b)
            return sess_id

    def append_turn(
        self,
        base_cid: str,
        track: str,
        session_id: str,
        role: str,
        jp_text: str = "",
        kr_text: str = "",
        raw_stt: str = "",
        is_proactive_ping: bool = False,
        ts: float | None = None,
        checkpoint_delta: int = DEFAULT_CHECKPOINT_TURN_DELTA,
    ) -> tuple[int, bool]:
        """
        Append a conversation turn to an active session.
        Returns `(total_turns_in_session, should_checkpoint_now)`.
        """
        clean_cid = _validate_student_id(base_cid)
        clean_track = _validate_track(track)
        clean_sid = _validate_session_id(session_id)
        clean_role = "user" if str(role).strip().lower() == "user" else "assistant"
        jp_s = _scrub_str(jp_text).strip()
        kr_s = _scrub_str(kr_text).strip()
        stt_s = _scrub_str(raw_stt or (kr_s or jp_s if clean_role == "user" else "")).strip()
        if not jp_s and not kr_s and not stt_s:
            return 0, False

        now = time.time() if ts is None else float(ts)
        is_noise = (clean_role == "user") and is_test_or_noise_utterance(stt_s)

        with self._lock:
            b = self._load_bucket_unlocked(clean_cid, clean_track)
            sess = next((s for s in reversed(b["sessions"]) if s.get("session_id") == clean_sid), None)
            if sess is None:
                return 0, False
            turns = sess.setdefault("turns", [])
            turns.append(
                {
                    "ts": now,
                    "role": clean_role,
                    "jp_text": jp_s or stt_s,
                    "kr_text": kr_s or stt_s,
                    "raw_stt": stt_s if clean_role == "user" else "",
                    "corrected_text": "",
                    "is_proactive_ping": bool(is_proactive_ping),
                    "filtered_noise": bool(is_noise),
                }
            )
            if len(turns) > MAX_TURNS_PER_SESSION:
                overflow = len(turns) - MAX_TURNS_PER_SESSION
                sess["turns"] = turns[overflow:]
                sess["summarized_turn_count"] = max(0, int(sess.get("summarized_turn_count", 0)) - overflow)

            # Mark `needs_summary` once enough substantive user turns exist
            unsummarized_turns = sess["turns"][int(sess.get("summarized_turn_count", 0)) :]
            substantive_pending = filter_turns_for_memory_extraction(
                unsummarized_turns,
                min_substantive_user_turns=DEFAULT_MIN_SUBSTANTIVE_USER_TURNS,
            )
            if substantive_pending:
                sess["needs_summary"] = True

            b["last_called_at"] = now
            self._save_bucket_unlocked(clean_cid, clean_track, b)

            delta = len(sess["turns"]) - int(sess.get("summarized_turn_count", 0))
            should_cp = bool(substantive_pending and delta >= max(6, int(checkpoint_delta)))
            return len(sess["turns"]), should_cp

    def end_session(
        self,
        base_cid: str,
        track: str,
        session_id: str,
        ended_at: float | None = None,
    ) -> dict[str, Any]:
        """
        Finalize an active session. Drops empty sessions (0 turns) cleanly so failed/aborted
        calls don't leave empty dividers in the MomoTalk timeline.
        Returns `{"kept": bool, "needs_summary": bool, "turn_count": int}`.
        """
        clean_cid = _validate_student_id(base_cid)
        clean_track = _validate_track(track)
        clean_sid = _validate_session_id(session_id)
        now = time.time() if ended_at is None else float(ended_at)
        with self._lock:
            b = self._load_bucket_unlocked(clean_cid, clean_track)
            idx = next((i for i in range(len(b["sessions"]) - 1, -1, -1) if b["sessions"][i].get("session_id") == clean_sid), -1)
            if idx < 0:
                return {"kept": False, "needs_summary": False, "turn_count": 0}

            sess = b["sessions"][idx]
            turns = sess.get("turns") or []
            if not turns:
                b["sessions"].pop(idx)
                self._save_bucket_unlocked(clean_cid, clean_track, b)
                return {"kept": False, "needs_summary": False, "turn_count": 0}

            sess["ended_at"] = now
            b["total_calls"] = max(int(b.get("total_calls", 0) or 0) + 1, len(b["sessions"]))
            b["last_called_at"] = now

            unsummarized = turns[int(sess.get("summarized_turn_count", 0)) :]
            eligible = bool(filter_turns_for_memory_extraction(unsummarized))
            sess["needs_summary"] = eligible

            last_preview = ""
            for t in reversed(turns):
                cand = _scrub_str(t.get("kr_text") or t.get("jp_text")).strip()
                if cand:
                    last_preview = cand[:120]
                    break
            self._save_bucket_unlocked(clean_cid, clean_track, b)

            # Update persisted recent calls list (collapsing costume entry_id cleanly)
            self.record_recent_call(
                base_cid=clean_cid,
                entry_id=str(sess.get("entry_id") or clean_cid),
                last_preview=last_preview,
                called_at=now,
                increment_calls=True,
            )
            return {"kept": True, "needs_summary": eligible, "turn_count": len(turns)}

    def delete_session(self, base_cid: str, track: str, session_id: str) -> bool:
        """Delete a single call session from a student's MomoTalk timeline."""
        clean_cid = _validate_student_id(base_cid)
        clean_track = _validate_track(track)
        clean_sid = _validate_session_id(session_id)
        with self._lock:
            b = self._load_bucket_unlocked(clean_cid, clean_track)
            before = len(b["sessions"])
            b["sessions"] = [s for s in b["sessions"] if s.get("session_id") != clean_sid]
            if len(b["sessions"]) == before:
                return False
            b["total_calls"] = len(b["sessions"])
            if b["sessions"]:
                last_s = b["sessions"][-1]
                b["last_called_at"] = float(last_s.get("ended_at") or last_s.get("started_at") or 0.0)
            else:
                b["last_called_at"] = 0.0
            self._save_bucket_unlocked(clean_cid, clean_track, b)
            self._refresh_recent_entry_for_student_unlocked(clean_cid)
            return True

    # ------------------------------------------------------------------
    # Summarization Preparation, Stage 3 Commit & Snapshot Rollback
    # ------------------------------------------------------------------

    def prepare_summary_request(
        self,
        base_cid: str,
        track: str,
        session_id: str,
        char_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Build a Stage 1+2 summarization payload for unsummarized turns in `session_id`.
        Returns None when there are not enough substantive turns (skipping LLM cost).
        """
        clean_cid = _validate_student_id(base_cid)
        clean_track = _validate_track(track)
        clean_sid = _validate_session_id(session_id)
        with self._lock:
            b = self._load_bucket_unlocked(clean_cid, clean_track)
            sess = next((s for s in reversed(b["sessions"]) if s.get("session_id") == clean_sid), None)
            if sess is None:
                return None
            turns = sess.get("turns") or []
            start_idx = int(sess.get("summarized_turn_count", 0) or 0)
            if start_idx >= len(turns):
                return None
            slice_turns = turns[start_idx:]
            filtered = filter_turns_for_memory_extraction(slice_turns)
            if not filtered:
                # Mark cleanly as no longer pending if session already ended
                if float(sess.get("ended_at", 0.0) or 0.0) > 0:
                    sess["needs_summary"] = False
                    sess["summarized_turn_count"] = len(turns)
                    self._save_bucket_unlocked(clean_cid, clean_track, b)
                return None
            # Re-index turn_index to absolute session turn indices for accurate STT correction mapping
            abs_filtered = [{**item, "turn_index": start_idx + int(item["turn_index"])} for item in filtered]
            prompt = build_memory_extraction_prompt(
                char_data=char_data,
                track=clean_track,
                existing_confirmed=b.get("confirmed_memories") or [],
                existing_pending=b.get("pending_memories") or [],
                existing_threads=b.get("open_threads") or [],
                filtered_turns=abs_filtered,
                sensei_profile=self.load_sensei_profile(),
            )
            return {
                "base_cid": clean_cid,
                "track": clean_track,
                "session_id": clean_sid,
                "covered_turn_count": len(turns),
                "prompt": prompt,
            }

    def apply_extraction_result(
        self,
        base_cid: str,
        track: str,
        session_id: str,
        parsed: dict[str, Any],
        covered_turn_count: int,
        snapshot_reason: str = "통화 요약 전 자동 백업",
    ) -> dict[str, int]:
        """
        Stage 3 atomic commit with automatic pre-update snapshot backup, STT typo repair overlay,
        pending->confirmed promotion, and fuzzy deduplication.
        """
        clean_cid = _validate_student_id(base_cid)
        clean_track = _validate_track(track)
        clean_sid = _validate_session_id(session_id)
        now = time.time()
        stats = {"added_confirmed": 0, "promoted": 0, "added_pending": 0, "stt_repaired": 0, "updated": 0, "removed": 0}
        with self._lock:
            b = self._load_bucket_unlocked(clean_cid, clean_track)
            sess = next((s for s in reversed(b["sessions"]) if s.get("session_id") == clean_sid), None)

            # 1. Apply contextual STT corrections non-destructively (preserving `raw_stt`)
            if sess is not None:
                turns = sess.get("turns") or []
                for corr in parsed.get("stt_corrections") or []:
                    idx = int(corr.get("turn_index", -1))
                    fixed = _scrub_str(corr.get("corrected")).strip()[:200]
                    if 0 <= idx < len(turns) and turns[idx].get("role") == "user" and fixed:
                        turns[idx]["corrected_text"] = fixed
                        stats["stt_repaired"] += 1

            skip_reason = _scrub_str(parsed.get("skip_reason")).strip()
            has_memory_mutations = bool(
                (not skip_reason)
                and (
                    parsed.get("add_confirmed")
                    or parsed.get("promote_pending_ids")
                    or parsed.get("update_confirmed")
                    or parsed.get("remove_confirmed_ids")
                    or parsed.get("add_pending")
                    or (parsed.get("open_threads") is not None and parsed.get("open_threads") != b.get("open_threads"))
                )
            )

            if has_memory_mutations:
                # Save pre-update snapshot for 1-click rollback
                b["snapshots"].append(
                    {
                        "snapshot_id": f"snap_{int(now)}_{uuid.uuid4().hex[:4]}",
                        "created_at": now,
                        "reason": snapshot_reason,
                        "confirmed_memories": deepcopy(b["confirmed_memories"]),
                        "pending_memories": deepcopy(b["pending_memories"]),
                        "open_threads": deepcopy(b["open_threads"]),
                    }
                )
                if len(b["snapshots"]) > MAX_SNAPSHOTS:
                    b["snapshots"] = b["snapshots"][-MAX_SNAPSHOTS:]

                # Remove requested IDs
                remove_ids = set(parsed.get("remove_confirmed_ids") or [])
                if remove_ids:
                    before = len(b["confirmed_memories"])
                    b["confirmed_memories"] = [m for m in b["confirmed_memories"] if m.get("id") not in remove_ids]
                    stats["removed"] = before - len(b["confirmed_memories"])

                # Update existing confirmed items
                upd_map = {str(u["id"]): _scrub_str(u["text"]).strip()[:120] for u in (parsed.get("update_confirmed") or [])}
                for m in b["confirmed_memories"]:
                    mid = str(m.get("id") or "")
                    if mid in upd_map and upd_map[mid]:
                        m["text"] = upd_map[mid]
                        m["updated_at"] = now
                        stats["updated"] += 1

                # Promote confirmed pending items
                promote_ids = set(parsed.get("promote_pending_ids") or [])
                if promote_ids:
                    still_pending: list[dict[str, Any]] = []
                    for pm in b["pending_memories"]:
                        if pm.get("id") in promote_ids:
                            txt = _scrub_str(pm.get("text")).strip()[:120]
                            if txt and not any(_is_near_duplicate(txt, cm.get("text", "")) for cm in b["confirmed_memories"]):
                                b["confirmed_memories"].append(
                                    {
                                        "id": f"mem_{uuid.uuid4().hex[:8]}",
                                        "category": pm.get("category", "fact"),
                                        "text": txt,
                                        "source": "auto",
                                        "mentions": 2,
                                        "created_at": float(pm.get("created_at") or now),
                                        "updated_at": now,
                                    }
                                )
                                stats["promoted"] += 1
                        else:
                            still_pending.append(pm)
                    b["pending_memories"] = still_pending

                # Add newly confirmed items (with fuzzy dedupe; bump mentions if already present)
                for item in parsed.get("add_confirmed") or []:
                    txt = _scrub_str(item.get("text")).strip()[:120]
                    cat = str(item.get("category") or "fact")
                    if not txt:
                        continue
                    dup_cm = next((cm for cm in b["confirmed_memories"] if _is_near_duplicate(txt, cm.get("text", ""))), None)
                    if dup_cm is not None:
                        dup_cm["mentions"] = int(dup_cm.get("mentions", 1) or 1) + 1
                        dup_cm["updated_at"] = now
                        continue
                    # Also auto-clean matching pending item if confirmed directly
                    b["pending_memories"] = [
                        pm for pm in b["pending_memories"] if not _is_near_duplicate(txt, pm.get("text", ""))
                    ]
                    b["confirmed_memories"].append(
                        {
                            "id": f"mem_{uuid.uuid4().hex[:8]}",
                            "category": cat,
                            "text": txt,
                            "source": "auto",
                            "mentions": 1,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    stats["added_confirmed"] += 1

                # Add pending items (held for confirmation)
                for item in parsed.get("add_pending") or []:
                    txt = _scrub_str(item.get("text")).strip()[:120]
                    cat = str(item.get("category") or "event")
                    reason = _scrub_str(item.get("reason") or "문맥 확인 필요").strip()[:80]
                    if not txt:
                        continue
                    if any(_is_near_duplicate(txt, cm.get("text", "")) for cm in b["confirmed_memories"]):
                        continue
                    if any(_is_near_duplicate(txt, pm.get("text", "")) for pm in b["pending_memories"]):
                        continue
                    b["pending_memories"].append(
                        {
                            "id": f"pnd_{uuid.uuid4().hex[:8]}",
                            "category": cat,
                            "text": txt,
                            "reason": reason,
                            "created_at": now,
                        }
                    )
                    stats["added_pending"] += 1

                # Update open threads if provided
                if parsed.get("open_threads") is not None:
                    b["open_threads"] = [
                        _scrub_str(t).strip()[:100]
                        for t in list(parsed["open_threads"])[:MAX_OPEN_THREADS]
                        if _scrub_str(t).strip()
                    ]

                # Respect caps (preserve manual & most recently refreshed confirmed items)
                if len(b["confirmed_memories"]) > MAX_CONFIRMED_MEMORIES:
                    b["confirmed_memories"].sort(
                        key=lambda m: (
                            1 if m.get("source") == "manual" else 0,
                            int(m.get("mentions", 1) or 1),
                            float(m.get("updated_at", 0.0) or 0.0),
                        ),
                        reverse=True,
                    )
                    b["confirmed_memories"] = b["confirmed_memories"][:MAX_CONFIRMED_MEMORIES]
                if len(b["pending_memories"]) > MAX_PENDING_MEMORIES:
                    b["pending_memories"] = b["pending_memories"][-MAX_PENDING_MEMORIES:]

            if sess is not None:
                sess["summarized_turn_count"] = max(int(sess.get("summarized_turn_count", 0) or 0), int(covered_turn_count))
                sess["needs_summary"] = False

            self._save_bucket_unlocked(clean_cid, clean_track, b)
            return stats

    def rollback_to_snapshot(
        self,
        base_cid: str,
        track: str,
        snapshot_id: str | None = None,
    ) -> tuple[bool, str]:
        """Restore `confirmed_memories`, `pending_memories`, and `open_threads` to a saved snapshot."""
        with self._lock:
            b = self._load_bucket_unlocked(base_cid, track)
            snaps = b.get("snapshots") or []
            if not snaps:
                return False, "되돌릴 수 있는 스냅샷이 없습니다."
            target_idx = len(snaps) - 1
            if snapshot_id:
                found = next((i for i, s in enumerate(snaps) if s.get("snapshot_id") == snapshot_id), -1)
                if found < 0:
                    return False, "선택한 스냅샷을 찾을 수 없습니다."
                target_idx = found
            snap = snaps[target_idx]
            b["confirmed_memories"] = deepcopy(snap.get("confirmed_memories") or [])
            b["pending_memories"] = deepcopy(snap.get("pending_memories") or [])
            b["open_threads"] = deepcopy(snap.get("open_threads") or [])
            # Trim snapshots newer than or equal to the restored snapshot
            b["snapshots"] = snaps[:target_idx]
            self._save_bucket_unlocked(base_cid, track, b)
            ts = float(snap.get("created_at") or 0.0)
            ts_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
            return True, f"{ts_str} 시점의 기억으로 되돌렸습니다."

    def list_pending_summary_targets(self) -> list[tuple[str, str, str]]:
        """Scan all student buckets for crash-interrupted sessions still awaiting summary."""
        out: list[tuple[str, str, str]] = []
        with self._lock:
            if not self.students_dir.is_dir():
                return out
            for fp in sorted(self.students_dir.glob("*.json")):
                try:
                    _ensure_within_dir(self.students_dir, fp)
                    raw = _safe_read_json(fp, max_bytes=MAX_MEMORY_JSON_BYTES)
                    if not isinstance(raw, dict):
                        continue
                    bcid = _validate_student_id(str(raw.get("base_cid") or ""))
                    tr = _validate_track(str(raw.get("track") or "official"))
                    for sess in raw.get("sessions") or []:
                        if isinstance(sess, dict) and bool(sess.get("needs_summary")) and sess.get("session_id"):
                            sid = _validate_session_id(str(sess["session_id"]))
                            out.append((bcid, tr, sid))
                except Exception:
                    continue
        return out

    # ------------------------------------------------------------------
    # Manual Memory CRUD (Memory Note Dialog)
    # ------------------------------------------------------------------

    def add_manual_memory(self, base_cid: str, track: str, text: str, category: str = "fact") -> dict[str, Any] | None:
        clean = _scrub_str(text).strip()[:120]
        if not clean:
            return None
        cat = category if category in MEMORY_CATEGORIES else "fact"
        now = time.time()
        with self._lock:
            b = self._load_bucket_unlocked(base_cid, track)
            item = {
                "id": f"mem_{uuid.uuid4().hex[:8]}",
                "category": cat,
                "text": clean,
                "source": "manual",
                "mentions": 1,
                "created_at": now,
                "updated_at": now,
            }
            b["confirmed_memories"].insert(0, item)
            if len(b["confirmed_memories"]) > MAX_CONFIRMED_MEMORIES:
                b["confirmed_memories"] = b["confirmed_memories"][:MAX_CONFIRMED_MEMORIES]
            self._save_bucket_unlocked(base_cid, track, b)
            return deepcopy(item)

    def update_memory_text(self, base_cid: str, track: str, mem_id: str, new_text: str, category: str | None = None) -> bool:
        clean = _scrub_str(new_text).strip()[:120]
        if not clean or not mem_id:
            return False
        now = time.time()
        with self._lock:
            b = self._load_bucket_unlocked(base_cid, track)
            for lst in (b["confirmed_memories"], b["pending_memories"]):
                for m in lst:
                    if m.get("id") == mem_id:
                        m["text"] = clean
                        if category and category in MEMORY_CATEGORIES:
                            m["category"] = category
                        m["updated_at"] = now
                        self._save_bucket_unlocked(base_cid, track, b)
                        return True
            return False

    def delete_memory_item(self, base_cid: str, track: str, mem_id: str) -> bool:
        if not mem_id:
            return False
        with self._lock:
            b = self._load_bucket_unlocked(base_cid, track)
            c_before = len(b["confirmed_memories"])
            p_before = len(b["pending_memories"])
            b["confirmed_memories"] = [m for m in b["confirmed_memories"] if m.get("id") != mem_id]
            b["pending_memories"] = [m for m in b["pending_memories"] if m.get("id") != mem_id]
            if len(b["confirmed_memories"]) != c_before or len(b["pending_memories"]) != p_before:
                self._save_bucket_unlocked(base_cid, track, b)
                return True
            return False

    def approve_pending_memory(self, base_cid: str, track: str, pending_id: str) -> bool:
        """Manually promote a held `pending_memories` item to `confirmed_memories`."""
        now = time.time()
        with self._lock:
            b = self._load_bucket_unlocked(base_cid, track)
            target = next((m for m in b["pending_memories"] if m.get("id") == pending_id), None)
            if target is None:
                return False
            b["pending_memories"] = [m for m in b["pending_memories"] if m.get("id") != pending_id]
            txt = _scrub_str(target.get("text")).strip()[:120]
            if txt and not any(_is_near_duplicate(txt, cm.get("text", "")) for cm in b["confirmed_memories"]):
                b["confirmed_memories"].insert(
                    0,
                    {
                        "id": f"mem_{uuid.uuid4().hex[:8]}",
                        "category": target.get("category", "fact"),
                        "text": txt,
                        "source": "manual",
                        "mentions": 2,
                        "created_at": float(target.get("created_at") or now),
                        "updated_at": now,
                    },
                )
            self._save_bucket_unlocked(base_cid, track, b)
            return True

    def update_open_threads(self, base_cid: str, track: str, threads: list[str]) -> list[str]:
        clean = []
        for t in threads or []:
            st = _scrub_str(t).strip()[:100]
            if st and st not in clean:
                clean.append(st)
        clean = clean[:MAX_OPEN_THREADS]
        with self._lock:
            b = self._load_bucket_unlocked(base_cid, track)
            b["open_threads"] = clean
            self._save_bucket_unlocked(base_cid, track, b)
            return list(clean)

    def clear_student_memory(self, base_cid: str, track: str, include_sessions: bool = False) -> None:
        """Reset a student's long-term memories (and optionally delete saved call logs too)."""
        now = time.time()
        with self._lock:
            b = self._load_bucket_unlocked(base_cid, track)
            if b["confirmed_memories"] or b["pending_memories"] or b["open_threads"]:
                b["snapshots"].append(
                    {
                        "snapshot_id": f"snap_{int(now)}_{uuid.uuid4().hex[:4]}",
                        "created_at": now,
                        "reason": "초기화 직전 자동 백업",
                        "confirmed_memories": deepcopy(b["confirmed_memories"]),
                        "pending_memories": deepcopy(b["pending_memories"]),
                        "open_threads": deepcopy(b["open_threads"]),
                    }
                )
            b["confirmed_memories"] = []
            b["pending_memories"] = []
            b["open_threads"] = []
            if include_sessions:
                b["sessions"] = []
                b["total_calls"] = 0
                b["last_called_at"] = 0.0
            self._save_bucket_unlocked(base_cid, track, b)
            if include_sessions:
                self._refresh_recent_entry_for_student_unlocked(base_cid)

    # ------------------------------------------------------------------
    # Prompt Injection Context Builder (Natural Reunion + Time Grounding)
    # ------------------------------------------------------------------

    def build_memory_context_block(
        self,
        base_cid: str,
        track: str,
        char_data: dict[str, Any] | None = None,
        max_recent_turns: int = 8,
        now_ts: float | None = None,
    ) -> tuple[str, bool]:
        """
        Build the compact memory & temporal context block injected on call start.
        Returns `(context_text, has_past_relationship)`.
        """
        with self._lock:
            sp = self.load_sensei_profile()
            b = self._load_bucket_unlocked(base_cid, track)

        now = time.time() if now_ts is None else float(now_ts)
        now_dt = datetime.datetime.fromtimestamp(now)
        clock_str, period_kr = format_time_of_day_kr(now_dt)

        confirmed = [m for m in (b.get("confirmed_memories") or []) if str(m.get("text") or "").strip()]
        threads = [str(t).strip() for t in (b.get("open_threads") or []) if str(t).strip()]
        sessions = [s for s in (b.get("sessions") or []) if s.get("turns")]

        # Collect recent clean turns from latest sessions (excluding noise)
        recent_turns: list[dict[str, Any]] = []
        for sess in reversed(sessions):
            for t in reversed(sess.get("turns") or []):
                if bool(t.get("filtered_noise")):
                    continue
                role = str(t.get("role") or "")
                if role == "user":
                    u_txt = str(t.get("corrected_text") or t.get("raw_stt") or t.get("kr_text") or t.get("jp_text") or "").strip()
                    if not u_txt or is_test_or_noise_utterance(u_txt):
                        continue
                    recent_turns.append({"role": "user", "text": u_txt})
                else:
                    a_txt = str(t.get("kr_text") or t.get("jp_text") or "").strip()
                    if not a_txt:
                        continue
                    recent_turns.append({"role": "assistant", "text": a_txt})
                if len(recent_turns) >= max_recent_turns:
                    break
            if len(recent_turns) >= max_recent_turns:
                break
        recent_turns.reverse()

        sp_lines: list[str] = []
        if sp.get("nickname"):
            sp_lines.append(f"- 선생님 호칭/별명: {sp['nickname']}")
        if sp.get("bio_notes"):
            sp_lines.append(f"- 선생님 소개/메모: {sp['bio_notes']}")
        if sp.get("preferences"):
            sp_lines.append(f"- 선생님 관심사/취향: {', '.join(sp['preferences'])}")

        has_past = bool(sessions or confirmed or threads)
        if not has_past and not sp_lines:
            return "", False

        name_kr = str((char_data or {}).get("name_kr") or base_cid).strip()
        total_calls = max(int(b.get("total_calls", 0) or 0), len(sessions))
        elapsed_str = format_elapsed_since_kr(b.get("last_called_at"), now_ts=now)

        sections: list[str] = ["[선생님과의 기억 및 시간 맥락]"]
        if has_past:
            sections.append(
                f"・현재 시각: {clock_str} ({period_kr}) / 이전 누적 통화: {total_calls}회 / 마지막 통화로부터 경과: {elapsed_str}"
            )
        else:
            sections.append(f"・현재 시각: {clock_str} ({period_kr}) / 이 학생과의 첫 통화")

        if sp_lines:
            sections.append("[선생님 기본 프로필 (공통 기억)]\n" + "\n".join(sp_lines))

        if confirmed:
            mem_lines = [
                f"- ({MEMORY_CATEGORY_LABELS.get(str(m.get('category', 'fact')), '정보')}) {m['text']}"
                for m in confirmed[:20]
            ]
            sections.append(f"[{name_kr}이(가) 기억하는 선생님과의 장기 기억]\n" + "\n".join(mem_lines))

        if threads:
            sections.append("[이어서 이야기할 수 있는 약속·화제]\n" + "\n".join(f"- {t}" for t in threads[:5]))

        if recent_turns:
            rec_lines = [
                f"- {'선생님' if rt['role'] == 'user' else name_kr}: {rt['text'][:120]}"
                for rt in recent_turns
            ]
            sections.append("[직전 통화의 마지막 대화 흐름]\n" + "\n".join(rec_lines))

        sections.append(
            "[기억 활용 원칙 — 은은하고 자연스러운 재회]\n"
            "1. 위 기억은 머릿속 자연스러운 배경 지식이다. 기억하고 있음을 과시하려고 여러 항목을 기계적으로 나열하지 마라.\n"
            "2. 첫인사에서는 현재 시각대나 며칠 만의 통화인지 정도를 캐릭터답게 자연스럽게 녹여내되, 이전 기억이나 약속은 정말 어울릴 때만 최대 1가지 정도 가볍게 꺼내거나 완전히 새로운 일상 화제로 시작해도 좋다.\n"
            "3. 대화 도중 선생님이 이전 이야기·취향·약속을 언급하면 위 기억을 바탕으로 자연스럽게 이어서 반응하라."
        )
        return "\n\n".join(sections), has_past

    # ------------------------------------------------------------------
    # JSON Backup & Restore
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_memory_item(raw_item: Any, *, pending: bool = False) -> dict[str, Any] | None:
        if not isinstance(raw_item, dict):
            return None
        txt = _scrub_str(raw_item.get("text")).strip()[:120]
        if not txt:
            return None
        cat = str(raw_item.get("category") or "fact").strip().lower()
        if cat not in MEMORY_CATEGORIES:
            cat = "fact"
        now = time.time()
        item_id = str(raw_item.get("id") or f"{'pnd' if pending else 'mem'}_{uuid.uuid4().hex[:8]}").strip()[:64]
        if pending:
            return {
                "id": item_id,
                "category": cat,
                "text": txt,
                "reason": _scrub_str(raw_item.get("reason") or "문맥 확인 필요").strip()[:80],
                "created_at": float(raw_item.get("created_at", now) or now),
            }
        src = "manual" if str(raw_item.get("source") or "") == "manual" else "auto"
        return {
            "id": item_id,
            "category": cat,
            "text": txt,
            "source": src,
            "mentions": max(1, int(raw_item.get("mentions", 1) or 1)),
            "created_at": float(raw_item.get("created_at", now) or now),
            "updated_at": float(raw_item.get("updated_at", now) or now),
        }

    @staticmethod
    def _sanitize_session_item(raw_sess: Any, default_cid: str) -> dict[str, Any] | None:
        if not isinstance(raw_sess, dict):
            return None
        raw_sid = str(raw_sess.get("session_id") or "").strip()
        if not raw_sid:
            return None
        clean_sid = _validate_session_id(raw_sid)
        raw_eid = str(raw_sess.get("entry_id") or default_cid).strip()
        try:
            clean_eid = _validate_student_id(raw_eid)
        except ValueError:
            clean_eid = default_cid
        clean_turns: list[dict[str, Any]] = []
        for t in (raw_sess.get("turns") or [])[-MAX_TURNS_PER_SESSION:]:
            if not isinstance(t, dict):
                continue
            role = "user" if str(t.get("role") or "").strip().lower() == "user" else "assistant"
            jp_t = _scrub_str(t.get("jp_text")).strip()
            kr_t = _scrub_str(t.get("kr_text")).strip()
            raw_stt = _scrub_str(t.get("raw_stt")).strip() if role == "user" else ""
            corr_t = _scrub_str(t.get("corrected_text")).strip() if role == "user" else ""
            if not jp_t and not kr_t and not raw_stt:
                continue
            clean_turns.append(
                {
                    "ts": float(t.get("ts", 0.0) or 0.0),
                    "role": role,
                    "jp_text": jp_t,
                    "kr_text": kr_t,
                    "raw_stt": raw_stt,
                    "corrected_text": corr_t,
                    "is_proactive_ping": bool(t.get("is_proactive_ping", False)),
                    "filtered_noise": bool(t.get("filtered_noise", False)),
                }
            )
        return {
            "session_id": clean_sid,
            "entry_id": clean_eid,
            "costume_label_kr": _scrub_str(raw_sess.get("costume_label_kr")).strip()[:80],
            "reply_language": "ko" if str(raw_sess.get("reply_language") or "").lower() == "ko" else "ja",
            "started_at": float(raw_sess.get("started_at", 0.0) or 0.0),
            "ended_at": float(raw_sess.get("ended_at", 0.0) or 0.0),
            "summarized_turn_count": max(0, min(len(clean_turns), int(raw_sess.get("summarized_turn_count", 0) or 0))),
            "needs_summary": bool(raw_sess.get("needs_summary", False)),
            "turns": clean_turns,
        }

    def export_backup(
        self,
        dest_path: Path,
        base_cid: str | None = None,
        track: str | None = None,
    ) -> dict[str, int]:
        """Export either all memory data or a single student bucket into a clean JSON backup."""
        dest = Path(dest_path)
        if "\x00" in str(dest):
            raise ValueError("Invalid backup destination path")
        if dest.is_symlink():
            raise ValueError("Refusing to overwrite symlinked backup file")
        with self._lock:
            payload: dict[str, Any] = {
                "backup_schema_version": MEMORY_SCHEMA_VERSION,
                "exported_at": time.time(),
                "scope": "single" if base_cid else "all",
                "sensei_profile": self.load_sensei_profile(),
                "recent_calls": self._load_recent_unlocked() if not base_cid else [],
                "buckets": {},
            }
            if base_cid:
                clean_cid = _validate_student_id(base_cid)
                tracks = [_validate_track(track)] if track else list(VALID_MEMORY_TRACKS)
                for tr in tracks:
                    p = self._bucket_path(clean_cid, tr)
                    if p.is_file() or tr == track:
                        b = self._load_bucket_unlocked(clean_cid, tr)
                        payload["buckets"][_safe_bucket_key(clean_cid, tr)] = b
            else:
                if self.students_dir.is_dir():
                    for fp in sorted(self.students_dir.glob("*.json")):
                        try:
                            _ensure_within_dir(self.students_dir, fp)
                            raw = _safe_read_json(fp, max_bytes=MAX_MEMORY_JSON_BYTES)
                            if isinstance(raw, dict):
                                payload["buckets"][fp.stem] = {k: raw[k] for k in _ALLOWED_BUCKET_KEYS if k in raw}
                        except Exception:
                            continue
            _atomic_write_json(dest, payload, harden_parent=False)
            total_sessions = sum(len(b.get("sessions") or []) for b in payload["buckets"].values())
            total_mems = sum(len(b.get("confirmed_memories") or []) for b in payload["buckets"].values())
            return {"buckets": len(payload["buckets"]), "memories": total_mems, "sessions": total_sessions}

    def import_backup(self, src_path: Path, merge: bool = True) -> dict[str, int]:
        """Restore memory backup JSON into `%LOCALAPPDATA%\\VoiceCompanionData\\memory\\`."""
        src = Path(src_path)
        raw = _safe_read_json(src, max_bytes=MAX_BACKUP_JSON_BYTES)
        if not isinstance(raw, dict) or not isinstance(raw.get("buckets"), dict):
            raise ValueError("유효한 모모톡 기억 백업 JSON 파일이 아닙니다.")
        raw_buckets = raw.get("buckets") or {}
        if len(raw_buckets) > MAX_IMPORT_BUCKETS:
            raise ValueError(f"백업 파일의 학생 버킷 수가 허용 한도({MAX_IMPORT_BUCKETS})를 초과했습니다.")

        restored_buckets = 0
        restored_memories = 0
        restored_sessions = 0

        with self._lock:
            sp_in = raw.get("sensei_profile")
            if isinstance(sp_in, dict):
                if merge:
                    cur_sp = self.load_sensei_profile()
                    nick = cur_sp["nickname"] or _scrub_str(sp_in.get("nickname")).strip()
                    bio = cur_sp["bio_notes"] or _scrub_str(sp_in.get("bio_notes")).strip()
                    in_prefs = sp_in.get("preferences") if isinstance(sp_in.get("preferences"), list) else []
                    prefs = list(dict.fromkeys([*(cur_sp.get("preferences") or []), *in_prefs]))
                    self.save_sensei_profile(nickname=nick, bio_notes=bio, preferences=prefs)
                else:
                    in_prefs = sp_in.get("preferences") if isinstance(sp_in.get("preferences"), list) else []
                    self.save_sensei_profile(
                        nickname=_scrub_str(sp_in.get("nickname")),
                        bio_notes=_scrub_str(sp_in.get("bio_notes")),
                        preferences=list(in_prefs),
                    )

            touched_cids: set[str] = set()
            for _key, b_in in raw_buckets.items():
                if not isinstance(b_in, dict):
                    continue
                raw_bcid = str(b_in.get("base_cid") or "").strip()
                raw_tr = str(b_in.get("track") or "official").strip()
                if not raw_bcid:
                    continue
                bcid = _validate_student_id(raw_bcid)
                tr = _validate_track(raw_tr)
                touched_cids.add(bcid)

                clean_confirmed = [
                    item
                    for cm in (b_in.get("confirmed_memories") or [])
                    if (item := self._sanitize_memory_item(cm, pending=False)) is not None
                ][:MAX_CONFIRMED_MEMORIES]
                clean_pending = [
                    item
                    for pm in (b_in.get("pending_memories") or [])
                    if (item := self._sanitize_memory_item(pm, pending=True)) is not None
                ][:MAX_PENDING_MEMORIES]
                clean_threads = [
                    st
                    for th in (b_in.get("open_threads") or [])
                    if (st := _scrub_str(th).strip()[:100])
                ][:MAX_OPEN_THREADS]
                clean_sessions = [
                    sess
                    for raw_s in (b_in.get("sessions") or [])
                    if (sess := self._sanitize_session_item(raw_s, bcid)) is not None
                ][-MAX_SESSIONS_PER_BUCKET:]

                if not merge:
                    target_b = self._default_bucket(bcid, tr)
                    target_b["confirmed_memories"] = clean_confirmed
                    target_b["pending_memories"] = clean_pending
                    target_b["open_threads"] = clean_threads
                    target_b["sessions"] = clean_sessions
                    target_b["total_calls"] = max(int(b_in.get("total_calls", 0) or 0), len(clean_sessions))
                    target_b["last_called_at"] = float(b_in.get("last_called_at", 0.0) or 0.0)
                    self._save_bucket_unlocked(bcid, tr, target_b)
                    restored_buckets += 1
                    restored_memories += len(clean_confirmed)
                    restored_sessions += len(clean_sessions)
                else:
                    cur_b = self._load_bucket_unlocked(bcid, tr)
                    for cm in clean_confirmed:
                        if not any(_is_near_duplicate(cm["text"], x.get("text", "")) for x in cur_b["confirmed_memories"]):
                            cur_b["confirmed_memories"].append(cm)
                            restored_memories += 1
                    cur_b["confirmed_memories"] = cur_b["confirmed_memories"][:MAX_CONFIRMED_MEMORIES]
                    for pm in clean_pending:
                        if not any(_is_near_duplicate(pm["text"], x.get("text", "")) for x in cur_b["pending_memories"]):
                            cur_b["pending_memories"].append(pm)
                    cur_b["pending_memories"] = cur_b["pending_memories"][-MAX_PENDING_MEMORIES:]
                    for st in clean_threads:
                        if not any(_is_near_duplicate(st, x) for x in cur_b["open_threads"]):
                            cur_b["open_threads"].append(st)
                    cur_b["open_threads"] = cur_b["open_threads"][:MAX_OPEN_THREADS]

                    existing_sids = {str(s.get("session_id") or "") for s in cur_b["sessions"]}
                    for sess in clean_sessions:
                        if sess["session_id"] not in existing_sids:
                            cur_b["sessions"].append(sess)
                            existing_sids.add(sess["session_id"])
                            restored_sessions += 1
                    cur_b["sessions"].sort(key=lambda s: float(s.get("started_at", 0.0) or 0.0))
                    cur_b["sessions"] = cur_b["sessions"][-MAX_SESSIONS_PER_BUCKET:]
                    cur_b["total_calls"] = max(int(cur_b.get("total_calls", 0) or 0), len(cur_b["sessions"]))
                    cur_b["last_called_at"] = max(
                        float(cur_b.get("last_called_at", 0.0) or 0.0),
                        float(b_in.get("last_called_at", 0.0) or 0.0),
                    )
                    self._save_bucket_unlocked(bcid, tr, cur_b)
                    restored_buckets += 1

            for bcid in touched_cids:
                self._refresh_recent_entry_for_student_unlocked(bcid)

        return {"buckets": restored_buckets, "memories": restored_memories, "sessions": restored_sessions}
