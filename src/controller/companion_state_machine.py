"""
5-State Machine for Bidirectional Silence Proactive Companion Mode
------------------------------------------------------------------
Strictly separates the 5 lifecycle states to prevent race conditions or audio feedback loops:
  - IDLE      : 대기 중 (the ONLY state where the bidirectional silence timer advances)
  - LISTENING : 사용자 말하기 (mic VAD detected speech; immediately resets silence timer)
  - THINKING  : 제미니 응답 중 (waiting for / streaming Gemini response)
  - SPEAKING  : Fish Audio 재생 중 (playing character TTS PCM audio)
  - AFK       : 자리 비움 (entered after consecutive unanswered proactive pings; paused until user speaks)
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class CompanionState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    AFK = "AFK"


STATE_KR_LABELS: dict[CompanionState, str] = {
    CompanionState.IDLE: "대기 중 (IDLE)",
    CompanionState.LISTENING: "사용자 말하기 (LISTENING)",
    CompanionState.THINKING: "제미니 응답 중 (THINKING)",
    CompanionState.SPEAKING: "Fish Audio 재생 중 (SPEAKING)",
    CompanionState.AFK: "자리 비움 (AFK)",
}


@dataclass
class CompanionStatus:
    state: CompanionState
    active: bool
    proactive_enabled: bool
    elapsed_idle_sec: float
    target_idle_sec: float
    consecutive_pings: int
    max_pings: int

    @property
    def kr_label(self) -> str:
        return STATE_KR_LABELS.get(self.state, self.state.value)


class CompanionStateMachine:
    """Thread-safe 5-state machine governing bidirectional silence proactive pings."""

    def __init__(
        self,
        proactive_enabled: bool = True,
        min_idle_sec: float = 45.0,
        max_idle_sec: float = 75.0,
        max_consecutive_pings: int = 2,
        rng: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._lock = threading.Lock()
        self._rng = rng

        self.proactive_enabled = bool(proactive_enabled)
        self.min_idle_sec = max(5.0, float(min_idle_sec))
        self.max_idle_sec = max(self.min_idle_sec, float(max_idle_sec))
        self.max_pings = max(1, int(max_consecutive_pings))

        self.active = False
        self.state = CompanionState.IDLE
        self.consecutive_pings = 0
        now = time.monotonic()
        self.idle_started_ts = now
        self.target_idle_sec = self._sample_target_locked()
        self._pre_listen_state = CompanionState.IDLE
        self._current_turn_is_ping = False
        self._turn_seq = 0

    @property
    def current_turn_seq(self) -> int:
        with self._lock:
            return self._turn_seq

    def _sample_target_locked(self) -> float:
        lo = min(self.min_idle_sec, self.max_idle_sec)
        hi = max(self.min_idle_sec, self.max_idle_sec)
        if abs(hi - lo) < 1e-6:
            return lo
        return float(self._rng(lo, hi))

    def _enter_idle_locked(self, ts: float) -> None:
        self.state = CompanionState.IDLE
        self.idle_started_ts = ts
        self.target_idle_sec = self._sample_target_locked()
        self._current_turn_is_ping = False

    def configure(
        self,
        proactive_enabled: bool | None = None,
        min_idle_sec: float | None = None,
        max_idle_sec: float | None = None,
        max_consecutive_pings: int | None = None,
        now: float | None = None,
    ) -> None:
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            if proactive_enabled is not None:
                self.proactive_enabled = bool(proactive_enabled)
            if min_idle_sec is not None:
                self.min_idle_sec = max(5.0, float(min_idle_sec))
            if max_idle_sec is not None:
                self.max_idle_sec = max(self.min_idle_sec, float(max_idle_sec))
            else:
                self.max_idle_sec = max(self.min_idle_sec, self.max_idle_sec)
            if max_consecutive_pings is not None:
                self.max_pings = max(1, int(max_consecutive_pings))
            if self.state == CompanionState.IDLE:
                self.target_idle_sec = self._sample_target_locked()
                if ts > self.idle_started_ts:
                    # Keep elapsed timer valid if user toggled settings
                    self.idle_started_ts = min(self.idle_started_ts, ts)

    def start_session(self, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            self.active = True
            self.consecutive_pings = 0
            self._pre_listen_state = CompanionState.IDLE
            self._enter_idle_locked(ts)

    def stop_session(self, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            self.active = False
            self.consecutive_pings = 0
            self._pre_listen_state = CompanionState.IDLE
            self._enter_idle_locked(ts)

    def on_opening_greeting_started(self, now: float | None = None) -> None:
        with self._lock:
            if not self.active:
                return
            self._turn_seq += 1
            self._current_turn_is_ping = False
            self.consecutive_pings = 0
            self.state = CompanionState.THINKING

    def on_mic_voice_start(self, now: float | None = None) -> bool:
        """
        Called immediately when local microphone VAD detects user speech onset.
        Resets silence timer to 0s and moves IDLE/AFK/SPEAKING/THINKING -> LISTENING
        to support full-duplex user barge-in.
        """
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            if not self.active:
                return False
            if self.state == CompanionState.LISTENING:
                self.idle_started_ts = ts
                return False
            if self.state not in (
                CompanionState.IDLE,
                CompanionState.AFK,
                CompanionState.SPEAKING,
                CompanionState.THINKING,
            ):
                return False
            self._pre_listen_state = self.state
            self.idle_started_ts = ts
            self.state = CompanionState.LISTENING
            return True

    def on_stt_empty(self, now: float | None = None) -> None:
        """Called when a VAD capture turned out to be noise/silence or speaker echo."""
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            if not self.active or self.state != CompanionState.LISTENING:
                return
            if self._pre_listen_state in (
                CompanionState.AFK,
                CompanionState.SPEAKING,
                CompanionState.THINKING,
            ):
                self.state = self._pre_listen_state
            else:
                self._enter_idle_locked(ts)

    def on_user_speech_confirmed(self, now: float | None = None) -> bool:
        """Called when valid user speech is transcribed and sent to Gemini."""
        with self._lock:
            if not self.active:
                return False
            self._turn_seq += 1
            self.consecutive_pings = 0
            self._current_turn_is_ping = False
            self._pre_listen_state = CompanionState.IDLE
            self.state = CompanionState.THINKING
            return True

    def trigger_manual_ping(self, now: float | None = None) -> bool:
        """Manually trigger a proactive ping turn from IDLE or AFK."""
        with self._lock:
            if not self.active:
                return False
            if self.state not in (CompanionState.IDLE, CompanionState.AFK):
                return False
            self._turn_seq += 1
            self._current_turn_is_ping = True
            self.state = CompanionState.THINKING
            return True

    def on_speaking_started(self, now: float | None = None) -> None:
        with self._lock:
            if not self.active:
                return
            if self.state == CompanionState.THINKING:
                self.state = CompanionState.SPEAKING
            elif self.state == CompanionState.LISTENING and self._pre_listen_state == CompanionState.THINKING:
                self._pre_listen_state = CompanionState.SPEAKING

    def on_turn_finished(
        self,
        now: float | None = None,
        expected_turn_seq: int | None = None,
    ) -> CompanionState:
        """
        Called after Gemini finishes generating AND Fish Audio finishes playing all chunks.
        Transitions to AFK if this turn was a proactive ping and consecutive_pings >= max_pings,
        otherwise returns to IDLE and starts a fresh random silence countdown.
        Guards against stale interrupted turns via `expected_turn_seq` and preserves in-progress LISTENING.
        """
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            if expected_turn_seq is not None and expected_turn_seq != self._turn_seq:
                return self.state
            if not self.active:
                self._enter_idle_locked(ts)
                return self.state
            post_state = (
                CompanionState.AFK
                if (self._current_turn_is_ping and self.consecutive_pings >= self.max_pings)
                else CompanionState.IDLE
            )
            if self.state == CompanionState.LISTENING:
                self._current_turn_is_ping = False
                self._pre_listen_state = post_state
                return self.state
            if post_state == CompanionState.AFK:
                self._current_turn_is_ping = False
                self.state = CompanionState.AFK
                return self.state
            self._enter_idle_locked(ts)
            return self.state

    def tick(self, now: float | None = None) -> bool:
        """
        Advance the silence watchdog. Returns True ONLY when IDLE reaches target_idle_sec
        and the caller should immediately send '[IDLE_PING]' to Gemini.
        """
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            if not self.active or not self.proactive_enabled:
                return False
            if self.state != CompanionState.IDLE:
                return False
            if (ts - self.idle_started_ts) < self.target_idle_sec:
                return False
            if self.consecutive_pings >= self.max_pings:
                self.state = CompanionState.AFK
                return False

            self._turn_seq += 1
            self.consecutive_pings += 1
            self._current_turn_is_ping = True
            self.state = CompanionState.THINKING
            return True

    def get_status(self, now: float | None = None) -> CompanionStatus:
        ts = time.monotonic() if now is None else float(now)
        with self._lock:
            elapsed = max(0.0, ts - self.idle_started_ts) if (self.active and self.state == CompanionState.IDLE) else 0.0
            return CompanionStatus(
                state=self.state,
                active=self.active,
                proactive_enabled=self.proactive_enabled,
                elapsed_idle_sec=elapsed,
                target_idle_sec=self.target_idle_sec,
                consecutive_pings=self.consecutive_pings,
                max_pings=self.max_pings,
            )
