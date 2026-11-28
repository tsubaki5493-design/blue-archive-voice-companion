"""
MomoTalk In-App Full-Automation Dialog for Blue Archive -> Fish Audio Batch Voice Cloning
-----------------------------------------------------------------------------------------
Lets users run both stages directly from the MomoTalk UI with zero command-line typing:
  - Stage 1 (Pre-payment, $0): Pre-extract all JP + KR studio reference WAVs from Steam
  - Sync: Pull existing models already on the user's Fish Audio account into config.json
  - Stage 2 (Post-payment): Resumable auto-upload with live progress bar, safe Stop,
    visibility selector (private/unlist/public), and real-time MomoTalk pink heart updates!
"""

from __future__ import annotations

import copy
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from src.controller.fish_batch_builder import (
    BatchProgress,
    _redact_secrets,
    run_fish_batch_pipeline,
    scan_voiced_roster,
)
from src.extractor.build_fish_reference import (
    DEFAULT_QUALITY_PROFILE,
    REFERENCE_QUALITY_PROFILES,
)
from src.utils.ui_scroll import (
    ScrollableFrame,
    bind_responsive_wrap,
    fit_window_to_screen,
    make_pinned_footer,
)

_PROFILE_CHOICES: list[tuple[str, str]] = [
    ("plus200", str(REFERENCE_QUALITY_PROFILES["plus200"]["label"])),
    ("pro85", str(REFERENCE_QUALITY_PROFILES["pro85"]["label"])),
    ("standard45", str(REFERENCE_QUALITY_PROFILES["standard45"]["label"])),
]
_PROF_LABEL_TO_CODE = {lbl: code for code, lbl in _PROFILE_CHOICES}

_VISIBILITY_CHOICES: list[tuple[str, str]] = [
    ("private", "🔒 비공개 private (Pro 무제한 · Plus 10개 한도 · 추천)"),
    ("unlist", "🔗 링크공개 unlist (목록 비노출 · 링크 보유자 전용)"),
    ("public", "🌐 공개 public (Plus 요금제 무제한 · 초상화 자동 첨부)"),
]
_VIS_LABEL_TO_CODE = {lbl: code for code, lbl in _VISIBILITY_CHOICES}

_LANG_CHOICES: list[tuple[tuple[str, ...], str]] = [
    (("JP", "KR"), "🇯🇵+🇰🇷 일본어 + 한국어 전체 (권장)"),
    (("JP",), "🇯🇵 일본어만 전체"),
    (("KR",), "🇰🇷 한국어만 전체"),
]
_LANG_LABEL_TO_TUPLE = {lbl: tup for tup, lbl in _LANG_CHOICES}

_ACTIVE_BATCH_LOCK = threading.Lock()
_ACTIVE_BATCH_RUNNING = False


def is_batch_worker_active() -> bool:
    """Return True if a background Fish Audio batch pipeline worker is currently running."""
    with _ACTIVE_BATCH_LOCK:
        return _ACTIVE_BATCH_RUNNING


class FishBatchDialog(tk.Toplevel):
    """MomoTalk modal window controlling pre-extraction, account sync, and full-auto upload."""

    def __init__(
        self,
        parent: tk.Misc,
        cfg: dict[str, Any],
        characters: dict[str, dict[str, Any]],
        api_key_getter: Callable[[], str] | None = None,
        on_model_linked: Callable[[str, str, str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        persist_voice_link_cb: Callable[[str, str, str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg_lock = threading.Lock()
        # Keep an isolated snapshot copy so background worker never mutates live app.cfg
        self.cfg = copy.deepcopy(cfg)
        self.characters = characters
        self._api_key_getter = api_key_getter
        self._on_model_linked = on_model_linked
        self._on_finished = on_finished
        self._persist_voice_link_cb = persist_voice_link_cb

        self._stop_event = threading.Event()
        self._running = False
        self._closing = False
        self._active_api_key: str = ""
        self._scan_cache: dict[str, dict[str, Any]] = {}
        self._options_expanded = True
        self._log_line_count = 0
        self._ui_dispatch_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._ui_dispatch_after_id: str | None = None
        self._start_ui_dispatch_pump()

        self.title("MomoTalk Live — 전 캐릭터 Fish Audio 일괄 자동화")
        self.configure(bg="#F6F8FB")
        fit_window_to_screen(
            self,
            preferred_w=820,
            preferred_h=720,
            min_w=680,
            min_h=460,
            center_on=parent,
        )
        self.transient(parent)
        try:
            self.grab_set()
        except tk.TclError:
            pass

        self._active_api_key = self._current_api_key()
        self._build_ui()
        self._refresh_inventory_banner(force_rescan=True)
        self.bind("<Escape>", self._on_escape_key)
        self.bind("<F5>", self._on_f5_refresh)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _start_ui_dispatch_pump(self) -> None:
        """Start a single Tk main-thread pump draining worker-thread UI callbacks safely."""
        if self._closing or self._ui_dispatch_after_id is not None:
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
        if self._closing:
            return
        try:
            if not self.winfo_exists():
                self._closing = True
                return
        except Exception:
            self._closing = True
            return

        while True:
            if self._closing:
                return
            try:
                if not self.winfo_exists():
                    self._closing = True
                    return
            except Exception:
                self._closing = True
                return
            try:
                cb = self._ui_dispatch_queue.get_nowait()
            except queue.Empty:
                break
            try:
                cb()
            except Exception:
                pass

        if not self._closing:
            try:
                if self.winfo_exists():
                    self._ui_dispatch_after_id = self.after(30, self._drain_ui_dispatch_queue)
            except Exception:
                self._ui_dispatch_after_id = None

    def _enqueue_ui(self, callback: Callable[[], None]) -> None:
        """Thread-safe worker-to-Tk dispatch: never calls `self.after()` off the Tk main thread."""
        if self._closing:
            return
        if threading.current_thread() is threading.main_thread():
            try:
                if not self._closing and self.winfo_exists():
                    callback()
            except Exception:
                pass
            return
        try:
            self._ui_dispatch_queue.put(callback)
        except Exception:
            pass

    def _current_api_key(self) -> str:
        if callable(self._api_key_getter):
            val = str(self._api_key_getter() or "").strip()
            if val:
                return val
        return str(self.cfg.get("fish_api_key") or "").strip()

    def _build_ui(self) -> None:
        # 1. Pinned Top Header (pack right controls BEFORE expanding title so they never squeeze)
        hdr = tk.Frame(self, bg="#FB7390", padx=16, pady=10)
        hdr.pack(side="top", fill="x")

        hdr_right = tk.Frame(hdr, bg="#FB7390")
        hdr_right.pack(side="right", padx=(8, 0))
        self.lbl_api_badge = tk.Label(
            hdr_right,
            text="",
            bg="#FFF1F2",
            fg="#9F1239",
            font=("Malgun Gothic", 8, "bold"),
            padx=8,
            pady=2,
        )
        self.lbl_api_badge.pack(side="left", padx=(0, 6))
        self.btn_refresh_inv = tk.Button(
            hdr_right,
            text="🔄 새로고침",
            command=self._on_manual_refresh,
            bg="#E45C7A",
            fg="#FFFFFF",
            activebackground="#BE185D",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        )
        self.btn_refresh_inv.pack(side="left")

        tk.Label(
            hdr,
            text="🚀 전 캐릭터 Fish Audio 보이스 완전 자동화 (JP / KR)",
            bg="#FB7390",
            fg="#FFFFFF",
            font=("Malgun Gothic", 11, "bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        # 2. Pinned Bottom Footer (Progress + Primary Controls — ALWAYS reachable)
        footer = make_pinned_footer(
            self,
            bg="#FFFFFF",
            border_color="#CBD5E1",
            padx=14,
            pady=(8, 10),
            separator=True,
        )

        prog_row = tk.Frame(footer, bg="#FFFFFF")
        prog_row.pack(fill="x", pady=(0, 8))

        bar_line = tk.Frame(prog_row, bg="#FFFFFF")
        bar_line.pack(fill="x")
        self.prog_var = tk.DoubleVar(value=0.0)
        self.progressbar = ttk.Progressbar(bar_line, variable=self.prog_var, maximum=100.0)
        self.progressbar.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.lbl_pct = tk.Label(
            bar_line,
            text="0%",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            width=5,
            anchor="e",
        )
        self.lbl_pct.pack(side="right")

        self.lbl_progress = tk.Label(
            prog_row,
            text="대기 중 — 1단계 사전 추출은 무료이며 API 키 없이 바로 시작할 수 있습니다. (F5 새로고침 · Esc 닫기)",
            bg="#FFFFFF",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_progress.pack(fill="x", pady=(4, 0))
        bind_responsive_wrap(self.lbl_progress, container=prog_row, pad=12, min_wrap=260)

        act_row = tk.Frame(footer, bg="#FFFFFF")
        act_row.pack(fill="x")

        self.btn_extract = tk.Button(
            act_row,
            text="1️⃣ 스팀 사전 추출 (무료)",
            command=lambda: self._start_run("extract_only"),
            bg="#0EA5E9",
            fg="#FFFFFF",
            activebackground="#0284C7",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=9,
            pady=6,
            cursor="hand2",
        )
        self.btn_extract.pack(side="left", padx=(0, 6))

        self.btn_sync = tk.Button(
            act_row,
            text="🔄 계정 동기화",
            command=lambda: self._start_run("sync_only"),
            bg="#475569",
            fg="#FFFFFF",
            activebackground="#334155",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=9,
            pady=6,
            cursor="hand2",
        )
        self.btn_sync.pack(side="left", padx=(0, 6))

        self.btn_upload = tk.Button(
            act_row,
            text="2️⃣ 자동 업로드 시작",
            command=lambda: self._start_run("upload"),
            bg="#FB7390",
            fg="#FFFFFF",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.btn_upload.pack(side="left", padx=(0, 6))

        self.btn_close = tk.Button(
            act_row,
            text="닫기",
            command=self._on_close,
            bg="#334155",
            fg="#FFFFFF",
            activebackground="#1E293B",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.btn_close.pack(side="right")

        self.btn_stop = tk.Button(
            act_row,
            text="⏹ 안전 중지",
            command=self._request_stop,
            state="disabled",
            bg="#EF4444",
            fg="#FFFFFF",
            activebackground="#DC2626",
            activeforeground="#FFFFFF",
            disabledforeground="#FCA5A5",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=9,
            pady=6,
            cursor="hand2",
        )
        self.btn_stop.pack(side="right", padx=(0, 6))

        # 3. Scrollable Main Content Body
        self.scroll_body = ScrollableFrame(
            self,
            bg="#F6F8FB",
            padx=14,
            pady=10,
            bottom_pad=24,
            auto_scroll_on_focus=True,
        )
        self.scroll_body.pack(side="top", fill="both", expand=True)
        body = self.scroll_body.inner

        # 3-A. Inventory summary card
        inv_card = tk.Frame(
            body,
            bg="#FFFFFF",
            highlightbackground="#E2E8F0",
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        inv_card.pack(fill="x", pady=(0, 10))

        self.lbl_inventory = tk.Label(
            inv_card,
            text="스팀 음성 팩 현황을 스캔하는 중...",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_inventory.pack(fill="x")
        bind_responsive_wrap(self.lbl_inventory, container=inv_card, pad=28, min_wrap=240)

        # Compact 2-column JP / KR status badges
        stat_grid = tk.Frame(inv_card, bg="#FFFFFF")
        stat_grid.pack(fill="x", pady=(6, 4))
        stat_grid.columnconfigure(0, weight=1)
        stat_grid.columnconfigure(1, weight=1)

        jp_pill = tk.Frame(stat_grid, bg="#F8FAFC", highlightbackground="#E2E8F0", highlightthickness=1, padx=8, pady=5)
        jp_pill.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.lbl_stat_jp = tk.Label(
            jp_pill,
            text="🇯🇵 일본어(JP): 확인 중...",
            bg="#F8FAFC",
            fg="#334155",
            font=("Malgun Gothic", 8, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_stat_jp.pack(fill="x")
        bind_responsive_wrap(self.lbl_stat_jp, container=jp_pill, pad=18, min_wrap=160)

        kr_pill = tk.Frame(stat_grid, bg="#F8FAFC", highlightbackground="#E2E8F0", highlightthickness=1, padx=8, pady=5)
        kr_pill.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.lbl_stat_kr = tk.Label(
            kr_pill,
            text="🇰🇷 한국어(KR): 확인 중...",
            bg="#F8FAFC",
            fg="#334155",
            font=("Malgun Gothic", 8, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_stat_kr.pack(fill="x")
        bind_responsive_wrap(self.lbl_stat_kr, container=kr_pill, pad=18, min_wrap=160)

        self.lbl_tip = tk.Label(
            inv_card,
            text=(
                "💡 팁: 결제 전에 [1️⃣ 스팀 사전 추출]을 미리 돌려두면, "
                "Fish Audio 요금제 결제 후 [2️⃣ 자동 업로드 시작]만 눌러 바로 💗 모모톡 하트가 채워집니다. "
                "(중단해도 이어서 재개되며 이미 계정에 올라간 모델은 중복 생성하지 않습니다.)"
            ),
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            justify="left",
            anchor="w",
        )
        self.lbl_tip.pack(fill="x", pady=(4, 0))
        bind_responsive_wrap(self.lbl_tip, container=inv_card, pad=28, min_wrap=240)

        # 3-B. Collapsible Options Card
        opt_card = tk.Frame(
            body,
            bg="#FFFFFF",
            highlightbackground="#E2E8F0",
            highlightthickness=1,
            padx=12,
            pady=10,
        )
        opt_card.pack(fill="x", pady=(0, 10))

        opt_hdr = tk.Frame(opt_card, bg="#FFFFFF")
        opt_hdr.pack(fill="x")
        self.btn_toggle_opts = tk.Button(
            opt_hdr,
            text="접기 ▴",
            command=self._toggle_options_card,
            bg="#F1F5F9",
            fg="#334155",
            activebackground="#E2E8F0",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        )
        self.btn_toggle_opts.pack(side="right")
        tk.Label(
            opt_hdr,
            text="⚙️ 일괄 작업 옵션",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        self.lbl_opt_summary = tk.Label(
            opt_hdr,
            text="",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            anchor="w",
        )
        self.lbl_opt_summary.pack(side="left", padx=(8, 4), fill="x", expand=True)

        self._opt_body = tk.Frame(opt_card, bg="#FFFFFF")
        self._opt_body.pack(fill="x", pady=(8, 0))

        # Row 1: Target language & Upload limit (responsive grid)
        row1 = tk.Frame(self._opt_body, bg="#FFFFFF")
        row1.pack(fill="x", pady=(0, 6))
        row1.columnconfigure(1, weight=3)
        row1.columnconfigure(3, weight=2)

        tk.Label(
            row1,
            text="대상 언어:",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            width=10,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.lang_var = tk.StringVar(value=_LANG_CHOICES[0][1])
        self.cmb_lang = ttk.Combobox(
            row1,
            textvariable=self.lang_var,
            values=[lbl for _, lbl in _LANG_CHOICES],
            state="readonly",
            width=24,
        )
        self.cmb_lang.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        self.cmb_lang.bind("<<ComboboxSelected>>", lambda _e: self._update_options_summary())

        tk.Label(
            row1,
            text="업로드 상한:",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        ).grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.limit_var = tk.StringVar(value="전체 (제한 없음)")
        self.cmb_limit = ttk.Combobox(
            row1,
            textvariable=self.limit_var,
            values=["전체 (제한 없음)", "5개 (소량 테스트)", "10개 (Plus 비공개 한도)", "30개", "50개"],
            state="readonly",
            width=18,
        )
        self.cmb_limit.grid(row=0, column=3, sticky="ew")
        self.cmb_limit.bind("<<ComboboxSelected>>", lambda _e: self._update_options_summary())

        # Row 2: Visibility
        row2 = tk.Frame(self._opt_body, bg="#FFFFFF")
        row2.pack(fill="x", pady=(0, 6))
        tk.Label(
            row2,
            text="공개 범위:",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            width=10,
            anchor="w",
        ).pack(side="left")
        self.vis_var = tk.StringVar(value=_VISIBILITY_CHOICES[0][1])
        self.cmb_vis = ttk.Combobox(
            row2,
            textvariable=self.vis_var,
            values=[lbl for _, lbl in _VISIBILITY_CHOICES],
            state="readonly",
            width=40,
        )
        self.cmb_vis.pack(side="left", fill="x", expand=True)
        self.cmb_vis.bind("<<ComboboxSelected>>", lambda _e: self._update_options_summary())

        # Row 2b: Quality profile
        row2b = tk.Frame(self._opt_body, bg="#FFFFFF")
        row2b.pack(fill="x", pady=(0, 8))
        tk.Label(
            row2b,
            text="음질 프리셋:",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            width=10,
            anchor="w",
        ).pack(side="left")
        self.profile_var = tk.StringVar(value=_PROFILE_CHOICES[0][1])
        self.cmb_prof = ttk.Combobox(
            row2b,
            textvariable=self.profile_var,
            values=[lbl for _, lbl in _PROFILE_CHOICES],
            state="readonly",
            width=40,
        )
        self.cmb_prof.pack(side="left", fill="x", expand=True)
        self.cmb_prof.bind("<<ComboboxSelected>>", lambda _e: self._on_profile_changed())

        # Row 3 & 4: Stacked wrap-safe checkbuttons (never clip horizontally!)
        self.cover_var = tk.BooleanVar(value=True)
        self.chk_cover = tk.Checkbutton(
            self._opt_body,
            text="스팀 학생 초상화 썸네일(cover_image) 자동 첨부",
            variable=self.cover_var,
            bg="#FFFFFF",
            fg="#334155",
            activebackground="#FFFFFF",
            font=("Malgun Gothic", 9),
            anchor="w",
            justify="left",
        )
        self.chk_cover.pack(fill="x", pady=(0, 2))
        bind_responsive_wrap(self.chk_cover, container=self._opt_body, pad=36, min_wrap=220)

        self.sync_var = tk.BooleanVar(value=True)
        self.chk_sync = tk.Checkbutton(
            self._opt_body,
            text="업로드 전 내 Fish Audio 계정 기존 모델 자동 동기화 (중복 방지)",
            variable=self.sync_var,
            bg="#FFFFFF",
            fg="#334155",
            activebackground="#FFFFFF",
            font=("Malgun Gothic", 9),
            anchor="w",
            justify="left",
        )
        self.chk_sync.pack(fill="x", pady=(0, 2))
        bind_responsive_wrap(self.chk_sync, container=self._opt_body, pad=36, min_wrap=220)

        self.replace_external_var = tk.BooleanVar(value=True)
        self.chk_replace_ext = tk.Checkbutton(
            self._opt_body,
            text="✨ 이미 링크 있는 학생도 내 계정 모델로 새로 만들기 (기존 외부 링크 자동 백업 후 교체 · 내 모델은 중복 방지)",
            variable=self.replace_external_var,
            command=lambda: self._refresh_inventory_banner(force_rescan=False),
            bg="#FFFFFF",
            fg="#BE185D",
            activebackground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
        )
        self.chk_replace_ext.pack(fill="x")
        bind_responsive_wrap(self.chk_replace_ext, container=self._opt_body, pad=36, min_wrap=220)

        # 3-C. Live Log Card
        log_card = tk.Frame(
            body,
            bg="#FFFFFF",
            highlightbackground="#E2E8F0",
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        log_card.pack(fill="both", expand=True)

        log_hdr = tk.Frame(log_card, bg="#FFFFFF")
        log_hdr.pack(fill="x", pady=(0, 6))
        tk.Label(
            log_hdr,
            text="📋 실시간 작업 로그",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        self.lbl_log_count = tk.Label(
            log_hdr,
            text="0줄",
            bg="#F1F5F9",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            padx=6,
            pady=1,
        )
        self.lbl_log_count.pack(side="left", padx=(6, 0))

        self.btn_clear_log = tk.Button(
            log_hdr,
            text="🧹 비우기",
            command=self._clear_log,
            bg="#F1F5F9",
            fg="#475569",
            activebackground="#E2E8F0",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=1,
            cursor="hand2",
        )
        self.btn_clear_log.pack(side="right")

        self.btn_copy_log = tk.Button(
            log_hdr,
            text="📋 복사",
            command=self._copy_log,
            bg="#E0F2FE",
            fg="#0369A1",
            activebackground="#BAE6FD",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=1,
            cursor="hand2",
        )
        self.btn_copy_log.pack(side="right", padx=(0, 4))

        self.autoscroll_var = tk.BooleanVar(value=True)
        self.chk_autoscroll = tk.Checkbutton(
            log_hdr,
            text="자동 스크롤",
            variable=self.autoscroll_var,
            bg="#FFFFFF",
            fg="#334155",
            activebackground="#FFFFFF",
            font=("Malgun Gothic", 8),
        )
        self.chk_autoscroll.pack(side="right", padx=(0, 6))

        log_wrap = tk.Frame(log_card, bg="#0F172A", padx=2, pady=2)
        log_wrap.pack(fill="both", expand=True)
        self.log_box = tk.Text(
            log_wrap,
            height=13,
            font=("Consolas", 9),
            bg="#0F172A",
            fg="#E2E8F0",
            insertbackground="#E2E8F0",
            selectbackground="#334155",
            selectforeground="#FFFFFF",
            wrap="word",
            relief="flat",
            bd=0,
            padx=6,
            pady=6,
            state="disabled",
        )
        log_sb = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="both", expand=True)

        self.log_box.tag_configure("err", foreground="#FCA5A5")
        self.log_box.tag_configure("ok", foreground="#86EFAC")
        self.log_box.tag_configure("warn", foreground="#FDE68A")
        self.log_box.tag_configure("info", foreground="#7DD3FC")

        self._update_api_badge()
        self._update_options_summary()
        self.scroll_body.refresh_scrollregion(defer=True)

    def _update_api_badge(self) -> None:
        has_key = bool(self._current_api_key())
        if has_key:
            self.lbl_api_badge.configure(text="🔑 API 키 연결됨", bg="#DCFCE7", fg="#166534")
        else:
            self.lbl_api_badge.configure(text="⚠️ API 키 미설정 (1단계 무료)", bg="#FEF3C7", fg="#92400E")

    def _update_options_summary(self) -> None:
        langs = _LANG_LABEL_TO_TUPLE.get(self.lang_var.get(), ("JP", "KR"))
        vis = _VIS_LABEL_TO_CODE.get(self.vis_var.get(), "private")
        prof = self._selected_quality_profile()
        lim = self._parse_limit()
        lim_txt = f"최대 {lim}개" if lim else "전체"
        if self._options_expanded:
            self.lbl_opt_summary.configure(text="")
        else:
            self.lbl_opt_summary.configure(text=f"({'+'.join(langs)} · {vis} · {prof} · {lim_txt})")

    def _toggle_options_card(self) -> None:
        self._options_expanded = not self._options_expanded
        if self._options_expanded:
            self._opt_body.pack(fill="x", pady=(8, 0))
            self.btn_toggle_opts.configure(text="접기 ▴")
        else:
            self._opt_body.pack_forget()
            self.btn_toggle_opts.configure(text="펼치기 ▾")
        self._update_options_summary()
        self.scroll_body.refresh_scrollregion(defer=False)

    def _on_profile_changed(self) -> None:
        self._update_options_summary()
        self._refresh_inventory_banner(force_rescan=False)

    def _on_manual_refresh(self) -> None:
        if self._running:
            return
        self._update_api_badge()
        self._refresh_inventory_banner(force_rescan=True)
        self.lbl_progress.configure(text="🔄 스팀 보이스 및 계정 연동 현황을 새로고침했습니다.")

    def _on_f5_refresh(self, _event: Any = None) -> None:
        self._on_manual_refresh()

    def _on_escape_key(self, _event: Any = None) -> str | None:
        try:
            focused = self.focus_get()
        except Exception:
            focused = None
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
        self._on_close()
        return "break"

    def _copy_log(self) -> None:
        try:
            content = self.log_box.get("1.0", tk.END).strip()
            if not content:
                self.lbl_progress.configure(text="복사할 로그 내용이 아직 없습니다.")
                return
            self.clipboard_clear()
            self.clipboard_append(content)
            self.lbl_progress.configure(text=f"📋 작업 로그 {self._log_line_count}줄을 클립보드에 복사했습니다.")
        except Exception:
            pass

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", tk.END)
        self.log_box.configure(state="disabled")
        self._log_line_count = 0
        self.lbl_log_count.configure(text="0줄")

    @staticmethod
    def _classify_log_tag(line: str) -> str | None:
        s = line.strip()
        if not s:
            return None
        if any(tok in s for tok in ("❌", "실패", "오류", "ERROR", "중단")):
            return "err"
        if any(tok in s for tok in ("✅", "완료", "성공", "💗")):
            return "ok"
        if any(tok in s for tok in ("⚠", "⏹", "중지", "건너뜀", "보류", "429", "403")):
            return "warn"
        if any(tok in s for tok in ("🚀", "📦", "🔄", "===", "---")):
            return "info"
        return None

    def _append_log(self, text: str) -> None:
        if self._closing:
            return
        active_key = self._active_api_key
        extra = (active_key,) if active_key else ()
        safe_text = _redact_secrets(str(text or ""), extra_secrets=extra)

        def _do() -> None:
            if self._closing or not self.winfo_exists():
                return
            tag = self._classify_log_tag(safe_text)
            self.log_box.configure(state="normal")
            if tag:
                self.log_box.insert(tk.END, safe_text + "\n", (tag,))
            else:
                self.log_box.insert(tk.END, safe_text + "\n")
            if bool(self.autoscroll_var.get()):
                self.log_box.see(tk.END)
            self.log_box.configure(state="disabled")
            self._log_line_count += 1
            self.lbl_log_count.configure(text=f"{self._log_line_count}줄")

        self._enqueue_ui(_do)

    def _selected_quality_profile(self) -> str:
        if hasattr(self, "profile_var"):
            return _PROF_LABEL_TO_CODE.get(self.profile_var.get(), DEFAULT_QUALITY_PROFILE)
        return DEFAULT_QUALITY_PROFILE

    def _refresh_inventory_banner(self, force_rescan: bool = False) -> None:
        self._update_api_badge()
        try:
            prof_key = self._selected_quality_profile()
            if force_rescan or prof_key not in self._scan_cache:
                with self._cfg_lock:
                    cfg_snap = copy.deepcopy(self.cfg)
                scan = scan_voiced_roster(
                    self.characters,
                    cfg_snap,
                    langs=("JP", "KR"),
                    game_dir=None,
                    quality_profile=prof_key,
                )
                self._scan_cache[prof_key] = dict(scan["summary"])
            s = self._scan_cache[prof_key]
            prof_tag = {
                "plus200": "Plus 200초 최고품질",
                "pro85": "85초 고품질",
                "standard45": "45초 표준",
            }.get(prof_key, prof_key)
            ext_jp = s.get("jp_external_linked", 0)
            ext_kr = s.get("kr_external_linked", 0)
            own_jp = s.get("jp_self_owned", 0)
            own_kr = s.get("kr_self_owned", 0)
            replace_ext = bool(self.replace_external_var.get()) if hasattr(self, "replace_external_var") else True
            ext_note = "교체예정" if replace_ext else "유지"
            self.lbl_inventory.configure(
                text=(
                    f"📦 스팀 보이스 현황 ({prof_tag}) — "
                    f"총 JP {s['jp_voiced']}명 · KR {s['kr_voiced']}명 음성 팩 감지됨"
                )
            )
            if hasattr(self, "lbl_stat_jp"):
                self.lbl_stat_jp.configure(
                    text=(
                        f"🇯🇵 JP {s['jp_voiced']}명  ·  추출 {s['jp_extracted']}  ·  "
                        f"내모델 {own_jp}  ·  외부링크 {ext_jp} ({ext_note})"
                    )
                )
            if hasattr(self, "lbl_stat_kr"):
                self.lbl_stat_kr.configure(
                    text=(
                        f"🇰🇷 KR {s['kr_voiced']}명  ·  추출 {s['kr_extracted']}  ·  "
                        f"내모델 {own_kr}  ·  외부링크 {ext_kr} ({ext_note})"
                    )
                )
        except Exception as exc:
            extra = (self._active_api_key,) if self._active_api_key else ()
            safe_exc = _redact_secrets(str(exc), extra_secrets=extra)
            self.lbl_inventory.configure(text=f"스팀 음성 현황 확인 실패: {safe_exc}")
        if hasattr(self, "scroll_body"):
            self.scroll_body.refresh_scrollregion(defer=True)

    def _set_running_ui(self, running: bool) -> None:
        self._running = running
        st = "disabled" if running else "normal"
        cmb_st = "disabled" if running else "readonly"
        self.btn_extract.configure(state=st)
        self.btn_sync.configure(state=st)
        self.btn_upload.configure(state=st)
        self.btn_stop.configure(state="normal" if running else "disabled")
        if hasattr(self, "btn_refresh_inv"):
            self.btn_refresh_inv.configure(state=st)
        for cmb_attr in ("cmb_lang", "cmb_limit", "cmb_vis", "cmb_prof"):
            cmb = getattr(self, cmb_attr, None)
            if cmb is not None:
                cmb.configure(state=cmb_st)
        for chk_attr in ("chk_cover", "chk_sync", "chk_replace_ext"):
            chk = getattr(self, chk_attr, None)
            if chk is not None:
                chk.configure(state=st)

    def _parse_limit(self) -> int | None:
        val = self.limit_var.get()
        for tok in val.split():
            digits = "".join(ch for ch in tok if ch.isdigit())
            if digits:
                n = int(digits)
                return n if n > 0 else None
        return None

    def _start_run(self, mode: str) -> None:
        global _ACTIVE_BATCH_RUNNING
        if self._running:
            return
        if is_batch_worker_active():
            messagebox.showwarning(
                "일괄 작업 진행 중",
                "이전 일괄 작업이 아직 백그라운드에서 진행/정리 중입니다.\n잠시 후 다시 시도해 주세요.",
                parent=self,
            )
            return

        api_key = self._current_api_key()
        self._active_api_key = api_key
        self._update_api_badge()
        if mode in ("sync_only", "upload") and not api_key:
            messagebox.showwarning(
                "Fish Audio API Key 필요",
                "먼저 설정 창 1단계에 Fish Audio API Key를 입력해 주세요!\n"
                "(결제 전이라면 [1️⃣ 스팀 사전 추출 (무료)]은 API 키 없이 바로 실행할 수 있습니다.)",
                parent=self,
            )
            return

        langs = _LANG_LABEL_TO_TUPLE.get(self.lang_var.get(), ("JP", "KR"))
        vis = _VIS_LABEL_TO_CODE.get(self.vis_var.get(), "private")
        prof_key = self._selected_quality_profile()
        limit_n = self._parse_limit()
        include_cover = bool(self.cover_var.get())
        sync_before_upload = bool(self.sync_var.get())
        replace_ext = bool(self.replace_external_var.get())
        if mode == "upload":
            lim_str = f"최대 {limit_n}개" if limit_n else "대상 전체"
            ext_policy_str = (
                "• 이미 링크된 학생도 기존 외부 링크를 자동 백업한 뒤 내 계정 모델로 새로 생성합니다.\n"
                "• 이미 내 계정으로 만든 모델은 자동 건너뛰어 중복 생성하지 않습니다."
                if replace_ext
                else "• 이미 링크가 연결된 학생과 계정에 존재하는 모델은 모두 건너뜁니다."
            )
            if not messagebox.askyesno(
                "전 캐릭터 자동 업로드 시작",
                f"대상 언어: {', '.join(langs)}\n"
                f"공개 범위: {vis}\n"
                f"음질 프리셋: {prof_key}\n"
                f"업로드 수량: {lim_str}\n\n"
                f"{ext_policy_str}\n"
                "지금 일괄 업로드를 시작할까요?",
                parent=self,
            ):
                return

        with _ACTIVE_BATCH_LOCK:
            if _ACTIVE_BATCH_RUNNING:
                busy = True
            else:
                _ACTIVE_BATCH_RUNNING = True
                busy = False
        if busy:
            messagebox.showwarning(
                "일괄 작업 진행 중",
                "이전 일괄 작업이 아직 백그라운드에서 진행/정리 중입니다.\n잠시 후 다시 시도해 주세요.",
                parent=self,
            )
            return

        self._stop_event.clear()
        self._set_running_ui(True)
        self.prog_var.set(0.0)
        self.lbl_pct.configure(text="0%")
        self.lbl_progress.configure(text="작업 준비 중...")

        def _on_prog(p: BatchProgress) -> None:
            if self._closing:
                return
            pct = (100.0 * p.current / max(1, p.total)) if p.total > 0 else 0.0
            c = p.counts
            summary_txt = (
                f"[{p.current}/{p.total}] {p.name_kr} {p.lang} — "
                f"추출 {c.get('extracted', 0)} · 업로드 {c.get('uploaded', 0)} · "
                f"동기화 {c.get('synced', 0)} · 건너뜀 {c.get('skipped', 0)} · 실패 {c.get('failed', 0)}"
            )

            def _upd() -> None:
                if self._closing or not self.winfo_exists():
                    return
                clamped = min(100.0, max(0.0, pct))
                self.prog_var.set(clamped)
                self.lbl_pct.configure(text=f"{int(round(clamped))}%")
                self.lbl_progress.configure(text=summary_txt)

            self._enqueue_ui(_upd)

        def _on_linked_wrap(cid: str, lang_up: str, mid: str) -> None:
            bucket = "saved_voices_kr" if str(lang_up or "").upper() == "KR" else "saved_voices"
            with self._cfg_lock:
                self.cfg.setdefault(bucket, {})[cid] = mid
            if callable(self._on_model_linked):
                self._on_model_linked(cid, lang_up, mid)

        def _worker() -> None:
            global _ACTIVE_BATCH_RUNNING
            fatal_err_text = ""
            res_ok = False
            try:
                with self._cfg_lock:
                    cfg_snapshot = copy.deepcopy(self.cfg)
                res = run_fish_batch_pipeline(
                    mode=mode,
                    langs=langs,
                    visibility=vis,
                    quality_profile=prof_key,
                    api_key=api_key,
                    characters=self.characters,
                    cfg=cfg_snapshot,
                    limit=limit_n if mode == "upload" else None,
                    delay_sec=1.5,
                    include_cover=include_cover,
                    sync_before_upload=sync_before_upload,
                    replace_external=replace_ext,
                    stop_event=self._stop_event,
                    on_progress=_on_prog,
                    on_model_linked=_on_linked_wrap,
                    log_cb=self._append_log,
                    persist_voice_link_cb=self._persist_voice_link_cb,
                )
                res_ok = bool(res.get("ok"))
                if not res_ok and res.get("error"):
                    fatal_err_text = _redact_secrets(str(res["error"]), extra_secrets=(api_key,))
            except Exception as exc:
                fatal_err_text = _redact_secrets(f"예기치 못한 오류: {exc}", extra_secrets=(api_key,))
                self._append_log(f"❌ {fatal_err_text}")
            finally:
                with _ACTIVE_BATCH_LOCK:
                    _ACTIVE_BATCH_RUNNING = False

                def _done_ui() -> None:
                    if self._closing or not self.winfo_exists():
                        return
                    self._set_running_ui(False)
                    self._scan_cache.clear()
                    self._refresh_inventory_banner(force_rescan=True)
                    if fatal_err_text:
                        self.lbl_progress.configure(text=f"중단됨: {fatal_err_text[:90]}")
                    elif self._stop_event.is_set():
                        self.lbl_progress.configure(text="⏹ 안전 중지 완료 — 지금까지 완료된 항목은 자동 저장되었습니다.")
                    elif res_ok:
                        self.prog_var.set(100.0)
                        self.lbl_pct.configure(text="100%")
                        self.lbl_progress.configure(text="✅ 일괄 작업이 정상 완료되었습니다.")
                    if callable(self._on_finished):
                        try:
                            self._on_finished()
                        except Exception:
                            pass

                self._enqueue_ui(_done_ui)

        try:
            threading.Thread(target=_worker, daemon=True, name="fish-batch-ui-worker").start()
        except Exception:
            with _ACTIVE_BATCH_LOCK:
                _ACTIVE_BATCH_RUNNING = False
            self._set_running_ui(False)
            raise

    def _request_stop(self) -> None:
        if self._running and not self._stop_event.is_set():
            self._stop_event.set()
            self.btn_stop.configure(state="disabled")
            self.lbl_progress.configure(text="⏹ 안전 중지 요청됨 — 현재 진행 중인 항목만 마무리하고 저장합니다...")
            self._append_log("⏹ 안전 중지 요청됨 — 현재 작업만 마치고 진행 상태를 저장합니다...")

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(
                "작업 진행 중",
                "일괄 작업이 진행 중입니다. 안전 중지하고 창을 닫을까요?\n(지금까지 완료된 항목은 모두 자동 저장됩니다.)",
                parent=self,
            ):
                return
            self._stop_event.set()
        self._closing = True
        self.destroy()

    def destroy(self) -> None:
        self._closing = True
        if self._running:
            self._stop_event.set()
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
        super().destroy()
